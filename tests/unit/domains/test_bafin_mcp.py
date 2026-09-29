"""Market bafin-notices MCP entry points: catalog, declared scopes, read-only answers and not_ready (#2106, BF11)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import bafin_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.bafin_notices import (
    BAFIN_SCOPES,
    BAFIN_TOOLS,
    BAFIN_WRITES,
    OPTIONAL_SCOPES,
    QUERY_EXAMPLES,
)

NAMESPACE_SCOPES = {
    f"namespace:{h.NS}:read",
    f"namespace:{h.NS}:write",
    f"namespace:{h.OWN_NS}:read",
    f"namespace:{h.OWN_NS}:write",
}
REQUIRED = {
    "bafin_holders_as_of",
    "bafin_managers_transactions",
    "bafin_net_short_positions",
    "bafin_warnings_for_entity",
    "bafin_notice_dossier",
}
READS = {
    "bafin_holders_as_of": {"namespace": h.NS, "isin": h.ISSUER, "as_of": "2026-04-14"},
    "bafin_managers_transactions": {
        "namespace": h.NS,
        "isin": h.ISSUER,
        "date_from": "2026-01-01",
        "date_to": "2026-06-30",
    },
    "bafin_net_short_positions": {
        "namespace": h.NS,
        "isin": h.ISSUER,
        "as_of": "2026-05-01",
    },
    "bafin_warnings_for_entity": {
        "namespace": h.NS,
        "name": "Fiktiva Invest GmbH",
        "as_of": "2026-06-01",
    },
    "bafin_authorisation_status": {
        "namespace": h.NS,
        "as_of": "2026-06-15",
        "bafin_id": "123456",
    },
    "bafin_notice_dossier": {
        "namespace": h.NS,
        "isin": h.ISSUER,
        "as_of": "2026-07-02",
    },
    "list_bafin_identity_candidates": {"namespace": h.NS},
}


def declared(name):
    return set(_required_scopes("knowledge_engine_mcp", _mutability(name), name))


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "bafin-mcp.duckdb")
    state = {
        "principal": "alice",
        "scopes": set(h.SCOPES) | NAMESPACE_SCOPES,
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
    h.acquire_all(conn)
    h.ownership_entities(conn)
    conn.close()


def test_tools_are_registered_with_scopes_mutability_examples_and_in_the_catalog(
    mcp_env,
):
    tools, _ = mcp_env
    assert REQUIRED <= BAFIN_TOOLS <= set(tools)
    for name in BAFIN_TOOLS:
        assert _mutability(name) == ("write" if name in BAFIN_WRITES else "read"), name
        expected = BAFIN_SCOPES.get(
            name,
            ["market:bafin:write" if name in BAFIN_WRITES else "market:bafin:read"],
        )
        assert (
            _required_scopes("knowledge_engine_mcp", _mutability(name), name)
            == expected
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    assert {
        t["name"] for t in catalog["tools"] if t["name"] in BAFIN_TOOLS
    } == BAFIN_TOOLS
    descriptor = json.loads(
        (h.ROOT / "packs/market/providers/market.bafin.json").read_text()
    )
    assert {
        op["tool"].split(".", 1)[1] for op in descriptor["operations"]
    } <= BAFIN_TOOLS
    assert REQUIRED <= {op["tool"].split(".", 1)[1] for op in descriptor["operations"]}
    for op in descriptor["operations"]:
        assert set(op["required_scopes"]) == declared(op["tool"].split(".", 1)[1]), op[
            "id"
        ]
    assert set(QUERY_EXAMPLES) <= BAFIN_TOOLS and all(
        e["semantics"] for e in QUERY_EXAMPLES.values()
    )
    assert "no advice or signal" in " ".join(
        tools["bafin_holders_as_of"].description.lower().split()
    )


def test_every_public_entry_point_is_not_ready_before_any_source_ran(mcp_env):
    tools, state = mcp_env
    duckdb.connect(state["path"]).close()
    calls = {
        **READS,
        "inspect_bafin_notice": {"namespace": h.NS, "notice_id": "bafin-notice:x"},
    }
    for name, arguments in calls.items():
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
    polled = tools["poll_bafin_notice_monitor"].fn(
        subscription_id="knowledge-subscription:none"
    )
    assert polled["ok"] is False and polled["error"]["code"] == "not_ready"
    for name, arguments in {
        "project_bafin_voting_rights": {
            "namespace": h.NS,
            "ownership_namespace": h.OWN_NS,
        },
        "propose_bafin_identity_matches": {"namespace": h.NS},
    }.items():
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )


def test_reads_answer_on_a_read_only_connection_with_exactly_the_declared_scopes(
    mcp_env,
):
    tools, state = mcp_env
    load(state["path"])
    for name, arguments in READS.items():
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        result = tools[name].fn(**arguments)
        assert result.get("ok", True) is not False, (name, result)
        for missing in sorted(declared(name)):
            state["scopes"] = (declared(name) - {missing}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**arguments)
            assert (
                refused["ok"] is False and refused["error"]["code"] == "unauthorized"
            ), (name, missing)
    state["scopes"] = declared("bafin_holders_as_of") | NAMESPACE_SCOPES
    answer = tools["bafin_holders_as_of"].fn(**READS["bafin_holders_as_of"])
    assert [r["source"]["source_id"] for r in answer["holders"]] == [
        "VR-2026-0007",
        "VR-2026-0009",
    ]
    notice_id = answer["holders"][0]["notice_id"]
    state["scopes"] = declared("inspect_bafin_notice") | NAMESPACE_SCOPES
    inspected = tools["inspect_bafin_notice"].fn(namespace=h.NS, notice_id=notice_id)
    assert inspected["chain"]["link"]["status"] == "resolved"


def test_scopes_for_optional_arguments_are_checked_when_they_are_used(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    state["scopes"] = declared("bafin_notice_dossier") | NAMESPACE_SCOPES
    arguments = {**READS["bafin_notice_dossier"], "market_namespace": h.MARKET_NS}
    refused = tools["bafin_notice_dossier"].fn(**arguments)
    assert (
        refused["ok"] is False
        and OPTIONAL_SCOPES["market_namespace"] in refused["error"]["message"]
    )
    state["scopes"] |= {
        OPTIONAL_SCOPES["market_namespace"],
        f"namespace:{h.MARKET_NS}:read",
    }
    answered = tools["bafin_notice_dossier"].fn(**arguments)
    assert answered.get("ok", True) is not False
    assert (
        answered["issuer_resolution"]["instrument"]["status"] == "unresolved"
    )  # no instrument master acquired
    bundle = tools["bafin_notice_dossier"].fn(
        **READS["bafin_notice_dossier"], export_bundle=True
    )
    assert bundle["bundle"]["contract"] == "noesis-evidence-bundle-v1"


def test_writes_work_with_exactly_the_declared_scopes(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    from src.kb.subscriptions import SubscriptionStore

    conn = duckdb.connect(state["path"])
    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion")
    conn.close()
    writes = {
        "propose_bafin_identity_matches": {
            "namespace": h.NS,
            "ownership_namespace": h.OWN_NS,
        },
        "project_bafin_voting_rights": {
            "namespace": h.NS,
            "ownership_namespace": h.OWN_NS,
        },
        "create_bafin_notice_monitor": {
            "namespace": h.NS,
            "request_key": "k",
            "watch": "issuer",
            "isin": h.ISSUER,
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
        for c in results["propose_bafin_identity_matches"]["candidates"]
        if c["basis"] == "name-jurisdiction"
    )
    review = {
        "namespace": h.NS,
        "candidate_id": candidate["candidate_id"],
        "decision": "accept",
        "reason": "register name and country agree",
    }
    for scope in sorted(declared("review_bafin_identity_match")):
        state["scopes"] = (
            declared("review_bafin_identity_match") - {scope}
        ) | NAMESPACE_SCOPES
        assert tools["review_bafin_identity_match"].fn(**review)["ok"] is False, scope
    state["scopes"] = declared("review_bafin_identity_match") | NAMESPACE_SCOPES
    assert tools["review_bafin_identity_match"].fn(**review)["state"] == "accepted"
    state["scopes"] = declared("revert_bafin_identity_match") | NAMESPACE_SCOPES
    reverted = tools["revert_bafin_identity_match"].fn(
        namespace=h.NS, candidate_id=candidate["candidate_id"], reason="checked again"
    )
    assert reverted["state"] == "reverted"
    link = {
        "namespace": h.NS,
        "party": "bafin:named:fiktiva invest",
        "target_key": "bafin:party:fiktiva invest",
        "target_source": "warning text",
        "kind": "name",
        "value": "Fiktiva Invest GmbH",
    }
    state["scopes"] = declared("propose_bafin_identity_link") | NAMESPACE_SCOPES
    assert tools["propose_bafin_identity_link"].fn(**link).get("ok", True) is not False
    subscription_id = results["create_bafin_notice_monitor"]["subscription_id"]
    for scope in sorted(declared("run_bafin_notice_monitor")):
        state["scopes"] = (
            declared("run_bafin_notice_monitor") - {scope}
        ) | NAMESPACE_SCOPES
        assert (
            tools["run_bafin_notice_monitor"].fn(subscription_id=subscription_id)["ok"]
            is False
        ), scope
    state["scopes"] = declared("run_bafin_notice_monitor") | NAMESPACE_SCOPES
    ran = tools["run_bafin_notice_monitor"].fn(subscription_id=subscription_id)
    assert ran["baseline"] is True and ran["notifications"]
    state["scopes"] = declared("poll_bafin_notice_monitor") | NAMESPACE_SCOPES
    assert tools["poll_bafin_notice_monitor"].fn(subscription_id=subscription_id)[
        "events"
    ]
    # A review through the BaFin tools never decides a candidate that concerns no BaFin party.
    state["scopes"] = declared("review_bafin_identity_match") | NAMESPACE_SCOPES
    foreign = tools["review_bafin_identity_match"].fn(
        namespace=h.NS, candidate_id="own-idc:missing", decision="accept", reason="x"
    )
    assert foreign["ok"] is False
