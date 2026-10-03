"""Linguistics MCP entry points: catalog, declared scopes, exact-scope runs and not_ready (LG11, #2189)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.subscriptions import SubscriptionStore
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import linguistics_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.linguistics import (
    LINGUISTICS_SCOPES,
    LINGUISTICS_TOOLS,
    LINGUISTICS_WRITES,
    QUERY_EXAMPLES,
)

NS = h.NS
NAMESPACE_SCOPES = {
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "namespace:global:read",
    "namespace:global:write",
}
REQUIRED = {
    "lookup_lexeme",
    "sense_history",
    "lexeme_etymology",
    "languoid_profile",
    "propose_languoid_matches",
    "propose_lexeme_matches",
    "create_linguistics_monitor",
}
SENSE = "sense:kaikki-wiktextract:qnv:tamo:noun:1#sense:en-tamo-qnv-noun-Ab1Cd2"
READS = {
    "lookup_lexeme": {"namespace": NS, "lemma": "tamo", "languoid": "nort3456"},
    "sense_history": {"namespace": NS, "sense": SENSE, "as_of": "2026-04-01"},
    "definition_changes": {"namespace": NS, "sense": SENSE},
    "lexeme_etymology": {"namespace": NS, "lexeme": "lexeme:wikidata-lexemes:L90001"},
    "languoid_profile": {"namespace": NS, "languoid": "qnv"},
    "typological_profile": {"namespace": NS, "languoid": "nort3456"},
    "lexeme_cross_language_links": {
        "namespace": NS,
        "key": "lexeme:kaikki-wiktextract:qnv:tamo:noun:1",
    },
    "list_linguistic_identity_candidates": {"namespace": NS},
    "export_lexeme_dossier": {
        "namespace": NS,
        "lemma": "tamo",
        "target_licence": "CC-BY-SA-4.0",
    },
}


def declared(name):
    return set(_required_scopes("knowledge_engine_mcp", _mutability(name), name))


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "linguistics-mcp.duckdb")
    state = {"principal": "alice", "scopes": set(h.SCOPES), "path": path}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(state["path"], read_only=read_only),
    )
    return tool_map(server.mcp), state


def load(path):
    conn = duckdb.connect(path)
    h.load_all(conn)
    SubscriptionStore(conn).commit_watermark(NS, 1, kind="ingestion")
    conn.close()


def test_tools_are_registered_with_declared_scopes_mutability_and_in_the_catalog(
    mcp_env,
):
    tools, _ = mcp_env
    assert REQUIRED <= LINGUISTICS_TOOLS <= set(tools)
    for name in LINGUISTICS_TOOLS:
        assert _mutability(name) == (
            "write" if name in LINGUISTICS_WRITES else "read"
        ), name
        default = [
            "knowledge:linguistics:write"
            if name in LINGUISTICS_WRITES
            else "knowledge:linguistics:read"
        ]
        assert _required_scopes(
            "knowledge_engine_mcp", _mutability(name), name
        ) == LINGUISTICS_SCOPES.get(name, default)
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    assert {
        t["name"] for t in catalog["tools"] if t["name"] in LINGUISTICS_TOOLS
    } == LINGUISTICS_TOOLS
    for path in (
        "linguistics.lexicon",
        "linguistics.languoids",
        "linguistics.typology",
    ):
        descriptor = json.loads(
            (h.ROOT / f"packs/linguistics/providers/{path}.json").read_text()
        )
        for operation in descriptor["operations"]:
            name = operation["tool"].split(".", 1)[1]
            assert set(operation["required_scopes"]) == declared(name), name
    assert set(QUERY_EXAMPLES) <= LINGUISTICS_TOOLS
    assert "never shown as sourced definitions" in tools["lookup_lexeme"].description


def test_every_public_read_is_not_ready_before_any_source_ran(mcp_env):
    tools, state = mcp_env
    duckdb.connect(state["path"]).close()
    for name, arguments in READS.items():
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
    polled = tools["poll_linguistics_monitor"].fn(
        subscription_id="knowledge-subscription:none"
    )
    assert polled["ok"] is False and polled["error"]["code"] == "not_ready"
    for name in (
        "propose_lexeme_matches",
        "propose_languoid_matches",
        "record_iso_code_events",
    ):
        result = tools[name].fn(namespace=NS)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", name
    readiness = tools["linguistics_readiness"].fn(namespace=NS)
    assert (
        readiness["providers"]["glottolog"]["state"] == "not_acquired"
        and readiness["n"] == 0
    )


def test_reads_run_read_only_with_exactly_the_declared_scopes(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    for name, arguments in {
        **READS,
        "linguistics_readiness": {"namespace": NS},
    }.items():
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        result = tools[name].fn(**arguments)
        assert result.get("ok", True) is not False, (name, result)
        for scope in sorted(declared(name)):
            state["scopes"] = (declared(name) - {scope}) | NAMESPACE_SCOPES
            assert tools[name].fn(**arguments)["ok"] is False, (name, scope)
    state["scopes"] = declared("export_lexeme_dossier") | NAMESPACE_SCOPES
    refused = tools["export_lexeme_dossier"].fn(
        namespace=NS, lemma="tamo", target_licence="CC-BY-4.0"
    )
    assert refused["ok"] is False and refused["error"]["code"] == "export_refused"


def test_writes_work_with_exactly_the_declared_scopes(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    writes = {
        "propose_languoid_matches": {"namespace": NS},
        "propose_lexeme_matches": {"namespace": NS},
        "record_iso_code_events": {"namespace": NS},
        "project_languoid_locations": {"namespace": NS},
        "index_linguistic_texts": {"namespace": NS},
        "propose_lexeme_alias": {
            "namespace": NS,
            "lexeme": "lexeme:wikidata-lexemes:L90001",
            "entity_id": "entity:river",
            "item": "Q4022",
        },
        "create_linguistics_monitor": {
            "namespace": NS,
            "request_key": "k",
            "watch": "languoid",
            "target": "coas1122",
        },
    }
    results = {}
    for name, arguments in writes.items():
        for scope in sorted(declared(name)):
            state["scopes"] = (declared(name) - {scope}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**arguments)
            assert (
                refused["ok"] is False and refused["error"]["code"] == "unauthorized"
            ), (name, scope, refused)
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        results[name] = tools[name].fn(**arguments)
        assert results[name].get("ok", True) is not False, (name, results[name])
    candidate = results["propose_lexeme_matches"]["candidates"][0]["candidate_id"]
    state["scopes"] = declared("review_linguistic_identity_match") | NAMESPACE_SCOPES
    reviewed = tools["review_linguistic_identity_match"].fn(
        namespace=NS, candidate_id=candidate, decision="accept", reason="same word"
    )
    assert reviewed["state"] == "accepted"
    state["scopes"] = declared("revert_linguistic_identity_match") | NAMESPACE_SCOPES
    assert (
        tools["revert_linguistic_identity_match"].fn(
            namespace=NS, candidate_id=candidate, reason="error"
        )["state"]
        == "reverted"
    )
    resolution = results["project_languoid_locations"]["links"][0]["resolution_id"]
    state["scopes"] = declared("review_languoid_location") | NAMESPACE_SCOPES
    assert (
        tools["review_languoid_location"].fn(
            namespace=NS,
            resolution_id=resolution,
            decision="accept",
            reason="cited point",
        )["decision"]
        == "accept"
    )
    subscription_id = results["create_linguistics_monitor"]["subscription_id"]
    for scope in sorted(declared("run_linguistics_monitor")):
        state["scopes"] = (
            declared("run_linguistics_monitor") - {scope}
        ) | NAMESPACE_SCOPES
        assert (
            tools["run_linguistics_monitor"].fn(subscription_id=subscription_id)["ok"]
            is False
        ), scope
    state["scopes"] = declared("run_linguistics_monitor") | NAMESPACE_SCOPES
    ran = tools["run_linguistics_monitor"].fn(subscription_id=subscription_id)
    assert ran["baseline"] is True and ran["notifications"]
    state["scopes"] = declared("poll_linguistics_monitor") | NAMESPACE_SCOPES
    assert tools["poll_linguistics_monitor"].fn(subscription_id=subscription_id)[
        "events"
    ]
