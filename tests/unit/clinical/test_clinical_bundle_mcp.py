"""H12: bundle declaration, readiness and MCP entry points with preserved tool ids and scopes."""

import asyncio
import json
from pathlib import Path

import duckdb
import pytest

from src.kb.clinical_bundle import BUNDLE, BundleError, readiness, set_enabled
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.clinical.harness import NS, QUESTION, Env
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.clinical import (
    CLINICAL_TOOLS,
    CLINICAL_WRITES,
    DEVICE_TOOLS,
    HEALTH_CAPACITY_TOOLS,
    MEDICINES_TOOLS,
    SURVEILLANCE_TOOLS,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "clinical-mcp.duckdb")
    env = Env(path)
    env.journey()
    scopes = env.scopes("knowledge:clinical:review")
    env.conn.close()
    state = {"principal": "alice", "scopes": set(scopes)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state, path


def test_declaration_reuses_existing_owners_and_states_the_boundary():
    workflows = BUNDLE["contributions"]["workflows"]
    declared = {t for w in workflows.values() for t in w["tools"] if not t.startswith("noesis-")}
    assert declared <= CLINICAL_TOOLS
    reused = " ".join(r for w in workflows.values() for r in w["reuses"])
    for owner in ("source-pack runtime", "PaperFamilyStore", "MethodologyStore", "OntologyAlignmentStore",
                  "SubscriptionStore"):
        assert owner in reused
    assert "give medical advice" in BUNDLE["never"] and "not medical advice" in BUNDLE["boundary"]
    sources = {s["provider"]: s for s in BUNDLE["contributions"]["sources"]}
    assert sources["prospero"]["status"] == "not-implemented" and sources["ctgov"]["status"] == "implemented"


def test_tools_are_registered_with_scopes_and_mutability_in_the_catalog(mcp_env):
    tools, _, _ = mcp_env
    # 20 clinical tools; the optional surveillance feature's tools have their own tests (test_surveillance_mcp.py).
    # The optional medicines feature's tools (#2214) are tested in test_medicines_mcp.py.
    # The optional health-capacity feature's tools (#2215) are tested in test_health_capacity_mcp.py.
    assert CLINICAL_TOOLS <= set(tools)
    # The clinical.devices provider's tools (#2654) are tested in tests/unit/domains/test_medical_devices_mcp.py.
    assert len(CLINICAL_TOOLS - SURVEILLANCE_TOOLS - MEDICINES_TOOLS - HEALTH_CAPACITY_TOOLS - DEVICE_TOOLS) == 20
    for name in CLINICAL_TOOLS:
        assert _mutability(name) == ("write" if name in CLINICAL_WRITES else "read")
    assert _required_scopes("knowledge_engine_mcp", "read", "clinical_provider_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "set_clinical_bundle_enabled") == ["operator"]
    assert _required_scopes("knowledge_engine_mcp", "read", "lookup_clinical_trial") == ["knowledge:clinical:read"]
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    entries = {t["id"]: t for t in catalog["tools"]}
    for name in CLINICAL_TOOLS:
        entry = entries[f"noesis-knowledge-engine.{name}"]
        assert entry["required_scopes"] == _required_scopes("knowledge_engine_mcp", entry["mutability"], name)


def test_lookup_map_switching_strength_and_export_through_mcp(mcp_env):
    tools, _, _ = mcp_env
    status = tools["clinical_bundle_status"].fn(namespace=NS)
    implemented = {p: s["status"] for p, s in status["providers"].items()}
    assert {implemented[p] for p in ("ctgov", "ctis", "euctr", "openfda", "ema")} == {"fixture-only"}
    assert implemented["prospero"] == implemented["who-ictrp"] == "not-implemented"
    trial = tools["lookup_clinical_trial"].fn(namespace=NS, registry="ctgov", identifier="NCT09000001")
    assert trial["trial"]["identifier"] == "NCT09000001" and trial["cross_registry"][0]["merged"] is False
    assert tools["lookup_clinical_trial"].fn(namespace=NS, registry="ctgov", identifier="NCT09999999")[
        "error"]["code"] == "record_not_found"
    design = tools["record_clinical_trial_design"].fn(namespace=NS, record_id=trial["record_id"])
    switching = tools["clinical_outcome_switching"].fn(namespace=NS, study_id=design["study_id"])
    assert {f["kind"] for f in switching["findings"]} == {"primary-retimed", "publication-retimed"}
    built = tools["build_clinical_evidence_map"].fn(namespace=NS, question=QUESTION, request_key="mcp-1")
    strength = tools["clinical_strength_view"].fn(namespace=NS, view_id=built["view_id"])
    assert strength["summary"]["rule"] == "S2" and strength["grade"]["asserted"] is False
    exported = tools["export_clinical_evidence_bundle"].fn(namespace=NS, view_id=built["view_id"])
    assert exported["bundle"]["contract"] == "noesis-evidence-bundle-v1"
    expansion = tools["expand_clinical_question"].fn(namespace=NS, question=QUESTION)
    assert expansion["expansion"]["condition"]["mesh_ids"] == ["D003924"]
    gaps = tools["clinical_coverage_gaps"].fn(namespace=NS)
    assert gaps["unregistered_publications"]


def test_other_namespaces_and_missing_scopes_are_refused(mcp_env):
    tools, state, _ = mcp_env
    state["scopes"] = {"knowledge:clinical:read", "namespace:other:read"}
    denied = tools["lookup_clinical_trial"].fn(namespace=NS, registry="ctgov", identifier="NCT09000001")
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    assert tools["lookup_clinical_trial"].fn(namespace="other", registry="ctgov", identifier="NCT09000001")[
        "error"]["code"] == "record_not_found"
    state["scopes"] = {"knowledge:read"}
    assert tools["build_clinical_evidence_map"].fn(namespace=NS, question=QUESTION, request_key="x")[
        "error"]["code"] == "unauthorized"


def test_enablement_requires_the_coordinator(mcp_env):
    tools, state, path = mcp_env
    state["scopes"] = state["scopes"] | {"operator"}
    refused = tools["set_clinical_bundle_enabled"].fn(namespace=NS, enabled=False)
    assert refused["error"]["code"] == "not_composition_managed"
    conn = duckdb.connect(path)
    with pytest.raises(BundleError):
        set_enabled(conn, NS, False, principal_id="alice", scopes={"knowledge:clinical:read"})
    assert readiness(conn, NS, scopes={"knowledge:clinical:read"})["enabled"] is True
    conn.close()


def test_linking_and_alignment_work_with_exactly_their_declared_scopes(mcp_env):
    """Each tool holding only its declared scopes (plus namespace access and the object-level document grants)."""
    from tools.knowledge_engine_mcp.clinical import CLINICAL_SCOPES

    tools, state, path = mcp_env
    conn = duckdb.connect(path, read_only=True)
    documents = {f"document:{r[0]}:read" for r in conn.execute("SELECT document_id FROM documents").fetchall()}
    conn.close()
    namespace = {f"namespace:{NS}:read", f"namespace:{NS}:write"}
    state["scopes"] = set(CLINICAL_SCOPES["link_clinical_publications"]) | namespace | documents
    linked = tools["link_clinical_publications"].fn(namespace=NS, observation="exact-scopes")
    assert linked.get("ok") is not False and linked["links"] and not linked["family_errors"], linked["family_errors"]
    mesh = json.loads((ROOT / "tests/fixtures/clinical/mesh_subset.json").read_text())
    state["scopes"] = set(CLINICAL_SCOPES["align_clinical_terms"]) | namespace
    aligned = tools["align_clinical_terms"].fn(namespace=NS, mesh_version=mesh["version"])
    assert aligned.get("ok") is not False and aligned["mappings"], aligned
    for name in ("link_clinical_publications", "align_clinical_terms"):
        state["scopes"] = (set(CLINICAL_SCOPES[name]) - {"knowledge:clinical:read"}) | namespace
        refused = tools[name].fn(namespace=NS, observation="x") if name.startswith("link") else tools[name].fn(
            namespace=NS, mesh_version=mesh["version"])
        assert refused["ok"] is False and refused["error"]["code"] == "unauthorized", name
