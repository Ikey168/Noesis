"""Canonical, bitemporal political entities and relations.

Political concepts extend Noesis' entity/relation ontology; they do not form a
second graph.  Stable object IDs remain distinct (a person is never an office
or a bounded office term), while aliases are resolved inside explicit type,
jurisdiction, and parent scopes.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any

from src.kb.temporal import record_temporal_assertion

POLITICAL_MODEL = "noesis-political-model-v1"
OBJECT_TYPES = frozenset(
    {
        "jurisdiction",
        "institution",
        "person",
        "office",
        "office_term",
        "party",
        "election",
        "proposal",
        "vote",
        "instrument",
    }
)
RELATION_TYPES = frozenset(
    {
        "in_jurisdiction",
        "part_of",
        "holds_office",
        "term_for_office",
        "term_holder",
        "member_of_party",
        "contests",
        "succeeds",
        "proposal_amends",
        "proposal_sponsored_by",
        "roll_call_on",
        "vote_cast_by",
        "adopted_as",
        "institutional_position",
        "corrects",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS political_objects (
    object_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    object_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    jurisdiction_id TEXT,
    parent_id TEXT,
    valid_from_ms BIGINT,
    valid_to_ms BIGINT,
    observed_at_ms BIGINT NOT NULL,
    source_document_id TEXT,
    attributes_json TEXT NOT NULL,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS political_aliases (
    alias_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    object_id TEXT NOT NULL,
    alias_norm TEXT NOT NULL,
    alias_display TEXT NOT NULL,
    object_type TEXT NOT NULL,
    jurisdiction_id TEXT,
    parent_id TEXT,
    source_document_id TEXT,
    active BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS political_relations (
    relation_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    valid_from_ms BIGINT,
    valid_to_ms BIGINT,
    observed_at_ms BIGINT NOT NULL,
    source_document_id TEXT,
    attributes_json TEXT NOT NULL,
    active BOOLEAN NOT NULL
);
CREATE TABLE IF NOT EXISTS political_corrections (
    correction_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    source_document_id TEXT,
    observed_at_ms BIGINT NOT NULL,
    reversed_at_ms BIGINT
);
"""


class PoliticalModelError(ValueError):
    """Stable political-model validation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _id(prefix: str, value: Any) -> str:
    return f"{prefix}:" + hashlib.sha256(_json(value).encode()).hexdigest()[:24]


def _norm(value: str) -> str:
    return re.sub(r"[^\w]+", " ", value.casefold(), flags=re.UNICODE).strip()


def _interval(start: Any, end: Any) -> tuple[int | None, int | None]:
    try:
        lower = int(start) if start is not None else None
        upper = int(end) if end is not None else None
    except (TypeError, ValueError) as exc:
        raise PoliticalModelError("bad_time", "political time bounds must be epoch milliseconds") from exc
    if lower is not None and lower < 0 or upper is not None and upper < 0:
        raise PoliticalModelError("bad_time", "political time bounds must be non-negative")
    if lower is not None and upper is not None and lower >= upper:
        raise PoliticalModelError("impossible_interval", "valid interval must be half-open with start < end")
    return lower, upper


def ensure_political_schema(conn: Any) -> None:
    conn.execute(_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_political_object_scope "
        "ON political_objects (domain, object_type, jurisdiction_id, parent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_political_alias_scope "
        "ON political_aliases (domain, alias_norm, object_type, jurisdiction_id, parent_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_political_relation_subject "
        "ON political_relations (domain, relation_type, subject_id, object_id)"
    )


def _object_row(conn: Any, object_id: str, domain: str | None = None) -> dict[str, Any] | None:
    where = "object_id = ?" + (" AND domain = ?" if domain is not None else "")
    params = [object_id, domain] if domain is not None else [object_id]
    row = conn.execute(
        "SELECT object_id, domain, object_type, canonical_name, jurisdiction_id, parent_id, "
        "valid_from_ms, valid_to_ms, observed_at_ms, source_document_id, "
        f"attributes_json, status, revision FROM political_objects WHERE {where}",
        params,
    ).fetchone()
    if row is None:
        return None
    keys = (
        "object_id", "domain", "object_type", "canonical_name", "jurisdiction_id", "parent_id",
        "valid_from_ms", "valid_to_ms", "observed_at_ms", "source_document_id",
        "attributes", "status", "revision",
    )
    result = dict(zip(keys, row))
    result["attributes"] = json.loads(result["attributes"] or "{}")
    return result


def record_object(
    conn: Any,
    *,
    object_id: str,
    object_type: str,
    canonical_name: str,
    jurisdiction_id: str | None = None,
    parent_id: str | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    source_document_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    status: str = "active",
    domain: str = "political",
    backing: str = "corpus-view",
    visibility: str = "public",
) -> dict[str, Any]:
    """Upsert one canonical entity extension and append its temporal version."""

    ensure_political_schema(conn)
    kind = str(object_type)
    if kind not in OBJECT_TYPES:
        raise PoliticalModelError("bad_object_type", f"unsupported political object type {kind!r}")
    if not str(object_id).strip() or not str(canonical_name).strip():
        raise PoliticalModelError("bad_request", "object_id and canonical_name are required")
    lower, upper = _interval(valid_from_ms, valid_to_ms)
    observed = int(observed_at_ms if observed_at_ms is not None else _now_ms())
    existing = _object_row(conn, object_id)
    if existing is not None and existing["object_type"] != kind:
        raise PoliticalModelError("identity_conflict", "an object ID cannot change political type")
    if existing is not None and existing["domain"] != domain:
        raise PoliticalModelError("identity_conflict", "an object ID cannot change knowledge domain")
    revision = int(existing["revision"] + 1) if existing else 1
    payload = {
        "political_contract": POLITICAL_MODEL,
        "object_id": str(object_id),
        "object_type": kind,
        "canonical_name": str(canonical_name).strip(),
        "jurisdiction_id": jurisdiction_id,
        "parent_id": parent_id,
        "attributes": dict(attributes or {}),
        "status": str(status),
        "revision": revision,
    }
    conn.execute(
        "INSERT OR REPLACE INTO political_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            object_id, domain, kind, payload["canonical_name"], jurisdiction_id, parent_id,
            lower, upper, observed, source_document_id, _json(payload["attributes"]),
            status, revision,
        ],
    )
    temporal_id = record_temporal_assertion(
        conn,
        domain=domain,
        backing=backing,
        assertion_kind="entity",
        assertion_id=object_id,
        payload=payload,
        observed_at_ms=observed,
        valid_from_ms=lower,
        valid_to_ms=upper,
        valid_time_precision="millisecond" if lower is not None or upper is not None else "unknown",
        source_reported=lower is not None or upper is not None,
        inferred=False,
        source_document_id=source_document_id,
        visibility=visibility,
        temporal_provenance={"model": POLITICAL_MODEL},
    )
    return {**payload, "valid_from_ms": lower, "valid_to_ms": upper, "observed_at_ms": observed, "temporal_id": temporal_id}


def record_alias(
    conn: Any,
    *,
    object_id: str,
    alias: str,
    jurisdiction_id: str | None = None,
    parent_id: str | None = None,
    source_document_id: str | None = None,
) -> str:
    """Record a scoped alias; jurisdiction and regional parent stay distinct."""

    ensure_political_schema(conn)
    obj = _object_row(conn, object_id)
    if obj is None:
        raise PoliticalModelError("not_found", f"unknown political object {object_id!r}")
    normalized = _norm(alias)
    if not normalized:
        raise PoliticalModelError("bad_alias", "alias must contain text")
    scope_jurisdiction = jurisdiction_id if jurisdiction_id is not None else obj["jurisdiction_id"]
    scope_parent = parent_id if parent_id is not None else obj["parent_id"]
    alias_id = _id(
        "pa",
        [object_id, normalized, obj["object_type"], scope_jurisdiction, scope_parent],
    )
    conn.execute(
        "INSERT OR REPLACE INTO political_aliases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            alias_id, obj["domain"], object_id, normalized, str(alias).strip(), obj["object_type"],
            scope_jurisdiction, scope_parent, source_document_id, True,
        ],
    )
    return alias_id


def resolve_alias(
    conn: Any,
    alias: str,
    *,
    object_type: str | None = None,
    jurisdiction_id: str | None = None,
    parent_id: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """Resolve only when the supplied scope leaves exactly one candidate."""

    ensure_political_schema(conn)
    clauses = ["a.alias_norm = ?", "a.active = TRUE"]
    params: list[Any] = [_norm(alias)]
    for column, value in (
        ("a.domain", domain),
        ("a.object_type", object_type),
        ("a.jurisdiction_id", jurisdiction_id),
        ("a.parent_id", parent_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    rows = conn.execute(
        "SELECT o.object_id, o.object_type, o.canonical_name, o.jurisdiction_id, o.parent_id "
        "FROM political_aliases a JOIN political_objects o ON o.object_id = a.object_id "
        f"WHERE {' AND '.join(clauses)} ORDER BY o.object_id",
        params,
    ).fetchall()
    candidates = [
        dict(zip(("object_id", "object_type", "canonical_name", "jurisdiction_id", "parent_id"), row))
        for row in rows
    ]
    return {
        "status": "not_found" if not candidates else "resolved" if len(candidates) == 1 else "ambiguous",
        "alias": alias,
        "scope": {"object_type": object_type, "jurisdiction_id": jurisdiction_id, "parent_id": parent_id},
        "candidates": candidates,
        "object": candidates[0] if len(candidates) == 1 else None,
    }


def record_relation(
    conn: Any,
    *,
    relation_type: str,
    subject_id: str,
    object_id: str,
    relation_id: str | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    observed_at_ms: int | None = None,
    source_document_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
    domain: str = "political",
    backing: str = "corpus-view",
    visibility: str = "public",
) -> dict[str, Any]:
    ensure_political_schema(conn)
    kind = str(relation_type)
    if kind not in RELATION_TYPES:
        raise PoliticalModelError("bad_relation_type", f"unsupported political relation {kind!r}")
    for endpoint in (subject_id, object_id):
        if _object_row(conn, endpoint, domain) is None:
            raise PoliticalModelError("not_found", f"unknown political endpoint {endpoint!r}")
    lower, upper = _interval(valid_from_ms, valid_to_ms)
    observed = int(observed_at_ms if observed_at_ms is not None else _now_ms())
    rid = relation_id or _id("pr", [kind, subject_id, object_id, lower, upper])
    payload = {
        "political_contract": POLITICAL_MODEL,
        "relation_id": rid,
        "relation_type": kind,
        "subject_id": subject_id,
        "object_id": object_id,
        "attributes": dict(attributes or {}),
    }
    conn.execute(
        "INSERT OR REPLACE INTO political_relations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [rid, domain, kind, subject_id, object_id, lower, upper, observed, source_document_id, _json(payload["attributes"]), True],
    )
    temporal_id = record_temporal_assertion(
        conn,
        domain=domain,
        backing=backing,
        assertion_kind="relation",
        assertion_id=rid,
        payload=payload,
        observed_at_ms=observed,
        valid_from_ms=lower,
        valid_to_ms=upper,
        valid_time_precision="millisecond" if lower is not None or upper is not None else "unknown",
        source_reported=lower is not None or upper is not None,
        inferred=False,
        source_document_id=source_document_id,
        visibility=visibility,
        temporal_provenance={"model": POLITICAL_MODEL},
    )
    return {**payload, "valid_from_ms": lower, "valid_to_ms": upper, "observed_at_ms": observed, "temporal_id": temporal_id}


def record_office_term(
    conn: Any,
    *,
    term_id: str,
    person_id: str,
    office_id: str,
    valid_from_ms: int,
    valid_to_ms: int | None,
    observed_at_ms: int,
    source_document_id: str | None = None,
    domain: str = "political",
) -> dict[str, Any]:
    """Create a bounded term entity and explicit person/office edges."""

    office = _object_row(conn, office_id, domain)
    if _object_row(conn, person_id, domain) is None or office is None:
        raise PoliticalModelError("not_found", "office term requires an existing person and office")
    term = record_object(
        conn,
        object_id=term_id,
        object_type="office_term",
        canonical_name=f"{office['canonical_name']} term",
        jurisdiction_id=office["jurisdiction_id"],
        parent_id=office_id,
        valid_from_ms=valid_from_ms,
        valid_to_ms=valid_to_ms,
        observed_at_ms=observed_at_ms,
        source_document_id=source_document_id,
        attributes={"person_id": person_id, "office_id": office_id},
        domain=domain,
    )
    for relation_type, endpoint in (("term_holder", person_id), ("term_for_office", office_id)):
        record_relation(
            conn,
            relation_type=relation_type,
            subject_id=term_id,
            object_id=endpoint,
            valid_from_ms=valid_from_ms,
            valid_to_ms=valid_to_ms,
            observed_at_ms=observed_at_ms,
            source_document_id=source_document_id,
            domain=domain,
        )
    return term


def correct_object(
    conn: Any,
    object_id: str,
    changes: Mapping[str, Any],
    *,
    source_document_id: str | None,
    observed_at_ms: int,
    domain: str = "political",
) -> str:
    """Apply an auditable object correction whose prior state can be restored."""

    before = _object_row(conn, object_id, domain)
    if before is None:
        raise PoliticalModelError("not_found", f"unknown political object {object_id!r}")
    allowed = {"canonical_name", "jurisdiction_id", "parent_id", "valid_from_ms", "valid_to_ms", "attributes", "status"}
    unknown = set(changes) - allowed
    if unknown:
        raise PoliticalModelError("bad_correction", f"unsupported correction fields: {sorted(unknown)}")
    after = {**before, **dict(changes)}
    correction_id = _id("pc", [object_id, before["revision"], observed_at_ms, changes])
    record_object(
        conn,
        object_id=object_id,
        object_type=before["object_type"],
        canonical_name=after["canonical_name"],
        jurisdiction_id=after["jurisdiction_id"],
        parent_id=after["parent_id"],
        valid_from_ms=after["valid_from_ms"],
        valid_to_ms=after["valid_to_ms"],
        observed_at_ms=observed_at_ms,
        source_document_id=source_document_id,
        attributes=after["attributes"],
        status=after["status"],
        domain=domain,
    )
    current = _object_row(conn, object_id, domain) or after
    conn.execute(
        "INSERT INTO political_corrections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [correction_id, domain, "object", object_id, _json(before), _json(current), source_document_id, int(observed_at_ms), None],
    )
    return correction_id


def reverse_correction(
    conn: Any,
    correction_id: str,
    *,
    reversed_at_ms: int,
    domain: str = "political",
) -> dict[str, Any]:
    row = conn.execute(
        "SELECT target_id, before_json, source_document_id, reversed_at_ms "
        "FROM political_corrections WHERE correction_id = ? AND domain = ?",
        [correction_id, domain],
    ).fetchone()
    if row is None:
        raise PoliticalModelError("not_found", f"unknown correction {correction_id!r}")
    if row[3] is not None:
        raise PoliticalModelError("already_reversed", "correction was already reversed")
    before = json.loads(row[1])
    restored = record_object(
        conn,
        object_id=row[0],
        object_type=before["object_type"],
        canonical_name=before["canonical_name"],
        jurisdiction_id=before["jurisdiction_id"],
        parent_id=before["parent_id"],
        valid_from_ms=before["valid_from_ms"],
        valid_to_ms=before["valid_to_ms"],
        observed_at_ms=reversed_at_ms,
        source_document_id=row[2],
        attributes=before["attributes"],
        status=before["status"],
        domain=domain,
    )
    conn.execute(
        "UPDATE political_corrections SET reversed_at_ms = ? WHERE correction_id = ?",
        [int(reversed_at_ms), correction_id],
    )
    return restored


def load_fixture(conn: Any, payload: Mapping[str, Any], *, domain: str = "political") -> dict[str, int]:
    """Load deterministic objects, aliases, terms, and relations for tests/demos."""

    if payload.get("contract") != "political-benchmark-v1":
        raise PoliticalModelError("bad_fixture", "fixture must declare political-benchmark-v1")
    for obj in payload.get("objects", []):
        record_object(conn, domain=domain, **dict(obj))
    for alias in payload.get("aliases", []):
        record_alias(conn, **dict(alias))
    for term in payload.get("office_terms", []):
        record_office_term(conn, domain=domain, **dict(term))
    for relation in payload.get("relations", []):
        record_relation(conn, domain=domain, **dict(relation))
    return {
        "objects": len(payload.get("objects", [])) + len(payload.get("office_terms", [])),
        "aliases": len(payload.get("aliases", [])),
        "relations": len(payload.get("relations", [])) + 2 * len(payload.get("office_terms", [])),
    }


__all__ = [
    "OBJECT_TYPES", "POLITICAL_MODEL", "RELATION_TYPES", "PoliticalModelError",
    "correct_object", "ensure_political_schema", "load_fixture", "record_alias",
    "record_object", "record_office_term", "record_relation", "resolve_alias",
    "reverse_correction",
]
