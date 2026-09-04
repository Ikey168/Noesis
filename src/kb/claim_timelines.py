"""Immutable claim states, evolution lineage, matching, timelines, and diffs."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
from collections import deque
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from src.database.local_warehouse_seed import ensure_schema
from src.kb.quantitative import QuantitativeStore, _decimal

STATE_CONTRACT = "noesis-claim-state-v1"
LINEAGE_CONTRACT = "noesis-claim-lineage-v1"
MATCH_CONTRACT = "noesis-claim-successor-match-v1"
TIMELINE_CONTRACT = "noesis-claim-timeline-v1"
DIFF_CONTRACT = "noesis-claim-semantic-diff-v1"
READ_SCOPE = "knowledge:claim-timeline:read"
WRITE_SCOPE = "knowledge:claim-timeline:write"
RELATIONS = {"successor", "refinement", "reversal", "withdrawal", "branch"}
STANCES = {"supports", "opposes", "neutral", "mixed", "unknown"}
NEGATIONS = {"not", "no", "never", "denies", "denied", "false", "without"}
HEDGES = {"may", "might", "could", "likely", "possibly", "reportedly", "estimated"}
SYNONYMS = {
    "gdp": "output",
    "economic": "output",
    "grew": "increase",
    "grown": "increase",
    "increased": "increase",
    "rose": "increase",
    "declined": "decrease",
    "fell": "decrease",
    "approximately": "about",
    "percent": "%",
}

_DDL = """
CREATE TABLE IF NOT EXISTS claim_timeline_states (
  state_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, claim_id TEXT NOT NULL,
  revision BIGINT NOT NULL, predecessor_state_id TEXT, wording TEXT NOT NULL,
  stance TEXT NOT NULL, certainty DOUBLE NOT NULL, epistemic_status TEXT NOT NULL,
  attribution_json TEXT NOT NULL, quantities_json TEXT NOT NULL, scope_json TEXT NOT NULL,
  interpretations_json TEXT NOT NULL, evidence_json TEXT NOT NULL, source_id TEXT NOT NULL,
  source_revision_id TEXT NOT NULL, source_retracted BOOLEAN NOT NULL,
  generation BIGINT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(namespace,claim_id,revision)
);
CREATE TABLE IF NOT EXISTS claim_timeline_current (
  namespace TEXT NOT NULL, claim_id TEXT NOT NULL, state_id TEXT NOT NULL,
  revision BIGINT NOT NULL, PRIMARY KEY(namespace,claim_id)
);
CREATE TABLE IF NOT EXISTS claim_lineage_edges (
  edge_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, predecessor_claim_id TEXT NOT NULL,
  successor_claim_id TEXT NOT NULL, relation TEXT NOT NULL, confidence DOUBLE NOT NULL,
  evidence_json TEXT NOT NULL, explanation_json TEXT NOT NULL, method_json TEXT NOT NULL,
  generation BIGINT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(namespace,predecessor_claim_id,successor_claim_id,relation)
);
CREATE TABLE IF NOT EXISTS claim_timeline_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claim_timeline_observed
  ON claim_timeline_states(namespace,observed_at_ms,generation);
CREATE INDEX IF NOT EXISTS idx_claim_lineage_predecessor
  ON claim_lineage_edges(namespace,predecessor_claim_id,successor_claim_id);
"""


class ClaimTimelineError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise ClaimTimelineError("unauthorized", f"missing required scope {required}")


def _fold_tokens(text: str) -> set[str]:
    return {
        SYNONYMS.get(token, token)
        for token in re.findall(r"[\w%]+", text.casefold())
        if len(token) > 1
    }


def _numbers(text: str) -> list[str]:
    return re.findall(r"[-+]?\d+(?:[.,]\d+)?", text.replace(",", ""))


def _cursor(payload: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(_canonical(payload).encode()).decode().rstrip("=")


def _uncursor(value: str) -> dict[str, Any]:
    try:
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except Exception as exc:
        raise ClaimTimelineError(
            "invalid_cursor", "claim timeline cursor is malformed"
        ) from exc


class ClaimTimelineStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_schema(conn)
            conn.execute(_DDL)
            QuantitativeStore(conn)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        audit_id = (
            "claim-timeline-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO claim_timeline_audit VALUES (?,?,?,?,?,?,?)",
            [
                audit_id,
                namespace,
                operation,
                object_id,
                principal_id,
                _canonical(detail),
                now,
            ],
        )

    def _claim(self, claim_id: str) -> tuple[str, str]:
        row = self.conn.execute(
            "SELECT claim_text,document_id FROM argument_claims WHERE claim_id=?",
            [claim_id],
        ).fetchone()
        if not row:
            raise ClaimTimelineError(
                "claim_not_found", "canonical argument claim does not exist"
            )
        return str(row[0]), str(row[1])

    def _normalize_quantities(
        self, namespace: str, quantities: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        store = QuantitativeStore(self.conn, initialize=False)
        result = []
        for item in quantities:
            value = dict(item)
            unit = store._unit(str(value.get("unit") or "count"), namespace)
            number = _decimal(value.get("value"))
            normalized = (number + Decimal(unit["offset"])) * Decimal(unit["factor"])
            value.update(
                {
                    "value": str(number),
                    "unit_id": unit["unit_id"],
                    "dimension": unit["dimension"],
                    "normalized_value": str(normalized),
                    "normalized_unit_factor": unit["factor"],
                }
            )
            result.append(value)
        return result

    def capture_state(
        self,
        namespace: str,
        claim_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        wording: str | None = None,
        stance: str = "unknown",
        certainty: float = 0.5,
        epistemic_status: str = "unassessed",
        attribution: Mapping[str, Any] | None = None,
        quantities: Sequence[Mapping[str, Any]] = (),
        scope: Mapping[str, Any] | None = None,
        interpretations: Sequence[Mapping[str, Any]] = (),
        evidence: Sequence[Mapping[str, Any]] = (),
        source_id: str,
        source_revision_id: str,
        source_retracted: bool = False,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        canonical_wording, document_id = self._claim(claim_id)
        certainty_value = float(certainty)
        if (
            stance not in STANCES
            or not math.isfinite(certainty_value)
            or not 0 <= certainty_value <= 1
        ):
            raise ClaimTimelineError(
                "invalid_state", "supported stance and certainty 0..1 are required"
            )
        evidence_values = [dict(item) for item in evidence]
        if not evidence_values or any(
            not (item.get("citation") or item.get("document_revision_id"))
            for item in evidence_values
        ):
            raise ClaimTimelineError(
                "citation_required", "every claim state needs citation-closed evidence"
            )
        current = self.conn.execute(
            "SELECT state_id,revision FROM claim_timeline_current WHERE namespace=? AND claim_id=?",
            [namespace, claim_id],
        ).fetchone()
        revision = int(current[1]) + 1 if current else 1
        now = self.now()
        stable = {
            "namespace": namespace,
            "claim_id": claim_id,
            "revision": revision,
            "predecessor_state_id": current[0] if current else None,
            "wording": str(wording if wording is not None else canonical_wording),
            "stance": stance,
            "certainty": certainty_value,
            "epistemic_status": epistemic_status,
            "attribution": dict(attribution or {}),
            "quantities": self._normalize_quantities(namespace, quantities),
            "scope": dict(scope or {}),
            "interpretations": [dict(item) for item in interpretations],
            "evidence": evidence_values,
            "source_id": source_id,
            "source_revision_id": source_revision_id,
            "source_retracted": bool(source_retracted),
            "document_id": document_id,
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-claim-timeline", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"evolution": "link-dont-merge-v1"}),
            "provenance": dict(provenance or {}),
        }
        semantic = {
            key: value
            for key, value in stable.items()
            if key not in {"revision", "predecessor_state_id", "observed_at_ms"}
        }
        prior_same = self.conn.execute(
            "SELECT state_id FROM claim_timeline_states WHERE namespace=? AND claim_id=? AND input_hash=?",
            [namespace, claim_id, _digest(semantic)],
        ).fetchone()
        if prior_same:
            return {
                **self.state(namespace, prior_same[0], scopes={READ_SCOPE}),
                "idempotent": True,
            }
        input_hash = _digest(semantic)
        state_id = "claim-state:" + _digest([claim_id, revision, input_hash])[:24]
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO claim_timeline_states VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    state_id,
                    namespace,
                    claim_id,
                    revision,
                    stable["predecessor_state_id"],
                    stable["wording"],
                    stance,
                    certainty_value,
                    epistemic_status,
                    _canonical(stable["attribution"]),
                    _canonical(stable["quantities"]),
                    _canonical(stable["scope"]),
                    _canonical(stable["interpretations"]),
                    _canonical(evidence_values),
                    source_id,
                    source_revision_id,
                    bool(source_retracted),
                    generation,
                    valid_from_ms,
                    valid_to_ms,
                    stable["observed_at_ms"],
                    _canonical(stable["producer"]),
                    _canonical(stable["policy"]),
                    _canonical(stable["provenance"]),
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO claim_timeline_current VALUES (?,?,?,?)",
                [namespace, claim_id, state_id, revision],
            )
            self._audit(
                namespace,
                "capture-state",
                state_id,
                principal_id,
                {"claim_id": claim_id, "revision": revision},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.state(namespace, state_id, scopes={READ_SCOPE})

    def state(
        self, namespace: str, state_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT claim_id,revision,predecessor_state_id,wording,stance,certainty,epistemic_status,attribution_json,quantities_json,scope_json,interpretations_json,evidence_json,source_id,source_revision_id,source_retracted,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,provenance_json,principal_id,input_hash,created_at_ms FROM claim_timeline_states WHERE namespace=? AND state_id=?",
            [namespace, state_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": STATE_CONTRACT,
            "state_id": state_id,
            "namespace": namespace,
            "claim_id": row[0],
            "revision": int(row[1]),
            "predecessor_state_id": row[2],
            "wording": row[3],
            "stance": row[4],
            "certainty": float(row[5]),
            "epistemic_status": row[6],
            "attribution": _load(row[7], {}),
            "quantities": _load(row[8], []),
            "scope": _load(row[9], {}),
            "interpretations": _load(row[10], []),
            "evidence": _load(row[11], []),
            "source_id": row[12],
            "source_revision_id": row[13],
            "source_retracted": bool(row[14]),
            "evidence_status": "retracted-source" if row[14] else "active",
            "generation": int(row[15]),
            "valid_from_ms": row[16],
            "valid_to_ms": row[17],
            "observed_at_ms": int(row[18]),
            "producer": _load(row[19], {}),
            "policy": _load(row[20], {}),
            "provenance": _load(row[21], {}),
            "principal_id": row[22],
            "input_hash": row[23],
            "created_at_ms": int(row[24]),
        }

    def latest(
        self,
        namespace: str,
        claim_id: str,
        *,
        scopes: set[str],
        as_of_ms: int | None = None,
        generation: int | None = None,
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT state_id FROM claim_timeline_states WHERE namespace=? AND claim_id=? AND (? IS NULL OR observed_at_ms<=?) AND (? IS NULL OR generation<=?) ORDER BY revision DESC LIMIT 1",
            [namespace, claim_id, as_of_ms, as_of_ms, generation, generation],
        ).fetchone()
        return self.state(namespace, row[0], scopes=scopes) if row else None

    def _has_path(self, namespace: str, start: str, target: str) -> bool:
        queue, seen = deque([start]), {start}
        while queue:
            current = queue.popleft()
            rows = self.conn.execute(
                "SELECT successor_claim_id FROM claim_lineage_edges WHERE namespace=? AND predecessor_claim_id=?",
                [namespace, current],
            ).fetchall()
            for (next_id,) in rows:
                if next_id == target:
                    return True
                if next_id not in seen:
                    seen.add(next_id)
                    queue.append(next_id)
        return False

    def link(
        self,
        namespace: str,
        predecessor_claim_id: str,
        successor_claim_id: str,
        relation: str,
        *,
        confidence: float,
        evidence: Sequence[Mapping[str, Any]],
        explanation: Mapping[str, Any],
        method: Mapping[str, Any],
        principal_id: str,
        scopes: set[str],
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        self._claim(predecessor_claim_id)
        self._claim(successor_claim_id)
        confidence_value = float(confidence)
        evidence_values = [dict(item) for item in evidence]
        if (
            predecessor_claim_id == successor_claim_id
            or relation not in RELATIONS
            or not 0 <= confidence_value <= 1
            or not evidence_values
            or any(
                not (item.get("citation") or item.get("document_revision_id"))
                for item in evidence_values
            )
        ):
            raise ClaimTimelineError(
                "invalid_lineage",
                "distinct claims, supported relation, confidence, and evidence are required",
            )
        if self._has_path(namespace, successor_claim_id, predecessor_claim_id):
            raise ClaimTimelineError(
                "lineage_cycle", "claim evolution lineage cannot contain a cycle"
            )
        now = self.now()
        stable = {
            "namespace": namespace,
            "predecessor_claim_id": predecessor_claim_id,
            "successor_claim_id": successor_claim_id,
            "relation": relation,
            "confidence": confidence_value,
            "evidence": evidence_values,
            "explanation": dict(explanation),
            "method": dict(method),
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-claim-timeline", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"cycles": "reject-v1"}),
            "provenance": dict(provenance or {}),
        }
        input_hash = _digest(
            {key: value for key, value in stable.items() if key != "observed_at_ms"}
        )
        edge_id = (
            "claim-lineage:"
            + _digest([namespace, predecessor_claim_id, successor_claim_id, relation])[
                :24
            ]
        )
        existing = self.conn.execute(
            "SELECT input_hash FROM claim_lineage_edges WHERE edge_id=?", [edge_id]
        ).fetchone()
        if existing:
            if existing[0] != input_hash:
                raise ClaimTimelineError(
                    "lineage_conflict",
                    "lineage relation already exists with different evidence",
                )
            return {
                **self.edge(namespace, edge_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO claim_lineage_edges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    edge_id,
                    namespace,
                    predecessor_claim_id,
                    successor_claim_id,
                    relation,
                    confidence_value,
                    _canonical(evidence_values),
                    _canonical(explanation),
                    _canonical(method),
                    generation,
                    valid_from_ms,
                    valid_to_ms,
                    stable["observed_at_ms"],
                    _canonical(stable["producer"]),
                    _canonical(stable["policy"]),
                    _canonical(stable["provenance"]),
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self._audit(
                namespace, "link", edge_id, principal_id, {"relation": relation}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.edge(namespace, edge_id, scopes={READ_SCOPE})

    def edge(
        self, namespace: str, edge_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT predecessor_claim_id,successor_claim_id,relation,confidence,evidence_json,explanation_json,method_json,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,provenance_json,principal_id,input_hash,created_at_ms FROM claim_lineage_edges WHERE namespace=? AND edge_id=?",
            [namespace, edge_id],
        ).fetchone()
        if not row:
            return None
        evidence = _load(row[4], [])
        return {
            "contract": LINEAGE_CONTRACT,
            "edge_id": edge_id,
            "namespace": namespace,
            "predecessor_claim_id": row[0],
            "successor_claim_id": row[1],
            "relation": row[2],
            "confidence": float(row[3]),
            "evidence": evidence,
            "evidence_status": "retracted-only"
            if evidence and all(item.get("retracted") for item in evidence)
            else "active",
            "explanation": _load(row[5], {}),
            "method": _load(row[6], {}),
            "generation": int(row[7]),
            "valid_from_ms": row[8],
            "valid_to_ms": row[9],
            "observed_at_ms": int(row[10]),
            "producer": _load(row[11], {}),
            "policy": _load(row[12], {}),
            "provenance": _load(row[13], {}),
            "principal_id": row[14],
            "input_hash": row[15],
            "created_at_ms": int(row[16]),
        }

    def match_successors(
        self,
        namespace: str,
        claim_id: str,
        *,
        scopes: set[str],
        principal_id: str,
        candidate_claim_ids: Sequence[str] = (),
        threshold: float = 0.45,
        limit: int = 20,
        embedding_scores: Mapping[str, float] | None = None,
        embedding_pin: Mapping[str, Any] | None = None,
        persist: bool = False,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE if persist else READ_SCOPE)
        if cancel_requested:
            return {
                "contract": MATCH_CONTRACT,
                "claim_id": claim_id,
                "status": "cancelled",
                "matches": [],
            }
        source = self.latest(namespace, claim_id, scopes={READ_SCOPE})
        if not source:
            raise ClaimTimelineError(
                "state_not_found", "source claim state does not exist"
            )
        if embedding_scores is not None and (
            not embedding_pin
            or not {"name", "version", "revision"} <= embedding_pin.keys()
        ):
            raise ClaimTimelineError(
                "unpinned_model", "embedding scores require name, version, and revision"
            )
        candidates = list(candidate_claim_ids)
        if not candidates:
            candidates = [
                row[0]
                for row in self.conn.execute(
                    "SELECT claim_id FROM claim_timeline_current WHERE namespace=? AND claim_id<>? ORDER BY claim_id LIMIT ?",
                    [namespace, claim_id, min(max(limit * 10, 1), 500)],
                ).fetchall()
            ]
        matches = []
        left_tokens, left_numbers = (
            _fold_tokens(source["wording"]),
            _numbers(source["wording"]),
        )
        for candidate_id in candidates[:500]:
            candidate = self.latest(namespace, candidate_id, scopes={READ_SCOPE})
            if not candidate:
                continue
            right_tokens = _fold_tokens(candidate["wording"])
            lexical = (
                len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
                if left_tokens | right_tokens
                else 0
            )
            embedding = float((embedding_scores or {}).get(candidate_id, 0))
            score = max(lexical, embedding)
            negation_changed = bool(left_tokens & NEGATIONS) != bool(
                right_tokens & NEGATIONS
            )
            numeric_changed = left_numbers != _numbers(candidate["wording"])
            scope_changed = source["scope"] != candidate["scope"]
            if score < threshold:
                continue
            relation = (
                "reversal"
                if negation_changed
                else "refinement"
                if numeric_changed or scope_changed
                else "successor"
            )
            explanation = {
                "lexical_overlap": round(lexical, 6),
                "embedding_score": round(embedding, 6)
                if embedding_scores is not None
                else None,
                "negation_changed": negation_changed,
                "numeric_changed": numeric_changed,
                "scope_changed": scope_changed,
                "shared_terms": sorted(left_tokens & right_tokens),
                "hedging_changed": bool(left_tokens & HEDGES)
                != bool(right_tokens & HEDGES),
            }
            match = {
                "candidate_claim_id": candidate_id,
                "relation": relation,
                "score": round(score, 6),
                "explanation": explanation,
            }
            if persist:
                match["edge"] = self.link(
                    namespace,
                    claim_id,
                    candidate_id,
                    relation,
                    confidence=score,
                    evidence=candidate["evidence"],
                    explanation=explanation,
                    method={
                        "kind": "deterministic+optional-embedding",
                        "embedding_pin": dict(embedding_pin or {}),
                    },
                    principal_id=principal_id,
                    scopes={WRITE_SCOPE},
                )
            matches.append(match)
        matches.sort(key=lambda item: (-item["score"], item["candidate_claim_id"]))
        return {
            "contract": MATCH_CONTRACT,
            "claim_id": claim_id,
            "status": "completed",
            "method": {
                "lexical": "canonical-token-jaccard-v1",
                "embedding_pin": dict(embedding_pin or {})
                if embedding_scores is not None
                else None,
            },
            "threshold": threshold,
            "matches": matches[: min(max(limit, 1), 100)],
            "truncated": len(matches) > min(max(limit, 1), 100),
            "match_hash": _digest([claim_id, threshold, matches]),
        }

    def diff(
        self,
        namespace: str,
        left_claim_id: str,
        right_claim_id: str,
        *,
        scopes: set[str],
        left_revision: int | None = None,
        right_revision: int | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)

        def resolve(claim_id, revision):
            if revision is None:
                return self.latest(namespace, claim_id, scopes={READ_SCOPE})
            row = self.conn.execute(
                "SELECT state_id FROM claim_timeline_states WHERE namespace=? AND claim_id=? AND revision=?",
                [namespace, claim_id, revision],
            ).fetchone()
            return self.state(namespace, row[0], scopes={READ_SCOPE}) if row else None

        left, right = (
            resolve(left_claim_id, left_revision),
            resolve(right_claim_id, right_revision),
        )
        if not left or not right:
            raise ClaimTimelineError(
                "state_not_found", "one or both claim states are missing"
            )
        fields = [
            "wording",
            "stance",
            "certainty",
            "epistemic_status",
            "attribution",
            "scope",
            "interpretations",
            "source_retracted",
        ]
        changes = {
            field: {"before": left[field], "after": right[field]}
            for field in fields
            if left[field] != right[field]
        }
        if left["quantities"] != right["quantities"]:
            left_normalized = [
                (item.get("normalized_value"), item.get("dimension"))
                for item in left["quantities"]
            ]
            right_normalized = [
                (item.get("normalized_value"), item.get("dimension"))
                for item in right["quantities"]
            ]
            changes["quantities"] = {
                "before": left["quantities"],
                "after": right["quantities"],
                "equivalent_after_conversion": left_normalized == right_normalized,
            }
        return {
            "contract": DIFF_CONTRACT,
            "left_state_id": left["state_id"],
            "right_state_id": right["state_id"],
            "changes": changes,
            "material": bool(changes),
            "citation_closure": {"left": left["evidence"], "right": right["evidence"]},
            "diff_hash": _digest([left["state_id"], right["state_id"], changes]),
        }

    def timeline(
        self,
        namespace: str,
        claim_id: str,
        *,
        scopes: set[str],
        as_of_ms: int | None = None,
        generation: int | None = None,
        max_depth: int = 6,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        if not 0 <= max_depth <= 12:
            raise ClaimTimelineError("invalid_bound", "max_depth must be 0..12")
        filters = {
            "namespace": namespace,
            "claim_id": claim_id,
            "as_of_ms": as_of_ms,
            "generation": generation,
            "max_depth": max_depth,
        }
        offset = 0
        if cursor:
            decoded = _uncursor(cursor)
            if decoded.get("filters_hash") != _digest(filters):
                raise ClaimTimelineError(
                    "cursor_mismatch", "cursor belongs to different timeline filters"
                )
            offset = int(decoded.get("offset", 0))
        queue, depths, edge_ids = deque([claim_id]), {claim_id: 0}, set()
        while queue:
            current = queue.popleft()
            depth = depths[current]
            if depth >= max_depth:
                continue
            rows = self.conn.execute(
                "SELECT edge_id,predecessor_claim_id,successor_claim_id FROM claim_lineage_edges WHERE namespace=? AND (predecessor_claim_id=? OR successor_claim_id=?) AND (? IS NULL OR observed_at_ms<=?) AND (? IS NULL OR generation<=?) ORDER BY edge_id",
                [
                    namespace,
                    current,
                    current,
                    as_of_ms,
                    as_of_ms,
                    generation,
                    generation,
                ],
            ).fetchall()
            for edge_id, predecessor, successor in rows:
                edge_ids.add(edge_id)
                other = successor if predecessor == current else predecessor
                if other not in depths:
                    depths[other] = depth + 1
                    queue.append(other)
        states = [
            self.latest(
                namespace,
                item,
                scopes={READ_SCOPE},
                as_of_ms=as_of_ms,
                generation=generation,
            )
            for item in depths
        ]
        states = sorted(
            [item for item in states if item],
            key=lambda item: (
                item["observed_at_ms"],
                item["claim_id"],
                item["revision"],
            ),
        )
        edges = [
            self.edge(namespace, item, scopes={READ_SCOPE}) for item in sorted(edge_ids)
        ]
        page_limit = min(max(limit, 1), 200)
        page = states[offset : offset + page_limit]
        next_cursor = (
            _cursor({"filters_hash": _digest(filters), "offset": offset + page_limit})
            if offset + page_limit < len(states)
            else None
        )
        return {
            "contract": TIMELINE_CONTRACT,
            "namespace": namespace,
            "root_claim_id": claim_id,
            "as_of_ms": as_of_ms,
            "generation": generation,
            "items": page,
            "edges": edges,
            "next_cursor": next_cursor,
            "timeline_hash": _digest(
                [filters, [item["state_id"] for item in states], sorted(edge_ids)]
            ),
        }

    def compare_sources(
        self,
        namespace: str,
        source_ids: Sequence[str],
        *,
        scopes: set[str],
        limit: int = 50,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT state_id FROM claim_timeline_states WHERE namespace=? AND source_id IN (SELECT unnest(?)) QUALIFY row_number() OVER (PARTITION BY claim_id ORDER BY revision DESC)=1 ORDER BY source_id,claim_id LIMIT ?",
            [namespace, list(source_ids), min(max(limit, 1), 200)],
        ).fetchall()
        states = [self.state(namespace, row[0], scopes={READ_SCOPE}) for row in rows]
        comparisons = []
        for index, left in enumerate(states):
            for right in states[index + 1 :]:
                if left["source_id"] != right["source_id"]:
                    comparisons.append(
                        self.diff(
                            namespace,
                            left["claim_id"],
                            right["claim_id"],
                            scopes={READ_SCOPE},
                        )
                    )
        return {
            "namespace": namespace,
            "source_ids": sorted(set(source_ids)),
            "states": states,
            "comparisons": comparisons,
            "comparison_hash": _digest(
                [sorted(set(source_ids)), [item["state_id"] for item in states]]
            ),
        }

    def replay(
        self, namespace: str, claim_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        timeline = self.timeline(
            namespace, claim_id, scopes=scopes, max_depth=12, limit=200
        )
        replayed = _digest(
            [
                {"state_id": item["state_id"], "input_hash": item["input_hash"]}
                for item in timeline["items"]
            ]
            + [
                {"edge_id": item["edge_id"], "input_hash": item["input_hash"]}
                for item in timeline["edges"]
            ]
        )
        return {
            "claim_id": claim_id,
            "deterministic": True,
            "replay_hash": replayed,
            "state_count": len(timeline["items"]),
            "edge_count": len(timeline["edges"]),
            "citation_closed": all(item["evidence"] for item in timeline["items"])
            and all(item["evidence"] for item in timeline["edges"]),
        }
