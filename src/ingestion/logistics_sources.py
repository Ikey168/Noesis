"""Shipping and logistics sources for the Economics ``logistics`` feature (#2229, SL01 and SL03-SL06).

Five providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`); four are formats of the
``logistics`` source-pack connector, and every candidate freight index has a licence decision
(:data:`FREIGHT_INDEX_DECISIONS`):

* **UN/LOCODE** (``unece-unlocode``, format ``unlocode-csv``) - the UNECE code list release (a zip holding the
  headerless CSV parts, or one CSV), read for the declared countries' entries with port function (function
  position 1 = ``1``). Code, name, subdivision, function, status, date, IATA code, coordinates and the change
  indicator are kept as published; each release is a version declared by the operator (``2099-1``).
* **UNCTADstat** (``unctadstat``, format ``unctadstat-csv``) - maritime reports (port calls, container port
  throughput, liner shipping connectivity, merchant fleet by flag) as the data centre's bulk CSV, with the declared
  column names, series definition and release. UNCTAD port identifiers are kept beside any UN/LOCODE the file
  publishes; country series keep M49 codes. The bulk files are documented as 7z archives (*verify*): the runtime
  reads CSV or zip only and refuses 7z (``unsupported_archive``); an operator extracts the CSV and records it with
  :func:`src.kb.logistics_series.operator_import` (evidence origin ``operator``). UNCTADstat does not expose an SDMX
  endpoint this runtime can confirm, so :mod:`src.ingestion.connectors.dataset.sdmx` is not used (*verify*).
* **Eurostat maritime transport** (``eurostat-maritime``, format ``eurostat-maritime-jsonstat``) - the ``mar_*``
  datasets through the existing :class:`~src.ingestion.connectors.dataset.eurostat.EurostatConnector`
  (:meth:`~EurostatConnector.dataset_url` and :meth:`~EurostatConnector.parse_cells`, no dimension collapsed).
  Reporting-port codes (``rep_mar``) and partner ports (``par_mar``) are kept as published; mapping to UN/LOCODE is
  left to reviewable identity (SL07) and a route series is only ever a published port-to-partner series.
* **BLS producer price index for deep sea freight transportation** (``bls-ppi``, format ``bls-timeseries-json``) -
  the one freight index cleared as openly licensed (US federal statistics, public domain), through the BLS public
  API v2 without a key. Preliminary values and their footnotes are kept; a revised value is a new vintage.

Commercial freight indices without a redistribution licence (Baltic Exchange indices, Drewry WCI, Freightos FBX,
SCFI, Xeneta XSI) are recorded as excluded and never acquired. Every provider is ``unverified-live`` until a dated
live run; names marked *verify* come from the providers' public documentation. See
``docs/roadmaps/economics-logistics-source-audit.md``. Nothing here forecasts freight rates, derives an index,
rebases, interpolates or infers a route from port totals.
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

CONNECTOR = "logistics"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-logistics-release-v1"
NEVER_SENTENCE = (
    "Published maritime and logistics statistics as each publisher released them: sources side by side and never "
    "merged; no freight-rate forecast, no derived, rebased or interpolated index, and no route inferred from port "
    "totals."
)
EXCLUSIONS = (
    "freight-rate forecasting or rate predictions",
    "redistributing commercial freight indices without a licence",
    "deriving, rebasing, chaining or interpolating index values",
    "inferring route flows from separate port totals",
    "merging series of different sources, units or frequencies",
    "derived trade-per-throughput ratios",
)

PROVIDER_HOSTS = {
    "unece-unlocode": {"service.unece.org"},
    "unctadstat": {"unctadstat-api.unctad.org"},
    "eurostat-maritime": {"ec.europa.eu"},
    "bls-ppi": {"api.bls.gov"},
}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "unece-unlocode": {
        "delivers": "UN/LOCODE entries (ports and other locations) per code-list release",
        "access_decision": "unverified-live",
        "reason": "public release files without authentication; file names, CSV part layout and encoding not yet "
        "checked live",
        "access": "file (zip holding the CSV parts of one release) on service.unece.org",
        "format": "headerless CSV: change indicator, country, location, name, name without diacritics, subdivision, "
        "function, status, date, IATA, coordinates, remarks (verify the column order and ISO-8859-1 encoding)",
        "entry_points": ["https://service.unece.org/trade/locode/loc{yy}{n}csv.zip (verify)"],
        "authentication": "none",
        "rate_limits": "not documented; one download per declared release",
        "identifiers": {
            "unlocode": "ISO 3166-1 alpha-2 country code plus a three-character location code (DEHAM)",
            "function": "eight positions; position 1 = port, as published",
            "status": "entry status code (AA, AC, AI, RL, ...) as published",
        },
        "update_cadence": "about twice a year (release 2024-1, 2024-2)",
        "temporal_semantics": "the declared release version and publication date; the file digest identifies it",
        "revision_model": "each release is a version; a changed entry adds a revision, a code absent from a later "
        "release of the same countries (or published with change indicator X) is marked removed, never deleted",
        "terms": "UNECE terms of use: free reuse with attribution to UNECE (verify the current wording)",
        "licence": {"id": "unece-terms", "attribution": "UN/LOCODE, United Nations Economic Commission for Europe"},
        "retained_evidence": "zip digest, member name and row number per entry",
        "verify": ["release file names", "member names of the parts", "column order", "encoding"],
    },
    "unctadstat": {
        "delivers": "maritime series: port calls and time in port, container port throughput, liner shipping "
        "connectivity (country and port), merchant fleet by flag of registration",
        "access_decision": "unverified-live",
        "reason": "the data centre's bulk downloads need no key but are documented as 7z archives that the runtime "
        "does not unpack; CSV or zip bodies are read, a 7z body is refused and the extracted CSV is an operator "
        "import; the data API (registered client id and secret) and SDMX are not used",
        "access": "bulk CSV per report (verify the path and archive type); operator import of an extracted CSV",
        "format": "CSV with declared column names per report (verify each header)",
        "entry_points": ["https://unctadstat-api.unctad.org/bulkdownload/{report}/{file} (verify)"],
        "authentication": "none for bulk files (verify)",
        "rate_limits": "not documented; one download per declared report, bounded by max_pages and max_results",
        "identifiers": {
            "economies": "UN M49 numeric economy codes as published (276 Germany, 528 Netherlands)",
            "ports": "UNCTAD port identifiers as published, with the UN/LOCODE column where the report states it",
            "series": "report code (US.PortCalls, US.ContPortThroughput, US.PLSCI, US.MerchantFleet; verify)",
        },
        "update_cadence": "per report: annual, semi-annual or quarterly",
        "temporal_semantics": "the declared release date (the report's last-update date on the data centre); "
        "without it the retrieval time, labelled",
        "revision_model": "a release that changes values is a new vintage; methodology changes stated in the "
        "report's notes are recorded as series breaks",
        "terms": "UNCTADstat terms of use: reuse with attribution to UNCTAD (verify)",
        "licence": {"id": "unctad-terms", "attribution": "UNCTADstat, United Nations Conference on Trade and "
                    "Development"},
        "retained_evidence": "file digest, header, row numbers and footnotes",
        "verify": ["bulk paths", "archive type", "column headers", "report codes", "release dates"],
    },
    "eurostat-maritime": {
        "delivers": "goods and passengers by main port, vessel traffic and port-to-partner-port flows",
        "access_decision": "unverified-live",
        "reason": "documented dissemination API without authentication, reached through the existing Eurostat "
        "connector; dataset codes and dimension names not yet run live",
        "access": "api (Eurostat dissemination API, JSON-stat 2.0, via EurostatConnector.dataset_url)",
        "format": "JSON-stat 2.0 cube per declared selection",
        "entry_points": ["https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{dataset} (verify)"],
        "authentication": "none",
        "rate_limits": "no published per-user limit (verify); each request bounded by its categories and a cell "
        "ceiling",
        "identifiers": {
            "ports": "rep_mar reporting-port codes and par_mar partner ports or coastal areas as published "
            "(verify the code list); never rewritten to UN/LOCODE here",
            "countries": "geo codes (Eurostat GEO, ISO 3166 alpha-2 except EL and UK)",
            "datasets": "mar_mg_aa_pwhd, mar_mg_aa_cwh, mar_go_am_{cc} (verify)",
        },
        "update_cadence": "quarterly and annual releases with revisions",
        "temporal_semantics": "the cube's updated stamp dates the release",
        "revision_model": "a changed cube with a new updated stamp is a new vintage",
        "terms": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with attribution",
        "licence": {"id": "eurostat-reuse", "attribution": "Eurostat"},
        "retained_evidence": "raw JSON-stat digest, status labels, category labels",
        "verify": ["dataset codes", "rep_mar/par_mar code lists", "unit codes", "status flags"],
    },
    "bls-ppi": {
        "delivers": "producer price index for deep sea freight transportation (NAICS 483111), monthly",
        "access_decision": "unverified-live",
        "reason": "BLS public API v2 without a key (daily query limit); series id and base period not yet checked live",
        "access": "api (BLS public API v2, GET one series, JSON)",
        "format": "JSON: Results.series[].data[] with year, period (M01-M13), value and footnotes",
        "entry_points": ["https://api.bls.gov/publicAPI/v2/timeseries/data/{series_id} (verify)"],
        "authentication": "none (a registration key would travel in the request, so none is used)",
        "rate_limits": "without registration about 25 queries a day and 10 years per query (verify)",
        "identifiers": {"series": "PCU483111483111 (verify)"},
        "update_cadence": "monthly; values are preliminary for four months and then revised",
        "temporal_semantics": "the declared PPI release date; without it the retrieval time, labelled",
        "revision_model": "a revised preliminary value is a new vintage; the P footnote is kept as published",
        "terms": "US federal statistics in the public domain; citation of BLS requested",
        "licence": {"id": "us-public-domain", "attribution": "U.S. Bureau of Labor Statistics"},
        "retained_evidence": "raw JSON digest, footnote codes and texts",
        "verify": ["series id", "base period", "footnote codes", "query limits"],
    },
}

# Per-index licence decisions (SL01). Only "in-scope" indices are acquired; excluded ones appear in coverage
# reports as "excluded by licence decision" with the reason.
FREIGHT_INDEX_DECISIONS: dict[str, dict[str, Any]] = {
    "bls-ppi-deep-sea-freight": {
        "name": "PPI: Deep sea freight transportation (NAICS 483111)",
        "publisher": "U.S. Bureau of Labor Statistics",
        "decision": "in-scope",
        "provider": "bls-ppi",
        "licence": {"id": "us-public-domain", "terms_url": "https://www.bls.gov/opub/copyright-information.htm",
                    "redistribution": "public domain; cite BLS"},
        "attribution": "U.S. Bureau of Labor Statistics, Producer Price Index",
        "reason": "US federal statistics are in the public domain; openly redistributable with citation",
    },
    "baltic-dry-index": {
        "name": "Baltic Dry Index (and the other Baltic Exchange indices)",
        "publisher": "The Baltic Exchange",
        "decision": "excluded",
        "reason": "commercial index; data licence and subscription required for use and redistribution",
    },
    "drewry-wci": {
        "name": "Drewry World Container Index",
        "publisher": "Drewry Shipping Consultants",
        "decision": "excluded",
        "reason": "weekly headline published on the web, but no licence granting storage or redistribution",
    },
    "freightos-fbx": {
        "name": "Freightos Baltic Index (FBX)",
        "publisher": "Freightos / The Baltic Exchange",
        "decision": "excluded",
        "reason": "commercial terms restrict redistribution of index values",
    },
    "scfi": {
        "name": "Shanghai Containerized Freight Index",
        "publisher": "Shanghai Shipping Exchange",
        "decision": "excluded",
        "reason": "no redistribution licence confirmed for the published values",
    },
    "xeneta-xsi": {
        "name": "Xeneta Shipping Index",
        "publisher": "Xeneta",
        "decision": "excluded",
        "reason": "commercial subscription product without a redistribution licence",
    },
}

LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "note": "no dated live run from this runtime; offline "
               "fixtures only"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# The bounded first coverage (SL01). No record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "unece-unlocode": {
        "countries": ["DE", "NL"],
        "entries": "port-function entries (function position 1) of the declared countries",
        "releases": "the two most recent releases",
    },
    "unctadstat": {
        "ports": ["DEHAM (Hamburg)", "DEBRV (Bremerhaven)", "NLRTM (Rotterdam)", "Wilhelmshaven"],
        "countries": ["276 Germany", "528 Netherlands"],
        "series": ["port calls", "port liner shipping connectivity", "container port throughput",
                   "merchant fleet by flag"],
        "periods": "the two most recent reference years (quarters for the port LSCI)",
        "record_cap": "max_results series per report",
    },
    "eurostat-maritime": {
        "ports": "main ports of Germany (Hamburg, Bremerhaven) as reporting ports, Rotterdam as partner port",
        "countries": ["DE", "NL"],
        "datasets": ["mar_mg_aa_pwhd (goods handled by port)", "mar_mg_aa_cwh (goods handled by country)",
                     "mar_go_am_de (German ports to partner ports)"],
        "periods": "two reference years",
        "record_cap": "max_results series per cube and a 2000-cell ceiling",
    },
    "bls-ppi": {
        "series": ["PCU483111483111"],
        "periods": "the most recent months of one reference year",
    },
    "freight-indices": {"in_scope": ["bls-ppi-deep-sea-freight"],
                        "excluded": sorted(k for k, v in FREIGHT_INDEX_DECISIONS.items()
                                           if v["decision"] == "excluded")},
}

FORMATS = {
    "unlocode-csv": {"provider": "unece-unlocode", "kind": "ports"},
    "unctadstat-csv": {"provider": "unctadstat", "kind": "series"},
    "eurostat-maritime-jsonstat": {"provider": "eurostat-maritime", "kind": "series"},
    "bls-timeseries-json": {"provider": "bls-ppi", "kind": "series"},
}
UNLOCODE_COLUMNS = ("change", "country", "location", "name", "name_wo_diacritics", "subdivision", "function",
                    "status", "date", "iata", "coordinates", "remarks")
CHANGE_INDICATORS = {
    "+": "added",
    "#": "name changed",
    "|": "entry changed",
    "X": "marked for removal",
    "=": "reference entry",
    "!": "retained for historical reasons",
}
GEO_KINDS = ("port", "country", "route")
GEO_SCHEMES = ("unlocode", "unctad-port", "m49", "eurostat-port", "eurostat-geo", "iso2", "us")
FREQUENCIES = ("annual", "semiannual", "quarterly", "monthly")
EUROSTAT_CONFIDENTIAL = {"c"}


class LogisticsFormatError(ValueError):
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
        number = Decimal(raw.replace(",", ""))
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


def normalise_period(raw: Any) -> str:
    """Published period codes in the dataset-series forms (2098, 2098-Q1, 2098-S1, 2098-01); others verbatim."""
    value = str(raw).strip()
    match = re.fullmatch(r"(\d{4})[-_ ]?Q([1-4])", value, flags=re.I)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}"
    match = re.fullmatch(r"(\d{4})[-_ ]?S([12])", value, flags=re.I)
    if match:
        return f"{match.group(1)}-S{match.group(2)}"
    match = re.fullmatch(r"(\d{4})[-_]?M(\d{2})", value, flags=re.I)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return value


def period_frequency(period: str) -> str:
    if re.fullmatch(r"\d{4}", period):
        return "annual"
    if re.fullmatch(r"\d{4}-Q[1-4]", period):
        return "quarterly"
    if re.fullmatch(r"\d{4}-S[12]", period):
        return "semiannual"
    if re.fullmatch(r"\d{4}-\d{2}", period):
        return "monthly"
    return "unknown"


def unlocode_coordinates(value: Any) -> dict[str, Any] | None:
    """``5333N 00958E`` as published plus decimal degrees; ``None`` when no coordinates are published."""
    raw = text(value)
    if raw is None:
        return None
    match = re.fullmatch(r"(\d{2})(\d{2})([NS])\s+(\d{3})(\d{2})([EW])", raw)
    if not match:
        return {"published": raw, "parsed": False}
    lat = int(match.group(1)) + int(match.group(2)) / 60
    lon = int(match.group(4)) + int(match.group(5)) / 60
    lat = -lat if match.group(3) == "S" else lat
    lon = -lon if match.group(6) == "W" else lon
    return {"published": raw, "parsed": True, "lat": round(lat, 6), "lon": round(lon, 6)}


def _body(raw: bytes, document: Mapping[str, Any]) -> tuple[bytes, str | None]:
    """The declared CSV member of a zip, or the body itself; 7z bodies are refused (operator import)."""
    if raw[:6] == b"7z\xbc\xaf'\x1c":
        raise LogisticsFormatError(
            "unsupported_archive", "7z archives are not unpacked by the runtime; import the extracted CSV"
        )
    if raw[:2] != b"PK":
        return raw, None
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise LogisticsFormatError("schema_drift", "the file is not a readable zip archive") from exc
    wanted = [str(m) for m in document.get("members") or ([document["member"]] if document.get("member") else [])]
    names = [n for n in archive.namelist() if n.lower().endswith(".csv")]
    chosen = [n for n in names if n in wanted] if wanted else names
    if not chosen or (wanted and len(chosen) != len(wanted)):
        raise LogisticsFormatError("schema_drift", "the archive does not hold the declared CSV members")
    parts = []
    for name in chosen:
        if archive.getinfo(name).file_size > 50_000_000:
            raise LogisticsFormatError("input_limit", "a CSV member is larger than allowed")
        parts.append(archive.read(name))
    return b"".join(part if part.endswith(b"\n") else part + b"\n" for part in parts), ",".join(chosen)


def _decode(body: bytes, encoding: str) -> str:
    try:
        return body.decode(encoding)
    except (UnicodeDecodeError, LookupError) as exc:
        raise LogisticsFormatError("schema_drift", "the CSV is not in the declared encoding") from exc


def _release(document: Mapping[str, Any]) -> dict[str, Any]:
    release = dict(document.get("release") or {})
    return {"published_on": iso_day(release.get("published_on")), "label": text(release.get("label")),
            "version": text(release.get("version"))}


def _licence(provider: str) -> dict[str, Any]:
    return dict(PROVIDER_CONTRACTS[provider]["licence"])


# ------------------------------------------------------------------ declarations


def logistics_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("logistics") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "logistics sources declare a known provider and its format")
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "a logistics source declares its documents")
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
        except LogisticsFormatError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return declared


def _check_series(document: Mapping[str, Any]) -> None:
    series = dict(document.get("series") or {})
    for key in ("concept", "measure_label", "frequency", "definition"):
        if not series.get(key):
            raise LogisticsFormatError("invalid_document", f"a series document declares its {key}")
    if series["frequency"] not in FREQUENCIES:
        raise LogisticsFormatError("invalid_document", f"frequency is one of {FREQUENCIES}")
    geography = dict(series.get("geography") or {})
    if geography.get("kind") not in GEO_KINDS or geography.get("scheme") not in GEO_SCHEMES:
        raise LogisticsFormatError("invalid_document", "a series declares its geography kind and code scheme")
    if not dict(series["definition"]).get("reference"):
        raise LogisticsFormatError("invalid_document", "a series definition cites its reference")


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")):
        raise LogisticsFormatError("invalid_document", "a document has a label")
    release = document.get("release")
    if release is not None and iso_day(dict(release).get("published_on")) is None:
        raise LogisticsFormatError("invalid_document", "a declared release states its publication date")
    if fmt == "unlocode-csv":
        if not document.get("url") or not document.get("countries"):
            raise LogisticsFormatError("invalid_document", "a UN/LOCODE document declares its URL and countries")
        if not _release(document)["version"]:
            raise LogisticsFormatError("invalid_document", "a UN/LOCODE document declares its release version")
        return
    _check_series(document)
    if fmt == "unctadstat-csv":
        columns = dict(document.get("columns") or {})
        if not document.get("url") or not all(columns.get(k) for k in ("period", "geography", "figure")):
            raise LogisticsFormatError("invalid_document", "a UNCTADstat document declares its URL and columns")
    elif fmt == "eurostat-maritime-jsonstat":
        geography = dict(document["series"]["geography"])
        if not document.get("dataset") or not geography.get("dimension"):
            raise LogisticsFormatError("invalid_document", "a Eurostat document names its dataset and geography "
                                                           "dimension")
        if geography["kind"] == "route" and not geography.get("partner_dimension"):
            raise LogisticsFormatError("invalid_document", "a route series names its published partner dimension")
    elif fmt == "bls-timeseries-json":
        decision = FREIGHT_INDEX_DECISIONS.get(str(document.get("index_id") or ""))
        if decision is None:
            raise LogisticsFormatError("invalid_document", "a freight index document names a decided index")
        if decision["decision"] != "in-scope":
            raise LogisticsFormatError("excluded_by_licence", f"{decision['name']} is excluded by licence decision: "
                                                              f"{decision['reason']}")
        if not re.fullmatch(r"[A-Z0-9]{6,20}", str(document.get("series_id") or "")):
            raise LogisticsFormatError("invalid_document", "a BLS document names one series id")
        if not document["series"].get("base"):
            raise LogisticsFormatError("invalid_document", "an index document states its base period")


def document_url(fmt: str, document: Mapping[str, Any], endpoint: str = "") -> str:
    if fmt in {"unlocode-csv", "unctadstat-csv"}:
        return str(document["url"])
    if fmt == "eurostat-maritime-jsonstat":
        from src.ingestion.connectors.dataset.eurostat import EurostatConnector

        return EurostatConnector().dataset_url(str(document["dataset"]), dict(document.get("filters") or {}))
    if fmt == "bls-timeseries-json":
        base = str(endpoint or "https://api.bls.gov/publicAPI/v2/timeseries/data").rstrip("/")
        params = {k: str(v) for k, v in dict(document.get("params") or {}).items()}
        return f"{base}/{document['series_id']}" + ("?" + urlencode(sorted(params.items())) if params else "")
    raise LogisticsFormatError("invalid_document", f"no URL for format {fmt}")


# ------------------------------------------------------------------ parsing


def parse_unlocode(raw: bytes, *, document: Mapping[str, Any], max_rows: int = 200_000) -> dict[str, Any]:
    """Port-function entries of the declared countries, every field as published."""
    body, member = _body(raw, document)
    reader = csv.reader(io.StringIO(_decode(body, str(document.get("encoding") or "latin-1"))))
    columns = tuple(document.get("column_order") or UNLOCODE_COLUMNS)
    countries = {str(c).upper() for c in document["countries"]}
    ports, seen = [], set()
    for number, row in enumerate(reader, start=1):
        if number > max_rows:
            raise LogisticsFormatError("input_limit", "the release has more rows than allowed")
        if not row:
            continue
        entry = dict(zip(columns, [cell.strip() for cell in row] + [""] * (len(columns) - len(row))))
        country, location = entry.get("country", "").upper(), entry.get("location", "").upper()
        if country not in countries or not location:
            continue  # other countries and the country header rows
        function = entry.get("function", "")
        if not function.startswith("1"):
            continue
        code = country + location
        if code in seen:
            raise LogisticsFormatError("schema_drift", f"UN/LOCODE {code} appears twice in one release")
        seen.add(code)
        change = text(entry.get("change"))
        ports.append({
            "kind": "port",
            "unlocode": code,
            "country": country,
            "location": location,
            "name": entry.get("name") or None,
            "name_wo_diacritics": text(entry.get("name_wo_diacritics")),
            "subdivision": text(entry.get("subdivision")),
            "function": function,
            "status": text(entry.get("status")),
            "date": text(entry.get("date")),
            "iata": text(entry.get("iata")),
            "coordinates": unlocode_coordinates(entry.get("coordinates")),
            "remarks": text(entry.get("remarks")),
            "change_indicator": change,
            "change_label": CHANGE_INDICATORS.get(change or "") if change else None,
            "row": number,
        })
    if not ports:
        raise LogisticsFormatError("schema_drift", "the release states no port entry for the declared countries")
    ports.sort(key=lambda p: p["unlocode"])
    release = _release(document)
    return {
        "kind": "ports",
        "provider": "unece-unlocode",
        "format": "unlocode-csv",
        "items": ports,
        "item_count": len(ports),
        "published_on": release["published_on"],
        "published_at": None,
        "release_basis": "declared_release",
        "release_label": release["label"],
        "release_version": release["version"],
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(ports),
        "structure": {"member": member, "countries": sorted(countries), "columns": list(columns)},
    }


def _series_head(provider: str, document: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(document["series"])
    return {
        "kind": "series",
        "provider": provider,
        "dataset": str(document.get("dataset") or declared.get("dataset") or ""),
        "concept": str(declared["concept"]),
        "measure_label": str(declared["measure_label"]),
        "unit": declared.get("unit"),
        "frequency": declared["frequency"],
        "definition": dict(declared["definition"]),
        "licence": _licence(provider),
        "freight_index": None,
        "breaks": [dict(b) for b in document.get("breaks") or []],
    }


def _geo(scheme: str, code: Any, label: Any = None, unlocode: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"scheme": scheme, "code": str(code).strip()}
    if text(label):
        out["label"] = text(label)
    if text(unlocode):
        out["unlocode"] = text(unlocode).replace(" ", "").upper()
    return out


def _finish(entry: dict[str, Any], periods: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    entry["observations"] = [dict(periods[p]) for p in sorted(periods)]
    entry["source_series_id"] = ":".join(
        [entry["provider"], entry["dataset"], entry["geography"]["code"]]
        + ([entry["partner"]["code"]] if entry.get("partner") else [])
        + [f"{k}={v}" for k, v in sorted(entry["dimensions"].items())]
    )
    return entry


def _declared_breaks(entry: dict[str, Any]) -> None:
    code = entry["geography"]["code"]
    entry["breaks"] = [b for b in entry["breaks"] if not b.get("geography") or str(b["geography"]) == code]


def _release_fields(release: Mapping[str, Any], fallback_label: str | None = None) -> dict[str, Any]:
    return {
        "published_on": release["published_on"],
        "published_at": None,
        "release_basis": "declared_release" if release["published_on"] else "retrieval_time",
        "release_label": release["label"] or fallback_label,
        "release_version": release["version"],
    }


def parse_unctad(raw: bytes, *, document: Mapping[str, Any], max_rows: int = 50_000) -> dict[str, Any]:
    """A UNCTADstat report CSV as series: one per geography (and declared dimension columns), values, flags and
    footnotes as published; UNCTAD port identifiers kept beside a published UN/LOCODE."""
    body, member = _body(raw, document)
    reader = csv.DictReader(io.StringIO(_decode(body, str(document.get("encoding") or "utf-8-sig"))))
    columns = dict(document["columns"])
    dimension_columns = dict(document.get("dimension_columns") or {})
    header = list(reader.fieldnames or [])
    missing = [c for c in [*columns.values(), *dimension_columns.values()] if c and c not in header]
    if missing:
        raise LogisticsFormatError("schema_drift", f"the report lacks declared columns {missing}")
    selection = {str(k): str(v) for k, v in dict(document.get("row_filter") or {}).items()}
    geography = dict(document["series"]["geography"])
    series: dict[str, dict[str, Any]] = {}
    periods: dict[str, dict[str, dict[str, Any]]] = {}
    for number, row in enumerate(reader, start=2):
        if number > max_rows:
            raise LogisticsFormatError("input_limit", "the report has more rows than allowed")
        if any(str(row.get(k, "")).strip() != v for k, v in selection.items()):
            continue
        code = text(row.get(columns["geography"]))
        period = text(row.get(columns["period"]))
        if code is None or period is None:
            continue
        dims = {name: str(row.get(col) or "").strip() for name, col in sorted(dimension_columns.items())}
        key = canonical([code, dims])
        if key not in series:
            entry = _series_head("unctadstat", document)
            entry.update({
                "geography": {"kind": geography["kind"], **_geo(
                    geography["scheme"], code, row.get(columns.get("geography_label") or ""),
                    row.get(columns.get("unlocode") or ""))},
                "partner": None,
                "dimensions": dims,
            })
            _declared_breaks(entry)
            series[key], periods[key] = entry, {}
        value_text = text(row.get(columns["figure"]))
        normalised = normalise_period(period)
        if normalised in periods[key]:
            raise LogisticsFormatError("schema_drift", f"period {normalised} appears twice in one series")
        value = decimal_text(value_text)
        periods[key][normalised] = {
            "period": normalised,
            "period_published": period,
            "value_text": value_text,
            "value": value,
            "status": "reported" if value is not None else "not_published",
            "flags": {k: v for k, v in {"flag": text(row.get(columns.get("flag") or ""))}.items() if v},
            "footnotes": [f for f in [text(row.get(columns.get("footnote") or ""))] if f],
            "row": number,
        }
    items = [_finish(series[k], periods[k]) for k in sorted(series)]
    if not items:
        raise LogisticsFormatError("schema_drift", "the report states no row of the declared selection")
    release = _release(document)
    return {
        "kind": "series",
        "provider": "unctadstat",
        "format": "unctadstat-csv",
        "items": items,
        "item_count": len(items),
        **_release_fields(release),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(items),
        "structure": {"member": member, "header": header, "report": document.get("dataset")},
    }


def parse_eurostat_maritime(raw: bytes, *, document: Mapping[str, Any], url: str,
                            max_cells: int = 2000) -> dict[str, Any]:
    """A Eurostat maritime cube as series: one per combination of the non-time dimensions, reporting and partner
    ports as published, status flags kept, confidential cells without value, absent cells never zero-filled."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.eurostat import EurostatConnector

    ref = SeriesRef(locator=str(document["dataset"]), metadata={"api": "statistics"})
    try:
        cube = EurostatConnector().parse_cells(
            RawSeries(ref, raw, content_type="application/json", source_url=url), max_cells=max_cells
        )
    except (ValueError, UnicodeDecodeError) as exc:
        raise LogisticsFormatError("schema_drift", f"Eurostat cube: {exc}") from exc
    geography = dict(document["series"]["geography"])
    dim, partner_dim = geography["dimension"], geography.get("partner_dimension")
    if dim not in cube["dimension_ids"] or (partner_dim and partner_dim not in cube["dimension_ids"]):
        raise LogisticsFormatError("schema_drift", "the cube lacks the declared geography dimensions")
    labels = {d: cube["dimensions"][d]["categories"] for d in cube["dimension_ids"]}
    other = [d for d in cube["dimension_ids"] if d not in {dim, partner_dim, "time", "freq"}]
    series: dict[str, dict[str, Any]] = {}
    periods: dict[str, dict[str, dict[str, Any]]] = {}
    for cell in cube["cells"]:
        dims = cell["dimensions"]
        code = str(dims[dim])
        extra = {d: str(dims[d]) for d in other}
        key = canonical([code, dims.get(partner_dim) if partner_dim else None, extra])
        if key not in series:
            entry = _series_head("eurostat-maritime", document)
            unit_code = extra.get("unit")
            entry.update({
                "unit": entry["unit"] or (
                    {"code": unit_code, "label": labels.get("unit", {}).get(unit_code)} if unit_code else None),
                "geography": {"kind": geography["kind"], **_geo(geography["scheme"], code, labels[dim].get(code))},
                "partner": _geo(geography.get("partner_scheme") or geography["scheme"], dims[partner_dim],
                                labels[partner_dim].get(dims[partner_dim])) if partner_dim else None,
                "dimensions": {d: {"code": v, "label": labels[d].get(v)} for d, v in sorted(extra.items())},
            })
            entry["dimensions"] = {d: v["code"] for d, v in entry["dimensions"].items()}
            entry["dimension_labels"] = {d: labels[d].get(v) for d, v in extra.items()}
            _declared_breaks(entry)
            series[key], periods[key] = entry, {}
        period = normalise_period(cell["time"])
        status = text(cell["status"])
        value_text = None if cell["value"] is None else str(cell["value"])
        value = decimal_text(value_text)
        periods[key][period] = {
            "period": period,
            "period_published": cell["time"],
            "value_text": value_text,
            "value": value,
            "status": "confidential" if status in EUROSTAT_CONFIDENTIAL else "reported" if value is not None
            else "not_published",
            "flags": {"status": status, "status_label": cube["status_labels"].get(status)} if status else {},
            "footnotes": [],
        }
    items = [_finish(series[k], periods[k]) for k in sorted(series)]
    release = _release(document)
    if cube["updated_at_ms"] is not None:
        published_at = datetime.fromtimestamp(cube["updated_at_ms"] / 1000, tz=timezone.utc).isoformat()
        fields = {"published_on": published_at[:10], "published_at": published_at,
                  "release_basis": "eurostat_dataset_updated", "release_label": release["label"],
                  "release_version": release["version"]}
    else:
        fields = _release_fields(release)
    return {
        "kind": "series",
        "provider": "eurostat-maritime",
        "format": "eurostat-maritime-jsonstat",
        "items": items,
        "item_count": len(items),
        **fields,
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


def parse_bls(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """One BLS index series: values, preliminary footnotes and base as published; the licence decision and
    attribution travel with the series and every observation. Annual averages (M13) are not a monthly value and are
    kept apart in the structure, never mixed into the series."""
    decision = FREIGHT_INDEX_DECISIONS[str(document["index_id"])]
    if decision["decision"] != "in-scope":
        raise LogisticsFormatError("excluded_by_licence", "index excluded by licence decision")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LogisticsFormatError("schema_drift", "the BLS answer is not JSON") from exc
    if payload.get("status") != "REQUEST_SUCCEEDED":
        raise LogisticsFormatError("schema_drift", f"BLS status {payload.get('status')!r}: {payload.get('message')}")
    found = [s for s in dict(payload.get("Results") or {}).get("series") or []
             if s.get("seriesID") == document["series_id"]]
    if len(found) != 1:
        raise LogisticsFormatError("schema_drift", "the answer does not hold exactly the declared series")
    licence = {**decision["licence"], "attribution": decision["attribution"]}
    periods, annual = {}, []
    for row in found[0].get("data") or []:
        year, code = str(row.get("year") or ""), str(row.get("period") or "")
        footnotes = [{"code": text(f.get("code")), "text": text(f.get("text"))}
                     for f in row.get("footnotes") or [] if f and (f.get("code") or f.get("text"))]
        value_text = text(row.get("value"))
        value = decimal_text(value_text)
        if code == "M13":
            annual.append({"year": year, "value_text": value_text})
            continue
        period = normalise_period(f"{year}M{code[1:]}")
        periods[period] = {
            "period": period,
            "period_published": f"{year} {code}",
            "value_text": value_text,
            "value": value,
            "status": "reported" if value is not None else "not_published",
            "flags": {"preliminary": any(f["code"] == "P" for f in footnotes)},
            "footnotes": [f["text"] or f["code"] for f in footnotes],
            "licence": licence,
        }
    entry = _series_head("bls-ppi", document)
    entry.update({
        "dataset": document["series_id"],
        "licence": licence,
        "freight_index": {"index_id": document["index_id"], "name": decision["name"],
                          "publisher": decision["publisher"], "decision": decision["decision"],
                          "reason": decision["reason"], "base": document["series"]["base"]},
        "geography": {"kind": "country", **_geo(document["series"]["geography"]["scheme"],
                                                document["series"]["geography"].get("code", "US"),
                                                document["series"]["geography"].get("label"))},
        "partner": None,
        "dimensions": {},
    })
    items = [_finish(entry, periods)]
    release = _release(document)
    return {
        "kind": "series",
        "provider": "bls-ppi",
        "format": "bls-timeseries-json",
        "items": items,
        "item_count": 1,
        **_release_fields(release),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(items),
        "structure": {"series_id": document["series_id"], "annual_averages": annual,
                      "annual_averages_note": "M13 annual averages are published values kept apart, never mixed"},
    }


def parse_document(fmt: str, raw: bytes, document: Mapping[str, Any], *, url: str, limit: int) -> dict[str, Any]:
    if fmt == "unlocode-csv":
        return parse_unlocode(raw, document=document)
    if fmt == "unctadstat-csv":
        return parse_unctad(raw, document=document)
    if fmt == "eurostat-maritime-jsonstat":
        return parse_eurostat_maritime(raw, document=document, url=url)
    if fmt == "bls-timeseries-json":
        return parse_bls(raw, document=document)
    raise LogisticsFormatError("invalid_document", f"unknown format {fmt}")


def release_records(release: Mapping[str, Any], document: Mapping[str, Any], url: str,
                    evidence_origin: str) -> list[dict[str, Any]]:
    """The page records of one parsed release: the release header and one item each."""
    header = {
        "contract": RELEASE_CONTRACT,
        "provider": release["provider"],
        "format": release["format"],
        "kind": release["kind"],
        "document": dict(document),
        "published_on": release["published_on"],
        "published_at": release["published_at"],
        "release_basis": release["release_basis"],
        "release_label": release["release_label"],
        "release_version": release.get("release_version"),
        "file_sha256": release["file_sha256"],
        "content_sha256": release["content_sha256"],
        "item_count": release["item_count"],
        "structure": release["structure"],
        "evidence_origin": evidence_origin,
        "live_verification": LIVE_VERIFICATION[release["provider"]]["status"],
        "url": url,
    }
    return [
        {
            "id": f"{release['file_sha256'][:16]}:{number}",
            "title": f"{document.get('label') or release['provider']} ({release['published_on'] or 'retrieved'})",
            "url": url,
            "language": "en",
            "published_at": release["published_on"],
            "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
            "logistics_release": header,
            "logistics_item": item,
        }
        for number, item in enumerate(release["items"])
    ]


# ------------------------------------------------------------------ runtime adapter


class LogisticsAdapter:
    """Fetch the declared logistics documents on the runtime's default transport; one page (one release) each."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # every logistics provider is read without a credential
        self.source = json.loads(json.dumps(source))
        self.declared = logistics_declaration(self.source)
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
            "logistics": {"provider": self.declared["provider"], "format": self.declared["format"],
                          "live_verification": LIVE_VERIFICATION[self.declared["provider"]]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "logistics runs fetch the declared documents only")

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the endpoint's host only")
        base, _, query = url.partition("?")
        response = self.transport(url=base, params=parse_qsl(query, keep_blank_values=True),
                                  headers={"Accept": "application/json, text/csv, application/zip"},
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
            release = parse_document(fmt, raw, document, url=url, limit=limit)
        except LogisticsFormatError as exc:
            code = "response_too_large" if exc.code == "input_limit" else "schema_drift"
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        if release["kind"] == "series" and release["item_count"] > limit:
            # Never a truncated release: a missing series would read as a series that was not published.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        records = release_records(release, document, url, origin)
        receipt = {
            "status": 200,
            "provider": release["provider"],
            "document": document.get("label"),
            "published_on": release["published_on"],
            "release_basis": release["release_basis"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "evidence_origin": origin,
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: LogisticsAdapter}


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
    adapter = LogisticsAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


def coverage_report() -> dict[str, Any]:
    """Every provider's decision and every freight index, excluded ones labelled 'excluded by licence decision'."""
    return {
        "providers": {p: {"access_decision": c["access_decision"], "delivers": c["delivers"]}
                      for p, c in PROVIDER_CONTRACTS.items()},
        "freight_indices": [
            {"index_id": k, "name": v["name"], "publisher": v["publisher"], "decision": v["decision"],
             "status": "in scope (openly licensed)" if v["decision"] == "in-scope"
             else "excluded by licence decision", "reason": v["reason"]}
            for k, v in sorted(FREIGHT_INDEX_DECISIONS.items())
        ],
        "bounded_coverage": BOUNDED_COVERAGE,
    }


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONNECTOR",
    "EXCLUSIONS",
    "FREIGHT_INDEX_DECISIONS",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "PROVIDER_CONTRACTS",
    "LogisticsAdapter",
    "LogisticsFormatError",
    "coverage_report",
    "fixture_request",
    "fixture_transport",
    "logistics_declaration",
    "parse_bls",
    "parse_document",
    "parse_eurostat_maritime",
    "parse_unctad",
    "parse_unlocode",
    "release_records",
    "replay_native_fixture",
]
