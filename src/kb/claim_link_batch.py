"""Batched Jev review of proposed claim links (opt-in).

TypeSafe System One answers every question in a request in one parallel pass,
so reviewing many links per request costs one round trip instead of one per
link. This module packs the links a linking pass proposes into requests by
token budget and maps the answers back to each link, applying exactly the
per-link rules (:func:`derive_claim_relation`,
:func:`derive_contradiction_assessment`, :func:`link_agrees`).

Isolation
    Each link's two claim spans live in state under their own key
    (``links.L0007.claim_a``/``claim_b``). Every question for that link is
    id-prefixed ``L0007__`` and its instructions name ``links.L0007``, so an
    answer can only be attributed to the link it was asked about.

Source binding
    Spans are bound and rechecked by :class:`DecisionRuntime` exactly as in
    per-link review: each distinct claim span is one exact committed
    revision slice, re-authorized before every attempt and before
    publication. The request state is composed by the runtime from its own
    captures (``compose_state``), never from caller-supplied text.

Budget
    Each request reserves a strict upper bound before it is sent: every byte
    of the request body counted as a token (tokenizer tokens span at least
    one byte), plus fixed per-question and per-request overheads, priced at
    the configured input rate, times the attempts allowed. The runtime
    refuses a request that would exceed ``max_total_cost_usd_micros`` and,
    once answered, settles the reservation to the reported ``usage``.

Failures
    429/529 are retried by :class:`TypeSafeClient` with bounded backoff
    (``max_attempts``, Retry-After honoured within the deadline). ``400
    max_tokens_exceeded`` splits the batch in half and retries each half.
    Any other failed batch leaves its links unreviewed, so they are written.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from src.integrations.decisions import MAX_QUESTIONS
from src.kb.claim_link_review import (
    _REVIEW_FAILURES,
    contradiction_detail,
    link_agrees,
    relation_detail,
    run_failure,
    run_stats,
)
from src.kb.decision_runtime import DEFAULT_MAX_SOURCES, MAX_BATCH_SOURCES
from src.kb.jev_evidence_adapters import (
    LONG_CLAIM_CHARS,
    _probability,
    derive_claim_relation,
    derive_contradiction_assessment,
)
from src.kb.jev_tasks import CONTRADICTION_KINDS, prepare_task

BATCH_TASK = "jev-claim_link_review_batch-v1"
TASK_FOR = {
    "contradicts": "contradiction_kind",
    "supports": "claim_links",
    "duplicate": "claim_links",
}
#: Documented System One limits for jev-1.13.0/1.14.0.
PROVIDER_REQUEST_TOKENS = 64_000
PROVIDER_QUESTION_TOKENS = 32_000
#: Same span bound as per-link review.
SPAN_MAX_CHARS = 8000
#: Allowances for provider-side framing that the body bytes do not show.
QUESTION_OVERHEAD_TOKENS = 32
REQUEST_OVERHEAD_TOKENS = 512
STATE_NOTE = (
    "Each entry of `links` is one independent pair of claims. Every question "
    "names exactly one entry; judge it using only that entry's claims."
)


@dataclass(frozen=True)
class BatchConfig:
    """Opt-in packing limits for batched claim-link review.

    ``state_token_budget`` bounds each request's state (default 24,000, which
    leaves headroom under the 32k state-plus-one-question limit);
    ``request_token_limit`` bounds state plus all questions (kept under the
    64k request limit). Packing estimates tokens as UTF-8 bytes divided by
    ``bytes_per_token`` (3.0 by default, conservative for English text where
    ~4 is typical); an underestimate is caught by the provider's
    ``max_tokens_exceeded`` and the batch is split. Budget reservations do
    not use this estimate: they use the strict byte upper bound.
    """

    state_token_budget: int = 24_000
    request_token_limit: int = 60_000
    question_token_limit: int = 32_000
    max_links: int = 120
    concurrency: int = 1
    max_attempts: int = 3
    bytes_per_token: float = 3.0
    input_price_usd_micros_per_million_tokens: int = 42_000
    deadline_s: float = 60

    def __post_init__(self) -> None:
        ints = (
            "state_token_budget", "request_token_limit", "question_token_limit",
            "max_links", "concurrency", "max_attempts",
            "input_price_usd_micros_per_million_tokens",
        )
        if any(type(getattr(self, name)) is not int for name in ints):
            raise TypeError("batch limits must be integers")
        if not 1 <= self.question_token_limit <= PROVIDER_QUESTION_TOKENS:
            raise ValueError("question_token_limit must be within the provider's 32k")
        if not 1 <= self.request_token_limit <= PROVIDER_REQUEST_TOKENS:
            raise ValueError("request_token_limit must be within the provider's 64k")
        if not 1 <= self.state_token_budget < min(
            self.question_token_limit, self.request_token_limit
        ):
            raise ValueError("state_token_budget must leave room for questions")
        if not 1 <= self.max_links <= 1000:
            raise ValueError("max_links must be between 1 and 1000")
        if not 1 <= self.concurrency <= 4:
            raise ValueError("concurrency must be between 1 and 4")
        if not 1 <= self.max_attempts <= 3:
            raise ValueError("max_attempts must be between 1 and 3")
        if (
            not isinstance(self.bytes_per_token, (int, float))
            or not math.isfinite(self.bytes_per_token)
            or not 1 <= self.bytes_per_token <= 8
        ):
            raise ValueError("bytes_per_token must be between 1 and 8")
        if self.input_price_usd_micros_per_million_tokens < 1:
            raise ValueError("input price must be positive")
        if not 0.1 <= self.deadline_s <= 120:
            raise ValueError("deadline_s must be between 0.1 and 120")

    def estimate(self, value: Any) -> int:
        return math.ceil(_bytes(value) / self.bytes_per_token)

    def cost(self, tokens: int) -> int:
        return max(1, math.ceil(tokens * self.input_price_usd_micros_per_million_tokens / 1_000_000))


def _bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _ref_key(ref: Mapping[str, Any]) -> str:
    return json.dumps(dict(ref), sort_keys=True)


class _UnlockedNetworkClient:
    """Client wrapper that releases the database lock only for network I/O.

    Concurrent batches share one warehouse connection. Every runtime database
    step runs under ``lock``; the provider call (including backoff sleeps)
    runs without it, and attempt reservations re-take it.
    """

    def __init__(self, inner: Any, lock: threading.Lock) -> None:
        self.inner = inner
        self.lock = lock

    def decide(self, request, *, reserve_attempt=None, **kwargs):
        def reserve(ordinal: int, amount: int) -> None:
            with self.lock:
                reserve_attempt(ordinal, amount)

        self.lock.release()
        try:
            return self.inner.decide(
                request, reserve_attempt=reserve if reserve_attempt else None, **kwargs
            )
        finally:
            self.lock.acquire()


def _answered(answers: Mapping[str, Any], key: str, kind: str) -> Mapping[str, Any] | None:
    answer = answers.get(key)
    if (
        not isinstance(answer, Mapping)
        or answer.get("kind") != kind
        or answer.get("status") != "answered"
    ):
        return None
    return answer


class _Batcher:
    def __init__(self, reviewer: Any, run_id: str) -> None:
        self.reviewer = reviewer
        self.config: BatchConfig = reviewer.batch
        self.run_id = run_id
        self.lock = threading.Lock()
        self.captures: dict[str, Any] = {}

    # -- preparation -----------------------------------------------------

    def _capture(self, ref: Mapping[str, Any]) -> str:
        key = _ref_key(ref)
        if key not in self.captures:
            try:
                _, captures = self.reviewer.runtime.capture_sources(
                    self.reviewer.namespace, self.reviewer.principal_id, [dict(ref)],
                    self.reviewer.scopes,
                )
                content = captures[0].get("content") if len(captures) == 1 else None
                if not isinstance(content, str) or not content:
                    raise ValueError("captured source text is unavailable")
                self.captures[key] = content
            except _REVIEW_FAILURES as exc:
                self.captures[key] = exc
        value = self.captures[key]
        if isinstance(value, Exception):
            raise value
        return value

    @staticmethod
    def _span(text: str, locator: Mapping[str, Any]) -> tuple[dict[str, int], str]:
        start, end = locator.get("start"), locator.get("end")
        if (
            type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(text) or end - start > SPAN_MAX_CHARS
        ):
            raise ValueError("bounded exact source character span required")
        passage = text[start:end]
        if locator.get("quote") is not None and locator["quote"] != passage:
            raise ValueError("citation quote differs from current source revision")
        return {"start": start, "end": end}, passage

    def prepare(self, index: int, item: Mapping[str, Any]) -> dict[str, Any]:
        """Return a packable entry, or ``{"review": ...}`` decided locally."""
        reviewer = self.reviewer
        relation = item["relation"]
        base = reviewer.base(relation, None)

        def done(status, detail, jev_relation=None):
            return {"review": reviewer.finish(base, status, None, jev_relation, detail)}

        def shaped(reason):
            if relation == "contradicts":
                return contradiction_detail(None, reason, None, None)
            return relation_detail(None, None, reason)

        spans = [reviewer.resolve_claim(cid, text) for cid, text in (item["premise"], item["hypothesis"])]
        if any(span is None for span in spans):
            return done("unresolved", {"reason": "no_exact_claim_span"})
        try:
            similarity = float(min(max(item["similarity"], 0.0), 1.0))
            threshold = item["duplicate_threshold"]
            if relation != "contradicts" and (
                type(threshold) not in {int, float} or not math.isfinite(threshold)
                or not 0 <= threshold <= 1
            ):
                raise ValueError("finite similarity and duplicate threshold required")
            refs = [dict(span["source_ref"]) for span in spans]
            texts = [self._capture(ref) for ref in refs]
            located = [self._span(text, span["locator"]) for text, span in zip(texts, spans, strict=True)]
        except _REVIEW_FAILURES as exc:
            return done("unavailable", {"error": str(getattr(exc, "code", type(exc).__name__))})
        locators = [loc for loc, _ in located]
        passages = [passage for _, passage in located]
        if refs[0] == refs[1] and locators[0] == locators[1]:
            return done("abstained", shaped("same_claim_span"))
        if relation != "contradicts" and max(map(len, passages)) > LONG_CLAIM_CHARS:
            return done("abstained", shaped("window_coverage_unverified"))
        task = TASK_FOR[relation]
        try:
            state, questions = prepare_task(
                task, {"claim_a": passages[0], "claim_b": passages[1]}, source_count=2
            )
        except _REVIEW_FAILURES as exc:
            return done("unavailable", {"error": str(getattr(exc, "code", type(exc).__name__))})
        key = f"L{index:04d}"
        namespaced = {}
        for qid, question in questions.items():
            instructions = (
                f"This question is only about `links.{key}`. "
                + question["instructions"]
                .replace("`claim_a`", f"`links.{key}.claim_a`")
                .replace("`claim_b`", f"`links.{key}.claim_b`")
            )
            namespaced[f"{key}__{qid}"] = {**question, "instructions": instructions}
        entry = {"claim_a": state["claim_a"], "claim_b": state["claim_b"]}
        question_tokens = {qid: self.config.estimate(q) + QUESTION_OVERHEAD_TOKENS for qid, q in namespaced.items()}
        return {
            "index": index,
            "key": key,
            "item": item,
            "relation": relation,
            "task": task,
            "similarity": similarity,
            "sources": list(zip(refs, locators, strict=True)),
            "entry": entry,
            "questions": namespaced,
            "state_tokens": self.config.estimate({key: entry}),
            "question_tokens": sum(question_tokens.values()),
            "max_question_tokens": max(question_tokens.values()),
        }

    # -- packing ---------------------------------------------------------

    def pack(self, entries: list[dict[str, Any]]) -> tuple[list[list[dict]], list[dict]]:
        """Greedy, order-preserving packing. Returns (batches, oversized)."""
        cfg = self.config
        frame = cfg.estimate({"instructions": STATE_NOTE, "links": {}})
        batches: list[list[dict]] = []
        oversized: list[dict] = []
        current: list[dict] = []
        totals = {"state": frame, "questions": 0, "max_q": 0, "n_q": 0, "sources": set()}

        def fits(entry, t) -> bool:
            state = t["state"] + entry["state_tokens"]
            sources = t["sources"] | {(_ref_key(r), loc["start"], loc["end"]) for r, loc in entry["sources"]}
            return (
                state <= cfg.state_token_budget
                and state + t["questions"] + entry["question_tokens"] + REQUEST_OVERHEAD_TOKENS
                <= cfg.request_token_limit
                and state + max(t["max_q"], entry["max_question_tokens"]) <= cfg.question_token_limit
                and t["n_q"] + len(entry["questions"]) <= MAX_QUESTIONS
                and len(sources) <= MAX_BATCH_SOURCES
            )

        def add(entry, t):
            t["state"] += entry["state_tokens"]
            t["questions"] += entry["question_tokens"]
            t["max_q"] = max(t["max_q"], entry["max_question_tokens"])
            t["n_q"] += len(entry["questions"])
            t["sources"] |= {(_ref_key(r), loc["start"], loc["end"]) for r, loc in entry["sources"]}

        def fresh():
            return {"state": frame, "questions": 0, "max_q": 0, "n_q": 0, "sources": set()}

        for entry in entries:
            if current and (len(current) >= cfg.max_links or not fits(entry, totals)):
                batches.append(current)
                current, totals = [], fresh()
            if not current and not fits(entry, totals):
                oversized.append(entry)
                continue
            current.append(entry)
            add(entry, totals)
        if current:
            batches.append(current)
        return batches, oversized

    # -- execution -------------------------------------------------------

    def _batch_id(self, entries: list[dict], depth: int) -> str:
        digest = hashlib.sha256(
            json.dumps(
                [depth, [(e["key"], e["relation"], e["item"]["premise"][0], e["item"]["hypothesis"][0]) for e in entries]]
            ).encode()
        ).hexdigest()[:24]
        return f"{self.run_id[:150]}:jevb:{digest}"

    def execute(self, entries: list[dict], depth: int = 0) -> dict[int, dict]:
        reviewer, cfg = self.reviewer, self.config
        batch_id = self._batch_id(entries, depth)
        sources: list[tuple[dict, dict]] = []
        source_index: dict[tuple, int] = {}
        for entry in entries:
            entry["source_slots"] = []
            for ref, loc in entry["sources"]:
                key = (_ref_key(ref), loc["start"], loc["end"])
                if key not in source_index:
                    source_index[key] = len(sources)
                    sources.append((ref, loc))
                entry["source_slots"].append(source_index[key])
        questions = {qid: q for e in entries for qid, q in e["questions"].items()}
        local_state = {"instructions": STATE_NOTE, "links": {e["key"]: e["entry"] for e in entries}}
        bound = (
            _bytes(local_state) + _bytes(questions)
            + QUESTION_OVERHEAD_TOKENS * len(questions) + REQUEST_OVERHEAD_TOKENS
        )
        per_attempt = cfg.cost(bound)
        batch_meta = {"batch_id": batch_id, "batch_size": len(entries), "split_depth": depth}

        def compose(captures: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "instructions": STATE_NOTE,
                "links": {
                    e["key"]: {
                        "claim_a": captures[e["source_slots"][0]]["content"].strip(),
                        "claim_b": captures[e["source_slots"][1]]["content"].strip(),
                    }
                    for e in entries
                },
            }

        started = time.monotonic()
        try:
            with self.lock:
                run = reviewer.runtime.run(
                    reviewer.namespace, batch_id, BATCH_TASK,
                    state={},
                    questions=questions,
                    source_refs=[dict(ref) for ref, _ in sources],
                    source_slices=[dict(loc) for _, loc in sources],
                    principal_id=reviewer.principal_id,
                    scopes=reviewer.scopes,
                    allow_remote=reviewer.allow_remote,
                    policy=reviewer.policy,
                    max_attempts=cfg.max_attempts,
                    max_cost_usd_micros=per_attempt * cfg.max_attempts,
                    deadline_s=cfg.deadline_s,
                    max_sources=max(DEFAULT_MAX_SOURCES, len(sources)),
                    max_questions=len(questions),
                    compose_state=compose,
                    settle_cost=lambda usage: cfg.cost(int(usage["input_tokens"])),
                    component_tasks=sorted({f"jev-{e['task']}-v1" for e in entries}),
                )
        except _REVIEW_FAILURES as exc:
            code = str(getattr(exc, "code", type(exc).__name__))
            reviewer.stats.append({
                "kind": "batch", "decision_run_id": batch_id, "links": len(entries),
                "questions": len(questions), "status": "failed", "error": code,
                "latency_s": round(time.monotonic() - started, 4),
                "estimated_state_tokens": cfg.estimate(local_state),
                "bound_tokens": bound, "split_depth": depth,
            })
            return {
                e["index"]: reviewer.finish(
                    reviewer.base(e["relation"], batch_id), "unavailable", None, None,
                    {"error": code, "batch": batch_meta},
                )
                for e in entries
            }
        reviewer.stats.append(run_stats(
            run, "batch", batch_id, links=len(entries), latency_s=time.monotonic() - started,
            questions=len(questions), estimated_state_tokens=cfg.estimate(local_state),
            bound_tokens=bound, split_depth=depth,
        ))
        failure = run_failure(run)
        if failure is not None and failure[1].get("error") == "max_tokens_exceeded" and len(entries) > 1:
            middle = len(entries) // 2
            return {**self.execute(entries[:middle], depth + 1), **self.execute(entries[middle:], depth + 1)}
        modes = run.get("component_rollout_modes") or {}
        answers = (run.get("receipt") or {}).get("answers") or {}
        return {e["index"]: self._interpret(e, batch_id, batch_meta, failure, modes, answers) for e in entries}

    def _interpret(self, entry, batch_id, batch_meta, failure, modes, answers) -> dict[str, Any]:
        reviewer = self.reviewer
        relation, key = entry["relation"], entry["key"]
        base = reviewer.base(relation, batch_id)
        if failure is None and modes.get(f"jev-{entry['task']}-v1") == "shadow":
            failure = ("abstained", {"reason": "shadow_rollout"})
        if relation == "contradicts":
            if failure is None:
                kind_answer = _answered(answers, f"{key}__kind", "choice")
                kind = kind_answer.get("value") if kind_answer else None
                if kind not in CONTRADICTION_KINDS:
                    kind = None
                jointly = _probability(_answered(answers, f"{key}__jointly_true", "noul"))
                same = _probability(_answered(answers, f"{key}__same_subject", "noul"))
                jev_relation, reason = derive_contradiction_assessment(kind, jointly, same)
                detail = contradiction_detail(kind, reason, jointly, same)
            else:
                jev_relation = None
                detail = contradiction_detail(None, "unanswered", None, None)
        else:
            if failure is None:
                labels = [
                    (answer or {}).get("value")
                    for answer in (
                        _answered(answers, f"{key}__a_to_b", "choice"),
                        _answered(answers, f"{key}__b_to_a", "choice"),
                    )
                ]
                jev_relation = derive_claim_relation(
                    labels, entry["similarity"], entry["item"]["duplicate_threshold"]
                )
                detail = relation_detail(labels[0], labels[1], None)
            else:
                jev_relation = None
                detail = relation_detail(None, None, None)
        if failure is not None:
            return reviewer.finish(base, failure[0], None, None, {**detail, **failure[1], "batch": batch_meta})
        detail["batch"] = batch_meta
        agrees = link_agrees(relation, jev_relation)
        if agrees is None:
            return reviewer.finish(base, "abstained", None, jev_relation, detail)
        return reviewer.finish(base, "answered", "agrees" if agrees else "disagrees", jev_relation, detail)


def review_batched(reviewer: Any, run_id: str, items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Review ``items`` with as few hosted requests as the token budget allows."""
    batcher = _Batcher(reviewer, run_id)
    reviews: dict[int, dict] = {}
    entries = []
    for index, item in enumerate(items):
        prepared = batcher.prepare(index, item)
        if "review" in prepared:
            reviews[index] = prepared["review"]
        else:
            entries.append(prepared)
    batches, oversized = batcher.pack(entries)
    for entry in oversized:
        reviews[entry["index"]] = reviewer.finish(
            reviewer.base(entry["relation"], None), "unavailable", None, None,
            {"error": "input_limit"},
        )
    concurrency = min(batcher.config.concurrency, len(batches)) or 1
    if concurrency == 1:
        for batch in batches:
            reviews.update(batcher.execute(batch))
    else:
        runtime = reviewer.runtime
        original = runtime.client
        runtime.client = _UnlockedNetworkClient(original, batcher.lock)
        try:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                for result in pool.map(batcher.execute, batches):
                    reviews.update(result)
        finally:
            runtime.client = original
    return [reviews[index] for index in range(len(items))]
