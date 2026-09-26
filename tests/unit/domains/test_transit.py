"""GTFS Schedule + GTFS Realtime transit over the Geospatial store (fictional fixture feed)."""

from __future__ import annotations

import base64
import copy
import json
from datetime import date
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)
from src.ingestion.transit_sources import (
    PROVIDER_CONTRACTS,
    GtfsAdapter,
    decode_feed_message,
    fixture_transport,
)
from src.kb.transit import READ_SCOPE, TransitError, TransitStore, service_time
from tests.unit.transit_fixture_builder import (
    HEADER_TIMESTAMP,
    REALTIME_URL,
    SCHEDULE_URL,
    build_zip,
    feed_message,
    fixture,
    message,
    realtime_entities,
    schedule_files,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/geospatial.json"
SCOPES = {READ_SCOPE, "namespace:global:read"}
OBSERVED_MS = (HEADER_TIMESTAMP + 60) * 1000


def schema(name):
    return jsonschema.Draft7Validator(json.loads((ROOT / f"contracts/schemas/jsonschema/{name}.json").read_text()))


def pack(*, realtime: bool):
    value = json.loads(PACK.read_text())
    item = next(s for s in value["sources"] if s["source_id"] == "vbb-gtfs")
    if realtime:
        item["transit"]["realtime_url"] = REALTIME_URL
    value = validate_source_pack(value)
    return value, copy.deepcopy(next(s for s in value["sources"] if s["source_id"] == "vbb-gtfs"))


def pages(**overrides):
    value = {page["url"]: dict(page) for page in fixture()["native_pages"]}
    for url, raw in overrides.items():
        value[url] = {"url": url, "status": 200, "body_base64": base64.b64encode(raw).decode()}
    return list(value.values())


def run(conn, value, key="fx"):
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for item in value["sources"]:
        runtime.accept_license(value["pack_id"], item["source_id"], principal_id="operator")
    return runtime.run({"pack_id": value["pack_id"], "run_key": key, "operation": "feed", "source_ids": ["vbb-gtfs"],
                        "max_results": 10, "max_bytes": 100_000_000, "timeout_ms": 120_000},
                       principal_id="operator", adapters=runtime.fixture_adapters(value["pack_id"], ROOT),
                       dns_resolver=lambda _h: ["8.8.8.8"])


@pytest.fixture()
def loaded():
    conn = duckdb.connect(":memory:")
    value, _ = pack(realtime=True)
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    receipt = run(conn, value)
    yield conn, receipt, TransitStore(conn, now=lambda: OBSERVED_MS)
    conn.close()


def test_contracts_and_offline_conformance():
    assert {k: v["status"] for k, v in PROVIDER_CONTRACTS.items()} == {
        "vbb": "unverified-live", "rail-and-flight-status": "not-implemented"}
    assert "not declared" in PROVIDER_CONTRACTS["vbb"]["realtime"]
    value, item = pack(realtime=False)
    assert "realtime_url" not in item["transit"]  # no realtime endpoint claimed for the live feed
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"] and next(s for s in result["sources"] if s["source_id"] == "vbb-gtfs")["records"] == 1


def test_adapter_bounds_and_record_contract():
    _, item = pack(realtime=True)
    for change, code in (({"route_ids": []}, "unbounded_source"), ({"max_stop_times": 0}, "unbounded_source"),
                         ({"realtime_url": "http://transit.example.org/rt"}, "unsafe_endpoint")):
        bad = copy.deepcopy(item)
        bad["transit"].update(change)
        with pytest.raises(SourcePackError) as caught:
            GtfsAdapter(bad)
        assert caught.value.code == code
    adapter = GtfsAdapter(item, transport=fixture_transport(pages()))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "feed", "parameters": {"route": "R9"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    first = adapter.fetch_page({"operation": "feed"}, cursor=None)
    second = adapter.fetch_page({"operation": "feed"}, cursor=first.next_cursor)
    validator = schema("noesis-transit-feed-v1")
    schedule, realtime = first.records[0]["transit_record"], second.records[0]["transit_record"]
    assert not list(validator.iter_errors(schedule)) and not list(validator.iter_errors(realtime))
    assert {t["trip_id"] for t in schedule["trips"]} == {"T1", "T2", "T3", "T4"}  # R9 not pinned
    assert {s["stop_id"] for s in schedule["stops"]} == {"S-A", "S-B", "S-C"}
    assert schedule["feed_info"]["feed_version"] == "2026-09-20" and schedule["missing_routes"] == []
    assert [u["trip"]["trip_id"] for u in realtime["trip_updates"]] == ["T1", "T3"]
    assert second.next_cursor is None and second.receipt["header_timestamp"] == HEADER_TIMESTAMP


def test_malformed_oversized_and_missing_feeds():
    _, item = pack(realtime=True)
    for raw, code in ((b"not a zip", "schema_drift"),
                      (build_zip({k: v for k, v in schedule_files().items() if k != "stop_times.txt"}),
                       "schema_drift"),
                      (build_zip({**schedule_files(), "../evil.txt": "x"}), "schema_drift")):
        adapter = GtfsAdapter(item, transport=fixture_transport(pages(**{SCHEDULE_URL: raw})))
        with pytest.raises(SourcePackError) as caught:
            adapter.fetch_page({"operation": "feed"}, cursor=None)
        assert caught.value.code == code
    small = copy.deepcopy(item)
    small["budgets"]["max_bytes"] = 100
    with pytest.raises(SourcePackError) as caught:
        GtfsAdapter(small, transport=fixture_transport(pages())).fetch_page({"operation": "feed"}, cursor=None)
    assert caught.value.code == "response_too_large"
    capped = copy.deepcopy(item)
    capped["transit"]["max_stop_times"] = 2
    page = GtfsAdapter(capped, transport=fixture_transport(pages())).fetch_page({"operation": "feed"}, cursor=None)
    assert page.receipt["stop_times_truncated"] is True
    cursor = json.dumps({"page": "realtime"})
    for raw in (feed_message(realtime_entities())[:-3], message((2, message((1, "no-header"))))):
        adapter = GtfsAdapter(item, transport=fixture_transport(pages(**{REALTIME_URL: raw})))
        with pytest.raises(SourcePackError) as caught:
            adapter.fetch_page({"operation": "feed"}, cursor=cursor)
        assert caught.value.code == "schema_drift"
    gone = [p for p in pages() if p["url"] != REALTIME_URL]
    page = GtfsAdapter(item, transport=fixture_transport(gone)).fetch_page({"operation": "feed"}, cursor=cursor)
    assert page.records == () and page.receipt["realtime"] == "missing"
    assert decode_feed_message(feed_message(realtime_entities()))["entity"][-1] == {"id": "future"}


def test_service_day_times_use_elapsed_time_from_noon_minus_twelve_hours():
    assert service_time(date(2026, 9, 28), 24 * 3600 + 1800, "Europe/Berlin").isoformat() == \
        "2026-09-29T00:30:00+02:00"
    assert service_time(date(2026, 10, 25), 0, "Europe/Berlin").isoformat() == "2026-10-25T01:00:00+02:00"
    assert service_time(date(2026, 3, 29), 0, "Europe/Berlin").isoformat() == "2026-03-28T23:00:00+01:00"


def test_departures_after_midnight_realtime_delay_cancellation_and_alert(loaded):
    _, receipt, store = loaded
    assert receipt["status"] == "complete"
    validator = schema("noesis-transit-departures-v1")
    at_a = store.departures("global", "vbb", "S-A", "2026-09-28", scopes=SCOPES)
    assert not list(validator.iter_errors(at_a))
    assert at_a["timezone"] == "Europe/Berlin" and at_a["realtime_state"] == "observed"
    assert [(d["trip_id"], d["scheduled_departure"]) for d in at_a["departures"]] == [
        ("T1", "2026-09-28T08:00:00+02:00"), ("T2", "2026-09-29T00:30:00+02:00")]
    assert at_a["departures"][0]["realtime"] == {"state": "observed", "trip": "SCHEDULED"}
    at_b = store.departures("global", "vbb", "S-B", "2026-09-28", scopes=SCOPES)
    assert not list(validator.iter_errors(at_b))
    by_trip = {d["trip_id"]: d["realtime"] for d in at_b["departures"]}
    assert by_trip["T1"] == {"state": "observed", "trip": "SCHEDULED", "stop": "SCHEDULED", "delay_s": 120}
    assert by_trip["T3"] == {"state": "observed", "trip": "CANCELED"}
    assert by_trip["T2"] == {"state": "observed"}
    assert at_b["alerts"] == [{"effect": "DETOUR", "header": ["Umleitung Linie X2"], "active_period": [
        {"start": HEADER_TIMESTAMP - 3600, "end": HEADER_TIMESTAMP + 7200}]}]


def test_calendar_exceptions_stale_and_missing_realtime(loaded):
    conn, _, store = loaded
    holiday = store.departures("global", "vbb", "S-A", "2026-09-30", scopes=SCOPES)
    assert holiday["active_services"] == ["SA"] and [d["trip_id"] for d in holiday["departures"]] == ["T4"]
    assert store.departures("global", "vbb", "S-A", "2026-09-27", scopes=SCOPES)["departures"] == []  # Sunday
    later = TransitStore(conn, now=lambda: (HEADER_TIMESTAMP + 3600) * 1000)
    stale = later.departures("global", "vbb", "S-B", "2026-09-28", scopes=SCOPES)
    assert stale["realtime_state"] == "stale"
    assert {d["realtime"]["state"] for d in stale["departures"]} == {"stale"}
    other = duckdb.connect(":memory:")
    value, _ = pack(realtime=False)
    SourcePackStore(other).install(value, principal_id="operator", enable=True, now_ms=10)
    assert run(other, value)["status"] == "complete"
    missing = TransitStore(other).departures("global", "vbb", "S-B", "2026-09-28", scopes=SCOPES)
    assert missing["realtime_state"] == "missing" and missing["alerts"] == []
    assert all(d["realtime"] == {"state": "missing"} for d in missing["departures"])
    other.close()


def test_feed_versions_relocations_shapes_and_stops_in_the_geospatial_store(loaded):
    conn, _, store = loaded
    _, item = pack(realtime=False)
    newer = build_zip(schedule_files(relocate_b=True, feed_version="2026-09-27"))
    page = GtfsAdapter(item, transport=fixture_transport(pages(**{SCHEDULE_URL: newer}))).fetch_page(
        {"operation": "feed"}, cursor=None)
    later = TransitStore(conn, now=lambda: OBSERVED_MS + 1)
    assert later.observe_page("global", page.records, run_id="second", source=item)["feed_versions"] == 1
    assert later.observe_page("global", page.records, run_id="replay", source=item) == {
        "feed_versions": 0, "stops": 0, "shapes": 0, "realtime": 0}
    feed = store.feed("global", "vbb", scopes=SCOPES)
    assert [v["feed_info"]["feed_version"] for v in feed["versions"]] == ["2026-09-20", "2026-09-27"]
    [moved] = feed["stop_relocations"]
    assert moved["stop_id"] == "S-B" and moved["from"] == [52.53, 13.41] and moved["to"] == [52.531, 13.4115]
    assert {(s["shape_id"], tuple(s["route_ids"]), s["points"]) for s in feed["route_shapes"]} == {("SH1", ("R1",), 3)}
    assert all(s["geometry_id"] for s in feed["route_shapes"])
    inside = later.stops_in_bbox("global", "vbb", [13.40, 52.51, 13.415, 52.535], scopes=SCOPES)
    assert [s["stop_id"] for s in inside["stops"]] == ["S-A", "S-B"]
    assert inside["feed_version"] == feed["versions"][-1]["feed_version"]
    [(source_json,)] = conn.execute("SELECT source_json FROM geospatial_geometries WHERE geometry_id=?",
                                    [inside["stops"][1]["geometry_id"]]).fetchall()
    assert json.loads(source_json)["kind"] == "gtfs-stop"
    with pytest.raises(TransitError) as caught:
        store.feed("global", "vbb", scopes={READ_SCOPE, "namespace:other:read"})
    assert caught.value.code == "unauthorized"
