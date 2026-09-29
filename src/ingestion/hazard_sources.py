"""Natural Hazards acquisition: USGS, EMSC, GDACS, NHC, EFFIS and GloFAS adapters (NH01, NH03-NH07).

One source-pack connector (``natural-hazards``) fetches each declared document
of a source (``natural_hazards.documents`` in
``config/source_packs/natural-hazards.json``) from the source's endpoint host,
parses it with the fail-closed parser for its declared format and emits
``noesis-hazard-record-v1`` records that
:class:`~src.kb.hazards_store.HazardProjector` appends as revisions:

* ``usgs-fdsn-geojson`` / ``usgs-detail-geojson`` (NH03) — FDSN event
  summaries and event details. Magnitude type and value, location, depth and
  review status per ``updated``; associated network IDs kept; deleted events
  recorded with ``status=deleted``; a requested ID that is now secondary is
  recorded as ``merged`` into the preferred event; PAGER (``losspager``)
  versions become impact estimates citing ``code`` + ``updateTime``.
* ``emsc-fdsn-json`` (NH04) — EMSC events keyed by ``unid``; magnitude,
  location, depth and authoring agency per ``lastupdate``. Never merged with
  USGS. Felt reports are not requested.
* ``gdacs-geojson`` (NH05) — GDACS events keyed by type + event ID; every
  episode is an event revision, an ``alert`` (level and severity text as
  published) and an episode alert-score ``impact_estimate``; GLIDE kept.
* ``nhc-tcm`` / ``nhc-tcp`` (NH06) — NHC forecast/advisory and public
  (including intermediate) advisory text products, each an ``advisory``
  exactly as issued plus a revision of the storm's ``hazard_event``. The
  forecast track is the NHC's; the cone is referenced by locator only.
* ``effis-burnt-area-geojson`` (NH07) — EFFIS burnt-area polygons keyed by
  EFFIS id with the published area estimate per ``LASTUPDATE``.
* ``glofas-notification-json`` (NH07) — operator-provided GloFAS
  notifications (key-gated source, disabled by default) as modelled alerts.

Numbers are kept as the published text (JSON floats are read as their
literal text), nothing is converted, averaged or recomputed, and a document
that does not parse is rejected (``schema_drift``), never guessed. Every
provider is ``unverified-live`` until a dated live run; see
``docs/development/hazards-evidence/source-audit.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb import hazards_records as hr

CONNECTOR = "natural-hazards"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
EXTRACTOR = "natural-hazards:1.0.0"
PROVIDER_HOSTS = {
    "usgs": ("earthquake.usgs.gov",),
    "emsc": ("www.seismicportal.eu",),
    "gdacs": ("www.gdacs.org",),
    "nhc": ("www.nhc.noaa.gov",),
    "effis": ("maps.effis.emergency.copernicus.eu",),
    "glofas": ("www.globalfloods.eu",),
}
FORMATS = {
    "usgs": ("usgs-fdsn-geojson", "usgs-detail-geojson"),
    "emsc": ("emsc-fdsn-json",),
    "gdacs": ("gdacs-geojson",),
    "nhc": ("nhc-tcm", "nhc-tcp"),
    "effis": ("effis-burnt-area-geojson",),
    "glofas": ("glofas-notification-json",),
}
PROVIDER_CONTRACTS = {
    "usgs": {"access": "public FDSN event service (GeoJSON) and ComCat event detail products",
             "licence": "U.S. public domain; credit 'U.S. Geological Survey' requested",
             "terms_url": "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
             "rate_limit": "no published quota; at most 20,000 events per query (verify)",
             "revision_marker": "event 'updated' (epoch ms); PAGER product 'code' + 'updateTime'",
             "access_decision": "unverified-live"},
    "emsc": {"access": "public EMSC FDSN event service (format=json)",
             "licence": "reuse with acknowledgement of EMSC-CSEM and contributing agencies; commercial redistribution by "
                        "agreement (verify)",
             "terms_url": "https://www.emsc-csem.org/", "rate_limit": "none published (verify)",
             "revision_marker": "'lastupdate' per 'unid'", "access_decision": "unverified-live",
             "excluded": "felt reports and testimonies (personal data)"},
    "gdacs": {"access": "public GDACS event API (geteventlist, GeoJSON)",
              "licence": "use with attribution 'GDACS' (verify)",
              "terms_url": "https://www.gdacs.org/About/termsofuse.aspx", "rate_limit": "none published (verify)",
              "revision_marker": "'episodeid' (+ 'datemodified')", "access_decision": "unverified-live"},
    "nhc": {"access": "public NHC forecast/advisory (TCM) and public advisory (TCP) text products",
            "licence": "U.S. government work, public domain; credit 'NOAA/NHC'",
            "terms_url": "https://www.weather.gov/disclaimer", "rate_limit": "none published (verify)",
            "revision_marker": "storm ID + advisory number (intermediate advisories keep their letter)",
            "access_decision": "unverified-live"},
    "effis": {"access": "EFFIS OGC services, burnt-area polygons (GeoJSON)",
              "licence": "Copernicus EMS free and open data with attribution (verify)",
              "terms_url": "https://effis.jrc.ec.europa.eu/about-effis/data-license", "rate_limit": "none published (verify)",
              "revision_marker": "'LASTUPDATE' per burnt-area id", "access_decision": "unverified-live",
              "excluded": "active-fire hotspots (NASA FIRMS detections under NASA terms; not implemented)"},
    "glofas": {"access": "registered-partner notifications / EWDS under the CEMS licence; operator-provided export",
               "licence": "CEMS-FLOODS licence; no mirroring of forecast layers",
               "terms_url": "https://www.globalfloods.eu/", "rate_limit": "per account (verify)",
               "revision_marker": "notification issue time", "access_decision": "key-gated",
               "secret_ref": "NOESIS_GLOFAS_TOKEN"},
}
LIVE_VERIFICATION = {
    provider: {"status": "key-gated" if contract["access_decision"] == "key-gated" else "unverified-live",
               "note": "no dated live run from this runtime; offline authored fixtures only"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Declared bounded coverage (NH01). A place outside every box is "source not covered".
BOUNDED_COVERAGE = {
    "usgs": {"bboxes": [[19, 34, 45, 43]], "hazard_types": ["earthquake"], "min_magnitude": "4.5",
             "window_days": 30, "record_cap": 200},
    "emsc": {"bboxes": [[19, 34, 45, 43]], "hazard_types": ["earthquake"], "min_magnitude": "4.5",
             "window_days": 30, "record_cap": 200},
    "gdacs": {"bboxes": [[19, 34, 45, 43], [-100, 5, -10, 45]], "hazard_types": list(hr.HAZARD_TYPES),
              "window_days": 30, "record_cap": 100},
    "nhc": {"bboxes": [[-100, 5, -10, 45]], "hazard_types": ["tropical_cyclone"], "window_days": None,
            "record_cap": 600},
    "effis": {"bboxes": [[-10, 34, 45, 46]], "hazard_types": ["wildfire"], "window_days": None, "record_cap": 500},
    "glofas": {"bboxes": [[19, 34, 45, 43]], "hazard_types": ["flood"], "window_days": 30, "record_cap": 50,
               "enabled_by_default": False},
}
_GDACS_TYPES = {"EQ": "earthquake", "TC": "tropical_cyclone", "FL": "flood", "VO": "volcano", "DR": "drought",
                "WF": "wildfire"}


class HazardFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: str = "unparseable") -> None:
    raise HazardFormatError(code, message)


def _json(raw: bytes) -> Any:
    """JSON with numbers kept as their literal text so nothing is rounded."""

    try:
        return json.loads(raw.decode("utf-8"), parse_float=lambda s: s, parse_int=lambda s: s)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HazardFormatError("unparseable", f"not JSON: {exc}") from exc


def _num(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _iso_ms(value: Any) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(str(value)) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _utc(value: Any) -> str | None:
    """Timestamps published without an offset are stated in UTC by the provider (recorded in the locator)."""

    if value in (None, ""):
        return None
    text = str(value).strip().replace(" ", "T")
    if len(text) == 10:
        return text
    if text.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", text):
        return text
    return text + "Z"


def _point(lon: Any, lat: Any) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [float(str(lon)), float(str(lat))]}


def _floats(value: Any) -> Any:
    if isinstance(value, list):
        return [_floats(v) for v in value]
    return float(str(value))


def _features(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping) and payload.get("type") == "FeatureCollection":
        return list(payload.get("features") or [])
    if isinstance(payload, Mapping) and payload.get("type") == "Feature":
        return [payload]
    _fail("expected a GeoJSON FeatureCollection or Feature")
    return []


# ------------------------------------------------------------------------ USGS (NH03)


def _usgs_event(feature: Mapping[str, Any], url: str) -> dict[str, Any]:
    props = dict(feature.get("properties") or {})
    event_id = str(feature.get("id") or "")
    if not event_id or props.get("updated") is None:
        _fail("USGS feature needs id and updated")
    coords = list((feature.get("geometry") or {}).get("coordinates") or [])
    geometry = _point(coords[0], coords[1]) if len(coords) >= 2 else None
    ids = [i for i in str(props.get("ids") or "").split(",") if i]
    parameters = []
    if props.get("mag") is not None:
        parameters.append(hr.parameter("magnitude", _num(props["mag"]), "magnitude", qualifier=props.get("magType")))
    if len(coords) >= 3 and coords[2] is not None:
        parameters.append(hr.parameter("depth", _num(coords[2]), "km"))
    unknowns = [name for name, present in (("magnitude", props.get("mag") is not None),
                                            ("depth", len(coords) >= 3), ("location", geometry is not None)) if not present]
    return hr.event(
        "usgs", event_id, str(props.get("title") or f"USGS event {event_id}"), hazard_type="earthquake",
        source_url=str(props.get("url") or f"https://earthquake.usgs.gov/earthquakes/eventpage/{event_id}"),
        revision_key=str(props["updated"]), published_at=_iso_ms(props["updated"]), event_time=_iso_ms(props.get("time")),
        parameters=parameters, geometry=geometry, geometry_role="preferred origin (epicentre) as published",
        status=props.get("status"), status_scheme="USGS review status (automatic/reviewed/deleted)",
        identifiers={"ids": ids, "net": props.get("net"), "code": props.get("code"),
                     "sources": [s for s in str(props.get("sources") or "").split(",") if s]},
        locator={"document": url, "place_text": props.get("place"), "detail": props.get("detail")},
        unknowns=unknowns)


def _pager(feature: Mapping[str, Any], event_id: str, url: str) -> list[dict[str, Any]]:
    products = dict((feature.get("properties") or {}).get("products") or {})
    records = []
    for product in products.get("losspager") or []:
        update_time = product.get("updateTime")
        props = dict(product.get("properties") or {})
        if update_time is None or not props.get("alertlevel"):
            _fail("PAGER product needs updateTime and alertlevel")
        estimate = {"alert_level": props.get("alertlevel")}
        if props.get("maxmmi") is not None:
            estimate["max_mmi"] = _num(props.get("maxmmi"))
        version = props.get("version") or str(update_time)
        records.append(hr.validate({
            "contract": hr.CONTRACT, "record_type": "impact_estimate", "provider": "usgs", "hazard_type": "earthquake",
            "native_id": f"{event_id}:losspager:{product.get('source')}{product.get('code')}",
            "title": f"USGS PAGER for {event_id}", "source_url": f"https://earthquake.usgs.gov/earthquakes/eventpage/{event_id}/pager",
            "revision_key": f"{product.get('code')}:{update_time}", "published_at": _iso_ms(update_time),
            "event_native_id": event_id, "product": "losspager", "product_version": str(version),
            "estimate": estimate, "status": str(product.get("status") or "published").lower(),
            "locator": {"document": url, "product_code": product.get("code"), "product_source": product.get("source"),
                        "version_basis": "PAGER 'version' property" if props.get("version") else "ComCat product updateTime"},
        }))
    return records


def parse_usgs(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    payload = _json(raw)
    records, excluded = [], []
    requested = document.get("requested_id")
    for feature in _features(payload):
        props = dict(feature.get("properties") or {})
        if props.get("type") not in (None, "earthquake"):
            excluded.append({"id": feature.get("id"), "type": props.get("type"), "reason": "not an earthquake"})
            continue
        event = _usgs_event(feature, url)
        records.append(event)
        if document.get("format") == "usgs-detail-geojson":
            records += _pager(feature, event["native_id"], url)
            if requested and requested != event["native_id"]:
                if requested not in event["identifiers"]["ids"]:
                    _fail(f"detail for {requested} returned unrelated event {event['native_id']}")
                records.append(hr.event(
                    "usgs", requested, f"USGS event {requested} (merged into {event['native_id']})",
                    hazard_type="earthquake", source_url=f"https://earthquake.usgs.gov/earthquakes/eventpage/{requested}",
                    revision_key=f"merged:{event['revision_key']}", published_at=event["published_at"],
                    event_time=None, parameters=[], status="merged", merged_into=event["native_id"],
                    status_scheme="Noesis record status: the requested ID is now a secondary ID of the preferred event",
                    locator={"document": url, "basis": "listed in the preferred event's ids"}, unknowns=["parameters"]))
    return {"records": records, "excluded": excluded}


# ------------------------------------------------------------------------ EMSC (NH04)


def parse_emsc(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    del document
    records = []
    for feature in _features(_json(raw)):
        props = dict(feature.get("properties") or {})
        unid = props.get("unid") or feature.get("id")
        if not unid or not props.get("lastupdate") or props.get("lat") is None or props.get("lon") is None:
            _fail("EMSC feature needs unid, lastupdate, lat and lon")
        parameters = []
        if props.get("mag") is not None:
            parameters.append(hr.parameter("magnitude", _num(props["mag"]), "magnitude", qualifier=props.get("magtype")))
        if props.get("depth") is not None:
            parameters.append(hr.parameter("depth", _num(props["depth"]), "km"))
        if props.get("auth"):
            parameters.append(hr.parameter("author", str(props["auth"]), None, kind="text"))
        records.append(hr.event(
            "emsc", str(unid), f"EMSC {props.get('flynn_region') or 'event'} {unid}", hazard_type="earthquake",
            source_url=f"https://www.seismicportal.eu/eventdetails.html?unid={unid}",
            revision_key=str(props["lastupdate"]), published_at=_utc(props["lastupdate"]), event_time=_utc(props.get("time")),
            parameters=parameters, geometry=_point(props["lon"], props["lat"]),
            geometry_role="EMSC epicentre as published", status=props.get("evtype"),
            status_scheme="EMSC event type code as published",
            identifiers={"unid": str(unid), "source_id": props.get("source_id"),
                         "source_catalog": props.get("source_catalog")},
            locator={"document": url, "region": props.get("flynn_region")},
            unknowns=[] if props.get("mag") is not None else ["magnitude"]))
    return {"records": records, "excluded": []}


# ------------------------------------------------------------------------ GDACS (NH05)


def parse_gdacs(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    del document
    records, excluded = [], []
    for feature in _features(_json(raw)):
        props = dict(feature.get("properties") or {})
        kind = props.get("eventtype")
        if kind not in _GDACS_TYPES:
            excluded.append({"eventtype": kind, "reason": "event type outside the declared hazard types"})
            continue
        event_id, episode = str(props.get("eventid") or ""), str(props.get("episodeid") or "")
        modified = _utc(props.get("datemodified"))
        if not event_id or not episode or not modified:
            _fail("GDACS feature needs eventid, episodeid and datemodified")
        hazard = _GDACS_TYPES[kind]
        native = f"{kind}{event_id}"
        report = str(dict(props.get("url") or {}).get("report")
                     or f"https://www.gdacs.org/report.aspx?eventid={event_id}&episodeid={episode}&eventtype={kind}")
        severity = dict(props.get("severitydata") or {})
        countries = [c.get("iso3") for c in props.get("affectedcountries") or [] if c.get("iso3")] or (
            [props["iso3"]] if props.get("iso3") else [])
        geometry = None
        raw_geometry = feature.get("geometry") or {}
        if raw_geometry.get("type") == "Point":
            coords = raw_geometry.get("coordinates") or []
            geometry = _point(coords[0], coords[1])
        elif raw_geometry.get("type") in {"Polygon", "MultiPolygon"}:
            geometry = {"type": raw_geometry["type"], "coordinates": _floats(raw_geometry["coordinates"])}
        identifiers = {"eventtype": kind, "eventid": event_id, "glide": props.get("glide") or None,
                       "source": props.get("source"), "sourceid": props.get("sourceid")}
        locator = {"document": url, "time_zone": "UTC (GDACS publishes times without an offset; verify)",
                   "polygon_label": props.get("polygonlabel")}
        parameters = []
        if severity.get("severity") is not None:
            parameters.append(hr.parameter("severity", _num(severity["severity"]), severity.get("severityunit") or None))
        if severity.get("severitytext"):
            parameters.append(hr.parameter("severity_text", severity["severitytext"], None, kind="text"))
        records.append(hr.event(
            "gdacs", native, str(props.get("name") or props.get("eventname") or native), hazard_type=hazard,
            source_url=report, revision_key=f"episode:{episode}", published_at=modified,
            event_time=_utc(props.get("fromdate")), event_end=_utc(props.get("todate")), parameters=parameters,
            geometry=geometry, geometry_role=f"GDACS {props.get('polygonlabel') or 'geometry'} as published",
            status="current" if str(props.get("iscurrent")).lower() == "true" else "not current",
            status_scheme="GDACS iscurrent flag", identifiers=identifiers, countries=countries, episode_id=episode,
            locator=locator))
        records.append(hr.validate({
            "contract": hr.CONTRACT, "record_type": "alert", "provider": "gdacs", "hazard_type": hazard,
            "native_id": f"{native}:{episode}", "title": f"GDACS {props.get('episodealertlevel') or props.get('alertlevel')} "
                                                         f"alert: {props.get('name') or native} (episode {episode})",
            "source_url": report, "revision_key": f"episode:{episode}", "published_at": modified,
            "event_native_id": native, "level": str(props.get("episodealertlevel") or props.get("alertlevel")),
            "level_scheme": "GDACS alert level (Green/Orange/Red)", "wording": severity.get("severitytext"),
            "episode_id": episode, "issued_at": modified, "valid_from": None, "valid_to": None,
            "validity_basis": "GDACS publishes no expiry; an episode alert stands until GDACS issues the next episode",
            "geometry": geometry, "countries": countries, "identifiers": identifiers, "locator": locator}))
        score = props.get("episodealertscore", props.get("alertscore"))
        if score is not None:
            records.append(hr.validate({
                "contract": hr.CONTRACT, "record_type": "impact_estimate", "provider": "gdacs", "hazard_type": hazard,
                "native_id": f"{native}:alertscore", "title": f"GDACS alert score for {native}", "source_url": report,
                "revision_key": f"episode:{episode}", "published_at": modified, "event_native_id": native,
                "product": "gdacs-episode-alert-score", "product_version": f"episode {episode}",
                "estimate": {"episode_alert_score": _num(score), "episode_alert_level": props.get("episodealertlevel"),
                             "event_alert_score": _num(props.get("alertscore")), "event_alert_level": props.get("alertlevel")},
                "identifiers": identifiers, "locator": locator}))
    return {"records": records, "excluded": excluded}


# ------------------------------------------------------------------------ NHC (NH06)

_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}
_TZ = {"UTC": 0, "GMT": 0, "AST": 4, "EDT": 4, "EST": 5, "CDT": 5, "CST": 6, "MDT": 6, "MST": 7, "PDT": 7, "PST": 8,
       "HST": 10}
_STORM_ID = re.compile(r"\b((?:AL|EP|CP)\d{2}\d{4})\b")
_LATLON = r"(\d{1,2}\.\d)([NS])\s+(\d{1,3}\.\d)([EW])"


def _coord(lat, ns, lon, ew):
    return {"type": "Point", "coordinates": [float(lon) * (-1 if ew == "W" else 1), float(lat) * (-1 if ns == "S" else 1)]}


def _day_time(issued: datetime, day: str, hhmm: str) -> str:
    """A DD/HHMMZ stamp relative to the issue time (the month rolls over when the day is earlier)."""

    moment = issued.replace(day=1, hour=int(hhmm[:2]), minute=int(hhmm[2:]), second=0)
    if int(day) < issued.day:
        moment = (moment + timedelta(days=32)).replace(day=1)
    return moment.replace(day=int(day)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _warnings(text: str) -> list[dict[str, Any]]:
    section = re.split(r"SUMMARY OF WATCHES AND WARNINGS IN EFFECT[.:]*", text, flags=re.I)
    if len(section) < 2:
        return []
    body = re.split(r"\n\s*\n(?=[A-Z][A-Z ]*(?:LOCATED|DISCUSSION|INTERESTS|FOR STORM)|\$\$)", section[1])[0]
    result = []
    for match in re.finditer(r"An? ([A-Za-z ]+?) (?:is|are) in effect for\.\.\.\s*\n((?:\s*\*.+\n?)+)", body, flags=re.I):
        for line in match.group(2).splitlines():
            area = line.strip().lstrip("*").strip()
            if area:
                result.append({"type": match.group(1).strip().title(), "area_text": area, "action": "in effect"})
    return result


def parse_nhc(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    text = raw.decode("utf-8", errors="strict").replace("\r\n", "\n")
    fmt = document.get("format")
    storm = _STORM_ID.search(text)
    if not storm:
        _fail("NHC product names no storm ID")
    storm_id = storm.group(1)
    if fmt == "nhc-tcm":
        head = re.search(r"^\s*([A-Z][A-Z \-]+?) ([A-Z][A-Z\-]+) FORECAST/ADVISORY NUMBER\s+(\d{1,3}[A-Z]?)\s*$", text, re.M)
        issued_line = re.search(r"^\s*(\d{4}) UTC \w{3} (\w{3}) (\d{2}) (\d{4})\s*$", text, re.M)
        if not head or not issued_line:
            _fail("TCM header or issue line missing")
        stage, name, number = head.group(1).strip(), head.group(2), head.group(3)
        hhmm, month, day, year = issued_line.groups()
        issued = datetime(int(year), _MONTHS[month.upper()], int(day), int(hhmm[:2]), int(hhmm[2:]), tzinfo=timezone.utc)
        center = re.search(r"CENTER LOCATED NEAR\s+" + _LATLON + r" AT (\d{2})/(\d{4})Z", text)
        pressure = re.search(r"ESTIMATED MINIMUM CENTRAL PRESSURE\s+(\d+) MB", text)
        winds = re.search(r"MAX SUSTAINED WINDS\s+(\d+) KT WITH GUSTS TO\s+(\d+) KT", text)
        if not center or not pressure or not winds:
            _fail("TCM centre, pressure or winds missing")
        position = _coord(*center.groups()[:4])
        parameters = [hr.parameter("max_sustained_wind", winds.group(1), "kt"),
                      hr.parameter("gusts", winds.group(2), "kt"),
                      hr.parameter("min_central_pressure", pressure.group(1), "mb")]
        movement = re.search(r"PRESENT MOVEMENT TOWARD (.+)", text)
        if movement:
            parameters.append(hr.parameter("movement", movement.group(1).strip(), None, kind="text"))
        track = []
        for match in re.finditer(r"(FORECAST|OUTLOOK) VALID (\d{2})/(\d{4})Z\s+" + _LATLON + r"(?:\s+(.*))?\n"
                                 r"MAX WIND\s+(\d+) KT\.\.\.GUSTS\s+(\d+) KT", text):
            track.append({"valid_at": _day_time(issued, match.group(2), match.group(3)),
                          "geometry": _coord(*match.groups()[3:7]), "label": match.group(1).lower(),
                          "stage": (match.group(8) or "").strip() or None,
                          "max_wind": {"value": match.group(9), "unit": "kt", "gusts": match.group(10)}})
        kind = "forecast/advisory (TCM)"
    elif fmt == "nhc-tcp":
        head = re.search(r"^\s*([A-Za-z][A-Za-z \-]+?) ([A-Za-z\-]+) (?:(Intermediate|Special) )?Advisory Number\s+(\d{1,3}[A-Z]?)\s*$",
                         text, re.M | re.I)
        local = re.search(r"^\s*(\d{3,4}) (AM|PM) ([A-Z]{3}) \w{3} (\w{3}) (\d{2}) (\d{4})\s*$", text, re.M)
        summary = re.search(r"SUMMARY OF .*?\.\.\.(\d{4}) UTC\.\.\.INFORMATION", text)
        if not head or not local or not summary:
            _fail("TCP header, issue line or UTC summary missing")
        stage, name, qualifier, number = head.group(1).strip().upper(), head.group(2).upper(), head.group(3), head.group(4)
        hhmm, meridiem, zone, month, day, year = local.groups()
        hour = int(hhmm[:-2]) % 12 + (12 if meridiem.upper() == "PM" else 0)
        if zone.upper() not in _TZ:
            _fail(f"unknown time zone {zone}")
        issued = (datetime(int(year), _MONTHS[month.upper()], int(day), hour, int(hhmm[-2:]), tzinfo=timezone.utc)
                  + timedelta(hours=_TZ[zone.upper()]))
        if issued.strftime("%H%M") != summary.group(1):
            _fail("local issue time and UTC summary disagree")
        location = re.search(r"LOCATION\.\.\." + _LATLON, text)
        winds = re.search(r"MAXIMUM SUSTAINED WINDS\.\.\.(\d+) MPH\.\.\.(\d+) KM/H", text)
        pressure = re.search(r"MINIMUM CENTRAL PRESSURE\.\.\.(\d+) MB", text)
        if not location or not winds or not pressure:
            _fail("TCP location, winds or pressure missing")
        position = _coord(*location.groups())
        parameters = [hr.parameter("max_sustained_wind", winds.group(1), "mph", as_published=f"{winds.group(1)} MPH...{winds.group(2)} KM/H"),
                      hr.parameter("min_central_pressure", pressure.group(1), "mb")]
        movement = re.search(r"PRESENT MOVEMENT\.\.\.(.+)", text)
        if movement:
            parameters.append(hr.parameter("movement", movement.group(1).strip(), None, kind="text"))
        track = []
        kind = f"{(qualifier or '').lower() + ' ' if qualifier else ''}public advisory (TCP)"
    else:
        _fail(f"unknown NHC format {fmt}")
    issued_at = issued.strftime("%Y-%m-%dT%H:%M:%SZ")
    title = f"{stage.title()} {name.title()} advisory {number}"
    advisory = hr.validate({
        "contract": hr.CONTRACT, "record_type": "advisory", "provider": "nhc", "hazard_type": "tropical_cyclone",
        "native_id": f"{storm_id}:{number}", "title": title, "source_url": url, "revision_key": f"advisory:{number}",
        "published_at": issued_at, "issued_at": issued_at, "storm_id": storm_id, "storm_name": name.title(),
        "advisory_number": number, "advisory_kind": kind, "event_native_id": storm_id, "parameters": parameters,
        "geometry": position, "geometry_role": "storm centre as issued", "forecast_track": track,
        "cone": {"locator": document["cone_url"], "mirrored": False} if document.get("cone_url") else None,
        "watches_warnings": _warnings(text), "identifiers": {"storm_id": storm_id, "advisory_number": number},
        "locator": {"document": url, "product": "TCM" if fmt == "nhc-tcm" else "TCP"}})
    storm_event = hr.event(
        "nhc", storm_id, f"{stage.title()} {name.title()} ({storm_id})", hazard_type="tropical_cyclone",
        source_url=url, revision_key=f"advisory:{number}", published_at=issued_at, event_time=None,
        parameters=[*parameters, hr.parameter("stage", stage.title(), None, kind="text")],
        geometry=position, geometry_role="storm centre at the advisory's issue", status="advisory issued",
        status_scheme="revised by each NHC advisory", identifiers={"storm_id": storm_id, "name": name.title()},
        locator={"document": url, "advisory_number": number}, unknowns=["event_time"])
    return {"records": [storm_event, advisory], "excluded": []}


# ------------------------------------------------------------------------ EFFIS (NH07)


def parse_effis(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    del document
    records = []
    for feature in _features(_json(raw)):
        props = dict(feature.get("properties") or {})
        native = props.get("id") or feature.get("id")
        if native is None or not props.get("LASTUPDATE") or not feature.get("geometry"):
            _fail("EFFIS burnt area needs id, LASTUPDATE and geometry")
        geometry = feature["geometry"]
        geometry = {"type": geometry["type"], "coordinates": _floats(geometry["coordinates"])}
        area = _num(props.get("AREA_HA"))
        places = ", ".join(str(props[k]) for k in ("COMMUNE", "PROVINCE") if props.get(k))
        records.append(hr.event(
            "effis", str(native), f"EFFIS burnt area {native}{' - ' + places if places else ''}", hazard_type="wildfire",
            source_url=f"https://effis.jrc.ec.europa.eu/apps/effis.statistics/?burntarea={native}",
            revision_key=str(props["LASTUPDATE"]), published_at=_utc(props["LASTUPDATE"]), event_time=_utc(props.get("FIREDATE")),
            parameters=[hr.parameter("burnt_area", area, "ha", qualifier="EFFIS mapped estimate"),
                        hr.parameter("mapping_date", str(props["LASTUPDATE"]), None, kind="text")],
            geometry=geometry, geometry_role="EFFIS mapped burnt-area polygon (satellite-derived estimate)",
            status="mapped", status_scheme="EFFIS burnt-area mapping", countries=[props["COUNTRY"]] if props.get("COUNTRY") else [],
            identifiers={"effis_id": str(native)},
            locator={"document": url, "province": props.get("PROVINCE"), "commune": props.get("COMMUNE")},
            unknowns=[] if area is not None else ["burnt_area"]))
    return {"records": records, "excluded": []}


# ------------------------------------------------------------------------ GloFAS (NH07)


def parse_glofas(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    del document
    payload = _json(raw)
    records = []
    for item in (payload or {}).get("notifications") or []:
        station = dict(item.get("station") or {})
        if not item.get("id") or not item.get("issued_at") or station.get("lon") is None:
            _fail("GloFAS notification needs id, issued_at and a station point")
        threshold = dict(item.get("threshold") or {})
        records.append(hr.validate({
            "contract": hr.CONTRACT, "record_type": "alert", "provider": "glofas", "hazard_type": "flood",
            "native_id": str(item["id"]), "title": f"GloFAS {item.get('type') or 'notification'} {item['id']} "
                                                   f"({item.get('river') or 'river point'})",
            "source_url": url, "revision_key": str(item["issued_at"]), "published_at": _utc(item["issued_at"]),
            "level": str(item.get("type") or "notification"), "level_scheme": "GloFAS notification type as issued",
            "wording": item.get("wording"), "issued_at": _utc(item["issued_at"]), "valid_from": _utc(item.get("valid_from")),
            "valid_to": _utc(item.get("valid_to")),
            "thresholds": [{"return_period_years": _num(threshold.get("return_period_years")),
                            "exceedance_probability_percent": _num(threshold.get("exceedance_probability_percent")),
                            "basis": "as issued by GloFAS"}] if threshold else [],
            "modelled": True, "model": {k: _num(v) if k == "version" else v for k, v in dict(item.get("model") or {}).items()},
            "geometry": _point(station["lon"], station["lat"]), "geometry_role": "GloFAS reporting point as issued",
            "countries": list(item.get("countries") or []), "identifiers": {"notification_id": str(item["id"]),
                                                                          "river": item.get("river")},
            "status": item.get("status") or "issued", "locator": {"document": url}}))
    return {"records": records, "excluded": []}


PARSERS: dict[str, Callable[..., dict[str, Any]]] = {
    "usgs-fdsn-geojson": parse_usgs, "usgs-detail-geojson": parse_usgs, "emsc-fdsn-json": parse_emsc,
    "gdacs-geojson": parse_gdacs, "nhc-tcm": parse_nhc, "nhc-tcp": parse_nhc,
    "effis-burnt-area-geojson": parse_effis, "glofas-notification-json": parse_glofas,
}


# ------------------------------------------------------------------------ runtime adapter


def hazards_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("natural_hazards") or {})
    provider = declared.get("provider")
    if provider not in PROVIDER_HOSTS:
        raise SourcePackError("invalid_source", "natural_hazards.provider is not a known hazard provider")
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_source", "natural_hazards.documents must declare at least one document")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("network_policy", f"{provider} is fetched from {PROVIDER_HOSTS[provider]} only")
    for document in documents:
        if document.get("format") not in FORMATS[provider]:
            raise SourcePackError("invalid_source", f"{provider} documents use one of {FORMATS[provider]}")
        if (urlsplit(str(document.get("url") or "")).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "declared documents are fetched from the endpoint's host only")
    return {"provider": provider, "documents": documents,
            "namespace": str(declared.get("namespace") or "hazards")}


class HazardAdapter:
    """One page per declared document on the runtime's transport; records are hazard revisions."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = hazards_declaration(self.source)
        self.secret = secret
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source.get("source_hash"), "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "natural_hazards": {"provider": self.declared["provider"],
                                "formats": sorted({d["format"] for d in self.declared["documents"]})},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "hazard runs fetch the declared documents only")

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        documents = self.declared["documents"]
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        url = str(document["url"])
        auth = dict(self.source.get("auth") or {})
        headers = {"Accept": "application/json, application/geo+json, text/plain"}
        if auth.get("kind") == "required-secret":
            if not self.secret:
                raise SourcePackError("authentication_failed", f"{auth.get('secret_ref')} is not configured")
            headers["Authorization"] = f"Bearer {self.secret}"
        base, _, query = url.partition("?")
        response = self.transport(url=base, params=dict(parse_qsl(query)), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "document was served from another host")
        status = int(response.get("status", 200))
        response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "document exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(response_headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"download refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"document returned HTTP {status}")
        try:
            parsed = PARSERS[document["format"]](raw, document=document, url=url)
        except HazardFormatError as exc:
            raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise SourcePackError("schema_drift", f"unparseable {document['format']}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(parsed["records"]) > limit:
            raise SourcePackError("budget_exhausted", "document has more records than the run's result budget")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        sha = hashlib.sha256(raw).hexdigest()
        records = [{
            "id": f"{r['provider']}:{r['record_type']}:{r['native_id']}:{r['revision_key']}",
            "title": r["title"], "url": r["source_url"], "language": "en",
            "published_at": r.get("published_at"), "content": hr.canonical(r), "hazard_record": r,
        } for r in parsed["records"]]
        receipt = {"status": status, "provider": self.declared["provider"], "document": document.get("label"),
                   "format": document["format"], "response_sha256": sha, "records": len(records),
                   "excluded": parsed.get("excluded") or [], "evidence_origin": origin,
                   "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


# The token the key-gated GloFAS fixture expects (sources without auth ignore it).
FIXTURE_SECRET = "fixture-glofas-token"
ADAPTERS = {CONNECTOR: HazardAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored documents keyed by URL path and sorted query; responses are marked as fixture evidence."""

    from urllib.parse import urlencode

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
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}), "content": content,
                "origin": "fixture", **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def fixture_request(url: str) -> str:
    from urllib.parse import urlencode

    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query)))
    return parts.path + ("?" + query if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = HazardAdapter(source, transport=fixture_transport(list(fixture["native_pages"])), secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = ["ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "FIXTURE_SECRET", "FORMATS", "LIVE_VERIFICATION", "PARSERS",
           "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "HazardAdapter", "HazardFormatError", "fixture_request",
           "fixture_transport", "hazards_declaration", "replay_native_fixture"]
