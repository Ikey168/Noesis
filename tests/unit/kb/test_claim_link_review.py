"""Jev review inside the claim linking pass (shadow and gate modes)."""

import json

import duckdb
import pytest

from src.kb.claim_link_review import JevClaimLinkReviewer, document_revision_resolver
from src.kb.claim_links import delete_run, run_claim_linking_pass
from tests.unit.kb.test_claim_links import (
    BASE_MS,
    CONTRA,
    DAY_MS,
    DUP_A,
    DUP_B,
    FakeNLI,
    FakeProvider,
    _seed_claims,
)


class FakeRuntime:
    """Decision runtime double: answers come from ``labels`` per question key."""

    def __init__(self, texts, labels, *, fail=None):
        self.texts = texts
        self.labels = labels
        self.fail = fail
        self.calls = []

    def capture_sources(self, namespace, principal_id, refs, scopes):
        return (
            [dict(ref) for ref in refs],
            [{"content": self.texts[ref["document_id"]]} for ref in refs],
        )

    def run(self, namespace, run_id, task, *, questions, state, source_refs, **kwargs):
        self.calls.append({"task": task, "run_id": run_id, "state": state})
        if self.fail:
            raise self.fail
        answers = {}
        for key, question in questions.items():
            value = self.labels[key]
            if question["type"] == "noul":
                answers[key] = {"kind": "noul", "status": "answered", "value": value}
            else:
                answers[key] = {
                    "kind": "choice", "status": "answered", "value": value,
                    "probabilities": {value: 1.0}, "vendor_confidence": 0.9,
                }
        return {
            "status": "completed", "rollout_mode": "manual",
            "receipt": {"answers": answers}, "source_binding": source_refs,
        }


TEXTS = {"c1": DUP_A, "c2": DUP_B, "c3": CONTRA}


def resolver(claim_id, claim_text):
    return {
        "source_ref": {"document_id": claim_id, "revision_id": "r1"},
        "locator": {"start": 0, "end": len(claim_text)},
    }


def reviewer(runtime, mode, resolve=resolver):
    return JevClaimLinkReviewer(
        runtime, "ns", principal_id="alice", scopes={"operator"},
        policy={"model": "jev-1.13.0"}, allow_remote=True,
        resolve_claim=resolve, mode=mode,
    )


CONFLICT = {"kind": "quantitative", "jointly_true": 0.05, "same_subject": 0.95}
RECONCILABLE = {"kind": "temporal", "jointly_true": 0.9, "same_subject": 0.9}


@pytest.fixture()
def conn():
    return duckdb.connect()


def _contradiction_pair(conn):
    _seed_claims(
        conn,
        [
            ("c1", DUP_A, "d1", BASE_MS, "economics"),
            ("c3", CONTRA, "d3", BASE_MS + DAY_MS, None),
        ],
    )


def _assessments(conn):
    return conn.execute(
        "SELECT claim_a, claim_b, relation, mode, status, verdict, action, jev_relation"
        " FROM claim_link_jev_assessments ORDER BY relation"
    ).fetchall()


def test_gate_withholds_a_contradiction_jev_finds_reconcilable(conn):
    _contradiction_pair(conn)
    runtime = FakeRuntime(TEXTS, RECONCILABLE)
    summary = run_claim_linking_pass(
        conn, provider=FakeProvider(), nli=FakeNLI(),
        jev_reviewer=reviewer(runtime, "gate"),
    )
    assert summary["links"]["contradicts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0] == 0
    # Both claims are new, so the pair is planned twice but asked once.
    assert len(runtime.calls) == 1
    assert runtime.calls[0]["task"] == "jev-contradiction_kind-v1"
    assert summary["jev"] == {
        "mode": "gate", "method": "jev:jev-1.13.0:gate", "reviewed": 1,
        "agrees": 0, "disagrees": 1, "withheld": 1, "abstained": 0,
        "unavailable": 0, "unresolved": 0, "requests": 1,
    }
    (row,) = _assessments(conn)
    assert row[2:] == ("contradicts", "gate", "answered", "disagrees", "withheld", "reconcilable")


def test_gate_writes_a_contradiction_jev_confirms(conn):
    _contradiction_pair(conn)
    summary = run_claim_linking_pass(
        conn, provider=FakeProvider(), nli=FakeNLI(),
        jev_reviewer=reviewer(FakeRuntime(TEXTS, CONFLICT), "gate"),
    )
    assert summary["links"]["contradicts"] == 1
    assert summary["jev"]["agrees"] == 1 and summary["jev"]["withheld"] == 0
    assert _assessments(conn)[0][5:7] == ("agrees", "written")


def test_shadow_writes_local_links_and_records_disagreement(conn):
    _contradiction_pair(conn)
    summary = run_claim_linking_pass(
        conn, provider=FakeProvider(), nli=FakeNLI(),
        jev_reviewer=reviewer(FakeRuntime(TEXTS, RECONCILABLE), "shadow"),
    )
    assert summary["links"]["contradicts"] == 1
    assert summary["jev"]["disagrees"] == 1 and summary["jev"]["withheld"] == 0
    assert _assessments(conn)[0][5:7] == ("disagrees", "written")


def test_duplicate_reviewed_in_both_directions_and_gated(conn):
    _seed_claims(
        conn,
        [
            ("c1", DUP_A, "d1", BASE_MS, "economics"),
            ("c2", DUP_B, "d2", BASE_MS + DAY_MS, "economics"),
        ],
    )
    runtime = FakeRuntime(TEXTS, {"a_to_b": "entailment", "b_to_a": "neutral"})
    summary = run_claim_linking_pass(
        conn, provider=FakeProvider(), nli=FakeNLI(),
        jev_reviewer=reviewer(runtime, "gate"),
    )
    # Jev finds one-way support, not a duplicate: the duplicate is withheld
    # and no other relation is invented in its place.
    assert runtime.calls[0]["task"] == "jev-claim_links-v1"
    assert summary["links"]["duplicate"] == 0
    assert sum(summary["links"].values()) == 0
    assert _assessments(conn)[0][7] == "a_supports_b"


def test_failures_and_missing_spans_keep_local_links(conn):
    _contradiction_pair(conn)
    from src.kb.decision_runtime import DecisionRuntimeError

    failing = FakeRuntime(
        TEXTS, CONFLICT, fail=DecisionRuntimeError("remote_disabled", "hosted off")
    )
    summary = run_claim_linking_pass(
        conn, provider=FakeProvider(), nli=FakeNLI(),
        jev_reviewer=reviewer(failing, "gate"),
    )
    assert summary["links"]["contradicts"] == 1
    assert summary["jev"]["unavailable"] == 1 and summary["jev"]["withheld"] == 0
    detail = conn.execute("SELECT detail_json FROM claim_link_jev_assessments").fetchone()[0]
    assert json.loads(detail) == {"error": "remote_disabled"}

    other = duckdb.connect()
    _contradiction_pair(other)
    runtime = FakeRuntime(TEXTS, RECONCILABLE)
    summary = run_claim_linking_pass(
        other, provider=FakeProvider(), nli=FakeNLI(),
        jev_reviewer=reviewer(runtime, "gate", resolve=lambda *_: None),
    )
    assert summary["links"]["contradicts"] == 1
    assert summary["jev"]["unresolved"] == 1 and runtime.calls == []


def test_delete_run_reverts_jev_assessments(conn):
    _contradiction_pair(conn)
    summary = run_claim_linking_pass(
        conn, provider=FakeProvider(), nli=FakeNLI(),
        jev_reviewer=reviewer(FakeRuntime(TEXTS, CONFLICT), "shadow"),
    )
    result = delete_run(conn, summary["run_id"])
    assert result["jev_assessments_deleted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM claim_link_jev_assessments").fetchone()[0] == 0


def test_programming_errors_are_not_swallowed(conn):
    _contradiction_pair(conn)
    broken = FakeRuntime(TEXTS, CONFLICT, fail=KeyError("bug"))
    with pytest.raises(KeyError):
        run_claim_linking_pass(
            conn, provider=FakeProvider(), nli=FakeNLI(),
            jev_reviewer=reviewer(broken, "gate"),
        )
    # Nothing was written: the review step runs before the write transaction.
    assert conn.execute("SELECT COUNT(*) FROM claim_links").fetchone()[0] == 0


def test_reviewer_rejects_unknown_mode():
    with pytest.raises(ValueError, match="mode"):
        reviewer(FakeRuntime(TEXTS, CONFLICT), "auto")


def test_document_revision_resolver_requires_one_exact_committed_span(conn):
    from src.ingestion.revisions import _DDL

    _seed_claims(conn, [("c1", "Rates will rise.", "d1", BASE_MS, None)])
    resolve = document_revision_resolver(conn)
    assert resolve("c1", "Rates will rise.") is None  # no revision tables yet
    conn.execute(_DDL)
    body = "Intro. Rates will rise. Outro."
    conn.execute(
        "INSERT INTO document_revision_records VALUES "
        "('d1', 1, 'rev-1', NULL, NULL, 'p', 'r', ?, 'ph', 'ch', 'mh', 'new',"
        " 'material', 'active', NULL, NULL, 0, 5, 0)",
        [json.dumps({"content": body})],
    )
    conn.execute("INSERT INTO document_current_revisions VALUES ('d1', 'rev-1', 1, 'active', 0)")
    span = resolve("c1", "Rates will rise.")
    assert span == {
        "source_ref": {"document_id": "d1", "revision_id": "rev-1"},
        "locator": {"start": 7, "end": 23},
    }
    assert resolve("c1", "Absent text.") is None
    conn.execute(
        "UPDATE document_revision_records SET payload_json = ?",
        [json.dumps({"content": body + " Rates will rise."})],
    )
    assert resolve("c1", "Rates will rise.") is None  # ambiguous


def test_end_to_end_with_the_real_decision_runtime(conn):
    """Real DecisionRuntime + revision store: spans resolve from committed
    revisions, the runtime's own transactions run outside the link write, and
    the gate withholds the reconcilable contradiction."""
    from src.ingestion.revisions import DocumentRevisionStore
    from src.kb.decision_runtime import DecisionRuntime

    _contradiction_pair(conn)
    revisions = DocumentRevisionStore(conn)
    revisions.observe({"document_id": "d1", "content": f"Lead. {DUP_A} More."})
    revisions.observe({"document_id": "d3", "content": CONTRA})

    class Client:
        def __init__(self):
            self.requests = []

        def decide(self, request, *, api_key, timeout_s, request_id, max_attempts,
                   max_cost_micros_per_attempt, reserve_attempt):
            reserve_attempt(1, max_cost_micros_per_attempt)
            self.requests.append(request)
            return {
                "contract": "noesis-typed-decision-v1", "status": "answered",
                "answers": {
                    "kind": {"kind": "choice", "status": "answered", "value": "scope"},
                    "jointly_true": {"kind": "noul", "status": "answered", "value": 0.8},
                    "same_subject": {"kind": "noul", "status": "answered", "value": 0.9},
                },
                "model_requested": "jev-1.13.0", "model_returned": "jev-1.13.0",
            }

    client = Client()
    runtime = DecisionRuntime(conn, client=client, credential_resolver=lambda ref: "secret")
    policy = {
        "hosted_allowed": True, "model": "jev-1.13.0", "rubric_id": "contradiction-kind-v1",
        "policy_id": "test-hosted-v1", "credential_ref": "typesafe", "budget_id": "links",
        "max_total_cost_usd_micros": 10_000, "max_concurrent": 1,
    }
    review = JevClaimLinkReviewer(
        runtime, "kb", principal_id="alice", scopes={"operator"}, policy=policy,
        allow_remote=True, resolve_claim=document_revision_resolver(conn), mode="gate",
    )
    summary = run_claim_linking_pass(
        conn, provider=FakeProvider(), nli=FakeNLI(), jev_reviewer=review,
    )
    assert summary["jev"]["reviewed"] == 1 and summary["jev"]["withheld"] == 1
    assert summary["links"]["contradicts"] == 0
    (request,) = client.requests
    # Only the exact claim spans leave the warehouse, not the whole document.
    sent = sorted(source["content"] for source in request.state["sources"])
    assert sent == sorted([DUP_A, CONTRA])
    assert conn.execute(
        "SELECT status FROM hosted_decision_runs"
    ).fetchone()[0] == "completed"
