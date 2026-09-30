"""Education and research-institution statistics sources for the Science ``education-statistics`` feature (#2227).

Five providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`, ED01) and implemented as formats
of the ``education-statistics`` source-pack connector:

* **US IPEDS** (``ipeds``, format ``ipeds-csv-zip``) - NCES IPEDS Data Center complete data files (a zip holding
  one CSV per survey component and year) keyed by ``UNITID``. Each declared document names the file, the CSV
  member (a final release ships the revised ``_rv`` member), the survey year and the release stage
  (``provisional`` or ``final``); every value keeps its variable code, label and unit and the published imputation
  flag (the ``X``-prefixed column) verbatim. The directory file (``HD``) yields institution profiles.
* **ETER** (``eter``, format ``eter-csv``) - the European Tertiary Education Register export keyed by ETER ID with
  the national identifier and any identifier ETER publishes; ETER's special codes (``m`` missing, ``a`` not
  applicable, ``x``/``xc``/``xr`` included elsewhere, ``c`` confidential, ``nc`` not calculated) are stored as
  those codes, never as zero.
* **UNESCO UIS** (``unesco-uis``, format ``uis-json``) - country indicators through the UIS Data API with the UIS
  data version; qualifiers (UIS or national estimates) and footnotes are kept as comparability notes.
* **OECD Education at a Glance** (``oecd-eag``, format ``oecd-sdmx-csv``) - declared EAG dataflows through the
  existing :class:`~src.ingestion.connectors.dataset.sdmx.SDMXConnector` (SDMX-CSV URL builder and reader); each
  EAG edition is a vintage and ``OBS_STATUS`` and declared note attributes become comparability notes.
* **Eurostat R&D statistics** (``eurostat-rd``, format ``eurostat-sdmx-csv``) - GERD and R&D personnel by sector of
  performance through the SDMX connector's Eurostat (``ESTAT``) SDMX-CSV path, the Eurostat dissemination API
  that :class:`~src.ingestion.connectors.dataset.eurostat.EurostatConnector` also reads; the ``LAST UPDATE`` stamp
  dates the vintage and every ``OBS_FLAG`` is kept with its Eurostat meaning.

Every provider is ``unverified-live`` until a dated live run (ED14, #2444); endpoint, parameter and column names
marked *verify* come from the providers' public documentation. See
``docs/development/education-evidence/source-audit.md``. Nothing here ranks institutions, computes a quality or
composite score, harmonises definitions, converts currencies or derives per-student or per-staff ratios.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "education-statistics"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-education-release-v1"
NEVER_SENTENCE = (
    "Education and research statistics as each publisher released them: values with their definitions, units, "
    "reference periods and vintages side by side, never merged, averaged, harmonised or converted; no rankings, "
    "quality or composite scores and no derived per-student or per-staff ratios."
)
EXCLUSIONS = (
    "university rankings or league tables",
    "quality, performance or composite scores",
    "harmonising or merging definitions across publishers",
    "averaging or combining values from different sources",
    "currency conversion or rounding of published values",
    "derived per-student, per-staff or per-capita ratios",
    "estimating or imputing missing, suppressed or confidential values",
)

PROVIDER_HOSTS = {
    "ipeds": {"nces.ed.gov"},
    "eter": {"www.eter-project.com"},
    "unesco-uis": {"api.uis.unesco.org"},
    "oecd-eag": {"sdmx.oecd.org"},
    "eurostat-rd": {"ec.europa.eu"},
}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "ipeds": {
        "delivers": "US postsecondary institution statistics (enrolment, completions, staff, finance) keyed by UNITID",
        "access_decision": "unverified-live",
        "reason": "public complete data files without authentication; file names, member names and variable codes "
        "not yet checked live from this runtime",
        "access": "bulk file (zip holding one CSV) from the IPEDS Data Center",
        "entry_points": ["https://nces.ed.gov/ipeds/datacenter/data/{FILE}.zip (verify)"],
        "format": "CSV in a zip; variable dictionary published separately as a workbook (labels declared per document)",
        "authentication": "none",
        "rate_limits": "not documented; one download per declared file, bounded by the source's max_pages, "
        "max_bytes and max_results (institutions per file)",
        "identifiers": {
            "institution": "UNITID (six-digit IPEDS unit id); OPEID where the directory file states it",
            "country": "US (IPEDS covers US institutions and territories)",
        },
        "licence": "US federal government work (public domain, 17 U.S.C. 105); attribution to NCES IPEDS requested",
        "attribution": "U.S. Department of Education, National Center for Education Statistics, IPEDS",
        "update_cadence": "annual per survey component; provisional release first, final release about a year later",
        "revision_model": "a provisional file and the final file are distinct vintages; the final release supersedes "
        "the provisional one (earlier vintage kept); a final zip carries the revised _rv member",
        "temporal_semantics": "the declared release date of the provisional or final file dates the vintage; the "
        "survey year (fall term or fiscal year) is the reference period",
        "special_values": "imputation flags in X-prefixed columns (R reported, A not applicable, B left blank, "
        "G generated, L group median, N nearest neighbour, P carry forward, Z implied zero; verify) kept verbatim; "
        "declared suppression codes are stored as suppressed without value",
        "retained_evidence": "zip digest, member name, row numbers",
        "verify": ["file and member names per survey year", "variable codes and labels", "imputation flag codes"],
    },
    "eter": {
        "delivers": "European higher-education institution profiles and statistics keyed by ETER ID",
        "access_decision": "unverified-live",
        "reason": "ETER publishes exports and an API; registration and export URL shape not yet checked live",
        "access": "bulk file (CSV export) from the ETER portal",
        "entry_points": ["https://www.eter-project.com/ (export path declared per document; verify)"],
        "format": "CSV (delimiter declared per document)",
        "authentication": "none for published exports (verify whether the API requires registration)",
        "rate_limits": "not documented; one export per declared reference year",
        "identifiers": {
            "institution": "ETER ID (country prefix and number) with the national identifier ETER publishes",
            "country": "ISO 3166-1 alpha-2 as published (EL for Greece, UK for the United Kingdom)",
        },
        "licence": "ETER terms of use: reuse with attribution to ETER (verify the current licence text)",
        "attribution": "European Tertiary Education Register (ETER)",
        "update_cadence": "annual data release per reference year; later releases may revise earlier years",
        "revision_model": "each data release is a vintage; changed values add revisions",
        "temporal_semantics": "the declared data-release date dates the vintage; the reference year (academic year "
        "or calendar year per variable) is the period",
        "special_values": "m missing, a not applicable, x/xc/xr included elsewhere, c confidential, nc not "
        "calculated (verify); stored as codes, never zero",
        "retained_evidence": "file digest, row numbers",
        "verify": ["export path", "column names", "special codes", "licence wording"],
    },
    "unesco-uis": {
        "delivers": "country education indicators (enrolment, graduates, teachers, expenditure, R&D)",
        "access_decision": "unverified-live",
        "reason": "documented UIS Data API without authentication (verify); not yet run live from this runtime",
        "access": "api (UIS Data API, JSON)",
        "entry_points": ["https://api.uis.unesco.org/api/public/data/indicators (verify)",
                         "https://api.uis.unesco.org/api/public/versions (verify)"],
        "format": "JSON records with indicator metadata and footnotes",
        "authentication": "none (verify)",
        "rate_limits": "record limit per call (about 100000 records; verify); each document is one bounded call",
        "identifiers": {"geography": "UIS geoUnit (ISO 3166-1 alpha-3 for countries)",
                        "indicator": "UIS indicator codes with the ISCED 2011 level in the code"},
        "licence": "CC BY-SA 3.0 IGO for UIS data (verify)",
        "attribution": "UNESCO Institute for Statistics (UIS)",
        "update_cadence": "data releases several times a year, each with a version id",
        "revision_model": "each UIS data version is a vintage; revised values add revisions",
        "temporal_semantics": "the indicator metadata lastDataUpdate stamp, else the declared version date, dates "
        "the vintage; the year is the reference period",
        "special_values": "qualifiers (UIS_EST, NAT_EST; verify) and magnitudes kept as notes; footnotes quoted",
        "retained_evidence": "raw JSON digest per call",
        "verify": ["path and parameter names", "qualifier codes", "footnote field", "licence"],
    },
    "oecd-eag": {
        "delivers": "OECD Education at a Glance indicators by country and ISCED level",
        "access_decision": "unverified-live",
        "reason": "OECD SDMX REST API without authentication through the existing SDMX connector; EAG dataflow ids "
        "not yet run live",
        "access": "sdmx (OECD SDMX REST API, SDMX-CSV via SDMXConnector('OECD'))",
        "entry_points": ["https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}?format=csvfile "
                         "(verify)"],
        "format": "SDMX-CSV",
        "authentication": "none",
        "rate_limits": "OECD API fair-use limits (about 60 queries per hour; verify); one call per declared key",
        "identifiers": {"geography": "REF_AREA (ISO 3166-1 alpha-3)", "level": "EDUCATION_LEV (ISCED 2011)"},
        "licence": "OECD terms and conditions: reuse with attribution (verify)",
        "attribution": "OECD, Education at a Glance",
        "update_cadence": "annual edition (September) with updates between editions",
        "revision_model": "each EAG edition is a vintage; values revised between editions add revisions",
        "temporal_semantics": "the declared edition release date dates the vintage (LAST UPDATE when stated)",
        "special_values": "OBS_STATUS codes (M missing, L not collected, B break, E estimated; verify) and declared "
        "note attributes kept as comparability notes",
        "retained_evidence": "raw SDMX-CSV digest, row numbers",
        "verify": ["dataflow ids and versions", "dimension order", "attribute names"],
    },
    "eurostat-rd": {
        "delivers": "Eurostat R&D expenditure (GERD) and R&D personnel by sector of performance",
        "access_decision": "unverified-live",
        "reason": "Eurostat dissemination API without authentication through the SDMX connector's ESTAT SDMX-CSV "
        "path (the API the Eurostat connector reads); not yet run live",
        "access": "sdmx (Eurostat SDMX 2.1 dissemination API, SDMX-CSV via SDMXConnector('ESTAT'))",
        "entry_points": ["https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}"
                         "?format=SDMX-CSV (verify)"],
        "format": "SDMX-CSV with LAST UPDATE and OBS_FLAG",
        "authentication": "none",
        "rate_limits": "no published per-user limit (verify); one call per declared key",
        "identifiers": {"geography": "Eurostat GEO codes (EL, UK and aggregates kept as published)",
                        "sector": "sectperf (BES, GOV, HES, PNP, TOTAL)"},
        "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with attribution",
        "attribution": "Eurostat",
        "update_cadence": "dataset updates when new data or revisions arrive",
        "revision_model": "each dataset update (LAST UPDATE) is a vintage; revised observations add revisions",
        "temporal_semantics": "LAST UPDATE dates the vintage, else the declared release, else the retrieval time",
        "special_values": "OBS_FLAG p provisional, e estimated, b break, d definition differs, u low reliability, "
        "c confidential, z not applicable, : not available; kept as published",
        "retained_evidence": "raw SDMX-CSV digest, row numbers",
        "verify": ["dataset codes", "key order", "flag letters"],
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "note": "no dated live run from this runtime; offline "
               "fixtures only (ED14 #2444 records live evidence)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# The bounded first coverage (ED01). No record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "ipeds": {
        "components": {"HD": "directory (profiles)", "EF": "fall enrolment (EFTOTLT)", "C": "completions (CTOTALT)",
                       "S": "instructional staff (SISTOTL)", "F": "finance (total revenues, USD)"},
        "institutions": "a declared sample of UNITIDs (at most max_results per file)",
        "years": "the two most recent survey years, provisional and final releases",
    },
    "eter": {"variables": ["total ISCED 5-7 students", "academic staff FTE", "current expenditure (EUR)"],
             "institutions": "a declared sample of ETER IDs in DE and FR", "years": "two most recent reference years"},
    "unesco-uis": {"indicators": ["gross enrolment ratio, tertiary", "tertiary graduates", "tertiary teachers",
                                  "government expenditure on tertiary education as % of GDP", "GERD as % of GDP"],
                   "countries": ["DEU", "FRA", "USA"], "years": "two most recent years"},
    "oecd-eag": {"dataflows": ["expenditure on educational institutions as % of GDP (tertiary)"],
                 "countries": ["DEU", "FRA", "USA"], "editions": "the two most recent EAG editions"},
    "eurostat-rd": {"datasets": {"rd_e_gerdtot": "GERD by sector of performance (HES and TOTAL)",
                                 "rd_p_perssci": "R&D personnel by sector of performance (HES)"},
                    "countries": ["DE", "FR"], "years": "two most recent years"},
    "out_of_scope": "ranking tables, quality or performance scores and composite indices published by any source "
    "are never acquired or stored",
}

FORMATS = {
    "ipeds-csv-zip": {"provider": "ipeds", "kind": "institution_statistic"},
    "eter-csv": {"provider": "eter", "kind": "institution_statistic"},
    "uis-json": {"provider": "unesco-uis", "kind": "education_indicator"},
    "oecd-sdmx-csv": {"provider": "oecd-eag", "kind": "education_indicator"},
    "eurostat-sdmx-csv": {"provider": "eurostat-rd", "kind": "education_indicator"},
}
CONCEPTS = (
    "enrolment",
    "graduates",
    "staff",
    "teachers",
    "finance",
    "education_expenditure",
    "rd_expenditure",
    "rd_personnel",
    "enrolment_ratio",
)
STATUSES = ("reported", "missing", "not_applicable", "included_elsewhere", "confidential", "suppressed",
            "not_published")
STAGES = ("provisional", "final", "revised", "release", "edition", "update")
IPEDS_FLAGS = {
    "A": "not applicable",
    "B": "institution left item blank",
    "C": "analyst corrected reported value",
    "D": "do not know",
    "G": "data generated from other data values",
    "H": "value not derived - data not usable",
    "J": "logical imputation",
    "K": "ratio adjustment",
    "L": "imputed using the Group Median procedure",
    "N": "imputed using the Nearest Neighbor procedure",
    "P": "imputed using the Carry Forward procedure",
    "R": "reported",
    "Y": "specific professional practice program not applicable",
    "Z": "implied zero",
}
ETER_SPECIAL = {
    "m": ("missing", "missing"),
    "a": ("not_applicable", "not applicable"),
    "x": ("included_elsewhere", "included in another category"),
    "xc": ("included_elsewhere", "included in another column"),
    "xr": ("included_elsewhere", "included in another row"),
    "c": ("confidential", "confidential"),
    "nc": ("not_published", "not calculated"),
}
OECD_STATUS = {
    "M": ("missing", "missing value"),
    "L": ("missing", "missing value; data exist but were not collected"),
    "a": ("not_applicable", "category not applicable"),
    "m": ("missing", "missing"),
    "x": ("included_elsewhere", "data included in another category"),
    "B": ("reported", "break in series"),
    "E": ("reported", "estimated value"),
    "P": ("reported", "provisional value"),
    "A": ("reported", "normal value"),
}
EUROSTAT_FLAGS = {
    "p": "provisional",
    "e": "estimated",
    "b": "break in time series",
    "d": "definition differs",
    "u": "low reliability",
    "c": "confidential",
    "z": "not applicable",
    "n": "not significant",
    "r": "revised",
    "s": "Eurostat estimate",
    "f": "forecast",
}
UIS_QUALIFIERS = {"UIS_EST": "UIS estimation", "NAT_EST": "national estimation"}
# Flag letters that describe comparability and become notes; c and z decide the status instead.
EUROSTAT_NOTE_FLAGS = ("b", "d", "e", "p", "u", "s", "r", "f", "n")


class EducationFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unverified(provider: str) -> bool:
    return PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"


def text(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def decimal_text(value: Any) -> str | None:
    """A published number as exact decimal text; ``None`` for a missing or non-numeric value (never zero)."""
    raw = text(value)
    if raw is None:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f") if number == number.to_integral_value() else format(number, "f")


def iso_day(value: Any) -> str | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def iso_instant(value: Any) -> str | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).isoformat()


# ------------------------------------------------------------------ declarations


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")):
        raise EducationFormatError("invalid_document", "a document has a label")
    release = dict(document.get("release") or {})
    if release:
        if iso_day(release.get("published_on")) is None or not text(release.get("label")):
            raise EducationFormatError("invalid_document", "a declared release states its label and publication date")
        if release.get("stage") is not None and release.get("stage") not in STAGES:
            raise EducationFormatError("invalid_document", f"a release stage is one of {STAGES}")
    variables = dict(document.get("variables") or {})
    for code, variable in variables.items():
        variable = dict(variable)
        if variable.get("concept") not in CONCEPTS or not text(variable.get("label")) or not variable.get("unit"):
            raise EducationFormatError("invalid_document",
                                       f"variable {code} states its label, unit and one of the concepts {CONCEPTS}")
    if fmt == "ipeds-csv-zip":
        if not re.fullmatch(r"[A-Z0-9_]{2,20}", str(document.get("file") or "")) or not text(document.get("member")):
            raise EducationFormatError("invalid_document", "an IPEDS document names its data file and CSV member")
        if not document.get("unitids") or not all(re.fullmatch(r"\d{6}", str(u)) for u in document["unitids"]):
            raise EducationFormatError("invalid_document", "an IPEDS document declares six-digit UNITIDs")
        if not re.fullmatch(r"\d{4}", str(document.get("survey_year") or "")):
            raise EducationFormatError("invalid_document", "an IPEDS document states its survey year")
        if release.get("stage") not in {"provisional", "final", "revised"}:
            raise EducationFormatError("invalid_document", "an IPEDS release is provisional, final or revised")
        if not variables and not document.get("profile"):
            raise EducationFormatError("invalid_document", "an IPEDS document declares variables or profile columns")
    elif fmt == "eter-csv":
        columns = dict(document.get("columns") or {})
        if not text(document.get("url")) or not columns.get("id") or not columns.get("country"):
            raise EducationFormatError("invalid_document", "an ETER document declares its URL and id/country columns")
        if not re.fullmatch(r"\d{4}", str(document.get("reference_year") or "")) or not variables:
            raise EducationFormatError("invalid_document", "an ETER document states its reference year and variables")
    elif fmt == "uis-json":
        if not document.get("indicators") or not document.get("geo_units") or not text(document.get("version")):
            raise EducationFormatError("invalid_document", "a UIS document names indicators, geo units and a version")
        if set(map(str, document["indicators"])) != set(variables):
            raise EducationFormatError("invalid_document", "each UIS indicator is declared as a variable")
    elif fmt in {"oecd-sdmx-csv", "eurostat-sdmx-csv"}:
        if not text(document.get("flow")) or not text(document.get("indicator")):
            raise EducationFormatError("invalid_document", "an SDMX document names its dataflow and indicator")
        if str(document["indicator"]) not in variables:
            raise EducationFormatError("invalid_document", "the SDMX document's indicator is declared as a variable")
        if fmt == "oecd-sdmx-csv" and not release:
            raise EducationFormatError("invalid_document", "an Education at a Glance document declares its edition")


def document_url(fmt: str, document: Mapping[str, Any], endpoint: str = "") -> str:
    if fmt == "ipeds-csv-zip":
        base = str(endpoint or "https://nces.ed.gov/ipeds/datacenter/data").rstrip("/")
        return f"{base}/{document['file']}.zip"
    if fmt == "eter-csv":
        return str(document["url"])
    if fmt == "uis-json":
        base = str(endpoint or "https://api.uis.unesco.org/api/public").rstrip("/")
        params = [("indicator", str(i)) for i in document["indicators"]]
        params += [("geoUnit", str(g)) for g in document["geo_units"]]
        params += [(k, str(document[k])) for k in ("start", "end") if document.get(k)]
        params += [("indicatorMetadata", "true"), ("footnotes", "true"), ("version", str(document["version"]))]
        return f"{base}/data/indicators?" + urlencode(sorted(params))
    if fmt in {"oecd-sdmx-csv", "eurostat-sdmx-csv"}:
        from src.ingestion.connectors.dataset.sdmx import SDMXConnector

        try:
            url, query = SDMXConnector("OECD" if fmt == "oecd-sdmx-csv" else "ESTAT").csv_url(
                str(document["flow"]), str(document.get("key") or ""), dict(document.get("params") or {})
            )
        except ValueError as exc:
            raise EducationFormatError("invalid_document", str(exc)) from exc
        return url + "?" + urlencode(sorted(query.items()))
    raise EducationFormatError("invalid_document", f"no URL for format {fmt}")


def education_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("education_statistics") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "education sources declare a known provider and its runtime format")
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "an education source declares its documents")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared documents than the source's page budget")
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError("invalid_manifest", "the endpoint is not the provider's documented host")
    urls = []
    for document in documents:
        try:
            check_document(fmt, document)
            url = document_url(fmt, document, source["endpoint"])
        except EducationFormatError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return declared


# ------------------------------------------------------------------ parsing helpers


def _variable(code: str, declared: Mapping[str, Any], published_label: str | None = None) -> dict[str, Any]:
    declared = dict(declared)
    return {
        "code": str(code),
        "label": text(published_label) or declared["label"],
        "concept": declared["concept"],
        "definition": text(declared.get("definition")),
        "definition_ref": text(declared.get("definition_ref")),
    }


def _unit(declared: Mapping[str, Any]) -> dict[str, Any]:
    unit = dict(declared.get("unit") or {})
    return {k: str(v) for k, v in unit.items() if v is not None}


def _isced(declared: Mapping[str, Any], published: str | None = None) -> dict[str, Any] | None:
    level = text(published) or text(dict(declared.get("isced") or {}).get("level"))
    if level is None:
        return None
    return {"scheme": "ISCED 2011", "level": level, "label": dict(declared.get("isced") or {}).get("label")}


def _release(document: Mapping[str, Any], default_stage: str) -> tuple[str | None, str | None, str]:
    release = dict(document.get("release") or {})
    return iso_day(release.get("published_on")), text(release.get("label")), str(release.get("stage") or default_stage)


def _finish(provider: str, fmt: str, raw: bytes, items: list[dict[str, Any]], *, published_on, published_at, basis,
            label, stage, structure) -> dict[str, Any]:
    for item in items:
        if item.get("observations") is not None:
            item["observations"].sort(key=lambda o: o["period"])
            periods = [o["period"] for o in item["observations"]]
            if len(set(periods)) != len(periods):
                raise EducationFormatError("schema_drift", "a series states the same period twice")
        if item.get("notes") is not None:
            item["notes"].sort(key=lambda n: (str(n.get("period") or ""), n["kind"], str(n.get("code") or "")))
    items.sort(key=lambda i: canonical([i["record_kind"], i["subject"], i.get("indicator"), i.get("dimensions")]))
    return {
        "provider": provider,
        "format": fmt,
        "items": items,
        "item_count": len(items),
        "published_on": published_on,
        "published_at": published_at,
        "release_basis": basis,
        "release_label": label,
        "release_stage": stage,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(items),
        "structure": structure,
    }


def _read_csv(body: bytes, *, encoding: str = "utf-8-sig", delimiter: str = ",") -> tuple[list[str], list[dict]]:
    try:
        reader = csv.DictReader(io.StringIO(body.decode(encoding)), delimiter=delimiter)
    except (UnicodeDecodeError, LookupError) as exc:
        raise EducationFormatError("schema_drift", "the CSV is not in the declared encoding") from exc
    header = [h.strip() for h in reader.fieldnames or []]
    rows = [{str(k).strip(): v for k, v in row.items()} for row in reader]
    return header, rows


# ------------------------------------------------------------------ IPEDS


def parse_ipeds(raw: bytes, *, document: Mapping[str, Any], max_rows: int = 200) -> dict[str, Any]:
    """One IPEDS complete data file: a series per (UNITID, variable) for the survey year, values and imputation flags
    as published; the directory file yields institution profiles."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise EducationFormatError("schema_drift", "the IPEDS file is not a zip archive") from exc
    member = str(document["member"])
    if member not in archive.namelist():
        raise EducationFormatError("schema_drift", f"the IPEDS archive lacks the declared member {member}")
    if archive.getinfo(member).file_size > 50_000_000:
        raise EducationFormatError("input_limit", "the IPEDS CSV is larger than allowed")
    header, rows = _read_csv(archive.read(member), encoding=str(document.get("encoding") or "utf-8-sig"))
    if "UNITID" not in header:
        raise EducationFormatError("schema_drift", "the IPEDS CSV has no UNITID column")
    variables = dict(document.get("variables") or {})
    profile = dict(document.get("profile") or {})
    missing = [c for c in [*variables, *profile.values()] if c not in header]
    if missing:
        raise EducationFormatError("schema_drift", f"the IPEDS CSV lacks declared columns {missing}")
    filters = {str(k): str(v) for k, v in dict(document.get("filters") or {}).items()}
    if [c for c in filters if c not in header]:
        raise EducationFormatError("schema_drift", "the IPEDS CSV lacks a declared filter column")
    suppression = {str(k): str(v) for k, v in dict(document.get("suppression_codes") or {}).items()}
    wanted = {str(u) for u in document["unitids"]}
    period = str(document.get("period") or document["survey_year"])
    items: list[dict[str, Any]] = []
    matched = set()
    for number, row in enumerate(rows, start=2):
        unitid = text(row.get("UNITID"))
        if unitid not in wanted or any(text(row.get(c)) != v for c, v in filters.items()):
            continue
        if unitid in matched:
            raise EducationFormatError("schema_drift", f"UNITID {unitid} appears twice after the declared filters")
        matched.add(unitid)
        if len(matched) > max_rows:
            raise EducationFormatError("input_limit", "more institutions than the source's result budget")
        subject = {"scheme": "ipeds-unitid", "code": unitid, "country": "US"}
        if profile:
            for key, column in profile.items():
                if text(row.get(column)) is not None:
                    subject["label" if key == "name" else key] = text(row.get(column))
            stated = {k[4:]: text(row.get(c)) for k, c in profile.items() if k.startswith("ids.") and text(row.get(c))}
            if stated:
                subject["stated_ids"] = stated
                for key in list(subject):
                    if key.startswith("ids."):
                        subject.pop(key)
            items.append({"record_kind": "institution_profile", "subject": subject, "reference_period": period,
                          "row": number})
        for code, declared in variables.items():
            value_text = text(row.get(code))
            flag = text(row.get("X" + code)) if ("X" + code) in header else None
            notes = []
            if value_text in suppression:
                status, value, special = "suppressed", None, value_text
                notes.append({"kind": "suppression", "code": value_text, "text": suppression[value_text],
                              "period": period})
            elif decimal_text(value_text) is not None:
                status, value, special = "reported", decimal_text(value_text), None
            else:
                status = "not_applicable" if flag == "A" else "not_published"
                value, special = None, value_text
            if flag and flag != "R":
                notes.append({"kind": "imputation" if flag in {"G", "J", "K", "L", "N", "P", "C"} else "flag",
                              "code": flag, "text": IPEDS_FLAGS.get(flag, "flag as published"), "period": period})
            items.append({
                "record_kind": "institution_statistic",
                "subject": {"scheme": "ipeds-unitid", "code": unitid, "country": "US"},
                "indicator": _variable(code, declared),
                "isced": _isced(declared),
                "unit": _unit(declared),
                "dimensions": {**filters, "survey": str(document.get("survey") or document["file"])},
                "frequency": "annual",
                "observations": [{"period": period, "value_text": value_text, "value": value, "status": status,
                                  "special_code": special,
                                  "flags": {"imputation_flag": flag, "imputation_label": IPEDS_FLAGS.get(flag or "")}
                                  if flag else {}, "row": number}],
                "notes": notes,
            })
    published_on, label, stage = _release(document, "provisional")
    return _finish("ipeds", "ipeds-csv-zip", raw, items, published_on=published_on, published_at=None,
                   basis="declared_release" if published_on else "retrieval_time", label=label, stage=stage,
                   structure={"file": document["file"], "member": member, "rows": len(rows),
                              "unitids_found": sorted(matched), "unitids_absent": sorted(wanted - matched)})


# ------------------------------------------------------------------ ETER


def parse_eter(raw: bytes, *, document: Mapping[str, Any], max_rows: int = 200) -> dict[str, Any]:
    """One ETER export: a profile per institution and a series per (ETER ID, variable) for the reference year;
    special codes stored as published codes with no value."""
    header, rows = _read_csv(raw, encoding=str(document.get("encoding") or "utf-8-sig"),
                             delimiter=str(document.get("delimiter") or ","))
    columns = dict(document["columns"])
    variables = dict(document["variables"])
    identifier_columns = dict(columns.get("identifiers") or {})
    needed = [c for k, c in columns.items() if k != "identifiers" and c] + list(variables) + list(
        identifier_columns.values())
    missing = [c for c in needed if c not in header]
    if missing:
        raise EducationFormatError("schema_drift", f"the ETER export lacks declared columns {missing}")
    wanted = {str(i) for i in document.get("eter_ids") or []}
    year = str(document["reference_year"])
    items: list[dict[str, Any]] = []
    seen = set()
    for number, row in enumerate(rows, start=2):
        eter_id = text(row.get(columns["id"]))
        if eter_id is None or (wanted and eter_id not in wanted):
            continue
        if columns.get("reference_year") and text(row.get(columns["reference_year"])) != year:
            continue
        if eter_id in seen:
            raise EducationFormatError("schema_drift", f"ETER ID {eter_id} appears twice for the reference year")
        seen.add(eter_id)
        if len(seen) > max_rows:
            raise EducationFormatError("input_limit", "more institutions than the source's result budget")
        subject = {"scheme": "eter-id", "code": eter_id, "country": text(row.get(columns["country"]))}
        for key in ("name", "city", "national_id"):
            if columns.get(key) and text(row.get(columns[key])):
                subject["label" if key == "name" else key] = text(row.get(columns[key]))
        stated = {k: text(row.get(c)) for k, c in identifier_columns.items() if text(row.get(c))}
        profile_subject = {**subject, **({"stated_ids": stated} if stated else {})}
        items.append({"record_kind": "institution_profile", "subject": profile_subject, "reference_period": year,
                      "row": number})
        for code, declared in variables.items():
            value_text = text(row.get(code))
            special = ETER_SPECIAL.get((value_text or "").casefold())
            notes = []
            if special:
                status, value = special[0], None
                notes.append({"kind": "special_code", "code": value_text, "text": special[1], "period": year})
            elif decimal_text(value_text) is not None:
                status, value = "reported", decimal_text(value_text)
            else:
                status, value = "not_published", None
            items.append({
                "record_kind": "institution_statistic",
                "subject": {"scheme": "eter-id", "code": eter_id, "country": subject["country"]},
                "indicator": _variable(code, declared),
                "isced": _isced(declared),
                "unit": _unit(declared),
                "dimensions": {},
                "frequency": "annual",
                "observations": [{"period": year, "value_text": value_text, "value": value, "status": status,
                                  "special_code": value_text if special else None, "flags": {}, "row": number}],
                "notes": notes,
            })
    published_on, label, stage = _release(document, "release")
    return _finish("eter", "eter-csv", raw, items, published_on=published_on, published_at=None,
                   basis="declared_release" if published_on else "retrieval_time", label=label, stage=stage,
                   structure={"rows": len(rows), "header": header, "institutions": sorted(seen),
                              "absent": sorted(wanted - seen)})


# ------------------------------------------------------------------ UNESCO UIS


def parse_uis(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """One UIS data call: a series per (indicator, geo unit), values as published, qualifiers, magnitudes and
    footnotes as comparability notes, dated by the indicator metadata's lastDataUpdate (else the declared release)."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EducationFormatError("schema_drift", "the UIS response is not JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("records"), list):
        raise EducationFormatError("schema_drift", "the UIS response has no records array")
    variables = dict(document["variables"])
    metadata = {str(m.get("indicatorCode")): dict(m) for m in payload.get("indicatorMetadata") or []
                if isinstance(m, Mapping)}
    geo_units = {str(g) for g in document["geo_units"]}
    series: dict[str, dict[str, Any]] = {}
    rejected = []
    for number, record in enumerate(payload["records"]):
        record = dict(record)
        code, geo, year = str(record.get("indicatorId")), text(record.get("geoUnit")), text(record.get("year"))
        if code not in variables or geo not in geo_units or year is None:
            rejected.append({"record": number, "reason": "outside the declared indicators or geo units"})
            continue
        declared = variables[code]
        meta = metadata.get(code, {})
        entry = series.setdefault(canonical([code, geo]), {
            "record_kind": "education_indicator",
            "subject": {"scheme": "uis-geo", "code": geo, "country": geo},
            "indicator": {**_variable(code, declared, meta.get("name")),
                          "definition": text(meta.get("definition")) or text(declared.get("definition"))},
            "isced": _isced(declared),
            "unit": _unit(declared),
            "dimensions": {},
            "frequency": "annual",
            "observations": [],
            "notes": [],
        })
        value_text = text(record.get("value"))
        value = decimal_text(value_text)
        qualifier, magnitude = text(record.get("qualifier")), text(record.get("magnitude"))
        entry["observations"].append({
            "period": year, "value_text": value_text, "value": value,
            "status": "reported" if value is not None else "not_published", "special_code": None,
            "flags": {k: v for k, v in (("qualifier", qualifier), ("magnitude", magnitude)) if v}, "row": number})
        if qualifier:
            entry["notes"].append({"kind": "qualifier", "code": qualifier,
                                   "text": UIS_QUALIFIERS.get(qualifier, "qualifier as published"), "period": year})
        if magnitude:
            entry["notes"].append({"kind": "magnitude", "code": magnitude, "text": "magnitude as published",
                                   "period": year})
        for footnote in record.get("footnotes") or []:
            note = footnote if isinstance(footnote, str) else dict(footnote).get("value") or dict(footnote).get("text")
            if text(note):
                entry["notes"].append({"kind": "footnote", "code": None, "text": text(note), "period": year})
    stamps = sorted({iso_instant(m.get("lastDataUpdate")) for m in metadata.values()} - {None})
    declared_on, label, stage = _release(document, "release")
    if stamps:
        published_at, published_on, basis = stamps[-1], stamps[-1][:10], "uis_last_data_update"
    elif declared_on:
        published_at, published_on, basis = None, declared_on, "declared_release"
    else:
        published_at, published_on, basis = None, None, "retrieval_time"
    return _finish("unesco-uis", "uis-json", raw, list(series.values()), published_on=published_on,
                   published_at=published_at, basis=basis, label=label or f"UIS data version {document['version']}",
                   stage=stage, structure={"version": document["version"], "records": len(payload["records"]),
                                           "rejected": rejected, "hints": payload.get("hints") or []})


# ------------------------------------------------------------------ SDMX (OECD EAG, Eurostat R&D)


def parse_sdmx(raw: bytes, *, fmt: str, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared SDMX-CSV series through the SDMX connector's reader: one series per dimension combination, values as
    published with status/flag attributes, notes from flags and declared note attributes."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    oecd = fmt == "oecd-sdmx-csv"
    connector = SDMXConnector("OECD" if oecd else "ESTAT")
    ref = SeriesRef(locator=f"{document['flow']}/{document.get('key') or ''}",
                    metadata={"flow": str(document["flow"])}, title=document.get("label"))
    try:
        records = connector.parse_csv(RawSeries(ref, raw, content_type="text/csv", source_url=url, fetched_at=0))
    except IntegrationError as exc:
        raise EducationFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise EducationFormatError("schema_drift", "the response states no series")
    code = str(document["indicator"])
    declared = dict(document["variables"])[code]
    area_dim = "REF_AREA" if oecd else "GEO"
    level_dim = str(document.get("isced_dimension") or ("EDUCATION_LEV" if oecd else ""))
    note_attributes = [str(a) for a in document.get("note_attributes") or []]
    items = []
    stamps = set()
    for record in records:
        meta = record.metadata
        dims = dict(meta["dimensions"])
        upper = {k.upper(): v for k, v in dims.items()}
        area = text(upper.get(area_dim))
        if area is None:
            raise EducationFormatError("schema_drift", f"the SDMX-CSV response has no {area_dim} dimension")
        if meta.get("provider_last_update_at"):
            stamps.add(meta["provider_last_update_at"])
        observations, notes = [], []
        unit_mult = set()
        for observation in record.observations:
            period = observation.period
            attributes = dict(meta["observation_attributes"].get(period) or {})
            upper_attrs = {k.upper(): v for k, v in attributes.items()}
            value_text = text(meta["original_values"].get(period))
            value = decimal_text(value_text) if value_text not in (None, ":") else None
            status, special = ("reported" if value is not None else "not_published"), None
            if oecd:
                code_ = text(upper_attrs.get("OBS_STATUS"))
                if code_:
                    mapped = OECD_STATUS.get(code_)
                    if value is None and mapped and mapped[0] != "reported":
                        status, special = mapped[0], code_
                    if code_ not in {"A"}:
                        notes.append({"kind": "status", "code": code_,
                                      "text": (mapped or (None, "status as published"))[1], "period": period})
                if text(upper_attrs.get("UNIT_MULT")):
                    unit_mult.add(text(upper_attrs["UNIT_MULT"]))
            else:
                flags = text(upper_attrs.get("OBS_FLAG")) or ""
                if "c" in flags and value is None:
                    status, special = "confidential", flags
                elif "z" in flags and value is None:
                    status, special = "not_applicable", flags
                for letter in flags:
                    if letter in EUROSTAT_NOTE_FLAGS:
                        notes.append({"kind": "flag", "code": letter, "text": EUROSTAT_FLAGS[letter],
                                      "period": period})
            for attribute in note_attributes:
                if text(attributes.get(attribute)):
                    notes.append({"kind": "footnote", "code": attribute, "text": text(attributes[attribute]),
                                  "period": period})
            observations.append({"period": period, "value_text": value_text, "value": value, "status": status,
                                 "special_code": special, "flags": attributes, "row": meta["row_lines"].get(period)})
        unit = _unit(declared)
        published_unit = text(upper.get("UNIT_MEASURE") or upper.get("UNIT"))
        if published_unit:
            unit["published_code"] = published_unit
        if len(unit_mult) == 1:
            unit["multiplier"] = next(iter(unit_mult))
        items.append({
            "record_kind": "education_indicator",
            "subject": {"scheme": "oecd-ref-area" if oecd else "eurostat-geo", "code": area, "country": area},
            "indicator": {**_variable(code, declared), "dataflow": meta.get("dataflow") or str(document["flow"])},
            "isced": _isced(declared, upper.get(level_dim.upper()) if level_dim else None),
            "unit": unit,
            "dimensions": {k: v for k, v in dims.items() if k.upper() != area_dim},
            "frequency": record.frequency,
            "observations": observations,
            "notes": notes,
        })
    declared_on, label, stage = _release(document, "edition" if oecd else "update")
    if stamps:
        published_at = iso_instant(sorted(stamps)[-1])
        published_on, basis = published_at[:10], "provider_last_update"
    elif declared_on:
        published_at, published_on, basis = None, declared_on, "declared_release"
    else:
        published_at, published_on, basis = None, None, "retrieval_time"
    return _finish("oecd-eag" if oecd else "eurostat-rd", fmt, raw, items, published_on=published_on,
                   published_at=published_at, basis=basis, label=label, stage=stage,
                   structure={"dataflow": str(document["flow"]), "key": document.get("key"), "series": len(records)})


# ------------------------------------------------------------------ runtime adapter


class EducationStatisticsAdapter:
    """Fetch the declared education documents on the runtime's default transport; one page (release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = education_declaration(self.source)
        del secret  # no provider in scope needs a credential
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
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
            "education_statistics": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "live_verification": LIVE_VERIFICATION[self.declared["provider"]]["status"],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "education runs fetch the declared documents only")

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the endpoint's host only")
        base, _, query = url.partition("?")
        response = self.transport(
            url=base,
            params=parse_qsl(query, keep_blank_values=True),
            headers={"Accept": "application/json, text/csv, application/zip, application/vnd.sdmx.data+csv"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        return raw, "fixture" if response.get("origin") == "fixture" else "live"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        fmt = self.declared["format"]
        url = document_url(fmt, document, self.source["endpoint"])
        raw, origin = self._get(url)
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        try:
            if fmt == "ipeds-csv-zip":
                release = parse_ipeds(raw, document=document, max_rows=limit)
            elif fmt == "eter-csv":
                release = parse_eter(raw, document=document, max_rows=limit)
            elif fmt == "uis-json":
                release = parse_uis(raw, document=document)
            else:
                release = parse_sdmx(raw, fmt=fmt, document=document, url=url)
        except EducationFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift", f"{exc.code}: {exc}"
            ) from exc
        statistics = [i for i in release["items"] if i["record_kind"] != "institution_profile"]
        if len(statistics) > limit * max(1, len(dict(document.get("variables") or {}))):
            # Never a truncated release: a missing series would read as a value that was not published.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": release["provider"],
            "format": release["format"],
            "document": document,
            "published_on": release["published_on"],
            "published_at": release["published_at"],
            "release_basis": release["release_basis"],
            "release_label": release["release_label"],
            "release_stage": release["release_stage"],
            "file_sha256": release["file_sha256"],
            "content_sha256": release["content_sha256"],
            "item_count": release["item_count"],
            "structure": release["structure"],
            # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
            "evidence_origin": origin,
            "live_verification": LIVE_VERIFICATION[release["provider"]]["status"],
            "url": url,
        }
        records = [
            {
                "id": f"{release['file_sha256'][:16]}:{number}",
                "title": f"{document.get('label') or release['provider']} ({release['published_on'] or 'retrieved'})",
                "url": url,
                "language": "en",
                "published_at": release["published_on"],
                "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
                "education_release": header,
                "education_item": item,
            }
            for number, item in enumerate(release["items"])
        ]
        receipt = {
            "status": 200,
            "provider": release["provider"],
            "document": document.get("label"),
            "published_on": release["published_on"],
            "release_basis": release["release_basis"],
            "release_stage": release["release_stage"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "evidence_origin": origin,
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: EducationStatisticsAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); ``body_base64`` carries binary bodies."""
    import base64

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
        query = urlencode(sorted(pairs))
        key = urlsplit(url).path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("body_base64") is not None:
            content = base64.b64decode(page["body_base64"])
        else:
            body = page.get("body")
            content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def fixture_request(fmt: str, document: Mapping[str, Any], endpoint: str = "") -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""
    parts = urlsplit(document_url(fmt, document, endpoint))
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return parts.path + ("?" + query if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = EducationStatisticsAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": min(source["operations"]), "parameters": {}, "limit": int(source["budgets"]["max_results"])},
            cursor=cursor,
        )
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONCEPTS",
    "CONNECTOR",
    "EXCLUSIONS",
    "EducationFormatError",
    "EducationStatisticsAdapter",
    "FORMATS",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "PROVIDER_CONTRACTS",
    "STATUSES",
    "fixture_request",
    "fixture_transport",
    "parse_eter",
    "parse_ipeds",
    "parse_sdmx",
    "parse_uis",
    "replay_native_fixture",
]
