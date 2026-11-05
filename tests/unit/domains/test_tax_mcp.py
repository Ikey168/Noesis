"""Tax transparency MCP entry points: registration, declared scopes, exclusions and answers (#2889)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.tax_records import forbidden_paths, personal_data_paths
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import tax_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.tax import TAX_SCOPES, TAX_TOOLS, TAX_WRITES


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "tax-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert TAX_TOOLS <= set(tools) and set(TAX_SCOPES) == TAX_TOOLS
    for name in TAX_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in TAX_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == TAX_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/economics/providers/economics.tax.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in TAX_TOOLS and name not in TAX_WRITES
        assert op["required_scopes"] == TAX_SCOPES[name]
    for name in ("tax_indicator_for_jurisdiction", "tax_series_history", "export_tax_evidence_bundle",
                 "tax_source_contracts", "tax_company_reports_for_entity"):
        description = " ".join(tools[name].description.lower().split())
        assert "no model estimate presented as an observation" in description, name
        assert "no own effective tax rate" in description, name


def test_the_generated_catalog_lists_the_tools_with_their_scopes():
    """Holds once contracts/generated/noesis-mcp-catalog-v1.json is regenerated (scripts/generate_mcp_catalog.py)."""
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in TAX_TOOLS:
        assert by_name[name]["required_scopes"] == TAX_SCOPES[name]


def test_reads_work_with_exactly_the_declared_scopes_and_writes_are_scoped(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    answer = tools["tax_indicator_for_jurisdiction"].fn(namespace="global", jurisdiction="NLD", as_of="2025-12-31")
    assert answer["status"] == "reported" and answer["model_estimates"] and answer["published_statistics"]
    assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    assert "presenting model estimates as observations" in answer["exclusions"]
    cthi = tools["tax_indicator_for_jurisdiction"].fn(namespace="global", jurisdiction="NLD",
                                                      concept="corporate_tax_haven_index")
    assert cthi["status"] == "not-implemented"
    bundle = tools["export_tax_evidence_bundle"].fn(namespace="global", jurisdiction="DEU")["evidence_bundle"]
    assert all(a["dependencies"][0]["revision"] and a["dependencies"][0]["as_of"]
               for s in bundle["sections"] for a in s["assertions"])
    series = tools["list_tax_series"].fn(namespace="global", nature="model_estimate")["series"]
    assert series and {s["nature"] for s in series} == {"model_estimate"}
    history = tools["tax_series_history"].fn(namespace="global", series_id=series[0]["series_id"])
    assert history["editions"]
    assert tools["list_tax_company_reports"].fn(namespace="global")["reports"] == []
    refused = tools["record_tax_company_reports"].fn(namespace="global", references=h.references())
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    denied = tools["list_tax_company_matches"].fn(namespace="global")
    assert denied["ok"] is False  # the ownership read scope is declared and required
    state["scopes"] = {"knowledge:tax:write", "namespace:global:write"}
    recorded = tools["record_tax_company_reports"].fn(namespace="global", references=h.references())
    assert recorded["status"] == "reference-only" and len(recorded["references"]) == 3
    figures = tools["record_tax_company_reports"].fn(namespace="global",
                                                     references=[{**h.references()[0], "revenue": "1"}])
    assert figures["ok"] is False and figures["error"]["code"] == "reference_only"
    state["scopes"] = set(h.SCOPES)
    proposed = tools["propose_tax_company_matches"].fn(namespace="global", ownership_namespace=h.OWN_NS)
    assert proposed["status"] == "ownership_absent" and len(proposed["unmatched"]) == 3
    state["scopes"] = set()
    contracts = tools["tax_source_contracts"].fn()
    assert contracts["live_verification"]["oecd-cts-cbcr"]["status"] == "unverified-live"
    assert contracts["live_verification"]["tjn-fsi"]["status"] == "not-implemented"
    assert contracts["live_verification"]["public-cbcr"]["status"] == "reference-only"
