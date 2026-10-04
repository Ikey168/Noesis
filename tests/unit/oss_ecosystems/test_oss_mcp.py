"""OSS Ecosystems MCP entry points: registration, exactly the declared scopes, and not_ready (OS11, #2203)."""

from __future__ import annotations

import duckdb
import pytest

from src.domains.technical.inventory import InventoryStore
from src.kb.subscriptions import SubscriptionStore
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import vulnerability_harness as vh
from tests.unit.oss_ecosystems import fixture_builder as fb
from tests.unit.oss_ecosystems import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.oss_ecosystems import (
    OSS_READS,
    OSS_SCOPES,
    OSS_TOOLS,
    OSS_WRITES,
    QUERY_EXAMPLES,
)

NAMESPACE = h.NAMESPACE


def _connect(monkeypatch, path, state):
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server, "_intake_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return tool_map(server.mcp)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "oss-mcp.duckdb")
    conn = duckdb.connect(path)
    for poll in (1, 2):
        h.ingest(conn, poll)
    vh.osv(conn, "osv_PYSEC-2099-1.json", "PYSEC-2099-1", at=vh.DAY["01-11"])
    conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT, "
        "entity_type TEXT)"
    )
    conn.execute(
        "INSERT INTO canonical_entities VALUES ('ent:fixture-labs', 'Fixture Labs', 'ORG')"
    )
    inventory = InventoryStore(conn).import_inventory(
        "fixture-parser==1.1.0\n", "requirements.txt", owner_id="analyst"
    )["inventory_id"]
    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion")
    conn.close()
    state = {"principal": "analyst", "scopes": set()}
    return _connect(monkeypatch, path, state), state, {"inventory": inventory}


def test_tools_are_registered_with_mutability_scopes_and_examples(mcp_env):
    tools, _, _ = mcp_env
    assert OSS_TOOLS <= set(tools) and OSS_READS.isdisjoint(OSS_WRITES)
    for name in OSS_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in OSS_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == OSS_SCOPES[name]
        ), name
    assert set(QUERY_EXAMPLES) <= OSS_TOOLS
    assert "not an observed lockfile" in tools["dependency_graph_as_of"].description


def test_every_tool_works_holding_exactly_its_declared_scopes(mcp_env):
    tools, state, ids = mcp_env
    d2 = fb.DATES["d2"]
    calls = [
        ("oss_source_contracts", {}),
        ("oss_ecosystems_readiness", {"namespace": h.NS}),
        (
            "package_release_history",
            {"namespace": h.NS, "package": "pkg:pypi:fixture-parser", "as_of": d2},
        ),
        ("licence_history", {"namespace": h.NS, "package": "pkg:pypi:fixture-parser"}),
        (
            "packages_by_organisation",
            {"namespace": h.NS, "organisation": "fixture-labs"},
        ),
        (
            "dependency_graph_as_of",
            {
                "namespace": h.NS,
                "package": "pkg:cargo:fixture-codec",
                "version": "0.1.0",
                "date": d2,
            },
        ),
        (
            "package_advisories",
            {
                "namespace": h.NS,
                "package": "pkg:pypi:fixture-parser",
                "version": "1.1.0",
            },
        ),
        (
            "compare_inventory_with_graph",
            {
                "namespace": h.NS,
                "inventory_id": ids["inventory"],
                "package": "pkg:pypi:fixture-parser",
                "version": "1.1.0",
                "inventory_date": d2,
            },
        ),
        ("list_oss_archive_provenance", {"namespace": h.NS}),
        ("propose_repository_package_matches", {"namespace": h.NS}),
        ("propose_publisher_organisation_matches", {"namespace": h.NS}),
        ("list_oss_identity_candidates", {"namespace": h.NS, "kind": "repository"}),
        (
            "create_package_monitor",
            {
                "namespace": h.NS,
                "request_key": "p",
                "watch": "package",
                "key": "pkg:pypi:fixture-parser",
            },
        ),
        ("register_oss_schemas", {}),
    ]
    results = {}
    for name, arguments in calls:
        state["scopes"] = set(OSS_SCOPES[name]) | NAMESPACE
        results[name] = tools[name].fn(**arguments)
        assert results[name].get("ok") is not False, (name, results[name])
    graph = results["dependency_graph_as_of"]
    state["scopes"] = set(OSS_SCOPES["replay_dependency_graph"]) | NAMESPACE
    assert (
        tools["replay_dependency_graph"].fn(receipt=graph["receipt"])["matched"] is True
    )
    candidate = next(
        c
        for c in results["list_oss_identity_candidates"]["candidates"]
        if c["right_key"].endswith("github.com/fixture-labs/codec")
    )
    follow_ups = [
        (
            "propose_oss_identity_link",
            {
                "namespace": h.NS,
                "left_key": "oss-package:pkg:npm:fixture-lexer",
                "right_key": "oss-repository:github.com/fixture-labs/lexer",
                "reason": "reviewer found the repository",
            },
        ),
        (
            "review_oss_identity_match",
            {
                "namespace": h.NS,
                "candidate_id": candidate["candidate_id"],
                "decision": "accept",
                "reason": "crate metadata",
            },
        ),
        (
            "revert_oss_identity_match",
            {
                "namespace": h.NS,
                "candidate_id": candidate["candidate_id"],
                "reason": "fork",
            },
        ),
        (
            "run_package_monitor",
            {"subscription_id": results["create_package_monitor"]["subscription_id"]},
        ),
        (
            "poll_package_monitor",
            {"subscription_id": results["create_package_monitor"]["subscription_id"]},
        ),
    ]
    for name, arguments in follow_ups:
        state["scopes"] = set(OSS_SCOPES[name]) | NAMESPACE
        result = tools[name].fn(**arguments)
        assert result.get("ok") is not False, (name, result)
    for name, arguments in calls:
        for missing in OSS_SCOPES[name]:
            state["scopes"] = (set(OSS_SCOPES[name]) - {missing}) | NAMESPACE
            result = tools[name].fn(**arguments)
            assert (
                result.get("ok") is False and result["error"]["code"] == "unauthorized"
            ), (name, missing, result)


def test_reads_before_any_source_ran_are_not_ready(tmp_path, monkeypatch):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    state = {
        "principal": "analyst",
        "scopes": {"knowledge:oss:read", "knowledge:technical:read"} | NAMESPACE,
    }
    tools = _connect(monkeypatch, path, state)
    for name, arguments in (
        (
            "package_release_history",
            {"namespace": h.NS, "package": "pkg:pypi:fixture-parser"},
        ),
        ("licence_history", {"namespace": h.NS, "package": "pkg:pypi:fixture-parser"}),
        (
            "packages_by_organisation",
            {"namespace": h.NS, "organisation": "fixture-labs"},
        ),
        (
            "dependency_graph_as_of",
            {
                "namespace": h.NS,
                "package": "pkg:pypi:fixture-parser",
                "version": "1.0.0",
                "date": "2026-04-01",
            },
        ),
        (
            "package_advisories",
            {"namespace": h.NS, "package": "pkg:pypi:fixture-parser"},
        ),
        (
            "compare_inventory_with_graph",
            {
                "namespace": h.NS,
                "inventory_id": "x",
                "package": "pkg:pypi:x",
                "version": "1.0",
            },
        ),
        ("list_oss_identity_candidates", {"namespace": h.NS}),
        ("list_oss_archive_provenance", {"namespace": h.NS}),
    ):
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
