"""Durable, cursor-based claim watches over knowledge-domain backings.

The matcher snapshots only committed consolidation watermarks, derives a
deterministic state for each selector, and atomically appends immutable events
with matcher progress.  Poll cursors are opaque and watch-bound.  Watch and
event access is always scoped to the owning principal; domains tagged private
also require an explicit grant.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from itertools import pairwise
from typing import Any

WATCH_CONTRACT_VERSION = "noesis-claim-watch-v1"
SCHEMA_VERSION = 1

SELECTOR_TYPES = ("query", "claim", "entity", "topic")
EVENT_TYPES = (
    "support_gained",
    "support_lost",
    "contradiction_added",
    "contradiction_removed",
    "independence_changed",
    "integrity_changed",
    "quantitative_verdict_changed",
    "coverage_stale",
    "source_delivery_failed",
    "guidance_stale",
)

_EVENT_ORDER = {name: index for index, name in enumerate(EVENT_TYPES)}
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)

_DDL = """
CREATE TABLE IF NOT EXISTS noesis_schema_migrations (
    component TEXT NOT NULL,
    version INTEGER NOT NULL,
    applied_at_ms BIGINT NOT NULL,
    PRIMARY KEY (component, version)
);
CREATE SEQUENCE IF NOT EXISTS claim_watch_event_sequence START 1;
CREATE SEQUENCE IF NOT EXISTS claim_watch_audit_sequence START 1;
CREATE TABLE IF NOT EXISTS claim_watch_domain_grants (
    principal_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    granted_at_ms BIGINT NOT NULL,
    PRIMARY KEY (principal_id, domain)
);
CREATE TABLE IF NOT EXISTS claim_watches (
    watch_id TEXT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    visibility TEXT NOT NULL,
    selector_type TEXT NOT NULL,
    selector_value TEXT NOT NULL,
    event_types_json TEXT NOT NULL,
    stale_after_ms BIGINT NOT NULL,
    status TEXT NOT NULL,
    created_at_ms BIGINT NOT NULL,
    updated_at_ms BIGINT NOT NULL,
    deleted_at_ms BIGINT,
    last_watermark BIGINT,
    last_state_json TEXT,
    retained_from_sequence BIGINT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS claim_watch_watermarks (
    watermark BIGINT PRIMARY KEY,
    consolidation_json TEXT NOT NULL,
    committed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_watch_snapshots (
    watch_id TEXT NOT NULL,
    watermark BIGINT NOT NULL,
    state_json TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    captured_at_ms BIGINT NOT NULL,
    consolidation_json TEXT NOT NULL,
    PRIMARY KEY (watch_id, watermark)
);
CREATE TABLE IF NOT EXISTS claim_watch_events (
    event_sequence BIGINT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    watch_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    visibility TEXT NOT NULL,
    event_type TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    watermark BIGINT NOT NULL,
    consolidation_json TEXT NOT NULL,
    observed_at_ms BIGINT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    explanation TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_watch_audit (
    audit_sequence BIGINT PRIMARY KEY,
    principal_id TEXT NOT NULL,
    watch_id TEXT,
    action TEXT NOT NULL,
    at_ms BIGINT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claim_watch_failures (
    watch_id TEXT NOT NULL,
    watermark BIGINT NOT NULL,
    attempts INTEGER NOT NULL,
    error_type TEXT NOT NULL,
    dead_lettered BOOLEAN NOT NULL,
    first_at_ms BIGINT NOT NULL,
    last_at_ms BIGINT NOT NULL,
    resolved_at_ms BIGINT,
    PRIMARY KEY (watch_id, watermark)
);
"""


class WatchError(Exception):
    """Stable application error raised by the watch service."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now_ms() -> int:
    return int(time.time() * 1000)


def _timestamp(value: int | None) -> int:
    return _now_ms() if value is None else int(value)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        return json.loads(value)
    return value


def ensure_watch_schema(conn: Any) -> None:
    """Apply the additive v1 DuckDB schema migration idempotently."""
    conn.execute(_DDL)
    conn.execute(
        "INSERT OR IGNORE INTO noesis_schema_migrations VALUES (?, ?, ?)",
        ["claim-watches", SCHEMA_VERSION, _now_ms()],
    )


def grant_watch_domain(
    conn: Any, principal_id: str, domain: str, *, granted_at_ms: int | None = None
) -> dict[str, Any]:
    """Grant a principal access to create and read watches in a private domain."""
    principal = _principal(principal_id)
    ensure_watch_schema(conn)
    conn.execute(
        "INSERT OR IGNORE INTO claim_watch_domain_grants VALUES (?, ?, ?)",
        [principal, domain, _timestamp(granted_at_ms)],
    )
    return {"principal_id": principal, "domain": domain, "granted": True}


def commit_watch_watermark(
    conn: Any,
    watermark: int,
    consolidation: Mapping[str, Any] | None = None,
    *,
    committed_at_ms: int | None = None,
) -> dict[str, Any]:
    """Declare a consolidation watermark complete and therefore safe to scan."""
    ensure_watch_schema(conn)
    value = _positive_watermark(watermark)
    metadata = dict(consolidation or {})
    encoded = _canonical(metadata)
    existing = conn.execute(
        "SELECT consolidation_json FROM claim_watch_watermarks WHERE watermark = ?",
        [value],
    ).fetchone()
    if existing is not None and existing[0] != encoded:
        raise WatchError(
            "watermark_conflict",
            f"watermark {value} was already committed with different metadata",
        )
    conn.execute(
        "INSERT OR IGNORE INTO claim_watch_watermarks VALUES (?, ?, ?)",
        [value, encoded, _timestamp(committed_at_ms)],
    )
    return {
        "watermark": value,
        "consolidation": metadata,
        "status": "committed",
    }


def _principal(value: Any) -> str:
    principal = str(value or "").strip()
    if not principal or len(principal) > 200:
        raise WatchError("unauthorized", "an authenticated principal is required")
    return principal


def _positive_watermark(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise WatchError("bad_request", "watermark must be a positive integer") from exc
    if result < 1:
        raise WatchError("bad_request", "watermark must be a positive integer")
    return result


def _visibility(backing: Any) -> str:
    tags = {str(tag).casefold() for tag in backing.definition.tags}
    return "private" if "private" in tags else "public"


def _authorize_domain(conn: Any, backing: Any, principal_id: str) -> str:
    visibility = _visibility(backing)
    if visibility == "public":
        return visibility
    ensure_watch_schema(conn)
    allowed = conn.execute(
        "SELECT 1 FROM claim_watch_domain_grants"
        " WHERE principal_id = ? AND domain = ?",
        [principal_id, backing.definition.name],
    ).fetchone()
    if allowed is None:
        raise WatchError(
            "unauthorized",
            f"principal is not authorized for domain {backing.definition.name!r}",
        )
    return visibility


def _validate_selector(selector: Mapping[str, Any]) -> tuple[str, str]:
    if not isinstance(selector, Mapping):
        raise WatchError("bad_selector", "selector must be an object")
    if set(selector) != {"type", "value"}:
        raise WatchError(
            "bad_selector", "selector must contain exactly 'type' and 'value'"
        )
    selector_type = str(selector.get("type") or "").strip().casefold()
    selector_value = str(selector.get("value") or "").strip()
    if selector_type not in SELECTOR_TYPES:
        raise WatchError(
            "bad_selector", f"selector type must be one of {list(SELECTOR_TYPES)}"
        )
    if not selector_value or len(selector_value) > 2_000:
        raise WatchError(
            "bad_selector", "selector value must contain 1 to 2000 characters"
        )
    return selector_type, selector_value


def _validate_event_types(values: Sequence[str] | None) -> list[str]:
    selected = list(EVENT_TYPES) if values is None else [str(value) for value in values]
    if not selected:
        raise WatchError("bad_request", "event_types may not be empty")
    unknown = sorted(set(selected) - set(EVENT_TYPES))
    if unknown:
        raise WatchError("bad_request", f"unknown event types: {unknown}")
    return sorted(set(selected), key=_EVENT_ORDER.__getitem__)


def _watch_id(
    principal_id: str,
    domain: str,
    selector_type: str,
    selector_value: str,
    event_types: Sequence[str],
    stale_after_ms: int,
) -> str:
    identity = {
        "principal_id": principal_id,
        "domain": domain,
        "selector": {"type": selector_type, "value": selector_value},
        "event_types": list(event_types),
        "stale_after_ms": stale_after_ms,
    }
    return "watch:" + _digest(identity)[:24]


def _watch_from_row(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "watch_contract": WATCH_CONTRACT_VERSION,
        "watch_id": row[0],
        "principal_id": row[1],
        "domain": row[2],
        "visibility": row[3],
        "selector": {"type": row[4], "value": row[5]},
        "event_types": _load(row[6], []),
        "stale_after_ms": int(row[7]),
        "status": row[8],
        "created_at_ms": int(row[9]),
        "updated_at_ms": int(row[10]),
        "deleted_at_ms": row[11],
        "last_watermark": row[12],
    }


_WATCH_COLUMNS = (
    "watch_id, principal_id, domain, visibility, selector_type, selector_value,"
    " event_types_json, stale_after_ms, status, created_at_ms, updated_at_ms,"
    " deleted_at_ms, last_watermark"
)


def _audit(
    conn: Any,
    principal_id: str,
    watch_id: str | None,
    action: str,
    details: Mapping[str, Any] | None = None,
    *,
    at_ms: int | None = None,
) -> None:
    # Deliberately limited to identifiers, counters, and state labels.  Selector
    # values, evidence excerpts, URLs, source text, and credentials never enter
    # the operational audit stream.
    conn.execute(
        "INSERT INTO claim_watch_audit VALUES"
        " (nextval('claim_watch_audit_sequence'), ?, ?, ?, ?, ?)",
        [
            principal_id,
            watch_id,
            action,
            _timestamp(at_ms),
            _canonical(dict(details or {})),
        ],
    )


def create_watch(
    backing: Any,
    principal_id: str,
    selector: Mapping[str, Any],
    event_types: Sequence[str] | None = None,
    *,
    stale_after_ms: int = 86_400_000,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Create an idempotent principal/domain-bound watch."""
    principal = _principal(principal_id)
    selector_type, selector_value = _validate_selector(selector)
    selected_types = _validate_event_types(event_types)
    try:
        stale_after = int(stale_after_ms)
    except (TypeError, ValueError) as exc:
        raise WatchError("bad_request", "stale_after_ms must be an integer") from exc
    if stale_after < 1:
        raise WatchError("bad_request", "stale_after_ms must be positive")
    conn = backing.conn
    ensure_watch_schema(conn)
    visibility = _authorize_domain(conn, backing, principal)
    watch_id = _watch_id(
        principal,
        backing.definition.name,
        selector_type,
        selector_value,
        selected_types,
        stale_after,
    )
    created = _timestamp(now_ms)
    conn.execute(
        """
        INSERT OR IGNORE INTO claim_watches
            (watch_id, principal_id, domain, visibility, selector_type,
             selector_value, event_types_json, stale_after_ms, status,
             created_at_ms, updated_at_ms, deleted_at_ms, last_watermark,
             last_state_json, retained_from_sequence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL, NULL, 0)
        """,
        [
            watch_id,
            principal,
            backing.definition.name,
            visibility,
            selector_type,
            selector_value,
            _canonical(selected_types),
            stale_after,
            created,
            created,
        ],
    )
    row = conn.execute(
        f"SELECT {_WATCH_COLUMNS} FROM claim_watches WHERE watch_id = ?",
        [watch_id],
    ).fetchone()
    if row is None or row[8] == "deleted":
        raise WatchError("watch_deleted", "an identical watch was previously deleted")
    _audit(conn, principal, watch_id, "create", {"status": row[8]}, at_ms=created)
    return _watch_from_row(row)


def list_watches(
    conn: Any,
    principal_id: str,
    *,
    domain: str | None = None,
    include_deleted: bool = False,
) -> list[dict[str, Any]]:
    """List only watches owned by the authenticated principal."""
    principal = _principal(principal_id)
    ensure_watch_schema(conn)
    clauses = ["principal_id = ?"]
    params: list[Any] = [principal]
    if domain:
        clauses.append("domain = ?")
        params.append(str(domain))
    if not include_deleted:
        clauses.append("status != 'deleted'")
    rows = conn.execute(
        f"SELECT {_WATCH_COLUMNS} FROM claim_watches"
        f" WHERE {' AND '.join(clauses)} ORDER BY created_at_ms, watch_id",
        params,
    ).fetchall()
    _audit(conn, principal, None, "list", {"count": len(rows)})
    return [_watch_from_row(row) for row in rows]


def _owned_watch(
    conn: Any, principal_id: str, watch_id: str, *, allow_deleted: bool = False
) -> tuple[dict[str, Any], str | None]:
    principal = _principal(principal_id)
    ensure_watch_schema(conn)
    row = conn.execute(
        f"SELECT {_WATCH_COLUMNS}, last_state_json FROM claim_watches"
        " WHERE watch_id = ?",
        [str(watch_id)],
    ).fetchone()
    if row is None:
        raise WatchError("watch_not_found", f"watch {watch_id!r} was not found")
    if row[1] != principal:
        raise WatchError("unauthorized", "the watch belongs to another principal")
    if row[8] == "deleted" and not allow_deleted:
        raise WatchError("watch_not_found", f"watch {watch_id!r} was not found")
    return _watch_from_row(row[:13]), row[13]


def set_watch_status(
    conn: Any,
    principal_id: str,
    watch_id: str,
    status: str,
    *,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Pause or resume a watch; applying the current state is idempotent."""
    if status not in {"active", "paused"}:
        raise WatchError("bad_request", "status must be active or paused")
    watch, _state = _owned_watch(conn, principal_id, watch_id)
    if watch["status"] == status:
        return watch
    updated = _timestamp(now_ms)
    conn.execute(
        "UPDATE claim_watches SET status = ?, updated_at_ms = ? WHERE watch_id = ?",
        [status, updated, watch_id],
    )
    _audit(
        conn,
        watch["principal_id"],
        watch_id,
        "resume" if status == "active" else "pause",
        {"status": status},
        at_ms=updated,
    )
    row = conn.execute(
        f"SELECT {_WATCH_COLUMNS} FROM claim_watches WHERE watch_id = ?",
        [watch_id],
    ).fetchone()
    return _watch_from_row(row)


def delete_watch(
    conn: Any,
    principal_id: str,
    watch_id: str,
    *,
    confirm: bool,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Soft-delete a watch only after explicit confirmation.

    Append-only events and snapshots remain retained for audit and deterministic
    replay, but normal list/poll access treats the deleted watch as absent.
    """
    if confirm is not True:
        raise WatchError(
            "confirmation_required", "delete requires confirm=true"
        )
    watch, _state = _owned_watch(
        conn, principal_id, watch_id, allow_deleted=True
    )
    if watch["status"] != "deleted":
        deleted = _timestamp(now_ms)
        conn.execute(
            "UPDATE claim_watches SET status = 'deleted', deleted_at_ms = ?,"
            " updated_at_ms = ? WHERE watch_id = ?",
            [deleted, deleted, watch_id],
        )
        _audit(
            conn,
            watch["principal_id"],
            watch_id,
            "delete",
            {"status": "deleted", "events_retained": True},
            at_ms=deleted,
        )
    return {
        "watch_contract": WATCH_CONTRACT_VERSION,
        "watch_id": watch_id,
        "domain": watch["domain"],
        "status": "deleted",
        "events_retained": True,
    }


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(value) if len(token) > 1}


def _selector_matches(
    selector_type: str,
    selector_value: str,
    cluster: Mapping[str, Any],
) -> bool:
    citations = cluster.get("citations", [])
    if selector_type == "claim":
        return any(str(row.get("claim_id")) == selector_value for row in citations)
    text = " ".join(
        str(row.get("claim_text") or "") for row in citations
    ).casefold()
    needle = selector_value.casefold()
    if selector_type == "entity":
        return needle in text
    wanted = _tokens(needle)
    if not wanted:
        return False
    return len(wanted & _tokens(text)) / len(wanted) >= 0.5


def _locator(
    row: Mapping[str, Any], documents: Mapping[str, Mapping[str, Any]], visibility: str
) -> dict[str, Any]:
    document_id = row.get("document_id")
    document = documents.get(str(document_id), {}) if document_id else {}
    source = row.get("source") or row.get("source_id") or document.get("source_id")
    url = row.get("url") or document.get("url")
    path = row.get("path") or (str(document_id) if document_id else None)
    return {
        "document_id": document_id,
        "claim_id": row.get("claim_id"),
        "source": source or "unknown",
        "url": url,
        "path": path,
        "cited": bool(document_id and (document or source or url)),
        "visibility": visibility,
    }


def _dedupe_evidence(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        item = dict(row)
        marker = (item.get("document_id"), item.get("claim_id"), item.get("path"))
        if marker not in seen:
            seen.add(marker)
            found.append(item)
    return sorted(
        found,
        key=lambda item: (
            str(item.get("document_id") or ""),
            str(item.get("claim_id") or ""),
            str(item.get("path") or ""),
        ),
    )


def _integrity_state(backing: Any, evidence: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    document_ids = sorted(
        {
            str(row["document_id"])
            for row in evidence
            if row.get("document_id") and row.get("cited")
        }
    )
    if not document_ids:
        return []
    ledger = backing.integrity_evidence(document_ids)
    visibility = _visibility(backing)
    documents = {str(row["document_id"]): row for row in backing.documents(limit=100_000)}
    findings = []
    for item in ledger.get("findings", []):
        locators = _dedupe_evidence(
            _locator(row, documents, visibility)
            for row in item.get("evidence", [])
            if isinstance(row, Mapping)
        )
        finding = {
            "fingerprint": _digest(
                {
                    "document_id": item.get("document_id"),
                    "kind": item.get("kind"),
                    "change_class": item.get("change_class"),
                    "evidence": [row.get("path") for row in locators],
                }
            )[:24],
            "document_id": item.get("document_id"),
            "kind": str(item.get("kind") or "unknown"),
            "change_class": item.get("change_class"),
            "severity": str(item.get("severity") or "review"),
            "evidence": locators,
        }
        findings.append(finding)
    return sorted(findings, key=lambda item: item["fingerprint"])


def _snapshot(backing: Any, watch: Mapping[str, Any], observed_at_ms: int) -> dict[str, Any]:
    documents_list = backing.documents(limit=100_000)
    documents = {
        str(row["document_id"]): dict(row)
        for row in documents_list
        if row.get("document_id")
    }
    clusters = backing.claims(limit=100_000)
    visibility = str(watch["visibility"])
    citation_index: dict[str, dict[str, Any]] = {}
    for cluster in clusters:
        for citation in cluster.get("citations", []):
            if citation.get("claim_id"):
                citation_index[str(citation["claim_id"])] = _locator(
                    citation, documents, visibility
                )

    matched = [
        cluster
        for cluster in clusters
        if _selector_matches(
            str(watch["selector"]["type"]),
            str(watch["selector"]["value"]),
            cluster,
        )
    ]
    claim_states = []
    all_evidence: list[dict[str, Any]] = []
    for cluster in sorted(matched, key=lambda item: str(item.get("cluster_id") or "")):
        support = _dedupe_evidence(
            _locator(row, documents, visibility)
            for row in cluster.get("citations", [])
        )
        contradictions = []
        contradiction_keys = []
        for conflict in cluster.get("contradictions", []):
            claim_id = str(conflict.get("claim_id") or "")
            locator = citation_index.get(claim_id)
            if locator is None:
                locator = _locator(conflict, documents, visibility)
            contradictions.append(locator)
            contradiction_keys.append(claim_id or _digest(conflict)[:16])
        contradictions = _dedupe_evidence(contradictions)
        sources = sorted(
            {
                str(row["source"]).strip().casefold()
                for row in support
                if row.get("cited") and row.get("source") not in {None, "", "unknown"}
            }
        )
        quantitative = {}
        for row in cluster.get("citations", []):
            claim_id = str(row.get("claim_id") or "")
            if not claim_id:
                continue
            check = backing.quantitative_check(claim_id)
            quantitative[claim_id] = (
                str(check.get("verdict"))
                if isinstance(check, Mapping) and check.get("verdict")
                else "unverifiable"
            )
        independence = cluster.get("independence") or {
            "method": "distinct-source-fallback-v1",
            "publication_count": sum(1 for row in support if row.get("cited")),
            "independent_source_count": len(sources),
            "probable_origin_count": len(sources),
            "unresolved_count": 0,
            "dependency_evidence": [],
        }
        state = {
            "cluster_id": str(cluster.get("cluster_id") or ""),
            "claim_ids": sorted(
                str(row["claim_id"])
                for row in cluster.get("citations", [])
                if row.get("claim_id")
            ),
            "support": support,
            "contradictions": contradictions,
            "contradiction_keys": sorted(set(contradiction_keys)),
            "independence": independence,
            "quantitative_verdicts": quantitative,
        }
        claim_states.append(state)
        all_evidence.extend(support)
        all_evidence.extend(contradictions)

    # Saved queries/topics/entities can still watch matching source documents
    # before claim extraction has run. Claim-id selectors intentionally cannot.
    if not claim_states and watch["selector"]["type"] != "claim":
        for document in backing.search(str(watch["selector"]["value"]), limit=500):
            locator = _locator(document, documents, visibility)
            claim_states.append(
                {
                    "cluster_id": f"document:{document.get('document_id')}",
                    "claim_ids": [],
                    "support": [locator],
                    "contradictions": [],
                    "contradiction_keys": [],
                    "independence": {
                        "method": "distinct-source-v1",
                        "publication_count": int(locator["cited"]),
                        "independent_source_count": int(
                            locator["cited"] and locator["source"] != "unknown"
                        ),
                        "origins": (
                            [str(locator["source"]).strip().casefold()]
                            if locator["cited"] and locator["source"] != "unknown"
                            else []
                        ),
                    },
                    "quantitative_verdicts": {},
                }
            )
            all_evidence.append(locator)

    evidence = _dedupe_evidence(all_evidence)
    integrity = _integrity_state(backing, evidence)
    latest_by_source: dict[str, dict[str, Any]] = {}
    for document in documents_list:
        source = str(document.get("source_id") or "").strip().casefold()
        if not source:
            continue
        previous = latest_by_source.get(source)
        if previous is None or int(document.get("ingested_at") or 0) > int(
            previous.get("ingested_at") or 0
        ):
            latest_by_source[source] = dict(document)
    last_ingested = max(
        (int(document.get("ingested_at") or 0) for document in documents_list),
        default=0,
    )
    stale_after = int(watch["stale_after_ms"])
    coverage_stale = bool(
        documents_list and observed_at_ms - last_ingested > stale_after
    )
    delivery = []
    for feed in backing.definition.feeds:
        key = str(feed.name or feed.url).strip().casefold()
        latest = latest_by_source.get(key)
        last_seen = int(latest.get("ingested_at") or 0) if latest else None
        delivery.append(
            {
                "source_key": key,
                "last_seen_ms": last_seen,
                "stale": bool(
                    latest is not None and observed_at_ms - int(last_seen or 0) > stale_after
                ),
                "evidence": (
                    [_locator(latest, documents, visibility)] if latest is not None else []
                ),
            }
        )
    return {
        "selector": dict(watch["selector"]),
        "watermark_observed_at_ms": observed_at_ms,
        "claims": claim_states,
        "integrity_findings": integrity,
        "coverage": {
            "last_ingested_ms": last_ingested or None,
            "stale": coverage_stale,
            "evidence": evidence[-5:],
            "source_delivery": sorted(delivery, key=lambda item: item["source_key"]),
        },
        "n": len(claim_states),
        "method": "deterministic selector snapshot v1",
        "assumptions": [
            "distinct normalized source identities approximate reporting origins",
            "a citation records provenance and does not establish truth",
            "watermarks are scanned only after an explicit committed marker",
        ],
    }


def _keyed_claims(state: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {str(row["cluster_id"]): row for row in state.get("claims", [])}


def _evidence_map(claim: Mapping[str, Any], field: str) -> dict[str, dict[str, Any]]:
    return {
        f"{row.get('document_id')}|{row.get('claim_id')}|{row.get('path')}": dict(row)
        for row in claim.get(field, [])
    }


def _event_spec(
    event_type: str,
    reason_code: str,
    explanation: str,
    evidence: Iterable[Mapping[str, Any]],
    delta_key: str,
) -> dict[str, Any] | None:
    locators = _dedupe_evidence(evidence)
    if not locators:
        return None
    return {
        "event_type": event_type,
        "reason_code": reason_code,
        "explanation": explanation,
        "evidence": locators,
        "delta_key": delta_key,
    }


def _event_specs(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    before_claims = _keyed_claims(before)
    after_claims = _keyed_claims(after)
    for cluster_id in sorted(set(before_claims) | set(after_claims)):
        old = before_claims.get(cluster_id, {})
        new = after_claims.get(cluster_id, {})
        old_support = _evidence_map(old, "support")
        new_support = _evidence_map(new, "support")
        gained = sorted(set(new_support) - set(old_support))
        lost = sorted(set(old_support) - set(new_support))
        if gained:
            spec = _event_spec(
                "support_gained",
                "independent_support_added",
                "A matching claim gained cited supporting evidence.",
                [new_support[key] for key in gained],
                f"{cluster_id}|{'|'.join(gained)}",
            )
            if spec:
                specs.append(spec)
        if lost:
            spec = _event_spec(
                "support_lost",
                "supporting_evidence_removed",
                "Previously matching supporting evidence is no longer present.",
                [old_support[key] for key in lost],
                f"{cluster_id}|{'|'.join(lost)}",
            )
            if spec:
                specs.append(spec)

        old_conflicts = _evidence_map(old, "contradictions")
        new_conflicts = _evidence_map(new, "contradictions")
        old_conflict_keys = set(old.get("contradiction_keys", []))
        new_conflict_keys = set(new.get("contradiction_keys", []))
        added_conflicts = sorted(new_conflict_keys - old_conflict_keys)
        removed_conflicts = sorted(old_conflict_keys - new_conflict_keys)
        if added_conflicts:
            evidence = list(new_conflicts.values()) or list(new_support.values())
            spec = _event_spec(
                "contradiction_added",
                "conflicting_claim_added",
                "A newly linked claim contradicts matching evidence.",
                evidence,
                f"{cluster_id}|{'|'.join(added_conflicts)}",
            )
            if spec:
                specs.append(spec)
        if removed_conflicts:
            evidence = list(old_conflicts.values()) or list(old_support.values())
            spec = _event_spec(
                "contradiction_removed",
                "conflicting_claim_removed",
                "A previously linked contradiction is no longer present.",
                evidence,
                f"{cluster_id}|{'|'.join(removed_conflicts)}",
            )
            if spec:
                specs.append(spec)

        old_independence = old.get("independence")
        new_independence = new.get("independence")
        if old and new and old_independence != new_independence:
            spec = _event_spec(
                "independence_changed",
                "origin_classification_changed",
                "The source-independence classification changed.",
                list(new_support.values()) or list(old_support.values()),
                f"{cluster_id}|{_digest(old_independence)}|{_digest(new_independence)}",
            )
            if spec:
                specs.append(spec)

        old_quant = old.get("quantitative_verdicts", {})
        new_quant = new.get("quantitative_verdicts", {})
        for claim_id in sorted(set(old_quant) & set(new_quant)):
            if old_quant[claim_id] != new_quant[claim_id]:
                spec = _event_spec(
                    "quantitative_verdict_changed",
                    "quantitative_verdict_transition",
                    "A quantitative claim changed verification verdict.",
                    list(new_support.values()) or list(old_support.values()),
                    f"{claim_id}|{old_quant[claim_id]}|{new_quant[claim_id]}",
                )
                if spec:
                    specs.append(spec)

    old_integrity = {
        row["fingerprint"]: row for row in before.get("integrity_findings", [])
    }
    new_integrity = {
        row["fingerprint"]: row for row in after.get("integrity_findings", [])
    }
    for fingerprint in sorted(set(new_integrity) - set(old_integrity)):
        finding = new_integrity[fingerprint]
        reason = {
            "silent_substantive": "silent_edit_detected",
            "retraction": "retraction_detected",
            "takedown": "source_removal_detected",
        }.get(finding.get("change_class"), "integrity_finding_added")
        spec = _event_spec(
            "integrity_changed",
            reason,
            "A cited source acquired a new integrity-ledger finding.",
            finding.get("evidence", []),
            fingerprint,
        )
        if spec:
            specs.append(spec)

    old_guidance = before.get("guidance_status", {})
    new_guidance = after.get("guidance_status", {})
    if (
        old_guidance
        and not old_guidance.get("stale")
        and new_guidance.get("stale")
    ):
        spec = _event_spec(
            "guidance_stale",
            "private_guidance_conflicts_with_newer_public_record",
            "Authorized private guidance conflicts with a newer public record.",
            new_guidance.get("evidence", []),
            str(new_guidance.get("comparison_id") or _digest(new_guidance)),
        )
        if spec:
            specs.append(spec)

    old_coverage = before.get("coverage", {})
    new_coverage = after.get("coverage", {})
    if not old_coverage.get("stale") and new_coverage.get("stale"):
        spec = _event_spec(
            "coverage_stale",
            "domain_coverage_stale",
            "The domain has received no document within the configured freshness window.",
            new_coverage.get("evidence", []),
            str(new_coverage.get("last_ingested_ms")),
        )
        if spec:
            specs.append(spec)
    old_delivery = {
        row["source_key"]: row for row in old_coverage.get("source_delivery", [])
    }
    new_delivery = {
        row["source_key"]: row for row in new_coverage.get("source_delivery", [])
    }
    for source_key in sorted(set(old_delivery) & set(new_delivery)):
        old = old_delivery[source_key]
        new = new_delivery[source_key]
        if not old.get("stale") and new.get("stale"):
            spec = _event_spec(
                "source_delivery_failed",
                "configured_source_not_delivering",
                "A configured source exceeded the delivery freshness window.",
                new.get("evidence", []),
                f"{source_key}|{new.get('last_seen_ms')}",
            )
            if spec:
                specs.append(spec)
    return sorted(
        specs,
        key=lambda item: (
            _EVENT_ORDER[item["event_type"]],
            item["reason_code"],
            item["delta_key"],
        ),
    )


def _insert_event(
    conn: Any,
    watch: Mapping[str, Any],
    watermark: int,
    consolidation: Mapping[str, Any],
    observed_at_ms: int,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    spec: Mapping[str, Any],
) -> bool:
    idempotency = _digest(
        {
            "watch_id": watch["watch_id"],
            "watermark": watermark,
            "event_type": spec["event_type"],
            "reason_code": spec["reason_code"],
            "delta_key": spec["delta_key"],
        }
    )
    event_id = "event:" + idempotency[:24]
    before_count = conn.execute(
        "SELECT COUNT(*) FROM claim_watch_events WHERE idempotency_key = ?",
        [idempotency],
    ).fetchone()[0]
    conn.execute(
        """
        INSERT OR IGNORE INTO claim_watch_events
            (event_sequence, event_id, idempotency_key, watch_id, principal_id,
             domain, visibility, event_type, reason_code, watermark,
             consolidation_json, observed_at_ms, before_json, after_json,
             evidence_json, explanation)
        VALUES (nextval('claim_watch_event_sequence'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            event_id,
            idempotency,
            watch["watch_id"],
            watch["principal_id"],
            watch["domain"],
            watch["visibility"],
            spec["event_type"],
            spec["reason_code"],
            watermark,
            _canonical(consolidation),
            observed_at_ms,
            _canonical(before),
            _canonical(after),
            _canonical(spec["evidence"]),
            spec["explanation"],
        ],
    )
    after_count = conn.execute(
        "SELECT COUNT(*) FROM claim_watch_events WHERE idempotency_key = ?",
        [idempotency],
    ).fetchone()[0]
    return before_count == 0 and after_count == 1


def record_external_snapshot(
    conn: Any,
    principal_id: str,
    watch_id: str,
    watermark: int,
    state: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> dict[str, Any]:
    """Append a canonical connector-produced state to an owned watch.

    This is the reusable bridge for evidence types that are not derived by the
    claim-cluster matcher itself. It retains the same committed-watermark,
    ordering, idempotency, authorization, snapshot, and replay guarantees.
    """
    watch, previous_raw = _owned_watch(conn, principal_id, watch_id)
    previous = _load(previous_raw, None)
    if watch["status"] != "active":
        raise WatchError("bad_request", "only active watches accept snapshots")
    value = _positive_watermark(watermark)
    committed = conn.execute(
        "SELECT consolidation_json FROM claim_watch_watermarks WHERE watermark = ?",
        [value],
    ).fetchone()
    if committed is None:
        raise WatchError(
            "watermark_uncommitted",
            f"watermark {value} has not been marked complete",
        )
    snapshot = dict(state)
    observed = _timestamp(observed_at_ms)
    snapshot.setdefault("watermark_observed_at_ms", observed)
    snapshot.setdefault("n", 0)
    snapshot.setdefault("method", "connector-produced canonical watch state")
    snapshot.setdefault("assumptions", [])
    snapshot.setdefault("selector", watch["selector"])
    snapshot.setdefault("claims", [])
    snapshot.setdefault("integrity_findings", [])
    snapshot.setdefault("coverage", {})
    if watch.get("last_watermark") is not None:
        last_watermark = int(watch["last_watermark"])
        if value < last_watermark:
            raise WatchError(
                "bad_request", "external snapshots must be watermark ordered"
            )
        if value == last_watermark:
            retained = conn.execute(
                "SELECT state_hash FROM claim_watch_snapshots"
                " WHERE watch_id = ? AND watermark = ?",
                [watch_id, value],
            ).fetchone()
            if retained is None or retained[0] != _digest(snapshot):
                raise WatchError(
                    "watermark_conflict",
                    "a different state is already retained at this watermark",
                )
            return {
                "watch_contract": WATCH_CONTRACT_VERSION,
                "watch_id": watch_id,
                "domain": watch["domain"],
                "watermark": value,
                "emitted_events": 0,
                "n": 0,
            }
    specs = [] if previous is None else _event_specs(previous, snapshot)
    specs = [spec for spec in specs if spec["event_type"] in set(watch["event_types"])]
    inserted = 0
    conn.execute("BEGIN TRANSACTION")
    try:
        for spec in specs:
            inserted += int(
                _insert_event(
                    conn,
                    watch,
                    value,
                    _load(committed[0], {}),
                    observed,
                    previous,
                    snapshot,
                    spec,
                )
            )
        conn.execute(
            "INSERT OR REPLACE INTO claim_watch_snapshots VALUES (?, ?, ?, ?, ?, ?)",
            [
                watch_id,
                value,
                _canonical(snapshot),
                _digest(snapshot),
                observed,
                committed[0],
            ],
        )
        conn.execute(
            "UPDATE claim_watches SET last_watermark = ?, last_state_json = ?,"
            " updated_at_ms = ? WHERE watch_id = ?",
            [value, _canonical(snapshot), observed, watch_id],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    _audit(
        conn,
        watch["principal_id"],
        watch_id,
        "external_snapshot",
        {"watermark": value, "emitted_events": inserted},
        at_ms=observed,
    )
    return {
        "watch_contract": WATCH_CONTRACT_VERSION,
        "watch_id": watch_id,
        "domain": watch["domain"],
        "watermark": value,
        "emitted_events": inserted,
        "n": inserted,
    }


def _record_failure(
    conn: Any, watch_id: str, watermark: int, exc: Exception, at_ms: int
) -> None:
    error_type = type(exc).__name__[:120]
    conn.execute(
        """
        INSERT INTO claim_watch_failures
            (watch_id, watermark, attempts, error_type, dead_lettered,
             first_at_ms, last_at_ms, resolved_at_ms)
        VALUES (?, ?, 1, ?, FALSE, ?, ?, NULL)
        ON CONFLICT (watch_id, watermark) DO UPDATE SET
            attempts = claim_watch_failures.attempts + 1,
            error_type = excluded.error_type,
            dead_lettered = claim_watch_failures.attempts + 1 >= 3,
            last_at_ms = excluded.last_at_ms,
            resolved_at_ms = NULL
        """,
        [watch_id, watermark, error_type, at_ms, at_ms],
    )


def run_watch_matcher(
    conn: Any,
    registry: Any,
    watermark: int,
    *,
    principal_id: str | None = None,
    observed_at_ms: int | None = None,
) -> dict[str, Any]:
    """Scan active watches at one committed watermark.

    A per-watch transaction covers snapshot/event writes and progress.  A
    failed watch is retriable without affecting other watches or changing the
    order/idempotency of successful logical events.
    """
    ensure_watch_schema(conn)
    value = _positive_watermark(watermark)
    committed = conn.execute(
        "SELECT consolidation_json FROM claim_watch_watermarks WHERE watermark = ?",
        [value],
    ).fetchone()
    if committed is None:
        raise WatchError(
            "watermark_uncommitted",
            f"watermark {value} has not been marked complete",
        )
    principal = _principal(principal_id) if principal_id is not None else None
    params: list[Any] = []
    principal_clause = ""
    if principal is not None:
        principal_clause = " AND principal_id = ?"
        params.append(principal)
    rows = conn.execute(
        f"SELECT {_WATCH_COLUMNS}, last_state_json FROM claim_watches"
        " WHERE status = 'active' AND (last_watermark IS NULL OR last_watermark < ?)"
        f"{principal_clause} ORDER BY watch_id",
        [value, *params],
    ).fetchall()
    observed = _timestamp(observed_at_ms)
    emitted = 0
    processed = 0
    failures = 0
    by_type = {name: 0 for name in EVENT_TYPES}
    for row in rows:
        watch = _watch_from_row(row[:13])
        previous = _load(row[13], None)
        try:
            backing = registry.resolve(watch["domain"], conn=conn)
            _authorize_domain(conn, backing, watch["principal_id"])
            state = _snapshot(backing, watch, observed)
            specs = [] if previous is None else _event_specs(previous, state)
            selected = set(watch["event_types"])
            specs = [spec for spec in specs if spec["event_type"] in selected]
            conn.execute("BEGIN TRANSACTION")
            try:
                for spec in specs:
                    if _insert_event(
                        conn,
                        watch,
                        value,
                        _load(committed[0], {}),
                        observed,
                        previous,
                        state,
                        spec,
                    ):
                        emitted += 1
                        by_type[spec["event_type"]] += 1
                conn.execute(
                    "INSERT OR REPLACE INTO claim_watch_snapshots VALUES"
                    " (?, ?, ?, ?, ?, ?)",
                    [
                        watch["watch_id"],
                        value,
                        _canonical(state),
                        _digest(state),
                        observed,
                        committed[0],
                    ],
                )
                conn.execute(
                    "UPDATE claim_watches SET last_watermark = ?, last_state_json = ?,"
                    " updated_at_ms = ? WHERE watch_id = ?",
                    [value, _canonical(state), observed, watch["watch_id"]],
                )
                conn.execute(
                    "UPDATE claim_watch_failures SET resolved_at_ms = ?"
                    " WHERE watch_id = ? AND watermark = ? AND resolved_at_ms IS NULL",
                    [observed, watch["watch_id"], value],
                )
                conn.execute("COMMIT")
                processed += 1
            except Exception:
                conn.execute("ROLLBACK")
                raise
        except Exception as exc:  # noqa: BLE001 - isolated operational boundary
            failures += 1
            _record_failure(conn, watch["watch_id"], value, exc, observed)
    return {
        "watch_contract": WATCH_CONTRACT_VERSION,
        "watermark": value,
        "processed_watches": processed,
        "emitted_events": emitted,
        "failed_watches": failures,
        "events_by_type": by_type,
        "n": processed,
        "method": "committed-watermark deterministic state transition matcher v1",
        "assumptions": [
            "each committed watermark represents complete consolidation state",
            "failed watches retain their prior progress for ordered retry",
        ],
    }


def _encode_cursor(watch_id: str, sequence: int) -> str:
    payload = _canonical({"v": 1, "watch": watch_id, "sequence": int(sequence)})
    encoded = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(("claim-watch-cursor-v1|" + encoded).encode()).hexdigest()[:12]
    return f"cw1.{encoded}.{checksum}"


def _decode_cursor(cursor: str, watch_id: str) -> int:
    try:
        prefix, encoded, checksum = str(cursor).split(".", 2)
        expected = hashlib.sha256(
            ("claim-watch-cursor-v1|" + encoded).encode()
        ).hexdigest()[:12]
        if prefix != "cw1" or checksum != expected:
            raise ValueError("checksum")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if payload != {
            "v": 1,
            "watch": watch_id,
            "sequence": int(payload["sequence"]),
        }:
            raise ValueError("binding")
        return int(payload["sequence"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WatchError("cursor_stale", "cursor is invalid or belongs to another watch") from exc


def _event_from_row(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "event_contract": WATCH_CONTRACT_VERSION,
        "sequence": int(row[0]),
        "event_id": row[1],
        "watch_id": row[2],
        "domain": row[3],
        "visibility": row[4],
        "event_type": row[5],
        "reason_code": row[6],
        "watermark": int(row[7]),
        "consolidation": _load(row[8], {}),
        "observed_at_ms": int(row[9]),
        "before": _load(row[10], {}),
        "after": _load(row[11], {}),
        "evidence": _load(row[12], []),
        "explanation": row[13],
    }


def poll_watch(
    conn: Any,
    principal_id: str,
    watch_id: str,
    *,
    cursor: str | None = None,
    limit: int = 50,
    event_types: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Poll immutable events strictly after a stable, watch-bound cursor."""
    watch, _state = _owned_watch(conn, principal_id, watch_id)
    try:
        page_limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise WatchError("bad_request", "limit must be an integer") from exc
    if not 1 <= page_limit <= 200:
        raise WatchError("bad_request", "limit must be between 1 and 200")
    selected = _validate_event_types(event_types) if event_types is not None else None
    after_sequence = _decode_cursor(cursor, watch_id) if cursor else 0
    latest = conn.execute(
        "SELECT COALESCE(MAX(event_sequence), 0) FROM claim_watch_events"
        " WHERE watch_id = ?",
        [watch_id],
    ).fetchone()[0]
    retained = conn.execute(
        "SELECT retained_from_sequence FROM claim_watches WHERE watch_id = ?",
        [watch_id],
    ).fetchone()[0]
    if after_sequence > int(latest) or after_sequence < int(retained):
        raise WatchError("cursor_stale", "cursor is outside the retained event range")
    clauses = ["watch_id = ?", "event_sequence > ?"]
    params: list[Any] = [watch_id, after_sequence]
    if selected:
        placeholders = ",".join("?" for _ in selected)
        clauses.append(f"event_type IN ({placeholders})")
        params.extend(selected)
    params.append(page_limit + 1)
    rows = conn.execute(
        "SELECT event_sequence, event_id, watch_id, domain, visibility,"
        " event_type, reason_code, watermark, consolidation_json, observed_at_ms, before_json,"
        " after_json, evidence_json, explanation FROM claim_watch_events"
        f" WHERE {' AND '.join(clauses)} ORDER BY event_sequence LIMIT ?",
        params,
    ).fetchall()
    has_more = len(rows) > page_limit
    page = rows[:page_limit]
    events = [_event_from_row(row) for row in page]
    next_sequence = events[-1]["sequence"] if events else int(latest)
    next_cursor = _encode_cursor(watch_id, next_sequence)
    _audit(
        conn,
        watch["principal_id"],
        watch_id,
        "poll",
        {"returned": len(events), "has_more": has_more},
    )
    return {
        "watch_contract": WATCH_CONTRACT_VERSION,
        "watch_id": watch_id,
        "domain": watch["domain"],
        "events": events,
        "cursor": next_cursor,
        "has_more": has_more,
        "n": len(events),
    }


def replay_watch(
    conn: Any,
    principal_id: str,
    watch_id: str,
    *,
    from_watermark: int,
    to_watermark: int,
) -> dict[str, Any]:
    """Reconstruct logical events from retained committed snapshots."""
    watch, _state = _owned_watch(
        conn, principal_id, watch_id, allow_deleted=True
    )
    start = _positive_watermark(from_watermark)
    end = _positive_watermark(to_watermark)
    if start > end:
        raise WatchError("bad_request", "from_watermark must not exceed to_watermark")
    rows = conn.execute(
        "SELECT watermark, state_json FROM claim_watch_snapshots"
        " WHERE watch_id = ? AND watermark <= ? ORDER BY watermark",
        [watch_id, end],
    ).fetchall()
    reconstructed: list[dict[str, Any]] = []
    for previous, current in pairwise(rows):
        watermark = int(current[0])
        if watermark < start:
            continue
        before = _load(previous[1], {})
        after = _load(current[1], {})
        for spec in _event_specs(before, after):
            if spec["event_type"] not in set(watch["event_types"]):
                continue
            key = _digest(
                {
                    "watch_id": watch_id,
                    "watermark": watermark,
                    "event_type": spec["event_type"],
                    "reason_code": spec["reason_code"],
                    "delta_key": spec["delta_key"],
                }
            )
            reconstructed.append(
                {
                    "watermark": watermark,
                    "event_type": spec["event_type"],
                    "reason_code": spec["reason_code"],
                    "idempotency_key": key,
                }
            )
    stored_rows = conn.execute(
        "SELECT watermark, event_type, reason_code, idempotency_key"
        " FROM claim_watch_events WHERE watch_id = ? AND watermark BETWEEN ? AND ?"
        " ORDER BY event_sequence",
        [watch_id, start, end],
    ).fetchall()
    stored = [
        {
            "watermark": int(row[0]),
            "event_type": row[1],
            "reason_code": row[2],
            "idempotency_key": row[3],
        }
        for row in stored_rows
    ]
    matches = sorted(reconstructed, key=_canonical) == sorted(stored, key=_canonical)
    _audit(
        conn,
        watch["principal_id"],
        watch_id,
        "replay",
        {"from_watermark": start, "to_watermark": end, "matches": matches},
    )
    return {
        "watch_contract": WATCH_CONTRACT_VERSION,
        "watch_id": watch_id,
        "domain": watch["domain"],
        "from_watermark": start,
        "to_watermark": end,
        "matches": matches,
        "reconstructed": reconstructed,
        "stored": stored,
        "n": len(reconstructed),
        "method": "deterministic transition replay over retained watermark snapshots",
        "assumptions": ["snapshots and events are append-only and retained in v1"],
    }


def watch_metrics(conn: Any) -> dict[str, Any]:
    """Return counters and matcher lag without selectors, text, URLs, or secrets."""
    ensure_watch_schema(conn)
    active, paused, deleted = conn.execute(
        "SELECT"
        " SUM(CASE WHEN status='active' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN status='paused' THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN status='deleted' THEN 1 ELSE 0 END)"
        " FROM claim_watches"
    ).fetchone()
    latest = conn.execute(
        "SELECT COALESCE(MAX(watermark), 0) FROM claim_watch_watermarks"
    ).fetchone()[0]
    oldest_progress = conn.execute(
        "SELECT COALESCE(MIN(COALESCE(last_watermark, 0)), 0)"
        " FROM claim_watches WHERE status='active'"
    ).fetchone()[0]
    event_count = conn.execute("SELECT COUNT(*) FROM claim_watch_events").fetchone()[0]
    failures = conn.execute(
        "SELECT COUNT(*) FROM claim_watch_failures WHERE resolved_at_ms IS NULL"
    ).fetchone()[0]
    dead_letters = conn.execute(
        "SELECT COUNT(*) FROM claim_watch_failures WHERE dead_lettered"
    ).fetchone()[0]
    return {
        "watch_contract": WATCH_CONTRACT_VERSION,
        "active_watches": int(active or 0),
        "paused_watches": int(paused or 0),
        "deleted_watches": int(deleted or 0),
        "event_count": int(event_count or 0),
        "unresolved_failures": int(failures or 0),
        "dead_letter_count": int(dead_letters or 0),
        "latest_committed_watermark": int(latest or 0),
        "oldest_active_progress": int(oldest_progress or 0),
        "matcher_lag": (
            max(0, int(latest or 0) - int(oldest_progress or 0))
            if int(active or 0)
            else 0
        ),
    }


def audit_entries(conn: Any, principal_id: str) -> list[dict[str, Any]]:
    """Read the principal's text-free lifecycle/read audit entries."""
    principal = _principal(principal_id)
    ensure_watch_schema(conn)
    rows = conn.execute(
        "SELECT audit_sequence, watch_id, action, at_ms, details_json"
        " FROM claim_watch_audit WHERE principal_id = ? ORDER BY audit_sequence",
        [principal],
    ).fetchall()
    return [
        {
            "sequence": int(row[0]),
            "watch_id": row[1],
            "action": row[2],
            "at_ms": int(row[3]),
            "details": _load(row[4], {}),
        }
        for row in rows
    ]
