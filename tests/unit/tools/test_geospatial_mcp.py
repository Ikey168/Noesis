from __future__ import annotations

import asyncio
import inspect

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_geospatial_mcp_registry_resolution_search_relations_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "geospatial.duckdb"
    scopes = {"knowledge:geospatial:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_geospatial_place",
        "revise_geospatial_place",
        "get_geospatial_place",
        "store_geospatial_geometry",
        "list_geospatial_geometries",
        "simplify_geospatial_geometry",
        "resolve_geospatial_candidates",
        "record_geospatial_resolution",
        "review_geospatial_resolution",
        "calculate_spatial_relation",
        "replay_spatial_relation",
        "search_geospatial_knowledge",
        "query_geospatial_event_map",
    }
    assert expected <= tools.keys()
    denied = _call(
        tools["register_geospatial_place"],
        namespace="osint",
        canonical_name="Checkpoint Alpha",
        place_type="facility",
        names=[{"value": "Checkpoint Alpha", "kind": "canonical"}],
        source_ids={"local": "alpha"},
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.update(
        {
            "knowledge:geospatial:write",
            "knowledge:geospatial:review",
            "knowledge:geospatial:calculate",
        }
    )
    place = _call(
        tools["register_geospatial_place"],
        namespace="osint",
        canonical_name="Checkpoint Alpha",
        place_type="facility",
        names=[
            {"value": "Checkpoint Alpha", "kind": "canonical", "language": "en"},
            {"value": "CP-A", "kind": "alias", "language": "und"},
        ],
        source_ids={"local": "alpha"},
        place_key="checkpoint-alpha",
        geometry={"type": "Point", "coordinates": [13.405, 52.52]},
    )
    found = _call(
        tools["resolve_geospatial_candidates"],
        namespace="osint",
        mention="CP-A",
    )
    assert found["selected_place_id"] == place["place_id"]
    saved = _call(
        tools["record_geospatial_resolution"],
        namespace="osint",
        mention="CP-A",
        context={"document_revision_id": "doc:1"},
    )
    review = _call(
        tools["review_geospatial_resolution"],
        namespace="osint",
        resolution_id=saved["resolution_id"],
        decision="accept",
        selected_place_id=place["place_id"],
        reason="Source coordinates agree.",
    )
    assert review["decision"] == "accept"
    polygon = _call(
        tools["store_geospatial_geometry"],
        namespace="osint",
        place_id=place["place_id"],
        geometry={
            "type": "Polygon",
            "coordinates": [
                [[13.3, 52.4], [13.5, 52.4], [13.5, 52.6], [13.3, 52.6], [13.3, 52.4]]
            ],
        },
        source={"map": "fixture"},
        evidence=[{"citation": "map:1"}],
        precision_m=5,
    )
    relation = _call(
        tools["calculate_spatial_relation"],
        namespace="osint",
        operation="contains",
        left_geometry_id=polygon["geometry_id"],
        right=[13.405, 52.52],
        tolerance_m=1,
    )
    assert relation["result"]["contains"]
    assert _call(
        tools["replay_spatial_relation"],
        namespace="osint",
        receipt_id=relation["receipt_id"],
    )["deterministic"]
    searched = _call(
        tools["search_geospatial_knowledge"],
        namespace="osint",
        bbox=[13, 52, 14, 53],
        limit=10,
    )
    assert polygon["geometry_id"] in {item["geometry_id"] for item in searched["items"]}


def test_optional_multipart_simplification_tool(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("shapely")
    pytest.importorskip("pyproj")
    from src.kb.geospatial import WRITE_SCOPE, GeospatialStore
    database = tmp_path / "multipart.duckdb"
    conn = duckdb.connect(str(database))
    store = GeospatialStore(conn)
    original = store.store_geometry("berlin", {"type":"MultiPolygon", "coordinates":[[[[13,52],[13.1,52],[13.1,52.1],[13,52.1],[13,52]]]]},
        place_id=None,crs="EPSG:4326",precision_m=1,simplified_from=None,disputed=False,
        admin_hierarchy=[],source={"fixture":True},evidence=[],principal_id="fixture",scopes={WRITE_SCOPE})
    conn.close()
    scopes = {WRITE_SCOPE}
    monkeypatch.setattr(server, "_context", lambda: ("fixture", scopes))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(str(database)))
    tools = asyncio.run(server.mcp.get_tools())
    result = _call(tools["simplify_geospatial_geometry"], namespace="berlin", geometry_id=original["geometry_id"],
        tolerance_m=10, backend="shapely", projected_crs="EPSG:25833")
    assert result["simplified_from"] == original["geometry_id"]
    assert result["source"]["simplification"]["request"]["repair_policy"] == "reject"
    scopes.clear()
    denied = _call(tools["simplify_geospatial_geometry"], namespace="berlin", geometry_id=original["geometry_id"],
        tolerance_m=10, backend="shapely", projected_crs="EPSG:25833")
    assert denied["error"]["code"] == "unauthorized"


def test_geospatial_capabilities_advertise_contracts_and_features():
    capabilities = server.knowledge_engine_capabilities.fn()
    assert {
        "noesis-geospatial-place-v1",
        "noesis-geospatial-geometry-v1",
        "noesis-geocode-resolution-v1",
        "noesis-spatial-result-v1",
    } <= set(capabilities["contracts"])
    assert {
        "versioned-place-gazetteer",
        "time-bounded-wgs84-geometry",
        "ambiguity-preserving-geocoding",
        "reproducible-spatial-relations",
        "bounded-event-map-queries",
    } <= set(capabilities["features"])
