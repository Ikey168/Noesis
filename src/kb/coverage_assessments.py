"""Pinned, non-exhaustive investigation coverage from committed pack runs."""

from __future__ import annotations

import hashlib
import itertools
import json
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.kb.research_gaps import ResearchGapStore
from src.kb.review_inbox import ReviewInboxStore
from src.kb.source_planner import SourcePlannerStore

CONTRACT = "noesis-investigation-coverage-v1"
COMPARISON_CONTRACT = "noesis-investigation-coverage-comparison-v1"
READ_SCOPE = "knowledge:coverage:read"
WRITE_SCOPE = "knowledge:coverage:write"
_DDL = """
CREATE TABLE IF NOT EXISTS investigation_coverage_assessments(
 assessment_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_hash TEXT NOT NULL,state_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""
_REASONS = {
    "credential_missing", "unsupported_capability", "budget_exhausted",
    "failed_acquisition", "access_restriction", "unattempted_source",
    "historical_record_unavailable", "partial_date_window", "query_scope_unverified",
    "stale_readiness", "license_not_accepted",
    "network_policy", "circuit_open", "source_unavailable",
}
_ORDER = {"observed": 4, "empty-successful": 3, "unavailable": 2, "unattempted": 1, "out-of-scope": 0}


class CoverageError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value: Any, label: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise CoverageError("invalid_input", f"{label} must be bounded nonempty text")
    return value.strip()


def _safe(value: Any) -> bool:
    if isinstance(value, Mapping):
        return all(str(k).casefold().replace("-", "_") not in
                   {"secret", "token", "password", "api_key", "apikey", "authorization", "credential_value"}
                   and _safe(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return all(_safe(v) for v in value)
    return True


def _supports(cell: Mapping[str, Any], coverage: Mapping[str, Any]) -> bool:
    for axis, key in (("geographies", "geography"), ("languages", "language"), ("topics", "topic")):
        allowed = coverage.get(axis)
        if isinstance(allowed, list) and allowed and cell[key] not in allowed:
            return False
    windows = coverage.get("date_windows")
    if isinstance(windows, list) and windows:
        return any(
            isinstance(window, Mapping)
            and type(window.get("from_ms")) is int and type(window.get("to_ms")) is int
            and window["from_ms"] <= cell["date"]["from_ms"]
            and window["to_ms"] >= cell["date"]["to_ms"]
            for window in windows
        )
    return True


def _verified_empty_query(
    request: Mapping[str, Any], cell: Mapping[str, Any],
    capability: Mapping[str, Any] | None, source_receipt: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    if not capability or not {"topic", "language", "geography", "date-range"} <= set(
        capability.get("query_forms") or []
    ):
        return False
    parameters = request.get("parameters") or {}
    if any(parameters.get(key) != cell[key] for key in ("topic", "language", "geography")):
        return False
    backfill = request.get("backfill") or {}
    return bool(
        request.get("mode") == "backfill"
        and type(backfill.get("from_ms")) is int
        and type(backfill.get("to_ms")) is int
        and backfill["from_ms"] <= cell["date"]["from_ms"]
        and backfill["to_ms"] >= cell["date"]["to_ms"]
        and source_receipt.get("cursor", {}).get("end") is None
        and receipt.get("watermark") is not None
    )


class CoverageAssessmentStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _auth(namespace: str, owner: str, principal_id: str, scopes: set[str], *, write=False):
        required = WRITE_SCOPE if write else READ_SCOPE
        ns = f"namespace:{namespace}:{'write' if write else 'read'}"
        if not principal_id or ("operator" not in scopes and
                (principal_id != owner or required not in scopes or ns not in scopes)):
            raise CoverageError("unauthorized", "coverage ownership and namespace scope required")

    @staticmethod
    def _cells(scope: Mapping[str, Any]) -> list[dict[str, Any]]:
        if not isinstance(scope, Mapping) or set(scope) != {"dates", "geographies", "languages", "topics"}:
            raise CoverageError("invalid_scope", "dates, geographies, languages and topics are required")
        axes = []
        for key in ("dates", "geographies", "languages", "topics"):
            values = scope[key]
            if not isinstance(values, list) or not 1 <= len(values) <= 8:
                raise CoverageError("invalid_scope", f"{key} requires one to eight values")
            if key == "dates":
                valid = []
                for item in values:
                    if not isinstance(item, Mapping) or set(item) != {"from_ms", "to_ms"} or (
                        type(item["from_ms"]) is not int or type(item["to_ms"]) is not int
                        or item["from_ms"] < 0 or item["to_ms"] <= item["from_ms"]
                    ):
                        raise CoverageError("invalid_scope", "date windows must be bounded epoch intervals")
                    valid.append(dict(item))
                axes.append(valid)
            else:
                axes.append([_text(item, key) for item in values])
        if any(len({_json(v) for v in axis}) != len(axis) for axis in axes):
            raise CoverageError("invalid_scope", "scope axis values must be unique")
        combinations = list(itertools.product(*axes))
        if len(combinations) > 64:
            raise CoverageError("scope_limit", "scope has more than 64 assessment cells")
        return [
            {"cell_id": "coverage-cell:" + _hash(values)[:16], "date": values[0],
             "geography": values[1], "language": values[2], "topic": values[3]}
            for values in combinations
        ]

    def _pack(self, pack_id: str, version: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT manifest_json,manifest_hash FROM source_pack_versions WHERE pack_id=? AND version=?",
            [pack_id, version],
        ).fetchone()
        if not row:
            raise CoverageError("pack_unavailable", "pinned pack version is unavailable")
        manifest = json.loads(row[0])
        if manifest.get("manifest_hash") != row[1]:
            raise CoverageError("pack_unavailable", "pinned manifest hash disagrees")
        return manifest

    def _run(self, run_id: str, pack_id: str, version: str, manifest_hash: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT receipt_json,request_json,status,pack_id,pack_version,manifest_hash "
            "FROM source_pack_runs WHERE run_id=?", [run_id],
        ).fetchone()
        if not row or tuple(row[3:]) != (pack_id, version, manifest_hash) or not row[0]:
            raise CoverageError("run_unavailable", "pinned source-pack run receipt is unavailable")
        receipt = json.loads(row[0])
        if receipt.get("run_id") != run_id or receipt.get("manifest_hash") != manifest_hash:
            raise CoverageError("run_unavailable", "pinned receipt identity disagrees")
        if receipt.get("status") != row[2]:
            raise CoverageError("run_unavailable", "pinned receipt status disagrees")
        return {"receipt": receipt, "request": json.loads(row[1])}

    def _evidence(self, ref: Mapping[str, Any], *, run_id: str, pack_id: str,
                  source_id: str, cell: Mapping[str, Any], scopes: set[str]) -> dict[str, Any]:
        if not isinstance(ref, Mapping) or set(ref) != {"document_id", "revision_id"}:
            raise CoverageError("invalid_evidence", "exact document revision is required")
        document_id = _text(ref["document_id"], "document ID")
        revision_id = _text(ref["revision_id"], "revision ID")
        if "operator" not in scopes and f"document:{document_id}:read" not in scopes:
            raise CoverageError("unauthorized", "document read scope required")
        row = self.conn.execute(
            "SELECT payload_json,content_hash,observed_at_ms FROM document_revision_records "
            "WHERE document_id=? AND revision_id=? AND run_id=? AND pack_id=? "
            "AND source_id=? AND committed_watermark IS NOT NULL",
            [document_id, revision_id, run_id, pack_id, source_id],
        ).fetchone()
        if not row:
            raise CoverageError("invalid_evidence", "document is not a committed revision of the pinned run")
        payload = json.loads(row[0])
        if payload.get("_payload_reclaimed"):
            raise CoverageError("historical_record_unavailable", "pinned evidence payload is reclaimed")
        language = payload.get("language")
        if language and language.casefold() != cell["language"].casefold():
            raise CoverageError("invalid_evidence", "document language lies outside cell")
        metadata = payload.get("metadata") or {}
        temporal = metadata.get("source_pack_temporal_json")
        temporal = json.loads(temporal) if isinstance(temporal, str) else temporal or {}
        event_time = temporal.get("published_at") or temporal.get("effective_at") or payload.get("created_at")
        if isinstance(event_time, str):
            try:
                parsed = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
                event_time = int(parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp() * 1000)
            except ValueError:
                event_time = None
        if not isinstance(event_time, (int, float)) or not cell["date"]["from_ms"] <= event_time < cell["date"]["to_ms"]:
            raise CoverageError("invalid_evidence", "document has no supported date inside cell")
        return {"document_id": document_id, "revision_id": revision_id,
                "content_hash": row[1], "observed_at_ms": int(row[2]), "event_at_ms": int(event_time)}

    def save(self, namespace: str, request_key: str, scope: Mapping[str, Any],
             bindings: Sequence[Mapping[str, Any]], *, principal_id: str, scopes: set[str],
             record_gap_observations: bool = False) -> dict[str, Any]:
        namespace, request_key = _text(namespace, "namespace"), _text(request_key, "request key")
        self._auth(namespace, principal_id, principal_id, scopes, write=True)
        cells = self._cells(scope)
        if not isinstance(bindings, list) or not 1 <= len(bindings) <= 50:
            raise CoverageError("invalid_bindings", "one to 50 pinned sources required")
        if not _safe(bindings):
            raise CoverageError("invalid_bindings", "credential material is forbidden")
        if record_gap_observations and "operator" not in scopes and "knowledge:gaps:write" not in scopes:
            raise CoverageError("unauthorized", "research gap write scope required")
        assessment_id = "coverage:" + _hash([namespace, principal_id, request_key])[:24]
        request_hash = _hash([scope, bindings, bool(record_gap_observations)])
        prior = self.conn.execute(
            "SELECT request_hash FROM investigation_coverage_assessments WHERE assessment_id=?",
            [assessment_id]).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise CoverageError("idempotency_conflict", "request key identifies different coverage")
            return {**self.inspect(namespace, assessment_id, principal_id=principal_id, scopes=scopes), "idempotent": True}
        cell_map = {c["cell_id"]: c for c in cells}
        results = {key: [] for key in cell_map}
        source_rows = []
        bound = set()
        for binding in bindings:
            if not isinstance(binding, Mapping) or set(binding) - {
                "pack_id", "pack_version", "source_id", "run_id", "cell_ids",
                "evidence", "readiness", "capability_id", "source_limitations",
                "plan_id", "review_task_id",
            }:
                raise CoverageError("invalid_bindings", "unsupported source binding")
            pack_id = _text(binding.get("pack_id"), "pack ID")
            version = _text(binding.get("pack_version"), "pack version")
            source_id = _text(binding.get("source_id"), "source ID")
            identity = (pack_id, version, source_id)
            if identity in bound:
                raise CoverageError("invalid_bindings", "duplicate pinned source")
            bound.add(identity)
            manifest = self._pack(pack_id, version)
            source = next((s for s in manifest["sources"] if s["source_id"] == source_id), None)
            if source is None:
                raise CoverageError("invalid_bindings", "source absent from pinned pack")
            covered = binding.get("cell_ids")
            if not isinstance(covered, list) or not covered or len(covered) > 64 or len(set(covered)) != len(covered) or set(covered) - set(cell_map):
                raise CoverageError("invalid_bindings", "source cell IDs are invalid")
            capability = None
            if binding.get("capability_id"):
                capability = SourcePlannerStore(self.conn).capability(
                    namespace, binding["capability_id"], scopes=scopes)
                if not capability or capability["source_id"] != source_id:
                    raise CoverageError("invalid_bindings", "pinned source capability disagrees")
            plan_id = binding.get("plan_id")
            if plan_id:
                plan = SourcePlannerStore(self.conn).plan(namespace, _text(plan_id, "plan ID"), scopes=scopes)
                if not any(step.get("source_id") == source_id for step in plan.get("steps", []) + plan.get("fallback_steps", [])):
                    raise CoverageError("invalid_bindings", "pinned source plan does not include source")
            review_task_id = binding.get("review_task_id")
            if review_task_id:
                ReviewInboxStore(self.conn).inspect(
                    namespace, _text(review_task_id, "review task ID"),
                    principal_id=principal_id, scopes=scopes)
            run_id = binding.get("run_id")
            run = self._run(_text(run_id, "run ID"), pack_id, version, manifest["manifest_hash"]) if run_id else None
            source_receipt = next((r for r in run["receipt"]["sources"] if r["source_id"] == source_id), None) if run else None
            source_failure = next((f for f in run["receipt"]["failures"] if f["source_id"] == source_id), None) if run else None
            readiness = binding.get("readiness")
            if readiness is not None:
                if not isinstance(readiness, Mapping) or set(readiness) != {
                    "observed_at_ms", "manifest_hash", "failures"
                } or readiness["manifest_hash"] != manifest["manifest_hash"] or (
                    type(readiness["observed_at_ms"]) is not int or not isinstance(readiness["failures"], list)
                    or any(f not in _REASONS for f in readiness["failures"])
                ):
                    raise CoverageError("invalid_readiness", "only safe pinned preflight reasons are accepted")
                if "credential_missing" in readiness["failures"] and source["auth"]["kind"] != "required-secret":
                    raise CoverageError("invalid_readiness", "credential gap disagrees with source authentication")
            evidence = binding.get("evidence") or {}
            if not isinstance(evidence, Mapping) or set(evidence) - set(covered) or sum(len(v) for v in evidence.values()) > 100:
                raise CoverageError("invalid_evidence", "evidence references exceed source or bound")
            limitations = binding.get("source_limitations") or []
            if not isinstance(limitations, list) or len(limitations) > 10:
                raise CoverageError("invalid_bindings", "source limitations exceed bound")
            limitations = [_text(v, "source limitation", 500) for v in limitations]
            cost = None
            if capability:
                configured = capability.get("cost") or {}
                value = configured.get("estimated_cost")
                if isinstance(value, (float, int)) and not isinstance(value, bool) and 0 <= value < 1_000_000:
                    cost = float(value)
            for cell_id in covered:
                cell = cell_map[cell_id]
                supported = not capability or _supports(cell, capability.get("coverage") or {})
                refs = evidence.get(cell_id) or []
                if not isinstance(refs, list) or len(refs) > 20 or (refs and not run):
                    raise CoverageError("invalid_evidence", "cell evidence requires a bounded pinned run")
                if refs and not supported:
                    raise CoverageError("invalid_evidence", "evidence lies outside pinned source capability")
                citations = []
                restricted = False
                for ref in refs:
                    try:
                        citations.append(self._evidence(
                            ref, run_id=run_id, pack_id=pack_id,
                            source_id=source_id, cell=cell, scopes=scopes))
                    except CoverageError as exc:
                        if exc.code != "unauthorized":
                            raise
                        restricted = True
                reasons = []
                if restricted:
                    reasons.append("access_restriction")
                if not supported:
                    reasons.append("unsupported_capability")
                if source_failure:
                    if source_failure.get("classification") == "preflight":
                        reasons.extend(source_failure.get("detail") or [])
                    else:
                        failure_code = (source_failure.get("error") or {}).get("code")
                        reasons.append("budget_exhausted" if failure_code == "budget_exhausted" else "failed_acquisition")
                if readiness:
                    age = self.now() - readiness["observed_at_ms"]
                    if age < 0 or age > 172_800_000:
                        reasons.append("stale_readiness")
                    else:
                        reasons.extend(readiness["failures"])
                if run:
                    backfill = run["request"].get("backfill") or {}
                    start, end = backfill.get("from_ms"), backfill.get("to_ms")
                    if (start is not None and start > cell["date"]["from_ms"]) or (
                        end is not None and end < cell["date"]["to_ms"]
                    ):
                        reasons.append("partial_date_window")
                if not supported:
                    status = "out-of-scope"
                elif citations:
                    status = "observed"
                elif source_receipt and source_receipt["status"] == "complete" and source_receipt["counts"]["fetched"] == 0:
                    if _verified_empty_query(run["request"], cell, capability, source_receipt, run["receipt"]):
                        status = "empty-successful"
                    else:
                        status = "unavailable"
                        reasons.append("query_scope_unverified")
                elif source_failure or reasons:
                    status = "unavailable"
                else:
                    status = "unattempted"
                    reasons.append("unattempted_source")
                if supported and source_receipt and source_receipt["status"] == "complete" and source_receipt["counts"]["fetched"] > 0 and not citations:
                    reasons.append("historical_record_unavailable")
                    status = "unavailable"
                results[cell_id].append({
                    "pack_id": pack_id, "pack_version": version, "manifest_hash": manifest["manifest_hash"],
                    "source_id": source_id, "run_id": run_id,
                    "receipt_hash": run["receipt"]["receipt_hash"] if run else None,
                    "commit_state": ("committed" if run["receipt"].get("watermark") is not None
                                     else "failed_uncommitted") if run else "unattempted",
                    "status": status, "reasons": sorted(set(reasons)), "citations": citations,
                    "source_limitations": [source["scope"], source["temporal_semantics"], *limitations],
                    "estimated_cost": cost,
                    "plan_id": plan_id, "review_task_id": review_task_id,
                    "readiness_observed_at_ms": readiness["observed_at_ms"] if readiness else None,
                })
            source_rows.append({"pack_id": pack_id, "pack_version": version, "source_id": source_id,
                                "manifest_hash": manifest["manifest_hash"], "run_id": run_id,
                                "receipt": run["receipt"] if run else None,
                                "commit_state": ("committed" if run["receipt"].get("watermark") is not None
                                                 else "failed_uncommitted") if run else "unattempted",
                                "request": run["request"] if run else None,
                                "capability_id": binding.get("capability_id")})
        assessed = []
        for cell in cells:
            entries = results[cell["cell_id"]]
            status = max((v["status"] for v in entries), key=_ORDER.get) if entries else "unattempted"
            reasons = sorted({reason for v in entries for reason in v["reasons"]})
            if not entries:
                reasons.append("unattempted_source")
            actions = []
            for entry in entries:
                for reason in entry["reasons"]:
                    action = {
                        "credential_missing": "configure_credential",
                        "budget_exhausted": "adjust_budget",
                        "failed_acquisition": "retry_acquisition",
                        "access_restriction": "request_access",
                        "unattempted_source": "plan_acquisition",
                        "stale_readiness": "refresh_preflight",
                    }.get(reason, "review_source")
                    candidate = {"action": action, "pack_id": entry["pack_id"], "source_id": entry["source_id"]}
                    if entry["estimated_cost"] is not None:
                        candidate["estimated_cost"] = entry["estimated_cost"]
                    if entry["plan_id"]:
                        candidate["plan_id"] = entry["plan_id"]
                    if entry["review_task_id"]:
                        candidate["review_task_id"] = entry["review_task_id"]
                    if candidate not in actions:
                        actions.append(candidate)
            assessed.append({**cell, "status": status, "reasons": reasons, "sources": entries,
                             "next_actions": actions[:10]})
        state = {"contract": CONTRACT, "assessment_id": assessment_id, "namespace": namespace,
                 "owner": principal_id, "scope": dict(scope), "denominator": len(cells),
                 "cells": assessed, "sources": source_rows,
                 "limitations": ["Only pinned runs and selected evidence are assessed.",
                                 "A successful provider query does not establish exhaustive topic coverage."]}
        if len(_json(state).encode()) > 4 * 1024 * 1024:
            raise CoverageError("assessment_limit", "coverage assessment exceeds 4 MiB")
        self.conn.execute("INSERT INTO investigation_coverage_assessments VALUES (?,?,?,?,?,?)",
                          [assessment_id, namespace, principal_id, request_hash, _json(state), self.now()])
        if record_gap_observations:
            gaps = ResearchGapStore(self.conn)
            for cell in assessed:
                gaps.observe(namespace, "methodology", assessment_id,
                             {k: cell[k] for k in ("date", "geography", "language", "topic")},
                             coverage_known=cell["status"] == "observed",
                             supports=[{"evidence_id": citation["revision_id"]}
                                       for source in cell["sources"] for citation in source["citations"]],
                             signals={"coverage_status": cell["status"], "reasons": cell["reasons"]},
                             principal_id=principal_id, scopes=scopes,
                             provenance={"assessment_id": assessment_id})
        return state

    def inspect(self, namespace: str, assessment_id: str, *, principal_id: str,
                scopes: set[str], limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 64 or type(offset) is not int or offset < 0:
            raise CoverageError("invalid_page", "bounded limit and offset required")
        row = self.conn.execute(
            "SELECT owner,state_json FROM investigation_coverage_assessments "
            "WHERE namespace=? AND assessment_id=?", [namespace, assessment_id]).fetchone()
        if not row:
            raise CoverageError("not_found", "coverage assessment is unavailable")
        self._auth(namespace, row[0], principal_id, scopes)
        state = json.loads(row[1])
        for cell in state["cells"]:
            for source in cell["sources"]:
                for citation in source["citations"]:
                    if "operator" not in scopes and f"document:{citation['document_id']}:read" not in scopes:
                        raise CoverageError("unauthorized", "current evidence access required")
        state["cells"] = state["cells"][offset:offset + limit]
        state["next_offset"] = offset + limit if offset + limit < state["denominator"] else None
        return state

    def compare(self, namespace: str, before_id: str, after_id: str, *,
                principal_id: str, scopes: set[str]) -> dict[str, Any]:
        before = self.inspect(namespace, before_id, principal_id=principal_id, scopes=scopes, limit=64)
        after = self.inspect(namespace, after_id, principal_id=principal_id, scopes=scopes, limit=64)
        old = {c["cell_id"]: c for c in before["cells"]}
        new = {c["cell_id"]: c for c in after["cells"]}
        scope_equal = before["scope"] == after["scope"]
        packs_equal = {(s["pack_id"], s["pack_version"], s["manifest_hash"]) for s in before["sources"]} == {
            (s["pack_id"], s["pack_version"], s["manifest_hash"]) for s in after["sources"]}
        credential_changed = any(
            "credential_missing" in c["reasons"] for c in before["cells"]
        ) != any("credential_missing" in c["reasons"] for c in after["cells"])
        changes = []
        for key in sorted(set(old) | set(new)):
            a, b = old.get(key), new.get(key)
            if a is None or b is None or a["status"] != b["status"] or a["reasons"] != b["reasons"]:
                improvement = bool(a and b and _ORDER[b["status"]] > _ORDER[a["status"]])
                cell_packs_equal = bool(a and b) and {
                    (s["pack_id"], s["pack_version"], s["manifest_hash"]) for s in a["sources"]
                } == {
                    (s["pack_id"], s["pack_version"], s["manifest_hash"]) for s in b["sources"]
                }
                cell_credential_changed = bool(a and b) and (
                    ("credential_missing" in a["reasons"]) != ("credential_missing" in b["reasons"])
                )
                changes.append({"cell_id": key, "before": a["status"] if a else None,
                                "after": b["status"] if b else None,
                                "classification": "scope_changed" if not scope_equal else
                                "pack_version_changed" if not cell_packs_equal else
                                "credential_availability_changed" if cell_credential_changed else
                                "coverage_improved" if improvement else "coverage_changed",
                                "before_reasons": a["reasons"] if a else [],
                                "after_reasons": b["reasons"] if b else []})
        return {"contract": COMPARISON_CONTRACT, "namespace": namespace,
                "before_id": before_id, "after_id": after_id, "scope_equal": scope_equal,
                "pack_versions_equal": packs_equal, "credential_availability_changed": credential_changed,
                "changes": changes, "denominators": {"before": before["denominator"], "after": after["denominator"]}}

    def export(self, namespace: str, before_id: str, after_id: str, *,
               principal_id: str, scopes: set[str]) -> dict[str, Any]:
        comparison = self.compare(namespace, before_id, after_id, principal_id=principal_id, scopes=scopes)
        before = self.inspect(namespace, before_id, principal_id=principal_id, scopes=scopes, limit=64)
        after = self.inspect(namespace, after_id, principal_id=principal_id, scopes=scopes, limit=64)
        return {**comparison, "scope": {"before": before["scope"], "after": after["scope"]},
                "assessed_dimensions": {"before": before["cells"], "after": after["cells"]},
                "receipts": {"before": before["sources"], "after": after["sources"]},
                "limitations": sorted(set(before["limitations"] + after["limitations"])),
                "report_ready": [
                    {"cell_id": c["cell_id"], "status": c["status"],
                     "missingness_reasons": c["reasons"],
                     "citations": [citation for source in c["sources"] for citation in source["citations"]]}
                    for c in after["cells"]
                ]}
