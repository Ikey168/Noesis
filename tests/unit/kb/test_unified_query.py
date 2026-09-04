from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from src.kb.unified_query import (
    RESULT_CONTRACT,
    QueryCatalog,
    StaticQueryAdapter,
    UnifiedQueryEngine,
    UnifiedQueryError,
    capability_definition,
    validate_query_request,
)


def request(**updates):
    value = {
        "query": "energy price policy",
        "task": "compare the evidence",
        "scope": {"domains": ["economic", "political"]},
        "surfaces": ["lexical"],
        "budgets": {"max_results": 10, "per_source_results": 10, "token_budget": 800},
    }
    value.update(updates)
    return value


def adapter(source, items, **kwargs):
    return StaticQueryAdapter(
        source, items, domains=kwargs.pop("domains", ["economic"]), **kwargs
    )


def test_request_contract_is_canonical_and_cursor_independent():
    first = validate_query_request(request())
    second = validate_query_request(request(cursor="opaque"))
    assert first["contract"] == "noesis-knowledge-query-request-v1"
    assert first["request_hash"] == second["request_hash"]
    assert first["scope"]["domains"] == ["economic", "political"]


@pytest.mark.parametrize(
    "update,code",
    [
        ({"scope": {}}, "bad_request"),
        ({"surfaces": ["magic"]}, "bad_request"),
        ({"source_policy": {"include": ["x"], "exclude": ["x"]}}, "bad_request"),
        ({"memory": {"mode": "evidence"}}, "bad_request"),
        ({"budgets": {"max_results": 0}}, "bad_request"),
    ],
)
def test_request_rejects_ambiguous_or_unbounded_values(update, code):
    with pytest.raises(UnifiedQueryError) as caught:
        validate_query_request(request(**update))
    assert caught.value.code == code


def test_capability_catalog_is_stable_and_authorization_filtered():
    public = adapter("public", [])
    private = adapter("private", [], required_scopes=["private:read"])
    catalog = QueryCatalog([private, public])
    assert [
        item["source_id"] for item in catalog.capabilities(scopes={"knowledge:read"})
    ] == ["public"]
    assert catalog.fingerprint(scopes={"knowledge:read"}) == catalog.fingerprint(
        scopes={"knowledge:read"}
    )
    assert capability_definition(
        "x", "static", surfaces=["lexical"], object_types=["document"]
    )["capability_hash"]


def test_plan_is_deterministic_budgeted_and_explainable():
    engine = UnifiedQueryEngine(QueryCatalog([adapter("b", []), adapter("a", [])]))
    one = engine.plan(
        request(source_policy={"exclude": ["b"]}), scopes={"knowledge:read"}
    )
    two = engine.plan(
        request(source_policy={"exclude": ["b"]}), scopes={"knowledge:read"}
    )
    assert one == two
    assert one["selected_sources"] == ["a"]
    assert one["omitted"] == [{"source": "b", "reason": "excluded"}]
    assert one["nodes"][0]["budget"]["max_results"] == 10
    assert one["nodes"][-1]["depends_on"] == ["merge"]


def test_required_source_must_be_plannable_and_must_succeed():
    missing = UnifiedQueryEngine(QueryCatalog([adapter("a", [])]))
    with pytest.raises(UnifiedQueryError, match="required") as caught:
        missing.plan(
            request(source_policy={"required": ["missing"]}), scopes={"knowledge:read"}
        )
    assert caught.value.code == "required_source_unavailable"
    failing = UnifiedQueryEngine(
        QueryCatalog([adapter("a", [], fail="source_unavailable")])
    )
    with pytest.raises(UnifiedQueryError) as caught:
        failing.execute(
            request(source_policy={"required": ["a"]}), scopes={"knowledge:read"}
        )
    assert caught.value.code == "required_source_failed"


def test_merge_preserves_native_scores_provenance_and_independence():
    shared = {
        "canonical_id": "policy:1",
        "id": "a-row",
        "origin_id": "wire:1",
        "text": "Price rose",
        "score": 0.91,
        "url": "https://a.example/1",
    }
    other = {
        "canonical_id": "policy:1",
        "id": "b-row",
        "origin_id": "report:9",
        "text": "Price increased",
        "score": 12,
        "url": "https://b.example/9",
    }
    engine = UnifiedQueryEngine(
        QueryCatalog([adapter("a", [shared]), adapter("b", [other])])
    )
    result = engine.execute(request(), scopes={"knowledge:read"})
    assert result["contract"] == RESULT_CONTRACT
    assert result["items"][0]["identity"] == "policy:1"
    assert result["items"][0]["independent_source_count"] == 2
    assert {item["native_score"] for item in result["items"][0]["evidence"]} == {
        0.91,
        12,
    }
    assert len(result["items"][0]["citations"]) == 2
    assert result["contradictions"] == ["policy:1"]
    assert result["context"]["context_contract"] == "noesis-context-v1"


def test_same_origin_is_not_false_corroboration_and_text_is_not_identity():
    rows = [
        adapter("a", [{"id": "one", "origin_id": "same", "text": "identical"}]),
        adapter("b", [{"id": "two", "origin_id": "same", "text": "identical"}]),
    ]
    result = UnifiedQueryEngine(QueryCatalog(rows)).execute(
        request(), scopes={"knowledge:read"}
    )
    assert len(result["items"]) == 1
    assert result["items"][0]["independent_source_count"] == 1


class MemoryFixture:
    def __init__(self):
        self.definition = capability_definition(
            "memory:research",
            "memory",
            namespaces=["research"],
            surfaces=["memory"],
            object_types=["memory"],
            temporal=True,
        )

    def describe(self):
        return self.definition

    def query(self, child, *, scopes):
        return {
            "source": "memory:research",
            "items": [
                {
                    "id": "m1",
                    "origin_id": "m1",
                    "object_type": "memory",
                    "text": "stored carbon context",
                    "score": 1,
                    "native_rank": 1,
                    "citations": [],
                    "evidence_class": "context-only",
                }
            ],
            "provenance": {
                "source_id": "memory:research",
                "capability_hash": self.definition["capability_hash"],
            },
        }


def test_memory_expands_query_but_never_becomes_evidence():
    local = adapter(
        "local",
        [
            {
                "id": "d1",
                "origin_id": "d1",
                "text": "carbon evidence",
                "url": "https://e.example",
            }
        ],
    )
    engine = UnifiedQueryEngine(QueryCatalog([MemoryFixture(), local]))
    result = engine.execute(
        request(
            scope={"namespaces": ["research"]},
            surfaces=["lexical", "memory"],
            memory={"mode": "query-expansion"},
        ),
        scopes={"knowledge:read"},
    )
    assert "stored carbon context" in local.calls[0]["query"]
    assert result["memory_context"][0]["id"] == "m1"
    assert result["memory_policy"]["counts_as_evidence"] is False
    assert all(
        evidence["source"] != "memory:research"
        for item in result["items"]
        for evidence in item["evidence"]
    )


def test_remote_sources_require_explicit_policy_and_scope():
    remote = adapter(
        "remote",
        [{"id": "r1", "text": "remote"}],
        remote=True,
        required_scopes=["knowledge:federation:read"],
    )
    engine = UnifiedQueryEngine(QueryCatalog([remote]))
    denied = engine.plan(request(), scopes={"knowledge:read"})
    assert denied["omitted"][0]["reason"] == "remote_disabled"
    allowed = engine.plan(
        request(source_policy={"allow_remote": True}),
        scopes={"knowledge:federation:read"},
    )
    assert allowed["selected_sources"] == ["remote"]


def test_historical_plan_excludes_current_only_sources():
    current = adapter("current", [{"id": "future", "text": "future"}])
    history = adapter(
        "history",
        [{"id": "past", "text": "past", "observed_at_ms": 100}],
        surfaces=["temporal"],
        temporal=True,
    )
    engine = UnifiedQueryEngine(QueryCatalog([current, history]))
    plan = engine.plan(
        request(surfaces=["lexical", "temporal"], temporal={"as_of": 200}),
        scopes={"knowledge:read"},
    )
    assert plan["selected_sources"] == ["history"]
    assert {item["reason"] for item in plan["omitted"]} == {"temporal_unsupported"}


def test_temporal_adapter_prevents_later_observation_leakage():
    duckdb = pytest.importorskip("duckdb")
    from src.kb.temporal import record_temporal_assertion
    from src.kb.unified_query import TemporalQueryAdapter

    conn = duckdb.connect()
    backing = SimpleNamespace(
        conn=conn,
        backing_type="corpus-view",
        definition=SimpleNamespace(name="research", tags=[]),
        documents=lambda limit=50: [],
        claims=lambda limit=50: [],
        entities=list,
    )
    record_temporal_assertion(
        conn,
        domain="research",
        backing="corpus-view",
        assertion_kind="claim",
        assertion_id="rate",
        payload={"value": 1},
        observed_at_ms=100,
        valid_from_ms=50,
        valid_to_ms=500,
    )
    record_temporal_assertion(
        conn,
        domain="research",
        backing="corpus-view",
        assertion_kind="claim",
        assertion_id="rate",
        payload={"value": 2},
        observed_at_ms=300,
        valid_from_ms=50,
        valid_to_ms=500,
    )
    engine = UnifiedQueryEngine(
        QueryCatalog([TemporalQueryAdapter("research", backing)])
    )
    result = engine.execute(
        request(
            scope={"domains": ["research"]},
            surfaces=["temporal"],
            temporal={"as_of": 200, "history": True},
        ),
        scopes={"knowledge:read"},
    )
    native = result["items"][0]["evidence"][0]["native_fields"]
    assert native["payload"] == {"value": 1}
    assert native["observed_at_ms"] == 100


def test_end_to_end_local_memory_historical_and_fake_remote():
    local = adapter(
        "local-history",
        [{"id": "local:1", "text": "historical local", "url": "https://local/1"}],
        surfaces=["temporal"],
        temporal=True,
    )
    remote = adapter(
        "remote-history",
        [{"id": "remote:1", "text": "historical remote", "url": "https://remote/1"}],
        surfaces=["temporal"],
        temporal=True,
        remote=True,
        required_scopes=["knowledge:federation:read"],
    )
    engine = UnifiedQueryEngine(QueryCatalog([MemoryFixture(), local, remote]))
    result = engine.execute(
        request(
            scope={"namespaces": ["research"]},
            surfaces=["memory", "temporal"],
            temporal={"as_of": 200},
            memory={"mode": "query-expansion"},
            source_policy={"allow_remote": True},
        ),
        scopes={"knowledge:read", "knowledge:federation:read"},
    )
    assert {item["identity"] for item in result["items"]} == {"local:1", "remote:1"}
    assert result["memory_context"]
    assert result["execution_receipt"]["memory_expansion_ids"] == ["m1"]
    assert result["memory_policy"]["counts_as_evidence"] is False


def test_partial_failure_and_cancellation_are_honest():
    engine = UnifiedQueryEngine(
        QueryCatalog(
            [
                adapter("ok", [{"id": "x", "text": "x"}]),
                adapter("bad", [], fail="source_unavailable"),
            ]
        )
    )
    result = engine.execute(request(), scopes={"knowledge:read"})
    assert result["status"] == "partial"
    assert result["coverage"]["partial"] is True
    assert result["failures"][0]["source"] == "bad"
    cancelled = threading.Event()
    cancelled.set()
    result = engine.execute(
        request(), scopes={"knowledge:read"}, cancelled=cancelled.is_set
    )
    assert result["status"] == "cancelled"


def test_cursor_drift_plan_drift_replay_and_evaluation():
    engine = UnifiedQueryEngine(
        QueryCatalog(
            [
                adapter(
                    "a",
                    [
                        {"id": str(i), "text": f"row {i}", "url": f"https://e/{i}"}
                        for i in range(3)
                    ],
                )
            ]
        )
    )
    first = engine.execute(
        request(
            budgets={"max_results": 1, "per_source_results": 3, "token_budget": 100}
        ),
        scopes={"knowledge:read"},
    )
    cursor = first["page"]["next_cursor"]
    second = engine.execute(
        request(
            cursor=cursor,
            budgets={"max_results": 1, "per_source_results": 3, "token_budget": 100},
        ),
        scopes={"knowledge:read"},
    )
    assert second["page"]["offset"] == 1
    payload = json.loads(
        __import__("base64").urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    )
    payload["catalog_hash"] = "0" * 64
    bad = (
        __import__("base64")
        .urlsafe_b64encode(json.dumps(payload).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(UnifiedQueryError) as caught:
        engine.execute(
            request(
                cursor=bad,
                budgets={
                    "max_results": 1,
                    "per_source_results": 3,
                    "token_budget": 100,
                },
            ),
            scopes={"knowledge:read"},
        )
    assert caught.value.code == "cursor_drift"
    replay = engine.replay(
        request(
            budgets={"max_results": 1, "per_source_results": 3, "token_budget": 100}
        ),
        first,
        scopes={"knowledge:read"},
    )
    assert replay["matched"] is True
    evaluation = engine.evaluate(first, expected_ids=["0"])
    assert evaluation["passed"] is True
    assert evaluation["metrics"]["recall"] == 1


def test_contract_schemas_accept_engine_values():
    jsonschema = pytest.importorskip("jsonschema")
    root = __import__("pathlib").Path(__file__).resolve().parents[3]
    engine = UnifiedQueryEngine(
        QueryCatalog([adapter("a", [{"id": "x", "text": "x", "url": "https://e/x"}])])
    )
    plan = engine.plan(request(), scopes={"knowledge:read"})
    result = engine.execute(request(), scopes={"knowledge:read"})
    values = [(plan["request"], "request"), (plan, "plan"), (result, "result")]
    for value, suffix in values:
        schema = json.loads(
            (
                root
                / "contracts"
                / "schemas"
                / "jsonschema"
                / f"noesis-knowledge-query-{suffix}-v1.json"
            ).read_text()
        )
        jsonschema.validate(value, schema)
