"""Transit schedules and realtime observations over the existing Geospatial store.

Projected from GTFS records (``noesis-transit-feed-v1``):

* **Feed versions** are the ZIP hash plus ``feed_info``; each acquired version
  is kept with its selected routes, trips, stop times, stops and calendars. A
  newer version replaces the *current* schedule but never becomes evidence of
  what ran before it.
* **Stops** become points in the existing ``GeospatialStore`` with agency and
  feed provenance; a stop whose coordinates change between versions is
  recorded as relocated.
* **Service days** keep GTFS times beyond midnight (``25:10:00``) and resolve
  against the agency timezone with the GTFS noon-minus-12h rule; calendar
  exceptions add or remove service on a date.
* **Realtime** observations keep the header timestamp. Departures report
  realtime as ``observed``, ``stale`` (older than the declared maximum age)
  or ``missing``; cancellations, skipped stops, delays and alerts are shown
  only from an observation.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.ingestion.transit_sources import (
    ALERT_EFFECT,
    STOP_RELATIONSHIP,
    TRIP_RELATIONSHIP,
)
from src.kb.geospatial import GeospatialError, GeospatialStore

READ_SCOPE = "knowledge:transit:read"
DEFAULT_NAMESPACE = "global"
DEPARTURES_CONTRACT = "noesis-transit-departures-v1"
GEOSPATIAL_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write"}

_DDL = """
CREATE TABLE IF NOT EXISTS transit_feed_versions (
  namespace TEXT NOT NULL, feed TEXT NOT NULL, feed_version TEXT NOT NULL, feed_info_json TEXT NOT NULL,
  schedule_json TEXT NOT NULL, missing_routes_json TEXT NOT NULL, truncated BOOLEAN NOT NULL, run_id TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, feed, feed_version)
);
CREATE TABLE IF NOT EXISTS transit_stops (
  namespace TEXT NOT NULL, feed TEXT NOT NULL, feed_version TEXT NOT NULL, stop_id TEXT NOT NULL, name TEXT,
  lat DOUBLE, lon DOUBLE, geometry_id TEXT, PRIMARY KEY(namespace, feed, feed_version, stop_id)
);
CREATE TABLE IF NOT EXISTS transit_route_shapes (
  namespace TEXT NOT NULL, feed TEXT NOT NULL, feed_version TEXT NOT NULL, shape_id TEXT NOT NULL,
  route_ids_json TEXT NOT NULL, points INTEGER NOT NULL, geometry_id TEXT,
  PRIMARY KEY(namespace, feed, feed_version, shape_id)
);
CREATE TABLE IF NOT EXISTS transit_realtime (
  namespace TEXT NOT NULL, feed TEXT NOT NULL, header_timestamp BIGINT NOT NULL, record_json TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, feed, header_timestamp)
);
"""


class TransitError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def _authorize(namespace: str, scopes) -> None:
    if READ_SCOPE not in scopes or not {f"namespace:{namespace}:read", f"namespace:{namespace}:write"} & set(scopes):
        raise TransitError("unauthorized", f"{READ_SCOPE} and namespace access are required")


def gtfs_seconds(value: str) -> int | None:
    parts = str(value or "").split(":")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    hours, minutes, seconds = (int(p) for p in parts)
    return hours * 3600 + minutes * 60 + seconds


def service_time(service_date: date, seconds: int, timezone: str) -> datetime:
    """GTFS times count from noon minus 12h of the service day in the agency timezone."""

    zone = ZoneInfo(timezone)
    noon = datetime(service_date.year, service_date.month, service_date.day, 12, tzinfo=zone).astimezone(UTC)
    # Elapsed (not wall-clock) time: on DST-change days the origin is not local midnight.
    return (noon - timedelta(hours=12) + timedelta(seconds=seconds)).astimezone(zone)


class TransitStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.geo = GeospatialStore(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)

    def observe_page(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str,
                     source: Mapping[str, Any], principal_id: str = "system:transit") -> dict[str, int]:
        observed = self.now()
        counts = {"feed_versions": 0, "stops": 0, "shapes": 0, "realtime": 0}
        shapes_pending = []
        pending = []
        self.conn.execute("BEGIN")
        try:
            for item in records:
                record = dict(item.get("transit_record") or {})
                feed = record["feed"]
                if record["kind"] == "schedule":
                    schedule = {k: record.get(k) or [] for k in ("agencies", "routes", "trips", "stop_times",
                                                                  "calendar", "calendar_dates")}
                    inserted = self.conn.execute(
                        "INSERT OR IGNORE INTO transit_feed_versions VALUES (?,?,?,?,?,?,?,?,?) RETURNING feed_version",
                        [namespace, feed, record["feed_version"], _canonical(record.get("feed_info") or {}),
                         _canonical(schedule), _canonical(record.get("missing_routes") or []),
                         bool(record.get("stop_times_truncated")), run_id, observed]).fetchall()
                    counts["feed_versions"] += len(inserted)
                    if inserted:
                        pending += [(feed, record["feed_version"], stop) for stop in record.get("stops") or []]
                        shapes_pending.append((feed, record["feed_version"], record))
                else:
                    counts["realtime"] += len(self.conn.execute(
                        "INSERT OR IGNORE INTO transit_realtime VALUES (?,?,?,?,?) RETURNING feed",
                        [namespace, feed, int(record["header_timestamp"]), _canonical(record), observed]).fetchall())
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        precision = float(dict(source.get("transit") or {}).get("stop_precision_m") or 10)
        for feed, version, stop in pending:
            geometry_id = None
            try:
                lat, lon = float(stop["stop_lat"]), float(stop["stop_lon"])
                geometry_id = self.geo.store_geometry(
                    namespace, {"type": "Point", "coordinates": [lon, lat]}, place_id=None, crs="EPSG:4326",
                    precision_m=precision, simplified_from=None, disputed=False, admin_hierarchy=[],
                    source={"kind": "gtfs-stop", "feed": feed, "feed_version": version, "stop_id": stop["stop_id"]},
                    evidence=[{"feed": feed, "feed_version": version, "stop_id": stop["stop_id"]}],
                    principal_id=principal_id, scopes=GEOSPATIAL_SCOPES)["geometry_id"]
            except (ValueError, KeyError, TypeError, GeospatialError):
                lat = lon = None
            self.conn.execute("INSERT OR IGNORE INTO transit_stops VALUES (?,?,?,?,?,?,?,?)",
                              [namespace, feed, version, stop["stop_id"], stop.get("stop_name"), lat, lon, geometry_id])
            counts["stops"] += 1
        for feed, version, record in shapes_pending:
            counts["shapes"] += self._project_shapes(namespace, feed, version, record, precision, principal_id)
        return counts

    def _project_shapes(self, namespace: str, feed: str, version: str, record: Mapping[str, Any],
                        precision: float, principal_id: str) -> int:
        routes_by_shape: dict[str, set[str]] = {}
        for trip in record.get("trips") or []:
            if trip.get("shape_id"):
                routes_by_shape.setdefault(trip["shape_id"], set()).add(trip["route_id"])
        points: dict[str, list[tuple[int, float, float]]] = {}
        for point in record.get("shapes") or []:
            try:
                points.setdefault(point["shape_id"], []).append(
                    (int(point["shape_pt_sequence"]), float(point["shape_pt_lon"]), float(point["shape_pt_lat"])))
            except (KeyError, ValueError):
                continue
        stored = 0
        for shape_id, sequence in sorted(points.items()):
            line = [[lon, lat] for _, lon, lat in sorted(sequence)]
            geometry_id = None
            if len(line) >= 2:
                try:
                    geometry_id = self.geo.store_geometry(
                        namespace, {"type": "LineString", "coordinates": line}, place_id=None, crs="EPSG:4326",
                        precision_m=precision, simplified_from=None, disputed=False, admin_hierarchy=[],
                        source={"kind": "gtfs-shape", "feed": feed, "feed_version": version, "shape_id": shape_id,
                                "route_ids": sorted(routes_by_shape.get(shape_id, set()))},
                        evidence=[{"feed": feed, "feed_version": version, "shape_id": shape_id}],
                        principal_id=principal_id, scopes=GEOSPATIAL_SCOPES)["geometry_id"]
                except (ValueError, KeyError, TypeError, GeospatialError):
                    geometry_id = None
            self.conn.execute("INSERT OR IGNORE INTO transit_route_shapes VALUES (?,?,?,?,?,?,?)",
                              [namespace, feed, version, shape_id,
                               _canonical(sorted(routes_by_shape.get(shape_id, set()))), len(line), geometry_id])
            stored += 1
        return stored

    def _current(self, namespace: str, feed: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        row = self.conn.execute(
            "SELECT feed_version, schedule_json, feed_info_json FROM transit_feed_versions WHERE namespace=? AND feed=? "
            "ORDER BY observed_at_ms DESC LIMIT 1", [namespace, feed]).fetchone()
        if row is None:
            raise TransitError("not_found", "no schedule has been acquired for this feed")
        return row[0], _load(row[1], {}), _load(row[2], {})

    def feed(self, namespace: str, feed: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes)
        rows = self.conn.execute(
            "SELECT feed_version, feed_info_json, missing_routes_json, truncated, observed_at_ms FROM "
            "transit_feed_versions WHERE namespace=? AND feed=? ORDER BY observed_at_ms", [namespace, feed]).fetchall()
        versions = [{"feed_version": r[0], "feed_info": _load(r[1], {}), "missing_routes": _load(r[2], []),
                     "stop_times_truncated": r[3], "observed_at_ms": r[4]} for r in rows]
        relocations = self.conn.execute(
            "SELECT a.stop_id, a.feed_version, b.feed_version, a.lat, a.lon, b.lat, b.lon FROM transit_stops a "
            "JOIN transit_stops b ON a.namespace=b.namespace AND a.feed=b.feed AND a.stop_id=b.stop_id "
            "AND a.feed_version<>b.feed_version JOIN transit_feed_versions va ON va.feed_version=a.feed_version "
            "JOIN transit_feed_versions vb ON vb.feed_version=b.feed_version "
            "WHERE a.namespace=? AND a.feed=? AND va.observed_at_ms<vb.observed_at_ms AND (a.lat<>b.lat OR a.lon<>b.lon)",
            [namespace, feed]).fetchall()
        shapes = self.conn.execute(
            "SELECT feed_version, shape_id, route_ids_json, points, geometry_id FROM transit_route_shapes "
            "WHERE namespace=? AND feed=? ORDER BY feed_version, shape_id", [namespace, feed]).fetchall()
        return {"feed": feed, "versions": versions,
                "route_shapes": [{"feed_version": r[0], "shape_id": r[1], "route_ids": _load(r[2], []), "points": r[3],
                                  "geometry_id": r[4]} for r in shapes],
                "stop_relocations": [{"stop_id": r[0], "from_version": r[1], "to_version": r[2],
                                      "from": [r[3], r[4]], "to": [r[5], r[6]]} for r in relocations],
                "notice": "Feed versions are schedules as published when acquired, not a record of operations."}

    def _active_services(self, schedule: Mapping[str, Any], service_date: date) -> set[str]:
        weekday = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"][service_date.weekday()]
        day = service_date.strftime("%Y%m%d")
        active = {c["service_id"] for c in schedule.get("calendar") or []
                  if c.get(weekday) == "1" and c.get("start_date", "") <= day <= c.get("end_date", "")}
        for exception in schedule.get("calendar_dates") or []:
            if exception.get("date") == day:
                if exception.get("exception_type") == "1":
                    active.add(exception["service_id"])
                elif exception.get("exception_type") == "2":
                    active.discard(exception["service_id"])
        return active

    def departures(self, namespace: str, feed: str, stop_id: str, service_date: str, *, scopes,
                   realtime_max_age_s: int = 300, now_ms: int | None = None) -> dict[str, Any]:
        _authorize(namespace, scopes)
        version, schedule, _ = self._current(namespace, feed)
        day = date.fromisoformat(service_date)
        timezone = (schedule.get("agencies") or [{}])[0].get("agency_timezone") or "UTC"
        active = self._active_services(schedule, day)
        trips = {t["trip_id"]: t for t in schedule.get("trips") or [] if t.get("service_id") in active}
        routes = {r["route_id"]: r for r in schedule.get("routes") or []}
        realtime_row = self.conn.execute(
            "SELECT header_timestamp, record_json FROM transit_realtime WHERE namespace=? AND feed=? "
            "ORDER BY header_timestamp DESC LIMIT 1", [namespace, feed]).fetchone()
        clock = (now_ms if now_ms is not None else self.now()) // 1000
        if realtime_row is None:
            realtime_state, realtime = "missing", {}
        else:
            realtime_state = "observed" if clock - int(realtime_row[0]) <= realtime_max_age_s else "stale"
            realtime = _load(realtime_row[1], {})
        updates = {dict(u.get("trip") or {}).get("trip_id"): u for u in realtime.get("trip_updates") or []}
        departures = []
        for stop_time in schedule.get("stop_times") or []:
            trip = trips.get(stop_time.get("trip_id"))
            if trip is None or stop_time.get("stop_id") != stop_id:
                continue
            seconds = gtfs_seconds(stop_time.get("departure_time") or stop_time.get("arrival_time"))
            if seconds is None:
                continue
            entry = {"trip_id": trip["trip_id"], "route_id": trip["route_id"],
                     "route_short_name": routes.get(trip["route_id"], {}).get("route_short_name"),
                     "headsign": trip.get("trip_headsign"), "stop_sequence": int(stop_time.get("stop_sequence") or 0),
                     "gtfs_departure_time": stop_time.get("departure_time"),
                     "scheduled_departure": service_time(day, seconds, timezone).isoformat(),
                     "realtime": {"state": realtime_state}}
            update = updates.get(trip["trip_id"])
            if update and realtime_state != "missing":
                trip_rel = dict(update.get("trip") or {}).get("schedule_relationship", 0)
                entry["realtime"]["trip"] = TRIP_RELATIONSHIP.get(trip_rel, "UNKNOWN")
                for stu in update.get("stop_time_update") or []:
                    if stu.get("stop_id") == stop_id or stu.get("stop_sequence") == entry["stop_sequence"]:
                        entry["realtime"]["stop"] = STOP_RELATIONSHIP.get(stu.get("schedule_relationship", 0),
                                                                          "UNKNOWN")
                        delay = dict(stu.get("departure") or stu.get("arrival") or {}).get("delay")
                        if delay is not None:
                            entry["realtime"]["delay_s"] = delay
            departures.append(entry)
        departures.sort(key=lambda item: item["scheduled_departure"])
        alerts = []
        for alert in realtime.get("alerts") or []:
            informed = alert.get("informed_entity") or []
            if any(e.get("stop_id") == stop_id or e.get("route_id") in {d["route_id"] for d in departures}
                   for e in informed):
                alerts.append({"effect": ALERT_EFFECT.get(alert.get("effect"), "UNKNOWN_EFFECT"),
                               "header": [t["text"] for t in dict(alert.get("header_text") or {}).get("translation") or []],
                               "active_period": alert.get("active_period") or []})
        return {"contract": DEPARTURES_CONTRACT, "feed": feed, "feed_version": version, "stop_id": stop_id,
                "service_date": service_date, "timezone": timezone, "active_services": sorted(active),
                "realtime_state": realtime_state,
                "realtime_header_timestamp": None if realtime_row is None else int(realtime_row[0]),
                "departures": departures, "alerts": alerts,
                "notice": "Scheduled times come from the current feed version; realtime is only what was observed."}

    def stops_in_bbox(self, namespace: str, feed: str, bbox: Sequence[float], *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes)
        version, _, _ = self._current(namespace, feed)
        if len(bbox) != 4 or not all(isinstance(v, int | float) for v in bbox) or bbox[1] > bbox[3]:
            raise TransitError("invalid_bbox", "bbox needs west,south,east,north")
        west, south, east, north = (float(v) for v in bbox)
        rows = self.conn.execute(
            "SELECT stop_id, name, lat, lon, geometry_id FROM transit_stops WHERE namespace=? AND feed=? "
            "AND feed_version=? AND lat BETWEEN ? AND ? AND (CASE WHEN ?<=? THEN lon BETWEEN ? AND ? "
            "ELSE lon>=? OR lon<=? END) ORDER BY stop_id",
            [namespace, feed, version, south, north, west, east, west, east, west, east]).fetchall()
        return {"feed": feed, "feed_version": version,
                "stops": [{"stop_id": r[0], "name": r[1], "lat": r[2], "lon": r[3], "geometry_id": r[4]}
                          for r in rows]}


class TransitProjector:
    def __init__(self, conn: Any) -> None:
        self.store = TransitStore(conn)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt
        namespace = str(dict(source.get("transit") or {}).get("namespace") or DEFAULT_NAMESPACE)
        return self.store.observe_page(namespace, records, run_id=run_id, source=source, principal_id=principal_id)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}

