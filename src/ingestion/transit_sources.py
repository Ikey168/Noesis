"""Bounded GTFS Schedule and GTFS Realtime acquisition for Geospatial transit queries.

One named feed per source. Page 0 reads the static GTFS ZIP within the byte
budget and keeps only the pinned routes (their trips, stop times, stops and
service calendars), capped by a stop-time ceiling; the feed version (the
ZIP hash plus ``feed_info``) identifies the schedule. Page 1, if a realtime URL
is declared, reads one GTFS Realtime FeedMessage (protobuf) and keeps trip
updates and alerts for the pinned routes with the header timestamp.

The protobuf decoder is a small stdlib wire-format reader for the GTFS-RT
fields used here; unknown fields are skipped. Static schedules never imply
that trips ran; realtime observations are what was observed, when.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-transit-feed-v1"
FIXTURE_SECRET = None
MAX_ROUTES = 20
MAX_MEMBER_BYTES = 200_000_000
MAX_SHAPE_POINTS = 50_000
PROVIDER_CONTRACTS = {
    "vbb": {
        "agency": "Verkehrsverbund Berlin-Brandenburg (VBB)",
        "schedule": "GTFS Schedule ZIP published by VBB (open data portal)",
        "realtime": "not declared: no official openly licensed GTFS-Realtime endpoint was validated for VBB",
        "license": "CC BY per the VBB open-data terms (operator must confirm)",
        "historical": "only feed versions acquired and stored here; replaced feeds are not historical operations",
        "status": "unverified-live",
    },
    "rail-and-flight-status": {"status": "not-implemented",
                               "reason": "access, terms and temporal semantics not assessed; static timetables "
                                         "never stand in for live operations"},
}

# ------------------------------------------------------------ protobuf wire


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise SourcePackError("schema_drift", "truncated protobuf varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise SourcePackError("schema_drift", "protobuf varint too long")


def _fields(data: bytes) -> list[tuple[int, int, Any]]:
    out, pos = [], 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            value, pos = data[pos:pos + 8], pos + 8
        elif wire == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos:pos + size], pos + size
            if pos > len(data):
                raise SourcePackError("schema_drift", "truncated protobuf field")
        elif wire == 5:
            value, pos = data[pos:pos + 4], pos + 4
        else:
            raise SourcePackError("schema_drift", f"unsupported protobuf wire type {wire}")
        out.append((number, wire, value))
    return out


def _signed(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def _message(data: bytes, schema: Mapping[int, tuple[str, Any, bool]]) -> dict[str, Any]:
    """Decode known fields; ``schema`` maps number -> (name, kind|sub-schema, repeated)."""

    result: dict[str, Any] = {}
    for number, _wire, value in _fields(data):
        if number not in schema:
            continue
        name, kind, repeated = schema[number]
        if isinstance(kind, dict):
            decoded = _message(value, kind)
        elif kind == "string":
            decoded = value.decode("utf-8", errors="replace")
        elif kind == "int":
            decoded = _signed(value)
        else:
            decoded = value
        if repeated:
            result.setdefault(name, []).append(decoded)
        else:
            result[name] = decoded
    return result


_TRANSLATED = {1: ("translation", {1: ("text", "string", False), 2: ("language", "string", False)}, True)}
_TRIP = {1: ("trip_id", "string", False), 2: ("start_time", "string", False), 3: ("start_date", "string", False),
         4: ("schedule_relationship", "uint", False), 5: ("route_id", "string", False)}
_EVENT = {1: ("delay", "int", False), 2: ("time", "int", False)}
_STU = {1: ("stop_sequence", "uint", False), 2: ("arrival", _EVENT, False), 3: ("departure", _EVENT, False),
        4: ("stop_id", "string", False), 5: ("schedule_relationship", "uint", False)}
_FEED = {
    1: ("header", {1: ("gtfs_realtime_version", "string", False), 2: ("incrementality", "uint", False),
                   3: ("timestamp", "uint", False)}, False),
    2: ("entity", {
        1: ("id", "string", False), 2: ("is_deleted", "uint", False),
        3: ("trip_update", {1: ("trip", _TRIP, False), 2: ("stop_time_update", _STU, True),
                            4: ("timestamp", "uint", False), 5: ("delay", "int", False)}, False),
        5: ("alert", {1: ("active_period", {1: ("start", "uint", False), 2: ("end", "uint", False)}, True),
                      5: ("informed_entity", {1: ("agency_id", "string", False), 2: ("route_id", "string", False),
                                              4: ("trip", _TRIP, False), 5: ("stop_id", "string", False)}, True),
                      6: ("cause", "uint", False), 7: ("effect", "uint", False),
                      10: ("header_text", _TRANSLATED, False), 11: ("description_text", _TRANSLATED, False)}, False),
    }, True),
}
TRIP_RELATIONSHIP = {0: "SCHEDULED", 1: "ADDED", 2: "UNSCHEDULED", 3: "CANCELED", 5: "REPLACEMENT"}
STOP_RELATIONSHIP = {0: "SCHEDULED", 1: "SKIPPED", 2: "NO_DATA"}
ALERT_EFFECT = {1: "NO_SERVICE", 2: "REDUCED_SERVICE", 3: "SIGNIFICANT_DELAYS", 4: "DETOUR", 5: "ADDITIONAL_SERVICE",
                6: "MODIFIED_SERVICE", 7: "OTHER_EFFECT", 8: "UNKNOWN_EFFECT", 9: "STOP_MOVED"}


def decode_feed_message(data: bytes) -> dict[str, Any]:
    return _message(data, _FEED)


# ---------------------------------------------------------------- schedule


def _read_csv(archive: zipfile.ZipFile, name: str, *, required: bool = True) -> list[dict[str, str]]:
    try:
        info = archive.getinfo(name)
    except KeyError:
        if required:
            raise SourcePackError("schema_drift", f"GTFS feed lacks {name}") from None
        return []
    if info.file_size > MAX_MEMBER_BYTES or info.file_size > max(1, info.compress_size) * 500:
        raise SourcePackError("response_too_large", f"GTFS member {name} exceeds the decompression limit")
    with archive.open(info) as stream:
        text = io.TextIOWrapper(stream, encoding="utf-8-sig", newline="")
        return [{k.strip(): (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(text)]


def parse_schedule(raw: bytes, route_ids: Sequence[str], *, max_stop_times: int) -> dict[str, Any]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise SourcePackError("schema_drift", "GTFS Schedule is not a ZIP archive") from exc
    with archive:
        if any(".." in info.filename or info.filename.startswith("/") for info in archive.infolist()):
            raise SourcePackError("schema_drift", "unsafe GTFS archive member path")
        agencies = _read_csv(archive, "agency.txt")
        routes = [r for r in _read_csv(archive, "routes.txt") if r.get("route_id") in set(route_ids)]
        trips = [t for t in _read_csv(archive, "trips.txt") if t.get("route_id") in set(route_ids)]
        trip_ids = {t["trip_id"] for t in trips}
        stop_times = [s for s in _read_csv(archive, "stop_times.txt") if s.get("trip_id") in trip_ids]
        truncated = len(stop_times) > max_stop_times
        stop_times = stop_times[:max_stop_times]
        stop_ids = {s["stop_id"] for s in stop_times}
        stops = [s for s in _read_csv(archive, "stops.txt") if s.get("stop_id") in stop_ids]
        service_ids = {t["service_id"] for t in trips}
        calendar = [c for c in _read_csv(archive, "calendar.txt", required=False) if c.get("service_id") in service_ids]
        dates = [c for c in _read_csv(archive, "calendar_dates.txt", required=False)
                 if c.get("service_id") in service_ids]
        shape_ids = {t["shape_id"] for t in trips if t.get("shape_id")}
        shapes = [p for p in _read_csv(archive, "shapes.txt", required=False) if p.get("shape_id") in shape_ids]
        feed_info = (_read_csv(archive, "feed_info.txt", required=False) or [{}])[0]
    if not agencies or not calendar and not dates:
        raise SourcePackError("schema_drift", "GTFS feed lacks agencies or service calendars")
    missing = sorted(set(route_ids) - {r["route_id"] for r in routes})
    return {"agencies": agencies, "routes": routes, "trips": trips, "stop_times": stop_times, "stops": stops,
            "calendar": calendar, "calendar_dates": dates, "shapes": shapes[:MAX_SHAPE_POINTS], "feed_info": feed_info,
            "missing_routes": missing, "stop_times_truncated": truncated,
            "shapes_truncated": len(shapes) > MAX_SHAPE_POINTS}


class GtfsAdapter:
    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        del secret
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        transit = dict(self.source.get("transit") or {})
        self.route_ids = [str(r) for r in transit.get("route_ids") or []]
        self.max_stop_times = int(transit.get("max_stop_times") or 0)
        self.realtime_url = transit.get("realtime_url")
        if not 1 <= len(self.route_ids) <= MAX_ROUTES or not 1 <= self.max_stop_times <= 200_000:
            raise SourcePackError("unbounded_source", f"transit sources pin 1-{MAX_ROUTES} routes and a stop-time cap")
        if self.realtime_url and urlsplit(self.realtime_url).scheme != "https":
            raise SourcePackError("unsafe_endpoint", "realtime feeds must be HTTPS")
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "transit": {"feed": transit.get("feed"), "routes": self.route_ids, "realtime": bool(self.realtime_url)},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _get(self, url: str) -> tuple[int, bytes]:
        response = self.transport(url=url, params={}, headers={},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "feed exceeds the source byte budget")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"feed returned HTTP {status}")
        return status, raw

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "transit runs use the pinned routes")
        feed = dict(self.source.get("transit") or {}).get("feed")
        if cursor is None:
            status, raw = self._get(self.source["endpoint"])
            if status >= 400:
                raise SourcePackError("schema_drift", f"GTFS Schedule returned HTTP {status}")
            schedule = parse_schedule(raw, self.route_ids, max_stop_times=self.max_stop_times)
            version = hashlib.sha256(raw).hexdigest()
            record = {"id": f"gtfs:{feed}:schedule:{version[:16]}", "title": f"GTFS schedule {feed} {version[:12]}",
                      "language": "de", "url": self.source["endpoint"],
                      "transit_record": {"contract": RECORD_CONTRACT, "kind": "schedule", "feed": feed,
                                         "feed_version": version, **schedule}}
            return RuntimePage((record,), json.dumps({"page": "realtime"}) if self.realtime_url else None, len(raw),
                               receipt={"status": status, "feed_version": version,
                                        "missing_routes": schedule["missing_routes"],
                                        "stop_times_truncated": schedule["stop_times_truncated"]})
        status, raw = self._get(self.realtime_url)
        if status >= 400 or not raw:
            return RuntimePage((), None, len(raw), receipt={"status": status, "realtime": "missing"})
        message = decode_feed_message(raw)
        header = dict(message.get("header") or {})
        if "timestamp" not in header:
            raise SourcePackError("schema_drift", "GTFS-RT FeedMessage lacks a header timestamp")
        routes = set(self.route_ids)
        updates, alerts = [], []
        for entity in message.get("entity") or []:
            if entity.get("is_deleted"):
                continue
            update = entity.get("trip_update")
            if update and dict(update.get("trip") or {}).get("route_id") in routes:
                updates.append({"entity_id": entity.get("id"), **update})
            alert = entity.get("alert")
            if alert and any(e.get("route_id") in routes or e.get("stop_id") for e in alert.get("informed_entity") or []):
                alerts.append({"entity_id": entity.get("id"), **alert})
        record = {"id": f"gtfs:{feed}:realtime:{header['timestamp']}",
                  "title": f"GTFS-RT {feed} at {header['timestamp']}", "language": "de", "url": self.realtime_url,
                  "transit_record": {"contract": RECORD_CONTRACT, "kind": "realtime", "feed": feed,
                                     "header_timestamp": header["timestamp"], "trip_updates": updates, "alerts": alerts,
                                     "raw_sha256": hashlib.sha256(raw).hexdigest()}}
        return RuntimePage((record,), None, len(raw), receipt={"status": status, "realtime": "observed",
                                                               "header_timestamp": header["timestamp"]})


ADAPTERS = {"gtfs": GtfsAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    import base64

    by_url = {page["url"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        page = by_url.get(url)
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        return {"status": int(page.get("status", 200)), "headers": {},
                "content": base64.b64decode(page["body_base64"])}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = GtfsAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
