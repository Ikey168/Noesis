"""Feature projection, refresh/removal semantics and recovery (#1699-#1701, #1703)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.ingestion.geojson_features import feature_key
from src.integrations import spatial
from src.integrations.common import IntegrationError
from src.kb.geospatial import GeospatialError
from src.kb.geospatial_features import GeospatialFeatureStore
from tests.unit.geospatial_pack_helpers import (
    FIXTURES,
    SCOPES,
    WfsServer,
    install,
    manifest,
    point,
    run,
    square,
)

SCHOOLS = "schulen:schulen"
PROVIDER = "Geoportal Berlin"


@pytest.fixture()
def pack():
    conn = duckdb.connect(":memory:")
    value, runtime = install(conn, manifest(page_size=2))
    yield conn, value, runtime, GeospatialFeatureStore(conn)
    conn.close()


def fid(native_id: str) -> str:
    return feature_key(PROVIDER, SCHOOLS, native_id)


def current(store, native_id):
    return store.feature("global", fid(native_id), scopes=SCOPES)


def test_projection_links_document_page_evidence_and_keeps_unknowns_unknown(pack):
    conn, value, runtime, store = pack
    result = run(runtime, value, "one", WfsServer([point("a", 0, 0), point("b", 10, 10), point("c", 20, 20)]))
    assert result["status"] == "complete"
    projection = result["sources"][0]["projection"]
    assert projection["completeness"] == "complete" and projection["states"] == {"projected": 3}
    feature = current(store, "a")
    revision = feature["current"]
    assert feature["provider"] == PROVIDER and feature["native_id"] == "a"
    assert revision["revision"] == 1 and revision["lifecycle"] == "active"
    assert conn.execute("SELECT 1 FROM documents WHERE document_id=?", [revision["document_id"]]).fetchone()
    assert revision["provenance"]["page"]["response_sha256"]
    assert revision["provenance"]["license"]["id"] == "dl-de-zero-2.0"
    assert revision["source_crs"] == "urn:ogc:def:crs:EPSG::25833"
    assert revision["source_geometry"]["coordinates"] == [390_000.0, 5_820_000.0]
    assert revision["temporal"]["valid_time"] == "unknown" and revision["temporal"]["valid_from_ms"] is None
    assert revision["precision"] == {"status": "unknown", "value_m": None,
                                     "basis": revision["precision"]["basis"]}
    assert revision["coordinate_transform"]["sha256"]
    geometry = store.geo.geometry("global", revision["geometry_id"], scopes=SCOPES)
    assert geometry["place_id"] is None, "features are not named places"
    assert geometry["source"]["coordinate_transform"]["sha256"] == revision["coordinate_transform"]["sha256"]
    assert geometry["policy"]["precision"]["status"] == "unknown"
    lon, lat = geometry["geometry"]["coordinates"]
    assert 13.3 < lon < 13.4 and 52.5 < lat < 52.6


def test_repeated_imports_are_idempotent_and_changes_keep_prior_revisions(pack):
    conn, value, runtime, store = pack
    features = [point("a", 0, 0, kind="school"), point("b", 10, 10)]
    run(runtime, value, "one", WfsServer(features))
    geometries = conn.execute("SELECT count(*) FROM geospatial_geometries WHERE namespace='global'").fetchone()[0]
    again = run(runtime, value, "two", WfsServer(features))
    assert again["sources"][0]["projection"]["states"] == {"unchanged": 2}
    assert conn.execute("SELECT count(*) FROM geospatial_geometries WHERE namespace='global'").fetchone()[0] == geometries
    features[0] = point("a", 5, 0, kind="school")
    run(runtime, value, "three", WfsServer(features))
    history = current(store, "a")["history"]
    assert [item["revision"] for item in history] == [1, 2]
    assert history[0]["geometry_id"] != history[1]["geometry_id"]
    assert history[0]["source_geometry"]["coordinates"][0] == 390_000.0
    assert current(store, "b")["current"]["revision"] == 1


def test_only_a_complete_snapshot_removes_absent_features(pack):
    _, value, runtime, store = pack
    run(runtime, value, "full", WfsServer([point("a", 0, 0), point("b", 10, 10), point("c", 20, 20)]))
    bounded = run(runtime, value, "bbox", WfsServer([point("a", 0, 0)]),
                  parameters={"bbox": [389_000, 5_819_000, 391_000, 5_821_000]})
    snapshot = bounded["sources"][0]["projection"]
    assert snapshot["completeness"] == "partial" and "bounded_scope" in snapshot["reasons"]
    assert current(store, "c")["current"]["lifecycle"] == "active"
    complete = run(runtime, value, "refresh", WfsServer([point("a", 0, 0), point("b", 10, 10)]))
    assert complete["sources"][0]["projection"]["removed"] == [fid("c")]
    removed = current(store, "c")
    assert removed["current"]["lifecycle"] == "removed"
    assert removed["current"]["provenance"]["basis"] == "absent_from_complete_snapshot"
    assert removed["current"]["temporal"]["valid_to_ms"] is None, "no invented end of validity"
    assert removed["history"][0]["lifecycle"] == "active"
    run(runtime, value, "back", WfsServer([point("a", 0, 0), point("b", 10, 10), point("c", 20, 20)]))
    assert [item["lifecycle"] for item in current(store, "c")["history"]] == ["active", "removed", "active"]


def test_interrupted_pagination_never_removes_and_resume_completes(pack):
    conn, value, runtime, store = pack
    run(runtime, value, "full", WfsServer([point(n, i, i) for i, n in enumerate("abcd")]))
    server = WfsServer([point("a", 0, 0), point("b", 1, 1), point("c", 2, 2)])
    server.failures = {2: {"status": 503, "headers": {}, "content": b""}}
    partial = run(runtime, value, "flaky", server, retries=0)
    assert partial["status"] == "failed"
    assert partial["sources"][0]["projection"]["completeness"] == "partial"
    assert current(store, "d")["current"]["lifecycle"] == "active"
    marker = conn.execute("SELECT snapshot_id FROM geospatial_feature_snapshot_current").fetchone()
    resumed = run(runtime, value, "flaky", server, retries=0)
    assert resumed["status"] == "complete"
    assert resumed["sources"][0]["projection"]["completeness"] == "complete"
    assert resumed["sources"][0]["projection"]["removed"] == [fid("d")]
    assert conn.execute("SELECT snapshot_id FROM geospatial_feature_snapshot_current").fetchone() != marker


@pytest.mark.parametrize("when", ["before", "after"])
def test_crash_around_projection_recovers_without_loss_or_duplicates(pack, when):
    conn, value, runtime, store = pack
    server = WfsServer([point(n, i, i) for i, n in enumerate("abcde")])
    projector = runtime._projector(value["sources"][0])
    original = projector.project_page
    crashed = {"done": False}

    def crashing(**kwargs):
        if kwargs["page_receipt"]["start_index"] == 2 and not crashed["done"]:
            crashed["done"] = True
            if when == "after":
                original(**kwargs)
            raise RuntimeError("simulated process crash")
        return original(**kwargs)

    projector.project_page = crashing
    with pytest.raises(RuntimeError):
        run(runtime, value, "crash", server)
    assert conn.execute("SELECT status FROM source_pack_runs").fetchone()[0] == "interrupted"
    result = run(runtime, value, "crash", server)
    assert result["status"] == "complete"
    assert result["sources"][0]["projection"]["completeness"] == "complete"
    assert conn.execute("SELECT count(*) FROM geospatial_features").fetchone()[0] == 5
    assert conn.execute("SELECT count(*) FROM geospatial_feature_revisions").fetchone()[0] == 5
    assert conn.execute(
        "SELECT count(*) FROM geospatial_geometries WHERE source_json LIKE '%acquired-feature%'"
    ).fetchone()[0] == 5


def test_provider_tombstones_remove_and_older_snapshots_do_not(pack):
    _, value, runtime, store = pack
    run(runtime, value, "t1", WfsServer([point("a", 0, 0), point("b", 1, 1)], timestamp="2026-09-20T00:00:00Z"))
    tomb = point("b", 1, 1)
    tomb["status"] = "deleted"
    run(runtime, value, "t2", WfsServer([point("a", 0, 0)], timestamp="2026-09-19T00:00:00Z"))
    assert current(store, "b")["current"]["lifecycle"] == "active", "older snapshot must not delete"
    older = store.collection_coverage("global", SCHOOLS, scopes=SCOPES)["latest_snapshot"]
    assert older["reasons"] == ["older_than_current_snapshot"]
    run(runtime, value, "t3", WfsServer([point("a", 0, 0), tomb], timestamp="2026-09-21T00:00:00Z"))
    removed = current(store, "b")["current"]
    assert removed["lifecycle"] == "removed" and removed["provenance"]["basis"] == "provider_tombstone"


def test_unsupported_geometry_is_quarantined_and_not_treated_as_absent(pack):
    conn, value, runtime, store = pack
    run(runtime, value, "one", WfsServer([point("a", 0, 0), point("m", 1, 1)]))
    multi = {"type": "Feature", "id": "m", "properties": {},
             "geometry": {"type": "MultiPoint", "coordinates": [[1, 1]]}}
    result = run(runtime, value, "two", WfsServer([point("a", 0, 0), multi]))
    assert result["sources"][0]["counts"]["quarantined"] == 1
    snapshot = result["sources"][0]["projection"]
    assert snapshot["states"] == {"rejected": 1, "unchanged": 1} and snapshot["removed"] == []
    quarantined = json.loads(conn.execute("SELECT record_json FROM source_pack_quarantine").fetchone()[0])
    assert quarantined["rejection"]["code"] == "unsupported_geometry"
    assert current(store, "m")["current"]["lifecycle"] == "active"


def test_projection_failure_blocks_completeness_until_retried(pack, monkeypatch):
    conn, value, runtime, store = pack
    original = spatial.transform_geometry

    def unavailable(*_args, **_kwargs):
        raise IntegrationError("transform_unavailable", "grid missing")

    monkeypatch.setattr(spatial, "transform_geometry", unavailable)
    result = run(runtime, value, "fail", WfsServer([point("a", 0, 0), point("b", 1, 1)]))
    snapshot = result["sources"][0]["projection"]
    assert snapshot["completeness"] == "partial" and snapshot["reasons"] == ["projection_failed"]
    assert conn.execute("SELECT count(*) FROM geospatial_features").fetchone()[0] == 0
    monkeypatch.setattr(spatial, "transform_geometry", original)
    retried = store.retry_failed(result["run_id"], principal_id="operator", scopes=SCOPES)
    assert retried["states"] == {"projected": 2}
    assert current(store, "a")["current"]["document_id"]


def test_points_within_holes_multipolygons_and_replay(tmp_path):
    conn = duckdb.connect(":memory:")
    store = GeospatialFeatureStore(conn)
    store.import_feature_collection(
        "team", {"type": "FeatureCollection", "features": [
            square("donut", 0, 0, 1000, hole=200), square("island", 5000, 0, 500, multi=True)]},
        provider="Team", collection="areas", source_crs="EPSG:25833", title_property="name",
        snapshot="complete", principal_id="alice", scopes=SCOPES,
    )
    store.import_feature_collection(
        "team", {"type": "FeatureCollection", "features": [
            point("inside", 100, 100), point("in-hole", 500, 500), point("outside", 2000, 2000),
            point("on-island", 5250, 250)]},
        provider="Team", collection="sites", source_crs="EPSG:25833", principal_id="alice",
        scopes=SCOPES, snapshot="complete",
    )
    donut = store.within("team", collection="sites", boundary_name="donut",
                         principal_id="alice", scopes=SCOPES)
    assert [item["native_id"] for item in donut["members"]] == ["inside"]
    assert donut["status"] == "complete" and donut["coverage"]["completeness"] == "complete"
    island = store.within("team", collection="sites",
                          boundary_feature_id=feature_key("Team", "areas", "island"),
                          principal_id="alice", scopes=SCOPES)
    assert [item["native_id"] for item in island["members"]] == ["on-island"]
    replay = store.replay_within("team", donut["receipt"]["receipt_id"], scopes=SCOPES)
    assert replay["deterministic"] and replay["recomputed_members"] == 1
    with pytest.raises(GeospatialError):
        store.within("team", collection="sites", principal_id="alice", scopes=SCOPES)


def test_namespaces_isolate_local_imports_and_ambiguous_names_need_review():
    conn = duckdb.connect(":memory:")
    store = GeospatialFeatureStore(conn)
    boundary = square("Mitte", 0, 0, 1000)
    store.import_feature_collection(
        "public", {"type": "FeatureCollection", "features": [boundary]}, provider="A",
        collection="districts", source_crs="EPSG:25833", title_property="name",
        principal_id="alice", scopes=SCOPES,
    )
    ortsteile = (FIXTURES / "ortsteile-mitte-subset.geojson").read_bytes()
    store.import_feature_collection(
        "team", ortsteile, provider="Geoportal Berlin", collection="alkis_ortsteile:ortsteile",
        source_crs="urn:ogc:def:crs:EPSG::25833", title_property="nam", principal_id="bob",
        scopes=SCOPES,
    )
    assert store.resolve_boundary("public", "Mitte", scopes=SCOPES)["status"] == "resolved"
    team = store.resolve_boundary("team", "mitte", scopes=SCOPES)
    assert team["status"] == "resolved", "namespaces do not see each other's imports"
    conn.execute("UPDATE geospatial_features SET namespace='global' WHERE collection='districts'")
    ambiguous = store.resolve_boundary("team", "Mitte", scopes=SCOPES)
    assert ambiguous["status"] == "needs_review" and len(ambiguous["candidates"]) == 2
    query = store.within("team", collection="sites", boundary_name="Mitte",
                         principal_id="bob", scopes=SCOPES)
    assert query["status"] == "needs_review" and query["members"] == [] and query["receipt"] is None
    with pytest.raises(GeospatialError):
        store.feature("other", feature_key("Geoportal Berlin", "alkis_ortsteile:ortsteile",
                                           "ortsteile.DEBE01YYK0000007"), scopes=SCOPES)
    with pytest.raises(GeospatialError):
        store.import_feature_collection("global", {"type": "FeatureCollection", "features": []},
                                        provider="A", collection="c", source_crs="EPSG:4326",
                                        principal_id="x", scopes=SCOPES)
    with pytest.raises(GeospatialError):
        store.import_feature_collection("team", {"type": "FeatureCollection", "features": []},
                                        provider="A", collection="c", source_crs="EPSG:4326",
                                        principal_id="x", scopes={"knowledge:geospatial:read"})
