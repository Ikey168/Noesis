"""Cited political-research query compositions over core Noesis contracts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from src.domains.political.model import ensure_political_schema
from src.kb.temporal import TemporalError, parse_source_time, query_temporal
from src.osint.independence import origin_summary

POLITICAL_RESEARCH_CONTRACT = "noesis-political-research-v1"
QUERY_TYPES = frozenset(
    {
        "officeholder_at_date",
        "proposal_lifecycle",
        "vote_records",
        "institutional_positions",
        "policy_changes",
    }
)
_SOURCE_CATALOG = Path(__file__).resolve().parents[3] / "config/political_sources.json"


class PoliticalQueryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _millis(value: Any, field: str) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and not value.strip().isdigit():
            return parse_source_time(value, field=field)[0]
        result = int(value)
    except (TypeError, ValueError, TemporalError) as exc:
        raise PoliticalQueryError("bad_time", f"{field} must be ISO-8601 or epoch milliseconds") from exc
    if result < 0:
        raise PoliticalQueryError("bad_time", f"{field} must be non-negative")
    return result


def _active_at(item: dict[str, Any], valid: int | None) -> bool:
    if valid is None:
        return True
    lower = item.get("valid_from_ms")
    upper = item.get("valid_to_ms")
    return (lower is None or lower <= valid) and (upper is None or upper > valid)


def _political_snapshot(
    backing: Any,
    temporal: dict[str, Any],
    jurisdiction: str,
    valid: int | None,
    observed: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project the authorized immutable temporal snapshot into political rows."""

    recorded = temporal["temporal_basis"]["effective"]["recorded_before_ms"]
    rows = backing.conn.execute(
        "SELECT assertion_kind, assertion_id, valid_from_ms, valid_to_ms, "
        "observed_at_ms, retracted_at_ms, source_document_id, payload_json "
        "FROM kb_temporal_assertions WHERE domain = ? AND observed_at_ms <= ? "
        "AND recorded_at_ms <= ? AND payload_json LIKE '%noesis-political-model-v1%' "
        "ORDER BY observed_at_ms DESC, assertion_kind, assertion_id, temporal_id",
        [backing.definition.name, observed, recorded],
    ).fetchall()
    latest: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row[0], row[1])
        latest[key] = max(latest.get(key, -1), int(row[4]))
    objects = []
    relation_candidates = []
    for row in rows:
        if int(row[4]) != latest[(row[0], row[1])]:
            continue
        if row[5] is not None and int(row[5]) <= observed:
            continue
        payload = json.loads(row[7])
        if payload.get("political_contract") != "noesis-political-model-v1":
            continue
        item = {
            **payload,
            "valid_from_ms": row[2],
            "valid_to_ms": row[3],
            "observed_at_ms": int(row[4]),
            "source_document_id": row[6],
        }
        if not _active_at(item, valid):
            continue
        if row[0] == "entity" and (
            item.get("jurisdiction_id") == jurisdiction and item.get("status") == "active"
        ):
            objects.append(item)
        elif row[0] == "relation":
            relation_candidates.append(item)
    object_ids = {item["object_id"] for item in objects}
    relations = [
        item for item in relation_candidates
        if item.get("subject_id") in object_ids or item.get("object_id") in object_ids
    ]
    objects.sort(key=lambda item: (item["object_type"], item["canonical_name"], item["object_id"]))
    relations.sort(key=lambda item: (item["observed_at_ms"], item["relation_type"], item["relation_id"]))
    return objects, relations


def _evidence(conn: Any, document_id: str | None) -> dict[str, Any]:
    if not document_id:
        return {
            "document_id": None,
            "source_id": None,
            "url": None,
            "title": None,
            "locator_available": False,
        }
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'documents'"
    ).fetchone()
    row = (
        conn.execute(
            "SELECT document_id, source_id, url, title FROM documents WHERE document_id = ?",
            [document_id],
        ).fetchone()
        if exists
        else None
    )
    return {
        "document_id": document_id,
        "source_id": row[1] if row else None,
        "url": row[2] if row else None,
        "title": row[3] if row else None,
        "locator_available": bool(row and row[2]),
    }


def _source_coverage(jurisdiction: str, cited: list[dict[str, Any]]) -> dict[str, Any]:
    try:
        catalog = json.loads(_SOURCE_CATALOG.read_text(encoding="utf-8"))
        declared = [
            source for source in catalog.get("sources", [])
            if source.get("jurisdiction") == jurisdiction
        ]
    except (OSError, ValueError):
        declared = []
    source_ids = {item.get("source_id") for item in cited if item.get("source_id")}
    manifest_ids = {source.get("source_id") for source in declared}
    return {
        "jurisdiction": jurisdiction,
        "declared_manifest_sources": sorted(manifest_ids),
        "represented_sources": sorted(source_ids),
        "source_classes": sorted({str(source.get("source_class")) for source in declared}),
        "missing_declared_sources": sorted(manifest_ids - source_ids),
        "manifest_count": len(declared),
    }


def _officeholders(objects: list[dict[str, Any]], office_id: str | None) -> list[dict[str, Any]]:
    by_id = {item["object_id"]: item for item in objects}
    results = []
    for term in objects:
        if term["object_type"] != "office_term":
            continue
        attrs = term["attributes"]
        if office_id and attrs.get("office_id") != office_id:
            continue
        person = by_id.get(attrs.get("person_id"))
        office = by_id.get(attrs.get("office_id"))
        results.append(
            {
                "term": term,
                "person": person,
                "office": office,
                "source_document_id": term.get("source_document_id"),
            }
        )
    return results


def _proposal_lifecycle(
    objects: list[dict[str, Any]], relations: list[dict[str, Any]], proposal_id: str | None
) -> list[dict[str, Any]]:
    proposals = [item for item in objects if item["object_type"] == "proposal"]
    if proposal_id:
        proposals = [item for item in proposals if item["object_id"] == proposal_id]
    results = []
    for proposal in proposals:
        links = [
            rel for rel in relations
            if rel["subject_id"] == proposal["object_id"] or rel["object_id"] == proposal["object_id"]
        ]
        results.append(
            {
                "proposal": proposal,
                "transitions": links,
                "source_document_id": proposal.get("source_document_id")
                or next((link.get("source_document_id") for link in links if link.get("source_document_id")), None),
            }
        )
    return results


def _vote_records(
    objects: list[dict[str, Any]], relations: list[dict[str, Any]], actor_id: str | None,
    proposal_id: str | None,
) -> list[dict[str, Any]]:
    results = []
    for vote in (item for item in objects if item["object_type"] == "vote"):
        links = [rel for rel in relations if rel["subject_id"] == vote["object_id"]]
        actor = next((rel["object_id"] for rel in links if rel["relation_type"] == "vote_cast_by"), None)
        proposal = next((rel["object_id"] for rel in links if rel["relation_type"] == "roll_call_on"), None)
        if actor_id and actor != actor_id or proposal_id and proposal != proposal_id:
            continue
        results.append(
            {
                "vote": vote,
                "actor_id": actor,
                "proposal_id": proposal,
                "position": vote["attributes"].get("position"),
                "source_document_id": vote.get("source_document_id"),
            }
        )
    return results


def _institutional_positions(
    objects: list[dict[str, Any]], relations: list[dict[str, Any]], institution_id: str | None,
) -> list[dict[str, Any]]:
    in_jurisdiction = {item["object_id"] for item in objects}
    return [
        {**rel, "source_document_id": rel.get("source_document_id")}
        for rel in relations
        if rel["relation_type"] == "institutional_position"
        and rel["subject_id"] in in_jurisdiction
        and (institution_id is None or rel["subject_id"] == institution_id)
    ]


def _policy_changes(objects: list[dict[str, Any]], relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = {item["object_id"] for item in objects}
    return [
        {**rel, "source_document_id": rel.get("source_document_id")}
        for rel in relations
        if rel["relation_type"] in {"adopted_as", "proposal_amends", "succeeds", "corrects"}
        and (rel["subject_id"] in ids or rel["object_id"] in ids)
    ]


def political_research(
    backing: Any,
    *,
    query_type: str,
    jurisdiction: str,
    at: Any = None,
    observed_before: Any = None,
    office_id: str | None = None,
    proposal_id: str | None = None,
    actor_id: str | None = None,
    institution_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Answer a political research query with temporal/evidence limitations."""

    if query_type not in QUERY_TYPES:
        raise PoliticalQueryError("bad_request", f"query_type must be one of {sorted(QUERY_TYPES)}")
    if not isinstance(jurisdiction, str) or not jurisdiction.strip():
        raise PoliticalQueryError("bad_request", "jurisdiction is required")
    try:
        page_size = int(limit)
    except (TypeError, ValueError) as exc:
        raise PoliticalQueryError("bad_request", "limit must be an integer") from exc
    if not 1 <= page_size <= 100:
        raise PoliticalQueryError("bad_request", "limit must be between 1 and 100")
    ensure_political_schema(backing.conn)
    valid = _millis(at, "at")
    requested_observed = _millis(observed_before, "observed_before")
    observed = requested_observed if requested_observed is not None else int(time.time() * 1000)
    temporal = query_temporal(
        backing,
        observed_before=observed,
        limit=100,
    )
    objects, relations = _political_snapshot(
        backing, temporal, jurisdiction.strip(), valid, observed
    )
    if query_type == "officeholder_at_date":
        results = _officeholders(objects, office_id)
    elif query_type == "proposal_lifecycle":
        results = _proposal_lifecycle(objects, relations, proposal_id)
    elif query_type == "vote_records":
        results = _vote_records(objects, relations, actor_id, proposal_id)
    elif query_type == "institutional_positions":
        results = _institutional_positions(objects, relations, institution_id)
    else:
        results = _policy_changes(objects, relations)
    results = results[:page_size]
    for result in results:
        result["evidence"] = _evidence(backing.conn, result.pop("source_document_id", None))
    cited = [result["evidence"] for result in results]
    source_coverage = _source_coverage(jurisdiction.strip(), cited)
    reasons = []
    if not results:
        reasons.append("no matching supported records are present for this jurisdiction and time")
    if source_coverage["manifest_count"] == 0:
        reasons.append("no official source manifest is declared for this jurisdiction")
    if any(not item.get("locator_available") for item in cited):
        reasons.append("some records lack a resolvable document URL")
    status = "unsupported" if not results else "partial" if reasons or source_coverage["missing_declared_sources"] else "supported"
    return {
        "political_contract": POLITICAL_RESEARCH_CONTRACT,
        "query": {
            "type": query_type,
            "jurisdiction": jurisdiction.strip(),
            "at_ms": valid,
            "observed_before_ms": observed,
            "office_id": office_id,
            "proposal_id": proposal_id,
            "actor_id": actor_id,
            "institution_id": institution_id,
        },
        "as_of": {"valid_at_ms": valid, "observed_before_ms": observed},
        "results": results,
        "n": len(results),
        "coverage": {"domain": backing.coverage(), "official_sources": source_coverage},
        "uncertainty": {"status": status, "reasons": reasons},
        "evidence_independence": origin_summary(
            backing.conn,
            [item.get("document_id") for item in cited],
            sources=[item.get("source_id") or "unknown" for item in cited],
        ),
        "temporal": {
            "contract": temporal["temporal_contract"],
            "basis": temporal["temporal_basis"],
            "matching_assertions": len(objects) + len(relations),
        },
        "composed_contracts": [
            "noesis-kb-v1",
            "noesis-temporal-v1",
            "noesis-evidence-independence-v1",
            "noesis-political-model-v1",
        ],
    }


__all__ = [
    "POLITICAL_RESEARCH_CONTRACT", "QUERY_TYPES", "PoliticalQueryError", "political_research",
]
