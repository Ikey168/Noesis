"""Engineering Safety MCP entry points: catalog, declared scopes, readiness and answers (ES15, #2076)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.engineering_safety import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.engineering_safety import (
    ENGINEERING_SAFETY_SCOPES,
    ENGINEERING_SAFETY_TOOLS,
    ENGINEERING_SAFETY_WRITES,
    OPTIONAL_SCOPES,
    QUERY_EXAMPLES,
)

NAMESPACE_SCOPES = {f"namespace:{h.NS}:read", f"namespace:{h.NS}:write"}
REQUIRED = {
    "search_directives",
    "directives_as_of",
    "inspect_investigation",
    "search_safety_recommendations",
    "engineering_safety_dossier",
    "propose_safety_subject_matches",
}
READS = {
    "search_directives": {"namespace": h.NS, "text": "Examplar"},
    "search_defect_investigations": {
        "namespace": h.NS,
        "text": "VELOMARK",
        "include_complaints": True,
    },
    "directives_as_of": {"namespace": h.NS, "subject": h.EX100, "as_of": "2026-06-01"},
    "inspect_investigation": {"namespace": h.NS, "investigation": "ntsb:ERA26FA101"},
    "inspect_safety_record": {"namespace": h.NS, "record": "faa-ad:2026-04-12"},
    "search_safety_recommendations": {
        "namespace": h.NS,
        "addressee": "Examplar Aircraft Company",
    },
    "engineering_safety_dossier": {"namespace": h.NS, "subject": h.EX100},
    "list_safety_subject_matches": {"namespace": h.NS},
}


def declared(name):
    return set(_required_scopes("knowledge_engine_mcp", _mutability(name), name))


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "engineering-safety-mcp.duckdb")
    state = {
        "principal": "alice",
        "scopes": {"operator"} | NAMESPACE_SCOPES,
        "path": path,
    }
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(state["path"], read_only=read_only),
    )
    return asyncio.run(server.mcp.get_tools()), state


def load(path):
    conn = duckdb.connect(path)
    env = h.Env(conn)
    assert env.run("r1")["status"] == "complete"
    conn.close()


def test_tools_are_registered_with_scopes_mutability_examples_and_in_the_catalog(
    mcp_env,
):
    tools, _ = mcp_env
    assert REQUIRED <= ENGINEERING_SAFETY_TOOLS <= set(tools)
    assert set(ENGINEERING_SAFETY_SCOPES) == ENGINEERING_SAFETY_TOOLS
    for name in ENGINEERING_SAFETY_TOOLS:
        assert _mutability(name) == (
            "write" if name in ENGINEERING_SAFETY_WRITES else "read"
        ), name
        assert (
            _required_scopes("knowledge_engine_mcp", _mutability(name), name)
            == ENGINEERING_SAFETY_SCOPES[name]
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    listed = {
        t["name"]: t for t in catalog["tools"] if t["name"] in ENGINEERING_SAFETY_TOOLS
    }
    assert set(listed) == ENGINEERING_SAFETY_TOOLS
    assert all(
        listed[n]["required_scopes"] == ENGINEERING_SAFETY_SCOPES[n] for n in listed
    )
    operations = {}
    for path in (h.ROOT / "packs/engineering-safety/providers").glob("*.json"):
        for op in json.loads(path.read_text())["operations"]:
            operations[op["tool"].split(".", 1)[1]] = op
    assert (
        set(operations) == ENGINEERING_SAFETY_TOOLS
    )  # every tool is bound by exactly one descriptor
    for name, op in operations.items():
        assert set(op["required_scopes"]) == declared(name), name
        assert (op["side_effect"] == "read-only") == (
            name not in ENGINEERING_SAFETY_WRITES
        ), name
    assert set(QUERY_EXAMPLES) <= ENGINEERING_SAFETY_TOOLS and all(
        e["semantics"] for e in QUERY_EXAMPLES.values()
    )
    assert "not a compliance determination" in " ".join(
        tools["directives_as_of"].description.split()
    )


def test_every_public_entry_point_is_not_ready_before_any_source_ran(mcp_env):
    tools, state = mcp_env
    duckdb.connect(state["path"]).close()
    calls = {
        **READS,
        "propose_safety_subject_matches": {"namespace": h.NS},
        "review_safety_subject_match": {
            "namespace": h.NS,
            "match_id": "es-match:x",
            "decision": "accepted",
            "reason": "r",
        },
        "revert_safety_subject_match": {
            "namespace": h.NS,
            "match_id": "es-match:x",
            "reason": "r",
        },
        "link_safety_citations": {"namespace": h.NS},
        "create_engineering_safety_monitor": {
            "namespace": h.NS,
            "request_key": "k",
            "watch": {"authorities": ["us-faa"]},
        },
        "run_engineering_safety_monitor": {"subscription_id": "subscription:none"},
        "poll_engineering_safety_monitor": {"subscription_id": "subscription:none"},
    }
    assert set(calls) == ENGINEERING_SAFETY_TOOLS
    for name, arguments in calls.items():
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
    conn = duckdb.connect(state["path"], read_only=True)
    assert not conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='es_matches'"
    ).fetchone()


def test_reads_answer_on_a_read_only_connection_with_exactly_the_declared_scopes(
    mcp_env,
):
    tools, state = mcp_env
    load(state["path"])
    for name, arguments in READS.items():
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        result = tools[name].fn(**arguments)
        assert result.get("ok", True) is not False, (name, result)
        assert not h.forbidden_keys(result), name
        for missing in sorted(declared(name)):
            state["scopes"] = (declared(name) - {missing}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**arguments)
            assert (
                refused["ok"] is False and refused["error"]["code"] == "unauthorized"
            ), (name, missing)
    state["scopes"] = declared("directives_as_of") | NAMESPACE_SCOPES
    answer = tools["directives_as_of"].fn(
        **READS["directives_as_of"], export_bundle=True
    )
    assert answer["bundle"]["contract"] == "noesis-evidence-bundle-v1"
    assert [i["native_id"] for i in answer["answer"]["in_effect"]] == [
        "2026-0123",
        "2026-04-12",
    ]


def test_scopes_for_optional_arguments_are_checked_when_they_are_used(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    state["scopes"] = declared("engineering_safety_dossier") | NAMESPACE_SCOPES
    arguments = {
        "namespace": h.NS,
        "subject": {"kind": "vehicle", "make": "VELOMARK", "model": "CITYRUNNER"},
        "products_namespace": h.NS,
    }
    refused = tools["engineering_safety_dossier"].fn(**arguments)
    assert (
        refused["ok"] is False
        and OPTIONAL_SCOPES["products_namespace"][0] in refused["error"]["message"]
    )
    state["scopes"] |= set(OPTIONAL_SCOPES["products_namespace"])
    answered = tools["engineering_safety_dossier"].fn(**arguments)
    assert (
        answered.get("ok", True) is not False
        and answered["status"] == "records on file"
    )


def test_writes_work_with_exactly_the_declared_scopes(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    conn = duckdb.connect(state["path"])
    h.seed_entities(conn)
    conn.close()
    writes = {
        "propose_safety_subject_matches": {"namespace": h.NS},
        "link_safety_citations": {"namespace": h.NS},
        "create_engineering_safety_monitor": {
            "namespace": h.NS,
            "request_key": "k",
            "watch": {"subjects": [h.EX100]},
        },
    }
    results = {}
    for name, arguments in writes.items():
        for scope in sorted(declared(name)):
            state["scopes"] = (declared(name) - {scope}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**arguments)
            assert (
                refused["ok"] is False and refused["error"]["code"] == "unauthorized"
            ), (name, scope)
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        results[name] = tools[name].fn(**arguments)
        assert results[name].get("ok", True) is not False, (name, results[name])
    candidate = next(
        c
        for c in results["propose_safety_subject_matches"]["candidates"]
        if c["target_id"] == "ent-examplar-pipeline"
    )
    review = {
        "namespace": h.NS,
        "match_id": candidate["match_id"],
        "decision": "accepted",
        "reason": "same name",
    }
    for scope in sorted(declared("review_safety_subject_match")):
        state["scopes"] = (
            declared("review_safety_subject_match") - {scope}
        ) | NAMESPACE_SCOPES
        assert tools["review_safety_subject_match"].fn(**review)["ok"] is False, scope
    state["scopes"] = declared("review_safety_subject_match") | NAMESPACE_SCOPES
    assert tools["review_safety_subject_match"].fn(**review)["attached"] is True
    state["scopes"] = declared("revert_safety_subject_match") | NAMESPACE_SCOPES
    reverted = tools["revert_safety_subject_match"].fn(
        namespace=h.NS, match_id=candidate["match_id"], reason="checked again"
    )
    assert reverted["review_state"] == "reverted"
    subscription_id = results["create_engineering_safety_monitor"]["subscription_id"]
    for scope in sorted(declared("run_engineering_safety_monitor")):
        state["scopes"] = (
            declared("run_engineering_safety_monitor") - {scope}
        ) | NAMESPACE_SCOPES
        assert (
            tools["run_engineering_safety_monitor"].fn(subscription_id=subscription_id)[
                "ok"
            ]
            is False
        ), scope
    state["scopes"] = declared("run_engineering_safety_monitor") | NAMESPACE_SCOPES
    ran = tools["run_engineering_safety_monitor"].fn(subscription_id=subscription_id)
    assert ran["baseline"] is True and ran["notifications"]
    state["scopes"] = declared("poll_engineering_safety_monitor") | NAMESPACE_SCOPES
    assert tools["poll_engineering_safety_monitor"].fn(subscription_id=subscription_id)[
        "events"
    ]
