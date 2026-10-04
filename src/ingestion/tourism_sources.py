"""Tourism statistics sources for the Economics ``economics.tourism`` provider (#2739, TO03, TO04).

The machine-readable copy of ``docs/development/tourism-evidence/source-audit.md`` (TO01). One native source-pack
connector, ``tourism-statistics``, driven by :mod:`src.ingestion.source_pack_runtime` with two sources of the
``economic-statistics-and-filings`` pack (``config/source_packs/economic.json``). Each source declares one provider and
a bounded list of documents; the adapter fetches one declared document per page and returns one *release* (a header
and one item per series) that :class:`src.kb.tourism_store.TourismProjector` appends as series vintages.

* **Eurostat tourism occupancy** (``eurostat-tourism-occupancy``, format ``eurostat-tourism-occupancy-sdmx-csv``) -
  nights spent (``tour_occ_nim``) and arrivals (``tour_occ_arm``) at tourist accommodation establishments, monthly for
  Germany, and nights spent by NUTS 2 region (``tour_occ_nin2``), annual for Berlin (``DE30``), through the existing
  :class:`~src.ingestion.connectors.dataset.sdmx.SDMXConnector` (provider ``ESTAT``, SDMX-CSV).
* **Eurostat tourism capacity** (``eurostat-tourism-capacity``, format ``eurostat-tourism-capacity-sdmx-csv``) -
  establishments, bedrooms and bed places by NUTS 2 region (``tour_cap_nuts2``), annual for Berlin, through the same
  connector. The reference date Eurostat states for capacity is stored per value.

The ``LAST UPDATE`` column dates each release; ``OBS_FLAG`` letters are kept verbatim per value and ``c``
(confidential) is a status, never a value. Residence of guest (``c_resid``), accommodation type (``nace_r2``), unit,
frequency and the geography with its NUTS version are part of every series key, so monthly national and annual NUTS 2
series are different series and a NUTS code-list change makes a different place key. The establishment-size threshold
each country applies is recorded as a definition note.

**UN Tourism** statistics are ``not-implemented``: no documented, stable, open machine API and unclear redistribution
terms; dashboard pages and e-library files are never scraped. Queries report ``not-implemented``, never empty.

Every Eurostat source is ``unverified-live`` until a dated live run (TO12); endpoints, dataset codes, dimension orders,
code lists and flag letters marked *verify* come from the publisher's documentation, not from a live response.
Nothing here nowcasts, fills a month or region, seasonally adjusts a series, blends Eurostat and UN Tourism figures,
or derives an occupancy rate, average, per-capita or per-bed figure.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "tourism-statistics"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-tourism-statistics-release-v1"
AUDIT = "docs/development/tourism-evidence/source-audit.md"
# Eurostat dissemination requests carry no credential, so the fixtures expect none (native-connector contract).
FIXTURE_SECRET = None
PROVIDERS = ("eurostat-tourism-occupancy", "eurostat-tourism-capacity")
NOT_IMPLEMENTED = ("un-tourism",)
FORMATS = {
    "eurostat-tourism-occupancy-sdmx-csv": {"provider": "eurostat-tourism-occupancy"},
    "eurostat-tourism-capacity-sdmx-csv": {"provider": "eurostat-tourism-capacity"},
}
PROVIDER_HOSTS = {"eurostat-tourism-occupancy": "ec.europa.eu", "eurostat-tourism-capacity": "ec.europa.eu"}
# The audited datasets: provider, bounded coverage kind, frequency and the concepts each may deliver (verify codes).
DATASETS = {
    "tour_occ_nim": {"provider": "eurostat-tourism-occupancy", "coverage": "monthly-national",
                     "frequency": "monthly", "concepts": ("nights_spent",)},
    "tour_occ_arm": {"provider": "eurostat-tourism-occupancy", "coverage": "monthly-national",
                     "frequency": "monthly", "concepts": ("arrivals",)},
    "tour_occ_nin2": {"provider": "eurostat-tourism-occupancy", "coverage": "annual-nuts2",
                      "frequency": "annual", "concepts": ("nights_spent",)},
    "tour_cap_nuts2": {"provider": "eurostat-tourism-capacity", "coverage": "annual-nuts2",
                       "frequency": "annual", "concepts": ("establishments", "bedrooms", "bed_places")},
}
# Out of scope until separately audited (TO01 minimisation decision): demand-side survey tables and experimental
# collaborative-economy platform data.
EXCLUDED_DATASETS = ("tour_dem_", "tour_ce_oa")
NEVER_SENTENCE = (
    "Published tourism statistics as Eurostat released them: monthly national and annual NUTS 2 series kept apart, "
    "each with its residence, accommodation type, unit, NUTS version, flags and release vintage; no nowcast, no filled "
    "month or region, no own seasonal adjustment, no blending with UN Tourism figures, and no occupancy rate, average, "
    "per-capita or per-bed figure of our own."
)
EXCLUSIONS = (
    "nowcasting tourism indicators",
    "filling months or regions a source did not publish",
    "seasonal adjustment of our own",
    "blending Eurostat and UN Tourism figures",
    "occupancy rates, averages, per-capita or per-bed figures of our own",
    "annual totals computed from months",
    "derived indicators",
    "forecasts",
    ("traveller survey microdata (tour_dem_*), establishment-level returns and experimental platform data "
     "(tour_ce_oa*)"),
    "values for confidential cells (stored as their status only)",
)
CONCEPTS = ("nights_spent", "arrivals", "establishments", "bedrooms", "bed_places")
RESIDENCES = {"TOTAL": "total", "DOM": "domestic", "FOR": "foreign"}
ACCOMMODATION_TYPES = {
    "I551-I553": "Hotels; holiday and other short-stay accommodation; camping grounds, recreational vehicle parks "
                 "and trailer parks",
    "I551": "Hotels and similar accommodation",
    "I552": "Holiday and other short-stay accommodation",
    "I553": "Camping grounds, recreational vehicle parks and trailer parks",
}
NUTS_VERSIONS = ("2016", "2021", "2024")
STATUSES = ("reported", "confidential", "not_published")
FREQUENCIES = ("monthly", "annual")
# Hard ceilings the adapter enforces on top of the source-pack budgets (TO01 bounded first coverage).
CAPS = {
    "eurostat-tourism-occupancy": {
        "monthly-national": {"documents": 2, "series_per_response": 30, "months": 36},
        "annual-nuts2": {"documents": 1, "series_per_response": 10},
    },
    "eurostat-tourism-capacity": {
        "annual-nuts2": {"documents": 1, "series_per_response": 10},
    },
}
BOUNDED_COVERAGE = {
    "eurostat-tourism-occupancy": {
        "monthly-national": {
            "places": {"Germany": "geo DE"},
            "series": "tour_occ_nim (nights spent) and tour_occ_arm (arrivals): total, domestic and foreign residence "
                      "(c_resid TOTAL, DOM, FOR), accommodation I551-I553",
            "periods": "at most 36 months from a declared start period",
            "caps": "2 documents, 30 series per response",
        },
        "annual-nuts2": {
            "places": {"Berlin": "geo DE30"},
            "series": "tour_occ_nin2 (nights spent): total residence, accommodation I551-I553",
            "periods": "from a declared start year",
            "caps": "1 document, 10 series",
        },
    },
    "eurostat-tourism-capacity": {
        "annual-nuts2": {
            "places": {"Berlin": "geo DE30"},
            "series": "tour_cap_nuts2: establishments and bed places, accommodation I551-I553",
            "periods": "from a declared start year",
            "caps": "1 document, 10 series",
        },
    },
    "un-tourism": {"status": "not-implemented", "reason": "no stable machine access and unclear redistribution terms; "
                                                          "dashboard pages and e-library files are never scraped"},
    "justification": "one country and one of its NUTS 2 regions show both frequencies, the residence split, "
                     "provisional-to-revised monthly vintages and a regional confidentiality flag without a second "
                     "publisher; Berlin is the region the Geospatial bundle already resolves, so the NUTS code links to "
                     "its boundary feature by shared published code, and the Labour link reaches economics.labour "
                     "series for accommodation and food services (NACE Rev.2 section I) for the same place by shared "
                     "code and citation, never as a ratio",
    "record_cap": "max_results series per response and max_pages documents per run; a larger release is refused, "
                  "never truncated; every further place or dataset is a source-pack version bump",
}
_EUROSTAT_ENTRY = ("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}"
                   "?format=SDMX-CSV&startPeriod=... (verify)")
_REVISION_MODEL = (
    "LAST UPDATE dates each release; monthly data are first published provisional and revised in later months, each "
    "changed release a new vintage; OBS_FLAG letters (p provisional, e estimated, b break, c confidential, u low "
    "reliability, d definition differs; verify the list) are kept per value; c is a status, never a value; a NUTS "
    "code-list change (NUTS 2021 to 2024, verify) makes a different place key, linked only through Eurostat's "
    "published correspondence; a series a later complete release no longer states becomes a removed_by_source vintage"
)
_FALLBACK = ("a failed document (HTTP error, redirect to another host, schema drift, a response larger than the budget) "
             "fails that source's run with its code and a receipt; earlier vintages stay current and nothing is marked "
             "removed or revised because of a failure; readiness reports the source as stale")
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eurostat-tourism-occupancy": {
        "publisher": "Eurostat, tourism statistics (Regulation (EU) No 692/2011, verify amendments)",
        "delivers": "nights spent and arrivals at tourist accommodation establishments, by residence of guest and "
                    "accommodation type; monthly national, annual NUTS 2",
        "access": "api (Eurostat SDMX 2.1 dissemination API, SDMX-CSV through the existing SDMX connector, ESTAT path); "
                  "datasets tour_occ_nim, tour_occ_arm, tour_occ_nin2 (verify codes and dimension order)",
        "entry_points": [_EUROSTAT_ENTRY],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "no published quota (verify); one request per declared document",
        "revision_model": _REVISION_MODEL,
        "definitions": "nights spent (each night a guest actually stays) and arrivals (guests checking in) as Eurostat "
                       "defines them; residence of guest (c_resid: domestic, foreign, total), accommodation type "
                       "(nace_r2: I551 hotels, I552 holiday and short-stay, I553 camping, I551-I553 total, verify), "
                       "unit, frequency and geography with its NUTS version; the establishment-size threshold each "
                       "country applies (the regulation's minimum and national deviations, verify) is a definition "
                       "note; monthly national and annual NUTS 2 series are different series",
        "personal_data": "none: published aggregates only",
        "unavailable_fallback": _FALLBACK,
        "status": "unverified-live",
        "verify": ["dataset codes and dimension order", "c_resid and nace_r2 code lists", "OBS_FLAG letters",
                   "LAST UPDATE format", "the NUTS version each dataset uses", "national establishment thresholds",
                   "the regulation's amendments", "the reuse policy", "rate limits"],
    },
    "eurostat-tourism-capacity": {
        "publisher": "Eurostat, tourism statistics",
        "delivers": "establishments, bedrooms and bed places, annual, national and NUTS 2",
        "access": "api (same SDMX-CSV path); tour_cap_nuts2 (verify)",
        "entry_points": [_EUROSTAT_ENTRY],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "no published quota (verify); one request per declared document",
        "revision_model": _REVISION_MODEL,
        "definitions": "capacity as establishments, bedrooms or bed places on the reference date or year Eurostat "
                       "states (stored per value); accommodation type, unit and geography with its NUTS version; the "
                       "national establishment-size threshold is a definition note",
        "personal_data": "none: published aggregates only",
        "unavailable_fallback": _FALLBACK,
        "status": "unverified-live",
        "verify": ["dataset code and dimension order", "the capacity indicator code list (accomunit, verify)",
                   "the capacity reference date", "the NUTS version", "national establishment thresholds"],
    },
    "un-tourism": {
        "publisher": "UN Tourism (formerly UNWTO): Tourism Statistics Database, Compendium and Yearbook of Tourism "
                     "Statistics, data dashboards",
        "delivers": "inbound and outbound arrivals, expenditure, accommodation and industry indicators per country",
        "access": "none acquired: no documented, stable, open machine API known; e-library distribution with bulk "
                  "reuse terms not established (verify); dashboards are interactive pages and are never scraped",
        "entry_points": [],
        "authentication": "-",
        "key_handling": "-",
        "licence": "not established (verify)",
        "redistribution": "not established",
        "rate_limits": "-",
        "revision_model": "-",
        "definitions": "-",
        "personal_data": "none acquired",
        "unavailable_fallback": "queries report not-implemented, never empty",
        "status": "not-implemented",
        "verify": ["whether a stable machine access and redistribution terms exist",
                   "the UNSD SDG API route (SDG 8.9.1), a different source that needs its own audit"],
    },
}
LIVE_VERIFICATION = {
    **{provider: {"status": "unverified-live", "checked": None, "evidence": None,
                  "intended": "verified-live after a dated bounded run (TO12, #2811)",
                  "note": "no dated live run from this runtime; authored offline fixtures only"}
       for provider in PROVIDERS},
    "un-tourism": {"status": "not-implemented", "checked": None, "evidence": None,
                   "intended": "stays not-implemented until an amended audit finds stable machine access and terms",
                   "note": "no stable machine access, terms not established (verify)"},
}
NOT_IMPLEMENTED_ANSWER = {
    "provider": "un-tourism", "status": "not-implemented",
    "reason": "UN Tourism statistics have no audited stable machine access and their redistribution terms are not "
              "established; nothing is acquired, so this is not an empty answer",
    "audit": AUDIT,
}
DEFINITION_NOTES = {
    "comparability": "monthly national and annual NUTS 2 series are different series even where they cover the same "
                     "nights; annual totals are never computed from months; national establishment-size thresholds "
                     "differ (definition note per country) and a d flag or published deviation is a source-stated "
                     "comparability note; Eurostat and UN Tourism figures are never blended",
}
# Eurostat OBS_FLAG letters (verify against the live code list).
FLAG_MEANINGS = {
    "b": "break in time series", "p": "provisional", "e": "estimated", "c": "confidential", "u": "low reliability",
    "d": "definition differs", "s": "Eurostat estimate", "z": "not applicable", "n": "not significant",
}
BREAK_FLAGS = {"b"}
CONFIDENTIAL_FLAGS = {"c"}
DEFINITION_FLAGS = {"d"}


class TourismFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unverified(provider: str) -> bool:
    return LIVE_VERIFICATION.get(provider, {}).get("status") != "verified-live"


def text(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def decimal_text(value: Any) -> str | None:
    """A published number as exact decimal text; ``None`` for a missing or non-numeric value (never zero)."""
    raw = text(value)
    if raw is None or raw in {":", "NaN", "nan", "null", "None"}:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number, "f")


def iso_day(value: Any) -> str | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def normalise_period(period: str) -> str:
    """SDMX period codes in the ``dataset-series-v1`` forms (``2096``, ``2096-01``)."""
    raw = str(period).strip()
    match = re.fullmatch(r"(\d{4})-?M(\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return raw


def period_matches(period: str, frequency: str) -> bool:
    pattern = r"\d{4}-\d{2}" if frequency == "monthly" else r"\d{4}"
    return re.fullmatch(pattern, str(period)) is not None


def series_key(item: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that make a series (never a release, so each release of the same key is a vintage)."""
    area = dict(item["area"])
    return {
        "provider": item["provider"],
        "dataset": item["dataset"],
        "indicator": dict(item["indicator"]).get("code"),
        "concept": dict(item["indicator"]).get("concept"),
        "residence": dict(item.get("residence") or {}).get("code"),
        "accommodation": dict(item.get("accommodation") or {}).get("code"),
        "unit": dict(item.get("unit") or {}).get("code"),
        "frequency": item.get("frequency"),
        "area": {"scheme": area["scheme"], "code": str(area["code"]), "nuts_version": area.get("nuts_version")},
    }


# ------------------------------------------------------------------ declarations


def excluded_dataset(flow: str) -> bool:
    return any(str(flow).startswith(prefix) for prefix in EXCLUDED_DATASETS)


def tourism_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(source.get("tourism_statistics") or {})
    provider, fmt = config.get("provider"), config.get("format")
    if provider not in PROVIDERS or FORMATS.get(str(fmt), {}).get("provider") != provider:
        raise SourcePackError("invalid_manifest",
                              "tourism-statistics sources declare a known provider and its runtime format")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} documents are fetched from {PROVIDER_HOSTS[provider]}")
    if dict(source.get("auth") or {}).get("kind") != "none":
        raise SourcePackError("invalid_manifest", "Eurostat dissemination requests carry no credential")
    documents = [dict(d) for d in config.get("documents") or []]
    if not documents:
        raise SourcePackError("invalid_manifest", f"{provider} declares at least one document")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared requests than the source's page budget")
    per_coverage: dict[str, int] = {}
    urls = []
    for document in documents:
        try:
            check_document(provider, document)
            url = document_url(document)
        except (TourismFormatError, ValueError) as exc:
            raise SourcePackError("invalid_manifest", f"{document.get('label')}: {exc}") from exc
        coverage = DATASETS[document["flow"]]["coverage"]
        per_coverage[coverage] = per_coverage.get(coverage, 0) + 1
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    for coverage, count in per_coverage.items():
        if count > CAPS[provider][coverage]["documents"]:
            raise SourcePackError("invalid_manifest", f"{provider} declares at most "
                                                      f"{CAPS[provider][coverage]['documents']} {coverage} documents")
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return {"provider": provider, "format": fmt, "namespace": config.get("namespace") or "global",
            "documents": documents, "live_verification": config.get("live_verification") or "unverified-live"}


def _months_between(start: str, end: str) -> int:
    first, last = (int(start[:4]) * 12 + int(start[5:7]), int(end[:4]) * 12 + int(end[5:7]))
    return last - first + 1


def check_document(provider: str, document: Mapping[str, Any]) -> None:
    for key in ("label", "flow", "key", "definition", "references"):
        if not document.get(key):
            raise TourismFormatError("invalid_document", f"a document states its {key}")
    flow = str(document["flow"])
    if excluded_dataset(flow):
        raise TourismFormatError("excluded_dataset", f"{flow} is excluded by the TO01 minimisation decision")
    dataset = DATASETS.get(flow)
    if dataset is None or dataset["provider"] != provider:
        raise TourismFormatError("unbounded_document", f"{flow} is not an audited dataset of {provider}")
    release = document.get("release")
    if release is not None and iso_day(dict(release).get("published_on")) is None:
        raise TourismFormatError("invalid_document", "a declared release states its publication date")
    for ref in document.get("references") or []:
        if not text(dict(ref).get("identifier")) or not text(dict(ref).get("kind")):
            raise TourismFormatError("invalid_document", "each reference states its kind and identifier as published")
    params = dict(document.get("params") or {})
    if not params.get("startPeriod"):
        raise TourismFormatError("unbounded_document", "documents pin a start period")
    if dataset["frequency"] == "monthly":
        if not params.get("endPeriod") or not re.fullmatch(r"\d{4}-\d{2}", str(params["startPeriod"])) or \
                not re.fullmatch(r"\d{4}-\d{2}", str(params["endPeriod"])):
            raise TourismFormatError("unbounded_document", "monthly documents pin monthly start and end periods")
        months = CAPS[provider]["monthly-national"]["months"]
        if not 1 <= _months_between(str(params["startPeriod"]), str(params["endPeriod"])) <= months:
            raise TourismFormatError("unbounded_document", f"monthly documents request at most {months} months")
    elif not re.fullmatch(r"\d{4}", str(params["startPeriod"])):
        raise TourismFormatError("unbounded_document", "annual documents pin a start year")
    indicator = dict(document.get("indicator") or {})
    if indicator.get("dimension"):
        codes = dict(indicator.get("codes") or {})
        if not codes:
            raise TourismFormatError("invalid_document", "an indicator dimension maps its codes")
        concepts = [dict(spec).get("concept") for spec in codes.values()]
    else:
        if not indicator.get("code"):
            raise TourismFormatError("invalid_document", "a document states its indicator code and concept")
        concepts = [indicator.get("concept")]
    if not all(c in dataset["concepts"] for c in concepts):
        raise TourismFormatError("invalid_document", f"{flow} delivers {dataset['concepts']}")
    if flow != "tour_cap_nuts2" and dict(document.get("residence") or {}).get("dimension") != "c_resid":
        raise TourismFormatError("invalid_document", "occupancy documents name the c_resid dimension")
    if dict(document.get("accommodation") or {}).get("dimension") != "nace_r2":
        raise TourismFormatError("invalid_document", "documents name the nace_r2 accommodation dimension")
    if not dict(document.get("unit") or {}).get("dimension"):
        raise TourismFormatError("invalid_document", "documents name the unit dimension")
    area = dict(document.get("area") or {})
    if not area.get("dimension") or area.get("scheme") != "eurostat-geo":
        raise TourismFormatError("invalid_document", "documents name the geo dimension")
    if str(area.get("nuts_version")) not in NUTS_VERSIONS:
        raise TourismFormatError("invalid_document", f"documents state the NUTS version ({NUTS_VERSIONS})")
    codes = list(dict(area.get("labels") or {}))
    level = 0 if dataset["coverage"] == "monthly-national" else 2
    if not codes or any(len(code) != 2 + level for code in codes):
        raise TourismFormatError("unbounded_document", f"{dataset['coverage']} documents declare NUTS {level} codes")
    thresholds = dict(dict(document["definition"]).get("coverage_thresholds") or {})
    if not thresholds or not all(text(v) for v in thresholds.values()):
        raise TourismFormatError("invalid_document", "documents record each country's establishment-size threshold")
    if flow == "tour_cap_nuts2" and not text(dict(document.get("capacity_reference") or {}).get("stated")):
        raise TourismFormatError("invalid_document", "capacity documents state Eurostat's capacity reference date")


def document_key(provider: str, document: Mapping[str, Any]) -> str:
    """Identity of a declared document across releases (removal detection compares releases of one document)."""
    return f"{provider}:{document['flow']}:{document['key']}"


def document_url(document: Mapping[str, Any]) -> str:
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    url, query = SDMXConnector("ESTAT").csv_url(str(document["flow"]), str(document["key"]),
                                                dict(document.get("params") or {}))
    return url + "?" + urlencode(sorted(query.items()))


# ------------------------------------------------------------------ parsing


def _definition(document: Mapping[str, Any], provider: str, **extra: Any) -> dict[str, Any]:
    definition = dict(document["definition"])
    return {
        "provider": provider,
        "source_text": text(definition.get("source_text")),
        "scope": text(definition.get("scope")),
        "methodology_notes": [str(n) for n in definition.get("methodology_notes") or []],
        "coverage_thresholds": {str(k): str(v) for k, v in dict(definition["coverage_thresholds"]).items()},
        "comparability": DEFINITION_NOTES["comparability"],
        "references": [dict(r) for r in document.get("references") or []],
        **extra,
    }


def _declared_release(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    release = dict(document.get("release") or {})
    return iso_day(release.get("published_on")), text(release.get("label"))


def _labelled(spec: Mapping[str, Any], code: str) -> dict[str, Any]:
    label = dict(spec.get("labels") or {}).get(code)
    return {"label": label} if label else {}


def parse_sdmx(raw: bytes, *, fmt: str, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared Eurostat tourism SDMX-CSV series as items; flags verbatim, residence, accommodation and NUTS version
    in the key; confidential cells as a status without a value."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    provider = FORMATS[fmt]["provider"]
    flow = str(document["flow"])
    dataset = DATASETS[flow]
    connector = SDMXConnector("ESTAT")
    ref = SeriesRef(locator=f"{flow}/{document['key']}", metadata={"flow": flow}, title=document.get("label"))
    try:
        records = connector.parse_csv(RawSeries(ref, raw, content_type="text/csv", source_url=url, fetched_at=0))
    except IntegrationError as exc:
        raise TourismFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise TourismFormatError("schema_drift", "the response states no series")
    if len(records) > CAPS[provider][dataset["coverage"]]["series_per_response"]:
        raise TourismFormatError("budget_exhausted", "the response has more series than the audited cap")
    indicator_spec = dict(document["indicator"])
    residence_spec = dict(document.get("residence") or {})
    accommodation_spec = dict(document["accommodation"])
    unit_spec = dict(document["unit"])
    area_spec = dict(document["area"])
    declared_areas = set(dict(area_spec.get("labels") or {}))
    capacity_reference = dict(document.get("capacity_reference") or {})
    items, flows, last_update = [], set(), None
    for record in records:
        meta = record.metadata
        dims = {str(k): str(v) for k, v in dict(meta["dimensions"]).items()}
        flows.add(meta.get("dataflow"))
        last_update = meta.get("provider_last_update_at") or last_update
        if record.frequency != dataset["frequency"]:
            raise TourismFormatError("schema_drift", f"{flow} answered with {record.frequency} data, not "
                                                     f"{dataset['frequency']}")
        roles = [("accommodation", accommodation_spec), ("unit", unit_spec), ("area", area_spec)]
        if residence_spec.get("dimension"):
            roles.append(("residence", residence_spec))
        if indicator_spec.get("dimension"):
            roles.append(("indicator", indicator_spec))
        for role, spec in roles:
            if spec["dimension"] not in dims:
                raise TourismFormatError("schema_drift", f"the response lacks the declared {role} dimension")
        if indicator_spec.get("dimension"):
            indicator_code = dims[indicator_spec["dimension"]]
            spec = dict(indicator_spec["codes"]).get(indicator_code)
            if spec is None:
                raise TourismFormatError("schema_drift", f"unmapped indicator code {indicator_code!r}")
            indicator = {"code": indicator_code, "concept": spec["concept"], "label": spec.get("label") or
                         indicator_code}
        else:
            indicator = {"code": str(indicator_spec["code"]), "concept": indicator_spec["concept"],
                         "label": indicator_spec.get("label") or str(indicator_spec["code"])}
        if residence_spec.get("dimension"):
            residence_code = dims[residence_spec["dimension"]]
            if residence_code not in RESIDENCES:
                raise TourismFormatError("schema_drift", f"unmapped residence code {residence_code!r}")
            residence = {"code": residence_code, "label": RESIDENCES[residence_code]}
        else:
            residence = {"code": "not_applicable", "label": "capacity is not broken down by residence"}
        accommodation_code = dims[accommodation_spec["dimension"]]
        if accommodation_code not in ACCOMMODATION_TYPES:
            raise TourismFormatError("schema_drift", f"unmapped accommodation code {accommodation_code!r}")
        area_code = dims[area_spec["dimension"]]
        if area_code not in declared_areas:
            raise TourismFormatError("schema_drift", f"the response states an undeclared place {area_code!r}")
        unit_code = dims[unit_spec["dimension"]]
        unit = {"code": unit_code, "label": dict(unit_spec.get("labels") or {}).get(unit_code) or unit_code}
        observations, breaks, definitional = [], [], []
        for observation in sorted(record.observations, key=lambda o: o.period):
            attributes = {str(k): str(v) for k, v in dict(meta["observation_attributes"].get(observation.period)
                                                         or {}).items()}
            value_text = meta["original_values"].get(observation.period)
            value = decimal_text(value_text)
            flag = attributes.get("OBS_FLAG") or ""
            letters = set(flag)
            status = "confidential" if letters & CONFIDENTIAL_FLAGS else (
                "reported" if value is not None else "not_published")
            period = normalise_period(observation.period)
            if not period_matches(period, dataset["frequency"]):
                raise TourismFormatError("schema_drift", f"period {period!r} is not a {dataset['frequency']} period")
            if letters & BREAK_FLAGS:
                breaks.append(period)
            if letters & DEFINITION_FLAGS:
                definitional.append(period)
            kept = {k: attributes[k] for k in sorted(attributes) if k != "OBS_FLAG"}
            if capacity_reference:
                kept["capacity_reference"] = {"period": period, "stated": str(capacity_reference["stated"]),
                                              **({"date": str(capacity_reference["date"])}
                                                 if capacity_reference.get("date") else {})}
            observations.append({
                "period": period,
                "value_text": value_text if value_text not in (None, "") else None,
                "value": value if status == "reported" else None,
                "status": status,
                "flags": {"OBS_FLAG": flag} if flag else {},
                "flag_meanings": sorted({FLAG_MEANINGS.get(letter, letter) for letter in letters}),
                "attributes": kept,
            })
        notes = []
        if breaks:
            notes.append({"kind": "break", "attribute": "OBS_FLAG", "value": "b", "periods": breaks,
                          "statement": f"{document['label']}: Eurostat flags a break in series (OBS_FLAG b)"})
        provisional = [o["period"] for o in observations if "p" in o["flags"].get("OBS_FLAG", "")]
        if provisional:
            notes.append({"kind": "provisional", "attribute": "OBS_FLAG", "value": "p", "periods": provisional,
                          "statement": f"{document['label']}: Eurostat marks these periods provisional"})
        if definitional:
            notes.append({"kind": "definition_differs", "attribute": "OBS_FLAG", "value": "d",
                          "periods": definitional,
                          "statement": f"{document['label']}: Eurostat flags a definition that differs (OBS_FLAG d), "
                                       "for example a national establishment-size threshold deviation"})
        threshold = dict(document["definition"]["coverage_thresholds"]).get(area_code[:2])
        items.append({
            "provider": provider,
            "dataset": flow,
            "native_key": ".".join(dims[k] for k in dims),
            "dataflow": {"reference": flow, "stated": meta.get("dataflow")},
            "indicator": indicator,
            "residence": residence,
            "accommodation": {"scheme": "NACE", "version": "Rev.2", "code": accommodation_code,
                              "label": ACCOMMODATION_TYPES[accommodation_code]},
            "area": {"scheme": "eurostat-geo", "code": area_code, "nuts_version": str(area_spec["nuts_version"]),
                     "level": len(area_code) - 2, **_labelled(area_spec, area_code)},
            "unit": unit,
            "frequency": record.frequency,
            "dimensions": dims,
            "definition": _definition(document, provider, dataset=flow, indicator=indicator["code"],
                                      coverage_threshold=threshold, nuts_version=str(area_spec["nuts_version"]),
                                      **({"capacity_reference": str(capacity_reference["stated"])}
                                         if capacity_reference else {})),
            "references": [dict(r) for r in document.get("references") or []],
            "source_notes": notes,
            "observations": observations,
        })
    items.sort(key=lambda i: i["native_key"])
    stated = sorted(f for f in flows if f)
    flow_version = None
    if stated:
        match = re.search(r"\((\d+(?:\.\d+)*)\)\s*$", stated[0])
        flow_version = match.group(1) if match else None
    declared_on, declared_label = _declared_release(document)
    if last_update:
        stamp = datetime.fromisoformat(last_update)
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
        published_on, published_at, basis = stamp.date().isoformat(), stamp.isoformat(), "provider_last_update"
    elif declared_on:
        published_on, published_at, basis = declared_on, None, "declared_release"
    else:
        published_on, published_at, basis = None, None, "retrieval_time"
    return {"items": items, "published_on": published_on, "published_at": published_at, "release_basis": basis,
            "release_label": declared_label or (f"LAST UPDATE {last_update}" if last_update else None),
            "dataflow_version": flow_version}


# ------------------------------------------------------------------ adapter


class TourismStatisticsAdapter:
    """Fetch the declared documents on the runtime's default transport; one page (one release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # Eurostat dissemination requests never carry a credential.
        self.source = json.loads(json.dumps(source))
        self.declared = tourism_declaration(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        provider = self.declared["provider"]
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "tourism_statistics": {"provider": provider, "format": self.declared["format"],
                                   "documents": len(self.declared["documents"]), "caps": CAPS[provider],
                                   "live_verification": LIVE_VERIFICATION[provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "tourism-statistics runs fetch the declared documents only")

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = PROVIDER_HOSTS[self.declared["provider"]]
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the provider's host only")
        base, _, query = url.partition("?")
        response = self.transport(url=base, params=parse_qsl(query, keep_blank_values=True),
                                  headers={"Accept": "text/csv"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
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
        documents = self.declared["documents"]
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        fmt, provider = self.declared["format"], self.declared["provider"]
        url = document_url(document)
        raw, origin = self._get(url)
        try:
            release = parse_sdmx(raw, fmt=fmt, document=document, url=url)
        except TourismFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "budget_exhausted" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        items = release["items"]
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(items) > limit:
            # Never a truncated release: a missing series would read as a series the source removed.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        header = {
            "contract": RELEASE_CONTRACT, "provider": provider, "format": fmt, "document": document,
            "document_key": document_key(provider, document), "published_on": release["published_on"],
            "published_at": release["published_at"], "release_basis": release["release_basis"],
            "release_label": release["release_label"], "dataflow_version": release["dataflow_version"],
            "file_sha256": hashlib.sha256(raw).hexdigest(), "content_sha256": digest(items),
            "item_count": len(items), "complete": True, "evidence_origin": origin,
            "live_verification": LIVE_VERIFICATION[provider]["status"], "url": url,
        }
        records = [{
            "id": f"{header['file_sha256'][:16]}:{number}",
            "title": f"{document['label']} ({release['published_on'] or 'retrieved'})",
            "url": url, "language": "en", "published_at": release["published_on"],
            "content": canonical(item), "tourism_release": header, "tourism_item": item,
        } for number, item in enumerate(items)]
        receipt = {"status": 200, "provider": provider, "document": document["label"],
                   "published_on": release["published_on"], "release_basis": release["release_basis"],
                   "file_sha256": header["file_sha256"], "items": len(records), "requests": 1,
                   "evidence_origin": origin, "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


ADAPTERS = {CONNECTOR: TourismStatisticsAdapter}


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    query = urlencode(sorted((str(k), str(v)) for k, v in pairs))
    return urlsplit(url).path + ("?" + query if query else "")


def fixture_request(document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under."""
    base, _, query = document_url(document).partition("?")
    return _fixture_key(base, parse_qsl(query, keep_blank_values=True))


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        key = _fixture_key(url, params)
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = TourismStatisticsAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ACCOMMODATION_TYPES",
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CAPS",
    "CONCEPTS",
    "CONNECTOR",
    "DATASETS",
    "EXCLUDED_DATASETS",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORMATS",
    "FREQUENCIES",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "NOT_IMPLEMENTED",
    "NOT_IMPLEMENTED_ANSWER",
    "NUTS_VERSIONS",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "RESIDENCES",
    "STATUSES",
    "TourismFormatError",
    "TourismStatisticsAdapter",
    "document_key",
    "document_url",
    "excluded_dataset",
    "fixture_request",
    "fixture_transport",
    "parse_sdmx",
    "period_matches",
    "replay_native_fixture",
    "series_key",
    "tourism_declaration",
    "unverified",
]
