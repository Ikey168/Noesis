"""Optional Jev review of claim links proposed by the local NLI pass.

:func:`src.kb.claim_links.run_claim_linking_pass` plans its links from the
pinned local NLI model, and can then hand each proposed ``supports``,
``duplicate`` or ``contradicts`` link to a :class:`JevClaimLinkReviewer`
before anything is written. The reviewer asks Jev (TypeSafe) through the
source-bound adapters in :mod:`src.kb.jev_evidence_adapters`:

* ``contradicts`` → :func:`suggest_contradiction_kind` (is this a real
  conflict, or a temporal/scope/attribution difference?);
* ``supports`` / ``duplicate`` → :func:`suggest_claim_relation` (both
  directions over the exact claim spans).

Modes:

``shadow``
    Every proposed link is written exactly as the local pass decided; the
    Jev assessment is recorded beside it for evaluation.
``gate``
    A link is withheld when Jev answers and disagrees, except a ``supports``
    link Jev reads in the opposite direction, which is written reversed.
    Jev never adds a link or changes its relation, and a missing span, an
    unavailable provider or an abstention keeps the local link: a failure is
    never converted into "no relation". A link listed in
    ``claim_link_jev_overrides`` (see :mod:`src.kb.jev_verdicts`) is written
    as planned whatever Jev answers.

Every review (agreeing, disagreeing, unavailable, unresolved) is stored in
``claim_link_jev_assessments`` with the linking ``run_id``, so
:func:`src.kb.claim_links.delete_run` reverts it together with the links.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from src.integrations.decisions import DecisionError
from src.kb.decision_runtime import DecisionRuntimeError
from src.kb.jev_evidence_adapters import (
    suggest_claim_relation,
    suggest_contradiction_kind,
    suggest_supersession,
)

#: Failures that leave a link unreviewed (and therefore written) rather than
#: aborting the linking pass. Programming errors still propagate.
_REVIEW_FAILURES = (DecisionRuntimeError, DecisionError, ValueError, OSError, TimeoutError)

MODES = ("shadow", "gate")
#: ``supersedes`` covers every same-source temporal transition derived from a
#: support or contradiction (supersedes, corrects, retracts); it is reviewed
#: with its own rubric ("does the later claim update the earlier one?").
REVIEWED_RELATIONS = ("supports", "duplicate", "contradicts", "supersedes")

ASSESSMENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_link_jev_assessments (
    run_id          TEXT NOT NULL,
    claim_a         TEXT NOT NULL,
    claim_b         TEXT NOT NULL,
    relation        TEXT NOT NULL,
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL,
    verdict         TEXT,
    action          TEXT NOT NULL,
    jev_relation    TEXT,
    decision_run_id TEXT,
    detail_json     TEXT NOT NULL,
    created_at      BIGINT NOT NULL,
    PRIMARY KEY (run_id, claim_a, claim_b, relation)
)
"""

#: ``resolve_claim(claim_id, claim_text)`` → ``{"source_ref", "locator"}`` or None
ClaimResolver = Callable[[str, str], Mapping[str, Any] | None]


#: Answers persisted as each review chunk returns, keyed by reviewer identity,
#: claim pair and claim texts: a restarted (or repeated) linking pass reuses
#: them instead of paying for the same question again.
REVIEW_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_link_review_cache (
    cache_key       TEXT PRIMARY KEY,
    relation        TEXT NOT NULL,
    premise_id      TEXT NOT NULL,
    hypothesis_id   TEXT NOT NULL,
    review_json     TEXT NOT NULL,
    decision_run_id TEXT,
    linking_run_id  TEXT NOT NULL,
    created_at      BIGINT NOT NULL
)
"""


def ensure_assessment_schema(conn) -> None:
    conn.execute(ASSESSMENTS_SCHEMA)
    conn.execute(REVIEW_CACHE_SCHEMA)


def document_revision_resolver(conn) -> ClaimResolver:
    """Locate a claim's exact span in its document's current committed revision.

    In order: the character offsets stored at extraction (when they belong
    to the current revision and are in range), then
    :func:`src.argument_mining.spans.locate_span` over the revision text
    (exact, first of several occurrences, whitespace- or quote-insensitive).
    The returned ``match`` records which rule found the span. Returns None
    (the link stays unreviewed, never dropped) only when the revision tables
    are absent, there is no committed current revision, or the text does not
    occur in it at all. The decision runtime re-authorizes and rechecks the
    revision before any remote request.
    """
    from src.argument_mining.spans import locate_span

    def resolve(claim_id: str, claim_text: str) -> Mapping[str, Any] | None:
        if not claim_text:
            return None
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('document_current_revisions', 'document_revision_records')"
            ).fetchall()
        }
        if len(tables) != 2:
            return None
        columns = {row[0] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'argument_claims'"
        ).fetchall()}
        stored = ("c.span_revision_id, c.span_start, c.span_end" if
                  {"span_revision_id", "span_start", "span_end"} <= columns else "NULL, NULL, NULL")
        row = conn.execute(
            f"""
            SELECT r.document_id, r.revision_id, r.payload_json, {stored}
            FROM argument_claims c
            JOIN document_current_revisions cur ON cur.document_id = c.document_id
            JOIN document_revision_records r
              ON r.document_id = cur.document_id AND r.revision_id = cur.revision_id
            WHERE c.claim_id = ? AND r.committed_watermark IS NOT NULL
            """,
            [claim_id],
        ).fetchone()
        if row is None:
            return None
        try:
            content = json.loads(row[2]).get("content")
        except (TypeError, ValueError):
            return None
        if not isinstance(content, str):
            return None
        ref = {"document_id": row[0], "revision_id": row[1]}
        span_revision, start, end = row[3], row[4], row[5]
        if (span_revision == row[1] and type(start) is int and type(end) is int
                and 0 <= start < end <= len(content) and content[start:end].strip()):
            return {"source_ref": ref, "locator": {"start": start, "end": end},
                    "match": "stored_offsets"}
        span = locate_span(content, claim_text)
        if span is None:
            return None
        return {"source_ref": ref, "locator": {"start": span["start"], "end": span["end"]},
                "match": span["match"]}

    return resolve


def _decision_run_id(run_id: str, relation: str, claim_a: str, claim_b: str) -> str:
    digest = hashlib.sha256(f"{relation}\0{claim_a}\0{claim_b}".encode()).hexdigest()[:24]
    return f"{run_id[:200]}:jev:{digest}"


def link_agrees(relation: str, jev_relation: str | None) -> bool | None:
    """Whether a Jev relation confirms a proposed local link (None = no answer)."""
    if jev_relation is None:
        return None
    if relation == "contradicts":
        return jev_relation == "conflict"
    if relation == "supersedes":
        return jev_relation == "supersedes"
    if relation == "duplicate":
        return jev_relation == "duplicate"
    return jev_relation in {"a_supports_b", "duplicate"}  # supports: a entails b


def run_failure(run: Mapping[str, Any] | None) -> tuple[str, dict[str, Any]] | None:
    """``(status, detail)`` when a hosted run produced no usable answers.

    A provider that never answered (rate limits, overload, schema drift, a
    failed publication) is ``unavailable``, never an abstention; a ``shadow``
    rollout ran but withholds its answers, so it abstains.
    """
    if run is None:
        return None
    if run.get("rollout_mode") == "shadow":
        return "abstained", {"reason": "shadow_rollout"}
    if run.get("status") == "completed":
        return None
    receipt = run.get("receipt") or {}
    code = (
        run.get("failure_code")
        or (receipt.get("execution") or {}).get("error_code")
        or run.get("status")
        or "unavailable"
    )
    return "unavailable", {"error": str(code)}


def contradiction_detail(kind, reason, jointly, same, decided_by=None) -> dict[str, Any]:
    return {
        "kind": kind,
        "reason": reason,
        "jointly_true_probability": jointly,
        "same_subject_probability": same,
        "decided_by": decided_by,
    }


def supersession_detail(kind, reason, same, decided_by=None) -> dict[str, Any]:
    return {"kind": kind, "reason": reason, "same_subject_probability": same,
            "decided_by": decided_by}


def empty_detail(relation: str, reason: str | None) -> dict[str, Any]:
    """The detail shape a relation records when Jev gave no answers."""
    if relation == "contradicts":
        return contradiction_detail(None, reason, None, None, "unanswered")
    if relation == "supersedes":
        return supersession_detail(None, reason, None, "unanswered")
    return relation_detail(None, None, reason)


def span_matches(spans: list[Mapping[str, Any] | None]) -> list[str] | None:
    """How each claim's span was found, when the resolver reports it."""
    matches = [span.get("match") for span in spans if span is not None]
    return matches if any(matches) else None


def relation_detail(a_to_b, b_to_a, reason) -> dict[str, Any]:
    return {"a_to_b": a_to_b, "b_to_a": b_to_a, "reason": reason}


class JevClaimLinkReviewer:
    """Review proposed claim links with Jev before the linking pass writes them.

    ``batch`` (a :class:`src.kb.claim_link_batch.BatchConfig`) packs many
    links into each TypeSafe request; the default ``None`` reviews one link
    per request, exactly as before. Both paths apply the same verdict rules
    and record the same assessment shape. ``stats`` lists every hosted
    request made (links, questions, estimated and reported tokens,
    reservation, latency) for evaluation.
    """

    def __init__(
        self,
        runtime: Any,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        policy: Mapping[str, Any],
        allow_remote: bool,
        resolve_claim: ClaimResolver,
        mode: str = "shadow",
        max_cost_usd_micros: int = 1000,
        deadline_s: float = 30,
        batch: Any = None,
        tie_break: str = "abstain",
        same_subject_threshold: float = 0.5,
        same_subject_gate: float | None = None,
        input_price_usd_micros_per_million_tokens: int | None = 42_000,
    ) -> None:
        """``tie_break`` settles a Choice label that disagrees with its Noul
        probabilities (``abstain``, ``label`` or ``probabilities``);
        ``same_subject_gate`` rejects confirmed conflicts whose same-subject
        probability is below it. Both are recorded in every assessment.
        ``input_price_usd_micros_per_million_tokens`` settles each per-link
        reservation to its reported usage (None keeps the full reservation).
        """
        from src.kb.jev_evidence_adapters import TIE_BREAKS

        if tie_break not in TIE_BREAKS:
            raise ValueError(f"tie_break must be one of {TIE_BREAKS}")
        for name, value in (("same_subject_threshold", same_subject_threshold),
                            ("same_subject_gate", same_subject_gate)):
            if value is not None and (type(value) not in {int, float} or not 0 < value < 1):
                raise ValueError(f"{name} must be a probability in (0, 1)")
        if input_price_usd_micros_per_million_tokens is not None and (
            type(input_price_usd_micros_per_million_tokens) is not int
            or input_price_usd_micros_per_million_tokens < 1
        ):
            raise ValueError("input price must be a positive integer")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if not callable(resolve_claim):
            raise TypeError("a claim span resolver is required")
        if batch is not None:
            from src.kb.claim_link_batch import BatchConfig

            if not isinstance(batch, BatchConfig):
                raise TypeError("batch must be a BatchConfig")
            if batch.concurrency > int(dict(policy).get("max_concurrent", 1)):
                raise ValueError("batch concurrency exceeds the policy's max_concurrent")
        self.runtime = runtime
        self.namespace = namespace
        self.principal_id = principal_id
        self.scopes = set(scopes)
        self.policy = dict(policy)
        self.allow_remote = allow_remote
        self.resolve_claim = resolve_claim
        self.mode = mode
        self.max_cost_usd_micros = max_cost_usd_micros
        self.deadline_s = deadline_s
        self.batch = batch
        self.tie_break = tie_break
        self.same_subject_threshold = float(same_subject_threshold)
        self.same_subject_gate = same_subject_gate
        self.input_price = input_price_usd_micros_per_million_tokens
        self.stats: list[dict[str, Any]] = []

    @property
    def rules(self) -> dict[str, Any]:
        """Decision rules applied to Jev's answers (part of every cache key)."""
        return {"tie_break": self.tie_break,
                "same_subject_threshold": self.same_subject_threshold,
                "same_subject_gate": self.same_subject_gate}

    def cache_identity(self) -> dict[str, Any]:
        """What must match for a stored review to be reused for a question."""
        return {
            "model": self.policy.get("model"),
            "rubric_id": self.policy.get("rubric_id"),
            "policy_id": self.policy.get("policy_id"),
            "batched_relations": sorted(self.batch.relations) if self.batch else [],
            "rules": self.rules,
        }

    def settle_cost(self, usage: Mapping[str, Any]) -> int | None:
        tokens = usage.get("input_tokens")
        if self.input_price is None or type(tokens) is not int:
            return None
        return max(1, -(-tokens * self.input_price // 1_000_000))

    @property
    def method(self) -> str:
        suffix = "" if self.batch is None else ":batched"
        return f"jev:{self.policy.get('model', 'unknown')}:{self.mode}{suffix}"

    def review_all(self, run_id: str, items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """Review many proposed links, returning one review per item in order.

        Each item has ``relation``, ``premise``/``hypothesis`` (``(claim_id,
        claim_text)`` in stored direction), ``similarity`` and
        ``duplicate_threshold``.
        """
        for item in items:
            if item["relation"] not in REVIEWED_RELATIONS:
                raise ValueError(f"unreviewable relation {item['relation']!r}")
        if self.batch is None:
            return [
                self.review(
                    run_id, item["relation"], item["premise"], item["hypothesis"],
                    similarity=item["similarity"],
                    duplicate_threshold=item["duplicate_threshold"],
                )
                for item in items
            ]
        from src.kb.claim_link_batch import review_batched

        return review_batched(self, run_id, items)

    def review(
        self,
        run_id: str,
        relation: str,
        premise: tuple[str, str],
        hypothesis: tuple[str, str],
        *,
        similarity: float,
        duplicate_threshold: float,
    ) -> dict[str, Any]:
        """Assess one proposed link in its own hosted request."""
        if relation not in REVIEWED_RELATIONS:
            raise ValueError(f"unreviewable relation {relation!r}")
        decision_run_id = _decision_run_id(run_id, relation, premise[0], hypothesis[0])
        base = self.base(relation, decision_run_id)
        spans = [self.resolve_claim(claim_id, text) for claim_id, text in (premise, hypothesis)]
        if any(span is None for span in spans):
            return self.finish(base, "unresolved", None, None, {"reason": "no_exact_claim_span"})
        common = {
            "principal_id": self.principal_id,
            "scopes": self.scopes,
            "allow_remote": self.allow_remote,
            "policy": self.policy,
            "max_cost_usd_micros": self.max_cost_usd_micros,
            "deadline_s": self.deadline_s,
            "settle_cost": self.settle_cost if self.input_price is not None else None,
        }
        matched = span_matches(spans)
        started = time.monotonic()
        try:
            if relation == "contradicts":
                result = suggest_contradiction_kind(
                    self.runtime, self.namespace, decision_run_id, *spans,
                    local_relation="contradicts", tie_break=self.tie_break,
                    same_subject_threshold=self.same_subject_threshold,
                    same_subject_gate=self.same_subject_gate, **common,
                )
            elif relation == "supersedes":
                result = suggest_supersession(
                    self.runtime, self.namespace, decision_run_id, *spans,
                    same_subject_threshold=self.same_subject_threshold,
                    tie_break=self.tie_break, **common,
                )
            else:
                result = suggest_claim_relation(
                    self.runtime, self.namespace, decision_run_id, *spans,
                    similarity=float(min(max(similarity, 0.0), 1.0)),
                    duplicate_threshold=duplicate_threshold, **common,
                )
        except _REVIEW_FAILURES as exc:  # provider, policy, budget or source failures
            code = getattr(exc, "code", type(exc).__name__)
            return self.finish(base, "unavailable", None, None, {"error": str(code)})
        run = result.get("decision_run")
        if run is not None:
            self.stats.append(run_stats(run, "link", decision_run_id, links=1,
                                        latency_s=time.monotonic() - started))
        if relation == "contradicts":
            jev_relation = result.get("assessment")
            detail = contradiction_detail(
                result.get("kind"), result.get("reason"),
                result.get("jointly_true_probability"), result.get("same_subject_probability"),
                result.get("decided_by"),
            )
        elif relation == "supersedes":
            jev_relation = result.get("assessment")
            detail = supersession_detail(
                result.get("kind"), result.get("reason"),
                result.get("same_subject_probability"), result.get("decided_by"),
            )
        else:
            jev_relation = result.get("relation")
            detail = relation_detail(result.get("a_to_b"), result.get("b_to_a"), result.get("reason"))
        if matched:
            detail["spans"] = matched
        failure = run_failure(run)
        if failure is not None:
            return self.finish(base, failure[0], None, None, {**detail, **failure[1]})
        agrees = link_agrees(relation, jev_relation)
        if agrees is None:
            return self.finish(base, "abstained", None, jev_relation, detail)
        return self.finish(base, "answered", "agrees" if agrees else "disagrees", jev_relation, detail)

    def base(self, relation: str, decision_run_id: str) -> dict[str, Any]:
        return {"relation": relation, "mode": self.mode, "decision_run_id": decision_run_id}

    def finish(self, base, status, verdict, jev_relation, detail) -> dict[str, Any]:
        gate = self.mode == "gate" and verdict == "disagrees"
        # A support Jev reads in the opposite direction is reversed, not
        # dropped: the evidence relation stands, its direction was wrong.
        reverse = gate and base["relation"] == "supports" and jev_relation == "b_supports_a"
        withhold = gate and not reverse
        return {
            **base,
            "status": status,
            "verdict": verdict,
            "jev_relation": jev_relation,
            "action": "reversed" if reverse else "withheld" if withhold else "written",
            "write": not withhold,
            "reverse": reverse,
            "detail": detail,
        }

    _finish = finish


def run_stats(run: Mapping[str, Any], kind: str, decision_run_id: str, *, links: int,
              latency_s: float, **extra: Any) -> dict[str, Any]:
    receipt = run.get("receipt") or {}
    usage = receipt.get("usage") or {}
    return {
        "kind": kind,
        "decision_run_id": decision_run_id,
        "links": links,
        "status": run.get("status"),
        "error": (run_failure(run) or (None, {}))[1].get("error"),
        "attempts": run.get("attempts_reserved"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "reserved_usd_micros": run.get("reserved_cost_usd_micros"),
        "settled_usd_micros": run.get("settled_cost_usd_micros"),
        "budget_overrun": run.get("budget_overrun"),
        "latency_s": round(latency_s, 4),
        **extra,
    }


def record_assessment(
    conn, run_id: str, claim_a: str, claim_b: str, review: Mapping[str, Any]
) -> None:
    conn.execute(
        """
        INSERT INTO claim_link_jev_assessments
            (run_id, claim_a, claim_b, relation, mode, status, verdict, action,
             jev_relation, decision_run_id, detail_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (run_id, claim_a, claim_b, relation) DO UPDATE SET
            mode = excluded.mode, status = excluded.status,
            verdict = excluded.verdict, action = excluded.action,
            jev_relation = excluded.jev_relation,
            decision_run_id = excluded.decision_run_id,
            detail_json = excluded.detail_json, created_at = excluded.created_at
        """,
        [
            run_id, claim_a, claim_b, review["relation"], review["mode"],
            review["status"], review["verdict"], review["action"],
            review["jev_relation"], review["decision_run_id"],
            json.dumps(review["detail"], sort_keys=True), int(time.time() * 1000),
        ],
    )


def build_jev_reviewer(
    conn,
    namespace: str,
    *,
    mode: str,
    policy: Mapping[str, Any],
    principal_id: str = "local-operator",
    scopes: set[str] | None = None,
    batch: Any = None,
    client: Any = None,
    **rules: Any,
) -> JevClaimLinkReviewer:
    """A reviewer over the real TypeSafe client for a local operator run.

    The policy must pin the model, rubric, policy, budget and
    ``credential_ref`` (``"typesafe"`` reads ``TYPESAFE_API_KEY`` at
    execution time) and set ``hosted_allowed``; the decision runtime rejects
    anything less. Calling this is the explicit remote-processing approval.
    ``batch`` (a :class:`src.kb.claim_link_batch.BatchConfig`) opts in to
    many links per request; ``None`` keeps one request per link.
    """
    import os

    from src.integrations.typesafe import TypeSafeClient
    from src.kb.decision_runtime import DecisionRuntime

    runtime = DecisionRuntime(
        conn,
        client=client or TypeSafeClient(),
        credential_resolver=lambda reference: (
            os.environ.get("TYPESAFE_API_KEY") if reference == "typesafe" else None
        ),
    )
    return JevClaimLinkReviewer(
        runtime,
        namespace,
        principal_id=principal_id,
        scopes=set(scopes) if scopes is not None else {"operator"},
        policy=policy,
        allow_remote=True,
        resolve_claim=document_revision_resolver(conn),
        mode=mode,
        batch=batch,
        **rules,
    )
