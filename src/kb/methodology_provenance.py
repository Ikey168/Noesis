"""Versioned study methods, exact-locator extraction, assessments, and artifact links."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

STUDY_CONTRACT = "noesis-methodology-study-v1"
EXTRACTION_CONTRACT = "noesis-methodology-extraction-v1"
ASSESSMENT_CONTRACT = "noesis-methodology-assessment-v1"
LINK_CONTRACT = "noesis-study-artifact-link-v1"
COMPARISON_CONTRACT = "noesis-methodology-comparison-v1"
READ_SCOPE = "knowledge:methodology:read"
WRITE_SCOPE = "knowledge:methodology:write"
EXTRACT_SCOPE = "knowledge:methodology:extract"
REVIEW_SCOPE = "knowledge:methodology:review"

_DDL = """
CREATE TABLE IF NOT EXISTS methodology_study_revisions (
  study_revision_id TEXT PRIMARY KEY, study_id TEXT NOT NULL, namespace TEXT NOT NULL,
  version TEXT NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
  predecessor_revision_id TEXT, generation BIGINT NOT NULL, valid_from_ms BIGINT,
  valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL,
  policy_json TEXT NOT NULL, provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(study_id,version)
);
CREATE TABLE IF NOT EXISTS methodology_study_current (
  study_id TEXT PRIMARY KEY, study_revision_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS methodology_extractions (
  extraction_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, study_id TEXT NOT NULL,
  document_id TEXT NOT NULL, input_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,study_id,document_id,input_hash)
);
CREATE TABLE IF NOT EXISTS methodology_statements (
  statement_id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL, namespace TEXT NOT NULL,
  study_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  locator_json TEXT NOT NULL, confidence DOUBLE NOT NULL, uncertainty TEXT,
  conflict_group TEXT, provenance_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS methodology_assessment_revisions (
  assessment_revision_id TEXT PRIMARY KEY, assessment_id TEXT NOT NULL,
  namespace TEXT NOT NULL, study_id TEXT NOT NULL, version BIGINT NOT NULL,
  framework TEXT NOT NULL, dimension TEXT NOT NULL, rating TEXT,
  rationale TEXT NOT NULL, evidence_statement_ids_json TEXT NOT NULL,
  applicability_json TEXT NOT NULL, reviewer_id TEXT, source_locator_json TEXT,
  content_hash TEXT NOT NULL, predecessor_revision_id TEXT, observed_at_ms BIGINT NOT NULL,
  provenance_json TEXT NOT NULL, payload_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(assessment_id,version)
);
CREATE TABLE IF NOT EXISTS methodology_assessment_current (
  assessment_id TEXT PRIMARY KEY, assessment_revision_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS methodology_artifact_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, study_id TEXT NOT NULL,
  artifact_type TEXT NOT NULL, artifact_id TEXT NOT NULL, relation TEXT NOT NULL,
  status TEXT NOT NULL, version TEXT, locator TEXT, indirect_via TEXT,
  content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(namespace,study_id,artifact_type,artifact_id,relation)
);
CREATE TABLE IF NOT EXISTS methodology_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


class MethodologyError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise MethodologyError("unauthorized", f"missing required scope {required}")


def _limit(value: int, maximum: int = 500) -> int:
    return min(max(int(value), 1), maximum)


class MethodologyStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        audit_id = (
            "method-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO methodology_audit VALUES (?,?,?,?,?,?,?)",
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

    def register_study(
        self,
        namespace: str,
        external_id: str,
        version: str,
        title: str,
        design: Mapping[str, Any],
        population: Mapping[str, Any],
        interventions: Sequence[Mapping[str, Any]],
        comparators: Sequence[Mapping[str, Any]],
        outcomes: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
        datasets: Sequence[Mapping[str, Any]] = (),
        samples: Sequence[Mapping[str, Any]] = (),
        instruments: Sequence[Mapping[str, Any]] = (),
        analysis_plans: Sequence[Mapping[str, Any]] = (),
        predecessor_revision_id: str | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if not all(str(v).strip() for v in (namespace, external_id, version, title)):
            raise MethodologyError(
                "invalid_study", "identity, version, and title are required"
            )
        design_value = dict(design)
        if not design_value.get("type"):
            raise MethodologyError("invalid_study", "design.type is required")
        study_id = "study:" + _digest([namespace, external_id])[:24]
        current = self.conn.execute(
            "SELECT r.study_revision_id,r.payload_json FROM methodology_study_current c JOIN methodology_study_revisions r ON r.study_revision_id=c.study_revision_id WHERE r.namespace=? AND r.study_id=?",
            [namespace, study_id],
        ).fetchone()
        if predecessor_revision_id and (
            not current or current[0] != predecessor_revision_id
        ):
            raise MethodologyError(
                "revision_conflict", "predecessor is not the current revision"
            )
        now = self.now()
        observed = observed_at_ms if observed_at_ms is not None else now
        payload = {
            "contract": STUDY_CONTRACT,
            "namespace": namespace,
            "study_id": study_id,
            "external_id": external_id,
            "version": version,
            "title": title,
            "design": design_value,
            "population": dict(population),
            "interventions": [dict(v) for v in interventions],
            "comparators": [dict(v) for v in comparators],
            "outcomes": [dict(v) for v in outcomes],
            "datasets": [dict(v) for v in datasets],
            "samples": [dict(v) for v in samples],
            "instruments": [dict(v) for v in instruments],
            "analysis_plans": [dict(v) for v in analysis_plans],
            "generation": int(generation),
            "valid_time": {"from_ms": valid_from_ms, "to_ms": valid_to_ms},
            "observed_at_ms": observed,
            "producer": dict(producer or {}),
            "policy": dict(policy or {}),
            "provenance": dict(provenance or {}),
        }
        content_hash = _digest(payload)
        revision_id = "study-revision:" + content_hash[:24]
        existing = self.conn.execute(
            "SELECT content_hash,payload_json FROM methodology_study_revisions WHERE study_id=? AND version=?",
            [study_id, version],
        ).fetchone()
        if existing:
            result = _load(existing[1], {})
            if existing[0] != content_hash:
                raise MethodologyError(
                    "version_conflict", "study version has different content"
                )
            return {**result, "study_revision_id": revision_id, "idempotent": True}
        payload["study_revision_id"] = revision_id
        payload["predecessor_revision_id"] = predecessor_revision_id
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT INTO methodology_study_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    study_id,
                    namespace,
                    version,
                    content_hash,
                    _canonical(payload),
                    predecessor_revision_id,
                    generation,
                    valid_from_ms,
                    valid_to_ms,
                    observed,
                    _canonical(producer or {}),
                    _canonical(policy or {}),
                    _canonical(provenance or {}),
                    principal_id,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT INTO methodology_study_current VALUES (?,?) ON CONFLICT(study_id) DO UPDATE SET study_revision_id=excluded.study_revision_id",
                [study_id, revision_id],
            )
            self._audit(
                namespace,
                "register-study",
                revision_id,
                principal_id,
                {"hash": content_hash},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**payload, "idempotent": False}

    def study(
        self, namespace: str, study_id: str, *, scopes: set[str], revision_id=None
    ):
        _require(scopes, READ_SCOPE)
        if revision_id:
            row = self.conn.execute(
                "SELECT payload_json FROM methodology_study_revisions WHERE namespace=? AND study_id=? AND study_revision_id=?",
                [namespace, study_id, revision_id],
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT r.payload_json FROM methodology_study_current c JOIN methodology_study_revisions r ON r.study_revision_id=c.study_revision_id WHERE r.namespace=? AND r.study_id=?",
                [namespace, study_id],
            ).fetchone()
        if not row:
            raise MethodologyError("study_not_found", "study was not found")
        return _load(row[0], {})

    def search(
        self, namespace: str, query: str, *, scopes: set[str], limit=50, offset=0
    ):
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT r.payload_json FROM methodology_study_current c JOIN methodology_study_revisions r ON r.study_revision_id=c.study_revision_id WHERE r.namespace=? ORDER BY r.study_id",
            [namespace],
        ).fetchall()
        needle = query.casefold().strip()
        items = [
            v for (raw,) in rows if needle in _canonical(v := _load(raw, {})).casefold()
        ]
        bounded = _limit(limit)
        page = items[max(int(offset), 0) : max(int(offset), 0) + bounded]
        end = max(int(offset), 0) + len(page)
        return {
            "items": page,
            "next_offset": end if end < len(items) else None,
            "total": len(items),
        }

    def extract(
        self,
        namespace: str,
        study_id: str,
        document_id: str,
        statements: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
        cancel_requested: bool = False,
        limit: int = 500,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, EXTRACT_SCOPE)
        self.study(namespace, study_id, scopes={READ_SCOPE})
        if cancel_requested:
            return {
                "contract": EXTRACTION_CONTRACT,
                "namespace": namespace,
                "study_id": study_id,
                "document_id": document_id,
                "status": "cancelled",
                "items": [],
                "input_hash": _digest(statements),
                "output_hash": _digest([]),
                "truncated": False,
            }
        bounded = list(statements)[: _limit(limit)]
        now = self.now()
        normalized = []
        for index, raw in enumerate(bounded):
            value = dict(raw)
            locator = dict(value.get("locator") or {})
            locator.setdefault("document_id", document_id)
            if locator["document_id"] != document_id or not any(
                locator.get(key) is not None
                for key in ("page", "section", "table", "passage")
            ):
                raise MethodologyError(
                    "invalid_locator", f"statement {index} needs an exact locator"
                )
            text = str(value.get("text") or "").strip()
            kind = str(value.get("kind") or "").strip()
            confidence = float(value.get("confidence", 0.0))
            if not text or not kind or not 0 <= confidence <= 1:
                raise MethodologyError(
                    "invalid_statement", f"statement {index} is incomplete"
                )
            normalized.append(
                {
                    "kind": kind,
                    "text": text,
                    "locator": locator,
                    "confidence": confidence,
                    "uncertainty": value.get("uncertainty"),
                    "provenance": dict(value.get("provenance") or provenance or {}),
                }
            )
        by_kind: dict[str, set[str]] = {}
        for value in normalized:
            by_kind.setdefault(value["kind"], set()).add(value["text"].casefold())
        for value in normalized:
            value["conflict_group"] = (
                "method-conflict:" + _digest([study_id, value["kind"]])[:20]
                if len(by_kind[value["kind"]]) > 1
                else None
            )
            value["statement_id"] = (
                "method-statement:" + _digest([study_id, document_id, value])[:24]
            )
        input_hash = _digest(statements)
        output_hash = _digest(normalized)
        extraction_id = (
            "method-extraction:"
            + _digest([namespace, study_id, document_id, input_hash])[:24]
        )
        receipt = {
            "contract": EXTRACTION_CONTRACT,
            "namespace": namespace,
            "extraction_id": extraction_id,
            "study_id": study_id,
            "document_id": document_id,
            "status": "completed",
            "items": normalized,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "truncated": len(statements) > len(bounded),
            "created_at_ms": now,
        }
        existing = self.conn.execute(
            "SELECT receipt_json FROM methodology_extractions WHERE extraction_id=?",
            [extraction_id],
        ).fetchone()
        if existing:
            return {**_load(existing[0], {}), "idempotent": True}
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT INTO methodology_extractions VALUES (?,?,?,?,?,?,?,?)",
                [
                    extraction_id,
                    namespace,
                    study_id,
                    document_id,
                    input_hash,
                    _canonical(receipt),
                    principal_id,
                    now,
                ],
            )
            for value in normalized:
                self.conn.execute(
                    "INSERT INTO methodology_statements VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        value["statement_id"],
                        extraction_id,
                        namespace,
                        study_id,
                        value["kind"],
                        value["text"],
                        _canonical(value["locator"]),
                        value["confidence"],
                        value["uncertainty"],
                        value["conflict_group"],
                        _canonical(value["provenance"]),
                        now,
                    ],
                )
            self._audit(
                namespace,
                "extract-methods",
                extraction_id,
                principal_id,
                {"count": len(normalized)},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**receipt, "idempotent": False}

    def replay_extraction(
        self, namespace: str, extraction_id: str, *, scopes: set[str]
    ):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT receipt_json FROM methodology_extractions WHERE namespace=? AND extraction_id=?",
            [namespace, extraction_id],
        ).fetchone()
        if not row:
            raise MethodologyError("extraction_not_found", "extraction was not found")
        value = _load(row[0], {})
        actual = _digest(value["items"])
        return {
            "extraction_id": extraction_id,
            "expected_hash": value["output_hash"],
            "actual_hash": actual,
            "deterministic": actual == value["output_hash"],
        }

    def assess(
        self,
        namespace: str,
        study_id: str,
        framework: str,
        dimension: str,
        rating: str | None,
        rationale: str,
        *,
        principal_id: str,
        scopes: set[str],
        evidence_statement_ids: Sequence[str] = (),
        applicability: Mapping[str, Any] | None = None,
        reviewer_id: str | None = None,
        source_locator: Mapping[str, Any] | None = None,
        observed_at_ms: int | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, REVIEW_SCOPE)
        self.study(namespace, study_id, scopes={READ_SCOPE})
        if not framework.strip() or not dimension.strip() or not rationale.strip():
            raise MethodologyError(
                "invalid_assessment", "framework, dimension, and rationale are required"
            )
        if not reviewer_id and not source_locator:
            raise MethodologyError(
                "unsourced_assessment", "a reviewer or source locator is required"
            )
        for statement_id in evidence_statement_ids:
            if not self.conn.execute(
                "SELECT 1 FROM methodology_statements WHERE namespace=? AND study_id=? AND statement_id=?",
                [namespace, study_id, statement_id],
            ).fetchone():
                raise MethodologyError(
                    "evidence_not_found", f"unknown statement {statement_id}"
                )
        assessment_id = (
            "method-assessment:"
            + _digest(
                [
                    namespace,
                    study_id,
                    framework,
                    dimension,
                    reviewer_id or source_locator,
                ]
            )[:24]
        )
        current = self.conn.execute(
            "SELECT r.assessment_revision_id,r.version,r.content_hash,r.payload_json FROM methodology_assessment_current c JOIN methodology_assessment_revisions r ON r.assessment_revision_id=c.assessment_revision_id WHERE c.assessment_id=?",
            [assessment_id],
        ).fetchone()
        now, observed = (
            self.now(),
            observed_at_ms if observed_at_ms is not None else self.now(),
        )
        version = int(current[1]) + 1 if current else 1
        payload = {
            "contract": ASSESSMENT_CONTRACT,
            "namespace": namespace,
            "assessment_id": assessment_id,
            "study_id": study_id,
            "version": version,
            "framework": framework,
            "dimension": dimension,
            "rating": rating,
            "rating_known": rating is not None,
            "rationale": rationale,
            "evidence_statement_ids": list(evidence_statement_ids),
            "applicability": dict(applicability or {}),
            "reviewer_id": reviewer_id,
            "source_locator": dict(source_locator) if source_locator else None,
            "observed_at_ms": observed,
            "provenance": dict(provenance or {}),
        }
        comparable = {k: v for k, v in payload.items() if k != "version"}
        content_hash = _digest(comparable)
        if current and current[2] == content_hash:
            return {**_load(current[3], {}), "idempotent": True}
        revision_id = (
            "method-assessment-revision:"
            + _digest([assessment_id, version, content_hash])[:24]
        )
        payload.update(
            {
                "assessment_revision_id": revision_id,
                "predecessor_revision_id": current[0] if current else None,
            }
        )
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT INTO methodology_assessment_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    assessment_id,
                    namespace,
                    study_id,
                    version,
                    framework,
                    dimension,
                    rating,
                    rationale,
                    _canonical(evidence_statement_ids),
                    _canonical(applicability or {}),
                    reviewer_id,
                    _canonical(source_locator) if source_locator else None,
                    content_hash,
                    current[0] if current else None,
                    observed,
                    _canonical(provenance or {}),
                    _canonical(payload),
                    principal_id,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT INTO methodology_assessment_current VALUES (?,?) ON CONFLICT(assessment_id) DO UPDATE SET assessment_revision_id=excluded.assessment_revision_id",
                [assessment_id, revision_id],
            )
            self._audit(
                namespace,
                "assess-method",
                revision_id,
                principal_id,
                {"rating_known": rating is not None},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**payload, "idempotent": False}

    def limitations(
        self,
        namespace: str,
        study_id: str,
        *,
        scopes: set[str],
        framework=None,
        rating=None,
        limit=100,
    ):
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT r.* FROM methodology_assessment_current c JOIN methodology_assessment_revisions r ON r.assessment_revision_id=c.assessment_revision_id WHERE r.namespace=? AND r.study_id=? ORDER BY r.assessment_id",
            [namespace, study_id],
        ).fetchall()
        columns = [item[0] for item in self.conn.description]
        items = []
        for row in rows:
            value = dict(zip(columns, row))
            if framework and value["framework"] != framework:
                continue
            if rating is not None and value["rating"] != rating:
                continue
            items.append(
                {
                    "contract": ASSESSMENT_CONTRACT,
                    "assessment_id": value["assessment_id"],
                    "assessment_revision_id": value["assessment_revision_id"],
                    "namespace": namespace,
                    "study_id": study_id,
                    "version": value["version"],
                    "framework": value["framework"],
                    "dimension": value["dimension"],
                    "rating": value["rating"],
                    "rating_known": value["rating"] is not None,
                    "rationale": value["rationale"],
                    "evidence_statement_ids": _load(
                        value["evidence_statement_ids_json"], []
                    ),
                    "applicability": _load(value["applicability_json"], {}),
                    "reviewer_id": value["reviewer_id"],
                    "source_locator": _load(value["source_locator_json"], None),
                    "observed_at_ms": value["observed_at_ms"],
                    "provenance": _load(value["provenance_json"], {}),
                }
            )
        return {
            "items": items[: _limit(limit)],
            "total": len(items),
            "reviewer_disagreement": len({(v["dimension"], v["rating"]) for v in items})
            > len({v["dimension"] for v in items}),
        }

    def link_artifact(
        self,
        namespace: str,
        study_id: str,
        artifact_type: str,
        artifact_id: str,
        relation: str,
        *,
        principal_id: str,
        scopes: set[str],
        status="available",
        version=None,
        locator=None,
        indirect_via=None,
        study_external_id=None,
        provenance=None,
    ):
        _require(scopes, WRITE_SCOPE)
        study = self.study(namespace, study_id, scopes={READ_SCOPE})
        allowed = {
            "preregistration",
            "protocol",
            "dataset",
            "code",
            "replication",
            "comment",
            "erratum",
            "retraction",
        }
        if artifact_type not in allowed:
            raise MethodologyError("invalid_artifact", "unsupported artifact type")
        mismatch = (
            study_external_id is not None and study_external_id != study["external_id"]
        )
        normalized_status = "identifier-mismatch" if mismatch else status
        payload = {
            "contract": LINK_CONTRACT,
            "namespace": namespace,
            "study_id": study_id,
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "relation": relation,
            "status": normalized_status,
            "version": version,
            "locator": locator,
            "indirect_via": indirect_via,
            "identifier_mismatch": mismatch,
            "provenance": dict(provenance or {}),
        }
        content_hash = _digest(payload)
        link_id = (
            "study-link:"
            + _digest([namespace, study_id, artifact_type, artifact_id, relation])[:24]
        )
        existing = self.conn.execute(
            "SELECT content_hash,payload_json FROM methodology_artifact_links WHERE link_id=?",
            [link_id],
        ).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise MethodologyError(
                    "link_conflict",
                    "artifact link already exists with different version or status",
                )
            return {**_load(existing[1], {}), "link_id": link_id, "idempotent": True}
        now = self.now()
        payload.update({"link_id": link_id, "created_at_ms": now})
        self.conn.execute(
            "INSERT INTO methodology_artifact_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                link_id,
                namespace,
                study_id,
                artifact_type,
                artifact_id,
                relation,
                normalized_status,
                version,
                locator,
                indirect_via,
                content_hash,
                _canonical(payload),
                principal_id,
                now,
            ],
        )
        self._audit(
            namespace,
            "link-artifact",
            link_id,
            principal_id,
            {"status": normalized_status},
            now,
        )
        return {**payload, "idempotent": False}

    def replication_graph(
        self, namespace: str, study_id: str, *, scopes: set[str], limit=100
    ):
        _require(scopes, READ_SCOPE)
        self.study(namespace, study_id, scopes=scopes)
        rows = self.conn.execute(
            "SELECT payload_json FROM methodology_artifact_links WHERE namespace=? AND study_id=? ORDER BY link_id LIMIT ?",
            [namespace, study_id, _limit(limit)],
        ).fetchall()
        links = [_load(row[0], {}) for row in rows]
        return {
            "contract": LINK_CONTRACT,
            "namespace": namespace,
            "study_id": study_id,
            "links": links,
            "replications": [v for v in links if v["artifact_type"] == "replication"],
            "citation_closure": all(
                v.get("locator") or v["status"] != "available" for v in links
            ),
        }

    def compare(
        self, namespace: str, study_ids: Sequence[str], *, scopes: set[str], limit=20
    ):
        _require(scopes, READ_SCOPE)
        ids = list(study_ids)[: _limit(limit, 20)]
        if len(ids) < 2:
            raise MethodologyError(
                "invalid_comparison", "at least two studies are required"
            )
        studies = [self.study(namespace, item, scopes=scopes) for item in ids]
        dimensions = (
            "design",
            "population",
            "interventions",
            "comparators",
            "outcomes",
            "analysis_plans",
        )
        differences = [
            {
                "dimension": key,
                "values": [
                    {"study_id": v["study_id"], "value": v[key]} for v in studies
                ],
            }
            for key in dimensions
            if len({_canonical(v[key]) for v in studies}) > 1
        ]
        result = {
            "contract": COMPARISON_CONTRACT,
            "namespace": namespace,
            "study_ids": ids,
            "differences": differences,
            "comparison_hash": _digest([namespace, ids, differences]),
        }
        return result

    def explain_strength(self, namespace: str, study_id: str, *, scopes: set[str]):
        _require(scopes, READ_SCOPE)
        study = self.study(namespace, study_id, scopes=scopes)
        limitations = self.limitations(namespace, study_id, scopes=scopes)["items"]
        graph = self.replication_graph(namespace, study_id, scopes=scopes)
        known = [v for v in limitations if v["rating_known"]]
        unknown = [v["dimension"] for v in limitations if not v["rating_known"]]
        signals = {
            "design": study["design"].get("type"),
            "known_assessments": len(known),
            "unknown_assessments": unknown,
            "available_replications": sum(
                v["status"] == "available" for v in graph["replications"]
            ),
            "retracted": any(
                v["artifact_type"] == "retraction" and v["status"] == "available"
                for v in graph["links"]
            ),
            "citation_closure": graph["citation_closure"],
        }
        return {
            "contract": COMPARISON_CONTRACT,
            "namespace": namespace,
            "study_id": study_id,
            "strength": "unknown" if unknown or not limitations else "qualified",
            "signals": signals,
            "explanation": "Strength is reported from explicit design, assessment, replication, and correction records; unknown fields remain unknown.",
            "explanation_hash": _digest(signals),
        }

    # -------------------------------------------- clinical trial design (H08)
    #
    # A registered trial is a study whose design carries the trial-design
    # fields, one study revision per registry version. Outcome switching
    # compares consecutive registered versions (and a publication's stated
    # primary outcome) and reports findings with both sides cited; it never
    # judges a trial. Non-trial studies are untouched.

    def register_trial_revision(
        self,
        namespace: str,
        external_id: str,
        registry_version: str,
        title: str,
        design: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        generation: int,
        observed_at_ms: int,
        population: Mapping[str, Any] | None = None,
        interventions: Sequence[Mapping[str, Any]] = (),
        comparators: Sequence[Mapping[str, Any]] = (),
        outcomes: Sequence[Mapping[str, Any]] = (),
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """One registry version as one study revision (design type ``clinical-trial``)."""
        _require(scopes, WRITE_SCOPE)
        fields = trial_design(design)
        study_id = "study:" + _digest([namespace, external_id])[:24]
        current = self.conn.execute(
            "SELECT study_revision_id FROM methodology_study_current WHERE study_id=?",
            [study_id],
        ).fetchone()
        return self.register_study(
            namespace,
            external_id,
            f"registry:{registry_version}",
            title or external_id,
            {"type": "clinical-trial", "contract": TRIAL_DESIGN_CONTRACT, "trial": fields},
            dict(population or {}),
            interventions,
            comparators,
            outcomes,
            principal_id=principal_id,
            scopes=scopes,
            predecessor_revision_id=current[0] if current else None,
            generation=int(generation),
            observed_at_ms=int(observed_at_ms),
            provenance=provenance,
            producer={"name": "noesis-clinical-evidence", "version": "1.0.0"},
        )

    def trial_revisions(self, namespace: str, study_id: str, *, scopes: set[str]):
        """Trial-design fields per registry version, in registry order."""
        _require(scopes, READ_SCOPE)
        self.study(namespace, study_id, scopes=scopes)
        rows = self.conn.execute(
            "SELECT payload_json FROM methodology_study_revisions WHERE namespace=? AND study_id=? "
            "ORDER BY generation, observed_at_ms, created_at_ms",
            [namespace, study_id],
        ).fetchall()
        revisions = [_load(row[0], {}) for row in rows]
        return [
            {
                "study_revision_id": r["study_revision_id"],
                "version": r["version"],
                "generation": r["generation"],
                "trial": r["design"].get("trial"),
                "provenance": r.get("provenance"),
            }
            for r in revisions
            if r["design"].get("type") == "clinical-trial"
        ]

    def outcome_switching(
        self,
        namespace: str,
        study_id: str,
        *,
        scopes: set[str],
        publication_statements: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Primary outcomes added, removed, re-timed or demoted across versions, or differing in a publication."""
        _require(scopes, READ_SCOPE)
        revisions = self.trial_revisions(namespace, study_id, scopes=scopes)
        if not revisions:
            return {
                "contract": OUTCOME_SWITCHING_CONTRACT,
                "study_id": study_id,
                "applies": False,
                "reason": "not a registered trial; outcome switching compares registry versions",
                "findings": [],
            }
        findings: list[dict[str, Any]] = []

        def side(revision, outcome=None):
            return {
                "study_revision_id": revision["study_revision_id"],
                "registry_version": revision["version"],
                "measure": (outcome or {}).get("measure"),
                "time_frame": (outcome or {}).get("time_frame"),
            }

        for before, after in zip(revisions, revisions[1:]):
            old = {_measure_key(o["measure"]): o for o in (before["trial"] or {}).get("primary_outcomes") or []}
            new = {_measure_key(o["measure"]): o for o in (after["trial"] or {}).get("primary_outcomes") or []}
            secondary = {
                _measure_key(o["measure"])
                for o in (after["trial"] or {}).get("secondary_outcomes") or []
            }
            for key in sorted(set(new) - set(old)):
                findings.append({"kind": "primary-added", "measure": new[key]["measure"],
                                 "before": side(before), "after": side(after, new[key])})
            for key in sorted(set(old) - set(new)):
                findings.append({"kind": "primary-demoted" if key in secondary else "primary-removed",
                                 "measure": old[key]["measure"], "before": side(before, old[key]),
                                 "after": side(after)})
            for key in sorted(set(old) & set(new)):
                if _time_key(old[key].get("time_frame")) != _time_key(new[key].get("time_frame")):
                    findings.append({"kind": "primary-retimed", "measure": new[key]["measure"],
                                     "before": side(before, old[key]), "after": side(after, new[key])})
        statements = list(publication_statements or [])
        if publication_statements is None:
            rows = self.conn.execute(
                "SELECT statement_id,text,locator_json FROM methodology_statements WHERE namespace=? AND study_id=? "
                "AND kind='primary-outcome' ORDER BY statement_id",
                [namespace, study_id],
            ).fetchall()
            statements = [{"statement_id": r[0], "text": r[1], "locator": _load(r[2], {})} for r in rows]
        latest = revisions[-1]
        for statement in statements:
            kind, outcome = _publication_difference(
                (latest["trial"] or {}).get("primary_outcomes") or [], statement
            )
            if kind:
                findings.append({
                    "kind": kind,
                    "measure": (outcome or {}).get("measure"),
                    "before": side(latest, outcome),
                    "after": {"publication": statement.get("locator"),
                              "statement_id": statement.get("statement_id"), "text": statement["text"]},
                })
        for finding in findings:
            finding.update(finding=True, note=SWITCH_NOTE)
        return {
            "contract": OUTCOME_SWITCHING_CONTRACT,
            "study_id": study_id,
            "applies": True,
            "versions_compared": [r["version"] for r in revisions],
            "publication_statements": len(statements),
            "findings": findings,
            "findings_hash": _digest(findings),
        }


TRIAL_DESIGN_CONTRACT = "noesis-methodology-trial-design-v1"
OUTCOME_SWITCHING_CONTRACT = "noesis-outcome-switching-v1"
TRIAL_DESIGN_FIELDS = (
    "phase", "allocation", "masking", "sample_size_planned", "sample_size_actual",
    "preregistration", "primary_outcomes", "secondary_outcomes",
)
SWITCH_NOTE = (
    "A recorded difference between registrations (or between a registration and a "
    "publication). It is a finding to examine, not a judgement of the trial or its results."
)
_STOP = {"from", "in", "the", "of", "to", "at", "a", "an", "change", "baseline", "mean", "percent", "percentage"}


def _words(value: Any) -> list[str]:
    import re

    return re.findall(r"[a-z0-9]+", str(value or "").casefold())


def _measure_key(measure: Any) -> str:
    return " ".join(_words(measure))


def _time_key(value: Any) -> tuple[str, ...]:
    """Time points stated in a time frame ('Baseline, week 26' -> ('week:26',)); else its words."""
    import re

    text = str(value or "").casefold()
    points = {f"{m.group(1)}:{m.group(2)}" for m in re.finditer(r"(week|month|day|year)s?\s*(\d+)", text)}
    points |= {f"{m.group(2)}:{m.group(1)}" for m in re.finditer(r"(\d+)[\s-]*(week|month|day|year)s?", text)}
    return tuple(sorted(points)) or tuple(_words(text))


def _publication_difference(registered: Sequence[Mapping[str, Any]], statement: Mapping[str, Any]):
    text_words = set(_words(statement["text"]))
    for outcome in registered:
        words = set(_words(outcome["measure"])) - _STOP
        if words and words <= text_words:
            registered_times = set(_time_key(outcome.get("time_frame")))
            stated_times = {t for t in _time_key(statement["text"]) if ":" in t}
            registered_points = {t for t in registered_times if ":" in t}
            if registered_points and stated_times and not registered_points & stated_times:
                return "publication-retimed", outcome
            return None, outcome
    return ("publication-different-primary", None) if registered else (None, None)


def trial_design(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate trial-design fields; missing values stay ``None`` and are listed as unknown."""
    if not isinstance(value, Mapping) or set(value) - set(TRIAL_DESIGN_FIELDS):
        raise MethodologyError(
            "invalid_trial_design", "trial design uses " + ", ".join(TRIAL_DESIGN_FIELDS)
        )
    design = {key: value.get(key) for key in TRIAL_DESIGN_FIELDS}
    for key in ("sample_size_planned", "sample_size_actual"):
        if design[key] is not None and (type(design[key]) is not int or design[key] < 0):
            raise MethodologyError("invalid_trial_design", f"{key} is a nonnegative integer or unknown")
    registration = design["preregistration"]
    if registration is not None and (
        not isinstance(registration, Mapping)
        or registration.get("prospective") not in (True, False, None)
    ):
        raise MethodologyError("invalid_trial_design", "preregistration.prospective is true, false or unknown")
    for key in ("primary_outcomes", "secondary_outcomes"):
        outcomes = design[key]
        if outcomes is not None and (
            not isinstance(outcomes, list)
            or any(not isinstance(o, Mapping) or not str(o.get("measure") or "").strip() for o in outcomes)
        ):
            raise MethodologyError("invalid_trial_design", f"{key} need a measure")
    unknown = [
        key for key in TRIAL_DESIGN_FIELDS
        if key != "secondary_outcomes" and design[key] in (None, "unknown", [], ["unknown"])
    ]
    if isinstance(registration, Mapping) and registration.get("prospective") is None:
        unknown.append("preregistration")
    design["unknown"] = sorted(set(unknown))
    return json.loads(_canonical(design))
