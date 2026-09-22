"""Versioned statement status and evidence-calibrated epistemic assessments."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

TAXONOMY_CONTRACT = "noesis-epistemic-taxonomy-v1"
ASSESSMENT_CONTRACT = "noesis-epistemic-assessment-v1"
EXPLANATION_CONTRACT = "noesis-epistemic-explanation-v1"
READ_SCOPE = "knowledge:epistemic:read"
WRITE_SCOPE = "knowledge:epistemic:write"
REVIEW_SCOPE = "knowledge:epistemic:review"
STATUSES = (
    "fact",
    "report",
    "allegation",
    "estimate",
    "forecast",
    "opinion",
    "hypothesis",
    "normative",
    "unknown",
)
ASSESSMENT_STATES = ("supported", "contested", "insufficient")

_DEFAULT_DEFINITIONS = {
    "fact": "A directly asserted, falsifiable descriptive statement.",
    "report": "A statement attributed to another speaker or publication.",
    "allegation": "A contested accusation or unverified assertion about conduct.",
    "estimate": "A present or past quantity expressed with measurement uncertainty.",
    "forecast": "A prediction about a future state or event.",
    "opinion": "A subjective evaluation, preference, or interpretation.",
    "hypothesis": "A tentative explanatory proposition requiring investigation.",
    "normative": "A claim about what ought, should, or must be done.",
    "unknown": "A statement whose epistemic kind cannot be determined safely.",
}

_DDL = """
CREATE TABLE IF NOT EXISTS epistemic_taxonomies (
  taxonomy_id TEXT PRIMARY KEY, name TEXT NOT NULL, semantic_version TEXT NOT NULL,
  definitions_json TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, domain TEXT NOT NULL DEFAULT 'core',
  supersedes_taxonomy_id TEXT, UNIQUE(name,semantic_version)
);
CREATE TABLE IF NOT EXISTS epistemic_assessments (
  assessment_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, statement_id TEXT NOT NULL,
  revision BIGINT NOT NULL, predecessor_assessment_id TEXT, statement_text TEXT NOT NULL,
  taxonomy_id TEXT NOT NULL, machine_status TEXT NOT NULL, effective_status TEXT NOT NULL,
  assessment_state TEXT NOT NULL, confidence DOUBLE NOT NULL, uncertainty DOUBLE NOT NULL,
  evidence_json TEXT NOT NULL, factors_json TEXT NOT NULL, classifier_json TEXT NOT NULL,
  override_json TEXT, source_revision_id TEXT, created_at_ms BIGINT NOT NULL,
  generation BIGINT NOT NULL DEFAULT 0, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL DEFAULT 0, producer_json TEXT NOT NULL DEFAULT '{}',
  policy_json TEXT NOT NULL DEFAULT '{}', principal_id TEXT NOT NULL DEFAULT 'unknown',
  input_hash TEXT NOT NULL DEFAULT '',
  UNIQUE(namespace,statement_id,revision)
);
CREATE TABLE IF NOT EXISTS epistemic_assessment_current (
  namespace TEXT NOT NULL, statement_id TEXT NOT NULL, assessment_id TEXT NOT NULL,
  revision BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,statement_id)
);
CREATE TABLE IF NOT EXISTS epistemic_overrides (
  override_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, statement_id TEXT NOT NULL,
  assessment_id TEXT NOT NULL, reviewer_id TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT NOT NULL, prior_status TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  predecessor_override_id TEXT, sequence BIGINT NOT NULL DEFAULT 1,
  review_hash TEXT NOT NULL DEFAULT '',
  UNIQUE(assessment_id,review_hash)
);
CREATE INDEX IF NOT EXISTS idx_epistemic_status
  ON epistemic_assessments(namespace,effective_status,assessment_state);
"""

_MIGRATIONS = (
    "ALTER TABLE epistemic_taxonomies ADD COLUMN IF NOT EXISTS domain TEXT DEFAULT 'core'",
    "ALTER TABLE epistemic_taxonomies ADD COLUMN IF NOT EXISTS supersedes_taxonomy_id TEXT",
    "ALTER TABLE epistemic_assessments ADD COLUMN IF NOT EXISTS generation BIGINT DEFAULT 0",
    "ALTER TABLE epistemic_assessments ADD COLUMN IF NOT EXISTS valid_from_ms BIGINT",
    "ALTER TABLE epistemic_assessments ADD COLUMN IF NOT EXISTS valid_to_ms BIGINT",
    "ALTER TABLE epistemic_assessments ADD COLUMN IF NOT EXISTS observed_at_ms BIGINT DEFAULT 0",
    "ALTER TABLE epistemic_assessments ADD COLUMN IF NOT EXISTS producer_json TEXT DEFAULT '{}'",
    "ALTER TABLE epistemic_assessments ADD COLUMN IF NOT EXISTS policy_json TEXT DEFAULT '{}'",
    "ALTER TABLE epistemic_assessments ADD COLUMN IF NOT EXISTS principal_id TEXT DEFAULT 'unknown'",
    "ALTER TABLE epistemic_assessments ADD COLUMN IF NOT EXISTS input_hash TEXT DEFAULT ''",
    "ALTER TABLE epistemic_overrides ADD COLUMN IF NOT EXISTS predecessor_override_id TEXT",
    "ALTER TABLE epistemic_overrides ADD COLUMN IF NOT EXISTS sequence BIGINT DEFAULT 1",
    "ALTER TABLE epistemic_overrides ADD COLUMN IF NOT EXISTS review_hash TEXT DEFAULT ''",
)


class EpistemicError(ValueError):
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
        raise EpistemicError("unauthorized", f"missing required scope {required}")


_RULES = (
    (
        "allegation",
        re.compile(
            r"\b(alleg(?:e|es|ed|edly|ation)|accus(?:e|es|ed|ation)|unverified)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "forecast",
        re.compile(
            r"\b(forecast|predict(?:s|ed|ion)?|expected to|projected to|will likely)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "estimate",
        re.compile(
            r"\b(estimate(?:s|d)?|approximately|roughly|about\s+\d|margin of error)\b",
            re.IGNORECASE,
        ),
    ),
    ("normative", re.compile(r"\b(ought to|should|must|need to)\b", re.IGNORECASE)),
    (
        "hypothesis",
        re.compile(
            r"\b(hypothesi[sz]|may explain|could explain|we propose|possibly because)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "opinion",
        re.compile(
            r"\b(in my opinion|we believe|best|worst|prefer(?:s|red)?)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "report",
        re.compile(
            r"\b(according to|reported(?:ly)?|said that|stated that|sources say)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "unknown",
        re.compile(
            r"\b(unknown|unclear|cannot be determined|insufficient information)\b",
            re.IGNORECASE,
        ),
    ),
)


def classify_statement(
    text: str,
    *,
    classifier: Callable[[str], Mapping[str, Any]] | None = None,
    classifier_pin: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    statement = re.sub(r"\s+", " ", str(text).strip())
    if not statement:
        raise EpistemicError("invalid_statement", "statement text is required")
    matches = [status for status, pattern in _RULES if pattern.search(statement)]
    rule_status = matches[0] if matches else "fact"
    result = {
        "status": rule_status,
        "confidence": 0.8 if matches else 0.55,
        "uncertainty": 0.2 if matches else 0.45,
        "signals": matches or ["unmarked-assertion"],
        "classifier": {
            "kind": "rules",
            "name": "noesis-epistemic-rules",
            "version": "1.0.0",
        },
    }
    if classifier is not None:
        pin = dict(classifier_pin or {})
        if not pin.get("name") or not pin.get("version") or not pin.get("revision"):
            raise EpistemicError(
                "unpinned_classifier",
                "classifier name, version, and revision are required",
            )
        model = dict(classifier(statement))
        status = str(model.get("status") or "")
        if status not in STATUSES:
            raise EpistemicError(
                "invalid_classification", "classifier returned an unknown status"
            )
        confidence = min(1.0, max(0.0, float(model.get("confidence", 0.0))))
        result = {
            "status": status,
            "confidence": confidence,
            "uncertainty": round(1.0 - confidence, 6),
            "signals": list(model.get("signals") or []),
            "classifier": {"kind": "model", **pin},
            "rule_fallback": {
                "status": rule_status,
                "signals": matches or ["unmarked-assertion"],
            },
        }
    return result


def aggregate_evidence(evidence: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for item in evidence:
        stance = str(item.get("stance") or "ambiguous")
        if stance not in {"support", "contradict", "ambiguous"}:
            raise EpistemicError("invalid_evidence", "evidence stance is unsupported")
        for factor, default in (
            ("reliability", 0.5),
            ("freshness", 1.0),
            ("methodology", 1.0),
        ):
            value = float(item.get(factor, default))
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise EpistemicError(
                    "invalid_evidence",
                    f"evidence {factor} must be a finite number from 0 to 1",
                )
        groups[
            str(
                item.get("independence_group") or item.get("source_id") or _digest(item)
            )
        ].append(item)
    totals = {"support": 0.0, "contradict": 0.0, "ambiguous": 0.0}
    group_receipts = []
    for group, items in sorted(groups.items()):
        ranked = sorted(
            items,
            key=lambda item: (
                -(
                    float(item.get("reliability", 0.5))
                    * float(item.get("freshness", 1.0))
                    * float(item.get("methodology", 1.0))
                )
            ),
        )
        best = ranked[0]
        weight = max(
            0.0,
            min(
                1.0,
                float(best.get("reliability", 0.5))
                * float(best.get("freshness", 1.0))
                * float(best.get("methodology", 1.0)),
            ),
        )
        stance = str(best.get("stance") or "ambiguous")
        totals[stance] += weight
        group_receipts.append(
            {
                "group": group,
                "stance": stance,
                "weight": round(weight, 6),
                "members": len(items),
            }
        )
    total = sum(totals.values())
    support = totals["support"] / total if total else 0.0
    contradiction = totals["contradict"] / total if total else 0.0
    if total < 0.5:
        state = "insufficient"
    elif support >= 0.65 and contradiction < 0.25:
        state = "supported"
    elif contradiction >= 0.25:
        state = "contested"
    else:
        state = "insufficient"
    confidence = max(support, contradiction) * min(1.0, total / 2.0)
    return {
        "assessment_state": state,
        "confidence": round(confidence, 6),
        "uncertainty": round(1.0 - confidence, 6),
        "totals": {key: round(value, 6) for key, value in totals.items()},
        "independent_groups": group_receipts,
        "evidence_count": len(evidence),
    }


class EpistemicStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
            for statement in _MIGRATIONS:
                conn.execute(statement)
            self._register_taxonomy("core", "1.0.0", _DEFAULT_DEFINITIONS)

    def register_taxonomy(
        self,
        name: str,
        semantic_version: str,
        definitions: Mapping[str, str],
        *,
        scopes: set[str],
        domain: str = "core",
        supersedes_taxonomy_id: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        return self._register_taxonomy(
            name,
            semantic_version,
            definitions,
            domain=domain,
            supersedes_taxonomy_id=supersedes_taxonomy_id,
        )

    def _register_taxonomy(
        self,
        name: str,
        semantic_version: str,
        definitions: Mapping[str, str],
        *,
        domain: str = "core",
        supersedes_taxonomy_id: str | None = None,
    ) -> dict[str, Any]:
        missing = set(STATUSES) - set(definitions)
        invalid = [
            status
            for status, definition in definitions.items()
            if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,63}", status)
            or not str(definition).strip()
        ]
        if missing or invalid:
            raise EpistemicError(
                "invalid_taxonomy",
                "taxonomy must define every core status and valid domain extension",
                missing=sorted(missing),
                invalid=sorted(invalid),
            )
        content_hash = _digest(
            {
                "definitions": definitions,
                "domain": domain,
                "supersedes_taxonomy_id": supersedes_taxonomy_id,
            }
        )
        taxonomy_id = (
            f"epistemic-taxonomy:{name}:{semantic_version}:{content_hash[:16]}"
        )
        conflict = self.conn.execute(
            "SELECT content_hash FROM epistemic_taxonomies WHERE name=? AND semantic_version=?",
            [name, semantic_version],
        ).fetchone()
        if conflict and conflict[0] != content_hash:
            raise EpistemicError(
                "immutable_version", "taxonomy version already has different content"
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO epistemic_taxonomies "
            "(taxonomy_id,name,semantic_version,definitions_json,content_hash,status,created_at_ms,domain,supersedes_taxonomy_id) "
            "VALUES (?,?,?,?,?,'active',?,?,?)",
            [
                taxonomy_id,
                name,
                semantic_version,
                _canonical(dict(definitions)),
                content_hash,
                self.now(),
                domain,
                supersedes_taxonomy_id,
            ],
        )
        return {
            "contract": TAXONOMY_CONTRACT,
            "taxonomy_id": taxonomy_id,
            "name": name,
            "semantic_version": semantic_version,
            "definitions": dict(definitions),
            "content_hash": content_hash,
            "domain": domain,
            "supersedes_taxonomy_id": supersedes_taxonomy_id,
        }

    def list_taxonomies(
        self, *, scopes: set[str], domain: str | None = None
    ) -> list[dict[str, Any]]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT taxonomy_id,name,semantic_version,definitions_json,content_hash,domain,"
            "supersedes_taxonomy_id FROM epistemic_taxonomies "
            + ("WHERE domain=? " if domain else "")
            + "ORDER BY domain,name,semantic_version,taxonomy_id",
            [domain] if domain else [],
        ).fetchall()
        return [
            {
                "contract": TAXONOMY_CONTRACT,
                "taxonomy_id": row[0],
                "name": row[1],
                "semantic_version": row[2],
                "definitions": _load(row[3], {}),
                "content_hash": row[4],
                "domain": row[5],
                "supersedes_taxonomy_id": row[6],
            }
            for row in rows
        ]

    def _active_taxonomy(self, domain: str) -> tuple[str, set[str]]:
        row = self.conn.execute(
            "SELECT taxonomy_id,definitions_json FROM epistemic_taxonomies "
            "WHERE status='active' AND domain IN (?,'core') "
            "ORDER BY CASE WHEN domain=? THEN 0 ELSE 1 END,created_at_ms DESC,taxonomy_id LIMIT 1",
            [domain, domain],
        ).fetchone()
        if not row:
            raise EpistemicError("invalid_assessment", "an active taxonomy is required")
        return str(row[0]), set(_load(row[1], {}))

    def assess(
        self,
        namespace: str,
        statement_id: str,
        text: str,
        evidence: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
        source_revision_id: str | None = None,
        classifier=None,
        classifier_pin: Mapping[str, Any] | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        taxonomy_id, _ = self._active_taxonomy(namespace)
        if not namespace or not statement_id:
            raise EpistemicError(
                "invalid_assessment",
                "taxonomy, namespace, and statement identity are required",
            )
        if generation < 0 or (
            valid_from_ms is not None
            and valid_to_ms is not None
            and valid_to_ms < valid_from_ms
        ):
            raise EpistemicError(
                "invalid_temporality", "generation and valid-time interval are invalid"
            )
        classification = classify_statement(
            text, classifier=classifier, classifier_pin=classifier_pin
        )
        aggregation = aggregate_evidence(evidence)
        producer_value = dict(
            producer
            or {
                "name": "noesis-epistemic-engine",
                "version": "1.0.0",
                "revision": "rules-1",
            }
        )
        policy_value = dict(policy or {"assessment": "independence-weighted-v1"})
        if not producer_value.get("name") or not producer_value.get("version"):
            raise EpistemicError(
                "invalid_producer", "producer name and version are required"
            )
        current = self.conn.execute(
            "SELECT c.assessment_id,c.revision,a.observed_at_ms "
            "FROM epistemic_assessment_current c JOIN epistemic_assessments a "
            "ON a.assessment_id=c.assessment_id WHERE c.namespace=? AND c.statement_id=?",
            [namespace, statement_id],
        ).fetchone()
        observation = int(
            observed_at_ms
            if observed_at_ms is not None
            else current[2]
            if current
            else self.now()
        )
        revision = int(current[1]) + 1 if current else 1
        predecessor = current[0] if current else None
        stable = {
            "namespace": namespace,
            "statement_id": statement_id,
            "text": text,
            "taxonomy_id": taxonomy_id,
            "classification": classification,
            "aggregation": aggregation,
            "evidence": [dict(item) for item in evidence],
            "source_revision_id": source_revision_id,
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": observation,
            "producer": producer_value,
            "policy": policy_value,
        }
        input_hash = _digest(stable)
        assessment_id = (
            "epistemic-assessment:"
            + _digest([namespace, statement_id, revision, input_hash])[:24]
        )
        if current:
            prior = self.get(
                namespace, statement_id, scopes={READ_SCOPE}, include_history=False
            )
            if prior and _digest(
                {
                    key: prior.get(key)
                    for key in (
                        "statement_text",
                        "machine_status",
                        "assessment_state",
                        "evidence",
                        "factors",
                        "classifier",
                        "source_revision_id",
                        "generation",
                        "valid_from_ms",
                        "valid_to_ms",
                        "observed_at_ms",
                        "producer",
                        "policy",
                    )
                }
            ) == _digest(
                {
                    "statement_text": text,
                    "machine_status": classification["status"],
                    "assessment_state": aggregation["assessment_state"],
                    "evidence": [dict(item) for item in evidence],
                    "factors": aggregation,
                    "classifier": classification,
                    "source_revision_id": source_revision_id,
                    "generation": int(generation),
                    "valid_from_ms": valid_from_ms,
                    "valid_to_ms": valid_to_ms,
                    "observed_at_ms": observation,
                    "producer": producer_value,
                    "policy": policy_value,
                }
            ):
                return {**prior, "idempotent": True}
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO epistemic_assessments "
                "(assessment_id,namespace,statement_id,revision,predecessor_assessment_id,statement_text,"
                "taxonomy_id,machine_status,effective_status,assessment_state,confidence,uncertainty,"
                "evidence_json,factors_json,classifier_json,override_json,source_revision_id,created_at_ms,"
                "generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,policy_json,principal_id,input_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    assessment_id,
                    namespace,
                    statement_id,
                    revision,
                    predecessor,
                    text,
                    taxonomy_id,
                    classification["status"],
                    classification["status"],
                    aggregation["assessment_state"],
                    aggregation["confidence"],
                    aggregation["uncertainty"],
                    _canonical([dict(item) for item in evidence]),
                    _canonical(aggregation),
                    _canonical(classification),
                    None,
                    source_revision_id,
                    now,
                    int(generation),
                    valid_from_ms,
                    valid_to_ms,
                    observation,
                    _canonical(producer_value),
                    _canonical(policy_value),
                    principal_id,
                    input_hash,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO epistemic_assessment_current VALUES (?,?,?,?,?)",
                [namespace, statement_id, assessment_id, revision, now],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(namespace, statement_id, scopes={READ_SCOPE})

    def get(
        self,
        namespace: str,
        statement_id: str,
        *,
        scopes: set[str],
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT a.assessment_id,a.revision,a.predecessor_assessment_id,a.statement_text,a.taxonomy_id,"
            "a.machine_status,a.effective_status,a.assessment_state,a.confidence,a.uncertainty,a.evidence_json,"
            "a.factors_json,a.classifier_json,a.override_json,a.source_revision_id,a.created_at_ms,"
            "a.generation,a.valid_from_ms,a.valid_to_ms,a.observed_at_ms,a.producer_json,a.policy_json,"
            "a.principal_id,a.input_hash "
            "FROM epistemic_assessments a WHERE a.namespace=? AND a.statement_id=? "
            + (
                "ORDER BY a.revision"
                if include_history
                else "ORDER BY a.revision DESC LIMIT 1"
            ),
            [namespace, statement_id],
        ).fetchall()
        values = [self._row(namespace, statement_id, row) for row in rows]
        for value in values:
            value["transitions"] = self._transitions(value["assessment_id"])
        if include_history:
            return {
                "namespace": namespace,
                "statement_id": statement_id,
                "revisions": values,
            }
        return values[0] if values else None

    def _transitions(self, assessment_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT override_id,reviewer_id,status,reason,prior_status,created_at_ms,"
            "predecessor_override_id,sequence,review_hash FROM epistemic_overrides "
            "WHERE assessment_id=? ORDER BY sequence,override_id",
            [assessment_id],
        ).fetchall()
        return [
            {
                "override_id": row[0],
                "reviewer_id": row[1],
                "status": row[2],
                "reason": row[3],
                "prior_status": row[4],
                "created_at_ms": int(row[5]),
                "predecessor_override_id": row[6],
                "sequence": int(row[7]),
                "review_hash": row[8],
            }
            for row in rows
        ]

    @staticmethod
    def _row(namespace: str, statement_id: str, row: Sequence[Any]) -> dict[str, Any]:
        return {
            "contract": ASSESSMENT_CONTRACT,
            "assessment_id": row[0],
            "namespace": namespace,
            "statement_id": statement_id,
            "revision": int(row[1]),
            "predecessor_assessment_id": row[2],
            "statement_text": row[3],
            "taxonomy_id": row[4],
            "machine_status": row[5],
            "effective_status": row[6],
            "assessment_state": row[7],
            "confidence": float(row[8]),
            "uncertainty": float(row[9]),
            "evidence": _load(row[10], []),
            "factors": _load(row[11], {}),
            "classifier": _load(row[12], {}),
            "override": _load(row[13], None),
            "source_revision_id": row[14],
            "created_at_ms": int(row[15]),
            "generation": int(row[16]),
            "valid_from_ms": row[17],
            "valid_to_ms": row[18],
            "observed_at_ms": int(row[19]),
            "producer": _load(row[20], {}),
            "policy": _load(row[21], {}),
            "principal_id": row[22],
            "input_hash": row[23],
        }

    def override(
        self,
        namespace: str,
        statement_id: str,
        status: str,
        reason: str,
        *,
        reviewer_id: str,
        scopes: set[str],
        expected_assessment_id: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, REVIEW_SCOPE)
        _, allowed_statuses = self._active_taxonomy(namespace)
        if status not in allowed_statuses or len(reason.strip()) < 10:
            raise EpistemicError(
                "invalid_override", "valid status and substantive reason are required"
            )
        prior = self.get(namespace, statement_id, scopes={READ_SCOPE})
        if not prior:
            raise EpistemicError("not_found", "assessment does not exist")
        if expected_assessment_id and prior["assessment_id"] != expected_assessment_id:
            raise EpistemicError(
                "review_conflict",
                "assessment changed after the reviewer read it",
                expected_assessment_id=expected_assessment_id,
                current_assessment_id=prior["assessment_id"],
            )
        now = self.now()
        review_input = {
            "reviewer_id": reviewer_id,
            "status": status,
            "reason": reason,
        }
        detail = {**review_input, "created_at_ms": now}
        review_hash = _digest([prior["assessment_id"], review_input])
        existing = self.conn.execute(
            "SELECT override_id FROM epistemic_overrides WHERE assessment_id=? AND review_hash=?",
            [prior["assessment_id"], review_hash],
        ).fetchone()
        if existing:
            return {**prior, "idempotent": True}
        last = self.conn.execute(
            "SELECT override_id,sequence FROM epistemic_overrides WHERE assessment_id=? "
            "ORDER BY sequence DESC,override_id DESC LIMIT 1",
            [prior["assessment_id"]],
        ).fetchone()
        predecessor_override_id = last[0] if last else None
        sequence = int(last[1]) + 1 if last else 1
        override_id = "epistemic-override:" + review_hash[:24]
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO epistemic_overrides "
                "(override_id,namespace,statement_id,assessment_id,reviewer_id,status,reason,prior_status,"
                "created_at_ms,predecessor_override_id,sequence,review_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    override_id,
                    namespace,
                    statement_id,
                    prior["assessment_id"],
                    reviewer_id,
                    status,
                    reason,
                    prior["effective_status"],
                    now,
                    predecessor_override_id,
                    sequence,
                    review_hash,
                ],
            )
            self.conn.execute(
                "UPDATE epistemic_assessments SET effective_status=?,override_json=? WHERE assessment_id=?",
                [
                    status,
                    _canonical({"override_id": override_id, **detail}),
                    prior["assessment_id"],
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(namespace, statement_id, scopes={READ_SCOPE})

    def search(
        self,
        namespace: str,
        *,
        scopes: set[str],
        statuses: Sequence[str] = (),
        states: Sequence[str] = (),
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        _require(scopes, READ_SCOPE)
        _, allowed_statuses = self._active_taxonomy(namespace)
        if set(statuses) - allowed_statuses or set(states) - set(ASSESSMENT_STATES):
            raise EpistemicError(
                "invalid_filter", "status or assessment-state filter is unsupported"
            )
        bounded_limit = min(max(1, int(limit)), 1000)
        rows = self.conn.execute(
            "SELECT statement_id FROM epistemic_assessment_current WHERE namespace=? "
            "ORDER BY statement_id LIMIT ?",
            [namespace, bounded_limit * 10],
        ).fetchall()
        values = [self.get(namespace, row[0], scopes=scopes) for row in rows]
        return [
            value
            for value in values
            if value
            and (not statuses or value["effective_status"] in statuses)
            and (not states or value["assessment_state"] in states)
        ][:bounded_limit]

    @staticmethod
    def aggregate(values: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
        by_status: dict[str, int] = defaultdict(int)
        by_state: dict[str, int] = defaultdict(int)
        for value in values:
            by_status[str(value["effective_status"])] += 1
            by_state[str(value["assessment_state"])] += 1
        return {
            "effective_status": dict(sorted(by_status.items())),
            "assessment_state": dict(sorted(by_state.items())),
        }

    def explain(
        self, namespace: str, statement_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        value = self.get(namespace, statement_id, scopes=scopes)
        if not value:
            raise EpistemicError("not_found", "assessment does not exist")
        return {
            "contract": EXPLANATION_CONTRACT,
            "assessment_id": value["assessment_id"],
            "machine_status": value["machine_status"],
            "effective_status": value["effective_status"],
            "assessment_state": value["assessment_state"],
            "confidence": value["confidence"],
            "uncertainty": value["uncertainty"],
            "classification": value["classifier"],
            "evidence_factors": value["factors"],
            "override": value["override"],
            "transitions": value["transitions"],
            "source_revision_id": value["source_revision_id"],
            "generation": value["generation"],
            "valid_time": {
                "from_ms": value["valid_from_ms"],
                "to_ms": value["valid_to_ms"],
            },
            "observed_at_ms": value["observed_at_ms"],
            "producer": value["producer"],
            "policy": value["policy"],
            "limitations": [
                "confidence is policy-calibrated evidence weight, not truth probability"
            ],
        }
