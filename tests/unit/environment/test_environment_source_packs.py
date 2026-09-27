"""E03-E07 through the source-pack runtime and E06 as a geospatial-berlin upgrade."""

import json

import duckdb
import pytest

from src.ingestion.source_packs import SUPPORTED_CONNECTORS, SourcePackConformance, validate_source_pack
from src.kb.environment_store import EnvironmentStore
from src.kb.geospatial_features import GeospatialFeatureStore
from tests.unit.environment import fixture_builder, harness
from tests.unit.environment.harness import NS, PUBLIC_DNS, ROOT


def test_fixtures_are_pinned_and_in_sync_with_the_authored_raw_files():
    regenerated = fixture_builder.build(write=False)
    for path, text in regenerated.items():
        assert path.read_text(encoding="utf-8") == text, path
    for manifest_path in (harness.PACK, harness.UPGRADE):
        result = SourcePackConformance(ROOT).offline(validate_source_pack(json.loads(manifest_path.read_text())))
        assert result["valid"], [s for s in result["sources"] if not s["valid"]]


def test_pack_declares_every_source_with_live_state_credentials_and_a_schedule():
    raw = json.loads(harness.PACK.read_text())
    manifest = harness.pack_manifest()
    assert manifest["pack_id"] == "climate-environment" and {s["connector"] for s in manifest["sources"]} == {"environment"}
    assert "environment" in SUPPORTED_CONNECTORS
    providers = {s["environment"]["provider"]: s for s in manifest["sources"]}
    assert set(providers) == {"openaq", "uba", "entsoe", "smard", "eea-industry", "eu-ets", "open-meteo-archive",
                              "open-meteo-forecast", "dwd"}
    from src.ingestion.environment_providers import LIVE_VERIFICATION

    for provider, source in providers.items():  # the pack states the dated live state, never more
        assert source["environment"]["live_verification"] == LIVE_VERIFICATION[provider]["status"] == "blocked"
    assert providers["openaq"]["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_OPENAQ_API_KEY"}
    assert providers["entsoe"]["auth"]["secret_ref"] == "NOESIS_ENTSOE_SECURITY_TOKEN"
    assert raw["defaults"]["schedule"] == {"kind": "interval", "interval_s": 86400}  # source-pack schedule, no new scheduler
    assert all(s["mapping"]["target_schema"] == "noesis-environment-record-v1" for s in manifest["sources"])


@pytest.fixture(scope="module")
def runtime_world():
    env = harness.Env()
    env.install_berlin()
    env.install_environment_pack()
    receipt = env.run_sources("climate-environment", [s["source_id"] for s in harness.pack_manifest()["sources"]],
                              "fixture-replay", operation="observe")
    return env, receipt


def test_runtime_acquisition_has_cursors_budgets_and_receipts(runtime_world):
    env, receipt = runtime_world
    assert receipt["status"] == "complete"
    by_source = {s["source_id"]: s for s in receipt["sources"]}
    assert by_source["uba-berlin"]["counts"]["pages"] == 4  # stations, components and two measures requests
    assert by_source["uba-berlin"]["cursor"]["end"] is None and by_source["uba-berlin"]["output_hash"]
    runtime = env.runtime()
    assert runtime.replay(receipt["run_id"])["matched"]
    manifest = harness.pack_manifest()
    for source in manifest["sources"]:
        assert by_source[source["source_id"]]["output_hash"], source["source_id"]
    store = EnvironmentStore(env.conn)
    stations = store.records(NS, scopes=harness.SCOPES, record_type="station")
    assert {s["provider"] for s in stations} == {"openaq", "uba", "dwd"}
    assert all(s["place_id"] and s["geometry_id"] for s in stations)


def test_runtime_rerun_projects_idempotently(runtime_world):
    env, _ = runtime_world
    before = env.conn.execute("SELECT count(*) FROM environment_record_revisions").fetchone()[0]
    vintages = env.conn.execute("SELECT count(*) FROM environment_vintages").fetchone()[0]
    again = env.run_sources("climate-environment", ["uba-berlin", "entsoe-de-lu"], "fixture-replay-2", operation="observe")
    assert again["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM environment_record_revisions").fetchone()[0] == before
    assert env.conn.execute("SELECT count(*) FROM environment_vintages").fetchone()[0] == vintages


def test_runtime_budget_and_credentials_fail_closed(runtime_world):
    env, _ = runtime_world
    runtime = env.runtime()
    adapters = runtime.fixture_adapters("climate-environment", ROOT)
    small = runtime.run({"pack_id": "climate-environment", "run_key": "tiny-budget", "operation": "observe",
                         "source_ids": ["uba-berlin"], "max_results": 1, "max_bytes": 5_000_000, "timeout_ms": 120_000},
                        principal_id="operator", adapters={"uba-berlin": adapters["uba-berlin"]}, dns_resolver=PUBLIC_DNS)
    source = small["sources"][0]
    assert source["status"] == "failed" and source["failure"]["code"] == "budget_exhausted"
    preflight = runtime.preflight({"pack_id": "climate-environment", "run_key": "no-secret", "operation": "observe",
                                   "source_ids": ["openaq-berlin-mitte"]}, secret_available=lambda _ref: False,
                                  dns_resolver=PUBLIC_DNS)
    assert preflight["sources"][0]["failures"] == ["credential_missing"]


def test_umweltatlas_layers_arrive_through_the_existing_wfs_path_as_a_source_pack_upgrade():
    env = harness.Env()
    env.install_berlin()
    installed_before = env.conn.execute("SELECT version, manifest_hash FROM source_pack_versions WHERE pack_id='geospatial-berlin'").fetchall()
    impact, receipt, run = env.upgrade_umweltatlas()
    changes = impact["preview"]["changes"]["sources"]
    assert [s["source_id"] for s in changes["added"]] == ["umweltatlas-klimafunktion", "umweltatlas-laerm-lden",
                                                          "umweltatlas-umweltzone"]
    assert changes["changed"] == [] and changes["removed"] == []  # existing Berlin sources untouched
    assert {s["capabilities"]["connector"] for s in changes["added"]} == {"wfs"}  # no new acquisition code path
    assert receipt["installed_version"] == "1.1.0" and receipt["candidate_version"] == "1.2.0"
    assert receipt["retained_old_version"]
    versions = env.conn.execute("SELECT version, manifest_hash FROM source_pack_versions WHERE pack_id='geospatial-berlin' "
                                "ORDER BY version").fetchall()
    assert versions[0] == installed_before[0]  # the 1.1.0 pin stays retained and valid
    assert run["status"] == "complete"
    store = GeospatialFeatureStore(env.conn)
    for collection, count in (("ua_umweltzone:umweltzone", 1), ("ua_stratlaerm_2022:laerm_lden", 2),
                              ("ua_klimaanalyse_2022:klimafunktion", 2)):
        coverage = store.collection_coverage("global", collection, scopes={"knowledge:geospatial:read"})
        assert coverage["active"] == count and coverage["completeness"] == "complete", collection
    feature_id = env.conn.execute("SELECT feature_id FROM geospatial_features WHERE collection='ua_umweltzone:umweltzone'").fetchone()[0]
    feature = store.feature("global", feature_id, scopes={"knowledge:geospatial:read"})
    assert feature["current"]["source_crs"] == "urn:ogc:def:crs:EPSG::25833"
    assert feature["current"]["provenance"]["page"]["response_sha256"]
    again = env.run_sources("geospatial-berlin", ["umweltatlas-umweltzone"], "umweltatlas-again", operation="features")
    assert again["sources"][0]["projection"]["states"] == {"unchanged": 1}


def test_existing_composition_pins_stay_compatible_with_the_upgrade():
    from src.composition.contracts import satisfies

    geospatial = json.loads((ROOT / "packs/geospatial/composition.json").read_text())
    for pin in geospatial["contributes"]["source_packs"]:
        assert pin["pack_id"] == "geospatial-berlin" and satisfies("1.2.0", pin["range"])
    manifest = json.loads((ROOT / "packs/climate-environment/manifest.json").read_text())
    berlin = next(p for p in manifest["contributes"]["source_packs"] if p["pack_id"] == "geospatial-berlin")
    assert berlin == {"pack_id": "geospatial-berlin", "version": "1.1.0", "range": "^1.1.0"}
    base = json.loads((ROOT / "config/source_packs/geospatial.json").read_text())
    upgrade = json.loads(harness.UPGRADE.read_text())
    assert upgrade["sources"][: len(base["sources"])] == base["sources"]


def test_namespace_projection_is_isolated(runtime_world):
    env, _ = runtime_world
    store = EnvironmentStore(env.conn)
    other = {"knowledge:environment:read", "namespace:other:read"}
    assert store.records("other", scopes=other) == []
    rid = store.records(NS, scopes=harness.SCOPES, record_type="station")[0]["record_id"]
    from src.kb.environment_store import EnvironmentStoreError

    with pytest.raises(EnvironmentStoreError) as error:
        store.record("other", rid, scopes=other)
    assert error.value.code == "not_found"
    with pytest.raises(EnvironmentStoreError) as denied:
        store.records(NS, scopes=other)
    assert denied.value.code == "unauthorized"
    assert duckdb.connect().execute("SELECT 1").fetchone() == (1,)
