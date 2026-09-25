"""Geospatial pack packaging and offline acceptance on the captured Berlin layers.

The acceptance journey replays both native WFS captures through the real
adapter, runtime, projection and query code, then checks every school's
computed district against the district the provider declares for it.  These
are fixture receipts; live-provider acceptance is recorded separately.
"""

from __future__ import annotations

import json
from collections import Counter

import duckdb
import pytest

from src.domains.pack_format import PackManifest, validate_manifest
from src.domains.pack_install import install_manifest, installed_packs, uninstall
from src.ingestion.geojson_features import feature_key
from src.ingestion.source_packs import (
    SUPPORTED_CONNECTORS,
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
)
from src.kb.geospatial_features import GeospatialFeatureStore, pack_readiness
from tests.unit.geospatial_pack_helpers import PUBLIC_DNS, ROOT, SCOPES, install, manifest

PACK_JSON = ROOT / "packs/geospatial/pack.json"
DISTRICTS = "alkis_bezirke:bezirksgrenzen"
SCHOOLS = "schulen:schulen"


def test_domain_pack_manifest_installs_reinstalls_and_uninstalls():
    data = json.loads(PACK_JSON.read_text())
    assert validate_manifest(data) == []
    try:
        first = install_manifest(PackManifest.from_dict(data))
        again = install_manifest(PackManifest.from_dict(data))
        assert first == again and installed_packs()["geospatial"] == "1.0.0"
        assert "points-inside-boundary-query" in first["capabilities"]
    finally:
        assert uninstall("geospatial")
    tools = {example["tool"] for example in data["query_examples"]}
    assert tools == {"query_geospatial_features_within", "search_geospatial_knowledge",
                     "calculate_spatial_relation"}
    assert all("semantics" in example for example in data["query_examples"])
    assert "road-network routing" in data["exclusions"]


def test_source_pack_declares_only_implemented_adapters_and_passes_offline_conformance():
    value = manifest()
    assert {item["connector"] for item in value["sources"]} <= SUPPORTED_CONNECTORS
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"]
    assert {item["source_id"]: item["records"] for item in result["sources"]} == {
        "berlin-bezirksgrenzen": 12, "berlin-schulen": 930}


def test_readiness_reports_install_terms_and_optional_dependencies():
    conn = duckdb.connect(":memory:")
    assert {b["code"] for b in pack_readiness(conn)["blockers"]} >= {"pack_not_installed"}
    SourcePackStore(conn).install(manifest(), principal_id="operator", enable=True, now_ms=1)
    from src.ingestion.source_pack_runtime import SourcePackRuntime

    runtime = SourcePackRuntime(conn)
    blocked = pack_readiness(conn)
    assert blocked["modes"] == {"fixture": "ready", "live": "blocked"}
    assert sum(b["code"] == "license_not_accepted" for b in blocked["blockers"]) == 2
    for source_id in ("berlin-bezirksgrenzen", "berlin-schulen"):
        runtime.accept_license("geospatial-berlin", source_id, principal_id="operator")
    ready = pack_readiness(conn)
    assert ready["modes"]["live"] == "ready"
    if not ready["transform"]["pyproj_available"]:
        assert any(b.get("dependency") == "pyproj" and b["severity"] == "degraded"
                   for b in ready["blockers"])
    SourcePackStore(conn).set_enabled("geospatial-berlin", False, principal_id="operator")
    assert pack_readiness(conn)["modes"] == {"fixture": "blocked", "live": "blocked"}


@pytest.fixture(scope="module")
def berlin():
    """One full replay of both captured layers (shared: it is the slow part)."""
    conn = duckdb.connect(":memory:")
    value, runtime = install(conn)
    adapters = runtime.fixture_adapters(value["pack_id"], ROOT)
    receipt = runtime.run(
        {"pack_id": value["pack_id"], "run_key": "fixture-replay", "operation": "features",
         "max_results": 5000, "max_bytes": 5_000_000, "timeout_ms": 120_000},
        principal_id="operator", adapters=adapters, dns_resolver=PUBLIC_DNS,
    )
    yield conn, value, runtime, receipt
    conn.close()


def test_both_layers_reach_the_spatial_store_as_complete_snapshots(berlin):
    conn, _, _, receipt = berlin
    assert receipt["status"] == "complete"
    by_source = {item["source_id"]: item for item in receipt["sources"]}
    assert by_source["berlin-bezirksgrenzen"]["counts"]["fetched"] == 12
    assert by_source["berlin-schulen"]["counts"]["fetched"] == 930
    for item in by_source.values():
        assert item["projection"]["completeness"] == "complete"
        assert item["projection"]["removed"] == []
        assert item["counts"]["quarantined"] == 0
    assert conn.execute(
        "SELECT geometry_type,count(*) FROM geospatial_feature_revisions GROUP BY 1 ORDER BY 1"
    ).fetchall() == [("MultiPolygon", 12), ("Point", 930)]


def test_every_school_falls_in_the_district_the_provider_declares(berlin):
    conn, _, _, _ = berlin
    store = GeospatialFeatureStore(conn)
    declared = Counter()
    for (properties,) in conn.execute(
        "SELECT r.properties_json FROM geospatial_features f JOIN geospatial_feature_revisions r USING(feature_id) WHERE f.collection=?",
        [SCHOOLS],
    ).fetchall():
        declared[json.loads(properties)["bezirk"]] += 1
    seen: set[str] = set()
    for district, expected in sorted(declared.items()):
        result = store.within("global", collection=SCHOOLS, boundary_name=district,
                              boundary_collection=DISTRICTS, principal_id="analyst",
                              scopes=SCOPES, limit=5000)
        assert result["status"] == "complete", district
        members = result["members"]
        mismatched = [m["native_id"] for m in members
                      if json.loads(conn.execute(
                          "SELECT properties_json FROM geospatial_feature_revisions WHERE revision_id=?",
                          [m["revision_id"]]).fetchone()[0])["bezirk"] != district]
        assert mismatched == [], (district, mismatched)
        assert len(members) == expected, district
        seen.update(m["native_id"] for m in members)
        member = members[0]
        assert member["document_id"] and member["provenance"]["page"]["response_sha256"]
        assert member["provenance"]["attribution"].startswith("Geoportal Berlin")
        assert member["precision"]["status"] == "unknown"
    assert len(seen) == 930 and sum(declared.values()) == 930


def test_mitte_receipt_replays_and_names_resolve_reviewably(berlin):
    conn, _, _, _ = berlin
    store = GeospatialFeatureStore(conn)
    result = store.within("global", collection=SCHOOLS, boundary_name="Mitte",
                          boundary_collection=DISTRICTS, principal_id="analyst", scopes=SCOPES)
    assert result["total_members"] == 86
    assert result["boundary"]["feature_id"] == feature_key("Geoportal Berlin", DISTRICTS,
                                                           "bezirksgrenzen.11000001")
    assert result["coverage"]["last_complete_snapshot"]["provider_timestamp"].startswith("2026-09-25")
    replay = store.replay_within("global", result["receipt"]["receipt_id"], scopes=SCOPES)
    assert replay["deterministic"] and replay["recomputed_members"] == 86
    assert store.resolve_boundary("global", "Atlantis", scopes=SCOPES)["status"] == "not_found"


def test_pack_disable_stops_acquisition_without_deleting_shared_evidence(berlin):
    conn, value, runtime, _ = berlin
    store = SourcePackStore(conn, initialize=False)
    store.set_enabled(value["pack_id"], False, principal_id="operator")
    try:
        with pytest.raises(SourcePackError) as caught:
            runtime.run({"pack_id": value["pack_id"], "run_key": "while-disabled",
                         "operation": "features"}, principal_id="operator",
                        adapters={}, dns_resolver=PUBLIC_DNS)
        assert caught.value.code == "pack_disabled"
        features = GeospatialFeatureStore(conn, initialize=False)
        coverage = features.collection_coverage("global", SCHOOLS, scopes=SCOPES)
        assert coverage["active"] == 930
        # Other packs' spatial use is unaffected: the builtin gazetteer still resolves.
        assert features.geo.search("global", scopes=SCOPES, contains_point=[13.4, 52.52])
    finally:
        store.set_enabled(value["pack_id"], True, principal_id="operator")
    reinstalled = store.install(manifest(), principal_id="operator", enable=True, now_ms=99)
    assert reinstalled["enabled"] is True
