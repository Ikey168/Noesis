"""Contract, fusion, selection, compression, and adapter tests for context v1."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb import contract
from src.kb.context import (
    CONTEXT_CONTRACT,
    TRUNCATION_MARKER,
    ContextAssemblyError,
    ContextRequest,
    assemble_context,
    evaluate_context,
)
from src.kb.context_eval import evaluate_fixture
from src.osint.independence import METHOD_VERSION, ensure_independence_schema

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "contracts/schemas/jsonschema"


def _row(identifier: str, text: str, source: str, **extra):
    return {
        "document_id": identifier,
        "content": text,
        "source": source,
        "url": f"https://example.test/{identifier}",
        **extra,
    }


class FixtureBacking:
    def __init__(self, domain: str, model: str, conn) -> None:
        self.definition = SimpleNamespace(name=domain, embedding_model=model)
        self.conn = conn

    def search(self, _query, limit=20):
        return [
            _row("copy-a", "A wire report says output grew by four percent.", "wire", score=9000),
            _row("copy-b", "A wire report says output grew by four percent.", "affiliate", score=-4),
            _row(
                "copy-c",
                "An affiliate repeats the wire estimate of four percent growth.",
                "syndicator",
            ),
            _row("audit", "An independent audit measured growth near three percent.", "auditor"),
            _row(
                "revision",
                "A later bulletin contradicts the original growth estimate.",
                "regulator",
                contradicting=True,
            ),
        ][:limit]

    def semantic_search(self, _query, limit=20):
        return [
            _row(
                "audit",
                "An independent audit measured growth near three percent.",
                "auditor",
                similarity=0.02,
            )
        ][:limit]

    def documents(self, limit=50):
        return [
            _row("report", "The complete report includes methods and limitations.", "archive")
        ][:limit]

    def claims(self, limit=50):
        return [
            {
                "claim_id": "claim-growth",
                "statement": "Output increased during the measured quarter.",
                "source": "researcher",
                "citations": [{"document_id": "claim-doc", "section": "results"}],
            }
        ][:limit]

    def entities(self, _name=None):
        return [
            {
                "canonical_id": "entity-agency",
                "name": "National Statistics Agency",
                "source": "registry",
                "citations": [{"document_id": "entity-doc"}],
            }
        ]

    def relations(self, _query, limit=50):
        return [
            {
                "relation_id": "relation-published",
                "text": "The agency published the quarterly report.",
                "source": "graph",
                "citations": [{"document_id": "graph-doc"}],
            }
        ][:limit]

    def quantitative_search(self, _query, limit=50):
        return [
            {
                "observation_id": "observation-growth",
                "statement": "The indexed value changed from 100 to 104.",
                "source": "dataset",
                "citations": [{"document_id": "data-doc", "path": "series/value"}],
                "score": 999999,
            }
        ][:limit]


def _connection():
    conn = duckdb.connect()
    ensure_independence_schema(conn)
    links = {
        "copy-a": ("wire-origin", "likely_dependent"),
        "copy-b": ("wire-origin", "likely_dependent"),
        "copy-c": ("wire-origin", "likely_dependent"),
        "audit": ("audit-origin", "known_independent"),
        "revision": ("revision-origin", "known_independent"),
        "report": ("report-origin", "known_independent"),
        "claim-doc": ("claim-origin", "known_independent"),
        "entity-doc": ("entity-origin", "known_independent"),
        "graph-doc": ("graph-origin", "known_independent"),
        "data-doc": ("data-origin", "known_independent"),
    }
    for document_id, (origin, state) in links.items():
        conn.execute(
            "INSERT INTO document_origin_links VALUES (?, 'fixture', ?, ?, ?, "
            "0.9, 0.8, 1.0, '[]', '[]', 1, 'run')",
            [document_id, METHOD_VERSION, origin, state],
        )
    return conn


def _request(**overrides):
    payload = {
        "task": "Research quarterly output",
        "query": "output",
        "domains": ["research", "economics"],
        "token_budget": 2000,
    }
    payload.update(overrides)
    return payload


def test_request_types_and_scope_are_strict_and_json_friendly():
    request = ContextRequest.from_value(
        {"task": "Research", "domains": ["research"], "token_budget": 100}
    )
    assert "semantic" in request.allowed_surfaces
    assert isinstance(request.to_dict()["domains"], list)
    with pytest.raises(ContextAssemblyError, match="mutually exclusive"):
        ContextRequest.from_value(
            {
                "task": "Research",
                "domains": ["research"],
                "all_authorized": True,
                "token_budget": 100,
            }
        )
    with pytest.raises(ContextAssemblyError, match="unsupported retrieval"):
        ContextRequest.from_value(
            {
                "task": "Research",
                "domains": ["research"],
                "token_budget": 100,
                "allowed_surfaces": ["magic"],
            }
        )


def test_fusion_preserves_object_score_domain_and_duplicate_provenance():
    conn = _connection()
    resolved = [
        ("research", FixtureBacking("research", "embed-a", conn)),
        ("economics", FixtureBacking("economics", "embed-b", conn)),
    ]
    result = assemble_context(resolved, _request())
    assert result["context_contract"] == CONTEXT_CONTRACT
    assert result["status"] == "assembled"
    assert {item["object_type"] for item in result["items"]} == {
        "document",
        "passage",
        "claim",
        "entity",
        "relation",
        "observation",
    }
    assert result["fusion"]["mixed_embedding_spaces"] is True
    assert "raw scores are retained but not compared" in result["fusion"]["score_interpretation"]
    assert all(
        score["probability"] is False
        for item in result["items"]
        for score in item["score_provenance"]
    )
    duplicate = next(item for item in result["items"] if "wire report" in item["text"])
    assert duplicate["provenance"]["domains"] == ["economics", "research"]
    assert duplicate["provenance"]["sources"] == ["affiliate", "wire"]
    assert {locator["document_id"] for locator in duplicate["citations"]} == {
        "copy-a",
        "copy-b",
    }
    assert any("contradicts" in item["text"] for item in result["items"])


def test_diversity_keeps_independent_and_contradicting_evidence():
    conn = _connection()
    backing = FixtureBacking("research", "embed-a", conn)
    result = assemble_context(
        [("research", backing)],
        _request(
            domains=["research"],
            allowed_surfaces=["lexical"],
            diversity={
                "max_per_source": 1,
                "max_per_domain": 4,
                "max_per_object_type": 4,
                "max_per_origin": 1,
            },
        ),
    )
    texts = [item["text"] for item in result["items"]]
    assert any("contradicts" in text for text in texts)
    assert any("independent audit" in text for text in texts)
    assert sum("wire report" in text for text in texts) == 1
    assert any(
        item["reason"] == "equivalent_content_deduplicated"
        for item in result["exclusions"]
    )
    assert any(
        item["reason"] == "diversity_limit:origin"
        and item["value"] == "wire-origin"
        for item in result["exclusions"]
    )


def test_compression_preserves_stable_anchors_and_reports_quality():
    conn = _connection()
    backing = FixtureBacking("research", "embed-a", conn)
    result = assemble_context(
        [("research", backing)],
        _request(
            domains=["research"],
            token_budget=15,
            allowed_surfaces=["document"],
        ),
    )
    item = result["items"][0]
    assert item["compression"] == {
        "method": "extractive-first",
        "lossy": True,
        "truncation_marker": TRUNCATION_MARKER,
        "unsupported_facts_added": False,
    }
    assert all(anchor in item["text"] for anchor in item["citation_anchors"])
    assert result["token_accounting"]["used"] <= 15
    evaluation = evaluate_context(result)
    assert evaluation["passed"] is True
    assert set(evaluation["metrics"]) == {
        "budget_compliance",
        "citation_preservation",
        "answer_support",
        "redundancy",
        "latency_ms",
        "refusal_quality",
    }


def test_impossible_budget_refuses_deterministically():
    conn = _connection()
    backing = FixtureBacking("research", "embed-a", conn)
    request = _request(
        domains=["research"],
        token_budget=1,
        allowed_surfaces=["document"],
        required_object_types=["document"],
    )
    first = assemble_context([("research", backing)], request)
    second = assemble_context([("research", backing)], request)
    for payload in (first, second):
        assert payload["status"] == "refused"
        assert payload["items"] == []
        assert payload["refusal"]["code"] == "impossible_budget"
        assert evaluate_context(payload)["passed"] is True
    for payload in (first, second):
        for trace in payload["assembly_trace"]:
            trace.pop("elapsed_ms", None)
    assert first == second


def test_missing_backend_is_traced_and_healthy_results_survive():
    conn = _connection()

    class PartialBacking(FixtureBacking):
        def semantic_search(self, _query, limit=20):
            raise RuntimeError("index offline")

    result = assemble_context(
        [("research", PartialBacking("research", "embed-a", conn))],
        _request(
            domains=["research"],
            allowed_surfaces=["lexical", "semantic"],
        ),
    )
    assert result["status"] == "partial"
    assert result["items"]
    assert any(
        item["surface"] == "semantic" and item["status"] == "partial_failure"
        for item in result["assembly_trace"]
    )


def test_recency_constraint_excludes_old_and_unverifiable_timestamps():
    conn = _connection()

    class RecencyBacking(FixtureBacking):
        def documents(self, limit=50):
            return [
                _row("recent", "A recent cited record.", "archive", ingested_at=20),
                _row("old", "An old cited record.", "archive", ingested_at=5),
                _row("undated", "An undated cited record.", "archive"),
            ][:limit]

    result = assemble_context(
        [("research", RecencyBacking("research", "embed-a", conn))],
        _request(
            domains=["research"],
            allowed_surfaces=["document"],
            recency_after_ms=10,
        ),
    )
    assert [item["provenance"]["raw_object_id"] for item in result["items"]] == [
        "recent"
    ]
    assert {item["reason"] for item in result["exclusions"]} >= {
        "recency_before_cutoff",
        "recency_timestamp_missing",
    }


def test_graph_fallback_reads_political_table_when_technical_table_also_exists():
    from src.domains.political.model import ensure_political_schema
    from src.domains.technical.model import ensure_technical_schema

    conn = _connection()
    ensure_technical_schema(conn)
    ensure_political_schema(conn)
    conn.execute(
        "INSERT INTO political_relations VALUES "
        "('rel-1', 'politics', 'holds_office', 'person:one', 'office:one', "
        "NULL, NULL, 20, 'graph-doc', '{}', TRUE)"
    )

    class GraphBacking:
        definition = SimpleNamespace(name="politics", embedding_model="embed-a")

        def __init__(self, connection):
            self.conn = connection

    result = assemble_context(
        [("politics", GraphBacking(conn))],
        {
            "task": "Find holds_office relations",
            "query": "holds_office",
            "domains": ["politics"],
            "token_budget": 100,
            "allowed_surfaces": ["graph"],
        },
    )
    assert result["items"][0]["object_type"] == "relation"
    assert result["items"][0]["provenance"]["raw_object_id"] == "rel-1"


def test_request_and_response_validate_against_governed_schemas():
    conn = _connection()
    request = _request()
    response = assemble_context(
        [
            ("research", FixtureBacking("research", "embed-a", conn)),
            ("economics", FixtureBacking("economics", "embed-b", conn)),
        ],
        request,
    )
    request_schema = json.loads((SCHEMAS / "noesis-context-request-v1.json").read_text())
    response_schema = json.loads((SCHEMAS / "noesis-context-response-v1.json").read_text())
    Draft7Validator(request_schema).validate(request)
    Draft7Validator(response_schema).validate(response)


def test_python_contract_resolves_scope_and_returns_envelope(tmp_path):
    from tests.unit.kb.test_contract import CONFIG, _seed

    conn = duckdb.connect()
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    _seed(conn, config_path)
    payload = contract.kb_context(
        "Research staking",
        100,
        query="staking",
        domains=["web3"],
        allowed_surfaces=["lexical"],
        conn=conn,
        config_path=config_path,
    )
    assert payload["contract"] == "noesis-kb-v1"
    assert payload["domain"] == "context"
    assert payload["data"]["scope"]["selected_domains"] == ["web3"]
    assert payload["data"]["items"][0]["citations"]


def test_real_semantic_surface_is_filtered_to_domain_members(tmp_path):
    from services.embeddings.provider import EmbeddingProvider
    from src.ingestion.document_store import DocumentStore
    from src.ingestion.embed import embed_documents
    from src.kb.membership import run_membership_pass
    from src.kb.registry import load_registry
    from tests.unit.kb.test_contract import BASE_MS, CONFIG, _seed

    conn = duckdb.connect()
    provider = EmbeddingProvider(provider="hashing")
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG.replace("fake-embed", provider.name()))
    _seed(conn, config_path)
    DocumentStore(conn).upsert(
        [
            {
                "document_id": "outside",
                "source_type": "news",
                "language": "en",
                "ingested_at": BASE_MS,
                "source_id": "sports-wire",
                "url": "https://example.test/outside",
                "title": "Championship final",
                "content": "The team won the championship in overtime.",
                "metadata": {"tags": ["sports"]},
            }
        ]
    )
    registry = load_registry(config_path)
    run_membership_pass(conn, registry)
    embed_documents(conn, provider=provider)
    backing = registry.resolve("web3", conn=conn)
    results = backing.semantic_search("championship overtime", limit=10)
    assert [item["document_id"] for item in results] == ["d1"]


def test_offline_regression_fixture_passes():
    report = evaluate_fixture(
        ROOT / "tests/fixtures/context_assembly/regression.json"
    )
    assert report["passed"] is True
    assert report["n"] == 3


def test_contract_registry_exposes_context_schemas():
    from tools.contract_mcp.server import get_contract

    assert get_contract.fn("context-request")["id"] == "noesis-context-request-v1"
    assert get_contract.fn("context-response")["id"] == "noesis-context-response-v1"


def test_rest_and_mcp_adapters_share_the_python_contract(monkeypatch):
    sentinel = {
        "contract": "noesis-kb-v1",
        "domain": "context",
        "as_of_ms": 1,
        "data": {"context_contract": CONTEXT_CONTRACT},
    }
    calls = []

    def fake_context(*args):
        calls.append(args)
        return sentinel

    monkeypatch.setattr(contract, "kb_context", fake_context)
    from src.api.routes import kb_routes

    request = kb_routes.ContextAssemblyRequest(
        task="Research output", token_budget=100, domains=["research"]
    )
    rest = kb_routes.assemble_public_context(request)
    spec = importlib.util.spec_from_file_location(
        "context_kb_mcp", ROOT / "tools/kb_mcp/server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    mcp = tools["kb_context"].fn(
        task="Research output", token_budget=100, domains=["research"]
    )
    assert rest == mcp == sentinel
    assert len(calls) == 2
