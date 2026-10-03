"""Public-finance MCP entry points: catalog registration, declared scopes, conditional scopes and answers (#1996)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.public_finance import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import public_finance_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.public_finance import (
    PUBLIC_FINANCE_SCOPES,
    PUBLIC_FINANCE_TOOLS,
    PUBLIC_FINANCE_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "public-finance-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_budgets(conn)
    line = h.line_id(conn, "de-bund-haushalt", titel="68101")
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return tool_map(server.mcp), state, line


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _, _ = mcp_env
    assert (
        PUBLIC_FINANCE_TOOLS <= set(tools)
        and set(PUBLIC_FINANCE_SCOPES) == PUBLIC_FINANCE_TOOLS
    )
    for name in PUBLIC_FINANCE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in PUBLIC_FINANCE_WRITES else "read"), (
            name
        )
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == PUBLIC_FINANCE_SCOPES[name]
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    by_name = {t["name"]: t for t in catalog["tools"]}
    assert PUBLIC_FINANCE_TOOLS <= set(by_name)
    for name in PUBLIC_FINANCE_TOOLS:
        assert by_name[name]["required_scopes"] == PUBLIC_FINANCE_SCOPES[name]
    for name in (
        "compare_budget_line",
        "budget_line_dossier",
        "beneficiary_dossier",
        "propose_public_finance_identity_matches",
    ):
        assert "conditional scope" in tools[name].description.lower()
    descriptor = json.loads(
        (h.ROOT / "packs/economics/providers/economics.public-finance.json").read_text()
    )
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in PUBLIC_FINANCE_TOOLS and name not in PUBLIC_FINANCE_WRITES
        assert op["required_scopes"] == PUBLIC_FINANCE_SCOPES[name]


def test_reads_run_on_a_read_only_connection_and_writes_are_scoped(mcp_env):
    tools, state, line = mcp_env
    compared = tools["compare_budget_line"].fn(
        namespace="global", line_id=line, fiscal_year="2099"
    )
    assert [c["figure_kind"] for c in compared["columns"]] == [
        "plan",
        "supplementary_plan",
        "outturn",
    ]
    assert forbidden_keys(compared) == []
    dossier = tools["budget_line_dossier"].fn(namespace="global", line_id=line)
    assert "no audit finding recorded that cites this line" in dossier["unknowns"]
    sheet = json.loads(h.body("brh_bemerkungen_2100.json"))
    assert (
        tools["import_audit_findings"].fn(namespace="global", sheet=sheet)["revisions"]
        == 2
    )
    assert (
        len(
            tools["list_audit_findings"].fn(namespace="global", line_id=line)[
                "findings"
            ]
        )
        == 2
    )
    payments = tools["list_beneficiary_payments"].fn(
        namespace="global", programme="Fictional Horizon Programme"
    )
    assert len(payments["payments"]) == 3
    key = "public-finance:beneficiary:eu-fts:vat:DE:DE999999999"
    beneficiary = tools["beneficiary_dossier"].fn(
        namespace="global", beneficiary_key=key
    )
    assert beneficiary["identity"]["state"] == "unmatched"
    state["scopes"] = set(h.REVIEW_SCOPES) - {"knowledge:economic:read"}
    refused = tools["compare_budget_line"].fn(
        namespace="global", line_id=line, fiscal_year="2099", gfs_series_id="estat:x"
    )
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = set(h.READ_ONLY)
    denied = tools["import_audit_findings"].fn(namespace="global", sheet=sheet)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    contracts = tools["public_finance_source_contracts"].fn()["contracts"]
    assert contracts["imf-gfs"]["access_decision"] == "not-implemented"
