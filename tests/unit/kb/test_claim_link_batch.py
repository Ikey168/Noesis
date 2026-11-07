"""Batched Jev claim-link review through the real runtime and a fake transport.

Only the HTTP transport is faked: DecisionRuntime, the revision store, the
TypeSafe client (retries, response validation) and the reviewer are real.
"""

from __future__ import annotations

import json
import math

import duckdb
import pytest

from src.ingestion.revisions import DocumentRevisionStore
from src.integrations.typesafe import TypeSafeClient
from src.kb.claim_link_batch import BatchConfig
from src.kb.claim_link_review import JevClaimLinkReviewer, document_revision_resolver
from src.kb.claim_links import delete_run, run_claim_linking_pass
from src.kb.decision_runtime import DecisionRuntime
from tests.unit.kb.test_claim_links import (
    BASE_MS,
    CONTRA,
    DAY_MS,
    DUP_A,
    FakeNLI,
    FakeProvider,
    _seed_claims,
)

POLICY = {
    "hosted_allowed": True,
    "model": "jev-1.13.0",
    "rubric_id": "claim-link-review-v1",
    "policy_id": "test-hosted-v1",
    "credential_ref": "typesafe",
    "budget_id": "links",
    "max_total_cost_usd_micros": 10_000_000,
    "max_concurrent": 2,
}

# -- deterministic fake System One -------------------------------------------


def _words(text):
    return set(text.lower().replace(".", "").split())


def _negated(text):
    return " not " in f" {text.lower()} "


def judge(suffix, a, b):
    """The fake model's answer for one question about claims (a, b)."""
    if suffix == "kind":
        if "2019" in a or "2019" in b:
            return "temporal"
        return "direct" if _negated(a) != _negated(b) else "not_contradiction"
    if suffix == "jointly_true":
        return 0.05 if _negated(a) != _negated(b) and "2019" not in a + b else 0.9
    if suffix == "same_subject":
        return 0.95
    premise, hypothesis = (a, b) if suffix == "a_to_b" else (b, a)
    if _negated(premise) != _negated(hypothesis):
        return "contradiction"
    return "entailment" if _words(hypothesis) <= _words(premise) else "neutral"


def _answer(question, value):
    if question["type"] == "noul":
        return {"type": "noul", "noul": value}
    labels = list(question["criteria"])
    rest = 0.1 / (len(labels) - 1)
    probabilities = {label: (0.9 if label == value else rest) for label in labels}
    return {"type": "choice", "choice": value, "probabilities": probabilities, "confidence": 0.8}


class FakeTransport:
    """Answers each question from the claims it names; scripted failures."""

    def __init__(self):
        self.bodies = []
        self.script = []  # statuses to return before answering normally
        self.max_links = None  # >max_links links -> 400 max_tokens_exceeded
        self.fail_if = None  # predicate(body) -> status to return

    def __call__(self, *, body, api_key, timeout_s):
        assert api_key == "secret"
        self.bodies.append(body)
        if self.script:
            status = self.script.pop(0)
            return status, {"retry-after": "0"}, b'{"error": {"code": "busy"}}'
        links = body["state"].get("links")
        if self.max_links is not None and links is not None and len(links) > self.max_links:
            return 400, {}, json.dumps({"error": {"code": "max_tokens_exceeded"}}).encode()
        if self.fail_if and (status := self.fail_if(body)):
            return status, {}, b"{}"
        answers = {}
        for qid, question in body["questions"].items():
            if "__" in qid:
                key, suffix = qid.split("__", 1)
                pair = links[key]
            else:
                suffix, pair = qid, body["state"]
            answers[qid] = _answer(question, judge(suffix, pair["claim_a"], pair["claim_b"]))
        tokens = math.ceil(len(json.dumps(body).encode()) / 4)
        payload = {"model": "jev-1.13.0", "answers": answers,
                   "usage": {"input_tokens": tokens, "output_tokens": 0}}
        return 200, {}, json.dumps(payload).encode()


# -- fixtures -----------------------------------------------------------------


def _claims(conn, pairs):
    """pairs: [(relation, text_a, text_b)] -> review items over committed revisions."""
    revisions = DocumentRevisionStore(conn)
    rows, items = [], []
    for index, (relation, text_a, text_b) in enumerate(pairs):
        ids = (f"a{index:03d}", f"b{index:03d}")
        for claim_id, text in zip(ids, (text_a, text_b), strict=True):
            rows.append((claim_id, text, f"doc-{claim_id}", BASE_MS, None))
        items.append({
            "relation": relation,
            "premise": (ids[0], text_a),
            "hypothesis": (ids[1], text_b),
            "similarity": 0.9,
            "duplicate_threshold": 0.88,
        })
    _seed_claims(conn, rows)
    for claim_id, text, doc_id, _, _ in rows:
        revisions.observe({"document_id": doc_id, "content": f"Lead. {text} Tail."})
    return items


def _mixed(n):
    pairs = []
    for i in range(n):
        kind = i % 4
        if kind == 0:
            pairs.append(("contradicts", f"Policy {i} raises the levy.", f"Policy {i} does not raise the levy."))
        elif kind == 1:
            pairs.append(("contradicts", f"Policy {i} raises the levy.", f"In 2019 policy {i} does not raise the levy."))
        elif kind == 2:
            pairs.append(("supports", f"Policy {i} sharply raises the levy.", f"Policy {i} raises the levy."))
        else:
            pairs.append(("duplicate", f"Policy {i} raises the levy.", f"Policy {i} raises the levy."))
    return pairs


@pytest.fixture()
def conn():
    return duckdb.connect()


def _reviewer(conn, transport, *, mode="gate", batch=None, policy=None, sleeps=None):
    client = TypeSafeClient(
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else (lambda _: None)),
    )
    runtime = DecisionRuntime(conn, client=client, credential_resolver=lambda ref: "secret")
    return JevClaimLinkReviewer(
        runtime, "kb", principal_id="alice", scopes={"operator"},
        policy=policy or POLICY, allow_remote=True,
        resolve_claim=document_revision_resolver(conn), mode=mode, batch=batch,
    )


def _comparable(review):
    detail = {k: v for k, v in review["detail"].items() if k != "batch"}
    return (review["status"], review["verdict"], review["jev_relation"], review["action"], detail)


# -- tests --------------------------------------------------------------------


def test_packing_respects_state_budget_and_max_links(conn):
    items = _claims(conn, _mixed(60))
    transport = FakeTransport()
    config = BatchConfig(state_token_budget=1_500, max_links=25)
    reviewer = _reviewer(conn, transport, batch=config)
    reviews = reviewer.review_all("run-pack", items)
    assert all(r["status"] == "answered" for r in reviews)
    sizes = [len(body["state"]["links"]) for body in transport.bodies]
    assert sum(sizes) == 60 and 1 < len(sizes) < 60
    assert max(sizes) <= 25
    for body in transport.bodies:
        assert config.estimate(body["state"]) <= config.state_token_budget
        assert config.estimate(body) <= config.request_token_limit
    # A larger budget packs up to the link cap instead.
    roomy = FakeTransport()
    _reviewer(conn, roomy, batch=BatchConfig(max_links=25)).review_all("run-pack-2", items)
    assert [len(b["state"]["links"]) for b in roomy.bodies] == [25, 25, 10]


def test_each_question_is_answered_against_its_own_link(conn):
    items = _claims(conn, _mixed(16))
    transport = FakeTransport()
    reviews = _reviewer(conn, transport, batch=BatchConfig()).review_all("run-map", items)
    (body,) = transport.bodies
    links = body["state"]["links"]
    for qid, question in body["questions"].items():
        key = qid.split("__")[0]
        assert key in links
        assert f"`links.{key}`" in question["instructions"]
        assert "`claim_a`" not in question["instructions"]
    expected = {
        0: ("conflict", "agrees"),
        1: ("reconcilable", "disagrees"),
        2: ("a_supports_b", "agrees"),
        3: ("duplicate", "agrees"),
    }
    for index, review in enumerate(reviews):
        assert (review["jev_relation"], review["verdict"]) == expected[index % 4]
        assert review["detail"]["batch"]["batch_size"] == 16
        assert review["decision_run_id"] == review["detail"]["batch"]["batch_id"]
    # The claim text in state is exactly the committed span, nothing else.
    assert links["L0000"] == {"claim_a": "Policy 0 raises the levy.",
                              "claim_b": "Policy 0 does not raise the levy."}


@pytest.mark.parametrize("mode", ["shadow", "gate"])
def test_batched_and_per_link_reviews_are_identical(conn, mode):
    pairs = _mixed(12) + [
        ("contradicts", "Same claim twice.", "Same claim twice."),  # same text, distinct spans
        ("supports", "x " * 2100 + "long.", "long."),  # long claim abstains before asking
    ]
    items = _claims(conn, pairs)
    per_link = _reviewer(conn, FakeTransport(), mode=mode).review_all("run-eq-a", items)
    batched_transport = FakeTransport()
    batched = _reviewer(conn, batched_transport, mode=mode, batch=BatchConfig()).review_all("run-eq-b", items)
    assert [_comparable(r) for r in per_link] == [_comparable(r) for r in batched]
    assert len(batched_transport.bodies) == 1
    withheld = [r["action"] == "withheld" for r in batched]
    assert any(withheld) is (mode == "gate")
    assert batched[-1]["detail"]["reason"] == "window_coverage_unverified"


def test_shadow_rollout_abstains_identically(conn):
    items = _claims(conn, _mixed(4))
    for batch in (None, BatchConfig()):
        reviewer = _reviewer(conn, FakeTransport(), batch=batch)
        reviewer.runtime.configure_task_rollout(
            "kb", "jev-contradiction_kind-v1", "shadow", model="jev-1.13.0",
            rubric_id=POLICY["rubric_id"], evaluation_ref=None,
            principal_id="alice", scopes={"operator"},
        )
        reviews = reviewer.review_all(f"run-shadow-{batch is None}", items)
        contradicts = [r for r, item in zip(reviews, items, strict=True) if item["relation"] == "contradicts"]
        assert {(r["status"], r["detail"]["reason"]) for r in contradicts} == {("abstained", "shadow_rollout")}
        assert all(r["write"] for r in contradicts)


def test_partial_failure_leaves_only_that_batch_unreviewed(conn):
    items = _claims(conn, _mixed(20))
    transport = FakeTransport()
    transport.fail_if = lambda body: 500 if "L0012" in body["state"]["links"] else None
    reviews = _reviewer(conn, transport, batch=BatchConfig(max_links=10)).review_all("run-partial", items)
    failed, ok = reviews[10:], reviews[:10]
    assert {(r["status"], r["detail"]["error"], r["action"]) for r in failed} == {
        ("unavailable", "provider_error", "written")
    }
    assert all(r["status"] == "answered" for r in ok)


def test_429_and_529_retry_with_bounded_backoff(conn):
    items = _claims(conn, _mixed(8))
    transport = FakeTransport()
    transport.script = [429, 529]
    sleeps = []
    reviewer = _reviewer(conn, transport, batch=BatchConfig(max_attempts=3), sleeps=sleeps)
    reviews = reviewer.review_all("run-retry", items)
    assert all(r["status"] == "answered" for r in reviews)
    assert len(transport.bodies) == 3 and len(sleeps) == 2 and max(sleeps) <= 8
    assert reviewer.stats[-1]["attempts"] == 3

    exhausted = FakeTransport()
    exhausted.script = [429, 429, 429]
    reviews = _reviewer(conn, exhausted, batch=BatchConfig(max_attempts=3)).review_all("run-retry-2", items)
    assert {(r["status"], r["detail"]["error"], r["write"]) for r in reviews} == {
        ("unavailable", "rate_limited", True)
    }


def test_max_tokens_exceeded_splits_the_batch(conn):
    items = _claims(conn, _mixed(32))
    transport = FakeTransport()
    transport.max_links = 8
    reviewer = _reviewer(conn, transport, batch=BatchConfig())
    reviews = reviewer.review_all("run-split", items)
    assert all(r["status"] == "answered" for r in reviews)
    sizes = [len(b["state"]["links"]) for b in transport.bodies]
    assert sizes == [32, 16, 8, 8, 16, 8, 8]
    assert {r["detail"]["batch"]["split_depth"] for r in reviews} == {2}
    assert {r["detail"]["batch"]["batch_size"] for r in reviews} == {8}

    stubborn = FakeTransport()
    stubborn.max_links = 0
    reviews = _reviewer(conn, stubborn, batch=BatchConfig()).review_all("run-split-2", items[:2])
    assert {(r["status"], r["detail"]["error"], r["write"]) for r in reviews} == {
        ("unavailable", "max_tokens_exceeded", True)
    }


def _budget_run(conn, items, config, total, budget_id):
    policy = {**POLICY, "budget_id": budget_id, "max_total_cost_usd_micros": total}
    reviewer = _reviewer(conn, FakeTransport(), batch=config, policy=policy)
    reviews = reviewer.review_all(f"run-{budget_id}", items)
    ledger = conn.execute(
        "SELECT reserved_cost_micros FROM hosted_decision_budgets WHERE budget_id=?", [budget_id]
    ).fetchone()[0]
    return reviewer, reviews, ledger


def test_budget_is_never_exceeded_and_settles_to_reported_usage(conn):
    items = _claims(conn, _mixed(40))
    config = BatchConfig(max_links=10, max_attempts=1)
    probe = _reviewer(conn, FakeTransport(), batch=config)
    probe.review_all("run-probe", items[:10])
    one_batch = probe.stats[-1]["reserved_usd_micros"]

    # Room for exactly one reservation: after it settles, no further
    # reservation fits on top of its charge, so the remaining batches are
    # refused before any request is sent.
    total = one_batch + 10
    reviewer, reviews, ledger = _budget_run(conn, items, config, total, "tight")
    sent = [s for s in reviewer.stats if s["status"] == "completed"]
    refused = [r for r in reviews if r["status"] == "unavailable"]
    assert len(sent) == 1 and len(refused) == 30
    assert {r["detail"]["error"] for r in refused} == {"budget_exhausted"}
    assert all(r["write"] for r in refused)
    assert ledger <= total
    assert ledger == sent[0]["settled_usd_micros"] == config.cost(sent[0]["input_tokens"])
    assert sent[0]["settled_usd_micros"] <= sent[0]["reserved_usd_micros"]
    assert sent[0]["budget_overrun"] is False

    # Without settlement only two up-front reservations would fit in 2.5x;
    # settling each answered batch to its reported usage lets all four run.
    total = int(one_batch * 2.5)
    reviewer, reviews, ledger = _budget_run(conn, items, config, total, "settled")
    sent = [s for s in reviewer.stats if s["status"] == "completed"]
    assert len(sent) == 4 and all(r["status"] == "answered" for r in reviews)
    assert ledger <= total
    assert ledger == sum(s["settled_usd_micros"] for s in sent)


def test_concurrent_batches_match_sequential(conn):
    items = _claims(conn, _mixed(30))
    sequential = _reviewer(conn, FakeTransport(), batch=BatchConfig(max_links=10)).review_all("run-seq", items)
    concurrent_transport = FakeTransport()
    concurrent = _reviewer(
        conn, concurrent_transport, batch=BatchConfig(max_links=10, concurrency=2)
    ).review_all("run-par", items)
    assert [_comparable(r) for r in sequential] == [_comparable(r) for r in concurrent]
    assert len(concurrent_transport.bodies) == 3
    with pytest.raises(ValueError, match="max_concurrent"):
        _reviewer(conn, FakeTransport(), batch=BatchConfig(concurrency=3))


def test_linking_pass_records_batches_and_delete_run_reverts_them(conn):
    _seed_claims(conn, [
        ("c1", DUP_A, "d1", BASE_MS, "economics"),
        ("c3", CONTRA, "d3", BASE_MS + DAY_MS, None),
    ])
    revisions = DocumentRevisionStore(conn)
    revisions.observe({"document_id": "d1", "content": DUP_A})
    revisions.observe({"document_id": "d3", "content": CONTRA})
    transport = FakeTransport()
    summary = run_claim_linking_pass(
        conn, provider=FakeProvider(), nli=FakeNLI(),
        jev_reviewer=_reviewer(conn, transport, batch=BatchConfig()),
    )
    assert summary["jev"]["requests"] == 1 and summary["jev"]["agrees"] == 1
    assert summary["links"]["contradicts"] == 1
    detail = json.loads(conn.execute("SELECT detail_json FROM claim_link_jev_assessments").fetchone()[0])
    assert detail["batch"]["batch_size"] == 1 and detail["batch"]["batch_id"].startswith(summary["run_id"])
    assert delete_run(conn, summary["run_id"])["jev_assessments_deleted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM claim_link_jev_assessments").fetchone()[0] == 0


def test_client_recognizes_max_tokens_exceeded_without_retrying():
    from src.integrations.decisions import DecisionRequest

    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return 400, {}, b'{"error": {"code": "max_tokens_exceeded", "message": "too long"}}'

    request = DecisionRequest(
        task_id="t", rubric_id="r", state={"x": "y"},
        questions={"q": {"type": "noul", "instructions": "Is it?"}},
        source_binding={"kind": "document_revision", "document_id": "d", "revision_id": "r"},
    )
    receipt = TypeSafeClient(transport=transport, sleep=lambda _: None).decide(
        request, api_key="k", max_attempts=3, max_cost_micros_per_attempt=1,
        reserve_attempt=lambda *_: None,
    )
    assert receipt.execution["error_code"] == "max_tokens_exceeded" and len(calls) == 1


@pytest.mark.parametrize("kwargs", [
    {"state_token_budget": 40_000},
    {"request_token_limit": 70_000},
    {"max_links": 0},
    {"concurrency": 5},
    {"max_attempts": 4},
    {"bytes_per_token": 0.5},
])
def test_batch_config_rejects_out_of_range_limits(kwargs):
    with pytest.raises(ValueError):
        BatchConfig(**kwargs)


# -- validation helper ---------------------------------------------------------


def test_validation_reports_agreement_latency_and_tokens(conn):
    from src.evaluation.jev_link_batching import run_validation

    items = _claims(conn, _mixed(60))

    def make(batch):
        return _reviewer(conn, FakeTransport(), mode="shadow", batch=batch)

    report = run_validation(items, make, batch_sizes=(25, 120), run_prefix="val",
                            min_links_per_relation=10)
    assert report["production_switch"] is False
    assert report["per_link"]["requests"] == 60
    small, large = report["batched"]["batch_25"], report["batched"]["batch_120"]
    assert small["usage"]["requests"] == 3 and large["usage"]["requests"] == 1
    assert small["agreement"]["verdict_agreement"] == 1.0 == large["agreement"]["outcome_agreement"]
    assert large["usage"]["input_tokens"] < report["per_link"]["input_tokens"]
    assert small["recommended"] is True and large["recommended"] is True
    assert set(large["agreement"]["by_relation"]) == {"contradicts", "supports", "duplicate"}


def test_validation_withholds_recommendation_on_disagreement(conn):
    from src.evaluation.jev_link_batching import run_validation

    items = _claims(conn, _mixed(40))

    class DriftingTransport(FakeTransport):
        """Batched requests answer every contradiction as reconcilable."""

        def __call__(self, *, body, api_key, timeout_s):
            status, headers, content = super().__call__(body=body, api_key=api_key, timeout_s=timeout_s)
            if status == 200 and "links" in body["state"]:
                payload = json.loads(content)
                for qid, answer in payload["answers"].items():
                    if qid.endswith("__kind"):
                        answer.update(_answer(body["questions"][qid], "temporal"))
                content = json.dumps(payload).encode()
            return status, headers, content

    def make(batch):
        return _reviewer(conn, DriftingTransport() if batch else FakeTransport(), mode="shadow", batch=batch)

    report = run_validation(items, make, batch_sizes=(25,), run_prefix="drift", min_links_per_relation=5)
    result = report["batched"]["batch_25"]
    assert result["recommended"] is False
    assert result["agreement"]["by_relation"]["contradicts"]["verdict_agreement"] == 0.5
    assert result["agreement"]["by_relation"]["supports"]["verdict_agreement"] == 1.0
    assert any("contradicts verdict agreement" in r for r in result["not_recommended_because"])


def test_sample_planned_links_reads_stored_links(conn):
    from src.evaluation.jev_link_batching import sample_planned_links

    _seed_claims(conn, [
        ("c1", DUP_A, "d1", BASE_MS, "economics"),
        ("c3", CONTRA, "d3", BASE_MS + DAY_MS, None),
    ])
    run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())
    (item,) = sample_planned_links(conn, sample=10)
    assert item["relation"] == "contradicts"
    assert {item["premise"][1], item["hypothesis"][1]} == {DUP_A, CONTRA}
    with pytest.raises(ValueError):
        sample_planned_links(conn, sample=1, relations=("corrects",))


def test_per_link_provider_failure_is_unavailable_not_abstained(conn):
    """Default per-link path: a run that never answered stays explicit."""
    items = _claims(conn, _mixed(2))
    transport = FakeTransport()
    transport.script = [429, 429]
    reviews = _reviewer(conn, transport).review_all("run-per-link-429", items)
    assert [(r["status"], r["detail"]["error"], r["action"]) for r in reviews] == [
        ("unavailable", "rate_limited", "written"),
        ("unavailable", "rate_limited", "written"),
    ]
