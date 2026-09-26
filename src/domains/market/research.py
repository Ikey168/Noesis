"""Evidence-linked company, industry, sizing, thesis, and brief artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
import time
import base64
from typing import Any
from src.domains.market.entitlements import recheck_stored_receipt_rights, withheld_receipt

RESEARCH_READ_SCOPE = "market:research:read"
RESEARCH_WRITE_SCOPE = "market:research:write"
MAX_ROWS = 500
MAX_EVIDENCE = 1000
ACCEPTANCE_REVIEW_CRITERIA = (
    "historical_cutoff_clear",
    "source_revisions_inspectable",
    "gaps_and_restrictions_visible",
    "deterministic_replay_credible",
    "five_company_comparison_useful",
    "performance_and_valuation_explanation_useful",
    "macro_scenario_useful",
    "supporting_and_contradicting_evidence_balanced",
    "export_usable",
)

_DDL = """
CREATE TABLE IF NOT EXISTS market_research_artifacts(
 namespace TEXT NOT NULL, artifact_id TEXT NOT NULL, kind TEXT NOT NULL,
 version BIGINT NOT NULL, owner TEXT NOT NULL, input_json TEXT NOT NULL,
 result_json TEXT NOT NULL, record_hash TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, artifact_id, version));
CREATE TABLE IF NOT EXISTS market_research_deliveries(
 namespace TEXT NOT NULL, delivery_key TEXT PRIMARY KEY, artifact_id TEXT NOT NULL,
 subscriber_id TEXT NOT NULL, status TEXT NOT NULL, attempts BIGINT NOT NULL,
 next_attempt_ms BIGINT, history_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS market_research_schedules(
 namespace TEXT NOT NULL, schedule_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
 report_id TEXT NOT NULL, cadence TEXT NOT NULL, next_due_ms BIGINT NOT NULL,
 request_json TEXT NOT NULL, status TEXT NOT NULL, last_run_id TEXT,
 created_at_ms BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_market_research_artifacts_owner
 ON market_research_artifacts(namespace, owner, kind, recorded_at_ms);
"""


class MarketResearchError(ValueError):
    """Typed market-research artifact failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def ensure_market_research_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MarketResearchError("invalid_request", "research input must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketResearchError("invalid_request", f"{name} must be bounded text")
    return value.strip()


def _millis(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketResearchError("invalid_request", f"{name} must be a nonnegative epoch millisecond")
    return value


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketResearchError("invalid_request", f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise MarketResearchError("invalid_request", f"{name} must be finite")
    return result


def _rows(value: Any, name: str, maximum: int = MAX_ROWS) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > maximum:
        raise MarketResearchError("bound_exceeded", f"{name} must contain at most {maximum} rows")
    result = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise MarketResearchError("invalid_request", f"{name}[{index}] must be an object")
        result.append(dict(row))
    return result


def _authorize(namespace: str, principal_id: str, scopes: set[str], *, write: bool) -> None:
    _text(namespace, "namespace", 100)
    _text(principal_id, "principal_id", 200)
    if "operator" in scopes:
        return
    required = RESEARCH_WRITE_SCOPE if write else RESEARCH_READ_SCOPE
    if required not in scopes or f"namespace:{namespace}:{'write' if write else 'read'}" not in scopes:
        raise MarketResearchError("unauthorized", "market research and namespace access is required")


def _owner(owner: str | None, principal_id: str, scopes: set[str]) -> str:
    if owner is None:
        return principal_id
    owner = _text(owner, "owner", 200)
    if owner != principal_id and "operator" not in scopes:
        raise MarketResearchError("unauthorized", "research artifact belongs to another principal")
    return owner


def _source_ids(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    values: set[str] = set()
    for row in rows:
        raw = row.get("source_revision_ids", [])
        if isinstance(raw, str):
            raw = [raw]
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            values.update(value for value in raw if isinstance(value, str) and value)
        singular = row.get("source_revision_id")
        if isinstance(singular, str) and singular:
            values.add(singular)
    return sorted(values)


def _chart_accessible_description(chart: Mapping[str, Any], index: int) -> str:
    supplied = chart.get("accessible_description")
    if supplied is not None:
        return _text(supplied, f"charts[{index}].accessible_description", 2000)
    series = chart.get("series", [])
    if not isinstance(series, list) or len(series) > 50:
        raise MarketResearchError("invalid_request", f"charts[{index}].series must contain at most 50 series")
    summaries: list[str] = []
    for series_index, item in enumerate(series):
        if not isinstance(item, Mapping):
            raise MarketResearchError("invalid_request", f"charts[{index}].series[{series_index}] must be an object")
        points = item.get("points", [])
        if not isinstance(points, list) or len(points) > 500:
            raise MarketResearchError("bound_exceeded", f"charts[{index}].series[{series_index}].points must contain at most 500 points")
        preview = "; ".join(_canonical(point) for point in points[:20]) or "no plotted values"
        if len(points) > 20:
            preview += f"; and {len(points) - 20} more points"
        summaries.append(f"{item.get('label', f'Series {series_index + 1}')}: {preview}")
    chart_type = chart.get("chart_type", "chart")
    return _text(
        f"{chart.get('title', f'Chart {index + 1}')} ({chart_type}). "
        + (" ".join(summaries) if summaries else "No plotted data is available."),
        f"charts[{index}].accessible_description",
        2000,
    )


class MarketResearchStore:
    """Persist versioned research artifacts without copying source authority."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_research_schema(conn)

    def _persist(self, namespace: str, artifact_id: str, kind: str, version: int, owner: str, input_payload: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        input_hash = _digest(input_payload)
        result = {
            **result,
            "artifact_id": artifact_id,
            "version": version,
            "owner": owner,
            "input_manifest": {"input_hash": input_hash, "kind": kind, "source_revision_ids": _source_ids(_rows(input_payload.get("source_rows", []), "source_rows")) if input_payload.get("source_rows") is not None else []},
        }
        record_hash = _digest(result)
        prior = self.conn.execute(
            "SELECT record_hash,result_json,recorded_at_ms FROM market_research_artifacts WHERE namespace=? AND artifact_id=? AND version=?",
            [namespace, artifact_id, version],
        ).fetchone()
        if prior:
            if prior[0] != record_hash:
                raise MarketResearchError("revision_conflict", "research artifact version is immutable")
            return {**json.loads(prior[1]), "record_hash": prior[0], "recorded_at_ms": int(prior[2]), "idempotent": True}
        recorded = _millis(self.now(), "recorded_at_ms")
        self.conn.execute(
            "INSERT INTO market_research_artifacts VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, artifact_id, kind, version, owner, _canonical(input_payload), _canonical(result), record_hash, recorded],
        )
        return {**result, "record_hash": record_hash, "recorded_at_ms": recorded, "idempotent": False}

    def inspect(self, namespace: str, artifact_id: str, version: int | None, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=False)
        query = "SELECT owner,result_json,record_hash,recorded_at_ms FROM market_research_artifacts WHERE namespace=? AND artifact_id=?"
        params: list[Any] = [namespace, _text(artifact_id, "artifact_id", 200)]
        if version is not None:
            query += " AND version=?"
            params.append(version)
        query += " ORDER BY version DESC LIMIT 1"
        row = self.conn.execute(query, params).fetchone()
        if row is None or (row[0] != principal_id and "operator" not in scopes):
            raise MarketResearchError("not_found", "research artifact is unavailable")
        result = json.loads(row[1])
        rights = recheck_stored_receipt_rights(
            self.conn, namespace, result, operation="derive",
            principal_id=principal_id, scopes=scopes, now_ms=int(self.now()),
            include_direct_refs=False,
        )
        if rights["state"] == "withheld":
            return withheld_receipt(
                "noesis-market-research-artifact-v1", "research_artifact", artifact_id, row[2], rights
            )
        # The artifact body stays byte-identical: exports recompute its hash.
        return {**result, "record_hash": row[2], "recorded_at_ms": int(row[3])}

    def save_materials(
        self,
        namespace: str,
        *,
        issuer_id: str,
        artifact_id: str,
        version: int,
        materials: Sequence[Mapping[str, Any]],
        cutoff_ms: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        issuer_id, artifact_id = _text(issuer_id, "issuer_id", 200), _text(artifact_id, "artifact_id", 200)
        cutoff_ms = _millis(cutoff_ms, "cutoff_ms")
        if type(version) is not int or version < 1:
            raise MarketResearchError("invalid_request", "version must be positive")
        rows = _rows(materials, "materials")
        normalized = []
        for index, row in enumerate(rows):
            item = dict(row)
            item["material_id"] = _text(item.get("material_id"), f"materials[{index}].material_id", 200)
            item["kind"] = _text(item.get("kind"), f"materials[{index}].kind", 60)
            item["published_at_ms"] = _millis(item.get("published_at_ms"), f"materials[{index}].published_at_ms")
            item["event_at_ms"] = None if item.get("event_at_ms") is None else _millis(item["event_at_ms"], f"materials[{index}].event_at_ms")
            item["document_locator"] = _text(item.get("document_locator"), f"materials[{index}].document_locator", 500)
            item["source_revision_id"] = _text(item.get("source_revision_id"), f"materials[{index}].source_revision_id", 300)
            status = _text(item.get("licensing_status", "unavailable"), f"materials[{index}].licensing_status", 40)
            if status not in {"public", "licensed", "private", "unlicensed", "unavailable"}:
                raise MarketResearchError("invalid_request", "unsupported material licensing status")
            item["licensing_status"] = status
            item["available"] = status in {"public", "licensed"} and item["published_at_ms"] <= cutoff_ms
            item["source_refs"] = item.get("source_refs", []) if isinstance(item.get("source_refs", []), list) else []
            normalized.append(item)
        normalized.sort(key=lambda item: (item["published_at_ms"], item["material_id"]))
        estimates = [item for item in normalized if item["kind"] in {"estimate", "consensus"}]
        corrections = [item for item in normalized if item.get("corrects_material_id")]
        result = {
            "contract": "noesis-market-materials-v1",
            "namespace": namespace,
            "issuer_id": issuer_id,
            "cutoff_ms": cutoff_ms,
            "materials": normalized,
            "pre_event_estimates": [item for item in estimates if item.get("event_at_ms") is None or item["published_at_ms"] <= item["event_at_ms"]],
            "corrections": corrections,
            # Dated management guidance in publication order, for comparing
            # what was guided before each later result. Statements are claims.
            "guidance_history": [
                {
                    "material_id": item["material_id"],
                    "reporting_period": item.get("reporting_period"),
                    "published_at_ms": item["published_at_ms"],
                    "statement_count": len(item.get("statements") or []),
                    "claim_status": item.get("claim_status", "management_claim"),
                }
                for item in normalized
                if item["kind"] == "guidance" and item["available"]
            ],
            "duplicates": [
                {"material_id": item["material_id"], "duplicate_of": item["duplicate_of"]}
                for item in normalized
                if item.get("duplicate_of")
            ],
            "coverage": {"total": len(normalized), "available": sum(1 for item in normalized if item["available"]), "unavailable": sum(1 for item in normalized if not item["available"])},
            "limitations": ["Material availability is determined from the supplied licensing status and cutoff; no private or unlicensed content is inferred.", "Document locators and source revisions remain authoritative outside this artifact."],
        }
        return self._persist(namespace, artifact_id, "materials", version, owner, {"issuer_id": issuer_id, "cutoff_ms": cutoff_ms, "materials": normalized, "source_rows": normalized}, result)

    def company_dossier(
        self,
        namespace: str,
        *,
        issuer_id: str,
        artifact_id: str,
        version: int,
        statements: Sequence[Mapping[str, Any]],
        materials: Sequence[Mapping[str, Any]],
        comparisons: Sequence[Mapping[str, Any]],
        evidence: Sequence[Mapping[str, Any]],
        cutoff_ms: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
        segment_disclosures: Sequence[Mapping[str, Any]] = (),
        ownership: Sequence[Mapping[str, Any]] = (),
        dated_peers: Sequence[Mapping[str, Any]] = (),
        headline_reconciliation: Sequence[Mapping[str, Any]] = (),
        input_gaps: Sequence[Mapping[str, Any]] = (),
        material_change_history: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        cutoff_ms = _millis(cutoff_ms, "cutoff_ms")
        statement_rows, material_rows, comparison_rows, evidence_rows = (_rows(value, name) for value, name in ((statements, "statements"), (materials, "materials"), (comparisons, "comparisons"), (evidence, "evidence")))

        def cutoff_linked_rows(value: Sequence[Mapping[str, Any]], name: str, *, require_as_of: bool = False) -> tuple[list[dict[str, Any]], dict[str, int]]:
            included: list[dict[str, Any]] = []
            excluded = {"missing_publication_clock": 0, "after_cutoff": 0, "missing_as_of_clock": 0, "as_of_after_cutoff": 0, "missing_source_revision": 0}
            for index, row in enumerate(_rows(value, name)):
                raw_public_at = row.get("public_at_ms", row.get("published_at_ms"))
                if raw_public_at is None:
                    excluded["missing_publication_clock"] += 1
                    continue
                public_at = _millis(raw_public_at, f"{name}[{index}].public_at_ms")
                if public_at > cutoff_ms:
                    excluded["after_cutoff"] += 1
                    continue
                item = {**row, "public_at_ms": public_at}
                if require_as_of:
                    raw_as_of = item.get("as_of_ms", item.get("observation_at_ms"))
                    if raw_as_of is None:
                        excluded["missing_as_of_clock"] += 1
                        continue
                    as_of = _millis(raw_as_of, f"{name}[{index}].as_of_ms")
                    if as_of > cutoff_ms:
                        excluded["as_of_after_cutoff"] += 1
                        continue
                    item["as_of_ms"] = as_of
                if not _source_ids([item]):
                    excluded["missing_source_revision"] += 1
                    continue
                included.append(item)
            return included, {key: count for key, count in excluded.items() if count}

        segment_rows, segment_excluded = cutoff_linked_rows(segment_disclosures, "segment_disclosures")
        ownership_rows, ownership_excluded = cutoff_linked_rows(ownership, "ownership", require_as_of=True)
        peer_rows, peer_excluded = cutoff_linked_rows(dated_peers, "dated_peers", require_as_of=True)
        headline_rows = _rows(headline_reconciliation, "headline_reconciliation")
        gap_rows = _rows(input_gaps, "input_gaps")
        change_rows = _rows(material_change_history, "material_change_history")
        calculated = []
        for row in comparison_rows:
            item = dict(row)
            actual = _number(item.get("actual"), "comparison.actual")
            prior = None if item.get("prior") is None else _number(item["prior"], "comparison.prior")
            item["verified_calculation"] = {"delta": None if prior is None else actual - prior, "pct_delta": None if prior in (None, 0) else actual / prior - 1.0}
            event_at = None if item.get("event_at_ms") is None else _millis(item["event_at_ms"], "comparison.event_at_ms")
            for key in ("guidance", "consensus"):
                if item.get(key) is not None:
                    estimate = item[key]
                    if not isinstance(estimate, Mapping):
                        raise MarketResearchError("invalid_request", f"comparison.{key} must be an object")
                    public_at = _millis(estimate.get("public_at_ms"), f"comparison.{key}.public_at_ms")
                    item[f"{key}_comparison"] = {"status": "available" if event_at is not None and public_at <= event_at else "unavailable_pre_event_clock", "delta": actual - _number(estimate.get("value"), f"comparison.{key}.value") if event_at is not None and public_at <= event_at else None}
            calculated.append(item)
        normalized_evidence = []
        for row in evidence_rows:
            stance = _text(row.get("stance"), "evidence.stance", 30)
            if stance not in {"supporting", "contradicting", "neutral", "uncertain"}:
                raise MarketResearchError("invalid_request", "unsupported evidence stance")
            normalized_evidence.append({**row, "stance": stance, "source_span": row.get("source_span"), "source_revision_id": row.get("source_revision_id")})
        source_gaps = []
        if not material_rows:
            source_gaps.append("earnings_materials_missing")
        if not any(item.get("consensus") for item in calculated):
            source_gaps.append("pre_event_consensus_missing")
        if not segment_rows:
            source_gaps.append("segment_disclosures_missing")
        if not ownership_rows:
            source_gaps.append("ownership_snapshot_missing")
        if not peer_rows:
            source_gaps.append("dated_peers_missing")
        source_gaps.extend(
            str(row["code"])
            for row in gap_rows
            if isinstance(row.get("code"), str) and row["code"]
        )
        source_gaps = list(dict.fromkeys(source_gaps))
        result = {
            "contract": "noesis-market-company-dossier-v1",
            "namespace": namespace,
            "issuer_id": _text(issuer_id, "issuer_id", 200),
            "cutoff_ms": cutoff_ms,
            "statements": statement_rows,
            "materials": material_rows,
            "segment_disclosures": segment_rows,
            "ownership": ownership_rows,
            "dated_peers": peer_rows,
            "comparisons": calculated,
            "evidence": normalized_evidence,
            "headline_reconciliation": headline_rows,
            "material_change_history": change_rows,
            "source_coverage": {
                "segment_disclosures": {"included": len(segment_rows), "excluded": segment_excluded},
                "ownership": {"included": len(ownership_rows), "excluded": ownership_excluded},
                "dated_peers": {"included": len(peer_rows), "excluded": peer_excluded},
            },
            "uncertainty": {"source_gaps": source_gaps, "gap_details": gap_rows, "management_claims": [row for row in normalized_evidence if row.get("claim_kind") == "management"], "verified_calculations": [row.get("metric") for row in calculated]},
            "supporting_source_spans": [row for row in normalized_evidence if row["stance"] == "supporting"],
            "contradicting_source_spans": [row for row in normalized_evidence if row["stance"] == "contradicting"],
            "limitations": ["Management claims are not treated as verified calculations.", "Comparisons require a source timestamp at or before the event cutoff; late or unavailable estimates stay unavailable.", "Segment disclosures, ownership snapshots, and dated peers without a source revision and eligible publication/as-of clocks are omitted and counted in source_coverage."],
        }
        return self._persist(namespace, _text(artifact_id, "artifact_id", 200), "company-dossier", version, owner, {"issuer_id": issuer_id, "cutoff_ms": cutoff_ms, "statements": statement_rows, "materials": material_rows, "comparisons": comparison_rows, "evidence": evidence_rows, "segment_disclosures": segment_rows, "ownership": ownership_rows, "dated_peers": peer_rows, "headline_reconciliation": headline_rows, "input_gaps": gap_rows, "material_change_history": change_rows, "source_rows": [*material_rows, *evidence_rows, *segment_rows, *ownership_rows, *peer_rows]}, result)

    def industry_model(
        self,
        namespace: str,
        *,
        artifact_id: str,
        version: int,
        profiles: Sequence[Mapping[str, Any]],
        relationships: Sequence[Mapping[str, Any]],
        as_of_ms: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        as_of_ms = _millis(as_of_ms, "as_of_ms")
        profile_rows = _rows(profiles, "profiles")
        relationship_rows = _rows(relationships, "relationships")
        normalized = []
        for index, row in enumerate(relationship_rows):
            item = dict(row)
            item["relationship_id"] = _text(item.get("relationship_id", f"relationship-{index + 1}"), "relationship_id", 200)
            item["relationship_type"] = _text(item.get("relationship_type"), "relationship_type", 60)
            item["subject_id"] = _text(item.get("subject_id"), "subject_id", 200)
            item["object_id"] = _text(item.get("object_id"), "object_id", 200)
            item["evidence_kind"] = _text(item.get("evidence_kind", "asserted"), "evidence_kind", 30)
            if item["evidence_kind"] not in {"asserted", "extracted", "analyst_inferred"}:
                raise MarketResearchError("invalid_request", "unsupported relationship evidence kind")
            item["valid_from_ms"] = _millis(item.get("valid_from_ms", 0), "relationship.valid_from_ms")
            item["valid_to_ms"] = None if item.get("valid_to_ms") is None else _millis(item["valid_to_ms"], "relationship.valid_to_ms")
            item["state"] = "expired" if item["valid_to_ms"] is not None and as_of_ms >= item["valid_to_ms"] else "active" if as_of_ms >= item["valid_from_ms"] else "future"
            normalized.append(item)
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for item in normalized:
            groups.setdefault((item["subject_id"], item["relationship_type"], item["object_id"]), []).append(item)
        contradictions = [
            {"key": list(key), "relationship_ids": [item["relationship_id"] for item in values], "status": "contradictory_sources"}
            for key, values in groups.items()
            if len({str(item.get("stance", "asserted")) for item in values}) > 1
        ]
        result = {
            "contract": "noesis-market-industry-model-v1",
            "namespace": namespace,
            "as_of_ms": as_of_ms,
            "profiles": profile_rows,
            "relationships": normalized,
            "active_relationships": [item for item in normalized if item["state"] == "active"],
            "contradictions": contradictions,
            "ambiguities": [item for item in normalized if item.get("ambiguous")],
            "limitations": ["Relationships retain asserted, extracted, and analyst-inferred provenance separately.", "Private-company coverage and product/pricing completeness depend on supplied public evidence."],
        }
        return self._persist(namespace, _text(artifact_id, "artifact_id", 200), "industry-model", version, owner, {"profiles": profile_rows, "relationships": relationship_rows, "as_of_ms": as_of_ms, "source_rows": relationship_rows}, result)

    def market_sizing(
        self,
        namespace: str,
        *,
        artifact_id: str,
        version: int,
        model_type: str,
        segments: Sequence[Mapping[str, Any]],
        scenarios: Mapping[str, Any] | None,
        sensitivity: Sequence[Mapping[str, Any]] | None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        model_type = _text(model_type, "model_type", 30)
        if model_type not in {"top_down", "bottom_up"}:
            raise MarketResearchError("invalid_request", "model_type must be top_down or bottom_up")
        rows = _rows(segments, "segments", 200)
        seen_ids: set[str] = set()
        overlap_groups: dict[str, list[str]] = {}
        calculated = []
        for index, row in enumerate(rows):
            item = dict(row)
            segment_id = _text(item.get("segment_id", f"segment-{index + 1}"), "segment_id", 100)
            if segment_id in seen_ids:
                raise MarketResearchError("invalid_request", "segment IDs must be unique")
            seen_ids.add(segment_id)
            group = item.get("overlap_group")
            if group:
                overlap_groups.setdefault(str(group), []).append(segment_id)
            for field in ("geography", "customer_segment", "unit", "period"):
                item[field] = _text(item.get(field), f"segment.{field}", 100)
            kind = _text(item.get("value_kind", "estimate"), "segment.value_kind", 20)
            if kind not in {"observed", "estimate", "assumption"}:
                raise MarketResearchError("invalid_request", "value_kind must be observed, estimate, or assumption")
            if model_type == "top_down":
                base = _number(item.get("market_size"), "segment.market_size")
                serviceable = _number(item.get("serviceable_share", 1), "segment.serviceable_share")
                obtainable = _number(item.get("obtainable_share", 1), "segment.obtainable_share")
                if min(base, serviceable, obtainable) < 0 or max(serviceable, obtainable) > 1:
                    raise MarketResearchError("invalid_request", "top-down size and shares are outside bounds")
                values = {"tam": base, "sam": base * serviceable, "som": base * serviceable * obtainable}
            else:
                customers = _number(item.get("customers"), "segment.customers")
                price = _number(item.get("annual_price"), "segment.annual_price")
                adoption = _number(item.get("adoption", 1), "segment.adoption")
                if min(customers, price, adoption) < 0 or adoption > 1:
                    raise MarketResearchError("invalid_request", "bottom-up assumptions are outside bounds")
                values = {"tam": customers * price, "sam": customers * price * adoption, "som": customers * price * adoption * _number(item.get("capture", 1), "segment.capture")}
            calculated.append({**item, "values": values})
        overlapping = [group for group, members in overlap_groups.items() if len(members) > 1 and not all(next(item for item in calculated if item["segment_id"] == member).get("exclusive", False) for member in members)]
        if overlapping:
            raise MarketResearchError("overlapping_segments", "segments overlap without an explicit exclusive declaration", groups=overlapping)
        totals = {key: sum(item["values"][key] for item in calculated) for key in ("tam", "sam", "som")}
        scenario_results = {}
        for name, raw in (scenarios or {"base": {}}).items():
            if not isinstance(raw, Mapping):
                raise MarketResearchError("invalid_request", "scenario must be an object")
            multiplier = _number(raw.get("multiplier", 1), f"scenario.{name}.multiplier")
            scenario_results[str(name)] = {key: value * multiplier for key, value in totals.items()}
        sensitivity_results = []
        for row in _rows(sensitivity or [], "sensitivity", 100):
            parameter = _text(row.get("parameter"), "sensitivity.parameter", 100)
            low = _number(row.get("low"), "sensitivity.low")
            high = _number(row.get("high"), "sensitivity.high")
            sensitivity_results.append({"parameter": parameter, "low": low, "base": totals["som"], "high": high, "formula_version": "noesis-market-sizing-sensitivity-v1"})
        result = {
            "contract": "noesis-market-sizing-v1",
            "namespace": namespace,
            "model_type": model_type,
            "segments": calculated,
            "totals": totals,
            "scenarios": scenario_results,
            "sensitivity": sensitivity_results,
            "definitions": {"tam": "total addressable market", "sam": "serviceable available market", "som": "serviceable obtainable market"},
            "limitations": ["Observed values, estimates, and assumptions are labeled separately.", "Segment totals are only additive after overlap groups pass the explicit exclusivity check.", "Sizing is an assumption model, not a forecast or causal claim."],
        }
        return self._persist(namespace, _text(artifact_id, "artifact_id", 200), "market-sizing", version, owner, {"model_type": model_type, "segments": calculated, "scenarios": scenarios or {}, "sensitivity": sensitivity or [], "source_rows": rows}, result)

    def driver_hypotheses(
        self,
        namespace: str,
        *,
        artifact_id: str,
        version: int,
        hypotheses: Sequence[Mapping[str, Any]],
        as_of_ms: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        as_of_ms = _millis(as_of_ms, "as_of_ms")
        rows = _rows(hypotheses, "hypotheses", 200)
        normalized = []
        for index, row in enumerate(rows):
            item = dict(row)
            item["hypothesis_id"] = _text(item.get("hypothesis_id", f"hypothesis-{index + 1}"), "hypothesis_id", 100)
            item["statement"] = _text(item.get("statement"), "hypothesis.statement", 1000)
            item["driver_types"] = list(item.get("driver_types", [])) if isinstance(item.get("driver_types", []), list) else []
            item["time_horizon"] = _text(item.get("time_horizon"), "hypothesis.time_horizon", 100)
            item["evidence"] = _rows(item.get("evidence", []), "hypothesis.evidence", MAX_EVIDENCE)
            for evidence in item["evidence"]:
                evidence["stance"] = _text(evidence.get("stance", "uncertain"), "hypothesis.evidence.stance", 30)
                if evidence["stance"] not in {"supporting", "contradicting", "uncertain"}:
                    raise MarketResearchError("invalid_request", "unsupported hypothesis evidence stance")
            normalized.append(item)
        result = {
            "contract": "noesis-market-driver-hypotheses-v1",
            "namespace": namespace,
            "as_of_ms": as_of_ms,
            "hypotheses": normalized,
            "transmission": [{"hypothesis_id": item["hypothesis_id"], "status": "descriptive_association", "alternatives": item.get("alternatives", [])} for item in normalized],
            "supporting_evidence": [evidence for item in normalized for evidence in item["evidence"] if evidence["stance"] == "supporting"],
            "contradicting_evidence": [evidence for item in normalized for evidence in item["evidence"] if evidence["stance"] == "contradicting"],
            "limitations": ["Transmission hypotheses distinguish association from causal findings.", "Policy and regulation claims retain supplied source revisions and are not silently upgraded to causal evidence."],
        }
        return self._persist(namespace, _text(artifact_id, "artifact_id", 200), "driver-hypotheses", version, owner, {"hypotheses": normalized, "as_of_ms": as_of_ms, "source_rows": [evidence for item in normalized for evidence in item["evidence"]]}, result)

    def save_thesis(
        self,
        namespace: str,
        *,
        thesis_id: str,
        version: int,
        question: str,
        thesis: str,
        alternatives: Sequence[str],
        catalysts: Sequence[str],
        horizon: str,
        assumptions: Sequence[str],
        evidence: Sequence[Mapping[str, Any]],
        falsification_conditions: Sequence[Mapping[str, Any]],
        watch_ids: Sequence[str] | None,
        integration_links: Mapping[str, Any] | None = None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        thesis_id = _text(thesis_id, "thesis_id", 200)
        if type(version) is not int or version < 1:
            raise MarketResearchError("invalid_request", "thesis version must be positive")
        evidence_rows = _rows(evidence, "evidence", MAX_EVIDENCE)
        conditions = _rows(falsification_conditions, "falsification_conditions", 100)
        body = {"question": _text(question, "question", 1000), "thesis": _text(thesis, "thesis", 2000), "alternatives": list(alternatives)[:20], "catalysts": list(catalysts)[:20], "horizon": _text(horizon, "horizon", 100), "assumptions": list(assumptions)[:50], "evidence": evidence_rows, "falsification_conditions": conditions, "watch_ids": list(watch_ids or [])[:50], "workflow_links": dict(integration_links or {})}
        result = {"contract": "noesis-market-thesis-v1", "namespace": namespace, "thesis_id": thesis_id, "version": version, **body, "status": "active", "review_state": "unreviewed", "limitations": ["Evidence watches propose review; they never silently overwrite an analyst conclusion.", "Supporting, contradicting, and insufficient evidence remain distinct until an owner records a new thesis revision."]}
        return self._persist(namespace, thesis_id, "thesis", version, owner, {**body, "source_rows": evidence_rows}, result)

    def review_thesis(
        self,
        namespace: str,
        *,
        thesis_id: str,
        base_version: int,
        evidence: Sequence[Mapping[str, Any]],
        proposed_revision: str | None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        evidence_rows = _rows(evidence, "evidence", MAX_EVIDENCE)
        supporting = sum(1 for item in evidence_rows if item.get("stance") == "supporting")
        contradicting = sum(1 for item in evidence_rows if item.get("stance") == "contradicting")
        state = "supporting" if supporting and not contradicting else "contradicting" if contradicting and not supporting else "insufficient_or_mixed"
        result = {"contract": "noesis-market-thesis-review-v1", "namespace": namespace, "thesis_id": _text(thesis_id, "thesis_id", 200), "base_version": base_version, "review_state": state, "supporting_count": supporting, "contradicting_count": contradicting, "evidence": evidence_rows, "proposed_revision": proposed_revision, "revision_required": bool(proposed_revision) or state == "contradicting", "conclusion_overwritten": False, "limitations": ["A review creates a proposed change; it does not mutate the base thesis."]}
        return self._persist(namespace, f"{thesis_id}:review", "thesis-review", base_version, owner, {"thesis_id": thesis_id, "base_version": base_version, "evidence": evidence_rows, "source_rows": evidence_rows}, result)

    def _compose_brief_artifacts(
        self,
        namespace: str,
        artifact_refs: Sequence[Mapping[str, Any]],
        *,
        cutoff_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        sections: list[dict[str, Any]] = []
        formulas: set[str] = set()
        assumptions: set[str] = set()
        locators: dict[str, dict[str, Any]] = {}
        charts: list[dict[str, Any]] = []
        pinned_refs: list[dict[str, Any]] = []
        restricted_revision_ids: set[str] = set()
        supported = {
            "noesis-market-company-dossier-v1",
            "noesis-market-industry-model-v1",
            "noesis-market-sizing-v1",
            "noesis-market-thesis-v1",
        }

        def add_locator(locator: Mapping[str, Any], fallback_id: str | None = None) -> None:
            allowed_keys = {
                "source_ref_id", "provider", "provider_object_id", "source_revision_id",
                "public_at_ms", "source_snapshot_id", "source_url", "retrieved_at_ms",
                "content_hash", "license_id", "entitlement_id", "locator",
            }
            item = {key: locator[key] for key in allowed_keys if key in locator}
            revision = item.get("source_revision_id") or fallback_id
            identity = str(item.get("source_ref_id") or revision or item.get("locator") or _digest(item))
            item.setdefault("source_revision_id", revision)
            item.setdefault("locator", f"revision:{revision}" if revision else identity)
            if revision:
                matching = [
                    key for key, existing in locators.items()
                    if existing.get("source_revision_id") == revision
                ]
                if item.get("source_ref_id"):
                    for key in matching:
                        if not locators[key].get("source_ref_id"):
                            locators.pop(key)
                elif any(locators[key].get("source_ref_id") for key in matching):
                    return
            locators[identity] = item

        def collect_sources(value: Any, inherited_locator: str | None = None) -> None:
            if isinstance(value, Mapping):
                if value.get("available") is False or value.get("licensing_status") in {"private", "unlicensed", "unavailable"}:
                    return
                local_locator = next(
                    (value.get(key) for key in ("locator", "document_locator", "source_locator", "source_span") if isinstance(value.get(key), str) and value.get(key)),
                    inherited_locator,
                )
                for revision in _source_ids([value]):
                    add_locator({"source_revision_id": revision, "locator": local_locator or f"revision:{revision}"})
                refs = value.get("source_refs")
                if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                    for ref in refs:
                        if isinstance(ref, Mapping):
                            add_locator({**dict(ref), "locator": local_locator or ref.get("source_url") or ref.get("source_ref_id", "source reference")})
                for key, child in value.items():
                    if key not in {"source_refs", "input_manifest", "source_revision_ids"}:
                        collect_sources(child, local_locator)
                manifest = value.get("input_manifest")
                if isinstance(manifest, Mapping) and isinstance(manifest.get("source_revision_ids"), list):
                    for revision in manifest["source_revision_ids"]:
                        if isinstance(revision, str) and revision and revision not in restricted_revision_ids:
                            add_locator({"source_revision_id": revision, "locator": f"revision:{revision}"})
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for child in value:
                    collect_sources(child, inherited_locator)

        def mark_restricted_sources(value: Any) -> None:
            if isinstance(value, Mapping):
                if value.get("available") is False or value.get("licensing_status") in {"private", "unlicensed", "unavailable"}:
                    restricted_revision_ids.update(_source_ids([value]))
                    refs = value.get("source_refs")
                    if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
                        restricted_revision_ids.update(
                            str(ref["source_revision_id"])
                            for ref in refs
                            if isinstance(ref, Mapping) and isinstance(ref.get("source_revision_id"), str)
                        )
                    return
                for key, child in value.items():
                    if key not in {"source_refs", "input_manifest"}:
                        mark_restricted_sources(child)
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                for child in value:
                    mark_restricted_sources(child)

        for index, raw_ref in enumerate(_rows(artifact_refs, "artifact_refs", 100)):
            artifact_id = _text(raw_ref.get("artifact_id"), f"artifact_refs[{index}].artifact_id", 200)
            version = raw_ref.get("version")
            if type(version) is not int or version < 1:
                raise MarketResearchError("invalid_request", f"artifact_refs[{index}].version must be positive")
            artifact = self.inspect(namespace, artifact_id, version, principal_id=principal_id, scopes=scopes)
            contract = artifact.get("contract")
            if contract not in supported:
                raise MarketResearchError("unsupported_artifact", "brief composition supports company dossiers, industry models, sizing models, and theses", artifact_id=artifact_id, contract=contract)
            source_cutoff = artifact.get("cutoff_ms", artifact.get("as_of_ms"))
            if source_cutoff is not None and _millis(source_cutoff, f"{artifact_id}.cutoff_ms") > cutoff_ms:
                raise MarketResearchError("future_artifact", "linked artifact is newer than the brief cutoff", artifact_id=artifact_id)
            pinned_refs.append({"artifact_id": artifact_id, "version": version, "contract": contract, "record_hash": artifact["record_hash"]})
            mark_restricted_sources(artifact)
            collect_sources(artifact)
            label = str(artifact.get("issuer_id") or artifact.get("thesis_id") or artifact_id)
            if contract == "noesis-market-company-dossier-v1":
                comparisons = artifact.get("comparisons", [])
                comparison_text = []
                for item in comparisons:
                    if not isinstance(item, Mapping):
                        continue
                    calc = item.get("verified_calculation", {})
                    comparison_text.append(
                        f"{item.get('metric', 'metric')}: actual {item.get('actual', 'unavailable')}, prior {item.get('prior', 'unavailable')}, delta {calc.get('delta', 'unavailable')}"
                    )
                uncertainty = artifact.get("uncertainty", {})
                gaps = uncertainty.get("source_gaps", []) if isinstance(uncertainty, Mapping) else []
                evidence = artifact.get("evidence", [])
                stance_counts = {
                    stance: sum(1 for row in evidence if isinstance(row, Mapping) and row.get("stance") == stance)
                    for stance in ("supporting", "contradicting", "uncertain", "neutral")
                }
                claims = [str(row.get("claim")) for row in evidence if isinstance(row, Mapping) and row.get("claim_kind") == "management" and row.get("claim")]
                body = (
                    f"Stored company dossier for {label}: {len(artifact.get('statements', []))} statement rows, "
                    f"{len(artifact.get('segment_disclosures', []))} dated segment disclosures, "
                    f"{len(artifact.get('ownership', []))} eligible ownership snapshots, and "
                    f"{len(artifact.get('dated_peers', []))} dated peers. "
                    f"Comparisons: {'; '.join(comparison_text) or 'none available'}. "
                    f"Evidence stance counts: {json.dumps(stance_counts, sort_keys=True)}. "
                    f"Management claims (not verified calculations): {'; '.join(claims) or 'none recorded'}. "
                    f"Source gaps: {', '.join(map(str, gaps)) or 'none recorded'}."
                )
                if comparisons:
                    formulas.add("noesis-market-dossier-delta-v1")
                    points = []
                    for item in comparisons:
                        if isinstance(item, Mapping) and isinstance(item.get("actual"), (int, float)):
                            points.append([str(item.get("metric", "metric")), item["actual"]])
                    if points:
                        charts.append({"chart_id": f"{artifact_id}:reported-results", "chart_type": "bar", "title": f"Reported results · {label}", "series": [{"label": "Actual", "points": points}], "source_revision_ids": _source_ids(comparisons)})
            elif contract == "noesis-market-industry-model-v1":
                active = artifact.get("active_relationships", [])
                body = f"Dated industry model as of {artifact.get('as_of_ms')}: {len(artifact.get('profiles', []))} profiles, {len(active)} active relationships, {len(artifact.get('contradictions', []))} contradictory source groups, and {len(artifact.get('ambiguities', []))} ambiguities."
            elif contract == "noesis-market-sizing-v1":
                totals = artifact.get("totals", {})
                scenario_names = sorted(artifact.get("scenarios", {}))
                body = f"{artifact.get('model_type', 'market')} sizing totals: {json.dumps(totals, sort_keys=True)}. Scenarios: {', '.join(scenario_names) or 'none recorded'}. Segment values retain observed/estimate/assumption labels."
                formulas.add("noesis-market-sizing-v1")
                points = [[key.upper(), value] for key, value in totals.items() if key in {"tam", "sam", "som"} and isinstance(value, (int, float))]
                if points:
                    charts.append({"chart_id": f"{artifact_id}:sizing-totals", "chart_type": "bar", "title": f"Market sizing · {artifact_id}", "series": [{"label": "Base", "points": points}], "source_revision_ids": _source_ids(artifact.get("segments", []))})
                for segment in artifact.get("segments", []):
                    if isinstance(segment, Mapping) and segment.get("value_kind") == "assumption":
                        assumptions.add(f"Sizing segment {segment.get('segment_id', 'unknown')} uses assumed inputs.")
            else:
                body = (
                    f"Versioned thesis for {label}: {artifact.get('thesis', 'not stated')} "
                    f"(horizon {artifact.get('horizon', 'not stated')}). Alternatives: "
                    f"{'; '.join(map(str, artifact.get('alternatives', []))) or 'none recorded'}. "
                    f"Falsification conditions: {len(artifact.get('falsification_conditions', []))}. Evidence remains separately attributed."
                )
                assumptions.update(str(item) for item in artifact.get("assumptions", []) if isinstance(item, str) and item)
            sections.append({"heading": f"{contract.removeprefix('noesis-market-').removesuffix('-v1').replace('-', ' ').title()} · {label}", "body": body})

        if not pinned_refs:
            raise MarketResearchError("invalid_request", "artifact composition requires at least one linked artifact")
        if not sections:
            raise MarketResearchError("invalid_request", "no supported artifact sections were generated")
        return {
            "sections": sections,
            "formula_versions": sorted(formulas),
            "assumptions": sorted(assumptions),
            "source_locators": list(locators.values())[:MAX_EVIDENCE],
            "charts": charts[:100],
            "artifact_refs": pinned_refs,
        }

    def generate_brief(
        self,
        namespace: str,
        *,
        report_id: str,
        version: int,
        title: str,
        sections: Sequence[Mapping[str, Any]],
        cutoff_ms: int,
        formula_versions: Sequence[str],
        assumptions: Sequence[str],
        source_locators: Sequence[Mapping[str, Any]],
        charts: Sequence[Mapping[str, Any]] | None = None,
        artifact_refs: Sequence[Mapping[str, Any]] | None = None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
        compose_artifacts: bool = False,
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        cutoff_ms = _millis(cutoff_ms, "cutoff_ms")
        if type(compose_artifacts) is not bool:
            raise MarketResearchError("invalid_request", "compose_artifacts must be boolean")
        section_rows = _rows(sections, "sections", 100)
        locator_rows = _rows(source_locators, "source_locators", MAX_EVIDENCE)
        chart_rows = _rows(charts or [], "charts", 100)
        artifact_rows = _rows(artifact_refs or [], "artifact_refs", 100)
        if compose_artifacts:
            composed = self._compose_brief_artifacts(namespace, artifact_rows, cutoff_ms=cutoff_ms, principal_id=principal_id, scopes=scopes)
            section_rows = [*composed["sections"], *section_rows]
            formula_versions = list(dict.fromkeys([*composed["formula_versions"], *formula_versions]))
            assumptions = list(dict.fromkeys([*composed["assumptions"], *assumptions]))
            locator_rows = [*composed["source_locators"], *locator_rows]
            chart_rows = [*composed["charts"], *chart_rows]
            artifact_rows = composed["artifact_refs"]
        if any(not isinstance(item, str) or not item for item in formula_versions):
            raise MarketResearchError("invalid_request", "formula_versions must contain text")
        for index, chart in enumerate(chart_rows):
            chart["chart_id"] = _text(chart.get("chart_id"), f"charts[{index}].chart_id", 200)
            chart["chart_type"] = _text(chart.get("chart_type", "line"), f"charts[{index}].chart_type", 30)
            if chart["chart_type"] not in {"line", "bar", "scatter", "area", "table"}:
                raise MarketResearchError("invalid_request", "unsupported market chart type")
            chart["title"] = _text(chart.get("title", chart["chart_id"]), f"charts[{index}].title", 300)
            if not isinstance(chart.get("series", []), list):
                raise MarketResearchError("invalid_request", f"charts[{index}].series must be a list")
            chart["accessible_description"] = _chart_accessible_description(chart, index)
            chart["source_revision_ids"] = sorted({value for value in chart.get("source_revision_ids", []) if isinstance(value, str) and value})
        for index, artifact in enumerate(artifact_rows):
            artifact["artifact_id"] = _text(artifact.get("artifact_id"), f"artifact_refs[{index}].artifact_id", 200)
            if type(artifact.get("version")) is not int or artifact["version"] < 1:
                raise MarketResearchError("invalid_request", f"artifact_refs[{index}].version must be positive")
        lines = [f"# {_text(title, 'title', 300)}", "", f"Historical cutoff: `{cutoff_ms}`", ""]
        for section in section_rows:
            heading = _text(section.get("heading"), "section.heading", 200)
            body = _text(section.get("body"), "section.body", 10000)
            lines.extend([f"## {heading}", "", body, ""])
        if chart_rows:
            lines.extend(["## Charts", ""])
            for chart in chart_rows:
                lines.extend([
                    f"### {chart['title']}",
                    "",
                    f"{chart['chart_type'].title()} chart. {chart['accessible_description']}",
                    "",
                ])
        if artifact_rows:
            lines.extend(["## Linked artifacts", "", *[f"- `{item['artifact_id']}` revision `{item['version']}`" for item in artifact_rows], ""])
        lines.extend(["## Assumptions", "", *[f"- {item}" for item in assumptions], "", "## Sources", "", *[f"- {item.get('locator', item.get('source_revision_id', 'unlocated'))}" for item in locator_rows]])
        result = {
            "contract": "noesis-market-brief-v1",
            "namespace": namespace,
            "report_id": _text(report_id, "report_id", 200),
            "version": version,
            "title": title,
            "cutoff_ms": cutoff_ms,
            "sections": section_rows,
            "formula_versions": list(formula_versions)[:100],
            "assumptions": list(assumptions)[:100],
            "source_locators": locator_rows,
            "charts": chart_rows,
            "artifact_refs": artifact_rows,
            "generation_mode": "artifact_composition" if compose_artifacts else "authored_sections",
            "formats": {"json": True, "markdown": "\n".join(lines), "csv": "section,body\n" + "\n".join(json.dumps([item.get("heading"), item.get("body")], ensure_ascii=False) for item in section_rows)},
            "rights": {"source_payloads_included": False, "derived_output": True},
            "limitations": ["The brief distinguishes supplied facts, calculations, and assumptions through its sections and formula list.", "Source locators are retained; unlicensed source payloads are never copied into the export."],
        }
        return self._persist(namespace, _text(report_id, "report_id", 200), "market-brief", version, owner, {"title": title, "cutoff_ms": cutoff_ms, "sections": section_rows, "charts": chart_rows, "artifact_refs": artifact_rows, "formula_versions": list(formula_versions), "assumptions": list(assumptions), "source_rows": [*locator_rows, *chart_rows]}, result)

    def deliver_brief(
        self,
        namespace: str,
        *,
        report_id: str,
        version: int,
        subscriber_id: str,
        delivery_outcome: str,
        retry_delay_ms: int,
        cooldown_ms: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        report = self.inspect(namespace, report_id, version, principal_id=principal_id, scopes=scopes)
        subscriber_id = _text(subscriber_id, "subscriber_id", 200)
        if type(retry_delay_ms) is not int or retry_delay_ms < 0 or type(cooldown_ms) is not int or cooldown_ms < 1:
            raise MarketResearchError("invalid_request", "delivery delays must be bounded integers")
        key = self.brief_delivery_key(namespace, report, report_id, version, subscriber_id, cooldown_ms)
        prior = self.conn.execute("SELECT status,attempts,next_attempt_ms,history_json FROM market_research_deliveries WHERE delivery_key=?", [key]).fetchone()
        if prior and not (prior[0] == "retrying" and prior[2] is not None and self.now() >= int(prior[2])):
            return {"contract": "noesis-market-brief-delivery-v1", "delivery_key": key, "status": prior[0], "attempts": int(prior[1]), "deduplicated": True, "history": json.loads(prior[3])}
        attempts = 1 if not prior else int(prior[1]) + 1
        # "withheld" (current rights forbid the export) is terminal: retrying
        # cannot succeed until rights change, and a new schedule run will retry.
        status = {"delivered": "delivered", "withheld": "withheld"}.get(delivery_outcome, "retrying")
        next_attempt = None if status != "retrying" else _millis(self.now(), "now_ms") + retry_delay_ms
        history = [] if not prior else json.loads(prior[3])
        history.append({"status": status, "at_ms": self.now(), "outcome": delivery_outcome})
        self.conn.execute("INSERT INTO market_research_deliveries VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(delivery_key) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,next_attempt_ms=excluded.next_attempt_ms,history_json=excluded.history_json", [namespace, key, f"{report_id}@{version}", subscriber_id, status, attempts, next_attempt, _canonical(history), self.now()])
        return {"contract": "noesis-market-brief-delivery-v1", "delivery_key": key, "report_id": report_id, "version": version, "subscriber_id": subscriber_id, "status": status, "attempts": attempts, "next_attempt_ms": next_attempt, "history": history, "deduplicated": False}

    @staticmethod
    def brief_delivery_key(
        namespace: str,
        report: Mapping[str, Any],
        report_id: str,
        version: int,
        subscriber_id: str,
        cooldown_ms: int,
    ) -> str:
        """Stable per-cooldown delivery identity, also used as Idempotency-Key."""
        bucket = int(report.get("recorded_at_ms", 0)) // cooldown_ms
        return _digest([namespace, report_id, version, subscriber_id, bucket])

    def export_brief(
        self,
        namespace: str,
        *,
        report_id: str,
        version: int | None,
        output_format: str,
        external: bool,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Export an authorized brief in native or optional formatted formats."""
        _authorize(namespace, principal_id, scopes, write=False)
        owner = _owner(owner, principal_id, scopes)
        output_format = _text(output_format, "output_format", 20).lower()
        if output_format not in {"json", "markdown", "csv", "docx", "pdf"}:
            raise MarketResearchError("invalid_request", "output_format must be json, markdown, csv, docx, or pdf")
        report = self.inspect(namespace, _text(report_id, "report_id", 200), version, principal_id=principal_id, scopes=scopes)
        source_locators = report.get("source_locators", [])
        entitlement_refs = [
            dict(item)
            for item in source_locators
            if isinstance(item, Mapping) and {"entitlement_id", "license_id", "provider"} <= set(item)
        ]
        rights: dict[str, Any] = {
            "status": "not_provided",
            "external": bool(external),
            "decisions": [],
            "export_withheld": False,
        }
        limitations = list(report.get("limitations", []))
        if source_locators and len(entitlement_refs) != len(source_locators):
            rights["status"] = "unverified"
            limitations.append(
                "One or more source locators lack provider entitlement metadata; export rights are not inferred."
            )
        elif not source_locators:
            limitations.append(
                "No source locators were supplied, so export rights cannot be verified."
            )
        elif entitlement_refs:
            from src.domains.market.entitlements import MarketEntitlementStore

            rights["decisions"] = MarketEntitlementStore(self.conn, initialize=True, now=self.now).authorize_sources(
                namespace,
                entitlement_refs,
                operation="export",
                external=external,
                principal_id=principal_id,
                scopes=scopes,
                now_ms=_millis(self.now(), "now_ms"),
            )
            rights["status"] = "authorized"
        if external and rights["status"] != "authorized":
            rights["status"] = "withheld"
            rights["export_withheld"] = True
            limitations.append(
                "External export was withheld because current rights could not be verified for every source."
            )
        payload: Any = None
        if not rights["export_withheld"] and output_format == "json":
            payload = report
        elif not rights["export_withheld"] and output_format == "markdown":
            payload = report.get("formats", {}).get("markdown", "")
        elif not rights["export_withheld"] and output_format == "csv":
            payload = report.get("formats", {}).get("csv", "")
        elif not rights["export_withheld"]:
            report_sections = [
                {"id": f"section-{index}", "title": section.get("heading", f"Section {index}"), "assertions": [{"id": f"assertion-{index}", "text": section.get("body", ""), "kind": "commentary", "dependencies": [], "citations": []}]}
                for index, section in enumerate(report.get("sections", []), start=1)
            ] or [{"id": "section-1", "title": "Brief", "assertions": [{"id": "assertion-1", "text": "No sections supplied.", "kind": "commentary", "dependencies": [], "citations": []}]}]
            if report.get("charts"):
                chart_assertions = [
                    {
                        "id": f"chart-alternative-{index}",
                        "text": f"Accessible chart summary: {chart.get('accessible_description') or _chart_accessible_description(chart, index - 1)}",
                        "kind": "commentary",
                        "dependencies": chart.get("source_revision_ids", []),
                        "citations": [],
                    }
                    for index, chart in enumerate(report["charts"], start=1)
                ]
                report_sections.append({"id": "charts", "title": "Charts and text alternatives", "assertions": chart_assertions})
            content = {
                "title": report.get("title", report_id),
                "sections": report_sections,
                "snapshot": {"id": f"market-brief:{report_id}:{report.get('version')}", "generations": {namespace: int(report.get("version", 1))}},
                "bibliography": [{"id": f"source-{index}", "text": str(item.get("locator", item.get("source_revision_id", "unlocated")))} for index, item in enumerate(source_locators, start=1)],
                "limitations": [*limitations, "Formatted rendering does not independently verify evidence support."],
            }
            try:
                from src.kb.citeproc_export import render_report

                package = {"report": {"report_id": report_id, "revision": int(report.get("version", 1)), "content": content}, "sha256": _digest(content)}
                rendered = render_report(package, output_format=output_format, locale="en-US")
            except Exception as exc:  # noqa: BLE001 - optional renderer boundary
                raise MarketResearchError("renderer_unavailable", "the optional formatted report renderer is unavailable") from exc
            payload = {"bytes_b64": base64.b64encode(rendered["content"]).decode("ascii"), "sha256": hashlib.sha256(rendered["content"]).hexdigest(), "renderer_receipt": rendered["receipt"]}
        payload_hash = (
            None
            if payload is None
            else hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        )
        return {
            "contract": "noesis-market-brief-export-v1",
            "namespace": namespace,
            "report_id": report_id,
            "version": report.get("version"),
            "owner": owner,
            "format": output_format,
            "rights": rights,
            "payload": payload,
            "integrity": {
                "algorithm": "sha256",
                "artifact_record_hash": report.get("record_hash"),
                "payload_sha256": payload_hash,
                "payload_included": payload is not None,
            },
            "limitations": limitations,
        }

    def export_brief_evidence_bundle(
        self,
        namespace: str,
        *,
        report_id: str,
        version: int | None,
        external: bool,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Export a brief and provenance through the shared Evidence Bundle contract.

        Current evidence rights are checked at export time. If any source is
        missing provenance or no longer permits evidence access, the bundle
        contains only a withholding receipt and declared omission—not report
        text, chart values, source locators, or source payloads.
        """
        _authorize(namespace, principal_id, scopes, write=False)
        owner = _owner(owner, principal_id, scopes)
        report = self.inspect(
            namespace,
            _text(report_id, "report_id", 200),
            version,
            principal_id=principal_id,
            scopes=scopes,
        )
        locators = report.get("source_locators", [])
        provenance_fields = {"entitlement_id", "license_id", "provider"}
        entitlement_refs = [
            dict(item)
            for item in locators
            if isinstance(item, Mapping) and provenance_fields <= set(item)
        ]
        reason_code: str | None = None
        evidence_decisions: list[dict[str, Any]] = []
        export_decisions: list[dict[str, Any]] = []
        now_ms = _millis(self.now(), "now_ms")
        if not locators or len(entitlement_refs) != len(locators):
            reason_code = "entitlement_unavailable"
        else:
            from src.domains.market.entitlements import (
                MarketEntitlementError,
                MarketEntitlementStore,
            )

            entitlements = MarketEntitlementStore(
                self.conn, initialize=True, now=self.now
            )
            try:
                evidence_decisions = entitlements.authorize_sources(
                    namespace,
                    entitlement_refs,
                    operation="evidence",
                    principal_id=principal_id,
                    scopes=scopes,
                    now_ms=now_ms,
                )
                if external:
                    export_decisions = entitlements.authorize_sources(
                        namespace,
                        entitlement_refs,
                        operation="export",
                        external=True,
                        principal_id=principal_id,
                        scopes=scopes,
                        now_ms=now_ms,
                    )
            except MarketEntitlementError as exc:
                reason_code = exc.code

        from src.evidence_bundle import EvidenceBundleBuilder

        builder = EvidenceBundleBuilder(
            "receipt",
            {
                "domain": "market",
                "report_id": report["report_id"],
                "version": report.get("version"),
                "external": bool(external),
            },
            created_at_ms=now_ms,
            as_of_ms=int(report["cutoff_ms"]),
        )
        source_revision_ids = sorted(
            {
                str(item.get("source_revision_id"))
                for item in locators
                if isinstance(item, Mapping) and item.get("source_revision_id")
            }
        )
        if reason_code is not None:
            root_payload = {
                "contract": "noesis-market-brief-evidence-receipt-v1",
                "status": "withheld",
                "report_id": report["report_id"],
                "version": report.get("version"),
                "cutoff_ms": report["cutoff_ms"],
                "rights": {"status": "withheld", "reason_code": reason_code},
                "source_revision_ids": source_revision_ids,
            }
            root_id = builder.add_object(
                "receipt", root_payload, object_id=f"market-brief:{report_id}:{report.get('version')}", root=True
            )
            builder.add_omission(
                "Market brief content and source locators were omitted because current rights do not authorize evidence export.",
                object_id=root_id,
            )
            return builder.build()

        safe_fields = (
            "source_ref_id",
            "source_revision_id",
            "provider",
            "license_id",
            "entitlement_id",
            "locator",
            "source_url",
            "content_hash",
            "retrieved_at_ms",
            "public_at_ms",
            "source_snapshot_id",
        )
        evidence_ids = []
        for index, item in enumerate(locators):
            safe_locator = {key: item[key] for key in safe_fields if key in item}
            identity = safe_locator or {"source_index": index}
            evidence_id = f"market-evidence:{_digest(identity)[:32]}"
            document_id = safe_locator.get("source_revision_id") or safe_locator.get("source_ref_id")
            locator = safe_locator.get("source_url") or safe_locator.get("locator")
            evidence_ids.append(
                builder.add_object(
                    "evidence",
                    {
                        "contract": "noesis-market-evidence-reference-v1",
                        **safe_locator,
                        "locator": {
                            "document_id": str(document_id) if document_id else None,
                            "url": locator,
                            "source": safe_locator.get("provider", "market source"),
                            "cited": bool(document_id or locator),
                        },
                    },
                    object_id=evidence_id,
                )
            )
        evidence_ids = sorted(set(evidence_ids))
        root_payload = {
            "contract": "noesis-market-brief-evidence-receipt-v1",
            "status": "authorized",
            "report": {
                "report_id": report["report_id"],
                "version": report.get("version"),
                "record_hash": report.get("record_hash"),
                "title": report.get("title"),
                "cutoff_ms": report["cutoff_ms"],
                "sections": report.get("sections", []),
                "charts": report.get("charts", []),
                "formula_versions": report.get("formula_versions", []),
                "declared_assumptions": report.get("assumptions", []),
                "artifact_refs": report.get("artifact_refs", []),
                "limitations": report.get("limitations", []),
            },
            "rights": {
                "status": "authorized",
                "external": bool(external),
                "evidence_decisions": evidence_decisions,
                "export_decisions": export_decisions,
            },
            "evidence_refs": evidence_ids,
        }
        builder.add_object(
            "receipt",
            root_payload,
            object_id=f"market-brief:{report_id}:{report.get('version')}",
            references=evidence_ids,
            root=True,
        )
        return builder.build()

    def schedule_brief(
        self,
        namespace: str,
        *,
        schedule_id: str,
        report_id: str,
        cadence: str,
        next_due_ms: int,
        request: Mapping[str, Any],
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        schedule_id = _text(schedule_id, "schedule_id", 200)
        report_id = _text(report_id, "report_id", 200)
        cadence = _text(cadence, "cadence", 20).lower()
        if cadence not in {"daily", "weekly", "monthly"}:
            raise MarketResearchError("invalid_request", "cadence must be daily, weekly, or monthly")
        next_due_ms = _millis(next_due_ms, "next_due_ms")
        if not isinstance(request, Mapping):
            raise MarketResearchError("invalid_request", "schedule request must be an object")
        request_body = dict(request)
        request_body["report_id"] = report_id
        request_body.setdefault("owner", owner)
        self.conn.execute(
            "INSERT INTO market_research_schedules VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(schedule_id) DO UPDATE SET cadence=excluded.cadence,next_due_ms=excluded.next_due_ms,request_json=excluded.request_json,status=excluded.status",
            [namespace, schedule_id, owner, report_id, cadence, next_due_ms, _canonical(request_body), "active", None, _millis(self.now(), "created_at_ms")],
        )
        return {"contract": "noesis-market-brief-schedule-v1", "namespace": namespace, "schedule_id": schedule_id, "owner": owner, "report_id": report_id, "cadence": cadence, "next_due_ms": next_due_ms, "status": "active", "request": request_body}

    def run_due_schedules(
        self,
        namespace: str,
        *,
        due_at_ms: int,
        limit: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        due_at_ms = _millis(due_at_ms, "due_at_ms")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MarketResearchError("invalid_request", "limit must be between 1 and 100")
        rows = self.conn.execute(
            "SELECT schedule_id,report_id,cadence,next_due_ms,request_json FROM market_research_schedules WHERE namespace=? AND owner=? AND status='active' AND next_due_ms<=? ORDER BY next_due_ms LIMIT ?",
            [namespace, owner, due_at_ms, limit],
        ).fetchall()
        receipts = []
        cadence_ms = {"daily": 86_400_000, "weekly": 7 * 86_400_000, "monthly": 30 * 86_400_000}
        for schedule_id, report_id, cadence, next_due, request_json in rows:
            request_body = json.loads(request_json)
            request_body["namespace"] = namespace
            request_body["owner"] = owner
            artifact = self.generate_brief(**request_body, principal_id=principal_id, scopes=scopes)
            next_due_ms = int(next_due) + cadence_ms[cadence]
            self.conn.execute("UPDATE market_research_schedules SET next_due_ms=?,last_run_id=? WHERE schedule_id=?", [next_due_ms, artifact["record_hash"], schedule_id])
            receipts.append({"schedule_id": schedule_id, "report_id": report_id, "status": "generated", "record_hash": artifact["record_hash"], "next_due_ms": next_due_ms})
        return {"contract": "noesis-market-brief-schedule-run-v1", "namespace": namespace, "due_at_ms": due_at_ms, "processed": len(receipts), "receipts": receipts}

    def acceptance_journey(
        self,
        namespace: str,
        *,
        journey_id: str,
        companies: Sequence[Mapping[str, Any]],
        macro_scenario: Mapping[str, Any],
        cutoff_ms: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        cutoff_ms = _millis(cutoff_ms, "cutoff_ms")
        company_rows = _rows(companies, "companies", 5)
        if len(company_rows) != 5:
            raise MarketResearchError("invalid_request", "acceptance journey requires exactly five companies")
        if not isinstance(macro_scenario, Mapping):
            raise MarketResearchError("invalid_request", "macro_scenario must be an object")
        comparisons = []
        gaps = []
        for row in company_rows:
            company_id = _text(row.get("company_id"), "company_id", 200)
            if row.get("valuation_change") is None:
                gaps.append({"company_id": company_id, "reason": "valuation_change_missing"})
            comparisons.append({"company_id": company_id, "performance": row.get("performance"), "valuation_change": row.get("valuation_change"), "source_revision_ids": list(row.get("source_revision_ids", [])), "formula_versions": list(row.get("formula_versions", []))})
        evidence = [row.get("evidence", []) for row in company_rows]
        result = {
            "contract": "noesis-market-acceptance-journey-v1",
            "namespace": namespace,
            "journey_id": _text(journey_id, "journey_id", 200),
            "cutoff_ms": cutoff_ms,
            "companies": comparisons,
            "macro_scenario": dict(macro_scenario),
            "supporting_and_contradicting_evidence": evidence,
            "gaps": gaps,
            "access_restrictions": [row for row in company_rows if row.get("access_restricted")],
            "restart_replay": {"deterministic_input_hash": _digest({"companies": company_rows, "macro_scenario": macro_scenario, "cutoff_ms": cutoff_ms}), "source_changes_visible": True},
            "analyst_review": {"status": "pending_human_review", "usefulness_criteria": list(ACCEPTANCE_REVIEW_CRITERIA)},
            "limitations": ["A local acceptance journey does not constitute live-provider evidence or an analyst sign-off.", "No causal claim is inferred from the macro scenario."],
        }
        return self._persist(namespace, _text(journey_id, "journey_id", 200), "acceptance-journey", 1, owner, {"companies": company_rows, "macro_scenario": dict(macro_scenario), "cutoff_ms": cutoff_ms, "source_rows": company_rows}, result)

    def review_acceptance_journey(
        self,
        namespace: str,
        *,
        journey_id: str,
        review_version: int,
        reviewer_id: str,
        reviewed_at_ms: int,
        decision: str,
        criteria: Sequence[Mapping[str, Any]],
        notes: str,
        live_evidence_refs: Sequence[str] | None,
        human_attestation: bool,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Record an immutable human usefulness review without mutating the journey."""

        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        journey_id = _text(journey_id, "journey_id", 200)
        if type(review_version) is not int or review_version < 1:
            raise MarketResearchError("invalid_request", "review_version must be positive")
        reviewer_id = _text(reviewer_id, "reviewer_id", 200)
        reviewed_at_ms = _millis(reviewed_at_ms, "reviewed_at_ms")
        if reviewer_id != principal_id and "operator" not in scopes:
            raise MarketResearchError("unauthorized", "reviewer identity must match the authenticated principal")
        if human_attestation is not True:
            raise MarketResearchError("human_review_required", "a human reviewer must explicitly attest this review")
        decision = _text(decision, "decision", 40)
        if decision not in {"accepted", "changes_requested", "rejected"}:
            raise MarketResearchError("invalid_request", "unsupported analyst-review decision")
        notes = _text(notes, "notes", 5000)
        rows = _rows(criteria, "criteria", len(ACCEPTANCE_REVIEW_CRITERIA))
        by_id: dict[str, dict[str, Any]] = {}
        for index, row in enumerate(rows):
            criterion_id = _text(row.get("criterion_id"), f"criteria[{index}].criterion_id", 100)
            outcome = _text(row.get("outcome"), f"criteria[{index}].outcome", 30)
            if outcome not in {"passed", "failed"}:
                raise MarketResearchError("invalid_request", "criterion outcome must be passed or failed")
            if criterion_id in by_id:
                raise MarketResearchError("invalid_request", "criterion IDs must be unique")
            by_id[criterion_id] = {
                "criterion_id": criterion_id,
                "outcome": outcome,
                "notes": _text(row.get("notes"), f"criteria[{index}].notes", 1000),
            }
        expected = set(ACCEPTANCE_REVIEW_CRITERIA)
        if set(by_id) != expected:
            raise MarketResearchError(
                "incomplete_review",
                "analyst review must decide every usefulness criterion",
                missing=sorted(expected - set(by_id)),
                unknown=sorted(set(by_id) - expected),
            )
        if decision == "accepted" and any(row["outcome"] != "passed" for row in by_id.values()):
            raise MarketResearchError("inconsistent_review", "an accepted review requires every criterion to pass")
        evidence_refs = list(live_evidence_refs or [])
        if len(evidence_refs) > 100 or any(not isinstance(item, str) or not item or len(item) > 500 for item in evidence_refs):
            raise MarketResearchError("invalid_request", "live_evidence_refs must contain bounded text")
        journey = self.inspect(
            namespace, journey_id, 1, principal_id=principal_id, scopes=scopes
        )
        if journey.get("withheld"):
            raise MarketResearchError("journey_withheld", "acceptance journey is unavailable under current rights")
        result = {
            "contract": "noesis-market-acceptance-review-v1",
            "namespace": namespace,
            "journey_id": journey_id,
            "journey_version": int(journey["version"]),
            "journey_record_hash": journey["record_hash"],
            "reviewer_id": reviewer_id,
            "reviewed_at_ms": reviewed_at_ms,
            "decision": decision,
            "criteria": [by_id[item] for item in ACCEPTANCE_REVIEW_CRITERIA],
            "notes": notes,
            "live_evidence_refs": sorted(set(evidence_refs)),
            "human_attestation": True,
            "limitations": [
                "This receipt records the authenticated reviewer's judgment; it does not manufacture missing provider evidence.",
                "The reviewed journey remains immutable and is linked by its record hash.",
            ],
        }
        return self._persist(
            namespace,
            f"{journey_id}:analyst-review",
            "acceptance-review",
            review_version,
            owner,
            {"journey_record_hash": journey["record_hash"], "reviewed_at_ms": reviewed_at_ms, "criteria": result["criteria"], "decision": decision, "notes": notes, "live_evidence_refs": result["live_evidence_refs"], "source_rows": []},
            result,
        )


__all__ = [
    "MarketResearchError",
    "MarketResearchStore",
    "ACCEPTANCE_REVIEW_CRITERIA",
    "RESEARCH_READ_SCOPE",
    "RESEARCH_WRITE_SCOPE",
    "ensure_market_research_schema",
    "verify_market_brief_export",
]


def check_market_brief_accessibility(export: Mapping[str, Any]) -> dict[str, Any]:
    """Automated structural accessibility checks for an exported brief.

    Covers what can be verified mechanically in Markdown and JSON exports: a
    single leading H1, no skipped heading levels, no empty headings, a text
    alternative for every chart, alt text for images, and header rows for
    tables. It does not replace assistive-technology or human review, and it
    reports ``not_applicable`` for withheld or binary (DOCX/PDF) payloads.
    """

    result = {
        "contract": "noesis-market-brief-accessibility-v1",
        "scope": "automated_structural",
        "human_review": "not_performed",
    }
    payload = export.get("payload") if isinstance(export, Mapping) else None
    output_format = export.get("format") if isinstance(export, Mapping) else None
    if payload is None or output_format not in {"markdown", "json"}:
        return {**result, "status": "not_applicable", "issues": []}
    issues: list[dict[str, Any]] = []
    if output_format == "json":
        for index, chart in enumerate(payload.get("charts") or []):
            if not str(chart.get("accessible_description") or "").strip():
                issues.append({"code": "chart_without_text_alternative", "chart": index})
        for index, section in enumerate(payload.get("sections") or []):
            if not str(section.get("heading") or "").strip():
                issues.append({"code": "empty_heading", "section": index})
        return {**result, "status": "failed" if issues else "passed", "issues": issues}
    lines = str(payload).splitlines()
    previous_level = 0
    h1_count = 0
    in_fence = False
    for number, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading = re.match(r"^(#{1,6})(\s*)(.*)$", line)
        if heading:
            level, text = len(heading.group(1)), heading.group(3).strip()
            if not heading.group(2) or not text:
                issues.append({"code": "empty_heading", "line": number})
            if level == 1:
                h1_count += 1
            if previous_level and level > previous_level + 1:
                issues.append({"code": "skipped_heading_level", "line": number, "from": previous_level, "to": level})
            if not previous_level and level != 1:
                issues.append({"code": "document_does_not_start_with_h1", "line": number})
            previous_level = level
        for alt in re.findall(r"!\[([^\]]*)\]\(", line):
            if not alt.strip():
                issues.append({"code": "image_without_alt_text", "line": number})
        if line.strip().startswith("|") and number < len(lines):
            prior = lines[number - 2].strip() if number > 1 else ""
            following = lines[number].strip()
            starts_table = not prior.startswith("|")
            if starts_table and not re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", following):
                issues.append({"code": "table_without_header_row", "line": number})
    if h1_count != 1:
        issues.append({"code": "document_h1_count", "count": h1_count})
    in_charts = False
    for number, line in enumerate(lines, start=1):
        if line.startswith("## "):
            in_charts = line.strip() == "## Charts"
        elif in_charts and line.startswith("### "):
            following = next((item for item in lines[number:] if item.strip()), "")
            if following.startswith("#") or not following.strip():
                issues.append({"code": "chart_without_text_alternative", "line": number})
    return {**result, "status": "failed" if issues else "passed", "issues": issues[:100]}


def verify_market_brief_export(export: Mapping[str, Any]) -> dict[str, Any]:
    """Check an exported brief's payload digest and JSON artifact hash offline.

    For an authenticity decision, compare ``artifact_record_hash`` with a
    trusted receipt or the immutable source store; an embedded digest alone is
    not a digital signature.
    """

    def invalid(reason: str) -> dict[str, Any]:
        return {"contract": "noesis-market-brief-export-verification-v1", "valid": False, "status": reason}

    if not isinstance(export, Mapping) or export.get("contract") != "noesis-market-brief-export-v1":
        return invalid("invalid_export_contract")
    integrity = export.get("integrity")
    if not isinstance(integrity, Mapping) or integrity.get("algorithm") != "sha256":
        return invalid("integrity_metadata_missing")
    artifact_hash = integrity.get("artifact_record_hash")
    if not isinstance(artifact_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", artifact_hash):
        return invalid("artifact_hash_invalid")
    payload = export.get("payload")
    if integrity.get("payload_included") is False:
        if payload is not None or integrity.get("payload_sha256") is not None:
            return invalid("withheld_payload_mismatch")
        return {
            "contract": "noesis-market-brief-export-verification-v1",
            "valid": True,
            "status": "payload_withheld",
            "artifact_record_hash": artifact_hash,
        }
    if payload is None or integrity.get("payload_included") is not True:
        return invalid("payload_presence_mismatch")
    expected_payload_hash = integrity.get("payload_sha256")
    if not isinstance(expected_payload_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_payload_hash):
        return invalid("payload_hash_invalid")
    observed_payload_hash = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    if observed_payload_hash != expected_payload_hash:
        return invalid("payload_hash_mismatch")
    if export.get("format") == "json":
        if not isinstance(payload, Mapping) or payload.get("record_hash") != artifact_hash:
            return invalid("artifact_record_hash_mismatch")
        record_body = {
            key: value
            for key, value in payload.items()
            if key not in {"record_hash", "recorded_at_ms", "idempotent"}
        }
        if _digest(record_body) != artifact_hash:
            return invalid("artifact_record_hash_mismatch")
    if isinstance(payload, Mapping) and "bytes_b64" in payload:
        try:
            rendered_bytes = base64.b64decode(payload["bytes_b64"], validate=True)
        except (ValueError, TypeError):
            return invalid("rendered_payload_encoding_invalid")
        if hashlib.sha256(rendered_bytes).hexdigest() != payload.get("sha256"):
            return invalid("rendered_payload_hash_mismatch")
    return {
        "contract": "noesis-market-brief-export-verification-v1",
        "valid": True,
        "status": "verified_consistency",
        "artifact_record_hash": artifact_hash,
        "payload_sha256": expected_payload_hash,
    }
