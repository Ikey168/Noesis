"""The Funding & Grants bundle under the composition contracts (C09.4, #1843)."""

import duckdb
import pytest

from src.composition import contracts as c
from src.composition import lifecycle as lc
from src.composition.deployment import candidates
from src.composition.resolver import resolve
from src.domains import pack_install
from src.domains import registry as domain_registry
from src.kb import funding_bundle
from src.kb.research_projects import ResearchProjectStore

SHARED = {"noesis.research-projects", "noesis.reports", "noesis.quantitative", "noesis.subscriptions",
          "noesis.documents"}
OPERATOR = {"operator"}


@pytest.fixture(autouse=True)
def isolated():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY,
             dict(pack_install._INSTALLED), dict(pack_install._TEMPLATES))
    lc.reset_runtime()
    lc.install_authority(None)
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


def _funding_manifest():
    return next(m for m in candidates()["manifests"] if m["name"] == "funding-grants")


def test_manifest_validates_and_binds_only_shared_providers_plus_its_own():
    manifest = _funding_manifest()
    assert c.validate_manifest(manifest) == manifest
    assert manifest["contributes"]["providers"] == [{"provider_id": "noesis.funding", "version": "1.0.0"}]
    shipped = candidates()
    result = resolve(roots=[{"name": "funding-grants", "range": "0.1.0"}], manifests=shipped["manifests"],
                     providers=shipped["providers"], contracts=shipped["contracts"])
    assert result["status"] == "resolved"
    bound = {b["provider_id"] for b in result["plan"]["bindings"]}
    assert bound == SHARED | {"noesis.funding"}
    funding = next(d for d in shipped["providers"] if d["provider_id"] == "noesis.funding")
    assert {s["record_kind"] for s in funding["stores"]} == {"funding_record"}
    # Applicant facts stay owner-scoped: no shared provider owns a funding table.
    shared_tables = {t for d in shipped["providers"] if d["provider_id"] in SHARED
                     for s in d["stores"] for t in s["tables"]}
    assert not {t for t in shared_tables if t.startswith("funding_")}
    entry_points = manifest["metadata"]["entry_points"]
    assert set(entry_points) == {"discovery", "profile_to_shortlist", "application_preparation", "monitoring"}
    offered = {cap["capability"] for d in shipped["providers"] for cap in d["capabilities"]}
    assert {cap for caps in entry_points.values() for cap in caps} <= offered


def _world():
    conn = duckdb.connect(":memory:")
    coordinator = lc.Coordinator(conn)
    shipped = candidates()
    for descriptor in shipped["providers"]:
        coordinator.store.install_provider(descriptor, principal_id="operator")
    coordinator.store.install_manifest(_funding_manifest(), principal_id="operator")
    coordinator.store.select("funding-grants", "0.1.0", principal_id="operator")
    assert coordinator.activate("funding-1", principal_id="operator")["status"] == "applied"
    coordinator.cutover("funding-grants", "cut-funding", principal_id="operator")
    return conn, coordinator


def test_enablement_is_a_composition_selection_change_with_one_authority():
    conn, coordinator = _world()
    assert funding_bundle.authority() == "composition"
    assert funding_bundle.is_enabled(conn, "ns") is True
    result = funding_bundle.set_enabled(conn, "ns", False, principal_id="op", scopes=OPERATOR)
    assert result["authority"] == "composition" and result["enabled"] is False
    assert "funding-grants" not in coordinator.store.selection()
    # The legacy per-namespace ledger was not written: one authority.
    assert not conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='funding_bundle_state'").fetchone()
    with pytest.raises(funding_bundle.BundleError):
        funding_bundle.require_enabled(conn, "other-namespace")
    # The legacy registry cannot enable it behind the coordinator's back.
    domain_registry.enable_pack("funding-grants")
    assert "funding-grants" in coordinator.store.selection()  # delegated, journaled activation
    funding_bundle.set_enabled(conn, "ns", False, principal_id="op", scopes=OPERATOR)
    assert funding_bundle.set_enabled(conn, "ns", True, principal_id="op", scopes=OPERATOR)["enabled"] is True
    with pytest.raises(funding_bundle.BundleError):
        funding_bundle.set_enabled(conn, "ns", False, principal_id="op", scopes={"knowledge:funding:read"})


def test_disabling_funding_leaves_shared_providers_operational():
    conn, coordinator = _world()
    funding_bundle.set_enabled(conn, "ns", False, principal_id="op", scopes=OPERATOR)
    project = ResearchProjectStore(conn).create(
        "ns", "unrelated", questions=["Q?"], success_criteria=["A"],
        scope={"domains": [], "namespaces": ["ns"]}, budget={}, principal_id="alice",
        scopes={"knowledge:projects:write", "knowledge:projects:read", "namespace:ns:write", "namespace:ns:read"})
    assert project["status"] == "active"
    readiness = funding_bundle.readiness(conn, "ns", scopes=OPERATOR)
    assert readiness["enabled"] is False and readiness["authority"] == "composition"


def test_readiness_reads_shared_provider_state_from_the_composition():
    conn, coordinator = _world()
    ready = funding_bundle.readiness(conn, "ns", scopes=OPERATOR)
    assert ready["composition"]["plan_digest"] == coordinator.store.active_plan()["digest"]
    assert {o["provider_id"] for o in ready["composition"]["operations"]} == SHARED | {"noesis.funding"}
    assert set(ready["entry_points"].values()) == {"unavailable"}  # nothing acquired yet
    coordinator.shutdown_provider("noesis.subscriptions", "shut-subs", reason="maintenance", principal_id="op")
    shut = funding_bundle.readiness(conn, "ns", scopes=OPERATOR)
    assert shut["composition"]["entry_points"]["monitoring"] == "unavailable"
    assert shut["composition"]["entry_points"]["profile_to_shortlist"] == "bound"


def test_legacy_mode_keeps_the_namespace_ledger(monkeypatch):
    monkeypatch.setenv(lc.LEGACY_FLAG, "legacy")
    conn = duckdb.connect(":memory:")
    assert funding_bundle.authority() == "legacy"
    result = funding_bundle.set_enabled(conn, "ns", False, principal_id="op", scopes=OPERATOR)
    assert result == {"namespace": "ns", "bundle": "funding-grants", "enabled": False, "authority": "legacy",
                      "scope": "namespace", "shared_capabilities_affected": []}
    assert funding_bundle.is_enabled(conn, "ns") is False and funding_bundle.is_enabled(conn, "other") is True
