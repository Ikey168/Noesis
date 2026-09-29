"""Migration and demographic statistics acquisition for the Economics ``demographics`` feature (#1914, M01/M03-M05).

One native connector, ``demographics``, fetches a bounded, declared list of
publications per source - one publication per page, one *release* (source
revision) per publication - and parses each into series whose values are
exactly as published. Supported documented formats:

* ``eurostat-jsonstat`` - Eurostat migration/asylum and population/demography
  datasets (``demo_pjan``, ``migr_pop1ctz``, ``migr_imm1ctz``, ``migr_asyappctza``,
  ``migr_asydcfsta``...) through the existing
  :class:`src.ingestion.connectors.dataset.eurostat.EurostatConnector`, one
  declared series per page, with the dataset's ``updated`` time as the
  provider vintage and every observation's status flag (provisional,
  estimated, break in series, confidential) kept;
* ``unhcr-population-json`` - the UNHCR Refugee Data Finder population API
  (``/population/v1/population/``), one row per year, country of origin and
  country of asylum, each declared population type its own series;
* ``iom-dtm-json`` - the IOM Displacement Tracking Matrix API (IDP figures per
  admin area and round; subscription key required);
* ``destatis-genesis-ffcsv`` - Destatis GENESIS-Online tables (12411, 12711)
  as flat-file CSV with the table's ``Updated`` stamp from the metadata call
  (credentialed);
* ``berlin-district-csv`` - Statistik Berlin-Brandenburg district tables in a
  declared column layout (Bezirk code, reference date, value columns).

BAMF publishes asylum figures as PDF reports only; they are never scraped and
enter as operator-recorded figure sheets (:func:`parse_operator_sheet`).

Nothing is recomputed: every value keeps its published text, flags and unit
beside the parsed decimal; definitions come from the declared definition and
the publication's own code lists (never from a dataset title); a column,
population type, unit or code the source contract does not declare is refused
as schema drift rather than partially read; a release is all-or-nothing.

``PROVIDER_CONTRACTS`` records the M01 access decisions (``unverified-live``
until a dated live run; ``not-implemented`` with a reason).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-demographic-release-v1"
SHEET_CONTRACT = "noesis-demographic-sheet-v1"
CONNECTOR = "demographics"
CONCEPTS = (
    "population_stock",
    "immigration_flow",
    "emigration_flow",
    "asylum_applications",
    "asylum_decisions",
    "refugee_stock",
    "asylum_seeker_stock",
    "idp_stock",
    "stateless_stock",
    "others_of_concern_stock",
    "displacement_stock",
)
MEASURES = ("stock", "flow")
BREAKDOWNS = ("citizenship", "country_of_birth", "country_of_origin", "none")
PROCEDURES = ("application", "decision", "none")
REFERENCE_KINDS = ("date", "period")
POPULATION_BASES = (
    "register",
    "census-2011",
    "census-2022",
    "estimate",
    "government-estimate",
    "unhcr-estimate",
    "not-stated",
)
# Geography code lists: the scheme a code belongs to and the levels it has. Codes are never translated across
# schemes; a series keeps the scheme, level and code-list version its publisher states.
GEOGRAPHY_SCHEMES = {
    "eu-country": ("country",),
    "nuts": ("nuts1", "nuts2", "nuts3"),
    "iso3166-1-alpha3": ("country",),
    "ags": ("land", "regierungsbezirk", "kreis", "gemeinde"),
    "berlin-bezirk": ("bezirk",),
    "cod-ab-pcode": ("admin1", "admin2"),
}
# Published units, the pint unit they normalise to and the exact scale (a rate is kept as a rate).
UNITS: dict[str, tuple[str | None, Decimal, str]] = {
    "persons": ("count", Decimal(1), "count"),
    "thousand persons": ("kilocount", Decimal(1000), "count"),
    "per 1000 persons": (None, Decimal(1), "rate"),
    "percent": (None, Decimal(1), "rate"),
}
FORMATS: dict[str, dict[str, Any]] = {
    "eurostat-jsonstat": {"provider": "eurostat", "jurisdiction": "EU"},
    "unhcr-population-json": {"provider": "unhcr", "jurisdiction": "INT"},
    "iom-dtm-json": {"provider": "iom-dtm", "jurisdiction": "INT"},
    "destatis-genesis-ffcsv": {"provider": "destatis", "jurisdiction": "DE"},
    "berlin-district-csv": {"provider": "statistik-bb", "jurisdiction": "DE-BE"},
    "operator-figure-sheet": {"provider": "bamf", "jurisdiction": "DE"},
}
# Eurostat dataset families selected in M01 (bounded coverage).
EUROSTAT_DATASETS = (
    "demo_pjan",
    "demo_r_pjanaggr3",
    "migr_pop1ctz",
    "migr_pop3ctb",
    "migr_imm1ctz",
    "migr_imm3ctb",
    "migr_emi1ctz",
    "migr_asyappctza",
    "migr_asyappctzm",
    "migr_asydcfsta",
)
# The Eurostat code-list dimension a declared breakdown needs in the cube.
BREAKDOWN_DIMENSIONS = {"citizenship": "citizen", "country_of_birth": "c_birth"}
# GENESIS special values (Zeichenerklärung): the published sign kept as the value text and a flag.
GENESIS_SIGNS = {
    "-": ("0", "nothing (exactly zero) as published"),
    ".": (None, "unknown or confidential"),
    "...": (None, "not yet available"),
    "x": (None, "not applicable"),
    "/": (None, "too uncertain to publish"),
}
REFERENCE_SCHEMES = ("celex", "eli", "ecli", "de-bgbl", "de-drucksache", "eu-procedure")
LINK_RELATIONS = ("defined_by", "reported_under", "referenced_in")
MAX_ROWS = 200_000
REVIEW_BOUNDARY = (
    "Published values only, each with its definition revision, unit, geography level, flags and source revision: "
    "series from different publishers, definitions or geography levels stay separate; no population projection, "
    "no causal claim about migration drivers or policy effect, no merged, averaged, netted, chained or apportioned "
    "value."
)

# M01 access decisions. Endpoints, layouts, identifiers and terms below are recorded
# from the providers' published documentation as known without network access;
# every item marked ``verify`` must be checked against the live terms and a
# published response before a dated live run is accepted (#2023).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eurostat": {
        "publisher": "Eurostat (migration and asylum; population and demography databases)",
        "delivers": [
            "population-stock",
            "immigration-flow",
            "emigration-flow",
            "asylum-applications",
            "asylum-decisions",
        ],
        "access": "JSON-stat 2.0 through the dissemination API "
        "(ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset}) via the existing Eurostat dataset "
        "connector; the same API family serves SDMX 2.1 (dataflow, datastructure, codelist) - the SDMX-ML data path "
        "stamps vintages with the retrieval time and needs the optional sdmx1 package, so data come through JSON-stat",
        "format": "eurostat-jsonstat",
        "authentication": "none",
        "rate_limits": "fair use, asynchronous delivery for large extractions (verify); one request per declared "
        "series per run",
        "pagination": "none; bounded by the declared dimension filters",
        "identifiers": "dataset codes (demo_pjan, demo_r_pjanaggr3, migr_pop1ctz, migr_pop3ctb, migr_imm1ctz, "
        "migr_imm3ctb, migr_emi1ctz, migr_asyappctza/m, migr_asydcfsta), dimension codes (citizen, c_birth, "
        "agedef, age, sex, unit, applicant, decision, geo), geo codes (country codes, NUTS)",
        "definitions": "stock (population on 1 January; migr_pop*) versus flow (immigration/emigration during the "
        "year); citizenship (citizen) versus country of birth (c_birth); asylum applications (applicant: first-time, "
        "subsequent) versus first-instance decisions (decision: total, positive, rejected...); reference date "
        "(1 January) versus reference period (calendar year or month)",
        "geography_levels": "country codes and NUTS 1/2/3 by NUTS version (NUTS 2021 current; verify per dataset)",
        "vintages": "the dataset's updated timestamp; each update may revise earlier periods",
        "flags": "p provisional, e estimated, b break in time series, c confidential, u low reliability, "
        "d definition differs, : not available (verify the current flag list)",
        "lowest_unit": "persons (NR); some rates per 1000 inhabitants",
        "cadence": "annual (population, migration flows, annual asylum), monthly (asylum applications)",
        "terms": "Eurostat reuse policy (Commission Decision 2011/833/EU), attribution required",
        "retained_evidence": "values with flags, the cube's dimension code lists and status labels, updated time",
        "legal_basis_note": "Regulation (EC) No 862/2007 (migration and international protection statistics) and "
        "Regulation (EU) No 1260/2013 (European demographic statistics) are named in the ESMS metadata (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified through the Eurostat connector; dataset codes, filters and flag coding need a "
        "dated live run",
    },
    "unhcr": {
        "publisher": "UNHCR Refugee Data Finder",
        "delivers": [
            "refugee-stock",
            "asylum-seeker-stock",
            "idp-stock",
            "stateless-stock",
            "others-of-concern-stock",
        ],
        "access": "REST API api.unhcr.org/population/v1/population/ with year, coo, coa, limit and page parameters "
        "(verify parameter names and the current base path)",
        "format": "unhcr-population-json",
        "authentication": "none",
        "rate_limits": "undocumented (verify); one request per declared selection per run",
        "pagination": "page/maxPages; a declared selection must fit one page (a multi-page answer is refused, "
        "never truncated)",
        "identifiers": "ISO 3166-1 alpha-3 country of origin (coo_iso) and country of asylum (coa_iso); UNHCR's "
        "own country codes (coo, coa) kept as published",
        "definitions": "population types as UNHCR defines them: refugees, asylum-seekers, IDPs, stateless persons, "
        "others of concern; year-end stocks (mid-year for mid-year releases), never flows",
        "geography_levels": "country only",
        "vintages": "no dataset timestamp in the response; the release (Global Trends, Mid-Year Trends) is declared "
        "with its publication date, or the HTTP Last-Modified header is used; otherwise the response is refused",
        "coverage_notes": "partial-year coverage, government versus UNHCR estimates, rounding and methodology changes "
        "are stated on the methodology pages and declared per selection (verify wording)",
        "lowest_unit": "persons",
        "cadence": "annual (June) and mid-year (November)",
        "terms": "UNHCR terms of use; attribution required (verify reuse terms)",
        "retained_evidence": "rows as published, declared coverage notes, response digest and URL",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; field names (coo_iso, coa_iso, population type fields) must be verified",
    },
    "iom-dtm": {
        "publisher": "IOM Displacement Tracking Matrix",
        "delivers": ["displacement-stock"],
        "access": "DTM API (dtmapi.iom.int) IDP admin 0/1 endpoints with a subscription key "
        "(Ocp-Apim-Subscription-Key); verify version path and field names",
        "format": "iom-dtm-json",
        "authentication": "subscription key (required-secret NOESIS_IOM_DTM_KEY)",
        "rate_limits": "per-subscription quota (verify); one request per declared selection per run",
        "pagination": "none for the declared admin-level selection (verify)",
        "identifiers": "operation, admin0Pcode (ISO3), admin1Pcode (COD-AB p-code), roundNumber, reportingDate",
        "definitions": "IDPs present (individuals) per round and admin area; a stock at the reporting date",
        "geography_levels": "admin 0 (country) and admin 1 (COD-AB p-codes, version as stated by the operation)",
        "vintages": "each round is published once; the acquisition release is dated by the declared publication "
        "date or Last-Modified",
        "coverage_notes": "rounds cover assessed locations only (partial coverage); declared per selection",
        "lowest_unit": "individuals",
        "cadence": "per operation round",
        "terms": "IOM DTM terms of use; attribution and non-commercial conditions for some datasets (verify)",
        "retained_evidence": "rows as published, round numbers, response digest and URL",
        "access_decision": "unverified-live",
        "reason": "credentialed API; fixture-verified parser only, the key, endpoint path and fields need a dated "
        "live run. HDX and portal CSV downloads are not scraped",
    },
    "bamf": {
        "publisher": "Bundesamt für Migration und Flüchtlinge (BAMF)",
        "delivers": ["asylum-applications", "asylum-decisions"],
        "access": "Asylgeschäftsstatistik and 'Aktuelle Zahlen' as PDF reports on bamf.de; no documented "
        "machine-readable export (verify)",
        "format": "operator-figure-sheet",
        "authentication": "none",
        "identifiers": "report month, table and page",
        "definitions": "first-time and follow-up applications (Erst- und Folgeanträge) versus decisions by outcome "
        "(Rechtsstellung als Flüchtling, subsidiärer Schutz, Abschiebungsverbot, Ablehnung, formelle "
        "Entscheidungen); monthly and cumulative periods",
        "geography_levels": "Germany (country)",
        "vintages": "each monthly report; later reports revise cumulative figures",
        "lowest_unit": "persons (applications and decisions counted per person)",
        "terms": "reuse of official publications with attribution (verify)",
        "retained_evidence": "report URL, page and table locator and the figures an operator records",
        "access_decision": "not-implemented",
        "reason": "PDF-only publication; never scraped. Figures enter as operator-recorded figure sheets with the "
        "report, page and table locator. Machine-readable German asylum series come from Eurostat migr_asy* "
        "(BAMF-reported)",
    },
    "destatis": {
        "publisher": "Statistisches Bundesamt (Destatis), GENESIS-Online",
        "delivers": ["population-stock", "immigration-flow", "emigration-flow"],
        "access": "GENESIS-Online REST API 2020 (www-genesis.destatis.de/genesisWS/rest/2020): metadata/table for "
        "the table's Updated stamp and data/tablefile?format=ffcsv for values; credentials (registered user or API "
        "token) in request headers (verify header names after the 2024 authentication change)",
        "format": "destatis-genesis-ffcsv",
        "authentication": "registered user or token (required-secret NOESIS_DESTATIS_GENESIS_TOKEN)",
        "rate_limits": "undocumented; large tables are delivered as background jobs (verify); one bounded table "
        "per declared document per run",
        "pagination": "none; the declared table selection is one file",
        "identifiers": "table codes 12411-0001..0014 (population by Land, age, sex, nationality) and 12711-0001.. "
        "(migration between Germany and abroad); Merkmal codes (DLAND = AGS Land code, NAT, GES, ALT...) and value "
        "codes (BEVSTD, BEV081, BEV082)",
        "definitions": "12411: population stock on 31 December (Fortschreibung on a census base; census 2011 or "
        "2022 base is part of the definition); 12711: immigration and emigration flows across the German border "
        "during the year, by nationality",
        "geography_levels": "Germany and Länder by AGS (Gebietsstand of the table; verify)",
        "vintages": "the table's Updated stamp (Stand); a census-base switch is a definition revision",
        "flags": "Zeichenerklärung: - nothing (exactly zero), . unknown or confidential, ... not yet available, "
        "x not applicable, / too uncertain",
        "lowest_unit": "persons",
        "cadence": "annual (quarterly for some 12411 tables)",
        "terms": "Datenlizenz Deutschland - Namensnennung 2.0 (verify)",
        "retained_evidence": "values with signs, table code and Updated stamp, response digests and URLs",
        "access_decision": "unverified-live",
        "reason": "credentialed API; fixture-verified parser only, the header names, ffcsv columns and sign coding "
        "need a dated live run",
    },
    "statistik-bb": {
        "publisher": "Amt für Statistik Berlin-Brandenburg",
        "delivers": ["population-stock", "immigration-flow", "emigration-flow"],
        "access": "district tables (Einwohnerregisterstatistik, Bevölkerungsfortschreibung) as CSV downloads on "
        "statistik-berlin-brandenburg.de / daten.berlin.de in a declared column layout (verify files and columns); "
        "PDF Statistische Berichte are not scraped",
        "format": "berlin-district-csv",
        "authentication": "none",
        "rate_limits": "undocumented; one bounded download per declared file per run",
        "pagination": "none",
        "identifiers": "Bezirk codes 01-12 (ALKIS gem 001-012), reference date",
        "definitions": "register-based residents (Einwohnerregister) versus census-based population "
        "(Fortschreibung auf Basis Zensus 2011 / 2022): the base is part of the definition revision",
        "geography_levels": "Berlin districts (Bezirke, 2001 district reform); LOR areas not selected",
        "vintages": "each file's Stand (declared or Last-Modified)",
        "lowest_unit": "persons",
        "cadence": "half-yearly (register), annual (Fortschreibung)",
        "terms": "CC BY 3.0 DE / Datenlizenz Deutschland (verify per dataset)",
        "retained_evidence": "values as published, file digest and URL",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; the CSV layout and file URLs must be verified",
    },
}


class DemographicFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    return text or None


def geography_code(scheme: str, value: Any) -> str | None:
    """A geography code normalised the same way on every side of a match (series, boundary, place)."""
    text = "".join(str(value or "").split()).upper()
    if not text:
        return None
    if scheme == "berlin-bezirk":
        digits = "".join(ch for ch in text if ch.isdigit())
        return digits.zfill(3)[-3:] if digits else None
    if scheme == "ags":
        return "".join(ch for ch in text if ch.isdigit()) or None
    return text


def parse_number(text: Any, number_format: str = "plain") -> Decimal | None:
    raw = "".join(str(text if text is not None else "").split())
    if not raw:
        return None
    if number_format == "de":
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise DemographicFormatError(
            "schema_drift", f"value {text!r} is not a number"
        ) from exc
    if not value.is_finite():
        raise DemographicFormatError("schema_drift", "value is not finite")
    return value


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:10], pattern).date().isoformat()
        except ValueError:
            continue
    return None


def _period(value: Any) -> str:
    """A published reference period or date in ISO form: YYYY, YYYY-MM, YYYY-Qn or YYYY-MM-DD."""
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}(-(0[1-9]|1[0-2]|Q[1-4]))?", text):
        return text
    day = _day(text)
    if day:
        return day
    match = re.fullmatch(r"(\d{4})M(\d{2})", text)
    if match:
        return f"{match[1]}-{match[2]}"
    raise DemographicFormatError(
        "schema_drift", f"reference period {value!r} is not a declared form"
    )


def check_unit(unit: Mapping[str, Any] | None) -> dict[str, str]:
    """The published unit (code and label); an undeclared unit is refused, never guessed."""
    unit = dict(unit or {})
    label = _clean(unit.get("label"))
    if label not in UNITS:
        raise DemographicFormatError(
            "schema_drift", f"unit {unit.get('label')!r} is not a declared unit"
        )
    return {"code": str(unit.get("code") or label), "label": label}


def check_definition(definition: Mapping[str, Any]) -> dict[str, Any]:
    """A declared definition: concept, stock or flow, breakdown, procedure, reference and base."""
    definition = dict(definition or {})
    reference = dict(definition.get("reference") or {})
    if (
        definition.get("concept") not in CONCEPTS
        or definition.get("measure") not in MEASURES
        or definition.get("breakdown", "none") not in BREAKDOWNS
        or definition.get("procedure", "none") not in PROCEDURES
        or reference.get("kind") not in REFERENCE_KINDS
        or definition.get("population_base", "not-stated") not in POPULATION_BASES
    ):
        raise DemographicFormatError(
            "invalid_definition",
            "a definition names a concept, stock or flow, breakdown, procedure, reference kind and base",
        )
    stock_concepts = {c for c in CONCEPTS if c.endswith("_stock")}
    if (definition["concept"] in stock_concepts) != (definition["measure"] == "stock"):
        raise DemographicFormatError(
            "invalid_definition", "a stock concept is measured as a stock"
        )
    if (definition["measure"] == "stock") != (reference["kind"] == "date"):
        raise DemographicFormatError(
            "invalid_definition",
            "a stock has a reference date and a flow a reference period",
        )
    return {
        "concept": definition["concept"],
        "measure": definition["measure"],
        "breakdown": definition.get("breakdown", "none"),
        "procedure": definition.get("procedure", "none"),
        "reference": {"kind": reference["kind"], "label": reference.get("label")},
        "population_base": definition.get("population_base", "not-stated"),
        "metadata_url": definition.get("metadata_url"),
        "source_text": _clean(definition.get("source_text")),
    }


def check_geography(level: Mapping[str, Any] | None) -> dict[str, Any]:
    level = dict(level or {})
    if level.get("scheme") not in GEOGRAPHY_SCHEMES or level.get(
        "level"
    ) not in GEOGRAPHY_SCHEMES.get(level.get("scheme"), ()):
        raise DemographicFormatError(
            "invalid_geography",
            "a geography level names a known code list and one of its levels",
        )
    return {
        "scheme": level["scheme"],
        "level": level["level"],
        "code_list_version": _clean(level.get("code_list_version")),
    }


def _observation(
    period: str,
    value_text: Any,
    *,
    flags: Sequence[str] = (),
    number_format: str = "plain",
    labels: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    text = None if value_text is None else str(value_text).strip()
    flags = [f for f in flags if f]
    if text in GENESIS_SIGNS:
        value, label = GENESIS_SIGNS[text]
        return {
            "period": period,
            "value_text": text,
            "value": value,
            "flags": [text, *flags],
            "flag_labels": {text: label, **{f: (labels or {}).get(f) for f in flags}},
        }
    number = parse_number(text, number_format) if text not in (None, "") else None
    return {
        "period": period,
        "value_text": text or None,
        "value": None if number is None else str(number),
        "flags": list(flags) if number is not None else [*flags, "not_published"],
        "flag_labels": {f: (labels or {}).get(f) for f in flags}
        | ({} if number is not None else {"not_published": "no value published"}),
    }


def _series(
    *,
    series_code: str,
    indicator: str,
    dimensions: Mapping[str, Any],
    geography: Mapping[str, Any],
    definition: Mapping[str, Any],
    unit: Mapping[str, Any],
    frequency: str,
    observations: list[dict[str, Any]],
    coverage_notes: Sequence[Any] = (),
    locator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    periods = [o["period"] for o in observations]
    if len(periods) != len(set(periods)):
        raise DemographicFormatError(
            "schema_drift", "a series states the same period twice"
        )
    return {
        "series_code": series_code,
        "indicator": indicator,
        "dimensions": {str(k): str(v) for k, v in sorted(dict(dimensions).items())},
        "geography": dict(geography),
        "definition": dict(definition),
        "unit": dict(unit),
        "frequency": frequency,
        "observations": sorted(observations, key=lambda o: o["period"]),
        "coverage_notes": [str(n) for n in coverage_notes if str(n).strip()],
        "locator": dict(locator or {}),
    }


def _geo(document: Mapping[str, Any], code: Any, label: Any = None) -> dict[str, Any]:
    level = check_geography(document.get("geography_level"))
    normalised = geography_code(level["scheme"], code)
    if not normalised:
        raise DemographicFormatError("schema_drift", "a row states no geography code")
    return {**level, "code": normalised, "label": _clean(label)}


# ---------------------------------------------------------------------- Eurostat


def parse_eurostat(
    raw: bytes, *, document: Mapping[str, Any], url: str
) -> dict[str, Any]:
    """One declared Eurostat series through the existing Eurostat connector, with flags and code lists kept."""
    from src.ingestion.connectors.dataset.base import RawSeries
    from src.ingestion.connectors.dataset.eurostat import EurostatConnector

    try:
        cube = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemographicFormatError(
            "schema_drift", "the response is not a JSON-stat document"
        ) from exc
    geo_index = (
        dict(
            dict(dict(cube.get("dimension") or {}).get("geo") or {}).get("category")
            or {}
        ).get("index")
        or {}
    )
    if set(geo_index) != {str(document["geography"])}:
        raise DemographicFormatError(
            "schema_drift", "the cube's geography is not the requested one"
        )
    connector = EurostatConnector(http_get=lambda _url: raw.decode("utf-8"))
    request = {
        "dataset": document["dataset"],
        "geography": document["geography"],
        **dict(document.get("filters") or {}),
        "series_key": "dimensions",
        "retain_status": True,
    }
    ref = next(iter(connector.discover(request)))
    try:
        records = connector.parse(
            RawSeries(
                ref=ref,
                content=raw.decode("utf-8"),
                content_type="application/json",
                source_url=url,
                fetched_at=0,
            )
        )
    except (ValueError, KeyError) as exc:
        raise DemographicFormatError(
            "schema_drift", f"JSON-stat cube could not be read: {exc}"
        ) from exc
    if len(records) != 1:
        raise DemographicFormatError(
            "schema_drift", "the cube has no time dimension or series"
        )
    record = records[0]
    meta = record.metadata
    if meta.get("vintage_basis") != "eurostat_dataset_updated":
        raise DemographicFormatError(
            "schema_drift", "the cube states no updated timestamp (provider vintage)"
        )
    if meta.get("unselected_multi_dimensions"):
        raise DemographicFormatError(
            "schema_drift",
            "the cube has several categories in unfiltered dimensions "
            f"({', '.join(meta['unselected_multi_dimensions'])}); declare a filter for each",
        )
    selected = dict(meta.get("selected_dimensions") or {})
    categories = dict(meta.get("dimension_categories") or {})
    definition = check_definition(document.get("definition"))
    needed = BREAKDOWN_DIMENSIONS.get(definition["breakdown"])
    if needed and needed not in selected:
        # The definition is checked against the dataset's own code lists, never read from its title.
        raise DemographicFormatError(
            "schema_drift",
            f"the declared breakdown needs the {needed!r} dimension, which the cube does not have",
        )
    unit = check_unit(document.get("unit"))
    if "unit" in selected and selected["unit"]["code"] != unit["code"]:
        raise DemographicFormatError(
            "schema_drift",
            f"the cube's unit {selected['unit']['code']!r} is not the declared {unit['code']!r}",
        )
    code_labels = {
        d: {
            "dimension_label": categories.get(d, {}).get("label"),
            "code": v["code"],
            "label": v["label"],
        }
        for d, v in sorted(selected.items())
        if d != "geo"
    }
    source_text = "; ".join(
        f"{v['dimension_label'] or d}: {v['label']} ({v['code']})"
        for d, v in code_labels.items()
    )
    definition = {
        **definition,
        "source_text": definition["source_text"] or source_text,
        "code_labels": code_labels,
    }
    status = dict(meta.get("observation_status") or {})
    labels = {str(k): str(v) for k, v in dict(meta.get("status_labels") or {}).items()}
    observations = []
    for obs in record.observations:
        flag = status.get(obs.period)
        flags = (
            list(flag)
            if flag and all(ch in labels for ch in flag)
            else ([flag] if flag else [])
        )
        value = None if obs.value is None else Decimal(str(obs.value))
        observations.append(
            _observation(
                obs.period,
                None if value is None else format(value, "f"),
                flags=flags,
                labels=labels,
            )
        )
    geo_label = dict(
        dict(dict(cube["dimension"]["geo"]).get("category") or {}).get("label") or {}
    ).get(str(document["geography"]))
    series = _series(
        series_code=str(document["dataset"]),
        indicator=str(document["dataset"]),
        dimensions={k: v["code"] for k, v in selected.items() if k not in {"geo"}},
        geography=_geo(document, document["geography"], geo_label),
        definition=definition,
        unit=unit,
        frequency=record.frequency,
        observations=observations,
        coverage_notes=document.get("coverage_notes") or [],
        locator={
            "dataset": document["dataset"],
            "filters": dict(document.get("filters") or {}),
        },
    )
    published = datetime.fromtimestamp(record.as_of / 1000, tz=timezone.utc)
    return {
        "published_on": published.date().isoformat(),
        "published_at": published.replace(tzinfo=None).isoformat(timespec="seconds"),
        "release_basis": "eurostat_dataset_updated",
        "series": [series],
        "structure": {
            "dataset": document["dataset"],
            "dimensions": categories,
            "status_labels": labels,
            "annotations": meta.get("cube_annotations") or [],
            "updated": cube.get("updated"),
        },
    }


# ---------------------------------------------------------------------- UNHCR


def parse_unhcr(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """UNHCR population rows: each declared population type of each origin/asylum pair is its own series."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemographicFormatError(
            "schema_drift", "the response is not JSON"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise DemographicFormatError("schema_drift", "the response has no items list")
    if int(payload.get("maxPages") or 1) > 1:
        # Never a truncated selection: a missing page would read as a missing population.
        raise DemographicFormatError(
            "input_limit",
            "the selection spans several pages; declare a narrower selection",
        )
    definitions = {
        str(k): check_definition(v)
        for k, v in dict(document.get("definitions") or {}).items()
    }
    if not definitions:
        raise DemographicFormatError(
            "invalid_definition", "a UNHCR selection declares its population types"
        )
    unit = check_unit(document.get("unit") or {"label": "persons"})
    notes = list(document.get("coverage_notes") or [])
    grouped: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    labels: dict[tuple[str, str], tuple[Any, Any]] = {}
    published_fields: set[str] = set()
    row_notes: dict[tuple[str, str], list[str]] = {}
    for item in payload["items"]:
        if not isinstance(item, dict):
            raise DemographicFormatError("schema_drift", "an item is not an object")
        published_fields |= set(item)
        missing = [k for k in ("year", "coo_iso", "coa_iso") if k not in item]
        missing += [k for k in definitions if k not in item]
        if missing:
            raise DemographicFormatError(
                "schema_drift", f"an item lacks declared fields {sorted(missing)}"
            )
        pair = (str(item["coa_iso"]).upper(), str(item["coo_iso"]).upper())
        labels[pair] = (item.get("coa_name"), item.get("coo_name"))
        if item.get("notes"):
            row_notes.setdefault(pair, []).append(str(item["notes"]))
        for key in definitions:
            value = item[key]
            text = None if value in (None, "", "-") else str(value)
            grouped.setdefault(pair, {}).setdefault(key, []).append(
                _observation(_period(item["year"]), text)
            )
    series = []
    for (coa, coo), by_type in sorted(grouped.items()):
        for key, observations in sorted(by_type.items()):
            series.append(
                _series(
                    series_code="population",
                    indicator=key,
                    dimensions={"country_of_origin": coo},
                    geography=_geo(document, coa, labels[(coa, coo)][0]),
                    definition=definitions[key],
                    unit=unit,
                    frequency="annual",
                    observations=observations,
                    coverage_notes=notes + row_notes.get((coa, coo), []),
                    locator={
                        "coa_iso": coa,
                        "coo_iso": coo,
                        "coo_name": labels[(coa, coo)][1],
                        "field": key,
                    },
                )
            )
    return {
        "published_on": None,
        "published_at": None,
        "release_basis": None,
        "series": series,
        "structure": {
            "published_fields": sorted(published_fields),
            "selected_population_types": sorted(definitions),
            "not_selected_fields": sorted(
                published_fields
                - set(definitions)
                - {
                    "year",
                    "coo_iso",
                    "coa_iso",
                    "coo",
                    "coa",
                    "coo_name",
                    "coa_name",
                    "coo_id",
                    "coa_id",
                    "notes",
                }
            ),
            "total": payload.get("total"),
        },
    }


# ---------------------------------------------------------------------- IOM DTM


def parse_dtm(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """DTM IDP rows per admin area and round: one series per operation and admin area."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemographicFormatError(
            "schema_drift", "the response is not JSON"
        ) from exc
    if not isinstance(payload, dict) or payload.get("isSuccess") is not True:
        raise DemographicFormatError(
            "schema_drift", "the response does not report success"
        )
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise DemographicFormatError("schema_drift", "the response has no result list")
    level = check_geography(document.get("geography_level"))
    code_key = "admin0Pcode" if level["scheme"] == "iso3166-1-alpha3" else "admin1Pcode"
    name_key = code_key.replace("Pcode", "Name")
    definition = check_definition(document.get("definition"))
    unit = check_unit(document.get("unit") or {"label": "persons"})
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    names: dict[tuple[str, str], Any] = {}
    for row in rows:
        missing = [
            k
            for k in (
                "operation",
                code_key,
                "reportingDate",
                "roundNumber",
                "numPresentIdpInd",
            )
            if k not in dict(row)
        ]
        if missing:
            raise DemographicFormatError(
                "schema_drift", f"a row lacks fields {sorted(missing)}"
            )
        key = (str(row["operation"]), str(row[code_key]))
        names[key] = row.get(name_key)
        day = _day(row["reportingDate"])
        if day is None:
            raise DemographicFormatError(
                "schema_drift", "a row states no reporting date"
            )
        observation = _observation(
            day,
            None
            if row["numPresentIdpInd"] in (None, "")
            else str(row["numPresentIdpInd"]),
        )
        observation["round"] = str(row["roundNumber"])
        grouped.setdefault(key, []).append(observation)
    series = [
        _series(
            series_code=f"dtm:{operation}",
            indicator="numPresentIdpInd",
            dimensions={"operation": operation},
            geography=_geo(document, code, names[(operation, code)]),
            definition=definition,
            unit=unit,
            frequency="per-round",
            observations=observations,
            coverage_notes=document.get("coverage_notes") or [],
            locator={"operation": operation, code_key: code},
        )
        for (operation, code), observations in sorted(grouped.items())
    ]
    return {
        "published_on": None,
        "published_at": None,
        "release_basis": None,
        "series": series,
        "structure": {"rows": len(rows), "admin_field": code_key},
    }


# ---------------------------------------------------------------------- Destatis GENESIS


def _csv(raw: bytes) -> list[dict[str, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise DemographicFormatError("schema_drift", "file has no header row")
    delimiter = max((";", ",", "\t"), key=lines[0].count)
    reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
    rows = []
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise DemographicFormatError(
                "schema_drift", "a row does not have the header's cells"
            )
        rows.append({str(k).strip(): (v or "").strip() for k, v in row.items()})
        if len(rows) > MAX_ROWS:
            raise DemographicFormatError(
                "input_limit", "file has more rows than the parser accepts"
            )
    return rows


def genesis_updated(meta_raw: bytes, *, table: str) -> tuple[str, str]:
    """The table's Updated stamp (Stand) from the GENESIS metadata/table answer."""
    try:
        meta = json.loads(meta_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DemographicFormatError(
            "schema_drift", "the metadata answer is not JSON"
        ) from exc
    status = dict(meta.get("Status") or {})
    obj = dict(meta.get("Object") or {})
    if int(status.get("Code", 0) or 0) != 0:
        raise DemographicFormatError(
            "schema_drift",
            f"GENESIS reported status {status.get('Code')}: {status.get('Content')}",
        )
    if obj.get("Code") != table:
        raise DemographicFormatError(
            "schema_drift", "the metadata answer is for another table"
        )
    text = str(obj.get("Updated") or "").strip().rstrip("h").strip()
    try:
        stamp = datetime.strptime(text, "%d.%m.%Y %H:%M:%S")
    except ValueError as exc:
        raise DemographicFormatError(
            "schema_drift", "the table states no Updated stamp (table version)"
        ) from exc
    return stamp.date().isoformat(), stamp.isoformat(timespec="seconds")


_VALUE_COLUMN = re.compile(r"^([A-Z0-9]+)__(.+?)__(.+)$")


def parse_genesis(
    data_raw: bytes, meta_raw: bytes, *, document: Mapping[str, Any]
) -> dict[str, Any]:
    """A GENESIS flat-file table: one series per value code and combination of Merkmal values."""
    table = str(document["table"])
    published_on, published_at = genesis_updated(meta_raw, table=table)
    rows = _csv(data_raw)
    if not rows:
        raise DemographicFormatError("schema_drift", "the table has no rows")
    columns = list(rows[0])
    value_columns = {
        c: _VALUE_COLUMN.match(c) for c in columns if _VALUE_COLUMN.match(c)
    }
    definitions = {
        str(k): check_definition(v)
        for k, v in dict(document.get("definitions") or {}).items()
    }
    stated = {m[1] for m in value_columns.values()}
    if not value_columns or stated != set(definitions):
        raise DemographicFormatError(
            "schema_drift",
            f"value columns {sorted(stated)} are not the declared {sorted(definitions)}",
        )
    for required in ("Statistik_Code", "Zeit"):
        if required not in columns:
            raise DemographicFormatError(
                "schema_drift", f"column {required} is missing"
            )
    merkmale = sorted(
        int(m[1]) for c in columns if (m := re.fullmatch(r"(\d+)_Merkmal_Code", c))
    )
    geo_attribute = str(document["geography_attribute"])
    units = dict(document.get("units") or {})
    grouped: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        if row["Statistik_Code"] != table.split("-")[0]:
            raise DemographicFormatError(
                "schema_drift", "a row belongs to another statistic"
            )
        dims: dict[str, str] = {}
        geo_code = geo_label = None
        for n in merkmale:
            code = row[f"{n}_Merkmal_Code"]
            value = row[f"{n}_Auspraegung_Code"]
            if code == geo_attribute:
                geo_code, geo_label = value, row.get(f"{n}_Auspraegung_Label")
            else:
                dims[code] = value
        if geo_code is None:
            raise DemographicFormatError(
                "schema_drift", f"a row has no {geo_attribute} geography code"
            )
        period = _period(row["Zeit"])
        for column, match in value_columns.items():
            key = (match[1], geo_code, tuple(sorted(dims.items())))
            entry = grouped.setdefault(
                key,
                {
                    "label": geo_label,
                    "value_label": match[2],
                    "unit_label": match[3],
                    "observations": [],
                },
            )
            entry["observations"].append(
                _observation(period, row[column], number_format="de")
            )
    series = []
    for (value_code, geo_code, dims), entry in sorted(grouped.items()):
        unit = check_unit(
            units.get(value_code)
            or {
                "code": entry["unit_label"],
                "label": {"Anzahl": "persons"}.get(entry["unit_label"]),
            }
        )
        definition = definitions[value_code]
        series.append(
            _series(
                series_code=table,
                indicator=value_code,
                dimensions=dict(dims),
                geography=_geo(document, geo_code, entry["label"]),
                definition={
                    **definition,
                    "source_text": definition["source_text"]
                    or f"{value_code}: {entry['value_label']} ({entry['unit_label']})",
                },
                unit=unit,
                frequency=str(document.get("frequency") or "annual"),
                observations=entry["observations"],
                coverage_notes=document.get("coverage_notes") or [],
                locator={"table": table, "value_column": value_code},
            )
        )
    return {
        "published_on": published_on,
        "published_at": published_at,
        "release_basis": "genesis_table_updated",
        "series": series,
        "structure": {
            "table": table,
            "columns": columns,
            "merkmale": [
                {
                    "code": rows[0][f"{n}_Merkmal_Code"],
                    "label": rows[0].get(f"{n}_Merkmal_Label"),
                }
                for n in merkmale
            ],
            "updated": published_at,
        },
    }


# ---------------------------------------------------------------------- Statistik Berlin-Brandenburg


def parse_berlin(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """A district table in the declared column layout: every column is declared, or the file is refused."""
    rows = _csv(raw)
    layout = dict(document.get("columns") or {})
    values = dict(layout.get("values") or {})
    declared = {
        layout.get("period"),
        layout.get("geography"),
        layout.get("geography_label"),
    } | set(values)
    declared.discard(None)
    if not rows:
        raise DemographicFormatError("schema_drift", "the file has no rows")
    if set(rows[0]) != declared:
        raise DemographicFormatError(
            "schema_drift",
            f"columns {sorted(rows[0])} are not the declared {sorted(declared)}",
        )
    definitions = {
        str(k): check_definition(v)
        for k, v in dict(document.get("definitions") or {}).items()
    }
    if set(values.values()) - set(definitions):
        raise DemographicFormatError(
            "invalid_definition", "every value column names a declared definition"
        )
    unit = check_unit(document.get("unit") or {"label": "persons"})
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        period = _period(row[layout["period"]])
        code = row[layout["geography"]]
        for column, key in values.items():
            entry = grouped.setdefault(
                (key, geography_code("berlin-bezirk", code) or ""),
                {
                    "code": code,
                    "label": row.get(layout.get("geography_label") or ""),
                    "observations": [],
                },
            )
            entry["observations"].append(
                _observation(period, row[column], number_format="de")
            )
    series = [
        _series(
            series_code=str(document.get("table") or "district-table"),
            indicator=key,
            dimensions={},
            geography=_geo(document, entry["code"], entry["label"]),
            definition=definitions[key],
            unit=unit,
            frequency=str(document.get("frequency") or "annual"),
            observations=entry["observations"],
            coverage_notes=document.get("coverage_notes") or [],
            locator={"column": next(c for c, k in values.items() if k == key)},
        )
        for (key, _code), entry in sorted(grouped.items())
    ]
    return {
        "published_on": None,
        "published_at": None,
        "release_basis": None,
        "series": series,
        "structure": {"columns": sorted(rows[0]), "rows": len(rows)},
    }


# ---------------------------------------------------------------------- operator figure sheets (BAMF)


def parse_operator_sheet(sheet: Mapping[str, Any]) -> dict[str, Any]:
    """Figures an operator recorded from a PDF-only publication, each with its page and table locator."""
    if (
        sheet.get("contract") != SHEET_CONTRACT
        or sheet.get("provider") not in FORMATS["operator-figure-sheet"]["provider"]
    ):
        raise DemographicFormatError(
            "invalid_sheet", "a figure sheet names its contract and provider"
        )
    release = dict(sheet.get("release") or {})
    if not str(release.get("url") or "").startswith("https://") or not _day(
        release.get("published_on")
    ):
        raise DemographicFormatError(
            "invalid_sheet",
            "a figure sheet names the publication (https URL, publication date)",
        )
    definitions = {
        str(k): check_definition(v)
        for k, v in dict(sheet.get("definitions") or {}).items()
    }
    document = {"geography_level": sheet.get("geography_level")}
    grouped: dict[tuple, dict[str, Any]] = {}
    for figure in sheet.get("figures") or []:
        figure = dict(figure)
        key = figure.get("definition")
        if key not in definitions or not dict(figure.get("locator") or {}).get("page"):
            raise DemographicFormatError(
                "invalid_sheet",
                "every figure names a declared definition and a page locator",
            )
        if definitions[key]["source_text"] is None:
            raise DemographicFormatError(
                "invalid_sheet", "every definition quotes the publication's own wording"
            )
        dims = {
            str(k): str(v)
            for k, v in sorted(dict(figure.get("dimensions") or {}).items())
        }
        group = (key, str(figure.get("geography")), tuple(dims.items()))
        entry = grouped.setdefault(
            group,
            {
                "observations": [],
                "locators": [],
                "series_code": figure.get("series_code"),
            },
        )
        observation = _observation(
            _period(figure.get("period")), figure.get("value_text"), number_format="de"
        )
        observation["locator"] = dict(figure["locator"])
        entry["observations"].append(observation)
    if not grouped:
        raise DemographicFormatError("invalid_sheet", "a figure sheet records figures")
    unit = check_unit(sheet.get("unit") or {"label": "persons"})
    series = [
        _series(
            series_code=str(
                entry["series_code"] or release.get("document") or "report"
            ),
            indicator=key,
            dimensions=dict(dims),
            geography=_geo(document, geo),
            definition=definitions[key],
            unit=unit,
            frequency=str(sheet.get("frequency") or "monthly"),
            observations=entry["observations"],
            coverage_notes=sheet.get("coverage_notes") or [],
            locator={"document": release.get("document")},
        )
        for (key, geo, dims), entry in sorted(grouped.items())
    ]
    return {
        "published_on": _day(release["published_on"]),
        "published_at": None,
        "release_basis": "declared_publication",
        "series": series,
        "structure": {
            "document": release.get("document"),
            "figures": len(sheet["figures"]),
        },
    }


# ---------------------------------------------------------------------- releases


def _last_modified(headers: Mapping[str, Any]) -> tuple[str | None, str | None]:
    value = headers.get("last-modified")
    if not value:
        return None, None
    try:
        stamp = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None, None
    return stamp.date().isoformat(), stamp.replace(tzinfo=None).isoformat(
        timespec="seconds"
    )


def finish_release(
    format_id: str,
    release: dict[str, Any],
    raw: bytes,
    *,
    document: Mapping[str, Any],
    headers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if release["published_on"] is None and document.get("published_on"):
        release["published_on"] = _day(document["published_on"])
        release["release_basis"] = "declared_publication"
    if release["published_on"] is None:
        release["published_on"], release["published_at"] = _last_modified(
            {str(k).casefold(): v for k, v in dict(headers or {}).items()}
        )
        release["release_basis"] = "http_last_modified"
    if release["published_on"] is None:
        # A retrieval time would pose as a publication; the response is refused instead.
        raise DemographicFormatError(
            "schema_drift", "the publication states no release date"
        )
    release.update(
        {
            "contract": RELEASE_CONTRACT,
            "provider": FORMATS[format_id]["provider"],
            "format": format_id,
            "jurisdiction": FORMATS[format_id]["jurisdiction"],
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": _digest(release["series"]),
            "item_count": len(release["series"]),
        }
    )
    return release


def parse_release(
    format_id: str,
    raw: bytes,
    *,
    document: Mapping[str, Any],
    headers: Mapping[str, Any] | None = None,
    url: str = "",
    metadata: bytes | None = None,
) -> dict[str, Any]:
    if format_id == "eurostat-jsonstat":
        release = parse_eurostat(raw, document=document, url=url)
    elif format_id == "unhcr-population-json":
        release = parse_unhcr(raw, document=document)
    elif format_id == "iom-dtm-json":
        release = parse_dtm(raw, document=document)
    elif format_id == "destatis-genesis-ffcsv":
        release = parse_genesis(raw, metadata or b"", document=document)
    elif format_id == "berlin-district-csv":
        release = parse_berlin(raw, document=document)
    else:
        raise DemographicFormatError(
            "schema_drift", f"unknown demographic format {format_id!r}"
        )
    return finish_release(
        format_id, release, raw + (metadata or b""), document=document, headers=headers
    )


def document_url(
    document: Mapping[str, Any], endpoint: str = "", *, part: str = "data"
) -> str:
    """The declared URL: Eurostat and GENESIS documents are addressed through their API's own URL form."""
    if document.get("url"):
        return str(document["url"])
    if document.get("dataset"):
        from src.ingestion.connectors.dataset.eurostat import EurostatConnector

        return EurostatConnector()._url(
            str(document["dataset"]),
            str(document.get("geography") or ""),
            dict(sorted(dict(document.get("filters") or {}).items())),
        )
    if document.get("table"):
        base = endpoint.rstrip("/")
        if part == "metadata":
            return f"{base}/metadata/table?" + urlencode(
                {"name": document["table"], "language": "de"}
            )
        return f"{base}/data/tablefile?" + urlencode(
            {
                "name": document["table"],
                "area": "all",
                "compress": "false",
                "format": "ffcsv",
                "language": "de",
            }
        )
    return ""


def demographic_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("demographics") or {})
    format_id = declared.get("format")
    if (
        format_id not in FORMATS
        or format_id == "operator-figure-sheet"
        or FORMATS[format_id]["provider"] != declared.get("provider")
    ):
        raise SourcePackError(
            "invalid_manifest",
            "demographic sources declare a matching provider and connector format",
        )
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError(
            "invalid_manifest", "a demographic source declares its documents"
        )
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError(
            "invalid_manifest", "more declared documents than the source's page budget"
        )
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    urls = [document_url(document, source["endpoint"]) for document in documents]
    if len(set(urls)) != len(urls):
        raise SourcePackError(
            "invalid_manifest", "each declared document is a distinct publication"
        )
    for document in documents:
        parts = urlsplit(document_url(document, source["endpoint"]))
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError(
                "invalid_manifest",
                "declared documents are HTTPS resources on the endpoint's host",
            )
        try:
            check_geography(document.get("geography_level"))
            for definition in (
                [document["definition"]]
                if document.get("definition")
                else list(dict(document.get("definitions") or {}).values())
            ):
                check_definition(definition)
            if document.get("unit"):
                check_unit(document["unit"])
        except DemographicFormatError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        if format_id == "eurostat-jsonstat" and (
            document.get("dataset") not in EUROSTAT_DATASETS
            or not document.get("geography")
            or not document.get("definition")
        ):
            raise SourcePackError(
                "invalid_manifest",
                "Eurostat documents name a selected dataset, a geography and a definition",
            )
        if format_id == "destatis-genesis-ffcsv" and (
            not re.fullmatch(r"(12411|12711)-\d{4}", str(document.get("table") or ""))
            or not document.get("geography_attribute")
        ):
            raise SourcePackError(
                "invalid_manifest",
                "GENESIS documents name a selected 12411 or 12711 table and its geography attribute",
            )
        for reference in document.get("references") or []:
            if (
                reference.get("scheme") not in REFERENCE_SCHEMES
                or reference.get("relation") not in LINK_RELATIONS
            ):
                raise SourcePackError(
                    "invalid_manifest",
                    "a publisher reference names a known scheme and relation",
                )
    return declared


def unverified(provider: str) -> bool:
    return (
        PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"
    )


class DemographicsAdapter:
    """Fetch the declared publications on the runtime's default transport; one page (one release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = demographic_declaration(self.source)
        self.secret = secret
        if transport is None:
            from functools import partial

            # The runtime's default transport: same-host public redirects only, a byte ceiling and the timeout.
            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "demographics": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "keyed": bool(secret),
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden",
                "demographic runs fetch the declared documents only",
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/csv, text/plain"}
        fmt = self.declared["format"]
        if fmt in {"iom-dtm-json", "destatis-genesis-ffcsv"}:
            if not self.secret:
                raise SourcePackError(
                    "authentication_failed", "this provider requires its credential"
                )
            if fmt == "iom-dtm-json":
                headers["Ocp-Apim-Subscription-Key"] = self.secret
            else:
                # GENESIS token login: the token as user name, an empty password (verify).
                headers["username"] = self.secret
                headers["password"] = ""
        return headers

    def _get(
        self, url: str, headers: Mapping[str, str]
    ) -> tuple[bytes, dict[str, Any], str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(url).hostname or "").casefold() != host or urlsplit(
            url
        ).scheme != "https":
            raise SourcePackError(
                "network_policy",
                "declared documents are fetched from the endpoint's host only",
            )
        from urllib.parse import parse_qsl

        base, _, query = url.partition("?")
        response = self.transport(
            url=base,
            params=dict(parse_qsl(query, keep_blank_values=True)),
            headers=dict(headers),
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "response was served from another host"
            )
        status = int(response.get("status", 200))
        response_headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "response exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(response_headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"request refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        return raw, response_headers, origin

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        fmt = self.declared["format"]
        headers = self._headers()
        endpoint = self.source["endpoint"]
        metadata = None
        origins = []
        if fmt == "destatis-genesis-ffcsv":
            metadata, _meta_headers, origin = self._get(
                document_url(document, endpoint, part="metadata"), headers
            )
            origins.append(origin)
        url = document_url(document, endpoint)
        raw, response_headers, origin = self._get(url, headers)
        origins.append(origin)
        try:
            release = parse_release(
                fmt,
                raw,
                document=document,
                headers=response_headers,
                url=url,
                metadata=metadata,
            )
        except DemographicFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if release["item_count"] > limit:
            # Never a truncated release: a missing series would read as a series without values.
            raise SourcePackError(
                "budget_exhausted",
                "release has more series than the run's result budget",
            )
        # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
        evidence_origin = "fixture" if set(origins) == {"fixture"} else "live"
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": release["provider"],
            "format": release["format"],
            "jurisdiction": release["jurisdiction"],
            "document": document,
            "published_on": release["published_on"],
            "published_at": release.get("published_at"),
            "release_basis": release["release_basis"],
            "file_sha256": release["file_sha256"],
            "content_sha256": release["content_sha256"],
            "item_count": release["item_count"],
            "structure": release.get("structure") or {},
            "evidence_origin": evidence_origin,
            "url": url,
        }
        records = [
            {
                "id": f"{release['file_sha256'][:16]}:{number}",
                "title": f"{document.get('label') or release['provider']} ({release['published_on']})",
                "url": url,
                "language": "de" if release["jurisdiction"].startswith("DE") else "en",
                "published_at": release["published_on"],
                "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
                "demographic_release": header,
                "demographic_series": item,
            }
            for number, item in enumerate(release["series"])
        ]
        receipt = {
            "status": 200,
            "provider": release["provider"],
            "document": document.get("label"),
            "published_on": release["published_on"],
            "release_basis": release["release_basis"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "evidence_origin": evidence_origin,
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: DemographicsAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        query = urlencode(sorted(dict(params or {}).items()))
        key = parts.path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = (
            body.encode()
            if isinstance(body, str)
            else b""
            if body is None
            else json.dumps(body).encode()
        )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            "origin": "fixture",
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def fixture_request(
    document: Mapping[str, Any], endpoint: str = "", *, part: str = "data"
) -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""
    from urllib.parse import parse_qsl

    parts = urlsplit(document_url(document, endpoint, part=part))
    query = urlencode(sorted(parse_qsl(parts.query)))
    return parts.path + ("?" + query if query else "")


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = DemographicsAdapter(
        source,
        transport=fixture_transport(list(fixture["native_pages"])),
        secret=FIXTURE_SECRET,
    )
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {
                "operation": min(source["operations"]),
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


def day_ms(day: str) -> int:
    return int(
        datetime.combine(
            date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc
        ).timestamp()
        * 1000
    )
