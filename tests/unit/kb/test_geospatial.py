from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.events import EventKnowledgeStore
from src.kb.geospatial import GeospatialError, GeospatialStore

READ = {"knowledge:geospatial:read"}
WRITE = {"knowledge:geospatial:write"}
REVIEW = {"knowledge:geospatial:review"}
CALCULATE = {"knowledge:geospatial:calculate"}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _place(store, name, key, coordinates, *, names=None, namespace="osint"):
    return store.register_place(
        namespace,
        name,
        "settlement",
        names=names or [{"value": name, "language": "en", "kind": "canonical"}],
        source_ids={"osm": key},
        parent_ids=[],
        place_key=key,
        geometry={"type": "Point", "coordinates": coordinates},
        principal_id="analyst",
        scopes=WRITE,
        provenance={"citation": f"osm:{key}"},
        generation=2,
    )


def _geometry(store, place_id, geometry, **updates):
    values = {
        "crs": "EPSG:4326",
        "precision_m": 5,
        "simplified_from": None,
        "disputed": False,
        "admin_hierarchy": ["country:test"],
        "source": {"dataset": "offline-fixture", "revision": "1"},
        "evidence": [{"citation": "map:1"}],
        "principal_id": "mapper",
        "scopes": WRITE,
    }
    values.update(updates)
    return store.store_geometry("osint", geometry, place_id=place_id, **values)


def test_versioned_places_same_names_aliases_historical_names_and_offline_bootstrap():
    conn = duckdb.connect(":memory:")
    store = GeospatialStore(conn, now=lambda: 1000)
    illinois = _place(store, "Springfield", "springfield-il", [-89.64, 39.78])
    massachusetts = _place(store, "Springfield", "springfield-ma", [-72.59, 42.10])
    assert illinois["place_id"] != massachusetts["place_id"]
    ambiguous = store.resolve("osint", "Springfield", scopes=READ)
    assert ambiguous["status"] == "ambiguous" and len(ambiguous["candidates"]) == 2
    cologne = _place(
        store,
        "Köln",
        "cologne",
        [6.96, 50.94],
        names=[
            {"value": "Köln", "language": "de", "kind": "canonical"},
            {"value": "Cologne", "language": "en", "kind": "alias"},
        ],
    )
    assert (
        store.resolve("osint", "Koln", scopes=READ)["selected_place_id"]
        == cologne["place_id"]
    )
    kyiv = _place(
        store,
        "Kyiv",
        "kyiv-local",
        [30.52, 50.45],
        names=[
            {
                "value": "Kyiv",
                "language": "en",
                "kind": "canonical",
                "valid_from_ms": 2022,
            },
            {
                "value": "Kiev",
                "language": "en",
                "kind": "historical",
                "valid_to_ms": 2022,
            },
            {"value": "Київ", "language": "uk", "kind": "native"},
        ],
    )
    assert (
        store.resolve("osint", "Kiev", scopes=READ, as_of_ms=2020)["selected_place_id"]
        == kyiv["place_id"]
    )
    assert (
        store.resolve("osint", "Kiev", scopes=READ, as_of_ms=2025)["status"]
        == "unresolved"
    )
    assert store.resolve("osint", "Berlin", scopes=READ)["status"] == "resolved"
    revised = store.revise_place(
        "osint",
        illinois["place_id"],
        1,
        {
            "names": illinois["names"]
            + [{"value": "Springfield, IL", "kind": "qualified", "language": "en"}]
        },
        reason="Add disambiguating label.",
        principal_id="curator",
        scopes=WRITE,
    )
    assert revised["revision"] == 2
    assert (
        len(
            store.place(
                "osint", illinois["place_id"], scopes=READ, include_history=True
            )["history"]
        )
        == 2
    )
    _validate("noesis-geospatial-place-v1.json", revised)
    _validate("noesis-geocode-resolution-v1.json", ambiguous)
    conn.close()


def test_time_bounded_boundaries_disputes_invalid_geometry_and_simplification():
    conn = duckdb.connect(":memory:")
    store = GeospatialStore(conn, now=lambda: 1000)
    region = _place(store, "Test Region", "region", [1, 1])
    old = _geometry(
        store,
        region["place_id"],
        {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]]},
        valid_from_ms=100,
        valid_to_ms=200,
    )
    new = _geometry(
        store,
        region["place_id"],
        {"type": "Polygon", "coordinates": [[[0, 0], [3, 0], [3, 3], [0, 3], [0, 0]]]},
        valid_from_ms=200,
        disputed=True,
    )
    at_150 = [
        item["geometry_id"]
        for item in store.geometries(
            "osint", region["place_id"], scopes=READ, as_of_ms=150
        )
    ]
    assert old["geometry_id"] in at_150 and new["geometry_id"] not in at_150
    undisputed_at_250 = [
        item["geometry_id"]
        for item in store.geometries(
            "osint",
            region["place_id"],
            scopes=READ,
            as_of_ms=250,
            include_disputed=False,
        )
    ]
    assert new["geometry_id"] not in undisputed_at_250
    with pytest.raises(GeospatialError, match="rings must be closed"):
        _geometry(
            store,
            region["place_id"],
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
        )
    line = _geometry(
        store,
        region["place_id"],
        {"type": "LineString", "coordinates": [[0, 0], [0.000001, 0], [1, 0]]},
    )
    simplified = store.simplify(
        "osint", line["geometry_id"], 10, principal_id="mapper", scopes=WRITE
    )
    assert simplified["simplified_from"] == line["geometry_id"]
    assert simplified["geometry"]["coordinates"] == [[0.0, 0.0], [1.0, 0.0]]
    _validate("noesis-geospatial-geometry-v1.json", new)
    conn.close()


def test_containment_border_dateline_polar_proximity_intersection_route_and_replay():
    conn = duckdb.connect(":memory:")
    store = GeospatialStore(conn, now=lambda: 1000)
    region = _place(store, "Dateline Zone", "dateline", [180, 80])
    polygon = _geometry(
        store,
        region["place_id"],
        {
            "type": "Polygon",
            "coordinates": [[[179, 79], [-179, 79], [-179, 81], [179, 81], [179, 79]]],
        },
        precision_m=2,
    )
    inside = store.relation(
        "osint",
        "contains",
        polygon["geometry_id"],
        [180, 80],
        tolerance_m=0,
        principal_id="mapper",
        scopes=CALCULATE,
    )
    border = store.relation(
        "osint",
        "contains",
        polygon["geometry_id"],
        [179, 80],
        tolerance_m=1,
        principal_id="mapper",
        scopes=CALCULATE,
    )
    assert inside["result"]["contains"] and border["result"]["contains"]
    pole = _geometry(
        store, region["place_id"], {"type": "Point", "coordinates": [0, 89.9]}
    )
    proximity = store.relation(
        "osint",
        "proximity",
        pole["geometry_id"],
        [180, 89.9],
        tolerance_m=25_000,
        principal_id="mapper",
        scopes=CALCULATE,
    )
    assert proximity["result"]["within_tolerance"]
    horizontal = _geometry(
        store,
        region["place_id"],
        {"type": "LineString", "coordinates": [[0, 0], [2, 2]]},
    )
    vertical = _geometry(
        store,
        region["place_id"],
        {"type": "LineString", "coordinates": [[0, 2], [2, 0]]},
    )
    assert store.relation(
        "osint",
        "intersects",
        horizontal["geometry_id"],
        vertical["geometry_id"],
        principal_id="mapper",
        scopes=CALCULATE,
    )["result"]["intersects"]
    route = store.relation(
        "osint",
        "route",
        horizontal["geometry_id"],
        None,
        principal_id="mapper",
        scopes=CALCULATE,
    )
    assert route["result"]["segments"] == 1 and route["result"]["length_m"] > 0
    assert store.replay("osint", route["receipt_id"], scopes=READ)["deterministic"]
    _validate("noesis-spatial-result-v1.json", route)
    conn.close()


def test_ambiguous_geocoding_coordinate_evidence_and_reversible_review():
    conn = duckdb.connect(":memory:")
    ticks = iter(range(1000, 1100))
    store = GeospatialStore(conn, now=lambda: next(ticks))
    west = _place(store, "Twin City", "west", [-120, 40])
    east = _place(store, "Twin City", "east", [120, 40])
    result = store.resolve(
        "osint",
        "Twin City",
        coordinate_hint=[-120, 40],
        context={"document_revision_id": "doc:1", "address": "partial"},
        scopes=READ,
    )
    assert result["status"] == "ambiguous"
    assert result["candidates"][0]["place_id"] == west["place_id"]
    assert result["candidates"][0]["confidence"] > result["candidates"][1]["confidence"]
    saved = store.save_resolution(result, principal_id="analyst", scopes=WRITE)
    accepted = store.review(
        "osint",
        saved["resolution_id"],
        "accept",
        selected_place_id=west["place_id"],
        reason="Coordinate evidence agrees.",
        principal_id="reviewer",
        scopes=REVIEW,
    )
    reversed_review = store.review(
        "osint",
        saved["resolution_id"],
        "accept",
        selected_place_id=east["place_id"],
        reason="Later source corrected the coordinates.",
        principal_id="reviewer",
        scopes=REVIEW,
    )
    assert reversed_review["predecessor_review_id"] == accepted["review_id"]
    with pytest.raises(GeospatialError, match="retained candidate"):
        store.review(
            "osint",
            saved["resolution_id"],
            "accept",
            selected_place_id="place:missing",
            reason="invalid",
            principal_id="reviewer",
            scopes=REVIEW,
        )
    conn.close()


def test_bbox_radius_containment_pagination_and_cursor_binding():
    conn = duckdb.connect(":memory:")
    store = GeospatialStore(conn, now=lambda: 1000)
    for index in range(3):
        _place(store, f"Site {index}", f"site-{index}", [10 + index * 0.01, 50])
    first = store.search("osint", scopes=READ, bbox=[9, 49, 11, 51], limit=2)
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = store.search(
        "osint", scopes=READ, bbox=[9, 49, 11, 51], limit=2, cursor=first["next_cursor"]
    )
    assert len(second["items"]) >= 1
    radius = store.search("osint", scopes=READ, center=[10, 50], radius_m=2000)
    assert len(radius["items"]) >= 2
    with pytest.raises(GeospatialError, match="different filters"):
        store.search(
            "osint",
            scopes=READ,
            bbox=[8, 49, 11, 51],
            limit=2,
            cursor=first["next_cursor"],
        )
    _validate("noesis-spatial-result-v1.json", first)
    conn.close()


def test_event_map_integration_namespace_and_auth():
    conn = duckdb.connect(":memory:")
    EventKnowledgeStore(conn, now=lambda: 100).create(
        "political",
        {
            "event_type": "protest",
            "participants": ["group:1"],
            "location": {"coordinates": [13.405, 52.52], "place_id": "place:berlin"},
            "time": {"start_ms": 10, "end_ms": 20},
            "evidence": [{"citation": "report:1"}],
        },
        event_key="protest:1",
        principal_id="analyst",
        scopes={"knowledge:event:write"},
    )
    store = GeospatialStore(conn, now=lambda: 200)
    mapped = store.event_map("political", scopes=READ, start_ms=0, end_ms=30)
    assert mapped["count"] == 1 and mapped["items"][0]["event_id"]
    assert store.event_map("scientific", scopes=READ)["items"] == []
    with pytest.raises(GeospatialError, match="required scope"):
        store.search("political", scopes=set())
    conn.close()
