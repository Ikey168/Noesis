"""Medicines MCP entry points: catalog registration, exact scopes and the boundary sentence (#2214, MR12)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.clinical_medicines import BOUNDARY
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import medicines_fixture_builder as fb
from tests.unit.clinical.harness import NS, ROOT
from tests.unit.clinical.medicines_harness import Env
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.clinical import (
    CLINICAL_TOOLS,
    MEDICINES_SCOPES,
    MEDICINES_TOOLS,
    MEDICINES_WRITES,
)

NS_READ = f"namespace:{NS}:read"
NS_WRITE = f"namespace:{NS}:write"


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory):
    from src.ingestion import medicines_sources
    from src.ingestion.clinical_providers import fixture_transport

    path = str(tmp_path_factory.mktemp("medicines") / "medicines-mcp.duckdb")
    env = Env(path)
    env.acquire("r1")
    env.serve_earlier()
    env.acquire_medicines("m1")
    env.serve_pinned()
    env.acquire_medicines("m2")
    env.conn.close()
    patch = pytest.MonkeyPatch()
    state = {"principal": "alice", "scopes": set()}
    original = medicines_sources.RxNavClient

    class FixtureRxNav(original):
        def __init__(self, transport=None, **kwargs):
            super().__init__(transport or fixture_transport(fb.rxnav_pages()), **kwargs)

    patch.setattr(medicines_sources, "RxNavClient", FixtureRxNav)
    patch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    patch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    yield tool_map(server.mcp), state
    patch.undo()


def call(tools, ctx, name, scopes, **kwargs):
    ctx["scopes"] = set(scopes)
    return tools[name].fn(**kwargs)


def test_tools_are_registered_in_the_catalog_with_every_scope_they_always_use(mcp_env):
    tools, _ = mcp_env
    assert MEDICINES_TOOLS <= set(tools) and set(MEDICINES_SCOPES) == MEDICINES_TOOLS
    assert MEDICINES_TOOLS <= CLINICAL_TOOLS
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in MEDICINES_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in MEDICINES_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == MEDICINES_SCOPES[name]
        assert by_name[name]["required_scopes"] == MEDICINES_SCOPES[name]


def test_the_journey_runs_through_the_tools_with_exactly_their_declared_scopes(mcp_env):
    tools, state = mcp_env

    def run(name, extra=(), **kwargs):
        if name in MEDICINES_WRITES:
            denied = call(tools, state, name, {NS_READ, NS_WRITE}, namespace=NS, **kwargs)
            assert denied["ok"] is False and denied["error"]["code"] == "unauthorized", name
        result = call(tools, state, name, {*MEDICINES_SCOPES[name], NS_READ, NS_WRITE, *extra}, namespace=NS,
                      **kwargs)
        assert result.get("ok") is not False, (name, result)
        assert result["boundary"] == BOUNDARY, name
        return result

    readiness = run("medicines_readiness")
    assert readiness["selected"] is False and readiness["providers"]["dailymed"]["last_execution"] == "injected"
    proposed = run("propose_medicine_identities")
    assert proposed["rxnorm_release"] == fb.RXNAV_RELEASE
    eu = next(m for m in run("list_medicine_identity_matches", state="proposed")["matches"]
              if m["subject_key"] == "ema:EMEA/H/C/009001")
    assert run("review_medicine_identity", match_id=eu["match_id"], decision="accept",
               reason="active substance reviewed")["state"] == "accepted"
    linked = run("link_medicine_evidence", observation="mcp-link-1")
    assert {t["trial"] for t in linked["trials"]} == {"NCT09000001", "2015-900001-10"}
    status = run("medicine_status_as_of", medicine="noetiglutide", as_of="2026-09-01")
    assert {a["jurisdiction"] for a in status["jurisdictions"]} == {"EU", "US"}
    label = run("medicine_label_as_of", medicine="noetiglutide", as_of="2025-01-01", provider="dailymed")
    assert label["labels"][0]["document"]["version"] == "7"
    diff = run("compare_medicine_labels", medicine="noetiglutide", document_id=fb.SET_ID)
    assert any(c["code"] == "34066-1" for c in diff["diffs"][0]["changes"])
    assert run("medicine_safety_communications", substance="noetiglutide")["communications"]
    timeline = run("medicine_regulatory_timeline", medicine="noetiglutide")
    assert timeline["on_record"] is True and timeline["linked_trials"]
    assert run("revert_medicine_identity", match_id=eu["match_id"], reason="undo")["state"] == "proposed"
    monitor = run("create_medicines_monitor", medicine="noetiglutide", request_key="mcp-watch",
                  extra={"knowledge:subscriptions:read"})
    ran = run("run_medicines_monitor", subscription_id=monitor["subscription_id"], watermark=1)
    assert ran["notifications"]
    assert run("poll_medicines_monitor", subscription_id=monitor["subscription_id"])
