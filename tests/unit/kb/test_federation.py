from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.federation import (
    FakeKnowledgeAdapter,
    FederatedQueryEngine,
    FederationError,
    FederationRegistry,
    GraphStoreAdapter,
    RemoteMCPAdapter,
    SQLKnowledgeAdapter,
    VectorStoreAdapter,
    source_definition,
)

ROOT = Path(__file__).resolve().parents[3]
SCOPES = {"knowledge:federation:read", "namespace:research:read"}


def test_contract_schema_and_fixture() -> None:
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-knowledge-source-v1.json").read_text())
    fixture = json.loads((ROOT / "contracts/examples/federation/fake-source.json").read_text())
    Draft7Validator.check_schema(schema)
    assert not list(Draft7Validator(schema).iter_errors(fixture))
    definition = source_definition(
        "fixture-research", "fake", capabilities=["search", "temporal"],
        schemas={"knowledge": {"type": "object"}},
        limits={"max_results": 100, "timeout_ms": 1000, "max_bytes": 1_000_000},
        freshness={"kind": "fixture"}, score_semantics="higher-is-better",
    )
    assert definition == fixture


def test_fake_adapter_pagination_authorization_and_redaction() -> None:
    adapter = FakeKnowledgeAdapter("fake", [{"id": "a"}, {"id": "b"}])
    with pytest.raises(FederationError, match="cannot query"):
        adapter.query({}, scopes=set())
    first = adapter.query({"limit": 1, "token": "never-log"}, scopes=SCOPES)
    assert first["cursor"] == "1"
    assert first["provenance"]["query_hash"]
    assert "never-log" not in json.dumps(first)
    assert adapter.query({"limit": 1, "cursor": first["cursor"]}, scopes=SCOPES)["items"][0]["id"] == "b"


def test_sql_adapter_typed_queries_injection_and_partial_schema(tmp_path: Path) -> None:
    path = tmp_path / "source.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE facts(id TEXT, value INTEGER, private TEXT)")
    conn.execute("INSERT INTO facts VALUES ('a', 2, 'x'), ('b', 4, 'y')")
    conn.close()

    def connect():
        return duckdb.connect(str(path), read_only=True)

    adapter = SQLKnowledgeAdapter("sql", connect, {"facts": ("id", "value")})
    schema = adapter.discover_schema(scopes=SCOPES)
    assert [item["name"] for item in schema["tables"]["facts"]] == ["id", "value"]
    result = adapter.query(
        {"table": "facts", "columns": ["id", "value"], "filters": [{"column": "value", "operator": "gte", "value": 3}]},
        scopes=SCOPES,
    )
    assert result["items"][0]["value"] == {"id": "b", "value": 4}
    assert adapter.query({"table": "facts", "operation": "aggregate", "columns": ["value"], "aggregate": "avg"}, scopes=SCOPES)["items"][0]["value"]["value"] == 3
    for bad in ({"sql": "DROP TABLE facts"}, {"table": "facts; DROP TABLE facts"}, {"table": "facts", "columns": ["private"]}):
        with pytest.raises(FederationError): adapter.query(bad, scopes=SCOPES)


class _MCP:
    version = "2"
    def list_resources(self): return [{"uri": "kb://facts"}]
    def list_tools(self): return [{"name": "search"}]
    def read_resource(self, uri): return {"uri": uri, "password": "leak"}
    def call_tool(self, name, arguments): return {"name": name, "arguments": arguments}


def test_remote_mcp_cache_allowlist_provenance_and_untrusted_content() -> None:
    adapter = RemoteMCPAdapter("remote", _MCP(), resources=["kb://facts"], tools=["search"])
    assert adapter.capabilities(scopes=SCOPES)["version"] == "2"
    result = adapter.query({"kind": "resource", "name": "kb://facts"}, scopes=SCOPES)
    assert result["items"][0]["value"]["password"] == "[REDACTED]"
    assert result["provenance"]["source_id"] == "remote"
    with pytest.raises(FederationError): adapter.query({"kind": "tool", "name": "write"}, scopes=SCOPES)


class _Vector:
    def search(self, query, **kwargs): return [{"id": "shared", "score": .8, "metric": "cosine", "value": {"claim": "x"}}]


class _Graph:
    def traverse(self, start, **kwargs): return [{"id": "edge", "depth": 1, "path": [start, "b"]}]


def test_vector_graph_limits_and_namespace_authorization() -> None:
    vector = VectorStoreAdapter("vector", _Vector(), namespaces=["research"])
    graph = GraphStoreAdapter("graph", _Graph(), namespaces=["research"], max_depth=2)
    assert vector.query({"namespace": "research", "text": "x"}, scopes=SCOPES)["items"][0]["backend"]["metric"] == "cosine"
    assert graph.query({"namespace": "research", "start_id": "a", "depth": 2}, scopes=SCOPES)["items"][0]["backend"]["path"] == ["a", "b"]
    with pytest.raises(FederationError): graph.query({"namespace": "research", "start_id": "a", "depth": 3}, scopes=SCOPES)
    with pytest.raises(FederationError): vector.query({"namespace": "private", "text": "x"}, scopes=SCOPES)


def test_federated_plan_merge_partial_failure_contradictions_and_replay() -> None:
    first = FakeKnowledgeAdapter("a", [{"id": "same", "value": {"claim": "yes"}}, {"id": "one"}])
    second = FakeKnowledgeAdapter("b", [{"id": "same", "value": {"claim": "no"}}])
    engine = FederatedQueryEngine(FederationRegistry([first, second]))
    request = {"capability": "search", "per_source": {"b": {"fail": True}}}
    partial = engine.execute(request, scopes=SCOPES)
    assert partial["coverage"]["partial"] is True
    assert partial["failures"][0]["source"] == "b"
    complete = engine.execute({"capability": "search"}, scopes=SCOPES)
    assert len(complete["results"]) == 2
    assert complete["contradictions"] == ["same"]
    again = engine.execute({"capability": "search"}, scopes=SCOPES)
    assert complete["replay_hash"] == again["replay_hash"]
    evaluation = engine.evaluate(complete, expected_ids=["same", "missing"])
    assert evaluation["recall"] == .5
    assert evaluation["provenance_completeness"] == 1
