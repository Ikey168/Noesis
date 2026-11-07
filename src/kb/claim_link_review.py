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
    A link is withheld when Jev answers and disagrees. Jev can only remove a
    proposed link, never add one or change its relation, and a missing span,
    an unavailable provider or an abstention keeps the local link: a failure
    is never converted into "no relation".

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
)

#: Failures that leave a link unreviewed (and therefore written) rather than
#: aborting the linking pass. Programming errors still propagate.
_REVIEW_FAILURES = (DecisionRuntimeError, DecisionError, ValueError, OSError, TimeoutError)

MODES = ("shadow", "gate")
REVIEWED_RELATIONS = ("supports", "duplicate", "contradicts")

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


def ensure_assessment_schema(conn) -> None:
    conn.execute(ASSESSMENTS_SCHEMA)


def document_revision_resolver(conn) -> ClaimResolver:
    """Locate a claim's exact span in its document's current committed revision.

    Returns None (the link stays unreviewed, never dropped) when the revision
    tables are absent, the document has no committed current revision, or the
    claim text does not occur exactly once in it. The decision runtime
    re-authorizes and rechecks the revision before any remote request.
    """

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
        row = conn.execute(
            """
            SELECT r.document_id, r.revision_id, r.payload_json
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
        start = content.find(claim_text)
        if start < 0 or content.find(claim_text, start + 1) >= 0:
            return None  # absent or ambiguous: no exact span
        return {
            "source_ref": {"document_id": row[0], "revision_id": row[1]},
            "locator": {"start": start, "end": start + len(claim_text)},
        }

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


def contradiction_detail(kind, reason, jointly, same) -> dict[str, Any]:
    return {
        "kind": kind,
        "reason": reason,
        "jointly_true_probability": jointly,
        "same_subject_probability": same,
    }


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
    ) -> None:
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
        self.stats: list[dict[str, Any]] = []

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
        }
        started = time.monotonic()
        try:
            if relation == "contradicts":
                result = suggest_contradiction_kind(
                    self.runtime, self.namespace, decision_run_id, *spans,
                    local_relation="contradicts", **common,
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
            )
        else:
            jev_relation = result.get("relation")
            detail = relation_detail(result.get("a_to_b"), result.get("b_to_a"), result.get("reason"))
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
        withhold = self.mode == "gate" and verdict == "disagrees"
        return {
            **base,
            "status": status,
            "verdict": verdict,
            "jev_relation": jev_relation,
            "action": "withheld" if withhold else "written",
            "write": not withhold,
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
    )
