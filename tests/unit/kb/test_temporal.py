"""Temporal normalization, bitemporal storage, queries, and transitions."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.ingestion.corrections import record_revision
from src.ingestion.document_store import DocumentStore
from src.kb import contract
from src.kb.claim_links import _link_pair, ensure_claim_link_schema
from src.kb.contract import KBContractError
from src.kb.membership import run_membership_pass
from src.kb.nli import CONTRADICTION
from src.kb.promotion import promote_to_namespace
from src.kb.registry import load_registry
from src.kb.temporal import (
    TemporalError,
    classify_temporal_relation,
    normalize_document_times,
    query_temporal,
    record_temporal_assertion,
    store_document_times,
)
from src.kb.watches import grant_watch_domain

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = REPO_ROOT / "contracts/schemas/jsonschema"

CONFIG = """
version: 1
domains:
  - name: research
    backing: corpus-view
    embedding_model: test-embed
    tags: [research]
    keywords: [study]
  - name: local
    backing: corpus-view
    embedding_model: test-embed
    tags: [local, private]
    keywords: [private]
"""


class EmptyBacking:
    backing_type = "corpus-view"

    def __init__(self, conn, name="research", tags=()):
        self.conn = conn
        self.definition = SimpleNamespace(name=name, tags=list(tags))

    def documents(self, limit=50):
        return []

    def claims(self, limit=50):
        return []

    def entities(self):
        return []


@pytest.fixture()
def corpus(tmp_path):
    conn = duckdb.connect()
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    ensure_claim_link_schema(conn)
    DocumentStore(conn).upsert(
        [
            {
                "document_id": "paper-1",
                "source_type": "paper",
                "language": "en",
                "source_id": "journal",
                "title": "Research study",
                "content": "The study reports a result.",
                "created_at": 1_000,
                "ingested_at": 2_000,
                "metadata": {
                    "tags": ["research"],
                    "effective_from": 1_100,
                    "effective_to": 9_000,
                },
            },
            {
                "document_id": "private-1",
                "source_type": "note",
                "language": "en",
                "source_id": "local",
                "title": "Private note",
                "content": "Private research notes.",
                "created_at": 1_500,
                "ingested_at": 2_500,
                "metadata": {"tags": ["local", "private"]},
            },
        ]
    )
    run_membership_pass(conn, load_registry(config_path))
    conn.executemany(
        "INSERT INTO argument_claims"
        " (claim_id, claim_text, document_id, source_type, confidence, prediction_mode)"
        " VALUES (?, ?, ?, ?, 0.9, 'deterministic:test')",
        [
            ("claim-paper", "The study reports a result.", "paper-1", "paper"),
            ("claim-private", "Private research notes.", "private-1", "note"),
        ],
    )
    return conn, config_path


@pytest.mark.parametrize(
    "source_type,payload,field,precision",
    [
        ("paper", {"created_at": "2026-01-03T10:20:30Z"}, "publication_at_ms", "second"),
        ("legislation", {"metadata": {"effective_date": "2027-04-01"}}, "effective_from_ms", "day"),
        ("filing", {"metadata": {"revision_at": "2026-02-01T12:00+02:00"}}, "revision_at_ms", "minute"),
        ("dataset", {"metadata": {"event_at": "~2025-06"}}, "event_from_ms", "month"),
        (
            "news",
            {"created_at": "2026-03-01T09:00:00", "metadata": {"source_timezone": "Europe/Berlin"}},
            "publication_at_ms",
            "second",
        ),
        ("upload", {"metadata": {"corrected_at": 2_000}}, "correction_at_ms", "millisecond"),
    ],
)
def test_connector_time_fixtures_retain_precision_and_provenance(
    source_type, payload, field, precision
):
    normalized = normalize_document_times(
        {"document_id": source_type, "source_type": source_type, "ingested_at": 3_000, **payload}
    )
    assert normalized["status"] == "normalized"
    assert isinstance(normalized[field], int)
    assert normalized["provenance"][field]["precision"] == precision
    assert normalized["provenance"][field]["original"]
    assert normalized["provenance"][field]["parser_version"] == "1.0.0"
    if source_type == "news":
        assert "Europe/Berlin" in normalized["provenance"][field]["timezone_assumption"]
    if source_type == "dataset":
        assert normalized["provenance"][field]["approximate"] is True


def test_malformed_and_impossible_source_times_are_quarantined():
    conn = duckdb.connect()
    malformed = store_document_times(
        conn,
        {
            "document_id": "bad-time",
            "ingested_at": 10,
            "metadata": {"event_at": "32 Smarch 2026"},
        },
    )
    impossible = store_document_times(
        conn,
        {
            "document_id": "bad-interval",
            "ingested_at": 10,
            "metadata": {"effective_from": 20, "effective_to": 10},
        },
    )
    assert malformed["status"] == impossible["status"] == "quarantined"
    assert malformed["event_from_ms"] is None
    assert conn.execute("SELECT COUNT(*) FROM kb_temporal_quarantine").fetchone()[0] == 2
    with pytest.raises(TemporalError) as excinfo:
        record_temporal_assertion(
            conn,
            domain="research",
            backing="corpus-view",
            assertion_kind="claim",
            assertion_id="bad",
            payload={},
            observed_at_ms=30,
            valid_from_ms=20,
            valid_to_ms=10,
        )
    assert excinfo.value.code == "impossible_interval"


def test_malformed_revision_does_not_overwrite_normalized_document_time():
    conn = duckdb.connect()
    store_document_times(
        conn,
        {
            "document_id": "stable-time",
            "created_at": "2026-01-01T00:00:00Z",
            "ingested_at": 10,
        },
    )
    result = store_document_times(
        conn,
        {
            "document_id": "stable-time",
            "created_at": "not-a-date",
            "ingested_at": 20,
        },
    )
    stored = conn.execute(
        "SELECT publication_at_ms, ingested_at_ms, status "
        "FROM kb_document_times WHERE document_id = 'stable-time'"
    ).fetchone()
    assert result["status"] == "quarantined"
    assert stored == (1_767_225_600_000, 10, "normalized")


def test_assertion_contract_fixtures_cover_all_persisted_object_types():
    schema = json.loads((SCHEMA_DIR / "noesis-temporal-assertion-v1.json").read_text())
    validator = Draft7Validator(schema)
    fixture_dir = REPO_ROOT / "contracts/examples/noesis-temporal-v1"
    kinds = set()
    for path in sorted(fixture_dir.glob("valid-*.json")):
        payload = json.loads(path.read_text())
        assert not list(validator.iter_errors(payload)), path.name
        kinds.add(payload["assertion_kind"])
    assert kinds == {"document", "claim", "relation", "observation"}


def _record(conn, assertion_id, observed, payload, **kwargs):
    return record_temporal_assertion(
        conn,
        domain="research",
        backing="corpus-view",
        assertion_kind="claim",
        assertion_id=assertion_id,
        payload=payload,
        observed_at_ms=observed,
        **kwargs,
    )


def test_snapshot_history_precedence_boundaries_conflicts_and_retractions():
    conn = duckdb.connect()
    backing = EmptyBacking(conn)
    _record(conn, "rate", 200, {"value": 1}, valid_from_ms=100, valid_to_ms=1_000, valid_time_precision="millisecond")
    _record(conn, "rate", 500, {"value": 2}, valid_from_ms=100, valid_to_ms=1_000, valid_time_precision="millisecond")
    _record(conn, "unknown", 250, {"value": "unknown time"})
    _record(conn, "withdrawn", 200, {"value": 3}, retracted_at_ms=600)
    _record(conn, "conflict", 400, {"value": "A"})
    _record(conn, "conflict", 400, {"value": "B"})

    as_of = query_temporal(backing, assertion_id="rate", as_of=300)
    assert [item["payload"]["value"] for item in as_of["items"]] == [1]
    assert as_of["temporal_basis"]["effective"]["valid_at_ms"] == 300
    assert as_of["temporal_basis"]["effective"]["observed_before_ms"] == 300
    assert as_of["temporal_basis"]["effective"]["recorded_before_ms"] > 0

    overridden = query_temporal(
        backing, assertion_id="rate", as_of=300, valid_at=700, observed_before=500
    )
    assert overridden["items"][0]["payload"]["value"] == 2
    assert query_temporal(backing, assertion_id="rate", valid_at=1_000)["items"] == []
    assert len(query_temporal(backing, assertion_id="rate", valid_at=700, observed_before=500, history=True)["items"]) == 2
    assert query_temporal(backing, assertion_id="withdrawn", observed_before=700)["items"] == []
    assert len(query_temporal(backing, assertion_id="withdrawn", observed_before=700, history=True)["items"]) == 1
    assert len(query_temporal(backing, assertion_id="conflict", observed_before=400)["items"]) == 2

    unknown = query_temporal(backing, valid_at=500)
    assert unknown["temporal_basis"]["unknown_valid_time_excluded"] >= 1
    assert "half-open" in unknown["temporal_basis"]["valid_interval_boundary"]
    assert "inclusive" in unknown["temporal_basis"]["observation_boundary"]


def test_history_cursor_is_snapshot_stable_and_query_bound():
    conn = duckdb.connect()
    backing = EmptyBacking(conn)
    for index in range(5):
        _record(conn, f"c-{index}", index + 1, {"index": index})
    first = query_temporal(backing, history=True, limit=2)
    _record(conn, "retroactive", 1, {"index": "late"})
    conn.execute(
        "UPDATE kb_temporal_assertions SET recorded_at_ms = ? "
        "WHERE assertion_id = 'retroactive'",
        [first["temporal_basis"]["effective"]["recorded_before_ms"] + 1],
    )
    second = query_temporal(
        backing, history=True, limit=2, cursor=first["page"]["next_cursor"]
    )
    assert first["page"]["returned"] == second["page"]["returned"] == 2
    assert {item["assertion_id"] for item in first["items"]}.isdisjoint(
        item["assertion_id"] for item in second["items"]
    )
    assert all(item["assertion_id"] != "retroactive" for item in second["items"])
    with pytest.raises(TemporalError) as excinfo:
        query_temporal(
            backing,
            assertion_kind="document",
            history=True,
            limit=2,
            cursor=first["page"]["next_cursor"],
        )
    assert excinfo.value.code == "cursor_stale"


def test_contract_migrates_corpus_and_namespace_backings_without_id_changes(corpus):
    conn, config_path = corpus
    corpus_result = contract.kb_temporal(
        "research", observed_before=2_000, conn=conn, config_path=config_path
    )
    response_schema = json.loads((SCHEMA_DIR / "noesis-temporal-response-v1.json").read_text())
    assert not list(Draft7Validator(response_schema).iter_errors(corpus_result))
    before = {(item["assertion_kind"], item["assertion_id"]) for item in corpus_result["data"]["items"]}
    assert {("document", "paper-1"), ("claim", "claim-paper")} <= before

    promote_to_namespace(conn, "research", config_path)
    namespace_result = contract.kb_temporal(
        "research", observed_before=2_000, conn=conn, config_path=config_path
    )
    after = {(item["assertion_kind"], item["assertion_id"]) for item in namespace_result["data"]["items"]}
    assert before == after
    assert corpus_result["data"]["temporal_basis"]["backing"] == "corpus-view"
    assert namespace_result["data"]["temporal_basis"]["backing"] == "namespace"


def test_private_history_requires_the_principal_domain_grant(corpus):
    conn, config_path = corpus
    with pytest.raises(KBContractError) as excinfo:
        contract.kb_temporal("local", conn=conn, config_path=config_path)
    assert excinfo.value.code == "unauthorized"
    grant_watch_domain(conn, "alice", "local", granted_at_ms=1)
    result = contract.kb_temporal(
        "local",
        principal_id="alice",
        include_private=True,
        conn=conn,
        config_path=config_path,
    )
    assert result["data"]["items"]
    assert {item["visibility"] for item in result["data"]["items"]} == {"private"}
    assert all(item.get("source_document_id") != "paper-1" for item in result["data"]["items"])


@pytest.mark.parametrize(
    "case,base,same_source,a,b,change,expected",
    [
        ("revised statistic", "contradicts", True, 1, 2, None, "supersedes"),
        ("amended law", "contradicts", True, 10, 20, "silent_substantive", "supersedes"),
        ("corrected article", "contradicts", True, 1, 2, "correction_notice", "corrects"),
        ("retracted paper", "contradicts", True, 1, 2, "retraction", "retracts"),
        ("contemporaneous conflict", "contradicts", True, 2, 2, None, "contradicts"),
        ("independent disagreement", "contradicts", False, 1, 2, None, "contradicts"),
    ],
)
def test_temporal_transition_types(case, base, same_source, a, b, change, expected):
    assert case
    assert classify_temporal_relation(
        base,
        same_source=same_source,
        observed_a_ms=a,
        observed_b_ms=b,
        newer_change_class=change,
    ) == expected


def test_claim_consolidation_does_not_report_same_source_revision_as_conflict():
    conn = duckdb.connect()
    ensure_claim_link_schema(conn)
    DocumentStore(conn).upsert(
        [
            {"document_id": "old", "source_type": "news", "language": "en", "source_id": "bureau", "content": "Rate was 4%.", "ingested_at": 100},
            {"document_id": "new", "source_type": "news", "language": "en", "source_id": "bureau", "content": "Rate was 3%.", "ingested_at": 200},
        ]
    )
    conn.executemany(
        "INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type) VALUES (?, ?, ?, 'news')",
        [("old-claim", "Rate was 4%.", "old"), ("new-claim", "Rate was 3%.", "new")],
    )

    class ContradictingNLI:
        name = "fixture-nli"
        prediction_mode = "deterministic:test"
        model_version = "fixture-v1"

        @staticmethod
        def classify(_premise, _hypothesis):
            return SimpleNamespace(label=CONTRADICTION, confidence=0.95)

    summary = {"links": {name: 0 for name in ("duplicate", "supports", "contradicts", "supersedes", "corrects", "retracts")}}
    _link_pair(
        conn,
        ContradictingNLI(),
        "fixture-run",
        summary,
        ("research", "old-claim", "Rate was 4%.", 100),
        ("research", "new-claim", "Rate was 3%.", 200),
        0.8,
        0.9,
        0.55,
        1_000,
    )
    relations = conn.execute("SELECT relation, claim_a, claim_b FROM claim_links").fetchall()
    assert relations == [("supersedes", "new-claim", "old-claim")]

    class RevisionBacking(EmptyBacking):
        def documents(self, limit=50):
            return [
                {
                    "document_id": "old",
                    "source_id": "bureau",
                    "ingested_at": 100,
                },
                {
                    "document_id": "new",
                    "source_id": "bureau",
                    "ingested_at": 200,
                },
            ]

        def claims(self, limit=50):
            return [
                {
                    "citations": [
                        {
                            "claim_id": "old-claim",
                            "document_id": "old",
                            "source": "bureau",
                        },
                        {
                            "claim_id": "new-claim",
                            "document_id": "new",
                            "source": "bureau",
                        },
                    ]
                }
            ]

    history = query_temporal(
        RevisionBacking(conn), assertion_kind="relation", history=True
    )
    transition = next(
        item for item in history["items"] if item["payload"]["relation"] == "supersedes"
    )
    assert {
        evidence["document_id"]
        for evidence in transition["payload"]["transition_evidence"]
    } == {"old", "new"}


@pytest.mark.parametrize(
    "replacement,expected",
    [
        ("Correction: the rate was 3%, not the previously reported figure.", "corrects"),
        ("This article has been retracted by the publisher after review.", "retracts"),
    ],
)
def test_declared_source_revision_materializes_typed_transition(replacement, expected):
    conn = duckdb.connect()
    ensure_claim_link_schema(conn)
    DocumentStore(conn).upsert(
        [
            {
                "document_id": "old",
                "source_type": "news",
                "language": "en",
                "source_id": "bureau",
                "content": "Rate was 4% according to the release.",
                "ingested_at": 100,
            },
            {
                "document_id": "new",
                "source_type": "news",
                "language": "en",
                "source_id": "bureau",
                "content": replacement,
                "ingested_at": 200,
            },
        ]
    )
    conn.executemany(
        "INSERT INTO argument_claims "
        "(claim_id, claim_text, document_id, source_type) VALUES (?, ?, ?, 'news')",
        [
            ("old-claim", "Rate was 4%.", "old"),
            ("new-claim", "Rate was 3%.", "new"),
        ],
    )
    record_revision(conn, "new", "Rate was 4% according to the release.", 100)
    record_revision(conn, "new", replacement, 200)

    class ContradictingNLI:
        name = "fixture-nli"
        prediction_mode = "deterministic:test"
        model_version = "fixture-v1"

        @staticmethod
        def classify(_premise, _hypothesis):
            return SimpleNamespace(label=CONTRADICTION, confidence=0.95)

    summary = {
        "links": {
            name: 0
            for name in (
                "duplicate",
                "supports",
                "contradicts",
                "supersedes",
                "corrects",
                "retracts",
            )
        }
    }
    _link_pair(
        conn,
        ContradictingNLI(),
        "fixture-run",
        summary,
        ("research", "old-claim", "Rate was 4%.", 100),
        ("research", "new-claim", "Rate was 3%.", 200),
        0.8,
        0.9,
        0.55,
        1_000,
    )
    assert conn.execute(
        "SELECT relation, claim_a, claim_b FROM claim_links"
    ).fetchall() == [(expected, "new-claim", "old-claim")]


def test_rest_and_mcp_temporal_surfaces_share_one_contract(monkeypatch):
    sentinel = {
        "contract": "noesis-kb-v1",
        "domain": "research",
        "as_of_ms": 1,
        "data": {"temporal_contract": "noesis-temporal-v1"},
    }
    calls = []

    def fake_temporal(*args):
        calls.append(args)
        return sentinel

    monkeypatch.setattr(contract, "kb_temporal", fake_temporal)
    from src.api.routes import kb_routes

    request = kb_routes.TemporalQueryRequest(domain="research", as_of=1)
    rest = kb_routes.temporal_query(request)
    spec = importlib.util.spec_from_file_location(
        "temporal_kb_mcp", REPO_ROOT / "tools/kb_mcp/server.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    mcp = tools["kb_temporal"].fn(domain="research", as_of="1")
    assert rest == mcp == sentinel
    assert len(calls) == 2


def test_indexed_temporal_pagination_performance():
    conn = duckdb.connect()
    backing = EmptyBacking(conn)
    for index in range(300):
        _record(conn, f"claim-{index}", index, {"index": index})
    started = time.perf_counter()
    result = query_temporal(backing, history=True, limit=100)
    elapsed = time.perf_counter() - started
    assert result["n"] == 100
    assert result["page"]["next_cursor"]
    assert elapsed < 2.0


def test_contract_registry_exposes_temporal_schemas():
    from tools.contract_mcp.server import get_contract

    assert get_contract.fn("temporal-assertion")["id"] == "noesis-temporal-assertion-v1"
    assert get_contract.fn("temporal-query")["id"] == "noesis-temporal-query-v1"
    assert get_contract.fn("temporal-response")["id"] == "noesis-temporal-response-v1"
