"""International trade-flow sources for the Economics ``trade`` features (#2210, TF01 and TF03-TF05).

Four providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`); three are implemented as
formats of the ``trade-flows`` source-pack connector, one is an operator import:

* **UN Comtrade** (``un-comtrade``, format ``comtrade-json``) - reported merchandise trade by reporter, partner,
  flow and commodity through the Comtrade API (``comtradeapi.un.org``), with a subscription key. Each declared
  document is one reporter's report for one bounded selection, fetched as two requests: the data-availability
  entry (the reporter-period release stamps) and the records. Every value, quantity, unit, CIF/FOB figure and
  estimation flag is kept verbatim. A document states whether it is the pair's *reporter* report or the partner's
  *mirror* report; both are acquired as separate observations and never reconciled.
* **Eurostat Comext** (``eurostat-comext``, format ``comext-jsonstat``) - EU trade in goods (CN8) through the
  existing :class:`~src.ingestion.connectors.dataset.eurostat.EurostatConnector` on its Comext dissemination path
  (``api: comext``, extended with :meth:`~EurostatConnector.parse_cells` so that no dimension is collapsed).
  Confidential or suppressed cells are recorded with their flag and no value, never zero-filled.
* **WITS concordances** (``wits``, format ``concordance-zip-csv``) - published product-code concordances between HS
  editions and SITC revisions (a zip holding one CSV). Mapping cardinality is kept as published or, when the table
  states only code pairs, derived from those pairs and labelled so; no weight is ever invented.
* **UN Statistics Division correlation tables** (``unsd-classifications``) - published as XLSX workbooks; the
  runtime does not parse XLSX (the optional ``openpyxl`` extra is not a runtime dependency), so an operator records
  a table's rows with the file digest and citation through :meth:`src.kb.trade_flows.TradeFlowStore.import_concordance`.

Every provider is ``unverified-live`` (or ``operator-import``) until a dated live run; the endpoint, parameter,
header and field names marked *verify* come from the providers' public documentation. See
``docs/development/trade-evidence/source-audit.md``. Nothing here estimates a missing or suppressed flow, nowcasts,
reconciles reporter and mirror figures, or infers sanctions evasion.
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

CONNECTOR = "trade-flows"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-trade-release-v1"
NEVER_SENTENCE = (
    "Reported trade flows as each reporter published them: reporter and mirror figures side by side and never "
    "reconciled into one value; no estimate of a missing or suppressed flow, no nowcast and no inference of "
    "sanctions evasion."
)
EXCLUSIONS = (
    "estimating or imputing missing, confidential or suppressed flows",
    "nowcasting",
    "reconciling reporter and mirror figures into one value",
    "inferring sanctions evasion or making compliance determinations",
    "re-allocating flows between areas",
    "converting flows between classifications with invented weights",
)

PROVIDER_HOSTS = {
    "un-comtrade": {"comtradeapi.un.org"},
    "eurostat-comext": {"ec.europa.eu"},
    "wits": {"wits.worldbank.org"},
    "unsd-classifications": {"unstats.un.org"},
}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "un-comtrade": {
        "delivers": "reported trade flows (reporter's own report; the partner's report is the mirror)",
        "access_decision": "unverified-live",
        "reason": "documented REST API with a subscription key (free tier); not yet run live from this runtime",
        "access": "api (UN Comtrade API v1, JSON)",
        "entry_points": [
            "https://comtradeapi.un.org/data/v1/get/{typeCode}/{freqCode}/{clCode} (verify)",
            "https://comtradeapi.un.org/data/v1/getDA/{typeCode}/{freqCode}/{clCode} (data availability; verify)",
        ],
        "authentication": "subscription key sent as the Ocp-Apim-Subscription-Key header (required-secret "
        "NOESIS_COMTRADE_KEY); a secret, never stored in request metadata",
        "rate_limits": "free tier: about 500 calls per day and up to 100000 records per call (verify the current "
        "figures on comtradedeveloper.un.org); each run is bounded by the source's max_pages and max_results",
        "pagination": "none; one call per declared selection, refused rather than truncated when the result budget "
        "is exceeded",
        "identifiers": {
            "areas": "reporterCode / partnerCode as UN M49 numeric codes with reporterISO / partnerISO as stated; "
            "special areas (World 0, Areas n.e.s. 899, bunkers 837, free zones 838, EU 97) stay distinct",
            "flows": "flowCode M import, X export, RM re-import, RX re-export (verify)",
            "classification": "classificationCode H0-H6 (HS1992-HS2022) and S1-S4 (SITC revisions) as reported; "
            "isOriginalClassification as stated",
            "commodity": "cmdCode with cmdDesc as published",
        },
        "update_cadence": "continuous; each reporter-period is released and revised on its own schedule",
        "temporal_semantics": "a release is dated by the data-availability lastReleased stamp of the reporter and "
        "period (firstReleased kept); without it the declared release date, else the retrieval time, labelled",
        "revision_model": "a re-release of a reporter-period with changed values is a new vintage; earlier "
        "vintages stay queryable",
        "classification_vintages": "HS editions H0 (1992), H1 (1996), H2 (2002), H3 (2007), H4 (2012), H5 (2017), "
        "H6 (2022); SITC S1-S4",
        "terms": "UN Comtrade terms of use: redistribution of extracted data restricted; attribution to UN "
        "Comtrade required (verify the current terms before redistributing any figure)",
        "retained_evidence": "raw JSON per request (file digest), release stamps per reporter-period",
        "coverage": "declared reporter/partner pairs, flows, HS codes and periods only",
        "unavailable_fallback": "a failed or rate-limited call leaves earlier vintages current and is reported "
        "in the run receipt",
        "verify": [
            "endpoint paths and the getDA path",
            "subscription-key header name",
            "field names (primaryValue, cifvalue, fobvalue, qty, qtyUnitAbbr, isQtyEstimated, "
            "legacyEstimationFlag, lastReleased)",
            "free-tier call and record limits",
            "redistribution terms",
        ],
    },
    "eurostat-comext": {
        "delivers": "reported EU trade in goods (CN8 and HS levels), reporter = EU member state",
        "access_decision": "unverified-live",
        "reason": "documented dissemination API without authentication, reached through the existing Eurostat "
        "connector's Comext path; dataset code and JSON-stat shape not yet run live",
        "access": "api (Eurostat Comext dissemination API, JSON-stat 2.0, via EurostatConnector api=comext)",
        "entry_points": [
            "https://ec.europa.eu/eurostat/api/comext/dissemination/statistics/1.0/data/{dataset} (verify)"
        ],
        "authentication": "none",
        "rate_limits": "no published per-user limit (verify); large extractions are asynchronous and out of scope; "
        "each request is bounded by its declared categories and the cube cell ceiling",
        "pagination": "none; one cube per declared selection",
        "identifiers": {
            "areas": "reporter and partner as Eurostat GEO codes (DE, FR, CN) with aggregates (EU27_2020, "
            "EXT_EU27_2020) kept distinct",
            "flows": "flow 1 import, 2 export",
            "classification": "CN8 codes of the reference year's Combined Nomenclature (CN vintage = the period's "
            "year); HS2/HS4/HS6 codes at shorter levels",
            "indicators": "VALUE_IN_EUROS, QUANTITY_IN_100KG, SUPPLEMENTARY_QUANTITY (verify)",
        },
        "update_cadence": "monthly releases with revisions of earlier months",
        "temporal_semantics": "the cube's updated stamp dates the release; the time dimension is the reference "
        "period",
        "revision_model": "a changed cube with a new updated stamp is a new vintage; earlier vintages stay",
        "classification_vintages": "CN is revised every year (CN year = the period's year); CN8 subdivides HS6",
        "terms": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with attribution",
        "retained_evidence": "raw JSON-stat per request (file digest), status labels and category labels",
        "coverage": "declared reporters, partners, CN8 codes, flows and months only",
        "confidentiality": "cells flagged confidential (status c) or without a published value are recorded as "
        "such with no value; never zero-filled or estimated",
        "unavailable_fallback": "a failed request leaves earlier vintages current",
        "verify": [
            "dataset code DS-045409 and the Comext path",
            "repeated-parameter selection",
            "status codes for confidential cells",
            "indicator codes",
        ],
    },
    "wits": {
        "delivers": "product-code concordances between HS editions and SITC revisions",
        "access_decision": "unverified-live",
        "reason": "published concordance files (zip with one CSV) without authentication; file paths and column "
        "headers not yet checked live",
        "access": "file (zip containing a CSV) on wits.worldbank.org",
        "entry_points": [
            "https://wits.worldbank.org/data/public/concordance/Concordance_{from}_to_{to}.zip (verify)"
        ],
        "authentication": "none",
        "rate_limits": "not documented; one download per declared table",
        "pagination": "none",
        "identifiers": {
            "tables": "the source and target nomenclature (H6 to H5, H6 to S4) of each file",
            "columns": "declared per document (verify the header names of each file)",
        },
        "update_cadence": "when a new HS edition or SITC revision is published",
        "temporal_semantics": "the declared table publication date; the file digest identifies the version",
        "revision_model": "a changed table is a new concordance revision; earlier revisions stay addressable",
        "terms": "World Bank WITS terms of use; the concordances derive from UN Statistics Division tables "
        "(verify attribution wording)",
        "retained_evidence": "the zip digest, the CSV member name and row numbers",
        "coverage": "declared tables only",
        "mapping_types": "WITS files state code pairs; cardinality (1:1, 1:n, n:1, n:n) is derived from the pairs "
        "of the file and labelled 'derived-from-published-pairs'; weights are never invented",
        "verify": ["file paths", "CSV member name", "column headers", "encoding"],
    },
    "unsd-classifications": {
        "delivers": "HS edition correlation tables and HS-SITC correspondence tables (UN Statistics Division)",
        "access_decision": "operator-import",
        "reason": "UNSD publishes the tables as XLSX workbooks; the runtime does not parse XLSX (openpyxl is an "
        "optional extra), so an operator records a table's rows with the workbook digest and citation; the "
        "relationship column (1:1, 1:n, n:1, n:n) is stored as published",
        "access": "operator import of a published workbook",
        "entry_points": ["https://unstats.un.org/unsd/classifications/Econ (verify)"],
        "authentication": "none",
        "coverage": "the tables an operator imports",
        "retained_evidence": "the workbook digest, sheet and row numbers as stated by the operator",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": contract["access_decision"],
        "note": "no dated live run from this runtime; offline fixtures only"
        if contract["access_decision"] == "unverified-live"
        else contract["reason"],
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# The bounded first coverage (TF01). No record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "un-comtrade": {
        "pairs": [["276", "156"], ["156", "276"]],
        "labels": "Germany (276) and China (156), each as reporter of the pair and as the other's mirror",
        "hs_chapters": ["29", "85"],
        "codes": ["293090", "854143"],
        "flows": ["M", "X"],
        "periods": "annual, two most recent reference years",
        "record_cap": "max_results per call (50) and max_pages per run (4 calls)",
    },
    "eurostat-comext": {
        "pairs": [["DE", "FR"], ["FR", "DE"]],
        "labels": "Germany and France, each as reporter and as the other's mirror (intra-EU)",
        "cn8": ["29309098", "85414300"],
        "flows": ["1", "2"],
        "periods": "monthly, the two most recent months declared per document",
        "record_cap": "max_results per cube (50 series) and a 2000-cell ceiling per cube",
    },
    "wits": {
        "tables": ["HS2022 (H6) to HS2017 (H5)", "HS2022 (H6) to SITC Rev.4 (S4)"],
        "record_cap": "max_results rows per table",
    },
    "unsd-classifications": {"tables": "operator-imported tables only"},
}

FORMATS = {
    "comtrade-json": {"provider": "un-comtrade", "kind": "flows"},
    "comext-jsonstat": {"provider": "eurostat-comext", "kind": "flows"},
    "concordance-zip-csv": {"provider": "wits", "kind": "concordance"},
    "operator-concordance": {"provider": "unsd-classifications", "kind": "concordance"},
}
# Comtrade classification codes -> (scheme, vintage).
COMTRADE_CLASSIFICATIONS = {
    "H0": ("HS", "HS1992"),
    "H1": ("HS", "HS1996"),
    "H2": ("HS", "HS2002"),
    "H3": ("HS", "HS2007"),
    "H4": ("HS", "HS2012"),
    "H5": ("HS", "HS2017"),
    "H6": ("HS", "HS2022"),
    "S1": ("SITC", "SITC1"),
    "S2": ("SITC", "SITC2"),
    "S3": ("SITC", "SITC3"),
    "S4": ("SITC", "SITC4"),
}
# WITS nomenclature codes in concordance file names.
WITS_NOMENCLATURES = {**COMTRADE_CLASSIFICATIONS}
SCHEMES = ("HS", "CN", "SITC")
COMTRADE_FLOWS = {
    "M": "import",
    "X": "export",
    "RM": "re-import",
    "RX": "re-export",
}
COMEXT_FLOWS = {"1": "import", "2": "export"}
MIRROR_FLOW = {
    "import": "export",
    "export": "import",
    "re-import": "re-export",
    "re-export": "re-import",
}
# The documented valuation convention when a record states no separate CIF/FOB figure: imports at CIF-type value,
# exports at FOB-type value (UN IMTS 2010 recommendation; Comtrade and Comext documentation - verify).
VALUATION_CONVENTION = {
    "import": "CIF",
    "re-import": "CIF",
    "export": "FOB",
    "re-export": "FOB",
}
COMTRADE_FLAG_FIELDS = (
    "isQtyEstimated",
    "isAltQtyEstimated",
    "isNetWgtEstimated",
    "isGrossWgtEstimated",
    "legacyEstimationFlag",
    "isReported",
    "isAggregate",
    "isOriginalClassification",
)
COMTRADE_DIMENSIONS = ("customsCode", "motCode", "mosCode", "partner2Code")
COMEXT_CONFIDENTIAL = {"c"}
COMEXT_VALUE_INDICATOR = "VALUE_IN_EUROS"
COMEXT_QUANTITY_UNITS = {
    "QUANTITY_IN_100KG": "100 kg",
    "SUPPLEMENTARY_QUANTITY": "supplementary unit",
}
ROLES = ("reporter", "mirror")


class TradeFormatError(ValueError):
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


def comtrade_period(value: Any, freq: str) -> str:
    raw = str(value).strip()
    if freq == "M" and re.fullmatch(r"\d{6}", raw):
        return f"{raw[:4]}-{raw[4:]}"
    return raw


def comext_period(value: Any) -> tuple[str, str]:
    """(normalised period, frequency) of a Comext time code (``2099M01`` -> ``2099-01``, monthly)."""
    raw = str(value).strip()
    match = re.fullmatch(r"(\d{4})M(\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}", "monthly"
    if re.fullmatch(r"\d{4}", raw):
        return raw, "annual"
    match = re.fullmatch(r"(\d{4})Q([1-4])", raw)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}", "quarterly"
    return raw, "unknown"


def mapping_types(pairs: Sequence[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """Cardinality of each published code pair within one table: 1:1, 1:n, n:1 or n:n."""
    sources: dict[str, set[str]] = {}
    targets: dict[str, set[str]] = {}
    for source, target in pairs:
        sources.setdefault(source, set()).add(target)
        targets.setdefault(target, set()).add(source)
    out = {}
    for source, target in pairs:
        many_targets = len(sources[source]) > 1
        many_sources = len(targets[target]) > 1
        out[(source, target)] = (
            "n:n" if many_targets and many_sources else "1:n" if many_targets else "n:1" if many_sources else "1:1"
        )
    return out


# ------------------------------------------------------------------ declarations


def trade_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("trade_flows") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or fmt == "operator-concordance" or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError(
            "invalid_manifest", "trade-flows sources declare a known provider and its runtime format"
        )
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "a trade-flows source declares its documents")
    if len(documents) * (2 if fmt == "comtrade-json" else 1) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared requests than the source's page budget")
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError("invalid_manifest", "the endpoint is not the provider's documented host")
    urls = []
    for document in documents:
        try:
            check_document(fmt, document)
            url = document_url(fmt, document, source["endpoint"])
        except TradeFormatError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return declared


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")):
        raise TradeFormatError("invalid_document", "a document has a label")
    release = document.get("release")
    if release is not None and iso_day(dict(release).get("published_on")) is None:
        raise TradeFormatError("invalid_document", "a declared release states its publication date")
    if FORMATS[fmt]["kind"] == "flows":
        role, pair = document.get("role"), dict(document.get("pair") or {})
        if role not in ROLES or not pair.get("reporter") or not pair.get("partner"):
            raise TradeFormatError(
                "invalid_document", "a flow document states its role (reporter or mirror) and the pair it serves"
            )
        reporter = str(document.get("reporter") or "")
        partners = [str(p) for p in document.get("partners") or []]
        expected = pair["reporter"] if role == "reporter" else pair["partner"]
        other = pair["partner"] if role == "reporter" else pair["reporter"]
        if reporter != str(expected) or str(other) not in partners:
            raise TradeFormatError(
                "invalid_document",
                "a reporter document is the pair reporter's own report; a mirror document is the partner's report "
                "of the same pair",
            )
    if fmt == "comtrade-json":
        if document.get("classification") != "HS" or document.get("type", "C") != "C" or document.get(
            "freq"
        ) not in {"A", "M"}:
            raise TradeFormatError("invalid_document", "Comtrade documents select goods (C), A or M and HS")
        for key in ("reporter", "partners", "flows", "commodities", "periods"):
            if not document.get(key):
                raise TradeFormatError("invalid_document", f"Comtrade documents declare {key}")
        if set(document["flows"]) - set(COMTRADE_FLOWS):
            raise TradeFormatError("invalid_document", "unknown Comtrade flow code")
        if not all(re.fullmatch(r"\d{2,6}", str(c)) for c in document["commodities"]):
            raise TradeFormatError("invalid_document", "Comtrade commodities are HS codes of 2 to 6 digits")
        if not all(re.fullmatch(r"\d{1,3}", str(c)) for c in [document["reporter"], *document["partners"]]):
            raise TradeFormatError("invalid_document", "Comtrade areas are M49 numeric codes")
    elif fmt == "comext-jsonstat":
        filters = dict(document.get("filters") or {})
        if not document.get("dataset") or not document.get("reporter") or not filters.get("product"):
            raise TradeFormatError("invalid_document", "Comext documents name a dataset, a reporter and products")
        if set(filters.get("flow") or []) - set(COMEXT_FLOWS):
            raise TradeFormatError("invalid_document", "unknown Comext flow code")
        if COMEXT_VALUE_INDICATOR not in (filters.get("indicators") or []):
            raise TradeFormatError("invalid_document", "Comext documents select the value indicator")
        if [str(p) for p in document.get("partners") or []] != [str(p) for p in filters.get("partner") or []]:
            raise TradeFormatError("invalid_document", "Comext partners are the filter's partner categories")
    elif fmt == "concordance-zip-csv":
        columns = dict(document.get("columns") or {})
        if not document.get("url") or not columns.get("source_code") or not columns.get("target_code"):
            raise TradeFormatError("invalid_document", "a concordance file declares its URL and code columns")
        for side in ("source", "target"):
            classification = dict(document.get(side) or {})
            if classification.get("scheme") not in SCHEMES or not classification.get("vintage"):
                raise TradeFormatError("invalid_document", f"a concordance states its {side} scheme and vintage")


def document_url(fmt: str, document: Mapping[str, Any], endpoint: str = "", *, part: str = "data") -> str:
    if fmt == "comtrade-json":
        base = str(endpoint or "https://comtradeapi.un.org/data/v1").rstrip("/")
        path = "getDA" if part == "availability" else "get"
        kind = f"{document.get('type', 'C')}/{document['freq']}/{document['classification']}"
        if part == "availability":
            params = {"reporterCode": str(document["reporter"]), "period": ",".join(map(str, document["periods"]))}
        else:
            params = {
                "reporterCode": str(document["reporter"]),
                "partnerCode": ",".join(map(str, document["partners"])),
                "flowCode": ",".join(document["flows"]),
                "cmdCode": ",".join(map(str, document["commodities"])),
                "period": ",".join(map(str, document["periods"])),
                "includeDesc": "true",
                **{str(k): str(v) for k, v in dict(document.get("filters") or {}).items()},
            }
        return f"{base}/{path}/{kind}?" + urlencode(sorted(params.items()))
    if fmt == "comext-jsonstat":
        from src.ingestion.connectors.dataset.eurostat import EurostatConnector

        return EurostatConnector().comext_url(
            str(document["dataset"]), str(document["reporter"]), dict(document.get("filters") or {})
        )
    if fmt == "concordance-zip-csv":
        return str(document["url"])
    raise TradeFormatError("invalid_document", f"no URL for format {fmt}")


# ------------------------------------------------------------------ parsing


def _declared_release(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    release = dict(document.get("release") or {})
    return iso_day(release.get("published_on")), text(release.get("label"))


def _area(scheme: str, code: Any, label: Any = None, iso3: Any = None) -> dict[str, Any]:
    out = {"scheme": scheme, "code": str(code).strip()}
    if text(label):
        out["label"] = text(label)
    if text(iso3):
        out["iso3"] = text(iso3)
    return out


def parse_comtrade(
    raw: bytes, *, document: Mapping[str, Any], availability: bytes | None = None
) -> dict[str, Any]:
    """One Comtrade reporter report: series per (partner, flow, commodity, classification, dimensions), values
    and flags verbatim, released at the data-availability lastReleased stamp of the reporter and periods."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TradeFormatError("schema_drift", "Comtrade response is not JSON") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("data"), list):
        raise TradeFormatError("schema_drift", "Comtrade response has no data array")
    if text(payload.get("error")):
        raise TradeFormatError("provider_error", f"Comtrade error: {payload['error']}")
    freq = str(document["freq"])
    stamps: dict[str, dict[str, Any]] = {}
    if availability is not None:
        try:
            available = json.loads(availability.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TradeFormatError("schema_drift", "Comtrade availability response is not JSON") from exc
        for row in list(dict(available).get("data") or []):
            if str(row.get("reporterCode")) != str(document["reporter"]):
                continue
            stamps[str(row.get("period"))] = {
                "period": str(row.get("period")),
                "classification": text(row.get("classificationCode")),
                "first_released": iso_instant(row.get("firstReleased")),
                "last_released": iso_instant(row.get("lastReleased")),
                "total_records": row.get("totalRecords"),
            }
    series: dict[str, dict[str, Any]] = {}
    rejected = []
    for number, record in enumerate(payload["data"]):
        record = dict(record)
        reporter = str(record.get("reporterCode"))
        classification_code = text(record.get("classificationCode"))
        flow = COMTRADE_FLOWS.get(str(record.get("flowCode")))
        if (
            reporter != str(document["reporter"])
            or classification_code not in COMTRADE_CLASSIFICATIONS
            or flow is None
            or text(record.get("cmdCode")) is None
            or text(record.get("period")) is None
        ):
            rejected.append({"row": number, "reason": "record outside the declared report or with unknown codes"})
            continue
        scheme, vintage = COMTRADE_CLASSIFICATIONS[classification_code]
        cif, fob = decimal_text(record.get("cifvalue")), decimal_text(record.get("fobvalue"))
        valuation = (
            {"basis": "CIF", "source": "reported"}
            if cif is not None and fob is None
            else {"basis": "FOB", "source": "reported"}
            if fob is not None and cif is None
            else {"basis": VALUATION_CONVENTION[flow], "source": "documented-convention"}
        )
        dimensions = {k: text(record.get(k)) for k in COMTRADE_DIMENSIONS if text(record.get(k)) is not None}
        key_body = {
            "reporter": reporter,
            "partner": str(record.get("partnerCode")),
            "flow": str(record.get("flowCode")),
            "product": str(record["cmdCode"]),
            "classification": classification_code,
            "dimensions": dimensions,
            "valuation": valuation["basis"],
        }
        key = canonical(key_body)
        entry = series.setdefault(
            key,
            {
                "reporter": _area("m49", reporter, record.get("reporterDesc"), record.get("reporterISO")),
                "partner": _area("m49", record.get("partnerCode"), record.get("partnerDesc"), record.get("partnerISO")),
                "flow": {"code": str(record["flowCode"]), "label": text(record.get("flowDesc")), "direction": flow},
                "product": {"code": str(record["cmdCode"]), "label": text(record.get("cmdDesc"))},
                "classification": {"scheme": scheme, "vintage": vintage, "code": classification_code},
                "frequency": "annual" if freq == "A" else "monthly",
                "valuation": valuation,
                "role": document["role"],
                "pair": {k: str(v) for k, v in dict(document["pair"]).items()},
                "measure": "trade_value",
                "unit": {"currency": "USD", "scale": "1"},
                "dimensions": dimensions,
                "observations": [],
            },
        )
        value_text = text(record.get("primaryValue"))
        quantities = []
        for field, unit_field, flag in (
            ("qty", "qtyUnitAbbr", "isQtyEstimated"),
            ("altQty", "altQtyUnitAbbr", "isAltQtyEstimated"),
            ("netWgt", None, "isNetWgtEstimated"),
            ("grossWgt", None, "isGrossWgtEstimated"),
        ):
            if text(record.get(field)) is None:
                continue
            quantities.append(
                {
                    "kind": field,
                    "value_text": text(record.get(field)),
                    "value": decimal_text(record.get(field)),
                    "unit": text(record.get(unit_field)) if unit_field else "kg",
                    "unit_code": text(record.get(unit_field.replace("Abbr", "Code"))) if unit_field else None,
                    "estimated": record.get(flag),
                }
            )
        entry["observations"].append(
            {
                "period": comtrade_period(record["period"], freq),
                "value_text": value_text,
                "value": decimal_text(value_text),
                "status": "reported" if decimal_text(value_text) is not None else "not_published",
                "cif_value": cif,
                "fob_value": fob,
                "quantities": quantities,
                "flags": {k: record.get(k) for k in COMTRADE_FLAG_FIELDS if k in record},
                "row": number,
            }
        )
    items = [dict(v) for _, v in sorted(series.items())]
    for item in items:
        item["observations"].sort(key=lambda o: o["period"])
        periods = [o["period"] for o in item["observations"]]
        if len(set(periods)) != len(periods):
            raise TradeFormatError("schema_drift", "a Comtrade series states the same period twice")
    released = sorted({s["last_released"] for s in stamps.values() if s["last_released"]})
    declared_on, declared_label = _declared_release(document)
    if released:
        published_at, basis = released[-1], "comtrade_last_released"
        published_on = published_at[:10]
    elif declared_on:
        published_at, published_on, basis = None, declared_on, "declared_release"
    else:
        published_at, published_on, basis = None, None, "retrieval_time"
    return {
        "kind": "flows",
        "provider": "un-comtrade",
        "format": "comtrade-json",
        "items": items,
        "item_count": len(items),
        "published_on": published_on,
        "published_at": published_at,
        "release_basis": basis,
        "release_label": declared_label,
        "file_sha256": hashlib.sha256(raw + (availability or b"")).hexdigest(),
        "content_sha256": digest(items),
        "structure": {
            "availability": [stamps[k] for k in sorted(stamps)],
            "rejected": rejected,
            "records": len(payload["data"]),
            "count_stated": payload.get("count"),
        },
    }


def parse_comext(raw: bytes, *, document: Mapping[str, Any], url: str, max_cells: int = 2000) -> dict[str, Any]:
    """A Comext cube as trade-flow series: one per (partner, flow, CN8 product, CN year), value and quantities per
    month, confidential and unpublished cells kept with their flag and no value."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.eurostat import EurostatConnector

    ref = SeriesRef(locator=f"{document['dataset']}/{document['reporter']}", metadata={"api": "comext"})
    try:
        cube = EurostatConnector().parse_cells(
            RawSeries(ref, raw, content_type="application/json", source_url=url), max_cells=max_cells
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise TradeFormatError("schema_drift", f"Comext cube: {exc}") from exc
    required = {"reporter", "partner", "product", "flow", "indicators", "time"}
    if not required <= set(cube["dimension_ids"]):
        raise TradeFormatError("schema_drift", "the Comext cube lacks a trade dimension")
    labels = {d: cube["dimensions"][d]["categories"] for d in cube["dimension_ids"]}
    series: dict[str, dict[str, Any]] = {}
    extra_dims = [d for d in cube["dimension_ids"] if d not in required and d != "freq"]
    for cell in cube["cells"]:
        dims = cell["dimensions"]
        if str(dims["reporter"]) != str(document["reporter"]):
            continue
        flow = COMEXT_FLOWS.get(str(dims["flow"]))
        if flow is None:
            continue
        period, frequency = comext_period(cell["time"])
        cn_year = period[:4]
        product = str(dims["product"])
        scheme = "CN" if len(product) == 8 else "HS"
        vintage = f"CN{cn_year}" if scheme == "CN" else f"CN{cn_year}-HS-level"
        other = {d: str(dims[d]) for d in extra_dims}
        key = canonical([dims["partner"], dims["flow"], product, scheme, vintage, frequency, other])
        entry = series.setdefault(
            key,
            {
                "reporter": _area("eurostat-geo", dims["reporter"], labels["reporter"].get(dims["reporter"])),
                "partner": _area("eurostat-geo", dims["partner"], labels["partner"].get(dims["partner"])),
                "flow": {"code": str(dims["flow"]), "label": labels["flow"].get(dims["flow"]), "direction": flow},
                "product": {"code": product, "label": labels["product"].get(product)},
                "classification": {"scheme": scheme, "vintage": vintage, "code": scheme},
                "frequency": frequency,
                "valuation": {"basis": VALUATION_CONVENTION[flow], "source": "documented-convention"},
                "role": document["role"],
                "pair": {k: str(v) for k, v in dict(document["pair"]).items()},
                "measure": "trade_value",
                "unit": {"currency": "EUR", "scale": "1"},
                "dimensions": other,
                "periods": {},
            },
        )
        observation = entry["periods"].setdefault(
            period,
            {"period": period, "value_text": None, "value": None, "status": None, "flags": {}, "quantities": []},
        )
        indicator = str(dims["indicators"])
        status = text(cell["status"])
        value_text = None if cell["value"] is None else str(cell["value"])
        if indicator == COMEXT_VALUE_INDICATOR:
            observation["value_text"] = value_text
            observation["value"] = decimal_text(value_text)
            observation["status"] = (
                "confidential"
                if status in COMEXT_CONFIDENTIAL
                else "reported"
                if observation["value"] is not None
                else "not_published"
            )
            if status:
                observation["flags"]["status"] = status
                observation["flags"]["status_label"] = cube["status_labels"].get(status)
        else:
            observation["quantities"].append(
                {
                    "kind": indicator,
                    "value_text": value_text,
                    "value": decimal_text(value_text),
                    "unit": COMEXT_QUANTITY_UNITS.get(indicator, indicator),
                    "status": status,
                    "confidential": status in COMEXT_CONFIDENTIAL,
                }
            )
    items = []
    for _, entry in sorted(series.items()):
        periods = entry.pop("periods")
        entry["observations"] = []
        for period in sorted(periods):
            observation = periods[period]
            if observation["status"] is None:
                # Only quantity indicators were published for this month: the value was not published.
                observation["status"] = "not_published"
            observation["quantities"].sort(key=lambda q: q["kind"])
            entry["observations"].append(observation)
        items.append(entry)
    declared_on, declared_label = _declared_release(document)
    if cube["updated_at_ms"] is not None:
        published_at = datetime.fromtimestamp(cube["updated_at_ms"] / 1000, tz=timezone.utc).isoformat()
        published_on, basis = published_at[:10], "eurostat_dataset_updated"
    elif declared_on:
        published_at, published_on, basis = None, declared_on, "declared_release"
    else:
        published_at, published_on, basis = None, None, "retrieval_time"
    return {
        "kind": "flows",
        "provider": "eurostat-comext",
        "format": "comext-jsonstat",
        "items": items,
        "item_count": len(items),
        "published_on": published_on,
        "published_at": published_at,
        "release_basis": basis,
        "release_label": declared_label,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(items),
        "structure": {
            "dataset": document["dataset"],
            "cube_label": cube["label"],
            "updated": cube["updated"],
            "status_labels": cube["status_labels"],
            "cell_count": cube["cell_count"],
            "absent_cells": cube["absent_cells"],
            "absent_cells_note": "cells absent from the published cube are not stored and never zero-filled",
        },
    }



def parse_concordance_zip(raw: bytes, *, document: Mapping[str, Any], max_rows: int = 20000) -> dict[str, Any]:
    """A published concordance file (a zip holding one CSV): code pairs, labels per vintage, cardinality as
    published or derived from the file's pairs, weights only when the file states them."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise TradeFormatError("schema_drift", "the concordance file is not a zip archive") from exc
    members = [n for n in archive.namelist() if n.lower().endswith(".csv") and not n.endswith("/")]
    wanted = text(document.get("member"))
    if wanted:
        members = [n for n in members if n == wanted]
    if len(members) != 1:
        raise TradeFormatError("schema_drift", "the concordance archive does not hold exactly one declared CSV")
    info = archive.getinfo(members[0])
    if info.file_size > 50_000_000:
        raise TradeFormatError("input_limit", "the concordance CSV is larger than allowed")
    body = archive.read(members[0])
    encoding = str(document.get("encoding") or "utf-8-sig")
    try:
        reader = csv.DictReader(io.StringIO(body.decode(encoding)))
    except (UnicodeDecodeError, LookupError) as exc:
        raise TradeFormatError("schema_drift", "the concordance CSV is not in the declared encoding") from exc
    columns = dict(document["columns"])
    header = list(reader.fieldnames or [])
    missing = [c for c in columns.values() if c and c not in header]
    if missing:
        raise TradeFormatError("schema_drift", f"the concordance CSV lacks declared columns {missing}")
    rows = []
    for number, row in enumerate(reader, start=2):
        if len(rows) >= max_rows:
            raise TradeFormatError("input_limit", "the concordance has more rows than the source's budget")
        source_code, target_code = text(row.get(columns["source_code"])), text(row.get(columns["target_code"]))
        if source_code is None or target_code is None:
            continue
        rows.append(
            {
                "source_code": source_code,
                "target_code": target_code,
                "source_label": text(row.get(columns.get("source_label") or "")),
                "target_label": text(row.get(columns.get("target_label") or "")),
                "published_mapping_type": text(row.get(columns.get("mapping_type") or "")),
                "weight": text(row.get(columns.get("weight") or "")),
                "note": text(row.get(columns.get("note") or "")),
                "row": number,
            }
        )
    if not rows:
        raise TradeFormatError("schema_drift", "the concordance states no code pair")
    derived = mapping_types([(r["source_code"], r["target_code"]) for r in rows])
    for row in rows:
        published = (row.pop("published_mapping_type") or "").replace(" ", "").lower()
        if published in {"1:1", "1:n", "n:1", "n:n"}:
            row["mapping_type"], row["mapping_basis"] = published, "published"
        else:
            row["mapping_type"] = derived[(row["source_code"], row["target_code"])]
            row["mapping_basis"] = "derived-from-published-pairs"
        if row["weight"] is None:
            row.pop("weight")
            row["weight_state"] = "not-published"
        else:
            row["weight_state"] = "published"
    rows.sort(key=lambda r: (r["source_code"], r["target_code"]))
    item = {
        "concordance": {
            "label": document["label"],
            "source": dict(document["source"]),
            "target": dict(document["target"]),
            "member": members[0],
            "rows": rows,
        }
    }
    declared_on, declared_label = _declared_release(document)
    return {
        "kind": "concordance",
        "provider": "wits",
        "format": "concordance-zip-csv",
        "items": [item],
        "item_count": 1,
        "published_on": declared_on,
        "published_at": None,
        "release_basis": "declared_release" if declared_on else "retrieval_time",
        "release_label": declared_label,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(item),
        "structure": {"member": members[0], "header": header, "rows": len(rows)},
    }


# ------------------------------------------------------------------ runtime adapter


class TradeFlowsAdapter:
    """Fetch the declared trade documents on the runtime's default transport; one page (one release) per document."""

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
        self.declared = trade_declaration(self.source)
        self.secret = secret
        if transport is None:
            from functools import partial

            # The runtime's default transport: same-host public redirects only, a byte ceiling and the timeout.
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
            "trade_flows": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "keyed": bool(secret),
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
            raise SourcePackError("parameter_forbidden", "trade runs fetch the declared documents only")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/csv, application/zip"}
        if self.declared["format"] == "comtrade-json":
            if not self.secret:
                raise SourcePackError("authentication_failed", "UN Comtrade requires its subscription key")
            headers["Ocp-Apim-Subscription-Key"] = self.secret
        return headers

    def _get(self, url: str, headers: Mapping[str, str]) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the endpoint's host only")
        base, _, query = url.partition("?")
        response = self.transport(
            url=base,
            params=parse_qsl(query, keep_blank_values=True),
            headers=dict(headers),
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(response_headers.get("retry-after")),
            )
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
        headers = self._headers()
        endpoint = self.source["endpoint"]
        origins, availability, size = [], None, 0
        if fmt == "comtrade-json":
            availability, origin = self._get(document_url(fmt, document, endpoint, part="availability"), headers)
            origins.append(origin)
            size += len(availability)
        url = document_url(fmt, document, endpoint)
        raw, origin = self._get(url, headers)
        origins.append(origin)
        size += len(raw)
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        try:
            if fmt == "comtrade-json":
                release = parse_comtrade(raw, document=document, availability=availability)
            elif fmt == "comext-jsonstat":
                release = parse_comext(raw, document=document, url=url)
            else:
                release = parse_concordance_zip(raw, document=document, max_rows=limit)
        except TradeFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift", f"{exc.code}: {exc}"
            ) from exc
        if release["kind"] == "flows" and release["item_count"] > limit:
            # Never a truncated release: a missing series would read as a flow that was not reported.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
        evidence_origin = "fixture" if set(origins) == {"fixture"} else "live"
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": release["provider"],
            "format": release["format"],
            "kind": release["kind"],
            "document": document,
            "published_on": release["published_on"],
            "published_at": release["published_at"],
            "release_basis": release["release_basis"],
            "release_label": release["release_label"],
            "file_sha256": release["file_sha256"],
            "content_sha256": release["content_sha256"],
            "item_count": release["item_count"],
            "structure": release["structure"],
            "evidence_origin": evidence_origin,
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
                "trade_release": header,
                "trade_item": item,
            }
            for number, item in enumerate(release["items"])
        ]
        receipt = {
            "status": 200,
            "provider": release["provider"],
            "document": document.get("label"),
            "published_on": release["published_on"],
            "release_basis": release["release_basis"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "requests": len(origins),
            "evidence_origin": evidence_origin,
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, size, receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: TradeFlowsAdapter}


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
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            "origin": "fixture",
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def fixture_request(fmt: str, document: Mapping[str, Any], endpoint: str = "", *, part: str = "data") -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""
    parts = urlsplit(document_url(fmt, document, endpoint, part=part))
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return parts.path + ("?" + query if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = TradeFlowsAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"])), secret=FIXTURE_SECRET
    )
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
    "CONNECTOR",
    "EXCLUSIONS",
    "LIVE_VERIFICATION",
    "MIRROR_FLOW",
    "NEVER_SENTENCE",
    "PROVIDER_CONTRACTS",
    "TradeFlowsAdapter",
    "TradeFormatError",
    "fixture_request",
    "fixture_transport",
    "mapping_types",
    "parse_comext",
    "parse_comtrade",
    "parse_concordance_zip",
    "replay_native_fixture",
    "trade_declaration",
]
