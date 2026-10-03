"""HR12 (#2283): the Humanitarian bundle manifest, provider descriptor, optional ACLED feature and MCP tools."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.evidence_bundle.verifier import verify_bundle
from src.kb.humanitarian_bundle import BUNDLE, BundleError, readiness, require_enabled, set_enabled
from src.kb.humanitarian_identity import HumanitarianIdentity
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.humanitarian.harness import NS, REVIEWER_SCOPES, SCOPES, world
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.humanitarian import HUMANITARIAN_TOOLS, HUMANITARIAN_WRITES
from src.mcp_host.introspection import tool_map

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "packs/humanitarian/manifest.json").read_text())
COMPOSITION = json.loads((ROOT / "packs/humanitarian/composition.json").read_text())
PROVIDER = json.loads((ROOT / "packs/humanitarian/providers/humanitarian.core.json").read_text())
SHARED = {"geospatial.place-resolution", "geospatial.spatial-relation", "geospatial.geometry-store",
          "osint.corroboration", "news.articles", "economics.demographics", "platform.entity-identity",
          "platform.subscriptions", "platform.source-acquisition"}


def plan_for(features=None):
    bundles = adapt_all()
    root = {"pack": "humanitarian", "version": bundles["humanitarian"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def test_manifest_and_provider_validate_and_the_composition_view_matches():
    assert validate_composition_manifest(MANIFEST) == []
    assert validate_provider_descriptor(PROVIDER) == []
    assert COMPOSITION["requires"] == MANIFEST["requires"]
    assert COMPOSITION["optional_features"] == MANIFEST["optional_features"]
    assert COMPOSITION["exclusions"] == MANIFEST["advisory"]["exclusions"] == BUNDLE["exclusions"]
    assert SHARED <= {r["capability"] for r in MANIFEST["requires"]}
    assert [(f["id"], f["default"]) for f in MANIFEST["optional_features"]] == [("acled", False)]
    assert "declined" in MANIFEST["optional_features"][0]["description"]
    tools = {o["tool"].split(".", 1)[1] for o in PROVIDER["operations"]}
    assert tools == HUMANITARIAN_TOOLS - {"set_humanitarian_bundle_enabled"}


def test_the_plan_binds_its_own_provider_plus_shared_providers_with_one_store_owner():
    plan = plan_for()
    bound = {b["provider"] for b in plan["bindings"] if "humanitarian" in b["consumers"]}
    assert "humanitarian.core" in bound
    assert {"geospatial.core", "osint.core", "news.core", "economics.demographics", "platform.entity-identity",
            "platform.subscriptions", "platform.source-runtime"} <= bound
    owned = {s["store"] for d in provider_descriptors() if d["id"] == "humanitarian.core" for s in d["stores"]}
    others = {s["store"] for d in provider_descriptors() if d["id"] != "humanitarian.core" for s in d["stores"]}
    assert owned and not owned & others and all(s.startswith("src.kb.humanitarian_") for s in owned)
    assert ("humanitarian-response", "^1.0.0") in {(p["pack_id"], p.get("range")) for p in plan["source_packs"]}


def test_tools_have_declared_scopes_and_mutability():
    for name in HUMANITARIAN_TOOLS:
        assert _mutability(name) == ("write" if name in HUMANITARIAN_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "humanitarian_source_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_humanitarian_identity") == [
        "knowledge:humanitarian:review"]
    assert _required_scopes("knowledge_engine_mcp", "read", "humanitarian_dossier") == ["knowledge:humanitarian:read"]
    catalog = {t["id"] for t in json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())["tools"]}
    assert {f"noesis-knowledge-engine.{t}" for t in HUMANITARIAN_TOOLS} <= catalog


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("humanitarian-mcp") / "hum.duckdb")
    value = world()
    value.conn.execute(f"ATTACH '{path}' AS target")
    value.conn.execute("COPY FROM DATABASE memory TO target")
    value.conn.close()
    return path


@pytest.fixture
def mcp_env(database, monkeypatch):
    state = {"principal": "alice", "scopes": set(SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(database, read_only=read_only))
    return tool_map(server.mcp), state, database


def test_dossier_identity_and_export_through_mcp(mcp_env):
    tools, state, path = mcp_env
    assert HUMANITARIAN_TOOLS <= set(tools)
    status = tools["humanitarian_bundle_status"].fn(namespace=NS)
    assert status["providers"]["acled"]["status"] == "declined"
    assert {p["status"] for n, p in status["providers"].items() if n != "acled"} == {"fixture-only"}
    proposed = tools["propose_humanitarian_identity"].fn(namespace=NS)
    assert proposed["proposed"]
    iso3 = next(a for a in tools["list_humanitarian_identity"].fn(namespace=NS, subject_key="place-ref:iso3:SDN")["assertions"]
                if a["target_kind"] == "geospatial-place")
    state.update(principal="bob", scopes=REVIEWER_SCOPES)
    assert tools["review_humanitarian_identity"].fn(namespace=NS, assertion_id=iso3["assertion_id"], decision="accept",
                                                    reason="ISO3 equals COD-AB admin 0")["state"] == "accepted"
    state.update(principal="alice", scopes=set(SCOPES))
    dossier = tools["humanitarian_dossier"].fn(namespace=NS, as_of="2099-07-01", pcode="SDN")
    assert dossier["published"]["items"] and dossier["sources_declined"][0]["reason"] == "not acquired (licence)"
    events = tools["query_humanitarian_conflict_events"].fn(namespace=NS, area={"pcode": "SD01"}, start="2098-01-01",
                                                            end="2098-12-31")
    assert set(events["by_coder"]) == {"ucdp"}
    bundle = tools["export_humanitarian_dossier"].fn(namespace=NS, as_of="2099-07-01", pcode="SDN",
                                                     events_area={"pcode": "SD01"}, events_start="2098-01-01",
                                                     events_end="2098-12-31")
    verified = verify_bundle(bundle)
    assert not verified.errors
    cited = [o["payload"]["citation"] for o in bundle["objects"] if o["payload"].get("kind") == "humanitarian-record-revision"]
    assert cited and all(c["source"] and c["revision"] and c["as_of"] for c in cited)
    history = tools["humanitarian_event_history"].fn(namespace=NS, record_key="ucdp:conflict_event:990001")
    assert [r["coding_status"] for r in history["revisions"]] == ["candidate", "final"]
    contracts = tools["humanitarian_source_contracts"].fn()
    assert contracts["acled_decision"]["status"] == "declined"


def test_scopes_are_enforced(mcp_env):
    tools, state, _ = mcp_env
    state["scopes"] = {"knowledge:humanitarian:read", f"namespace:{NS}:read"}
    denied = tools["propose_humanitarian_identity"].fn(namespace=NS)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = set(SCOPES) - {"knowledge:ingestion:execute"}
    blocked = tools["acquire_humanitarian_source"].fn(namespace=NS, source_id="ucdp-ged-sdn", run_key="k", operation="events")
    assert blocked["ok"] is False and blocked["error"]["code"] == "unauthorized"


def test_disabling_blocks_only_humanitarian_entry_points(mcp_env):
    tools, state, path = mcp_env
    state["scopes"] = set(SCOPES) | {"operator"}
    assert tools["set_humanitarian_bundle_enabled"].fn(namespace=NS, enabled=False)["enabled"] is False
    try:
        blocked = tools["humanitarian_dossier"].fn(namespace=NS, as_of="2099-07-01", pcode="SDN")
        assert blocked["ok"] is False and blocked["error"]["code"] == "bundle_disabled"
        conn = duckdb.connect(path)
        with pytest.raises(BundleError):
            require_enabled(conn, NS)
        require_enabled(conn, "other-namespace")
        with pytest.raises(BundleError):
            set_enabled(conn, NS, True, principal_id="alice", scopes=SCOPES)
        assert readiness(conn, NS, scopes=SCOPES)["enabled"] is False
        HumanitarianIdentity(conn, initialize=False).admin_places(NS)  # shared geospatial places still read
        conn.close()
    finally:
        assert tools["set_humanitarian_bundle_enabled"].fn(namespace=NS, enabled=True)["enabled"] is True
