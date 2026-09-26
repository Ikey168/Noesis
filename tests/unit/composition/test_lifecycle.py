"""Durable selection, journal, generation switch, retention and authority (C05, #1819-#1823)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.composition import contracts as c
from src.composition import lifecycle as lc
from src.composition.readiness import catalog_state
from src.composition.store import CompositionStore
from src.domains import pack_install
from src.domains import registry as domain_registry
from src.kb.geospatial import GeospatialStore

GEO = next(p for p in c.load_providers() if p["provider_id"] == "noesis.geospatial")
CONTRACTS = {"noesis-spatial-result-v1": ["1.0.0"], "noesis-geospatial-geometry-v2": ["2.0.0"],
             "noesis-geocode-resolution-v1": ["1.0.0"]}


def bundle(name, version="1.0.0", *, requires=(), providers=(), template=None, capability_label=None):
    v1 = {"pack_format": "noesis-pack-v1", "name": name, "version": version,
          "description": f"{name} {version}",
          "capabilities": [capability_label or f"{name}-capability-{version}"]}
    if template:
        v1["provisioning_templates"] = [{"name": template, "description": "kg", "sources": []}]
    manifest = c.adapt_v1(v1)
    manifest.pop("manifest_hash")
    manifest["requires"] = [{"capability": cap, "range": "^1.0.0"} for cap in requires]
    if providers:
        manifest["contributes"]["providers"] = [{"provider_id": p, "version": v} for p, v in providers]
    return c.validate_manifest(manifest)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY,
             dict(pack_install._INSTALLED), dict(pack_install._TEMPLATES))
    lc.reset_runtime()
    yield
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


@pytest.fixture
def world():
    conn = duckdb.connect(":memory:")
    GeospatialStore(conn)
    coordinator = lc.Coordinator(conn, contracts=CONTRACTS)
    store = coordinator.store
    store.install_provider(GEO, principal_id="operator")
    store.install_manifest(bundle("geospatial", providers=[("noesis.geospatial", "1.0.0")],
                                  template="geo_kg"), principal_id="operator")
    store.install_manifest(bundle("osint", requires=["spatial.relation"]), principal_id="operator")
    store.install_manifest(bundle("research", requires=["spatial.relation", "spatial.resolution-record"]),
                           principal_id="operator")
    return conn, coordinator


def select_all(store, *names):
    for name in names:
        store.select(name, "^1.0.0", principal_id="operator")


def restart(conn, **kwargs):
    lc.reset_runtime()
    domain_registry.set_authority(None)
    return lc.Coordinator(conn, contracts=CONTRACTS, **kwargs)


# --------------------------------------------------------------------------- #
# C05.1 durable install / select / resolve
# --------------------------------------------------------------------------- #

def test_install_select_resolve_are_separate_and_do_not_touch_the_legacy_registry(world, monkeypatch):
    conn, coordinator = world
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    enabled_before = set(domain_registry._ENABLED)
    store = coordinator.store
    assert {m["name"] for m in store.manifests()} == {"geospatial", "osint", "research"}
    assert store.selection() == {}
    select_all(store, "osint")
    assert set(store.selection()) == {"osint"}
    assert store.plan_for_selection(store.selection()) is None
    preview = coordinator.preview()
    digest = store.store_plan(preview["plan"], store.selection())
    assert store.plan_for_selection(store.selection())["digest"] == digest
    assert store.active_generation() is None  # resolved is not activated
    assert set(domain_registry._ENABLED) == enabled_before
    reopened = CompositionStore(conn)
    assert reopened.selection() == store.selection()
    assert reopened.plan(digest)["digest"] == digest


# --------------------------------------------------------------------------- #
# C05.2 journal, atomic switch, reconciliation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stage", ["previewed", "staged", "verified", "published"])
def test_crash_after_each_stage_keeps_a_working_generation_after_restart(world, stage):
    conn, coordinator = world
    select_all(coordinator.store, "osint")
    first = coordinator.activate("gen-1", principal_id="operator")
    assert first["status"] == "applied" and first["new_generation"] == 1
    coordinator.store.select("research", "^1.0.0", principal_id="operator")

    def crash(at):
        if at == stage:
            raise lc.Crash(at)

    crashing = lc.Coordinator(conn, contracts=CONTRACTS, fault=crash)
    with pytest.raises(lc.Crash):
        crashing.activate("gen-2", principal_id="operator")
    after = restart(conn)
    report = after.reconcile()
    expected = 2 if stage == "published" else 1
    assert report["generation"] == expected
    assert lc.runtime().generation == expected
    consumers = {b["consumer"] for b in lc.runtime().bindings()}
    assert consumers == ({"osint@1.0.0", "research@1.0.0"} if expected == 2 else {"osint@1.0.0"})
    if stage != "published":
        assert report["abandoned"]
        stages = {r[0] for r in conn.execute(
            "SELECT stage FROM composition_journal WHERE activation_id=?", [report["abandoned"][0]]).fetchall()}
        assert "abandoned" in stages and "published" not in stages


def test_reconcile_is_idempotent_and_rebuilds_identical_bindings_from_an_empty_journal(world):
    conn, coordinator = world
    select_all(coordinator.store, "osint", "research")
    coordinator.store.set_authority("geospatial", "composition", principal_id="operator")
    coordinator.activate("gen-1", principal_id="operator")
    before = (lc.runtime().generation, json.dumps(lc.runtime().plan, sort_keys=True),
              sorted(lc.runtime().providers), dict(lc.runtime().bundles))
    registered = dict(domain_registry._REGISTRY)
    conn.execute("DELETE FROM composition_journal")
    for _ in range(3):
        restart(conn).reconcile()
    after = (lc.runtime().generation, json.dumps(lc.runtime().plan, sort_keys=True),
             sorted(lc.runtime().providers), dict(lc.runtime().bundles))
    assert after == before
    assert set(domain_registry._REGISTRY) == set(registered)
    assert list(pack_install._TEMPLATES).count("geo_kg") == 1


# --------------------------------------------------------------------------- #
# C05.3 coordinator
# --------------------------------------------------------------------------- #

def test_replacing_a_composed_pack_version_never_leaves_it_unregistered(world):
    conn, coordinator = world
    store = coordinator.store
    select_all(store, "osint")
    store.set_authority("geospatial", "composition", principal_id="operator")
    coordinator.activate("gen-1", principal_id="operator")
    assert domain_registry.get_pack("geospatial").description == "geospatial 1.0.0"
    store.install_manifest(bundle("geospatial", "1.1.0", providers=[("noesis.geospatial", "1.0.0")]),
                           principal_id="operator")
    observed = []

    def watch(at):
        pack = domain_registry.get_pack("geospatial")
        observed.append((at, pack.description, domain_registry.is_pack_enabled("geospatial")))

    store.select("osint", "^1.0.0", principal_id="operator")
    lc.Coordinator(conn, contracts=CONTRACTS, fault=watch).activate("gen-2", principal_id="operator", retain=False)
    assert observed[:3] == [(s, "geospatial 1.0.0", True) for s in ("previewed", "staged", "verified")]
    assert observed[3] == ("published", "geospatial 1.0.0", True)
    assert domain_registry.get_pack("geospatial").description == "geospatial 1.1.0"
    assert domain_registry.is_pack_enabled("geospatial")


def test_preview_lists_affected_consumers_for_a_provider_change(world):
    conn, coordinator = world
    select_all(coordinator.store, "osint", "research")
    coordinator.activate("gen-1", principal_id="operator")
    revised = json.loads(json.dumps(GEO))
    revised.pop("descriptor_hash")
    revised["version"] = "1.1.0"
    for capability in revised["capabilities"]:
        capability["version"] = "1.1.0"
    coordinator.store.install_provider(revised, principal_id="operator")
    preview = coordinator.preview(retain=False)
    assert preview["affected_consumers"] == ["osint@1.0.0", "research@1.0.0"]
    assert {c["after"] for c in preview["binding_changes"]} == {"noesis.geospatial@1.1.0"}
    assert coordinator.preview(retain=True)["affected_consumers"] == []


def test_every_publish_and_failed_activation_emits_a_valid_receipt(world):
    conn, coordinator = world
    select_all(coordinator.store, "osint")
    applied = coordinator.activate("gen-1", principal_id="operator")
    rival = json.loads(json.dumps(GEO))
    rival.pop("descriptor_hash")
    rival["provider_id"] = "example.rival"
    rival["stores"] = []
    for capability in rival["capabilities"]:
        capability["record_kinds"] = []
        capability["effect"] = "read-only"
    coordinator.store.install_provider(rival, principal_id="operator")
    failed = coordinator.activate("gen-2", principal_id="operator", retain=False)
    plans = coordinator.store.generation_plans()
    for receipt in (applied, failed):
        c.validate_receipt({k: v for k, v in receipt.items() if k != "idempotent"}, generation_plans=plans)
    assert failed["status"] == "failed" and failed["error"]["code"] == "ambiguous_selection"
    assert failed["recovery_status"] == "previous-generation-retained"
    assert coordinator.store.active_generation()["generation"] == 1
    assert coordinator.activate("gen-1", principal_id="operator")["idempotent"] is True


# --------------------------------------------------------------------------- #
# C05.4 retention, shutdown, uninstall
# --------------------------------------------------------------------------- #

def test_disabling_osint_keeps_the_shared_provider_for_research(world):
    conn, coordinator = world
    select_all(coordinator.store, "osint", "research")
    coordinator.activate("gen-1", principal_id="operator")
    receipt = coordinator.disable("osint", "disable-osint", principal_id="operator")
    assert receipt["status"] == "applied" and receipt["operation"] == "disable"
    assert ("noesis.geospatial", "1.0.0") in lc.runtime().providers
    assert {b["consumer"] for b in lc.runtime().bindings()} == {"research@1.0.0"}
    readiness = coordinator.readiness(principal_id="analyst", scopes={"operator"})
    assert {o["state"] for o in readiness["operations"]} == {"ready"}


def test_pinned_active_run_retains_its_provider(world):
    conn, coordinator = world
    select_all(coordinator.store, "osint")
    coordinator.activate("gen-1", principal_id="operator")
    coordinator.store.pin_run("run-1", coordinator.store.active_plan()["digest"], owner="analyst")
    coordinator.disable("osint", "disable-osint", principal_id="operator")
    assert ("noesis.geospatial", "1.0.0") in lc.runtime().providers
    coordinator.store.release_run("run-1")
    coordinator.apply_active()
    assert ("noesis.geospatial", "1.0.0") not in lc.runtime().providers


def test_administrative_shutdown_reports_consumers_and_blocks_their_operations(world):
    conn, coordinator = world
    select_all(coordinator.store, "osint", "research")
    coordinator.activate("gen-1", principal_id="operator")
    report = coordinator.shutdown_provider("noesis.geospatial", "shutdown-1", reason="maintenance",
                                           principal_id="operator")
    assert report["affected_consumers"] == ["osint@1.0.0", "research@1.0.0"]
    readiness = coordinator.readiness(principal_id="analyst", scopes={"operator"})
    for op in readiness["operations"]:
        assert {b["kind"] for b in op["blockers"]} == {"provider-shutdown"}
        assert catalog_state(op)[0] == "unavailable"


def test_uninstall_refuses_in_use_packs_and_never_touches_evidence(world):
    conn, coordinator = world
    select_all(coordinator.store, "osint")
    coordinator.activate("gen-1", principal_id="operator")
    evidence = conn.execute("SELECT count(*) FROM geospatial_geometries").fetchone()[0]
    with pytest.raises(c.CompositionError) as caught:
        coordinator.uninstall("osint", "1.0.0", "uninstall-1", principal_id="operator")
    assert caught.value.code == "in_use"
    coordinator.disable("osint", "disable-osint", principal_id="operator")
    receipt = coordinator.uninstall("osint", "1.0.0", "uninstall-2", principal_id="operator")
    assert receipt["registrations"]["removed"] == ["manifest:osint@1.0.0"]
    assert conn.execute("SELECT count(*) FROM geospatial_geometries").fetchone()[0] == evidence
    assert "osint" not in {m["name"] for m in coordinator.store.manifests()}


# --------------------------------------------------------------------------- #
# C05.5 one authority, delegation, rollback
# --------------------------------------------------------------------------- #

def test_legacy_calls_on_a_cut_over_bundle_delegate_or_refuse(world):
    conn, coordinator = world
    select_all(coordinator.store, "osint", "research")
    coordinator.activate("gen-1", principal_id="operator")
    coordinator.cutover("osint", "cutover-osint", principal_id="operator")
    assert domain_registry.is_pack_enabled("osint")
    domain_registry.disable_pack("osint")
    assert "osint" not in coordinator.store.selection()
    assert not domain_registry.is_pack_enabled("osint")
    domain_registry.enable_pack("osint")
    assert "osint" in coordinator.store.selection()
    assert domain_registry.is_pack_enabled("osint")
    with pytest.raises(domain_registry.CompatibilityError):
        pack_install.uninstall("osint")
    from src.domains.pack_format import PackManifest

    with pytest.raises(domain_registry.CompatibilityError):
        pack_install.install_manifest(PackManifest.from_dict(c.v1_view(bundle("osint"))))
    # No divergent state: enabled exactly when selected, and no legacy ledger entry.
    assert domain_registry.is_pack_enabled("osint") == ("osint" in coordinator.store.selection())
    assert "osint" not in pack_install.installed_packs()
    # Bundles not cut over stay on the legacy path.
    domain_registry.disable_pack("research")
    assert "research" in coordinator.store.selection()


def test_rollback_restores_legacy_bindings_and_leaves_sources_and_evidence_unchanged(world):
    conn, coordinator = world
    conn.execute("CREATE TABLE source_pack_versions(pack_id TEXT, version TEXT, manifest_hash TEXT)")
    conn.execute("INSERT INTO source_pack_versions VALUES ('geospatial-berlin','1.1.0','h')")
    select_all(coordinator.store, "osint")
    coordinator.activate("gen-1", principal_id="operator")
    snapshot = lambda: (conn.execute("SELECT * FROM source_pack_versions").fetchall(),  # noqa: E731
                        conn.execute("SELECT count(*) FROM geospatial_geometries").fetchone())
    before = snapshot()
    coordinator.cutover("osint", "cutover-1", principal_id="operator")
    receipt = coordinator.rollback("osint", "rollback-1", principal_id="operator")
    assert receipt["stages"][0]["stage"] == "rolled-back"
    assert pack_install.installed_packs()["osint"] == "1.0.0"
    assert domain_registry.is_pack_enabled("osint")
    domain_registry.disable_pack("osint")  # legacy authority again
    assert "osint" in coordinator.store.selection()
    assert snapshot() == before
    coordinator.cutover("osint", "cutover-2", principal_id="operator")
    assert coordinator.store.authority()["osint"]["authority"] == "composition"
    assert snapshot() == before


def test_legacy_flag_keeps_the_authority_hook_uninstalled(world, monkeypatch):
    conn, coordinator = world
    select_all(coordinator.store, "osint")
    coordinator.activate("gen-1", principal_id="operator")
    coordinator.cutover("osint", "cutover-1", principal_id="operator")
    monkeypatch.setenv(lc.LEGACY_FLAG, "legacy")
    restart(conn).reconcile()
    assert domain_registry._AUTHORITY is None
    domain_registry.disable_pack("osint")
    assert "osint" in coordinator.store.selection()


def test_startup_is_a_no_op_without_composition_state():
    assert lc.startup(duckdb.connect(":memory:")) is None
    assert lc.startup(None) is None
