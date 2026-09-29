"""Fisheries and Maritime Activity sources: GFW, FAO FishStat, RFMO registers and IUU lists (#2222, FI01 and FI03-FI06).

Six providers run as sources of the ``fisheries-maritime`` source pack
(``config/source_packs/fisheries.json``, connector ``fisheries``) through
:mod:`src.ingestion.source_pack_runtime` - licence acceptance, budgets,
receipts, checkpoints and the runtime's same-host HTTPS transport - each under
a recorded access contract (:data:`PROVIDER_CONTRACTS`):

* **Global Fishing Watch** (``gfw``, #2310) - vessel identity (self-reported
  identity segments keyed by GFW vessel id and dataset version) and published
  fishing-effort *aggregates* from the 4Wings report API (grid resolution,
  period, flag, gear, hours and GFW's apparent-fishing method note). Tracks,
  positions and events are never requested or stored; the API token is the
  ``NOESIS_GFW_API_TOKEN`` secret reference.
* **FAO FishStat** (``fao-fishstat``, #2314) - the global capture production
  release file, filtered to the bounded FAO major areas, ASFIS species,
  countries and years; quantities, units and FAO status flags as published,
  tied to the release.
* **ICCAT, WCPFC, IOTC** (#2318, #2321) - one adapter mode per register
  format (ICCAT CSV, WCPFC JSON, IOTC semicolon CSV) for the authorised-vessel
  registers (bounded to the selected flag states) and their IUU vessel lists
  (taken whole, they are short), each page a dated snapshot.
* **Combined IUU Vessel List** (``combined-iuu``, #2321) - entries citing the
  originating RFMO listings; never counted as independent confirmations.

Every selection is explicit (``docs/development/fisheries-evidence/source-audit.md``):
one page per selected list, release, vessel or effort report, never an
enumeration. :data:`EXCLUDED_FIELDS` (tracks, positions, events, owner
addresses) are never parsed and are reported as dropped in the page receipt.
Every provider is ``unverified-live`` until a dated live run (#2346); request
paths and field names marked *verify* are authored from public documentation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.fisheries_records import (
    METHOD_NOTE_GFW,
    FisheriesError,
    area_key,
    digest,
    imo_key,
    species_key,
    statement,
)

CONNECTOR = "fisheries"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("gfw", "fao-fishstat", "iccat", "wcpfc", "iotc", "combined-iuu")
PROVIDER_HOSTS = {
    "gfw": ("gateway.api.globalfishingwatch.org",),
    "fao-fishstat": ("www.fao.org",),
    "iccat": ("www.iccat.int",),
    "wcpfc": ("vessels.wcpfc.int",),
    "iotc": ("iotc.org",),
    "combined-iuu": ("iuu-vessels.org",),
}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "gfw": {
        "publisher": "Global Fishing Watch",
        "access": "GFW APIs v3 (Vessels API, 4Wings report API), HTTPS GET with a bearer token",
        "endpoints": ["/v3/vessels/{vessel_id}?dataset=public-global-vessel-identity:v3.0",
                      "/v3/4wings/report?datasets[0]=public-global-fishing-effort:v3.0&... (verify parameter names)"],
        "authentication": "API token issued by GFW to a registered user, held as the NOESIS_GFW_API_TOKEN secret "
                          "reference; never stored in manifests, receipts or records",
        "licence": "GFW API terms of use: non-commercial use; data licensed CC BY-NC 4.0 (verify the current terms "
                   "and any commercial-use agreement before redistribution)",
        "terms_url": "https://globalfishingwatch.org/our-apis/documentation#terms-of-use",
        "attribution": "Source: Global Fishing Watch (https://globalfishingwatch.org/), dataset version as cited",
        "rate_limits": "per-token limits set by GFW (verify); the pack keeps to one report or vessel per page",
        "revision_behaviour": "datasets are versioned (e.g. public-global-fishing-effort:v3.0); a new dataset "
                              "version re-estimates history and is stored as a new revision superseding the prior",
        "access_decision": "unverified-live; aggregates and vessel identity only",
        "licence_decision": {
            "stored": ["vessel identity segments (self-reported name, flag, call sign, IMO, MMSI, transmission "
                       "period) for explicitly selected GFW vessel ids",
                       "fishing-effort aggregates at the published grid (LOW, 0.1 degree) and monthly period, grouped "
                       "by flag and gear, for the selected RFMO regions"],
            "not_stored": ["per-vessel tracks or positions", "events (fishing, encounters, loitering, port visits)",
                           "per-vessel effort", "insights or risk indicators"],
            "presentation": "apparent fishing effort estimated by a model from AIS; never presented as confirmed "
                            "fishing, never used to infer illegal fishing",
        },
        "fields": ["id", "shipname", "flag", "callsign", "imo", "ssvid", "transmissionDateFrom",
                   "transmissionDateTo", "geartypes", "date", "flag", "geartype", "hours", "lat", "lon"],
    },
    "fao-fishstat": {
        "publisher": "Food and Agriculture Organization of the United Nations (FAO), Fisheries and Aquaculture "
                     "Division - FishStat",
        "access": "FishStat global capture production bulk release file (CSV), anonymous HTTPS GET",
        "endpoints": ["/fishery/static/Data/Capture_Global.csv (verify: FAO publishes zipped CSV bundles; the release label and date are read from the file's RELEASE and RELEASE_DATE columns)"],
        "authentication": "none",
        "licence": "CC BY-NC-SA 3.0 IGO (FAO statistical databases terms; verify the release's own notice)",
        "terms_url": "https://www.fao.org/contact-us/terms/db-terms-of-use/en/",
        "attribution": "Source: FAO. FishStat: Global capture production 1950-2023 (release as cited). "
                       "Fisheries and Aquaculture Division, Rome",
        "rate_limits": "none published; one release file per page within the source budget",
        "revision_behaviour": "an annual release (plus corrections) republishes the whole series; earlier years may "
                              "be revised; each release is a new revision of every cell it publishes",
        "access_decision": "unverified-live",
        "fields": ["COUNTRY.ISO3_CODE", "SPECIES.ALPHA_3_CODE", "AREA.CODE", "MEASURE", "PERIOD", "VALUE",
                   "STATUS"],
    },
    "iccat": {
        "publisher": "International Commission for the Conservation of Atlantic Tunas (ICCAT)",
        "access": "ICCAT Record of Vessels and IUU Vessel List downloads (CSV), anonymous HTTPS GET",
        "endpoints": ["/en/vesselsrecord.asp?export=csv (verify)", "/en/IUU.asp?export=csv (verify)"],
        "authentication": "none",
        "licence": "public record published under ICCAT recommendations; reproduction with attribution (verify "
                   "the site's terms)",
        "terms_url": "https://www.iccat.int/en/",
        "attribution": "Source: ICCAT Record of Vessels / ICCAT IUU Vessel List (https://www.iccat.int/)",
        "rate_limits": "none published; one list per page",
        "revision_behaviour": "the registers change continuously; each download is a dated snapshot "
                              "(Last-Modified), authorisation start/end dates are published per entry, and a vessel "
                              "absent from a later snapshot is recorded as removed at that snapshot",
        "access_decision": "unverified-live",
        "fields": ["ICCATSerialNo", "VesselName", "FlagCode", "IRCS", "IMO", "GearCode", "AuthFrom", "AuthTo",
                   "OwnerName", "IUURef", "PreviousNames", "PreviousFlags", "ListedOn", "DelistedOn", "Reason"],
    },
    "wcpfc": {
        "publisher": "Western and Central Pacific Fisheries Commission (WCPFC)",
        "access": "WCPFC Record of Fishing Vessels and IUU Vessel List (JSON export), anonymous HTTPS GET",
        "endpoints": ["/api/rfv/vessels.json (verify)", "/api/iuu/vessels.json (verify)"],
        "authentication": "none",
        "licence": "public record under the WCPFC Convention; reproduction with attribution (verify)",
        "terms_url": "https://www.wcpfc.int/",
        "attribution": "Source: WCPFC Record of Fishing Vessels / WCPFC IUU Vessel List (https://www.wcpfc.int/)",
        "rate_limits": "none published; one list per page",
        "revision_behaviour": "dated snapshots (as_of in the export); authorisation periods per entry; removals by "
                              "absence at a later snapshot, IUU delistings dated in the list",
        "access_decision": "unverified-live",
        "fields": ["wcpfc_id", "vessel_name", "flag", "ircs", "imo", "gear", "authorised_from", "authorised_to",
                   "owner", "entry", "previous_names", "listed", "removed", "reason"],
    },
    "iotc": {
        "publisher": "Indian Ocean Tuna Commission (IOTC)",
        "access": "IOTC Record of Authorised Vessels and IUU Vessel List (semicolon CSV), anonymous HTTPS GET",
        "endpoints": ["/vessels/export/rav.csv (verify)", "/vessels/export/iuu.csv (verify)"],
        "authentication": "none",
        "licence": "public record under IOTC resolutions; reproduction with attribution (verify)",
        "terms_url": "https://iotc.org/",
        "attribution": "Source: IOTC Record of Authorised Vessels / IOTC IUU Vessel List (https://iotc.org/)",
        "rate_limits": "none published; one list per page",
        "revision_behaviour": "dated snapshots (Last-Modified); authorisation periods per entry; removals by absence",
        "access_decision": "unverified-live",
        "fields": ["IOTC.No", "Vessel.Name", "Flag", "IRCS", "IMO", "Gear", "Auth.From", "Auth.To", "Owner",
                   "Ref", "Previous.Names", "Listed", "Delisted", "Reason"],
    },
    "combined-iuu": {
        "publisher": "Combined IUU Vessel List (Trygg Mat Tracking, iuu-vessels.org)",
        "access": "combined list export (JSON), anonymous HTTPS GET",
        "endpoints": ["/iuu/api/vessels.json (verify: the site publishes an HTML list and downloads)"],
        "authentication": "none",
        "licence": "published for public information with attribution to the Combined IUU Vessel List and the "
                   "originating RFMOs (verify reuse terms)",
        "terms_url": "https://iuu-vessels.org/",
        "attribution": "Source: Combined IUU Vessel List (https://iuu-vessels.org/), compiling RFMO IUU lists",
        "rate_limits": "none published; one list per page",
        "revision_behaviour": "a compilation of RFMO IUU listings updated after RFMO decisions; each entry cites "
                              "the originating listings, which remain the authority",
        "access_decision": "unverified-live",
        "fields": ["id", "current_name", "current_flag", "imo", "call_sign", "previous_names", "listings (rfmo, "
                   "reference, listed, delisted)", "reason"],
    },
}
EXCLUDED_RFMOS = {
    "ccsbt": "CCSBT authorised-vessel records overlap the three selected tuna RFMOs for the bounded flags; "
             "deferred to keep coverage bounded",
    "iattc": "IATTC vessel register export requires form navigation (no stable file URL found); deferred",
    "nafo": "NAFO vessel registry not selected: bounded coverage is the three tuna RFMOs plus the combined list",
    "neafc": "NEAFC IUU A/B lists are included in the Combined IUU Vessel List; its own register is not selected",
    "sprfmo": "SPRFMO record not selected for the bounded coverage",
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "note": "no dated live run from this runtime; offline fixtures only (#2346)"}
    for provider in PROVIDERS
}
# Never acquired: per-vessel movement data and personal addresses. Dropped when a response carries them.
EXCLUDED_FIELDS = ("track", "tracks", "positions", "position", "events", "event", "owner address", "ownersaddress",
                   "operator address", "insights", "risk")
_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DMY = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_RFMO_LIST_REQUESTS = {
    ("iccat", "authorised-vessels"): ("/en/vesselsrecord.asp", {"export": "csv"}),
    ("iccat", "iuu-vessels"): ("/en/IUU.asp", {"export": "csv"}),
    ("wcpfc", "authorised-vessels"): ("/api/rfv/vessels.json", {}),
    ("wcpfc", "iuu-vessels"): ("/api/iuu/vessels.json", {}),
    ("iotc", "authorised-vessels"): ("/vessels/export/rav.csv", {}),
    ("iotc", "iuu-vessels"): ("/vessels/export/iuu.csv", {}),
    ("combined-iuu", "iuu-vessels"): ("/iuu/api/vessels.json", {}),
}
GFW_SPATIAL = {"LOW": 0.1, "HIGH": 0.01}


class FisheriesFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    if match := _DATE.match(text):
        return "-".join(match.groups())
    if match := _DMY.fullmatch(text):
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    return None


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _label(key: Any) -> str:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(key))
    return re.sub(r"[_\s.-]+", " ", spaced).strip().casefold()


def dropped_fields(payload: Any) -> list[str]:
    """Excluded keys a response carries (never parsed); reported, never stored."""
    found: set[str] = set()
    if isinstance(payload, str):  # a CSV document: its header row names the columns
        first = payload.lstrip("﻿").splitlines()[0] if payload.strip() else ""
        payload = {column.strip(): None for column in re.split(r"[;,]", first) if column.strip()}

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                label = _label(key)
                if label in EXCLUDED_FIELDS or label.replace(" ", "") in EXCLUDED_FIELDS:
                    found.add(str(key))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    return sorted(found)


# ------------------------------------------------------------------ selections


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = dict(source.get("fisheries") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"fisheries sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    if not 1 <= len(entries) <= int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "a fisheries source selects 1..max_pages pages explicitly")
    for entry in entries:
        kind = entry.get("kind")
        if provider == "gfw":
            if kind == "vessel" and not re.fullmatch(r"[0-9a-f-]{8,64}", str(entry.get("vessel_id") or "")):
                raise SourcePackError("invalid_manifest", "GFW vessel selections name a GFW vessel id")
            if kind == "effort" and not (entry.get("region_id") and entry.get("date_from") and entry.get("date_to")):
                raise SourcePackError("invalid_manifest", "GFW effort selections name a region and a date range")
            if kind not in {"vessel", "effort"}:
                raise SourcePackError("invalid_manifest", "GFW selections are a vessel or an effort report")
            if kind == "effort" and entry.get("spatial_resolution", "LOW") not in GFW_SPATIAL:
                raise SourcePackError("invalid_manifest", "GFW effort is stored at LOW or HIGH published grids only")
        elif provider == "fao-fishstat":
            if not all(entry.get(k) for k in ("areas", "species", "countries", "years")):
                raise SourcePackError("invalid_manifest", "a FishStat selection names bounded areas, species, "
                                                          "countries and years")
            if any(species_key(s) is None for s in entry["species"]) or \
                    any(area_key("fao-major-area", a) is None for a in entry["areas"]):
                raise SourcePackError("invalid_manifest", "FishStat species are ASFIS 3-alpha codes and areas FAO "
                                                          "major area codes")
        else:
            if (provider, entry.get("list")) not in _RFMO_LIST_REQUESTS:
                raise SourcePackError("invalid_manifest", f"{provider} publishes no {entry.get('list')!r} list here")
            if entry.get("list") == "authorised-vessels" and not entry.get("flags"):
                raise SourcePackError("invalid_manifest", "a register selection is bounded to explicit flag states")
    return provider, entries


def selection_key(entry: Mapping[str, Any]) -> str:
    return digest({k: entry[k] for k in sorted(entry) if k != "label"})[:16]


def requests_for(provider: str, entry: Mapping[str, Any]) -> list[tuple[str, str, dict[str, str]]]:
    """(role, path, query) for one selected page; paths are relative to the source endpoint."""
    if provider == "gfw" and entry["kind"] == "vessel":
        return [("vessel", f"/v3/vessels/{quote(str(entry['vessel_id']))}",
                 {"dataset": str(entry.get("dataset") or "public-global-vessel-identity:v3.0")})]
    if provider == "gfw":
        return [("effort", "/v3/4wings/report", {
            "datasets[0]": str(entry.get("dataset") or "public-global-fishing-effort:v3.0"),
            "date-range": f"{entry['date_from']},{entry['date_to']}",
            "spatial-resolution": str(entry.get("spatial_resolution") or "LOW"),
            "temporal-resolution": str(entry.get("temporal_resolution") or "MONTHLY"),
            "group-by": str(entry.get("group_by") or "FLAGANDGEARTYPE"), "format": "JSON",
            "region-dataset": str(entry.get("region_dataset") or "public-rfmo"), "region-id": str(entry["region_id"])})]
    if provider == "fao-fishstat":
        return [("release", f"/fishery/static/Data/Capture_{quote(str(entry.get('dataset_file') or 'Global'))}.csv",
                 {})]
    path, query = _RFMO_LIST_REQUESTS[(provider, entry["list"])]
    return [(entry["list"], path, dict(query))]


# ------------------------------------------------------------------ parsers


def _source(url: str, locator: str, provider: str, origin: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "locator": locator, "attribution": PROVIDER_CONTRACTS[provider]["attribution"],
            "evidence_origin": origin, **{k: v for k, v in extra.items() if v is not None}}


def _imo_fields(raw: Any) -> dict[str, Any]:
    text = _text(raw)
    if text is None or text in {"0", "-", "N/A", "n/a"}:
        return {"imo": None}
    key = imo_key(text)
    return {"imo": key or text, **({} if key else {"imo_malformed": True})}


def parse_gfw_vessel(body: Mapping[str, Any], url: str, *, origin: str, entry: Mapping[str, Any]) -> list[dict]:
    segments = body.get("selfReportedInfo")
    if not isinstance(segments, list) or not segments:
        raise FisheriesFormatError("schema_drift", "GFW vessel payload has no selfReportedInfo segments")
    dataset = _text(body.get("dataset")) or str(entry.get("dataset") or "public-global-vessel-identity:v3.0")
    vessel_id = str(entry["vessel_id"])
    gears = sorted({str(g.get("name")) for info in body.get("combinedSourcesInfo") or []
                    for g in info.get("geartypes") or [] if g.get("name")})
    ordered = sorted(enumerate(segments), key=lambda p: str(p[1].get("transmissionDateFrom") or ""))
    subject = {"key": f"gfw:vessel:{vessel_id}", "kind": "vessel", "name": _text(ordered[-1][1].get("shipname"))}
    out = []
    for index, segment in ordered:
        start, end = _day(segment.get("transmissionDateFrom")), _day(segment.get("transmissionDateTo"))
        published = {"gfw_vessel_id": vessel_id, "segment_id": _text(segment.get("id")),
                     "shipname": _text(segment.get("shipname")) or "not stated",
                     "flag": _text(segment.get("flag")), "callsign": _text(segment.get("callsign")),
                     **_imo_fields(segment.get("imo")), "mmsi": _text(segment.get("ssvid")),
                     "transmission_from": start, "transmission_to": end, "geartypes": gears,
                     "note": "self-reported AIS identity as published by GFW; not a registry record"}
        out.append(statement("vessel", "gfw", subject, f"identity:{start or index}", published,
                             source=_source(url, f"/selfReportedInfo/{index}", "gfw", origin,
                                            list="vessel-identity", dataset_version=dataset),
                             effective_from=start, effective_to=end, date_basis="AIS transmission period"))
    return out


def parse_gfw_effort(body: Mapping[str, Any], url: str, *, origin: str, entry: Mapping[str, Any]) -> list[dict]:
    dataset = str(entry.get("dataset") or "public-global-fishing-effort:v3.0")
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise FisheriesFormatError("schema_drift", "4Wings report has no entries")
    resolution = str(entry.get("spatial_resolution") or "LOW")
    region = str(entry["region_id"])
    region_dataset = str(entry.get("region_dataset") or "public-rfmo")
    subject = {"key": f"gfw:region:{region_dataset}/{region}", "kind": "area", "name": _text(entry.get("region_name"))}
    scheme = {"public-rfmo": "rfmo-convention-area", "public-eez-areas": "eez"}.get(region_dataset, "gfw-region")
    out = []
    for e_index, block in enumerate(entries):
        cells = block.get(dataset)
        if not isinstance(cells, list):
            raise FisheriesFormatError("schema_drift", f"4Wings entry lacks the {dataset} dataset")
        for c_index, cell in enumerate(cells):
            month = str(cell.get("date") or "")
            if not re.fullmatch(r"\d{4}-\d{2}", month):
                raise FisheriesFormatError("schema_drift", "4Wings cell lacks a monthly date")
            year, mon = int(month[:4]), int(month[5:])
            last = {2: 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28}.get(
                mon, 30 if mon in {4, 6, 9, 11} else 31)
            period = {"from": f"{month}-01", "to": f"{month}-{last:02d}", "resolution": "month"}
            lat, lon = cell.get("lat"), cell.get("lon")
            grid = ({"resolution": resolution, "resolution_deg": GFW_SPATIAL[resolution], "lat": lat, "lon": lon}
                    if lat is not None and lon is not None else None)
            flag, gear = _text(cell.get("flag")) or "not stated", _text(cell.get("geartype")) or "not stated"
            published = {"area": {"scheme": scheme, "code": region, "name": _text(entry.get("region_name"))},
                         "grid": grid, "period": period, "flag": flag, "gear": gear, "unit": "hours",
                         "value": cell.get("hours"), "vessel_count": cell.get("vesselIDs")
                         if isinstance(cell.get("vesselIDs"), int) else None, "method": METHOD_NOTE_GFW}
            cell_key = f"{lat},{lon}" if grid else "region"
            out.append(statement("effort_aggregate", "gfw", subject, f"effort:{month}:{flag}:{gear}:{cell_key}",
                                 published, source=_source(url, f"/entries/{e_index}/{dataset}/{c_index}", "gfw",
                                                           origin, list="fishing-effort", dataset_version=dataset),
                                 event="release", effective_from=period["from"], effective_to=period["to"],
                                 date_basis="aggregation period"))
    return out


def parse_fishstat(body: str, url: str, *, origin: str, entry: Mapping[str, Any]) -> list[dict]:
    reader = csv.DictReader(io.StringIO(body))
    need = {"COUNTRY.ISO3_CODE", "SPECIES.ALPHA_3_CODE", "AREA.CODE", "MEASURE", "PERIOD", "VALUE", "STATUS"}
    if not need <= set(reader.fieldnames or []):
        raise FisheriesFormatError("schema_drift", "FishStat release lacks its expected columns")
    areas, species = {str(a) for a in entry["areas"]}, {str(s) for s in entry["species"]}
    countries, years = {str(c) for c in entry["countries"]}, {int(y) for y in entry["years"]}
    units = dict(entry.get("units") or {"Q_tlw": "t (tonnes live weight)", "Q_no_1": "number of individuals"})
    out = []
    for index, row in enumerate(reader, start=2):
        if row["AREA.CODE"] not in areas or row["SPECIES.ALPHA_3_CODE"] not in species \
                or row["COUNTRY.ISO3_CODE"] not in countries or not str(row["PERIOD"]).isdigit() \
                or int(row["PERIOD"]) not in years:
            continue
        year = int(row["PERIOD"])
        release = _text(row.get("RELEASE")) or _text(entry.get("release"))
        if release is None:
            raise FisheriesFormatError("schema_drift", "a FishStat row names no release")
        release_date = _day(row.get("RELEASE_DATE")) or _day(entry.get("release_date"))
        value = _text(row["VALUE"])
        flags = [f for f in re.split(r"[\s,]+", str(row.get("STATUS") or "").strip()) if f]
        published = {"area": {"scheme": "fao-major-area", "code": row["AREA.CODE"], "name": None},
                     "species": row["SPECIES.ALPHA_3_CODE"], "flag": row["COUNTRY.ISO3_CODE"],
                     "flag_basis": "country or area of the fishing unit, as reported to FAO",
                     "period": {"from": f"{year}-01-01", "to": f"{year}-12-31", "resolution": "year"},
                     "quantity": value, "measure": row["MEASURE"], "unit": units.get(row["MEASURE"], row["MEASURE"]),
                     "status_flags": flags, "release": release}
        subject = {"key": f"fao-fishstat:area:{row['AREA.CODE']}", "kind": "area",
                   "name": f"FAO major fishing area {row['AREA.CODE']}"}
        out.append(statement(
            "catch_observation", "fao-fishstat", subject,
            f"capture:{row['COUNTRY.ISO3_CODE']}:{row['SPECIES.ALPHA_3_CODE']}:{year}:{row['MEASURE']}", published,
            source=_source(url, f"/row/{index}", "fao-fishstat", origin, list="capture-production",
                           release=release), event="release", effective_from=release_date,
            date_basis="FishStat release date"))
    return out


def _rows_csv(body: str, delimiter: str) -> list[dict[str, str]]:
    return [dict(r) for r in csv.DictReader(io.StringIO(body), delimiter=delimiter)]


# Column names per register format: one adapter mode per RFMO.
_REGISTER_COLUMNS = {
    "iccat": {"number": "ICCATSerialNo", "name": "VesselName", "flag": "FlagCode", "call_sign": "IRCS", "imo": "IMO",
              "gear": "GearCode", "from": "AuthFrom", "to": "AuthTo", "owner": "OwnerName"},
    "wcpfc": {"number": "wcpfc_id", "name": "vessel_name", "flag": "flag", "call_sign": "ircs", "imo": "imo",
              "gear": "gear", "from": "authorised_from", "to": "authorised_to", "owner": "owner"},
    "iotc": {"number": "IOTC.No", "name": "Vessel.Name", "flag": "Flag", "call_sign": "IRCS", "imo": "IMO",
             "gear": "Gear", "from": "Auth.From", "to": "Auth.To", "owner": "Owner"},
}
_IUU_COLUMNS = {
    "iccat": {"entry": "IUURef", "name": "VesselName", "flag": "Flag", "imo": "IMO", "call_sign": "IRCS",
              "previous_names": "PreviousNames", "previous_flags": "PreviousFlags", "listed": "ListedOn",
              "delisted": "DelistedOn", "reason": "Reason"},
    "wcpfc": {"entry": "entry", "name": "vessel_name", "flag": "flag", "imo": "imo", "call_sign": "ircs",
              "previous_names": "previous_names", "previous_flags": "previous_flags", "listed": "listed",
              "delisted": "removed", "reason": "reason"},
    "iotc": {"entry": "Ref", "name": "Vessel.Name", "flag": "Flag", "imo": "IMO", "call_sign": "IRCS",
             "previous_names": "Previous.Names", "previous_flags": "Previous.Flags", "listed": "Listed",
             "delisted": "Delisted", "reason": "Reason"},
}
_REGISTER_NAMES = {"iccat": "ICCAT Record of Vessels", "wcpfc": "WCPFC Record of Fishing Vessels",
                   "iotc": "IOTC Record of Authorised Vessels"}
_IUU_NAMES = {"iccat": "ICCAT IUU Vessel List", "wcpfc": "WCPFC IUU Vessel List", "iotc": "IOTC IUU Vessel List"}


def _list_rows(provider: str, body: Any, key: str) -> list[dict[str, Any]]:
    if provider == "wcpfc":
        if not isinstance(body, Mapping) or not isinstance(body.get(key), list):
            raise FisheriesFormatError("schema_drift", f"WCPFC export lacks its {key!r} array")
        return [dict(r) for r in body[key]]
    if not isinstance(body, str):
        raise FisheriesFormatError("schema_drift", f"{provider} list is not a CSV document")
    return _rows_csv(body, ";" if provider == "iotc" else ",")


def _split(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in re.split(r"[|;]", str(value or "")) if v.strip()]


def parse_register(provider: str, body: Any, url: str, *, origin: str, entry: Mapping[str, Any],
                   snapshot_date: str | None) -> list[dict]:
    columns = _REGISTER_COLUMNS[provider]
    rows = _list_rows(provider, body, "vessels")
    if rows and not {columns["number"], columns["name"], columns["flag"]} <= set(rows[0]):
        raise FisheriesFormatError("schema_drift", f"{provider} register lacks its expected columns")
    flags = {str(f) for f in entry["flags"]}
    out = []
    for index, row in enumerate(rows):
        if str(row.get(columns["flag"]) or "") not in flags:
            continue
        number = _text(row.get(columns["number"]))
        if number is None:
            raise FisheriesFormatError("schema_drift", f"{provider} register row lacks its register number")
        valid_from, valid_to = _day(row.get(columns["from"])), _day(row.get(columns["to"]))
        published = {"register": _REGISTER_NAMES[provider], "register_number": number,
                     "vessel_name": _text(row.get(columns["name"])) or "not stated",
                     "flag": _text(row.get(columns["flag"])), "call_sign": _text(row.get(columns["call_sign"])),
                     **_imo_fields(row.get(columns["imo"])), "gear": _text(row.get(columns["gear"])) or "not stated",
                     "valid_from": valid_from, "valid_to": valid_to,
                     "dates_as_published": {"from": _text(row.get(columns["from"])),
                                            "to": _text(row.get(columns["to"]))},
                     "owner_or_operator": _text(row.get(columns["owner"])),
                     "convention_area": {"scheme": "rfmo-convention-area", "code": provider.upper(), "name": None}}
        subject = {"key": f"{provider}:vessel:{number}", "kind": "vessel", "name": published["vessel_name"]}
        out.append(statement("authorisation", provider, subject, f"authorisation:{number}", published,
                             source=_source(url, f"/row/{index}", provider, origin, list="authorised-vessels",
                                            snapshot_date=snapshot_date),
                             event="authorised", effective_from=valid_from, effective_to=valid_to,
                             date_basis="authorisation period as published"))
    return out


def parse_rfmo_iuu(provider: str, body: Any, url: str, *, origin: str, snapshot_date: str | None) -> list[dict]:
    columns = _IUU_COLUMNS[provider]
    rows = _list_rows(provider, body, "entries")
    out = []
    for index, row in enumerate(rows):
        number = _text(row.get(columns["entry"]))
        if number is None:
            raise FisheriesFormatError("schema_drift", f"{provider} IUU row lacks its list reference")
        listed, delisted = _day(row.get(columns["listed"])), _day(row.get(columns["delisted"]))
        names, flags = _split(row.get(columns["previous_names"])), _split(row.get(columns["previous_flags"]))
        published = {"list_body": provider.upper(), "list": _IUU_NAMES[provider], "list_entry": number,
                     "vessel_name": _text(row.get(columns["name"])) or "not stated",
                     "flag": _text(row.get(columns["flag"])), "call_sign": _text(row.get(columns["call_sign"])),
                     **_imo_fields(row.get(columns["imo"])),
                     "previous_identities": {"names": names, "flags": flags},
                     "listed_on": listed, "delisted_on": delisted,
                     "stated_reason": str(row.get(columns["reason"]) or "not stated")}
        subject = {"key": f"{provider}:iuu-entry:{number}", "kind": "vessel", "name": published["vessel_name"]}
        out.append(statement("listing", provider, subject, f"iuu-listing:{number}", published,
                             source=_source(url, f"/row/{index}", provider, origin, list="iuu-vessels",
                                            snapshot_date=snapshot_date),
                             event="delisted" if delisted else "listed", effective_from=delisted or listed,
                             date_basis="delisting date as published" if delisted else "listing date as published"))
    return out


def parse_combined(body: Any, url: str, *, origin: str, snapshot_date: str | None) -> list[dict]:
    if not isinstance(body, Mapping) or not isinstance(body.get("vessels"), list):
        raise FisheriesFormatError("schema_drift", "combined list export lacks its vessels array")
    out = []
    for index, row in enumerate(body["vessels"]):
        number = _text(row.get("id"))
        listings = [{"rfmo": _text(item.get("rfmo")), "reference": _text(item.get("reference")),
                     "listed": _day(item.get("listed")), "delisted": _day(item.get("delisted"))}
                    for item in row.get("listings") or []]
        if number is None or not listings:
            raise FisheriesFormatError("schema_drift", "a combined entry cites no originating RFMO listing")
        active = [x for x in listings if not x["delisted"]]
        listed = min((x["listed"] for x in listings if x["listed"]), default=None)
        delisted = None if active else max((x["delisted"] for x in listings), default=None)
        published = {"list_body": "Combined IUU Vessel List", "list": "Combined IUU Vessel List",
                     "list_entry": number, "vessel_name": _text(row.get("current_name")) or "not stated",
                     "flag": _text(row.get("current_flag")), "call_sign": _text(row.get("call_sign")),
                     **_imo_fields(row.get("imo")),
                     "previous_identities": {"names": _split(row.get("previous_names")),
                                             "flags": _split(row.get("previous_flags"))},
                     "listed_on": listed, "delisted_on": delisted,
                     "stated_reason": str(row.get("reason") or "not stated"),
                     "originating_listings": listings, "independent_confirmation": False,
                     "note": "a compilation entry citing the originating RFMO listings, which remain the authority"}
        subject = {"key": f"combined-iuu:iuu-entry:{number}", "kind": "vessel", "name": published["vessel_name"]}
        out.append(statement("listing", "combined-iuu", subject, f"iuu-listing:{number}", published,
                             source=_source(url, f"/vessels/{index}", "combined-iuu", origin, list="iuu-vessels",
                                            snapshot_date=snapshot_date),
                             event="delisted" if delisted else "listed", effective_from=delisted or listed,
                             date_basis="earliest originating RFMO listing date as published"
                             if not delisted else "latest originating delisting date as published"))
    return out


def _snapshot_date(provider: str, body: Any, headers: Mapping[str, Any]) -> str | None:
    if isinstance(body, Mapping):
        for field in ("as_of", "generated"):
            if _day(body.get(field)):
                return _day(body.get(field))
    modified = {str(k).casefold(): v for k, v in headers.items()}.get("last-modified")
    if modified:
        try:
            return parsedate_to_datetime(str(modified)).date().isoformat()
        except (TypeError, ValueError):
            return None
    return None


# ------------------------------------------------------------------ runtime adapter


class FisheriesSourceAdapter:
    """One page per selected list, release, vessel or effort report on the runtime's default transport."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.provider, self.entries = selection_entries(self.source)
        self.declared = dict(self.source["fisheries"])
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self._secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "fisheries": {"provider": self.provider, "selected": len(self.entries)},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "fisheries runs fetch the declared selection only")

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, Any, str, str, dict[str, Any]]:
        base = self.source["endpoint"].rstrip("/") + path
        url = base + ("?" + urlencode(sorted(query.items())) if query else "")
        headers = {"Accept": "application/json, text/csv"}
        if self.provider == "gfw":
            if not self._secret:
                raise SourcePackError("authentication_failed", "the GFW API token secret is not configured")
            headers["Authorization"] = f"Bearer {self._secret}"
        response = self.transport(url=base, params=dict(query), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        headers_in = dict(response.get("headers") or {})
        if status == 404:
            return status, None, url, origin, headers_in
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            folded = {str(k).casefold(): v for k, v in headers_in.items()}
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(folded.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise SourcePackError("schema_drift", "response is not UTF-8") from exc
        stripped = text.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                return status, json.loads(text), url, origin, headers_in
            except json.JSONDecodeError as exc:
                raise SourcePackError("schema_drift", "response is not valid JSON") from exc
        return status, text, url, origin, headers_in

    def _parse(self, entry, body, url, origin, snapshot_date):
        if self.provider == "gfw":
            parser = parse_gfw_vessel if entry["kind"] == "vessel" else parse_gfw_effort
            return parser(body, url, origin=origin, entry=entry)
        if self.provider == "fao-fishstat":
            if not isinstance(body, str):
                raise FisheriesFormatError("schema_drift", "FishStat release is not a CSV document")
            return parse_fishstat(body, url, origin=origin, entry=entry)
        if self.provider == "combined-iuu":
            return parse_combined(body, url, origin=origin, snapshot_date=snapshot_date)
        if entry["list"] == "authorised-vessels":
            return parse_register(self.provider, body, url, origin=origin, entry=entry, snapshot_date=snapshot_date)
        return parse_rfmo_iuu(self.provider, body, url, origin=origin, snapshot_date=snapshot_date)

    def _list_kind(self, entry: Mapping[str, Any]) -> str:
        if self.provider == "gfw":
            return "vessel-identity" if entry["kind"] == "vessel" else "fishing-effort"
        if self.provider == "fao-fishstat":
            return "capture-production"
        return str(entry["list"])

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(self.entries):
            raise SourcePackError("cursor_drift", "cursor names no declared selection")
        entry = self.entries[index]
        ((role, path, query),) = requests_for(self.provider, entry)
        status, body, url, origin, headers = self._get(path, query)
        dropped = [f"{role}:{name}" for name in dropped_fields(body)] if body is not None else []
        label = {k: entry[k] for k in sorted(entry) if k in {"kind", "list", "vessel_id", "region_id", "dataset",
                                                              "date_from", "date_to", "label"}}
        snapshot_date = _snapshot_date(self.provider, body, headers) if body is not None else None
        if body is None:
            outcome, statements = "not_found", []
        else:
            try:
                statements = self._parse(entry, body, url, origin, snapshot_date)
            except (FisheriesFormatError, FisheriesError, KeyError, TypeError, ValueError) as exc:
                raise SourcePackError("schema_drift", f"{getattr(exc, 'code', 'parse')}: {exc}") from exc
            outcome = "found"
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            raise SourcePackError("budget_exhausted", "selection has more statements than the run's result budget")
        records = []
        for item in statements:
            content = json.dumps(item, sort_keys=True, ensure_ascii=False)
            records.append({
                "id": f"{item['subject']['key']}|{item['record_type']}|{item['record_key']}|"
                      + hashlib.sha256(content.encode()).hexdigest()[:12],
                "title": f"{item['subject'].get('name') or item['subject']['key']}: {item['record_type']}",
                "url": item["source"]["url"], "language": "en", "content": content, "fisheries_record": item})
        list_kind = self._list_kind(entry)
        receipt = {"status": 200, "provider": self.provider, "selection": label, "outcome": outcome,
                   "statements": len(records), "excluded_fields_dropped": sorted(set(dropped)),
                   "evidence_origin": origin, "final_page": index + 1 >= len(self.entries),
                   "snapshot": {"list_key": f"{self.provider}:{list_kind}", "provider": self.provider,
                                "list_kind": list_kind, "snapshot_date": snapshot_date,
                                "release": next((s["source"].get("release") or s["source"].get("dataset_version")
                                                 for s in statements), None),
                                "selection_key": selection_key(entry), "url": url}}
        next_cursor = str(index + 1) if index + 1 < len(self.entries) else None
        return RuntimePage(tuple(records), next_cursor, len(json.dumps(body)) if body is not None else 0,
                           receipt=receipt)


FIXTURE_SECRET = "fixture-credential"
ADAPTERS = {CONNECTOR: FisheriesSourceAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query; responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + urlencode(sorted(dict(params or {}).items())) if params else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = FisheriesSourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                     secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS", "CONNECTOR", "EXCLUDED_FIELDS", "EXCLUDED_RFMOS", "FIXTURE_SECRET", "FisheriesFormatError",
    "FisheriesSourceAdapter", "LIVE_VERIFICATION", "PROVIDERS", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS",
    "dropped_fields", "fixture_transport", "parse_combined", "parse_fishstat", "parse_gfw_effort",
    "parse_gfw_vessel", "parse_register", "parse_rfmo_iuu", "replay_native_fixture", "requests_for",
    "selection_entries", "selection_key",
]
