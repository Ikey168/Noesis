"""Linking/review operations: spans, decision rules, supersession, reuse, cache, ops."""

from __future__ import annotations

import json
import logging

import duckdb
import pytest

from src.argument_mining.boilerplate import boilerplate_reason
from src.argument_mining.spans import locate_span
from src.ingestion.revisions import DocumentRevisionStore
from src.integrations.typesafe import TypeSafeClient
from src.kb import claim_links
from src.kb.claim_exclusions import run_claim_exclusion_pass, undo_claim_exclusions
from src.kb.claim_link_batch import BatchConfig
from src.kb.claim_link_review import JevClaimLinkReviewer, document_revision_resolver
from src.kb.claim_links import ensure_claim_link_schema, run_claim_linking_pass
from src.kb.decision_runtime import DecisionRuntime
from src.kb.jev_evidence_adapters import contradiction_decision, supersession_decision
from tests.unit.kb.test_claim_hygiene import WordNLI
from tests.unit.kb.test_claim_link_batch import POLICY, FakeTransport, _claims, _mixed
from tests.unit.kb.test_claim_link_review import FakeRuntime, resolver

DAY = 86_400_000
BASE = 1_750_000_000_000


@pytest.fixture()
def conn():
    return duckdb.connect()


def _seed(conn, rows):
    """rows: (claim_id, text, document_id, source_id, ingested_at)."""
    ensure_claim_link_schema(conn)
    for claim_id, text, document_id, source_id, ingested_at in rows:
        conn.execute(
            "INSERT OR IGNORE INTO documents (document_id, source_type, source_id, ingested_at)"
            " VALUES (?, 'blog', ?, ?)",
            [document_id, source_id, ingested_at],
        )
        conn.execute(
            "INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type, confidence)"
            " VALUES (?, ?, ?, 'blog', 0.8)",
            [claim_id, text, document_id],
        )


def _links(conn):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT claim_a, claim_b, relation FROM claim_links").fetchall())


def _fake_reviewer(texts, labels, mode="gate", **kwargs):
    return JevClaimLinkReviewer(
        FakeRuntime(texts, dict(labels)), "ns", principal_id="alice", scopes={"operator"},
        policy={"model": "jev-1.13.0"}, allow_remote=True, resolve_claim=resolver, mode=mode,
        **kwargs,
    )


# -- #14 canonicalize_url --------------------------------------------------------


def test_template_port_returns_the_url_unchanged():
    from src.ingestion.canonical import canonicalize_url
    from src.osint.independence import _canonical_url

    url = "https://Example.com:{port}/a?utm_source=x"
    assert canonicalize_url(url) == url
    assert _canonical_url(url) == url
    assert canonicalize_url("https://EXAMPLE.com:8443/a/") == "https://example.com:8443/a"


# -- #4 / #5 spans ---------------------------------------------------------------


def test_locate_span_reports_how_it_matched():
    source = "Intro.  The loader   writes a “scheduled task”.\nThe loader writes.  The loader writes."
    assert locate_span(source, "The loader writes.")["match"] == "first_of_n"
    span = locate_span(source, "The loader writes a “scheduled task”.")
    assert span["match"] == "whitespace_insensitive"
    assert source[span["start"]:span["end"]].startswith("The loader   writes")
    assert locate_span(source, 'The loader writes a "scheduled task".')["match"] == "normalized"
    assert locate_span(source, "Absent text") is None


def test_extraction_stores_offsets_and_the_resolver_prefers_them(conn):
    from src.kb.workflows import _materialize_argument_claims

    ensure_claim_link_schema(conn)
    revisions = DocumentRevisionStore(conn)
    body = "Lead.   The loader  writes a task. Tail. The loader writes a task."
    record = revisions.observe({"document_id": "d1", "content": body})
    conn.execute("INSERT INTO documents (document_id, source_type) VALUES ('d1', 'blog')")
    span = locate_span(body, "The loader writes a task.")
    outputs = [{"status": "produced", "output": {"output_type": "claim", "value": {
        "claim_id": "c1", "statement": "The loader writes a task.", "document_id": "d1",
        "source_locator": {"document_id": "d1", "revision_id": record["revision_id"],
                           "start": span["start"], "end": span["end"], "match": span["match"]},
    }}}]
    _materialize_argument_claims(conn, [{"document_id": "d1"}], {"outputs": outputs}, run_id="wf")
    stored = conn.execute(
        "SELECT span_revision_id, span_start, span_end, span_match FROM argument_claims").fetchone()
    assert stored == (record["revision_id"], span["start"], span["end"], "exact")
    resolved = document_revision_resolver(conn)("c1", "The loader writes a task.")
    assert resolved["match"] == "stored_offsets"
    assert resolved["locator"] == {"start": span["start"], "end": span["end"]}
    # Offsets from an older revision are ignored in favour of a fresh match.
    revisions.observe({"document_id": "d1", "content": "New. The loader writes a task."})
    assert document_revision_resolver(conn)("c1", "The loader writes a task.")["match"] == "exact"


# -- #6 tie-break and #2 same-subject gate -----------------------------------------


@pytest.mark.parametrize("kind,jointly,same,tie,gate,expected", [
    ("direct", 0.05, 0.9, "abstain", None, ("conflict", None, "consistent")),
    ("direct", 0.9, 0.9, "abstain", None, (None, "inconsistent_answers", "abstain")),
    ("direct", 0.9, 0.9, "label", None, ("conflict", None, "label")),
    ("direct", 0.9, 0.9, "probabilities", None, ("no_conflict", None, "probabilities")),
    ("not_contradiction", 0.1, 0.8, "abstain", None, (None, "inconsistent_answers", "abstain")),
    ("not_contradiction", 0.1, 0.8, "label", None, ("no_conflict", None, "label")),
    ("not_contradiction", 0.1, 0.8, "probabilities", None, ("conflict", None, "probabilities")),
    ("quantitative", 0.05, 0.6, "abstain", 0.8, ("no_conflict", "different_subject", "same_subject_gate")),
    ("temporal", 0.9, 0.9, "label", None, ("reconcilable", None, "consistent")),
])
def test_contradiction_tie_break_rules(kind, jointly, same, tie, gate, expected):
    assert contradiction_decision(kind, jointly, same, tie_break=tie, same_subject_gate=gate) == expected


def test_decision_rule_is_recorded_and_the_same_subject_gate_withholds(conn):
    _seed(conn, [
        ("c1", "The event-stream package runs the payload.", "d1", "a", BASE),
        ("c2", "The event-stream package does not run the payload.", "d2", "b", BASE),
    ])
    texts = {"c1": "The event-stream package runs the payload.",
             "c2": "The event-stream package does not run the payload."}
    labels = {"kind": "direct", "jointly_true": 0.05, "same_subject": 0.6}
    summary = run_claim_linking_pass(conn, nli=WordNLI(), jev_reviewer=_fake_reviewer(texts, labels))
    assert summary["links"]["contradicts"] == 1  # 0.6 >= the default 0.5 threshold
    detail = json.loads(conn.execute("SELECT detail_json FROM claim_link_jev_assessments").fetchone()[0])
    assert detail["decided_by"] == "consistent"

    gated = duckdb.connect()
    _seed(gated, [(c, t, f"d{c}", s, BASE) for (c, t), s in zip(texts.items(), "ab", strict=True)])
    summary = run_claim_linking_pass(
        gated, nli=WordNLI(), jev_reviewer=_fake_reviewer(texts, labels, same_subject_gate=0.8))
    assert summary["links"]["contradicts"] == 0 and summary["jev"]["withheld"] == 1
    detail = json.loads(gated.execute("SELECT detail_json FROM claim_link_jev_assessments").fetchone()[0])
    assert (detail["reason"], detail["decided_by"]) == ("different_subject", "same_subject_gate")


# -- #3 supersession rubric ---------------------------------------------------------


OLD = "The vendor package event-stream runs the payload on install."
NEW = "The vendor package event-stream does not run the payload on install."


def _same_source(conn):
    _seed(conn, [("old", OLD, "d1", "vendor", BASE), ("new", NEW, "d2", "vendor", BASE + 5 * DAY)])


def test_supersession_is_reviewed_with_its_own_rubric(conn):
    _same_source(conn)
    reviewer = _fake_reviewer({"old": OLD, "new": NEW}, {"kind": "updates", "same_subject": 0.9})
    summary = run_claim_linking_pass(conn, nli=WordNLI(), jev_reviewer=reviewer)
    assert _links(conn) == [("new", "old", "supersedes")]
    assert reviewer.runtime.calls[0]["task"] == "jev-supersession-v1"
    state = reviewer.runtime.calls[0]["state"]
    assert set(state) == {"later_claim", "earlier_claim"}
    assert summary["jev"]["planned"] == {"supersedes": 1} and summary["jev"]["agrees"] == 1
    row = conn.execute(
        "SELECT claim_a, claim_b, relation, verdict, jev_relation FROM claim_link_jev_assessments"
    ).fetchone()
    assert row == ("new", "old", "supersedes", "agrees", "supersedes")


def test_an_independent_pair_is_not_kept_as_a_supersession(conn):
    _same_source(conn)
    reviewer = _fake_reviewer({"old": OLD, "new": NEW}, {"kind": "independent", "same_subject": 0.2})
    run_claim_linking_pass(conn, nli=WordNLI(), jev_reviewer=reviewer)
    assert _links(conn) == []
    assert supersession_decision("restates", 0.9) == ("restates", None, "consistent")
    assert supersession_decision("updates", 0.2, tie_break="label") == ("supersedes", None, "label")


# -- #11 per-link settlement ----------------------------------------------------------


def _real_reviewer(conn, transport, *, batch=None, policy=None):
    runtime = DecisionRuntime(
        conn, client=TypeSafeClient(transport=transport, sleep=lambda _: None),
        credential_resolver=lambda ref: "secret",
    )
    return JevClaimLinkReviewer(
        runtime, "kb", principal_id="alice", scopes={"operator"}, policy=policy or POLICY,
        allow_remote=True, resolve_claim=document_revision_resolver(conn), batch=batch,
    )


def test_per_link_reviews_settle_to_reported_usage(conn):
    items = _claims(conn, _mixed(8))
    reviewer = _real_reviewer(conn, FakeTransport())
    reviews = reviewer.review_all("run-settle", items)
    assert all(r["status"] == "answered" for r in reviews)
    ledger = conn.execute(
        "SELECT reserved_cost_micros FROM hosted_decision_budgets WHERE budget_id = 'links'"
    ).fetchone()[0]
    settled = [s["settled_usd_micros"] for s in reviewer.stats]
    assert ledger == sum(settled)
    assert all(s["settled_usd_micros"] < s["reserved_usd_micros"] for s in reviewer.stats)
    assert all(s["settled_usd_micros"] == reviewer.settle_cost({"input_tokens": s["input_tokens"]})
               for s in reviewer.stats)


# -- #9 per-relation batching ---------------------------------------------------------


def test_default_batching_packs_duplicates_only(conn):
    items = _claims(conn, _mixed(12))
    transport = FakeTransport()
    reviews = _real_reviewer(conn, transport, batch=BatchConfig()).review_all("run-rel", items)
    assert all(r["status"] == "answered" for r in reviews)
    batched = [b for b in transport.bodies if "links" in b["state"]]
    assert len(batched) == 1 and len(batched[0]["state"]["links"]) == 3  # the 3 duplicates
    assert len(transport.bodies) == 1 + 9  # 9 supports/contradicts reviewed per link
    for item, review in zip(items, reviews, strict=True):
        assert ("batch" in review["detail"]) is (item["relation"] == "duplicate")
    with pytest.raises(ValueError, match="relations"):
        BatchConfig(relations=("corrects",))


# -- #7 reuse, #12 cache, #13 progress, #15 chunked writes -----------------------------

GROUP = [
    ("a1", "The event-stream package steals wallet keys.", "d1", "s1", BASE),
    ("a2", "The event-stream package steals wallet keys.", "d2", "s2", BASE),  # identical text
    ("b1", "The event-stream package steals keys.", "d3", "s3", BASE),
]


def _group_reviewer():
    texts = {cid: text for cid, text, *_ in GROUP}
    return _fake_reviewer(texts, {"a_to_b": "entailment", "b_to_a": "entailment"}, mode="shadow")


def test_identical_wording_and_duplicate_groups_are_asked_once(conn, caplog):
    _seed(conn, GROUP)
    reviewer = _group_reviewer()
    with caplog.at_level(logging.INFO, logger="src.kb.claim_links"):
        summary = run_claim_linking_pass(conn, nli=WordNLI(), jev_reviewer=reviewer)
    jev = summary["jev"]
    # a1~a2 duplicate, plus a1->b1 and a2->b1 supports: three links planned.
    assert jev["planned"] == {"duplicate": 1, "supports": 2}
    # Once a1~a2 is a confirmed duplicate, a2->b1 reuses a1->b1 (identical text too).
    assert jev["requests"] == len(reviewer.runtime.calls) == 2
    assert jev["reused"]["identical_text"] + jev["reused"]["duplicate_group"] == 1
    reused = [json.loads(r[0]).get("reused_from") for r in conn.execute(
        "SELECT detail_json FROM claim_link_jev_assessments WHERE relation = 'supports'").fetchall()]
    assert sorted(bool(r) for r in reused) == [False, True]
    assert any("3 planned" in record.message for record in caplog.records)
    assert any("asked" in record.message for record in caplog.records)


def test_a_restart_reuses_answers_already_paid_for(conn, monkeypatch):
    _seed(conn, GROUP)
    original = claim_links._write_reviewed

    def crash(*args, **kwargs):
        raise MemoryError("write ran out of memory")

    monkeypatch.setattr(claim_links, "_write_reviewed", crash)
    with pytest.raises(MemoryError):
        run_claim_linking_pass(conn, nli=WordNLI(), jev_reviewer=_group_reviewer())
    monkeypatch.setattr(claim_links, "_write_reviewed", original)
    assert _links(conn) == []
    reviewer = _group_reviewer()
    summary = run_claim_linking_pass(conn, nli=WordNLI(), jev_reviewer=reviewer)
    assert reviewer.runtime.calls == []  # every answer came from the persisted cache
    # The two answers that were asked are cached; the third reuses them again.
    reused = summary["jev"]["reused"]
    assert reused["cache"] == 2 and sum(reused.values()) == 3
    assert len(_links(conn)) == 3
    assert run_claim_linking_pass(duckdb.connect(), nli=WordNLI(), review_cache=False)["scanned"] == 0


def test_writes_commit_in_chunks(conn, monkeypatch):
    _seed(conn, [(f"c{i}", f"Package pkg-{i} raises the levy.", f"d{i}", "s", BASE) for i in range(5)])
    calls = {"n": 0}
    original = claim_links._write_reviewed

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("crash after three claims")
        return original(*args, **kwargs)

    monkeypatch.setattr(claim_links, "_write_reviewed", flaky)
    with pytest.raises(RuntimeError):
        run_claim_linking_pass(conn, nli=WordNLI(), write_chunk_claims=3)
    scanned = conn.execute("SELECT COUNT(*) FROM kb_claim_link_scans").fetchone()[0]
    assert scanned == 3  # the first chunk committed; the failed one rolled back
    monkeypatch.setattr(claim_links, "_write_reviewed", original)
    assert run_claim_linking_pass(conn, nli=WordNLI())["scanned"] == 2
    with pytest.raises(ValueError):
        run_claim_linking_pass(duckdb.connect(), nli=WordNLI(), write_chunk_claims=0)


# -- #1 boilerplate gaps and the excluded_reason column -----------------------------------


@pytest.mark.parametrize("text,reason", [
    ("Trusted by 150k+ orgs worldwide", "marketing"),
    ("Related: How attackers abuse GitHub Actions", "navigation"),
    ("You might also enjoy our report on loaders", "navigation"),
    ("Jane Doe is a senior threat researcher at Acme Security.", "byline"),
    ("Protect your supply chain with Acme today.", "marketing"),
])
def test_more_furniture_is_recognised(text, reason):
    assert boilerplate_reason(text) == reason


@pytest.mark.parametrize("text", [
    "The CEO is a former analyst who joined in 2021 after the breach was disclosed publicly by regulators.",
    "Lumma is a stealer that targets browser credentials and crypto wallets.",
    "The package was trusted by thousands of developers before the takeover.",
])
def test_claims_resembling_furniture_are_kept(text):
    assert boilerplate_reason(text) is None


def test_excluded_reason_column_mirrors_exclusions(conn):
    _seed(conn, [
        ("f1", "Trusted by 150k+ orgs worldwide", "d1", "s", BASE),
        ("k1", "The loader writes a scheduled task named Updater.", "d2", "s", BASE),
    ])
    run_claim_exclusion_pass(conn, run_id="ex-1")
    assert dict(conn.execute("SELECT claim_id, excluded_reason FROM argument_claims").fetchall()) == {
        "f1": "marketing", "k1": None}
    undo_claim_exclusions(conn, "ex-1")
    assert conn.execute("SELECT COUNT(*) FROM argument_claims WHERE excluded_reason IS NOT NULL").fetchone()[0] == 0
