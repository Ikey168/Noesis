"""OSINT movement sources: aircraft registries, OpenSky samples, GFW port visits, open AIS and port-call aggregates.

Six providers run as sources of the ``bounded-public-osint`` source pack
(``config/source_packs/osint.json``, connector ``osint-movements``) through
:mod:`src.ingestion.source_pack_runtime` - licence acceptance, budgets,
receipts, checkpoints and the runtime's same-host HTTPS transport - each under
the access decision in ``docs/security/osint-movements-access.md`` (MV01),
recorded here as :data:`PROVIDER_CONTRACTS`:

* **FAA Aircraft Registry** (``faa-registry``, MV03 #2242) - one Aircraft
  Inquiry page per selected N-number: registration, Mode S code (hex), serial,
  type, status and registrant as published. Every change is a dated revision;
  deregistration is a revision. Owner addresses are never parsed; entries
  withheld under the FAA privacy programmes are not stored and are added to
  the refusal list.
* **UK CAA G-INFO** (``uk-caa-ginfo``, MV04 #2247) - one entry per selected
  G- registration mark, with its nationality prefix; the ICAO 24-bit address
  only when G-INFO publishes it.
* **OpenSky Network** (``opensky``, MV05 #2252) - for one named ICAO 24-bit
  address and a window of at most 48 hours: the flights OpenSky lists (with its
  *estimated* departure and arrival airports, stored as source-published
  calls) and at most five flight tracks, whose waypoints become at most 500
  position samples in a sample window with declared gaps. No area query.
* **Global Fishing Watch port visits** (``gfw-port-visits``, MV06 #2258) - the
  port-visit events GFW publishes for one named GFW vessel id and a window of at
  most 366 days, with GFW's confidence as published. Vessel identity is the
  Fisheries pack's (read from its store by citation), never re-acquired here.
* **Kystdatahuset open AIS** (``kystdatahuset-ais``, MV07 #2263) - positions
  for one named MMSI and a window of at most 72 hours (at most 500 samples).
* **UNCTAD port-call statistics** (``unctad-port-calls``, MV07) - published
  aggregates for declared economies and years, stored as aggregates.

Every selection is explicit (one identifier and one window per page) and is
checked against the source's ``privacy_refusals`` before any request. Every
provider is ``unverified-live`` until a dated live run (#2291); request paths
and field names marked *verify* are authored from public documentation.
"""

from __future__ import annotations

import csv
import hashlib
import html
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.osint.movements import (
    BOUNDS,
    COVERAGE_CAVEAT,
    MovementError,
    coverage_gaps,
    digest,
    hours_between,
    identifier_key,
    nationality_prefix,
    parse_time,
    stamp,
    statement,
    subject_key,
    thin,
    window_bound,
    window_id,
)

CONNECTOR = "osint-movements"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("faa-registry", "uk-caa-ginfo", "opensky", "gfw-port-visits", "kystdatahuset-ais", "unctad-port-calls")
PROVIDER_HOSTS = {
    "faa-registry": ("registry.faa.gov",),
    "uk-caa-ginfo": ("siteapps.caa.co.uk",),
    "opensky": ("opensky-network.org",),
    "gfw-port-visits": ("gateway.api.globalfishingwatch.org",),
    "kystdatahuset-ais": ("kystdatahuset.no",),
    "unctad-port-calls": ("unctadstat-api.unctad.org",),
}
_DECISION = "docs/security/osint-movements-access.md#decisions"
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "faa-registry": {
        "publisher": "Federal Aviation Administration, Aircraft Registration Branch",
        "access": "Aircraft Inquiry by N-number (HTTPS GET, HTML); the Releasable Aircraft Database carries the same "
                  "fields",
        "endpoints": ["/AircraftInquiry/Search/NNumberResult?nNumberTxt={N-number} (verify)"],
        "authentication": "none",
        "licence": "US federal public record",
        "attribution": "Source: FAA Aircraft Registry (registry.faa.gov)",
        "redistribution": "public record; registry facts may be republished",
        "rate_limits": "none published; one N-number per page within the source budget",
        "volume": "one N-number per page, at most 50 per source; registry state only",
        "privacy": "entries withheld under the FAA privacy programmes are not stored; LADD/PIA identifiers the "
                   "operator holds are declared as privacy_refusals and refused before any request; owner addresses "
                   "are never parsed; a natural-person registrant is stored as published, flagged, never a key",
        "decision": _DECISION,
    },
    "uk-caa-ginfo": {
        "publisher": "UK Civil Aviation Authority (G-INFO)",
        "access": "G-INFO aircraft entry by registration mark (HTTPS GET, JSON; verify the endpoint and field names)",
        "endpoints": ["/g-info/api/aircraft?registration={G-mark} (verify)"],
        "authentication": "none",
        "licence": "CAA G-INFO terms: reuse with attribution (verify commercial-use terms)",
        "attribution": "Source: UK Civil Aviation Authority, G-INFO",
        "redistribution": "registry facts with attribution; no bulk republication",
        "rate_limits": "none published; one registration per page",
        "volume": "one registration per page, at most 50 per source",
        "privacy": "individual owners stored only as published, flagged, never a key; addresses never parsed",
        "decision": _DECISION,
    },
    "opensky": {
        "publisher": "The OpenSky Network",
        "access": "REST API: /api/flights/aircraft and /api/tracks/all for one ICAO 24-bit address (HTTPS GET, JSON)",
        "endpoints": ["/api/flights/aircraft?icao24={hex}&begin={epoch}&end={epoch} (interval at most 2 days, verify)",
                      "/api/tracks/all?icao24={hex}&time={epoch} (experimental endpoint, verify)"],
        "authentication": "optional account credential NOESIS_OPENSKY_CREDENTIAL (user:password) for historical "
                          "data (verify)",
        "licence": "OpenSky terms of use and research licence: research and non-commercial use; cite the OpenSky "
                   "paper (verify)",
        "attribution": "The OpenSky Network, https://opensky-network.org; Schäfer et al., IPSN 2014",
        "redistribution": "no commercial redistribution; bounded samples cited with attribution",
        "rate_limits": "credit-based daily limits per account (verify); one aircraft and window per page",
        "volume": "one ICAO 24-bit address, window at most 48 h, at most 5 tracks and 500 samples per window",
        "privacy": "aircraft on privacy_refusals are refused before any request; samples of an aircraft whose "
                   "registry record names a natural person are refused at projection",
        "decision": _DECISION,
    },
    "gfw-port-visits": {
        "publisher": "Global Fishing Watch",
        "access": "Events API v3, port-visit dataset, for one GFW vessel id (HTTPS GET, JSON, bearer token)",
        "endpoints": ["/v3/events?datasets[0]=public-global-port-visits-events:latest&vessels[0]={id}&start-date="
                      "{date}&end-date={date}&limit=200&offset=0 (verify)"],
        "authentication": "API token NOESIS_GFW_API_TOKEN (shared with the Fisheries pack)",
        "licence": "GFW API terms: non-commercial use; data CC BY-NC 4.0 (verify)",
        "attribution": "Source: Global Fishing Watch, port-visit events dataset as cited",
        "redistribution": "non-commercial with attribution",
        "rate_limits": "per-token limits (verify); one vessel and window per page",
        "volume": "one vessel id, window at most 366 days, at most 200 events; no tracks",
        "identity": "vessel identity segments are the Fisheries pack's records (fisheries_records, provider gfw) and "
                    "are read and cited from that store; they are not re-acquired",
        "decision": _DECISION,
    },
    "kystdatahuset-ais": {
        "publisher": "Kystverket (Norwegian Coastal Administration), Kystdatahuset",
        "access": "AIS positions for one MMSI and a time window (HTTPS GET, JSON; verify the endpoint and fields)",
        "endpoints": ["/ws/api/ais/positions/for-mmsis-time?mmsiIds={mmsi}&start={iso}&end={iso} (verify)"],
        "authentication": "registered-user token NOESIS_KYSTDATAHUSET_TOKEN (verify)",
        "licence": "Norwegian Licence for Open Government Data (NLOD 2.0) (verify)",
        "attribution": "Source: Kystverket / Kystdatahuset, NLOD 2.0",
        "redistribution": "permitted with attribution",
        "rate_limits": "not published (verify); one vessel and window per page",
        "volume": "one MMSI, window at most 72 h, at most 500 samples",
        "decision": _DECISION,
    },
    "unctad-port-calls": {
        "publisher": "UNCTAD (UNCTADstat), port call statistics derived from AIS",
        "access": "UNCTADstat bulk CSV, filtered to declared economies and years (verify the bulk path)",
        "endpoints": ["/bulkdownload/US.PortCalls/US_PortCalls (verify)"],
        "authentication": "none",
        "licence": "UNCTADstat terms: reuse with attribution (verify)",
        "attribution": "Source: UNCTADstat, port call statistics",
        "redistribution": "permitted with attribution",
        "rate_limits": "none published; one release file per page",
        "volume": "at most 10 economies and 5 years per selection; aggregates only, never per vessel",
        "decision": _DECISION,
    },
}
EXCLUDED_SOURCES = {
    "transport-canada-ccar": "bulk whole-register download only; no per-mark query within the per-source bound",
    "lba-luftfahrzeugrolle": "not published as open data; no reuse right recorded",
    "dgac-france": "individual look-ups without a reuse licence",
    "adsb-exchange": "API terms prohibit republication and storage beyond the session",
    "flightradar24": "terms prohibit scraping, storage and redistribution",
    "flightaware": "terms prohibit scraping, storage and redistribution",
    "noaa-marinecadastre-ais": "bulk daily or zone files only; the per-vessel bound needs mirroring",
    "dma-ais": "daily whole-area files only",
    "aishub": "member feed; redistribution restricted to members' own use",
    "marinetraffic": "commercial terms forbid scraping, storage and redistribution",
    "vesselfinder": "commercial terms forbid scraping, storage and redistribution",
    "myshiptracking": "commercial terms forbid scraping, storage and redistribution",
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "note": "no dated live run from this runtime; offline fixtures only "
                                                    "(#2291)"}
    for provider in PROVIDERS
}
# Never parsed: owner and operator addresses, per-person contact data and whole-track blobs of other aircraft.
EXCLUDED_FIELDS = ("street", "street2", "city", "state", "zip code", "zip", "county", "address", "owner address",
                   "email", "phone", "telephone")
FAA_PERSON_TYPES = {"individual", "co-owned", "non-citizen co-owned"}
GINFO_PERSON_TYPES = {"individual", "private individual", "joint individuals"}
_FAA_LABEL = re.compile(r'<td[^>]*data-label="([^"]+)"[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_MDY = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_PRIVACY_PROGRAMMES = ("faa-ladd", "faa-pia", "faa-privacy", "operator-declared", "caa-withheld")


class MovementFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str | None:
    text = " ".join(html.unescape(_TAG.sub(" ", str(value))).split()) if value is not None else ""
    return text or None


def _date(value: Any) -> str | None:
    text = _text(value) or ""
    if match := _MDY.fullmatch(text):
        return f"{match.group(3)}-{match.group(1)}-{match.group(2)}"
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    return None


def _label(key: Any) -> str:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", str(key))
    return re.sub(r"[_\s.-]+", " ", spaced).strip().casefold()


# ------------------------------------------------------------------ selections


def _declared(source: Mapping[str, Any]) -> dict[str, Any]:
    return dict(source.get("osint_movements") or {})


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = _declared(source)
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"movement sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    bounds = BOUNDS[provider]
    limit = min(int(dict(source.get("budgets") or {}).get("max_pages", 1)),
                int(bounds.get("max_identifiers_per_source") or bounds.get("max_selections") or 10))
    if not 1 <= len(entries) <= limit:
        raise SourcePackError("invalid_manifest", f"a {provider} source selects 1..{limit} identifiers explicitly")
    for entry in entries:
        try:
            _check_entry(provider, entry)
        except MovementError as exc:
            raise SourcePackError("invalid_manifest", f"{exc.code}: {exc}") from exc
    for refusal in declared.get("privacy_refusals") or []:
        if refusal.get("programme") not in _PRIVACY_PROGRAMMES or not refusal.get("reason") or \
                identifier_key(str(refusal.get("scheme")), refusal.get("identifier")) is None:
            raise SourcePackError("invalid_manifest", "a privacy refusal names a programme, a reason and a "
                                                      "well-formed identifier")
    return provider, entries


def _check_entry(provider: str, entry: Mapping[str, Any]) -> None:
    def window(start_key: str, end_key: str) -> None:
        span = hours_between(entry[start_key], entry[end_key])
        if span <= 0:
            raise MovementError("invalid_selection", "a selection window ends after it starts")
        if span > BOUNDS[provider]["max_window_hours"]:
            raise MovementError("over_bound", f"a {provider} window is at most "
                                              f"{BOUNDS[provider]['max_window_hours']} h")

    if provider == "faa-registry":
        mark = identifier_key("registration", entry.get("n_number"))
        if not mark or not mark.startswith("N"):
            raise MovementError("invalid_selection", "FAA selections name one N-number")
    elif provider == "uk-caa-ginfo":
        mark = identifier_key("registration", entry.get("registration"))
        if not mark or not mark.startswith("G-"):
            raise MovementError("invalid_selection", "G-INFO selections name one G- registration mark")
    elif provider == "opensky":
        if identifier_key("icao24", entry.get("icao24")) is None or not entry.get("begin") or not entry.get("end"):
            raise MovementError("invalid_selection", "OpenSky selections name one ICAO 24-bit address and a window")
        window("begin", "end")
    elif provider == "gfw-port-visits":
        if identifier_key("gfw_vessel_id", entry.get("vessel_id")) is None or not entry.get("start") or \
                not entry.get("end"):
            raise MovementError("invalid_selection", "GFW port-visit selections name one vessel id and a window")
        window("start", "end")
    elif provider == "kystdatahuset-ais":
        if identifier_key("mmsi", entry.get("mmsi")) is None or not entry.get("start") or not entry.get("end"):
            raise MovementError("invalid_selection", "AIS selections name one MMSI and a window")
        if entry.get("imo") and identifier_key("imo", entry["imo"]) is None:
            raise MovementError("invalid_selection", "a stated IMO number must verify")
        window("start", "end")
    elif provider == "unctad-port-calls":
        economies, years = list(entry.get("economies") or []), list(entry.get("years") or [])
        if not economies or not years or len(economies) > BOUNDS[provider]["max_economies"] or \
                len(years) > BOUNDS[provider]["max_years"]:
            raise MovementError("over_bound", "port-call selections name 1..10 economies and 1..5 years")


def selection_key(entry: Mapping[str, Any]) -> str:
    return digest({k: entry[k] for k in sorted(entry) if k != "label"})[:16]


def _epoch(value: Any) -> int:
    return int(parse_time(value).timestamp())


def entry_identifier(provider: str, entry: Mapping[str, Any]) -> tuple[str, str] | None:
    """(scheme, key) a selection names, for the privacy check."""
    field = {"faa-registry": ("registration", "n_number"), "uk-caa-ginfo": ("registration", "registration"),
             "opensky": ("icao24", "icao24"), "gfw-port-visits": ("gfw_vessel_id", "vessel_id"),
             "kystdatahuset-ais": ("mmsi", "mmsi")}.get(provider)
    if field is None:
        return None
    return field[0], identifier_key(field[0], entry[field[1]]) or ""


def requests_for(provider: str, entry: Mapping[str, Any]) -> list[tuple[str, str, dict[str, str]]]:
    """(role, path, query) of the first request(s) of one page; OpenSky tracks follow from its flight list."""
    if provider == "faa-registry":
        return [("registry", "/AircraftInquiry/Search/NNumberResult",
                 {"nNumberTxt": identifier_key("registration", entry["n_number"]) or ""})]
    if provider == "uk-caa-ginfo":
        return [("registry", "/g-info/api/aircraft", {"registration": identifier_key("registration",
                                                                                      entry["registration"]) or ""})]
    if provider == "opensky":
        return [("flights", "/api/flights/aircraft", {"icao24": identifier_key("icao24", entry["icao24"]) or "",
                                                     "begin": str(_epoch(entry["begin"])),
                                                     "end": str(_epoch(entry["end"]))})]
    if provider == "gfw-port-visits":
        return [("events", "/v3/events", {
            "datasets[0]": str(entry.get("dataset") or "public-global-port-visits-events:latest"),
            "vessels[0]": str(entry["vessel_id"]), "start-date": str(entry["start"])[:10],
            "end-date": str(entry["end"])[:10], "limit": str(BOUNDS[provider]["max_events"]), "offset": "0"})]
    if provider == "kystdatahuset-ais":
        return [("positions", "/ws/api/ais/positions/for-mmsis-time",
                 {"mmsiIds": str(entry["mmsi"]), "start": stamp(entry["start"]), "end": stamp(entry["end"])})]
    return [("release", "/bulkdownload/US.PortCalls/US_PortCalls", {})]


# ------------------------------------------------------------------ parsers


def _source(url: str, locator: str, origin: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "locator": locator, "evidence_origin": origin, **{k: v for k, v in extra.items() if v}}


def dropped_fields(labels: Sequence[str]) -> list[str]:
    return sorted({str(label) for label in labels if _label(label) in EXCLUDED_FIELDS})


def parse_faa(body: str, url: str, *, origin: str, entry: Mapping[str, Any]) -> tuple[list[dict], dict[str, Any]]:
    """One FAA Aircraft Inquiry page -> a registry_record statement; a withheld entry yields none."""
    mark = identifier_key("registration", entry["n_number"])
    folded = body.casefold()
    if "withheld" in folded and ("privacy" in folded or "owner's request" in folded):
        return [], {"outcome": "withheld", "refusal": {"scheme": "registration", "identifier": mark,
                                                       "programme": "faa-privacy",
                                                       "reason": "the FAA registry withholds this entry under a "
                                                                 "privacy programme"}}
    pairs: dict[str, str] = {}
    labels = []
    for raw_label, raw_value in _FAA_LABEL.findall(body):
        label = _text(raw_label) or ""
        labels.append(label)
        if _label(label) in EXCLUDED_FIELDS:
            continue
        pairs.setdefault(label.casefold(), _text(raw_value) or "")
    if not pairs:
        if "not assigned" in folded or "no records" in folded or "not found" in folded:
            return [], {"outcome": "not_found"}
        raise MovementFormatError("schema_drift", "FAA inquiry page carries no labelled fields")
    hex_code = identifier_key("icao24", pairs.get("mode s code (base 16 / hex)") or pairs.get("mode s code (hex)"))
    status = pairs.get("status") or "not stated"
    registrant_type = pairs.get("type registrant") or "not stated"
    person = registrant_type.casefold() in FAA_PERSON_TYPES
    deregistered = "dereg" in status.casefold() or "deregistered aircraft" in folded
    published = {
        "registration": mark, "nationality_prefix": nationality_prefix(mark), "icao24": hex_code,
        "mode_s_octal": pairs.get("mode s code (base 8 / oct)") or None, "serial_number": pairs.get("serial number"),
        "manufacturer": pairs.get("manufacturer name"), "model": pairs.get("model"),
        "aircraft_type": pairs.get("type aircraft"), "engine_type": pairs.get("type engine"), "status": status,
        "certificate_issue_date": _date(pairs.get("certificate issue date")),
        "expiration_date": _date(pairs.get("expiration date")),
        "cancel_date": _date(pairs.get("cancel date")),
        "registrant": {"kind": registrant_type, "name": pairs.get("name"), "natural_person": person,
                       "note": "as published; never a lookup key" + ("; a natural person" if person else "")},
    }
    identifiers = [{"scheme": "registration", "value": mark}]
    if hex_code:
        identifiers.append({"scheme": "icao24", "value": hex_code})
    if published["serial_number"]:
        identifiers.append({"scheme": "serial_number", "value": published["serial_number"]})
    effective = published["cancel_date"] if deregistered else published["certificate_issue_date"]
    value = statement("registry_record", "faa-registry", subject_key("registration", mark), f"faa:{mark}",
                      {k: v for k, v in published.items() if v is not None}, identifiers=identifiers,
                      source=_source(url, "#aircraft-inquiry", origin), event="deregistered" if deregistered else
                      "registered", effective_from=effective, effective_to=published["expiration_date"]
                      if not deregistered else None, date_basis="deregistration (cancel) date" if deregistered else
                      "certificate issue date")
    return [value], {"outcome": "found", "dropped": dropped_fields(labels)}


def parse_ginfo(body: Mapping[str, Any], url: str, *, origin: str,
                entry: Mapping[str, Any]) -> tuple[list[dict], dict[str, Any]]:
    mark = identifier_key("registration", entry["registration"])
    item = body.get("aircraft") if isinstance(body.get("aircraft"), Mapping) else body
    if not isinstance(item, Mapping) or not item.get("registrationMark"):
        raise MovementFormatError("schema_drift", "G-INFO payload has no registrationMark")
    if item.get("withheld"):
        return [], {"outcome": "withheld", "refusal": {"scheme": "registration", "identifier": mark,
                                                       "programme": "caa-withheld",
                                                       "reason": "G-INFO withholds this entry"}}
    labels = [str(k) for k in item]
    for owner in item.get("owners") or []:
        labels += [str(k) for k in owner]
    hex_code = identifier_key("icao24", item.get("icaoAircraftAddress"))
    owner_type = str(item.get("ownerType") or "not stated")
    person = owner_type.casefold() in GINFO_PERSON_TYPES
    owners = [_text(o.get("name")) for o in item.get("owners") or [] if _text(o.get("name"))]
    status = str(item.get("registrationStatus") or "not stated")
    deregistered = status.casefold().startswith("dereg")
    published = {
        "registration": mark, "nationality_prefix": nationality_prefix(mark), "icao24": hex_code,
        "manufacturer": _text(item.get("manufacturer")), "model": _text(item.get("model")),
        "serial_number": _text(item.get("serialNumber")), "status": status,
        "registered_date": _date(item.get("registeredDate")), "deregistered_date": _date(item.get("deregisteredDate")),
        "deregistration_reason": _text(item.get("deregistrationReason")),
        "registrant": {"kind": owner_type, "name": "; ".join(owners) or None, "natural_person": person,
                       "note": "as published; never a lookup key" + ("; a natural person" if person else "")},
    }
    identifiers = [{"scheme": "registration", "value": mark}]
    if hex_code:
        identifiers.append({"scheme": "icao24", "value": hex_code})
    if published["serial_number"]:
        identifiers.append({"scheme": "serial_number", "value": published["serial_number"]})
    value = statement("registry_record", "uk-caa-ginfo", subject_key("registration", mark), f"ginfo:{mark}",
                      {k: v for k, v in published.items() if v is not None}, identifiers=identifiers,
                      source=_source(url, "/aircraft", origin), event="deregistered" if deregistered else
                      "registered", effective_from=published["deregistered_date"] if deregistered else
                      published["registered_date"], date_basis="deregistration date" if deregistered else
                      "registration date")
    return [value], {"outcome": "found", "dropped": dropped_fields(labels)}


def _window_statement(provider: str, subject: str, identifiers: list[dict], win: Mapping[str, Any],
                      query: Mapping[str, Any], url: str, origin: str, *, published: int, stored: list[str],
                      thinned: bool, receiver: str, events: int | None = None) -> dict[str, Any]:
    bound = window_bound(provider)
    if events is None:
        status = "positions_observed" if stored else "no_coverage_observed"
        gaps = coverage_gaps(stored, win["start"], win["end"], bound["gap_seconds"])
    else:
        status = "events_published" if events else "no_events_published"
        gaps = []
    coverage = {"status": status, "samples_published": published, "samples_stored": len(stored),
                "thinned": thinned, "gaps": gaps, "receiver_category": receiver, "caveat": COVERAGE_CAVEAT}
    return statement("sample_window", provider, subject, f"window:{win['window_id']}",
                     {"query": dict(query), "start": win["start"], "end": win["end"],
                      **({"events_published": events} if events is not None else {})},
                     identifiers=identifiers, source=_source(url, "#window", origin, query=dict(query)),
                     event="observed", effective_from=win["start"], effective_to=win["end"],
                     date_basis="requested window", window=win, bound=bound, coverage=coverage)


OPENSKY_RECEIVERS = ("OpenSky Network crowdsourced ADS-B / Mode S receivers; the tracks endpoint does not publish a "
                     "per-point position source")


def parse_opensky(flights: Any, tracks: Sequence[tuple[str, Any]], flights_url: str, *, origin: str,
                  entry: Mapping[str, Any]) -> tuple[list[dict], dict[str, Any]]:
    hex_code = identifier_key("icao24", entry["icao24"])
    subject = subject_key("icao24", hex_code)
    begin, end = stamp(entry["begin"]), stamp(entry["end"])
    query = {"icao24": hex_code, "begin": begin, "end": end}
    win = {"window_id": window_id("opensky", subject, begin, end, query), "start": begin, "end": end}
    ids = [{"scheme": "icao24", "value": hex_code}]
    out: list[dict] = []
    flights = flights if isinstance(flights, list) else []
    callsigns = set()
    for index, flight in enumerate(flights):
        callsign = _text(flight.get("callsign"))
        if callsign and identifier_key("flight_callsign", callsign):
            callsigns.add(callsign)
        for event, airport_key, time_key in (("departure", "estDepartureAirport", "firstSeen"),
                                             ("arrival", "estArrivalAirport", "lastSeen")):
            at = flight.get(time_key)
            if at is None:
                continue
            at_stamp = stamp(int(at))
            if not begin <= at_stamp <= end:
                continue
            airport = _text(flight.get(airport_key))
            out.append(statement(
                "call", "opensky", subject, f"flight:{flight.get('firstSeen')}:{event}",
                {"status": "source-published", "event": event, "time": at_stamp,
                 "facility": {"kind": "airport", "code": airport, "resolution": "unresolved"} if airport else
                 {"kind": "airport", "code": None, "resolution": "not published by the source"},
                 "confidence": f"estimated by OpenSky ({airport_key})", "flight_callsign": callsign},
                identifiers=ids, source=_source(flights_url, f"/{index}", origin, query=query), event="published",
                effective_from=at_stamp, date_basis=f"OpenSky {time_key}", window=win))
    for callsign in sorted(callsigns):
        out.append(statement("aircraft_identity", "opensky", subject, f"callsign:{callsign}",
                             {"icao24": hex_code, "flight_callsign": callsign,
                              "note": "a flight call sign is context, not an aircraft identity"},
                             identifiers=[*ids, {"scheme": "flight_callsign", "value": callsign}],
                             source=_source(flights_url, "#callsign", origin, query=query), event="observed",
                             effective_from=begin, effective_to=end, date_basis="requested window"))
    points = []
    for track_url, track in tracks:
        for position, waypoint in enumerate((track or {}).get("path") or []):
            if not isinstance(waypoint, list) or len(waypoint) < 3 or waypoint[1] is None or waypoint[2] is None:
                continue
            at_stamp = stamp(int(waypoint[0]))
            if begin <= at_stamp <= end:
                points.append((at_stamp, track_url, position, waypoint))
    points = sorted(dict(((p[0], p) for p in points)).values())
    kept, thinned = thin(points, BOUNDS["opensky"]["max_samples"])
    for at_stamp, track_url, position, waypoint in kept:
        out.append(statement(
            "position_sample", "opensky", subject, f"sample:{at_stamp}",
            {"timestamp": at_stamp, "lat": float(waypoint[1]), "lon": float(waypoint[2]),
             "altitude_m": None if len(waypoint) < 4 or waypoint[3] is None else float(waypoint[3]),
             "altitude_kind": "barometric", "true_track": None if len(waypoint) < 5 or waypoint[4] is None
             else float(waypoint[4]), "on_ground": bool(waypoint[5]) if len(waypoint) > 5 else None,
             "receiver_category": OPENSKY_RECEIVERS},
            identifiers=ids, source=_source(track_url, f"/path/{position}", origin), event="observed",
            effective_from=at_stamp, window=win))
    out.append(_window_statement("opensky", subject, ids, win, query, flights_url, origin, published=len(points),
                                 stored=[p[0] for p in kept], thinned=thinned, receiver=OPENSKY_RECEIVERS))
    return out, {"outcome": "found" if flights or points else "no_coverage", "thinned": thinned}


def parse_gfw_events(body: Any, url: str, *, origin: str, entry: Mapping[str, Any]) -> tuple[list[dict], dict]:
    vessel = identifier_key("gfw_vessel_id", entry["vessel_id"])
    subject = subject_key("gfw_vessel_id", vessel)
    start, end = stamp(entry["start"]), stamp(entry["end"])
    query = {"vessel_id": vessel, "start": start, "end": end,
             "dataset": str(entry.get("dataset") or "public-global-port-visits-events:latest")}
    win = {"window_id": window_id("gfw-port-visits", subject, start, end, query), "start": start, "end": end}
    ids = [{"scheme": "gfw_vessel_id", "value": vessel}]
    entries = body.get("entries") if isinstance(body, Mapping) else None
    if entries is None:
        raise MovementFormatError("schema_drift", "GFW events payload has no entries")
    if len(entries) > BOUNDS["gfw-port-visits"]["max_events"]:
        raise MovementFormatError("over_bound", "more port-visit events than the MV01 bound")
    out: list[dict] = []
    identities = {}
    for index, event in enumerate(entries):
        if event.get("type") not in (None, "port_visit"):
            continue
        visit = event.get("port_visit") or {}
        anchorage = visit.get("startAnchorage") or visit.get("intermediateAnchorage") or {}
        published_vessel = event.get("vessel") or {}
        mmsi = identifier_key("mmsi", published_vessel.get("ssvid"))
        arrival, departure = stamp(event["start"]), stamp(event["end"])
        if mmsi:
            identities.setdefault(mmsi, []).append((arrival, departure))
        out.append(statement(
            "call", "gfw-port-visits", subject, f"gfw-port-visit:{event.get('id')}",
            {"status": "source-published", "event_id": _text(event.get("id")), "arrival": arrival,
             "departure": departure, "duration_hours": visit.get("durationHrs"),
             "confidence": visit.get("confidence"),
             "confidence_scale": "GFW port-visit confidence level (2-4) as published",
             "facility": {"kind": "port", "name": _text(anchorage.get("name")),
                          "anchorage_id": _text(anchorage.get("anchorageId") or anchorage.get("id")),
                          "flag": _text(anchorage.get("flag")), "resolution": "unresolved"},
             "vessel_as_published": {"gfw_vessel_id": _text(published_vessel.get("id")), "mmsi": mmsi,
                                     "name": _text(published_vessel.get("name")),
                                     "flag": _text(published_vessel.get("flag"))}},
            identifiers=ids, source=_source(url, f"/entries/{index}", origin, query=query,
                                            dataset_version=query["dataset"]),
            event="published", effective_from=arrival, effective_to=departure, date_basis="port-visit start and end",
            window=win))
    for mmsi, spans in sorted(identities.items()):
        out.append(statement(
            "vessel_identity", "gfw-port-visits", subject, f"mmsi:{mmsi}",
            {"gfw_vessel_id": vessel, "mmsi": mmsi, "note": "as stated on GFW port-visit events; identity history "
                                                            "is the Fisheries pack's GFW vessel record"},
            identifiers=[*ids, {"scheme": "mmsi", "value": mmsi, "valid_from": min(s[0] for s in spans),
                                "valid_to": max(s[1] for s in spans)}],
            source=_source(url, "#vessel", origin, query=query, dataset_version=query["dataset"]), event="observed",
            effective_from=min(s[0] for s in spans), effective_to=max(s[1] for s in spans),
            date_basis="span of the events stating it"))
    out.append(_window_statement("gfw-port-visits", subject, ids, win, query, url, origin, published=0, stored=[],
                                 thinned=False, receiver="GFW port-visit events derived by GFW from AIS",
                                 events=len(out) - len(identities)))
    return out, {"outcome": "found" if entries else "no_coverage"}


AIS_RECEIVERS = "Kystverket terrestrial and satellite AIS receivers (as published)"


def parse_ais(body: Any, url: str, *, origin: str, entry: Mapping[str, Any]) -> tuple[list[dict], dict]:
    mmsi = identifier_key("mmsi", entry["mmsi"])
    subject = subject_key("mmsi", mmsi)
    start, end = stamp(entry["start"]), stamp(entry["end"])
    query = {"mmsi": mmsi, "start": start, "end": end}
    win = {"window_id": window_id("kystdatahuset-ais", subject, start, end, query), "start": start, "end": end}
    ids = [{"scheme": "mmsi", "value": mmsi}]
    rows = body.get("data") if isinstance(body, Mapping) else None
    if rows is None:
        raise MovementFormatError("schema_drift", "AIS payload has no data array")
    points = []
    stated_imo = set()
    for index, row in enumerate(rows):
        if identifier_key("mmsi", row.get("mmsi")) != mmsi:
            continue  # another vessel's row is never stored
        at_stamp = stamp(row["msgtime"])
        if start <= at_stamp <= end and row.get("latitude") is not None and row.get("longitude") is not None:
            points.append((at_stamp, index, row))
        if identifier_key("imo", row.get("imo")):
            stated_imo.add(identifier_key("imo", row.get("imo")))
    points = sorted(dict(((p[0], p) for p in points)).values())
    kept, thinned = thin(points, BOUNDS["kystdatahuset-ais"]["max_samples"])
    out = [statement("position_sample", "kystdatahuset-ais", subject, f"sample:{at}",
                     {"timestamp": at, "lat": float(row["latitude"]), "lon": float(row["longitude"]),
                      "speed_over_ground_kn": row.get("speedOverGround"), "course_over_ground": row.get(
                          "courseOverGround"), "receiver_category": AIS_RECEIVERS},
                     identifiers=ids, source=_source(url, f"/data/{index}", origin), event="observed",
                     effective_from=at, window=win) for at, index, row in kept]
    declared = identifier_key("imo", entry.get("imo")) if entry.get("imo") else None
    for imo in sorted(stated_imo | ({declared} if declared else set())):
        out.append(statement(
            "vessel_identity", "kystdatahuset-ais", subject, f"imo:{imo}",
            {"mmsi": mmsi, "imo": imo, "basis": "AIS static data" if imo in stated_imo else
             "stated in the selection", "note": "MMSI and IMO as broadcast; MMSIs are reassigned, so the pair holds "
                                                "for this window only"},
            identifiers=[*ids, {"scheme": "imo", "value": imo, "valid_from": start, "valid_to": end}],
            source=_source(url, "#static", origin, query=query), event="observed", effective_from=start,
            effective_to=end, date_basis="requested window"))
    out.append(_window_statement("kystdatahuset-ais", subject, ids, win, query, url, origin, published=len(points),
                                 stored=[p[0] for p in kept], thinned=thinned, receiver=AIS_RECEIVERS))
    return out, {"outcome": "found" if points else "no_coverage", "thinned": thinned}


def parse_unctad(body: str, url: str, *, origin: str, entry: Mapping[str, Any]) -> tuple[list[dict], dict]:
    reader = csv.DictReader(io.StringIO(body.lstrip("﻿")))
    economies = {str(e) for e in entry["economies"]}
    years = {str(y) for y in entry["years"]}
    out = []
    for index, row in enumerate(reader):
        economy, period = str(row.get("Economy") or "").strip(), str(row.get("Period") or "").strip()
        if economy not in economies or period not in years:
            continue  # outside the declared selection: never stored
        market = str(row.get("CommercialMarket Label") or row.get("CommercialMarket") or "All ships").strip()
        out.append(statement(
            "aggregate", "unctad-port-calls", f"area:unctad-economy:{economy}",
            f"port-calls:{economy}:{period}:{market}",
            {"economy_code": economy, "economy_label": _text(row.get("Economy Label")), "period": period,
             "market_segment": market, "port_calls": _number(row.get("NumberOfPortCalls")),
             "median_time_in_port_days": _number(row.get("MedianTimeInPort_Days")),
             "note": "a published aggregate; never disaggregated into vessels or tracks"},
            source=_source(url, f"/row/{index + 2}", origin, release=entry.get("release")), event="release",
            effective_from=f"{period}-01-01", effective_to=f"{period}-12-31", date_basis="statistical year"))
    return out, {"outcome": "found" if out else "not_found"}


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "")) if str(value or "").strip() else None
    except ValueError:
        return None


# ------------------------------------------------------------------ runtime adapter


class MovementSourceAdapter:
    """One page per selected identifier and window on the runtime's default transport."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.provider, self.entries = selection_entries(self.source)
        self.declared = _declared(self.source)
        self.refusals = {(r["scheme"], identifier_key(r["scheme"], r["identifier"])): r
                         for r in self.declared.get("privacy_refusals") or []}
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
            "osint_movements": {"provider": self.provider, "selected": len(self.entries)},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "movement runs fetch the declared selection only")

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, Any, str, str, int]:
        base = self.source["endpoint"].rstrip("/") + path
        url = base + ("?" + urlencode(sorted(query.items())) if query else "")
        headers = {"Accept": "application/json, text/html, text/csv"}
        auth = dict(self.source.get("auth") or {}).get("kind")
        if auth == "required-secret" and not self._secret:
            raise SourcePackError("authentication_failed", f"the {self.provider} credential is not configured")
        if self._secret and auth != "none":
            if self.provider == "opensky":
                import base64

                headers["Authorization"] = "Basic " + base64.b64encode(self._secret.encode()).decode()
            else:
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
        if status == 404:
            return status, None, url, origin, len(raw)
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            folded = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
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
        if text.lstrip().startswith(("{", "[")):
            try:
                return status, json.loads(text), url, origin, len(raw)
            except json.JSONDecodeError as exc:
                raise SourcePackError("schema_drift", "response is not valid JSON") from exc
        return status, text, url, origin, len(raw)

    def _fetch(self, entry: Mapping[str, Any]) -> tuple[list[dict], dict[str, Any], str, int]:
        ((role, path, query),) = requests_for(self.provider, entry)
        status, body, url, origin, size = self._get(path, query)
        if self.provider == "opensky":
            tracks = []
            flights = body if isinstance(body, list) else []
            for flight in flights[:BOUNDS["opensky"]["max_flights"]]:
                if flight.get("firstSeen") is None:
                    continue
                track_query = {"icao24": query["icao24"], "time": str(int(flight["firstSeen"]))}
                _, track, track_url, _, track_size = self._get("/api/tracks/all", track_query)
                size += track_size
                if isinstance(track, Mapping):
                    tracks.append((track_url, track))
            statements, info = parse_opensky(flights, tracks, url, origin=origin, entry=entry)
            return statements, info, origin, size
        if body is None:
            if self.provider in {"gfw-port-visits", "kystdatahuset-ais"}:
                body = {"entries": []} if self.provider == "gfw-port-visits" else {"data": []}
            else:
                return [], {"outcome": "not_found"}, origin, size
        parsers = {"faa-registry": parse_faa, "uk-caa-ginfo": parse_ginfo, "gfw-port-visits": parse_gfw_events,
                   "kystdatahuset-ais": parse_ais, "unctad-port-calls": parse_unctad}
        if self.provider in {"faa-registry", "unctad-port-calls"} and not isinstance(body, str):
            raise MovementFormatError("schema_drift", f"{self.provider} page is not a text document")
        if self.provider in {"uk-caa-ginfo", "gfw-port-visits", "kystdatahuset-ais"} and not isinstance(body, Mapping):
            raise MovementFormatError("schema_drift", f"{self.provider} page is not a JSON object")
        statements, info = parsers[self.provider](body, url, origin=origin, entry=entry)
        return statements, info, origin, size

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(self.entries):
            raise SourcePackError("cursor_drift", "cursor names no declared selection")
        entry = self.entries[index]
        label = {k: entry[k] for k in sorted(entry) if k not in {"economies", "years"}}
        refusals = [dict(r) for r in self.declared.get("privacy_refusals") or []] if index == 0 else []
        named = entry_identifier(self.provider, entry)
        statements: list[dict] = []
        size, origin = 0, "fixture"
        if named and named in self.refusals:
            info = {"outcome": "privacy_refused", "programme": self.refusals[named]["programme"]}
        else:
            try:
                statements, info, origin, size = self._fetch(entry)
            except (MovementFormatError, MovementError, KeyError, TypeError, ValueError) as exc:
                raise SourcePackError("schema_drift", f"{getattr(exc, 'code', 'parse')}: {exc}") from exc
        if info.get("refusal"):
            refusals.append(info["refusal"])
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            raise SourcePackError("budget_exhausted", "selection has more statements than the run's result budget")
        records = []
        for item in statements:
            content = json.dumps(item, sort_keys=True, ensure_ascii=False)
            records.append({
                "id": f"{item['subject']['key']}|{item['record_type']}|{item['record_key']}|"
                      + hashlib.sha256(content.encode()).hexdigest()[:12],
                "title": f"{item['subject']['key']}: {item['record_type']}",
                "url": item["source"].get("url"), "language": "en", "content": content, "movement_record": item})
        receipt = {"status": 200, "provider": self.provider, "selection": label, "outcome": info["outcome"],
                   "statements": len(records), "excluded_fields_dropped": info.get("dropped", []),
                   "thinned": bool(info.get("thinned")), "evidence_origin": origin,
                   "privacy_refusals": refusals, "final_page": index + 1 >= len(self.entries),
                   "selection_key": selection_key(entry)}
        next_cursor = str(index + 1) if index + 1 < len(self.entries) else None
        return RuntimePage(tuple(records), next_cursor, size, receipt=receipt)


FIXTURE_SECRET = "fixture-credential"
ADAPTERS = {CONNECTOR: MovementSourceAdapter}


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
    adapter = MovementSourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                    secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


def source_contracts() -> dict[str, Any]:
    """The MV01 decisions as data: adopted providers with bounds and live status, and excluded sources."""
    return {
        "contract": "noesis-osint-movement-source-contracts-v1",
        "decision_document": "docs/security/osint-movements-access.md",
        "providers": {p: {**PROVIDER_CONTRACTS[p], "bounds": {k: v for k, v in BOUNDS[p].items() if k != "anchor"},
                          "live_verification": LIVE_VERIFICATION[p]} for p in PROVIDERS},
        "excluded": dict(EXCLUDED_SOURCES),
        "answer_bounds": {"max_window_days": BOUNDS["answer"]["max_window_days"],
                          "monitor_max_identifiers": BOUNDS["monitor"]["max_identifiers"]},
        "rejected": "continuous bulk ADS-B or AIS mirroring, area-wide or background polling, real-time tracking",
        "coverage_caveat": COVERAGE_CAVEAT,
    }


__all__ = [
    "ADAPTERS", "CONNECTOR", "EXCLUDED_FIELDS", "EXCLUDED_SOURCES", "FIXTURE_SECRET", "LIVE_VERIFICATION",
    "MovementFormatError", "MovementSourceAdapter", "PROVIDERS", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS",
    "fixture_transport", "parse_ais", "parse_faa", "parse_gfw_events", "parse_ginfo", "parse_opensky",
    "parse_unctad", "replay_native_fixture", "requests_for", "selection_entries", "selection_key",
    "source_contracts",
]
