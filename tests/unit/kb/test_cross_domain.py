"""Cross-domain KB contract, retrieval, links, answers, and privacy tests."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.ingestion.document_store import DocumentStore
from src.kb import contract
from src.kb.answer_eval import evaluate_answer
from src.kb.claim_links import _upsert_link, ensure_claim_link_schema
from src.kb.clusters import ensure_cluster_schema
from src.kb.contract import KBContractError
from src.kb.cross_domain import (
    record_manual_claim_equivalence,
    resolve_scope,
    search_across,
    unlink_manual_claim_equivalence,
)
from src.kb.entities import run_entity_canonicalization_pass
from src.kb.membership import run_membership_pass
from src.kb.promotion import promote_to_namespace
from src.kb.registry import load_registry
from src.kb.watches import grant_watch_domain

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUEST_SCHEMA = REPO_ROOT / "contracts/schemas/jsonschema/noesis-cross-domain-request-v1.json"
RESPONSE_SCHEMA = REPO_ROOT / "contracts/schemas/jsonschema/noesis-cross-domain-response-v1.json"
ANSWER_SCHEMA = REPO_ROOT / "contracts/schemas/jsonschema/noesis-answer-v1.json"

CONFIG = """
version: 1
domains:
  - name: economics
    backing: corpus-view
    embedding_model: economics-embed
    tags: [economics]
    keywords: [inflation]
  - name: papers
    backing: corpus-view
    embedding_model: papers-embed
    tags: [papers]
    keywords: [research]
  - name: local
    backing: corpus-view
    embedding_model: economics-embed
    tags: [local, private]
    keywords: [private]
"""


def _seed(conn, config_path: Path) -> None:
    ensure_claim_link_schema(conn)
    ensure_cluster_schema(conn)
    DocumentStore(conn).upsert(
        [
            {
                "document_id": "shared",
                "source_type": "paper",
                "source_id": "Shared source",
                "language": "en",
                "ingested_at": 100,
                "url": "https://example.invalid/shared",
                "title": "Inflation research overview",
                "content": "Research about inflation and prices.",
                "metadata": {"tags": ["economics", "papers"]},
            },
            {
                "document_id": "econ-doc",
                "source_type": "news",
                "source_id": "Economic office",
                "language": "en",
                "ingested_at": 200,
                "url": "https://example.invalid/economics",
                "title": "Economic inflation release",
                "content": "Inflation rose three percent in 2025.",
                "metadata": {"tags": ["economics"]},
            },
            {
                "document_id": "paper-doc",
                "source_type": "paper",
                "source_id": "Research journal",
                "language": "en",
                "ingested_at": 300,
                "url": "https://example.invalid/paper",
                "title": "Scientific inflation study",
                "content": "The study found inflation rose three percent in 2025.",
                "metadata": {"tags": ["papers"]},
            },
            {
                "document_id": "private-doc",
                "source_type": "note",
                "source_id": "Private archive",
                "language": "en",
                "ingested_at": 400,
                "url": None,
                "title": "Private inflation memo",
                "content": "Private inflation planning notes.",
                "metadata": {"tags": ["local", "private"]},
            },
        ]
    )
    registry = load_registry(config_path)
    run_membership_pass(conn, registry)
    conn.executemany(
        "INSERT INTO argument_claims"
        " (claim_id, claim_text, document_id, source_type, confidence, prediction_mode)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "claim-econ",
                "Inflation rose three percent in 2025.",
                "econ-doc",
                "news",
                0.91,
                "deterministic:test",
            ),
            (
                "claim-paper",
                "The study found inflation rose three percent in 2025.",
                "paper-doc",
                "paper",
                0.89,
                "deterministic:test",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO claim_clusters VALUES (?, ?, 'test', 0)",
        [
            ("claim-econ", "cluster-econ"),
            ("claim-paper", "cluster-paper"),
        ],
    )
    conn.executemany(
        "INSERT INTO document_actors"
        " (document_id, source_type, actor_name, role, confidence)"
        " VALUES (?, ?, ?, 'subject', 1.0)",
        [
            ("econ-doc", "news", "Federal Reserve"),
            ("paper-doc", "paper", "Fed"),
        ],
    )
    run_entity_canonicalization_pass(
        conn, manual_aliases=[("Fed", "Federal Reserve")], run_id="entities-test"
    )
    record_manual_claim_equivalence(
        conn,
        "economics",
        "claim-econ",
        "papers",
        "claim-paper",
        actor="reviewer",
    )


@pytest.fixture()
def corpus(tmp_path):
    conn = duckdb.connect()
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    _seed(conn, config_path)
    return conn, config_path


def _validate(schema_path: Path, payload: dict) -> None:
    schema = json.loads(schema_path.read_text())
    errors = list(Draft7Validator(schema).iter_errors(payload))
    assert not errors, [f"{error.json_path}: {error.message}" for error in errors]


def test_request_contract_requires_exactly_one_scope_mode():
    validator = Draft7Validator(json.loads(REQUEST_SCHEMA.read_text()))
    valid = {
        "contract": "noesis-cross-domain-v1",
        "operation": "search",
        "domains": ["economics", "papers"],
        "all_authorized": False,
        "query": "inflation",
    }
    assert not list(validator.iter_errors(valid))
    assert list(validator.iter_errors({**valid, "all_authorized": True}))
    all_mode = dict(valid)
    all_mode.pop("domains")
    all_mode["all_authorized"] = True
    assert not list(validator.iter_errors(all_mode))


def test_search_deduplicates_documents_and_uses_rank_fusion(corpus):
    conn, config_path = corpus
    payload = contract.kb_search_domains(
        "inflation",
        domains=["economics", "papers"],
        limit=10,
        per_domain_limit=10,
        conn=conn,
        config_path=config_path,
    )
    _validate(RESPONSE_SCHEMA, payload)
    assert payload["domain"] == "cross-domain"
    assert payload["data"]["scope"]["selected_domains"] == ["economics", "papers"]
    assert payload["data"]["scope"]["embedding_models_compatible"] is False
    shared = next(row for row in payload["data"]["results"] if row["document_id"] == "shared")
    assert shared["domains"] == ["economics", "papers"]
    assert len(shared["retrieval"]) == 2
    assert shared["score_kind"] == "reciprocal-rank-fusion"
    assert all("as_of_ms" in hit for hit in shared["retrieval"])
    repeated = contract.kb_search_domains(
        "inflation",
        domains=["economics", "papers"],
        limit=10,
        per_domain_limit=10,
        conn=conn,
        config_path=config_path,
    )
    assert [row["document_id"] for row in payload["data"]["results"]] == [
        row["document_id"] for row in repeated["data"]["results"]
    ]
    assert payload["data"]["n"] <= 10
    assert all(
        hit["rank"] <= 10
        for row in payload["data"]["results"]
        for hit in row["retrieval"]
    )


def test_search_is_backing_independent_and_keeps_backing_receipts(corpus):
    conn, config_path = corpus
    promote_to_namespace(conn, "papers", config_path)
    payload = contract.kb_search_domains(
        "inflation",
        domains=["economics", "papers"],
        conn=conn,
        config_path=config_path,
    )
    assert payload["data"]["scope"]["domains"] == [
        {
            "domain": "economics",
            "backing": "corpus-view",
            "embedding_model": "economics-embed",
            "status": "ok",
        },
        {
            "domain": "papers",
            "backing": "namespace",
            "embedding_model": "papers-embed",
            "status": "ok",
        },
    ]
    shared = next(row for row in payload["data"]["results"] if row["document_id"] == "shared")
    assert {item["backing"] for item in shared["retrieval"]} == {
        "corpus-view",
        "namespace",
    }


def test_all_authorized_excludes_private_and_explicit_private_fails_closed(corpus):
    conn, config_path = corpus
    public = contract.kb_search_domains(
        "inflation",
        all_authorized=True,
        conn=conn,
        config_path=config_path,
    )
    assert public["data"]["scope"]["selected_domains"] == ["economics", "papers"]
    assert public["data"]["scope"]["excluded_domains"] == [
        {"domain": "local", "reason": "private_not_requested"}
    ]
    assert "private-doc" not in {
        row["document_id"] for row in public["data"]["results"]
    }

    with pytest.raises(KBContractError) as excinfo:
        contract.kb_search_domains(
            "private",
            domains=["local"],
            conn=conn,
            config_path=config_path,
        )
    assert excinfo.value.code == "unauthorized"

    grant_watch_domain(conn, "alice", "local", granted_at_ms=1)
    private = contract.kb_search_domains(
        "private",
        domains=["local"],
        principal_id="alice",
        include_private=True,
        conn=conn,
        config_path=config_path,
    )
    assert {row["document_id"] for row in private["data"]["results"]} == {
        "private-doc"
    }


def test_bad_scope_unknown_domain_and_limits_have_typed_errors(corpus):
    conn, config_path = corpus
    calls = [
        lambda: contract.kb_search_domains(
            "x", conn=conn, config_path=config_path
        ),
        lambda: contract.kb_search_domains(
            "x", domains=["economics", "economics"], conn=conn, config_path=config_path
        ),
        lambda: contract.kb_search_domains(
            "x", domains=["missing"], conn=conn, config_path=config_path
        ),
        lambda: contract.kb_search_domains(
            "x", domains=["economics"], limit=0, conn=conn, config_path=config_path
        ),
    ]
    expected = ["bad_request", "bad_request", "unknown_domain", "bad_request"]
    for call, code in zip(calls, expected):
        with pytest.raises(KBContractError) as excinfo:
            call()
        assert excinfo.value.code == code


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        (RuntimeError("offline"), "offline"),
        (TimeoutError("deadline exceeded"), "deadline exceeded"),
    ],
)
def test_partial_domain_failure_is_visible_and_other_results_survive(
    corpus, monkeypatch, failure, message
):
    conn, config_path = corpus
    registry = load_registry(config_path)
    resolved, scope = resolve_scope(
        registry, conn=conn, domains=["economics", "papers"], limit=10
    )
    monkeypatch.setattr(
        resolved[1][1],
        "search",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    result = search_across(resolved, scope, "inflation")
    assert result["results"]
    assert result["partial_failures"] == [
        {
            "domain": "papers",
            "backing": "corpus-view",
            "code": "domain_unavailable",
            "message": message,
        }
    ]
    assert next(
        item for item in result["scope"]["domains"] if item["domain"] == "papers"
    )["status"] == "unavailable"


def test_cross_domain_answer_groups_duplicate_claims_and_preserves_citations(corpus):
    conn, config_path = corpus
    payload = contract.kb_answer_domains(
        "How much did inflation rise in 2025?",
        domains=["economics", "papers"],
        limit=5,
        per_domain_limit=5,
        conn=conn,
        config_path=config_path,
    )
    _validate(RESPONSE_SCHEMA, payload)
    _validate(ANSWER_SCHEMA, payload)
    assert evaluate_answer(payload)["passed"] is True
    assert payload["data"]["answer_status"] == "answered"
    assert len(payload["data"]["statements"]) == 1
    statement = payload["data"]["statements"][0]
    assert statement["domains"] == ["economics", "papers"]
    assert {row["document_id"] for row in statement["supporting_evidence"]} == {
        "econ-doc",
        "paper-doc",
    }
    assert statement["corroboration"]["independent_source_count"] == 2
    plan = payload["data"]["evidence_plan"]
    assert plan["scope"]["selected_domains"] == ["economics", "papers"]
    assert {run["domain"] for run in plan["domain_runs"]} == {
        "economics",
        "papers",
    }


def test_cross_domain_answer_refuses_when_no_domain_has_evidence(corpus):
    conn, config_path = corpus
    payload = contract.kb_answer_domains(
        "What was the rainfall on Neptune?",
        domains=["economics", "papers"],
        conn=conn,
        config_path=config_path,
    )
    assert payload["data"]["answer_status"] == "refused"
    assert payload["data"]["evidence_plan"]["coverage_gaps"] == [
        {"domain": "economics", "reason": "no_relevant_evidence"},
        {"domain": "papers", "reason": "no_relevant_evidence"},
    ]
    assert evaluate_answer(payload)["passed"] is True


def test_cross_domain_answer_resolves_conflicting_domain_citation(corpus):
    conn, config_path = corpus
    conn.execute(
        "INSERT INTO argument_claims"
        " (claim_id, claim_text, document_id, source_type, confidence, prediction_mode)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            "claim-paper-conflict",
            "Inflation did not rise three percent in 2025.",
            "paper-doc",
            "paper",
            0.88,
            "deterministic:test",
        ],
    )
    conn.execute(
        "INSERT INTO claim_clusters VALUES (?, ?, 'test', 0)",
        ["claim-paper-conflict", "cluster-paper-conflict"],
    )
    _upsert_link(
        conn,
        ("economics", "claim-econ"),
        ("papers", "claim-paper-conflict"),
        "contradicts",
        0.97,
        "test-conflict",
        "deterministic:test",
        0.97,
        "test-v1",
        "conflict-test-run",
    )

    payload = contract.kb_answer_domains(
        "How much did inflation rise in 2025?",
        domains=["economics", "papers"],
        conn=conn,
        config_path=config_path,
    )
    contradicted = next(
        statement
        for statement in payload["data"]["statements"]
        if statement["verdict"] == "contradicted"
    )
    assert contradicted["contradicting_evidence"]
    assert contradicted["contradicting_evidence"][0]["domains"] == ["papers"]
    assert contradicted["contradicting_evidence"][0]["source"] == "Research journal"


def test_cross_domain_links_include_entity_and_claim_provenance(corpus):
    conn, config_path = corpus
    payload = contract.kb_cross_links(
        domains=["economics", "papers"],
        conn=conn,
        config_path=config_path,
    )
    _validate(RESPONSE_SCHEMA, payload)
    entity = next(row for row in payload["data"]["links"] if row["kind"] == "entity")
    assert entity["relation"] == "equivalent"
    assert {row["domain"] for row in entity["endpoints"]} == {
        "economics",
        "papers",
    }
    claim = next(row for row in payload["data"]["links"] if row["kind"] == "claim")
    assert claim["relation"] == "duplicate"
    assert claim["method"] == "manual-correction:reviewer"
    assert claim["prediction_mode"] == "human-reviewed"
    assert claim["confidence"] == 1.0
    assert claim["model_version"] == "manual-v1"
    assert claim["run_id"].startswith("manual-")
    assert isinstance(claim["as_of_ms"], int)
    assert {row["document_id"] for row in claim["evidence"]} == {
        "econ-doc",
        "paper-doc",
    }


def test_unresolved_ambiguous_entity_surface_is_not_silently_linked(corpus):
    conn, config_path = corpus
    conn.executemany(
        "INSERT INTO document_actors"
        " (document_id, source_type, actor_name, role, confidence)"
        " VALUES (?, ?, 'Jordan', 'subject', 1.0)",
        [("econ-doc", "news"), ("paper-doc", "paper")],
    )
    payload = contract.kb_cross_links(
        domains=["economics", "papers"],
        kind="entity",
        conn=conn,
        config_path=config_path,
    )
    assert all(
        not any(item["name"] == "Jordan" for item in link["evidence"])
        for link in payload["data"]["links"]
    )


def test_manual_claim_equivalence_can_be_unlinked_and_recomputed(corpus):
    conn, _config_path = corpus
    assert unlink_manual_claim_equivalence(conn, "claim-paper", "claim-econ") is True
    assert unlink_manual_claim_equivalence(conn, "claim-paper", "claim-econ") is False
    result = record_manual_claim_equivalence(
        conn,
        "economics",
        "claim-econ",
        "papers",
        "claim-paper",
        actor="second-reviewer",
    )
    assert result["written"] is True


def test_mcp_and_rest_use_the_same_cross_domain_contract(monkeypatch):
    sentinel = {
        "contract": "noesis-kb-v1",
        "domain": "cross-domain",
        "as_of_ms": 0,
        "data": {"cross_domain_contract": "noesis-cross-domain-v1"},
    }
    calls = []

    def fake_search(*args):
        calls.append(args)
        return sentinel

    monkeypatch.setattr(contract, "kb_search_domains", fake_search)
    from src.api.routes import kb_routes

    request = kb_routes.CrossDomainSearchRequest(
        query="inflation", domains=["economics", "papers"]
    )
    rest = kb_routes.cross_domain_search(request)

    server_path = REPO_ROOT / "tools/kb_mcp/server.py"
    spec = importlib.util.spec_from_file_location("cross_domain_kb_mcp", server_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    mcp = tools["kb_search_domains"].fn(
        query="inflation", domains=["economics", "papers"]
    )
    assert rest == mcp == sentinel
    assert len(calls) == 2


def test_contract_registry_exposes_cross_domain_schemas():
    from tools.contract_mcp.server import get_contract

    request = get_contract.fn("cross-domain-request")
    response = get_contract.fn("cross-domain-response")
    assert request["id"] == "noesis-cross-domain-request-v1"
    assert response["id"] == "noesis-cross-domain-response-v1"
