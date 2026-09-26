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
    "noesis-legal-record-v1": "src.kb.legal",
    "noesis-cultural-object-v1": "src.kb.cultural",
    "noesis-patent-part-v1": "src.kb.patents",
    "noesis-lei-part-v1": "src.kb.lei",
    "noesis-standard-catalogue-v1": "src.kb.standards",
    "noesis-transit-feed-v1": "src.kb.transit",
    "noesis-math-record-v1": "src.kb.mathematics",
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
    configs = {json.loads(p.read_text())["pack_id"]: p.read_text() for p in (ROOT / "config/source_packs").glob("*.json")}
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
    assert not any(b["provider"].startswith("energy") for b in plan["bindings"])  # energy has no implementation


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
    assert set(adapt_all()) - {"energy"} <= set(report["disagreements"]) | {"funding-grants"}
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
