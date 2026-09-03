"""Claim-watch storage, matcher, authorization, cursor, and surface tests."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from fastapi import HTTPException
from jsonschema import Draft7Validator

from src.analytics.claim_check import record_check
from src.analytics.honesty import analytic_envelope
from src.database.local_warehouse_seed import ensure_schema
from src.ingestion.corrections import record_revision
from src.ingestion.document_store import DocumentStore
from src.kb import contract
from src.kb.clusters import ensure_cluster_schema
from src.kb.contract import KBContractError
from src.kb.membership import run_membership_pass
from src.kb.registry import load_registry
from src.kb.watches import (
    EVENT_TYPES,
    WatchError,
    audit_entries,
    commit_watch_watermark,
    create_watch,
    delete_watch,
    ensure_watch_schema,
    grant_watch_domain,
    list_watches,
    poll_watch,
    replay_watch,
    run_watch_matcher,
    set_watch_status,
    watch_metrics,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = (
    REPO_ROOT
    / "contracts/schemas/jsonschema/noesis-claim-watch-v1.json"
)
CONFIG = """
version: 1
domains:
  - name: economics
    backing: corpus-view
    embedding_model: fake-embed
    tags: [economics]
    keywords: [inflation]
    feeds:
      - url: https://example.invalid/source-a.xml
        name: Source A
        tags: [economics]
  - name: local
    backing: corpus-view
    embedding_model: fake-embed
    tags: [local, private]
    keywords: [acme, private, memo]
"""


def _document(
    document_id: str,
    source: str,
    content: str,
    ingested_at: int,
    *,
    private: bool = False,
) -> dict:
    return {
        "document_id": document_id,
        "source_type": "note" if private else "news",
        "source_id": source,
        "language": "en",
        "ingested_at": ingested_at,
        "url": f"https://{'private' if private else 'example'}.invalid/{document_id}",
        "title": content,
        "content": content,
        "metadata": {"tags": ["local", "private"] if private else ["economics"]},
    }


def _insert_claim(
    conn,
    claim_id: str,
    document_id: str,
    text: str,
    *,
    cluster_id: str | None = "cluster-inflation",
) -> None:
    conn.execute(
        "INSERT INTO argument_claims"
        " (claim_id, claim_text, document_id, source_type, confidence, prediction_mode)"
        " VALUES (?, ?, ?, 'news', 0.9, 'pretrained:test')",
        [claim_id, text, document_id],
    )
    if cluster_id is not None:
        conn.execute(
            "INSERT INTO claim_clusters VALUES (?, ?, 'test', 0)",
            [claim_id, cluster_id],
        )


@pytest.fixture()
def watch_corpus(tmp_path):
    conn = duckdb.connect()
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    ensure_schema(conn)
    initial = "Federal Reserve inflation policy was unchanged."
    DocumentStore(conn).upsert([_document("doc-a", "Source A", initial, 1_000)])
    run_membership_pass(conn, load_registry(config_path))
    ensure_cluster_schema(conn)
    _insert_claim(conn, "claim-a", "doc-a", initial)
    registry = load_registry(config_path)
    backing = registry.resolve("economics", conn=conn)
    return conn, config_path, registry, backing


def _add_support(conn, config_path: Path) -> None:
    text = "A second report said Federal Reserve inflation policy was unchanged."
    DocumentStore(conn).upsert([_document("doc-b", "Source B", text, 2_000)])
    run_membership_pass(conn, load_registry(config_path))
    _insert_claim(conn, "claim-b", "doc-b", text)


def _add_contradiction(conn, config_path: Path) -> None:
    text = "Federal Reserve inflation policy changed substantially."
    DocumentStore(conn).upsert([_document("doc-c", "Source C", text, 3_000)])
    run_membership_pass(conn, load_registry(config_path))
    _insert_claim(conn, "claim-c", "doc-c", text, cluster_id=None)
    conn.execute(
        """
        INSERT INTO claim_links
            (domain_a, claim_a, domain_b, claim_b, relation, score, method,
             prediction_mode, confidence, model_version, run_id, created_at)
        VALUES ('economics', 'claim-a', 'economics', 'claim-c',
                'contradicts', 0.9, 'test', 'zero-shot:test', 0.9,
                'test', 'test', 3000)
        """
    )


def _scan(conn, registry, watermark: int, observed_at_ms: int):
    commit_watch_watermark(
        conn, watermark, {"membership": f"m{watermark}"}, committed_at_ms=observed_at_ms
    )
    return run_watch_matcher(
        conn, registry, watermark, observed_at_ms=observed_at_ms
    )


def test_migration_is_idempotent_and_survives_restart(tmp_path):
    database = tmp_path / "watches.duckdb"
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    conn = duckdb.connect(str(database))
    ensure_schema(conn)
    ensure_watch_schema(conn)
    ensure_watch_schema(conn)
    assert conn.execute(
        "SELECT version FROM noesis_schema_migrations"
        " WHERE component='claim-watches'"
    ).fetchall() == [(1,)]
    commit_watch_watermark(conn, 1, {"clusters": "run-1"})
    DocumentStore(conn).upsert(
        [_document("restart-doc", "Source A", "Inflation restart fixture.", 1)]
    )
    run_membership_pass(conn, load_registry(config_path))
    persisted = create_watch(
        load_registry(config_path).resolve("economics", conn=conn),
        "restart-principal",
        {"type": "topic", "value": "inflation"},
        now_ms=1,
    )
    conn.close()

    reopened = duckdb.connect(str(database))
    assert reopened.execute(
        "SELECT consolidation_json FROM claim_watch_watermarks WHERE watermark=1"
    ).fetchone() == ('{"clusters":"run-1"}',)
    assert list_watches(reopened, "restart-principal")[0]["watch_id"] == (
        persisted["watch_id"]
    )
    reopened.close()
    replay = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/replay_claim_watches.py"),
            "--db-path",
            str(database),
            "--principal-id",
            "restart-principal",
            "--watch-id",
            persisted["watch_id"],
            "--from-watermark",
            "1",
            "--to-watermark",
            "1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["matches"] is True


@pytest.mark.parametrize("selector_type", ["query", "claim", "entity", "topic"])
def test_all_selector_types_match_deterministically(watch_corpus, selector_type):
    conn, config_path, registry, backing = watch_corpus
    values = {
        "query": "inflation policy",
        "claim": "claim-a",
        "entity": "Federal Reserve",
        "topic": "inflation",
    }
    watch = create_watch(
        backing,
        f"principal-{selector_type}",
        {"type": selector_type, "value": values[selector_type]},
        ["support_gained"],
        now_ms=1,
    )
    _scan(conn, registry, 1, 1_000)
    _add_support(conn, config_path)
    summary = _scan(conn, registry, 2, 2_000)
    events = poll_watch(conn, f"principal-{selector_type}", watch["watch_id"])[
        "events"
    ]
    assert summary["failed_watches"] == 0
    assert [event["event_type"] for event in events] == ["support_gained"]
    assert events[0]["evidence"][0]["document_id"] == "doc-b"


def test_seeded_change_sequence_is_cited_idempotent_and_replayable(watch_corpus):
    conn, config_path, registry, backing = watch_corpus
    watch = create_watch(
        backing,
        "alice",
        {"type": "claim", "value": "claim-a"},
        [
            "support_gained",
            "contradiction_added",
            "integrity_changed",
            "quantitative_verdict_changed",
        ],
        now_ms=1,
    )
    _scan(conn, registry, 1, 1_000)

    _add_support(conn, config_path)
    assert _scan(conn, registry, 2, 2_000)["emitted_events"] == 1
    assert _scan(conn, registry, 2, 2_000)["emitted_events"] == 0

    _add_contradiction(conn, config_path)
    assert _scan(conn, registry, 3, 3_000)["emitted_events"] == 1

    original = "A second report said Federal Reserve inflation policy was unchanged."
    changed = "A second report said Federal Reserve inflation policy was reversed."
    record_revision(conn, "doc-b", original, fetched_at=3_100)
    record_revision(conn, "doc-b", changed, fetched_at=3_200)
    assert _scan(conn, registry, 4, 4_000)["emitted_events"] == 1

    check = analytic_envelope(
        n=1,
        method="fixture quantitative check",
        assumptions=["fixed fixture"],
        verdict="supported",
    )
    record_check(conn, check, claim_id="claim-b", now_ms=4_100)
    assert _scan(conn, registry, 5, 5_000)["emitted_events"] == 1

    page = poll_watch(conn, "alice", watch["watch_id"], limit=20)
    assert [event["event_type"] for event in page["events"]] == [
        "support_gained",
        "contradiction_added",
        "integrity_changed",
        "quantitative_verdict_changed",
    ]
    assert all(event["evidence"] for event in page["events"])
    assert all(
        locator["cited"]
        for event in page["events"]
        for locator in event["evidence"]
    )
    assert all(event["reason_code"] for event in page["events"])
    assert len({event["event_id"] for event in page["events"]}) == 4
    enveloped = contract.watch_poll(watch["watch_id"], "alice", conn=conn)
    schema = json.loads(SCHEMA_PATH.read_text())
    assert not list(Draft7Validator(schema).iter_errors(enveloped))
    replay = replay_watch(
        conn,
        "alice",
        watch["watch_id"],
        from_watermark=1,
        to_watermark=5,
    )
    assert replay["matches"] is True
    assert replay["reconstructed"] == replay["stored"]
    replay_envelope = contract.watch_replay(
        watch["watch_id"], "alice", 1, 5, conn=conn
    )
    assert not list(Draft7Validator(schema).iter_errors(replay_envelope))


def test_cursor_paging_filtering_and_no_event_progress(watch_corpus):
    conn, config_path, registry, backing = watch_corpus
    watch = create_watch(
        backing,
        "alice",
        {"type": "claim", "value": "claim-a"},
        ["support_gained", "independence_changed"],
    )
    _scan(conn, registry, 1, 1_000)
    empty = poll_watch(conn, "alice", watch["watch_id"])
    assert empty["events"] == []
    _scan(conn, registry, 2, 1_100)
    still_empty = poll_watch(
        conn, "alice", watch["watch_id"], cursor=empty["cursor"]
    )
    assert still_empty["events"] == []

    _add_support(conn, config_path)
    _scan(conn, registry, 3, 2_000)
    first = poll_watch(conn, "alice", watch["watch_id"], limit=1)
    second = poll_watch(
        conn, "alice", watch["watch_id"], cursor=first["cursor"], limit=1
    )
    assert first["has_more"] is True
    assert second["has_more"] is False
    sequences = [first["events"][0]["sequence"], second["events"][0]["sequence"]]
    assert sequences == sorted(set(sequences))
    filtered = poll_watch(
        conn,
        "alice",
        watch["watch_id"],
        event_types=["independence_changed"],
    )
    assert [event["event_type"] for event in filtered["events"]] == [
        "independence_changed"
    ]
    with pytest.raises(WatchError, match="cursor") as excinfo:
        poll_watch(conn, "alice", watch["watch_id"], cursor="cw1.invalid.bad")
    assert excinfo.value.code == "cursor_stale"


def test_late_arrival_after_no_change_has_no_gap(watch_corpus):
    conn, config_path, registry, backing = watch_corpus
    watch = create_watch(
        backing,
        "alice",
        {"type": "claim", "value": "claim-a"},
        ["support_gained"],
    )
    _scan(conn, registry, 1, 1_000)
    _scan(conn, registry, 2, 2_000)
    _add_support(conn, config_path)
    _scan(conn, registry, 3, 3_000)
    events = poll_watch(conn, "alice", watch["watch_id"])["events"]
    assert [(event["watermark"], event["event_type"]) for event in events] == [
        (3, "support_gained")
    ]


def test_lost_support_is_detected_from_the_prior_snapshot(watch_corpus):
    conn, config_path, registry, backing = watch_corpus
    watch = create_watch(
        backing,
        "alice",
        {"type": "claim", "value": "claim-a"},
        ["support_lost"],
    )
    _scan(conn, registry, 1, 1_000)
    _add_support(conn, config_path)
    _scan(conn, registry, 2, 2_000)
    conn.execute(
        "DELETE FROM document_domains WHERE document_id='doc-b' AND domain='economics'"
    )
    _scan(conn, registry, 3, 3_000)
    events = poll_watch(conn, "alice", watch["watch_id"])["events"]
    assert [(event["event_type"], event["evidence"][0]["document_id"]) for event in events] == [
        ("support_lost", "doc-b")
    ]


def test_uncommitted_watermark_is_rejected_without_progress(watch_corpus):
    conn, _config_path, registry, backing = watch_corpus
    watch = create_watch(
        backing,
        "alice",
        {"type": "claim", "value": "claim-a"},
    )
    with pytest.raises(WatchError) as excinfo:
        run_watch_matcher(conn, registry, 9, observed_at_ms=9_000)
    assert excinfo.value.code == "watermark_uncommitted"
    assert list_watches(conn, "alice")[0]["last_watermark"] is None
    assert poll_watch(conn, "alice", watch["watch_id"])["events"] == []


def test_coverage_and_configured_source_staleness_events(watch_corpus):
    conn, _config_path, registry, backing = watch_corpus
    watch = create_watch(
        backing,
        "alice",
        {"type": "topic", "value": "inflation"},
        ["coverage_stale", "source_delivery_failed"],
        stale_after_ms=100,
    )
    _scan(conn, registry, 1, 1_000)
    _scan(conn, registry, 2, 1_101)
    events = poll_watch(conn, "alice", watch["watch_id"])["events"]
    assert {event["event_type"] for event in events} == {
        "coverage_stale",
        "source_delivery_failed",
    }
    assert all(event["evidence"][0]["document_id"] == "doc-a" for event in events)


def test_lifecycle_is_idempotent_and_delete_requires_confirmation(watch_corpus):
    conn, config_path, registry, backing = watch_corpus
    first = create_watch(
        backing, "alice", {"type": "claim", "value": "claim-a"}, now_ms=1
    )
    second = create_watch(
        backing, "alice", {"type": "claim", "value": "claim-a"}, now_ms=2
    )
    assert first["watch_id"] == second["watch_id"]
    assert len(list_watches(conn, "alice")) == 1
    assert set_watch_status(conn, "alice", first["watch_id"], "paused")[
        "status"
    ] == "paused"
    assert set_watch_status(conn, "alice", first["watch_id"], "paused")[
        "status"
    ] == "paused"
    _add_support(conn, config_path)
    assert _scan(conn, registry, 1, 2_000)["processed_watches"] == 0
    assert set_watch_status(conn, "alice", first["watch_id"], "active")[
        "status"
    ] == "active"
    assert _scan(conn, registry, 2, 2_000)["processed_watches"] == 1
    assert poll_watch(conn, "alice", first["watch_id"])["events"] == []
    with pytest.raises(WatchError) as excinfo:
        delete_watch(conn, "alice", first["watch_id"], confirm=False)
    assert excinfo.value.code == "confirmation_required"
    deleted = delete_watch(conn, "alice", first["watch_id"], confirm=True)
    assert deleted["status"] == "deleted"
    assert deleted["events_retained"] is True
    assert delete_watch(conn, "alice", first["watch_id"], confirm=True) == deleted
    assert list_watches(conn, "alice") == []


def test_private_domain_and_principal_isolation(watch_corpus):
    conn, config_path, registry, _backing = watch_corpus
    private_text = "The private Acme memo approved Atlas."
    DocumentStore(conn).upsert(
        [_document("private-doc", "Private Archive", private_text, 1_500, private=True)]
    )
    run_membership_pass(conn, load_registry(config_path))
    _insert_claim(
        conn,
        "private-claim",
        "private-doc",
        private_text,
        cluster_id="cluster-private",
    )
    private_backing = registry.resolve("local", conn=conn)
    with pytest.raises(WatchError) as excinfo:
        create_watch(
            private_backing,
            "alice",
            {"type": "query", "value": "Acme memo"},
        )
    assert excinfo.value.code == "unauthorized"
    grant_watch_domain(conn, "alice", "local", granted_at_ms=1)
    private_watch = create_watch(
        private_backing,
        "alice",
        {"type": "query", "value": "Acme memo"},
    )
    with pytest.raises(WatchError) as excinfo:
        poll_watch(conn, "bob", private_watch["watch_id"])
    assert excinfo.value.code == "unauthorized"

    public_watch = create_watch(
        registry.resolve("economics", conn=conn),
        "alice",
        {"type": "query", "value": "Acme memo"},
    )
    _scan(conn, registry, 1, 2_000)
    assert poll_watch(conn, "alice", public_watch["watch_id"])["events"] == []
    public_state = conn.execute(
        "SELECT last_state_json FROM claim_watches WHERE watch_id = ?",
        [public_watch["watch_id"]],
    ).fetchone()[0]
    assert "private-doc" not in public_state
    private_state = conn.execute(
        "SELECT last_state_json FROM claim_watches WHERE watch_id = ?",
        [private_watch["watch_id"]],
    ).fetchone()[0]
    assert json.loads(private_state)["claims"][0]["support"][0]["visibility"] == (
        "private"
    )


def test_failures_retry_dead_letter_and_metrics_are_text_free(watch_corpus):
    conn, config_path, registry, backing = watch_corpus
    watch = create_watch(
        backing,
        "alice-secret-principal",
        {"type": "query", "value": "inflation secret"},
    )
    commit_watch_watermark(conn, 1, {"membership": "ready"})
    assert run_watch_matcher(
        conn, registry, 1, observed_at_ms=1_000
    )["processed_watches"] == 1
    _add_support(conn, config_path)
    commit_watch_watermark(conn, 2, {"membership": "ready-2"})

    class BrokenRegistry:
        def resolve(self, domain, conn=None):
            raise RuntimeError("source text must not leak")

    for _ in range(3):
        result = run_watch_matcher(
            conn, BrokenRegistry(), 2, observed_at_ms=2_000
        )
        assert result["failed_watches"] == 1
    metrics = watch_metrics(conn)
    assert metrics["unresolved_failures"] == 1
    assert metrics["dead_letter_count"] == 1
    assert "secret" not in json.dumps(metrics)
    metrics_envelope = contract.watch_observability(conn=conn)
    schema = json.loads(SCHEMA_PATH.read_text())
    assert not list(Draft7Validator(schema).iter_errors(metrics_envelope))

    recovered = run_watch_matcher(conn, registry, 2, observed_at_ms=2_000)
    assert recovered["processed_watches"] == 1
    assert recovered["emitted_events"] == 2
    assert run_watch_matcher(
        conn, registry, 2, observed_at_ms=2_000
    )["emitted_events"] == 0
    events = poll_watch(
        conn, "alice-secret-principal", watch["watch_id"]
    )["events"]
    assert len(events) == len({event["event_id"] for event in events}) == 2
    assert watch_metrics(conn)["unresolved_failures"] == 0
    assert watch_metrics(conn)["dead_letter_count"] == 1
    audits = audit_entries(conn, "alice-secret-principal")
    assert "inflation secret" not in json.dumps(audits)


def test_contract_errors_and_surface_parity(monkeypatch, watch_corpus):
    conn, _config_path, _registry, backing = watch_corpus
    sentinel = {
        "contract": "noesis-kb-v1",
        "domain": "economics",
        "as_of_ms": 0,
        "data": {"watch_contract": "noesis-claim-watch-v1"},
    }
    calls = []

    def fake_create(domain, principal_id, selector, event_types=None, stale_after_ms=0):
        calls.append((domain, principal_id, selector, event_types, stale_after_ms))
        return sentinel

    monkeypatch.setattr(contract, "watch_create", fake_create)
    from src.api.routes import kb_routes

    request = kb_routes.WatchCreateRequest(
        domain="economics",
        selector={"type": "topic", "value": "inflation"},
        event_types=["support_gained"],
        stale_after_ms=100,
    )
    rest = kb_routes.create_watch(request, {"sub": "alice"})

    server_path = REPO_ROOT / "tools/kb_mcp/server.py"
    spec = importlib.util.spec_from_file_location("claim_watch_mcp", server_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    mcp = tools["watch_create"].fn(
        domain="economics",
        principal_id="alice",
        selector={"type": "topic", "value": "inflation"},
        event_types=["support_gained"],
        stale_after_ms=100,
    )
    assert rest == mcp == sentinel
    assert len(calls) == 2

    owned = create_watch(
        backing,
        "alice",
        {"type": "topic", "value": "inflation"},
    )

    def authorized_poll(watch_id, principal_id, cursor=None, limit=50, event_types=None):
        try:
            payload = poll_watch(
                conn,
                principal_id,
                watch_id,
                cursor=cursor,
                limit=limit,
                event_types=event_types,
            )
        except WatchError as exc:
            raise KBContractError(exc.code, str(exc)) from exc
        return {
            "contract": "noesis-kb-v1",
            "domain": payload["domain"],
            "as_of_ms": 0,
            "data": payload,
        }

    monkeypatch.setattr(contract, "watch_poll", authorized_poll)
    with pytest.raises(HTTPException) as excinfo:
        kb_routes.poll_watch(owned["watch_id"], current_user={"sub": "mallory"})
    assert excinfo.value.status_code == 403
    assert tools["watch_poll"].fn(owned["watch_id"], "mallory") == {
        "error": {
            "code": "unauthorized",
            "message": "the watch belongs to another principal",
        }
    }


def test_validation_errors_are_stable(watch_corpus):
    _conn, _config_path, _registry, backing = watch_corpus
    with pytest.raises(WatchError) as excinfo:
        create_watch(backing, "alice", {"type": "unknown", "value": "x"})
    assert excinfo.value.code == "bad_selector"
    with pytest.raises(WatchError) as excinfo:
        create_watch(backing, "", {"type": "query", "value": "x"})
    assert excinfo.value.code == "unauthorized"
    with pytest.raises(WatchError) as excinfo:
        create_watch(
            backing,
            "alice",
            {"type": "query", "value": "x"},
            ["not-an-event"],
        )
    assert excinfo.value.code == "bad_request"
    assert set(EVENT_TYPES) >= {
        "support_gained",
        "contradiction_added",
        "integrity_changed",
        "quantitative_verdict_changed",
    }


@pytest.mark.parametrize("name", ["valid-watch.json", "valid-poll.json"])
def test_committed_contract_examples_validate(name):
    schema = json.loads(SCHEMA_PATH.read_text())
    payload = json.loads(
        (
            REPO_ROOT / "contracts/examples/noesis-claim-watch-v1" / name
        ).read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(payload))


def test_invalid_event_fixture_is_rejected():
    schema = json.loads(SCHEMA_PATH.read_text())
    payload = json.loads(
        (
            REPO_ROOT
            / "contracts/examples/noesis-claim-watch-v1"
            / "invalid-event-without-evidence.json"
        ).read_text()
    )
    assert list(Draft7Validator(schema).iter_errors(payload))


@pytest.mark.parametrize("alias", ["claim-watch", "watch-event"])
def test_contract_registry_resolves_watch_aliases(alias):
    from tools.contract_mcp.server import validate

    result = validate.fn(alias, "valid-watch")
    assert result["valid"] is True
    assert result["verdicts"]["jsonschema"]["contract"] == (
        "noesis-claim-watch-v1"
    )


def test_daily_brief_consumes_events_as_a_projection(watch_corpus):
    conn, config_path, registry, backing = watch_corpus
    watch = create_watch(
        backing,
        "alice",
        {"type": "claim", "value": "claim-a"},
        ["support_gained"],
    )
    _scan(conn, registry, 1, 1_000)
    _add_support(conn, config_path)
    _scan(conn, registry, 2, 2_000)
    events = poll_watch(conn, "alice", watch["watch_id"])["events"]

    from src.kb.brief import watch_event_digest

    digest = watch_event_digest(events, budget=1)
    assert digest["events"] == events
    assert "support_gained" in digest["markdown"]
    assert "doc-b" in digest["markdown"]
    assert digest["meta"]["source_of_truth"] is False
