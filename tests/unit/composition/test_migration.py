"""Migration of every bundle and projector to composition management (C09.2-C09.5)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all, bundle_id
from src.composition.contracts import validate_composition_manifest, validate_provider_set
from src.composition.lifecycle import CompositionCoordinator, registry_name
from src.composition.shadow import SHADOW_REPORT, provider_descriptors
from src.domains import pack_install
from src.domains import registry as domain_registry
from src.domains.pack_format import PackManifest
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackStore, load_source_packs
from src.kb import funding_bundle

ROOT = Path(__file__).resolve().parents[3]
# Each projector's authoritative record owner (C01.2 store-ownership table).
PROJECTOR_OWNERS = {
    "noesis-geospatial-feature-v1": "src.kb.geospatial_features",
    "noesis-product-record-v1": "src.kb.products",
    "noesis-product-safety-notice-v1": "src.kb.product_safety",
    "noesis-legal-record-v1": "src.kb.legal",
    "noesis-cultural-object-v1": "src.kb.cultural",
    "noesis-patent-part-v1": "src.kb.patents",
    "noesis-lei-part-v1": "src.kb.lei",
    "noesis-standard-catalogue-v1": "src.kb.standards",
    "noesis-transit-feed-v1": "src.kb.transit",
    "noesis-math-record-v1": "src.kb.mathematics",
    "noesis-clinical-record-v1": "src.kb.clinical_records",
    "noesis-ownership-part-v1": "src.kb.ownership_store",
    "noesis-procurement-record-v1": "src.kb.procurement_notices",
    "noesis-environment-record-v1": "src.kb.environment_store",
    "noesis-hazard-record-v1": "src.kb.hazards_store",
    "noesis-sanctions-record-v1": "src.kb.sanctions",
    "noesis-vulnerability-record-v1": "src.kb.vulnerabilities",
    "noesis-lobbying-record-v1": "src.kb.lobbying",
    "noesis-election-record-v1": "src.kb.elections",
    "noesis-public-finance-record-v1": "src.kb.public_finance",
    "noesis-demographic-series-v1": "src.kb.demographics",
    "noesis-development-finance-record-v1": "src.kb.development_finance",
    "noesis-bafin-notice-v1": "src.domains.market.bafin_notices",
    "noesis-astronomy-record-v1": "src.kb.astronomy_store",
    "noesis-weather-record-v1": "src.kb.weather_store",
    "noesis-trade-flow-record-v1": "src.kb.trade_flows",
    "noesis-energy-record-v1": "src.kb.energy_store",
    "noesis-humanitarian-record-v1": "src.kb.humanitarian_store",
    "noesis-housing-record-v1": "src.kb.housing",
    "noesis-surveillance-record-v1": "src.kb.surveillance",
    "noesis-clinical-medicines-record-v1": "src.kb.clinical_records",
    "noesis-substance-record-v1": "src.kb.substances_store",
    "noesis-engineering-safety-record-v1": "src.kb.engineering_safety_store",
    "noesis-material-record-v1": "src.kb.materials_store",
    "noesis-sports-record-v1": "src.kb.sports_store",
    "noesis-linguistic-record-v1": "src.kb.linguistics_store",
    "noesis-oss-ecosystem-record-v1": "src.kb.oss_ecosystem_store",
    "noesis-legislation-record-v1": "src.kb.legislation",
    "noesis-fisheries-record-v1": "src.kb.fisheries_store",
    "noesis-agrifood-record-v1": "src.kb.agrifood_store",
    "noesis-court-justice-record-v1": "src.kb.legal_dockets",
    "noesis-biodiversity-record-v1": "src.kb.biodiversity_store",
    "noesis-logistics-record-v1": "src.kb.logistics_series",
}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def _source_tables(conn):
    return {table: conn.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()
            for table in ("source_pack_versions", "source_pack_current", "source_pack_checkpoints",
                          "source_pack_watermarks", "source_pack_schedules")}


def _migrated(conn=None):
    """Every bundle installed, selected, activated and cut over, as the migration leaves it."""

    conn = conn or duckdb.connect(":memory:")
    coordinator = CompositionCoordinator(conn, legacy_config=lambda: ["news", "research"])
    bundles = adapt_all()
    for manifest in bundles.values():
        coordinator.install(manifest)
    for descriptor in provider_descriptors():
        coordinator.install(descriptor)
    for bundle, manifest in sorted(bundles.items()):
        coordinator.select(bundle, manifest["version"])
    receipt = coordinator.activate("migrate-all-bundles")
    for bundle in sorted(bundles):
        coordinator.cutover(bundle)
    coordinator.reconcile()
    return conn, coordinator, bundles, receipt


# ------------------------------------------------------------ C09.3 projectors


def test_every_projector_has_one_record_owner_one_descriptor_and_its_source_pack():
    assert set(PROJECTOR_OWNERS) == set(PROJECTORS)
    descriptors = provider_descriptors()
    assert validate_provider_set(descriptors) == []  # one owner per record type, no duplicate store
    configs = {}
    # Deployment packs plus the source packs a bundle ships with its manifest (packs/<bundle>/source_packs).
    for path in [*(ROOT / "config/source_packs").glob("*.json"), *(ROOT / "packs").glob("*/source_packs/*.json")]:
        configs[json.loads(path.read_text())["pack_id"]] = configs.get(json.loads(path.read_text())["pack_id"], "") + path.read_text()
    for schema, module in PROJECTOR_OWNERS.items():
        owners = [d for d in descriptors if any(s["store"] == module for s in d["stores"])]
        assert len(owners) == 1, (schema, [d["id"] for d in owners])
        declared = [ref["pack_id"] for ref in owners[0].get("source_packs") or []]
        assert any(schema in configs[pack] for pack in declared), (schema, declared)


def test_capabilities_without_an_implementation_stay_unbound():
    _, coordinator, bundles, _ = _migrated()
    plan = coordinator.active()["plan"]
    bound = {b["capability"] for b in plan["bindings"]}
    for manifest in bundles.values():
        for capability in manifest["contributes"].get("capabilities") or []:
            if capability.get("legacy_name"):  # v1 names: declared, never bound to a provider
                assert capability["id"] not in bound and "provider" not in capability


# ------------------------------------------------------------ C09.2 bundles


def test_every_bundle_migrates_with_one_authority_and_unchanged_source_pins(isolated_registry):
    conn = duckdb.connect(":memory:")
    store = SourcePackStore(conn)
    for manifest in load_source_packs(ROOT / "config/source_packs"):
        store.install(manifest, principal_id="operator", enable=True, now_ms=1)
    before = _source_tables(conn)
    _, coordinator, bundles, receipt = _migrated(conn)
    assert receipt["status"] == "published"
    assert _source_tables(conn) == before  # pins, cursors, schedules untouched
    authorities = dict(conn.execute("SELECT bundle, authority FROM composition_authority").fetchall())
    assert authorities == {bundle: "composition" for bundle in bundles}
    published = coordinator._active_names()
    assert {registry_name(m) for m in bundles.values()} == published


def test_shadow_diff_is_annotated_for_every_migrated_bundle():
    report = json.loads(SHADOW_REPORT.read_text())
    assert set(adapt_all()) <= set(report["disagreements"]) | {"funding-grants"}
    for bundle, items in report["disagreements"].items():
        for item in items:
            assert item["annotation"] != "unreviewed", (bundle, item)


# ------------------------------------------------------------ C09.4 funding


def test_funding_manifest_resolves_to_its_own_provider_plus_shared_providers(isolated_registry):
    manifest = json.loads((ROOT / "packs/funding-grants/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    _, coordinator, _, _ = _migrated()
    plan = coordinator.active()["plan"]
    funding = [b for b in plan["bindings"] if "funding-grants" in b["consumers"]]
    assert {b["provider"] for b in funding} == {"funding.core", "platform.research-projects",
                                                "platform.authored-reports", "platform.decisions",
                                                "platform.subscriptions", "platform.intake-sessions"}
    owned = {s["record_type"] for d in provider_descriptors() if d["id"] == "funding.core" for s in d["stores"]}
    shared = {s["record_type"] for d in provider_descriptors() if d["id"].startswith("platform.") for s in d["stores"]}
    assert not owned & shared  # no parallel project, report, decision or subscription store


def test_disabling_funding_is_a_selection_change_that_keeps_shared_providers(isolated_registry):
    conn, coordinator, _, _ = _migrated()
    assert funding_bundle.is_enabled(conn, "funding")
    status = funding_bundle.readiness(conn, "funding", scopes={"operator", "knowledge:funding:read"})
    assert status["composition"]["operations"] and {o["provider"] for o in status["composition"]["operations"]} >= {
        "funding.core", "platform.research-projects"}
    result = funding_bundle.set_enabled(conn, "funding", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    assert not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='funding_bundle_state'").fetchone()
    plan = coordinator.active()["plan"]
    assert "funding-grants" not in {p["id"] for p in plan["packs"]}
    sessions = next(b for b in plan["bindings"] if b["capability"] == "platform.session-artifacts")
    assert set(sessions["consumers"]) >= {"osint", "science"}  # other workflows keep their shared provider
    assert {d["id"] for d in coordinator.installed("provider")} >= {"platform.research-projects", "platform.subscriptions"}
    from src.kb.research_projects import ResearchProjectStore

    ResearchProjectStore(conn)  # the shared project store still initializes and serves
    again = funding_bundle.set_enabled(conn, "funding", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True and funding_bundle.is_enabled(conn, "other-namespace")


# ------------------------------------------------------------ C09.5 retired legacy paths


def test_no_retired_legacy_path_changes_enabled_state_independently_of_the_coordinator(isolated_registry):
    conn, coordinator, bundles, _ = _migrated()
    published = coordinator._active_names()

    def managed_state():
        return {n for n in domain_registry._ENABLED if coordinator.manages(n)}

    assert managed_state() == published
    names = {registry_name(m) for m in bundles.values()} | {bundle_id(n) for n in bundles}
    for name in sorted(names):
        for call in (domain_registry.enable_pack, domain_registry.disable_pack):
            try:
                call(name)
            except domain_registry.CompositionAuthorityError:
                pass
            assert managed_state() == published, (call.__name__, name)
        with pytest.raises(pack_install.PackInstallError):
            pack_install.install_manifest(PackManifest.from_dict({"pack_format": "noesis-pack-v1", "name": name,
                                                                  "version": "9.9.9"}))
        with pytest.raises(pack_install.PackInstallError):
            pack_install.uninstall(name)
    domain_registry.load_config()
    assert managed_state() == published
    before = funding_bundle.is_enabled(conn, "ns")
    with pytest.raises(funding_bundle.BundleError):
        funding_bundle.set_enabled(conn, "ns", not before, principal_id="analyst", scopes={"knowledge:read"})
    assert funding_bundle.is_enabled(conn, "ns") == before


# ------------------------------------------------------------ corporate ownership (#1860)


def test_ownership_manifest_resolves_to_its_own_provider_plus_shared_providers(isolated_registry):
    from src.kb import ownership_bundle

    manifest = json.loads((ROOT / "packs/corporate-ownership/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    _, coordinator, _, _ = _migrated()
    plan = coordinator.active()["plan"]
    bound = {(b["capability"], b["provider"]) for b in plan["bindings"] if "corporate-ownership" in b["consumers"]}
    assert {p for _, p in bound} == {"ownership.core", "market.lei", "platform.entity-identity",
                                     "platform.source-runtime", "platform.authored-reports"}
    assert ("market.legal-entities", "market.lei") in bound  # LEI records stay with their owner
    stores = {}
    for descriptor in provider_descriptors():
        for store in descriptor["stores"]:
            stores.setdefault(store["record_type"], []).append(descriptor["id"])
    assert all(len(owners) == 1 for owners in stores.values())  # one authority per store
    owned = {s["store"] for d in provider_descriptors() if d["id"] == "ownership.core" for s in d["stores"]}
    assert owned == {"src.kb.ownership_store", "src.kb.ownership_identity"}  # no second entity or LEI store
    assert ownership_bundle.BUNDLE["architecture"]["manifest"] == "packs/corporate-ownership/manifest.json"


def test_disabling_ownership_is_a_selection_change_that_keeps_shared_providers(isolated_registry):
    from src.kb import ownership_bundle

    conn, coordinator, _, _ = _migrated()
    assert ownership_bundle.is_enabled(conn, "ownership")
    status = ownership_bundle.readiness(conn, "ownership", scopes={"operator", "knowledge:ownership:read"})
    assert {o["provider"] for o in status["composition"]["operations"]} >= {"ownership.core", "market.lei"}
    result = ownership_bundle.set_enabled(conn, "ownership", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    assert result["receipt"]  # activation receipt for the selection change
    assert not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='ownership_bundle_state'").fetchone()
    plan = coordinator.active()["plan"]
    assert "corporate-ownership" not in {p["id"] for p in plan["packs"]}
    lei = next(b for b in plan["bindings"] if b["capability"] == "market.legal-entities")
    assert "market" in lei["consumers"]  # the market bundle keeps its LEI provider
    assert {d["id"] for d in coordinator.installed("provider")} >= {
        "platform.entity-identity", "platform.source-runtime", "platform.authored-reports", "market.lei"}
    from src.kb.entity_history import EntityHistoryStore
    from src.kb.lei import LeiStore

    LeiStore(conn)
    EntityHistoryStore(conn)  # shared owners still initialize and serve
    again = ownership_bundle.set_enabled(conn, "ownership", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True and again["receipt"]


# ------------------------------------------------------------ Climate and Environment (#1849, E12)


def test_climate_environment_manifest_resolves_to_its_own_provider_plus_shared_providers(isolated_registry):
    manifest = json.loads((ROOT / "packs/climate-environment/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    _, coordinator, _, _ = _migrated()
    plan = coordinator.active()["plan"]
    bound = [b for b in plan["bindings"] if "climate-environment" in b["consumers"]]
    assert {b["provider"] for b in bound} == {"environment.core", "geospatial.core", "geospatial.transit", "market.lei",
                                              "platform.source-runtime", "platform.subscriptions"}
    owned = {s["store"] for d in provider_descriptors() if d["id"] == "environment.core" for s in d["stores"]}
    others = {s["store"] for d in provider_descriptors() if d["id"] != "environment.core" for s in d["stores"]}
    assert not owned & others  # no parallel spatial, LEI, subscription or source store
    assert all(s.startswith("src.kb.environment_") for s in owned)
    pins = {(p["pack_id"], p.get("range")) for p in plan["source_packs"]}
    assert ("geospatial-berlin", "^1.1.0") in pins and ("climate-environment", "^1.0.0") in pins


def test_disabling_climate_environment_is_a_selection_change_that_keeps_geospatial(isolated_registry):
    from src.kb import environment_bundle
    from src.kb.geospatial_features import GeospatialFeatureStore

    conn, coordinator, _, _ = _migrated()
    assert environment_bundle.is_enabled(conn, "environment")
    status = environment_bundle.readiness(conn, "environment", scopes={"operator", "knowledge:environment:read"})
    assert {o["provider"] for o in status["composition"]["operations"]} >= {"environment.core"}
    # The Weather bundle (#2175) requires Climate & Environment's stations: while Weather is selected, deselecting
    # Climate & Environment keeps it in the plan as a retained dependency.
    retained = environment_bundle.set_enabled(conn, "environment", False, principal_id="operator",
                                              scopes={"operator"})
    assert retained["receipt"]["status"] == "published" and retained["enabled"] is True
    assert "climate-environment" in {p["id"] for p in coordinator.active()["plan"]["packs"]}
    coordinator.select("climate-environment", "^0.1.0")
    coordinator.activate("climate-environment:reselect")
    assert coordinator.disable("weather", "weather:disable")["status"] == "published"
    result = environment_bundle.set_enabled(conn, "environment", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    assert result["receipt"]["status"] == "published" and result["receipt"]["receipt_id"].startswith("activation:")
    assert not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='environment_bundle_state'").fetchone()
    plan = coordinator.active()["plan"]
    assert "climate-environment" not in {p["id"] for p in plan["packs"]}
    assert "geospatial" in {p["id"] for p in plan["packs"]}
    bindings = {b["capability"]: b for b in plan["bindings"]}
    assert bindings["geospatial.feature-query"]["provider"] == "geospatial.core"
    assert {d["id"] for d in coordinator.installed("provider")} >= {"geospatial.core", "market.lei", "environment.core"}
    GeospatialFeatureStore(conn)  # the shared spatial owner still initializes and serves
    again = environment_bundle.set_enabled(conn, "environment", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True and again["receipt"]["status"] == "published"
