"""Composition lifecycle (C05): selections, journal, generation switch, retention, authority."""

from __future__ import annotations

import socket

import duckdb
import pytest

from src.composition.contracts import validate_receipt
from src.composition.lifecycle import CompositionCoordinator, CompositionLifecycleError, SimulatedCrash
from src.domains import pack_install
from src.domains import registry as domain_registry
from src.domains.pack_format import PackManifest
from tests.unit import composition_corpus as corpus

GEO = [corpus.spatial_provider(), corpus.spatial_provider(version="1.1.0")]


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    domain_registry._REGISTRY.clear()
    domain_registry._ENABLED.clear()
    domain_registry.set_authority(None)

    def refuse(*_args, **_kwargs):
        raise AssertionError("lifecycle made a network request")

    monkeypatch.setattr(socket, "create_connection", refuse)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


class Clock:
    def __init__(self):
        self.value = 1_790_000_000_000

    def __call__(self):
        self.value += 1
        return self.value


def _coordinator(conn=None, clock=None, **kwargs):
    return CompositionCoordinator(conn or duckdb.connect(":memory:"), now=clock or Clock(), **kwargs)


def _installed(coordinator, providers=GEO, extra=()):
    for manifest in corpus.resolver_world()["packs"] + list(extra):
        coordinator.install(manifest)
    for descriptor in providers:
        coordinator.install(descriptor)
    for bundle in ("osint", "science", "geospatial", "news"):
        coordinator.cutover(bundle)
    return coordinator


def _both(coordinator):
    coordinator.select("osint", "^1.0.0")
    coordinator.select("science", "^1.0.0")
    return coordinator


def _check(receipt, coordinator):
    digests = {g: coordinator.generation(g)["plan_digest"] for (g,) in
               coordinator.conn.execute("SELECT generation_id FROM composition_generations").fetchall()}
    assert validate_receipt(receipt, generation_digests=digests) == []
    return receipt


# ------------------------------------------------------------ C05.1


def test_install_select_resolve_are_separate_persisted_transitions_that_survive_restart():
    conn = duckdb.connect(":memory:")
    coordinator = _coordinator(conn)
    for manifest in corpus.resolver_world()["packs"]:
        coordinator.install(manifest)
    for descriptor in GEO:
        coordinator.install(descriptor)
    assert coordinator.selections() == [] and coordinator.active() is None
    assert conn.execute("SELECT COUNT(*) FROM composition_plans").fetchone()[0] == 0
    coordinator.select("osint", "^1.0.0", features=[], providers={"geospatial.spatial-relation": "geospatial.core"})
    assert conn.execute("SELECT COUNT(*) FROM composition_plans").fetchone()[0] == 0
    result = coordinator.resolve()
    assert result.ok and coordinator.active() is None
    assert domain_registry._ENABLED == set() and domain_registry._REGISTRY == {}

    restarted = _coordinator(conn)
    assert restarted.selections() == coordinator.selections()
    assert restarted.plan(result.plan["digest"]) == result.plan
    assert len(restarted.installed("pack")) == 4 and len(restarted.installed("provider")) == 2


def test_install_rejects_invalid_documents():
    coordinator = _coordinator()
    with pytest.raises(CompositionLifecycleError) as caught:
        coordinator.install({**corpus.manifest(), "description": "edited after sealing"})
    assert caught.value.code == "invalid_document"


# ------------------------------------------------------------ C05.2 / C05.3


def test_publish_switches_generation_and_registers_the_plan():
    coordinator = _both(_installed(_coordinator()))
    receipt = _check(coordinator.activate("enable-1"), coordinator)
    assert receipt["status"] == "published" and receipt["previous_generation"] is None
    active = coordinator.active()
    assert active["id"] == receipt["new_generation"]["id"]
    assert {"osint", "science", "geospatial"} <= domain_registry._ENABLED
    assert coordinator.activate("enable-1") == receipt  # idempotent per key
    assert [s["stage"] for s in receipt["stages"]] == ["preview", "stage", "verify", "publish"]


@pytest.mark.parametrize("stage", ["previewed", "staged", "verified"])
def test_crash_before_publish_leaves_previous_generation_active_after_restart(stage):
    conn, clock = duckdb.connect(":memory:"), Clock()
    coordinator = _installed(_coordinator(conn, clock), providers=GEO[:1])
    _both(coordinator)
    first = coordinator.activate("gen-1")
    coordinator.install(GEO[1])
    with pytest.raises(SimulatedCrash):
        coordinator.activate("gen-2", upgrade={"geospatial.core"}, crash_after=stage)

    restarted = _coordinator(conn, clock)
    summary = restarted.reconcile()
    assert summary["active_generation"] == first["new_generation"]["id"]
    assert {b["provider_version"] for b in summary["bindings"]} == {"1.0.0"}
    interrupted = [e for e in restarted.journal() if e["stage"] == "recovered"]
    assert len(interrupted) == 1 and interrupted[0]["status"] == "failed"
    assert restarted.reconcile()["settled"] == []  # idempotent
    assert {"osint", "science", "geospatial"} <= domain_registry._ENABLED


def test_crash_after_publish_and_empty_restart_reproduce_identical_bindings():
    conn, clock = duckdb.connect(":memory:"), Clock()
    coordinator = _both(_installed(_coordinator(conn, clock)))
    with pytest.raises(SimulatedCrash):
        coordinator.activate("gen-1", crash_after="published")
    published = coordinator.active()
    domain_registry._REGISTRY.clear()
    domain_registry._ENABLED.clear()
    first = _coordinator(conn, clock).reconcile()
    second = _coordinator(conn, clock).reconcile()
    assert first["active_generation"] == published["id"] == second["active_generation"]
    assert first["bindings"] == second["bindings"] == published["plan"]["bindings"]
    assert first["rebuilt_registrations"] == ["geospatial", "osint", "science"]
    assert {"osint", "science", "geospatial"} <= domain_registry._ENABLED


def test_staged_source_upgrades_reconcile_from_owner_receipts_without_rerunning():
    conn, clock = duckdb.connect(":memory:"), Clock()
    coordinator = _both(_installed(_coordinator(conn, clock)))
    upgrades = [{"pack_id": "geonames", "apply_key": "k1", "principal_id": "operator"},
                {"pack_id": "openalex", "apply_key": "k2", "principal_id": "operator"}]
    with pytest.raises(SimulatedCrash):
        coordinator.activate("gen-1", crash_after="staged", source_upgrades=upgrades)
    from src.ingestion.source_pack_upgrades import _DDL

    conn.execute(_DDL)
    conn.execute("INSERT INTO source_pack_upgrade_receipts VALUES ('a','geonames','operator','k1','h','{}',1)")
    restarted = _coordinator(conn, clock)
    statuses = {s["owner_operation"]: s["status"] for s in restarted.reconcile()["settled"] if "owner_operation" in s}
    assert statuses == {"source-upgrade:geonames:k1": "applied", "source-upgrade:openalex:k2": "unknown"}
    assert not [s for s in restarted.reconcile()["settled"] if "owner_operation" in s]  # no duplicates
    assert restarted.active() is None


def test_replacing_a_pack_version_keeps_the_old_registration_until_publish():
    coordinator = _both(_installed(_coordinator()))
    coordinator.activate("gen-1")
    old = domain_registry.get_pack("osint")
    coordinator.install(corpus.pack("osint", "1.1.0", requires=[corpus.SPATIAL], aliases={"neuronews-osint": "osint"},
                                    contributes=[{"id": "osint.origin-aware-corroboration"}]))
    seen = []

    def during_verify(_plan):
        seen.append((domain_registry.get_pack("osint") is old, "osint" in domain_registry._ENABLED))
        return None

    receipt = coordinator.activate("gen-2", upgrade={"osint"}, fail_verify=during_verify)
    assert seen == [(True, True)]
    assert domain_registry.get_pack("osint") is not old and "osint" in domain_registry._ENABLED
    assert {"kind": "pack", "id": "osint", "action": "replace"} in receipt["affected_registrations"]


def test_preview_lists_affected_consumers_of_a_provider_change():
    coordinator = _both(_installed(_coordinator(), providers=GEO[:1]))
    coordinator.activate("gen-1")
    coordinator.install(GEO[1])
    preview = coordinator.preview(upgrade={"geospatial.core"})
    assert preview["affected_consumers"] == ["osint", "science"]
    (change,) = preview["binding_changes"]
    assert change["before"]["version"] == "1.0.0" and change["after"]["version"] == "1.1.0"
    assert coordinator.preview()["binding_changes"] == []  # the pin holds without an upgrade


def test_failed_activation_emits_a_valid_receipt_and_keeps_the_generation():
    coordinator = _both(_installed(_coordinator()))
    first = coordinator.activate("gen-1")
    failed = _check(coordinator.activate("gen-2", fail_verify=lambda plan: "staged binding rejected"), coordinator)
    assert failed["status"] == "failed" and failed["stages"][-1]["status"] == "failed"
    assert coordinator.active()["id"] == first["new_generation"]["id"]
    coordinator.select("science", "^9.0.0")
    unresolved = _check(coordinator.activate("gen-3"), coordinator)
    assert unresolved["status"] == "failed" and unresolved["new_generation"] is None


# ------------------------------------------------------------ C05.4


def test_disabling_osint_keeps_research_spatial_operations_and_the_provider():
    coordinator = _both(_installed(_coordinator()))
    coordinator.activate("gen-1")
    receipt = _check(coordinator.disable("osint", "disable-osint"), coordinator)
    plan = coordinator.active()["plan"]
    assert [b["consumers"] for b in plan["bindings"]] == [["science"]]
    assert {p["id"] for p in plan["providers"]} == {"geospatial.core"}
    assert "osint" not in domain_registry._ENABLED and {"science", "geospatial"} <= domain_registry._ENABLED
    assert {"kind": "pack", "id": "osint", "action": "unregister"} in receipt["affected_registrations"]
    readiness = coordinator.readiness(scopes=corpus.provider()["operations"][0]["required_scopes"])
    spatial = next(o for o in readiness["operations"] if o["operation"] == "calculate-spatial-relation")
    assert not any(b["kind"] == "disabled_provider" for b in spatial["blockers"])


def test_administrative_shutdown_lists_consumers_and_blocks_their_spatial_operations():
    coordinator = _both(_installed(_coordinator()))
    coordinator.activate("gen-1")
    report = coordinator.shutdown_provider("geospatial.core", reason="maintenance", principal="operator")
    assert report["affected_consumers"] == ["osint", "science"]
    assert "geospatial.spatial-relation:calculate-spatial-relation" in report["unavailable_operations"]
    readiness = coordinator.readiness()
    for operation in readiness["operations"]:
        assert operation["state"] == "blocked"
        assert {"kind": "disabled_provider",
                "detail": "provider geospatial.core was administratively shut down"} in operation["blockers"]
    coordinator.restore_provider("geospatial.core")
    assert coordinator.shutdowns() == {}


def test_pinned_run_retains_a_provider_and_uninstall_is_bounded():
    conn = duckdb.connect(":memory:")
    coordinator = _installed(_coordinator(conn), extra=[corpus.pack("news")])
    coordinator.select("osint", "^1.0.0")
    coordinator.select("news", "^1.0.0")
    coordinator.activate("gen-1")
    coordinator.pin_run("run-1")
    conn.execute("CREATE TABLE osint_evidence (evidence_id TEXT, pack TEXT)")
    conn.execute("INSERT INTO osint_evidence VALUES ('ev-1', 'osint')")

    receipt = coordinator.disable("osint", "disable-osint")
    assert coordinator.active()["retained_providers"] == ["geospatial.core"]
    assert {"kind": "binding", "id": "geospatial.core", "action": "retain"} in receipt["affected_registrations"]
    with pytest.raises(CompositionLifecycleError) as caught:
        coordinator.uninstall("provider", "geospatial.core")
    assert caught.value.code == "in_use"
    with pytest.raises(CompositionLifecycleError):
        coordinator.uninstall("pack", "news")

    coordinator.release_run("run-1")
    removed = coordinator.uninstall("pack", "osint")
    assert removed == {"kind": "pack", "id": "osint", "removed_versions": ["1.0.0"], "data_deleted": False}
    assert domain_registry.get_pack("osint") is None
    assert conn.execute("SELECT evidence_id FROM osint_evidence").fetchall() == [("ev-1",)]


# ------------------------------------------------------------ C05.5


def test_legacy_calls_on_composition_managed_packs_delegate_or_refuse_without_divergence():
    coordinator = _both(_installed(_coordinator()))
    coordinator.activate("gen-1")
    before = set(domain_registry._ENABLED)
    domain_registry.enable_pack("osint")  # already what the coordinator published: a no-op
    with pytest.raises(domain_registry.CompositionAuthorityError):
        domain_registry.disable_pack("osint")
    with pytest.raises(pack_install.PackInstallError):
        pack_install.install_manifest(PackManifest.from_dict({"pack_format": "noesis-pack-v1", "name": "osint",
                                                              "version": "9.0.0"}))
    with pytest.raises(pack_install.PackInstallError):
        pack_install.uninstall("osint")
    domain_registry.load_config()
    assert {"osint", "science", "geospatial"} <= domain_registry._ENABLED
    assert {n for n in before if coordinator.manages(n)} == {n for n in domain_registry._ENABLED
                                                             if coordinator.manages(n)}
    domain_registry.enable_pack("legal")  # legacy authority is untouched for other bundles
    assert "legal" in domain_registry._ENABLED


def test_compatibility_rollback_restores_legacy_bindings_and_keeps_sources_and_evidence():
    conn = duckdb.connect(":memory:")
    coordinator = _both(_installed(_coordinator(conn, legacy_config=lambda: ["news"])))
    coordinator.activate("gen-1")
    conn.execute("CREATE TABLE source_pack_current (pack_id TEXT, version TEXT, manifest_hash TEXT)")
    conn.execute("INSERT INTO source_pack_current VALUES ('geonames', '1.0.0', 'sha256:abc')")
    conn.execute("CREATE TABLE osint_evidence (evidence_id TEXT)")
    conn.execute("INSERT INTO osint_evidence VALUES ('ev-1')")
    generations = conn.execute("SELECT COUNT(*) FROM composition_generations").fetchone()

    result = coordinator.rollback_to_legacy("osint", "rollback-osint")
    assert result == {"bundle": "osint", "authority": "legacy", "compat_rollback": True, "enabled": []}
    assert not coordinator.manages("osint") and "osint" not in domain_registry._ENABLED
    domain_registry.enable_pack("osint")  # the legacy path is the authority again
    assert "osint" in domain_registry._ENABLED
    assert conn.execute("SELECT * FROM source_pack_current").fetchall() == [("geonames", "1.0.0", "sha256:abc")]
    assert conn.execute("SELECT * FROM osint_evidence").fetchall() == [("ev-1",)]
    assert conn.execute("SELECT COUNT(*) FROM composition_generations").fetchone() == generations
    assert any(e["stage"] == "compat-rollback" for e in coordinator.journal())
    assert conn.execute("SELECT compat_rollback FROM composition_authority WHERE bundle='osint'").fetchone() == (True,)
