"""Migrated bundles and source-pack projectors (C09.2, C09.3) and retired legacy paths (C09.5)."""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path

import pytest

from src.composition import bundles
from src.composition import contracts as c
from src.composition import lifecycle as lc
from src.composition.deployment import candidates, reference_plan
from src.composition.migration import migrate, source_state
from src.domains import pack_install
from src.domains import registry as domain_registry
from src.domains.pack_format import PackManifest
from src.ingestion.source_pack_runtime import PROJECTORS
from src.composition.workflows import templates_of
from tests.unit.composition.journey import NAMESPACE, World, consumer_manifest, location_template

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def isolated():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY,
             dict(pack_install._INSTALLED), dict(pack_install._TEMPLATES))
    lc.reset_runtime()
    yield
    lc.install_authority(None)
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])
    pack_install._INSTALLED.clear()
    pack_install._INSTALLED.update(saved[3])
    pack_install._TEMPLATES.clear()
    pack_install._TEMPLATES.update(saved[4])
    lc.reset_runtime()


# --------------------------------------------------------------------------- #
# Descriptors and ownership
# --------------------------------------------------------------------------- #

def test_generated_descriptors_and_overlays_are_current():
    assert bundles.stale() == []


def test_every_provider_validates_and_no_record_kind_or_table_has_two_owners():
    providers = c.load_providers()
    kinds: dict[str, str] = {}
    tables: dict[str, str] = {}
    for descriptor in providers:
        for store in descriptor["stores"]:
            assert kinds.setdefault(store["record_kind"], descriptor["provider_id"]) == descriptor["provider_id"]
            for table in store["tables"]:
                assert tables.setdefault(table, descriptor["provider_id"]) == descriptor["provider_id"], table
    # The whole shipped set resolves: no conflicting store owner, no undeclared binding.
    plan = reference_plan()
    assert {p["provider_id"] for p in plan["providers"]} >= set(bundles.OWNERSHIP)


def test_every_projector_has_one_named_owner_with_a_store_and_source_declaration():
    assert set(bundles.PROJECTOR_OWNERS) == set(PROJECTORS)
    providers = {d["provider_id"]: d for d in c.load_providers()}
    source_configs = {}
    for path in (ROOT / "config/source_packs").glob("*.json"):
        text = path.read_text(encoding="utf-8")
        source_configs[json.loads(text)["pack_id"]] = text
    for schema, owner in bundles.PROJECTOR_OWNERS.items():
        descriptor = providers[owner]
        assert descriptor["stores"], owner
        # The owner declares a source pack whose manifest maps into this schema.
        declared = [ref["pack_id"] for ref in descriptor.get("source_packs", [])]
        assert any(f'"{schema}"' in source_configs[pack] for pack in declared), (schema, owner)
        # The projector factory builds the owner's own store module: no duplicate store.
        factory_source = inspect.getsource(PROJECTORS[schema])
        module = factory_source.split("from ", 1)[1].split(" import", 1)[0]
        assert module in descriptor["implementation"]["identity"], (schema, module)
        importlib.import_module(module)


def test_declared_capability_labels_are_bound_aliased_or_marked_unbound():
    capability_map = json.loads(bundles.CAPABILITY_MAP.read_text(encoding="utf-8"))["capabilities"]
    manifests = {m["name"]: m for m in candidates()["manifests"]}
    offered = c.provided_capabilities(c.load_providers())
    for label, info in capability_map.items():
        for declaring in info["declaring_bundles"]:
            if not declaring.startswith("packs/"):
                continue
            manifest = manifests[declaring.split("/", 1)[1]]
            aliases = manifest.get("aliases", {}).get("capability_labels", {})
            unbound = manifest.get("metadata", {}).get("unbound_capability_labels", {})
            assert label in aliases or label in unbound, (declaring, label)
            if label in aliases:
                assert aliases[label] in offered, (declaring, label, aliases[label])


def test_code_registered_twins_keep_their_domain_pack_registration():
    from src.composition.identifiers import code_registered_packs

    code = code_registered_packs()
    for name in ("legal", "economics", "political", "technology"):
        manifest = next(m for m in candidates()["manifests"] if m["name"] == name)
        registration = lc._registration(manifest)
        assert registration["kind"] == "code-registered"
        domain_registry._REGISTRY.pop(name, None)
        lc._apply_registration(registration)
        assert domain_registry.get_pack(name) is code[name]
        assert list(domain_registry.get_pack(name).route_modules) == list(code[name].route_modules)


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #

def _migrated(enabled=("geospatial", "osint", "science", "legal", "market", "research", "funding-grants")):
    world = World()
    coordinator = lc.Coordinator(world.conn)
    before = source_state(world.conn)
    result = migrate(world.conn, principal_id="operator", enabled=enabled, coordinator=coordinator)
    assert source_state(world.conn) == before
    return world, coordinator, result


def test_migration_selects_legacy_enabled_bundles_and_cuts_every_bundle_over():
    world, coordinator, result = _migrated()
    shipped = {m["name"] for m in candidates()["manifests"]}
    assert result["activation"] == "applied" and result["source_state_unchanged"] is True
    assert set(result["cut_over"]) == shipped
    assert set(result["authorities"].values()) == {"composition"}
    assert set(coordinator.store.selection()) == {"geospatial", "osint", "science", "legal", "market",
                                                   "research", "funding-grants"}
    assert set(lc.runtime().bundles) >= set(coordinator.store.selection())
    for name in shipped - set(coordinator.store.selection()):
        assert not domain_registry.is_pack_enabled(name), name
    # Idempotent: a second run changes nothing.
    again = migrate(world.conn, principal_id="operator", enabled=result["selected"], coordinator=coordinator)
    assert again["selected"] == [] and set(again["cut_over"]) == shipped


def test_migration_leaves_source_pins_and_cursors_unchanged_after_a_run():
    template = location_template("probe", "probe.location-investigation")
    world = World(consumers={"probe": consumer_manifest("probe", template)})
    manifest = next(m for m in world.coordinator.store.manifests() if m["name"] == "probe")
    (tmpl,) = templates_of(manifest)
    run = world.dispatcher().start(tmpl, namespace=NAMESPACE, consumer="probe@1.0.0",
                                   parameters={"place": "Mitte"}, run_key="before-migration",
                                   principal_id="analyst", session_id=world.session("m")["session_id"])
    assert run["status"] == "completed", run
    before = source_state(world.conn)
    assert before["source_pack_checkpoints"] and before["source_pack_current"]
    result = migrate(world.conn, principal_id="operator", enabled={"geospatial", "osint", "science"},
                     coordinator=world.coordinator)
    assert result["activation"] == "applied"
    assert source_state(world.conn) == before


# --------------------------------------------------------------------------- #
# C09.5: retired legacy paths cannot change enabled state on their own
# --------------------------------------------------------------------------- #

def _enabled_matches_coordinator(coordinator, names):
    selected = set(coordinator.store.selection())
    for name in names:
        assert domain_registry.is_pack_enabled(name) == (name in selected and name in lc.runtime().bundles), name


def test_no_retired_legacy_path_changes_enabled_state_independently():
    world, coordinator, result = _migrated(enabled=("geospatial", "osint", "legal"))
    names = set(result["cut_over"])
    _enabled_matches_coordinator(coordinator, names)
    # Registry enable/disable delegate to the coordinator (journaled activations).
    domain_registry.enable_pack("market")
    assert "market" in coordinator.store.selection()
    domain_registry.disable_pack("legal")
    assert "legal" not in coordinator.store.selection()
    _enabled_matches_coordinator(coordinator, names)
    # Installing or uninstalling through the legacy installer is refused.
    v1 = json.loads((ROOT / "packs/products/pack.json").read_text(encoding="utf-8"))
    with pytest.raises(domain_registry.CompatibilityError):
        pack_install.install_manifest(PackManifest.from_dict(v1))
    assert "products" not in pack_install._INSTALLED
    with pytest.raises(domain_registry.CompatibilityError):
        pack_install.uninstall("osint")
    # Reloading the legacy config leaves managed bundles to the coordinator.
    domain_registry.load_config()
    _enabled_matches_coordinator(coordinator, names)
    # The funding flag is a selection change through the coordinator.
    from src.kb import funding_bundle

    assert funding_bundle.authority() == "composition"
    funding_bundle.set_enabled(world.conn, "global", True, principal_id="operator", scopes={"operator"})
    assert "funding-grants" in coordinator.store.selection()
    _enabled_matches_coordinator(coordinator, names)
    assert not world.conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='funding_bundle_state'").fetchone()
    # Compatibility rollback hands one bundle back to the legacy path, explicitly.
    coordinator.rollback("market", "rollback-market", principal_id="operator")
    assert coordinator.store.authority()["market"]["authority"] == "legacy"
    assert domain_registry.is_pack_enabled("market")
