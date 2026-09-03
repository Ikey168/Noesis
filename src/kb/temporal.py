"""Bitemporal normalization, persistence, migration, and domain queries.

Valid time describes when an assertion applies in the world.  Observation
time describes when Noesis learned it.  The two axes are deliberately never
collapsed in storage; ``as_of`` is only a query shorthand for setting both.

Intervals are half-open (``[valid_from, valid_to)``), observation cutoffs are
inclusive, unknown valid time remains explicit, and conflicting assertions at
the same observation time are preserved rather than resolved by last writer.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TEMPORAL_CONTRACT = "noesis-temporal-v1"
PARSER_NAME = "noesis-temporal-normalizer"
PARSER_VERSION = "1.0.0"
KINDS = {"document", "claim", "entity", "relation", "observation"}
PRECISIONS = {"unknown", "year", "month", "day", "minute", "second", "millisecond"}

_DOCUMENT_TIMES_DDL = """
CREATE TABLE IF NOT EXISTS kb_document_times (
    document_id        TEXT PRIMARY KEY,
    publication_at_ms  BIGINT,
    effective_from_ms  BIGINT,
    effective_to_ms    BIGINT,
    event_from_ms      BIGINT,
    event_to_ms        BIGINT,
    revision_at_ms     BIGINT,
    correction_at_ms   BIGINT,
    retracted_at_ms    BIGINT,
    ingested_at_ms     BIGINT,
    status             TEXT NOT NULL,
    provenance_json    TEXT NOT NULL,
    errors_json        TEXT NOT NULL
)
"""

_QUARANTINE_DDL = """
CREATE TABLE IF NOT EXISTS kb_temporal_quarantine (
    quarantine_id TEXT PRIMARY KEY,
    document_id   TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    errors_json   TEXT NOT NULL,
    quarantined_at_ms BIGINT NOT NULL
)
"""

_ASSERTIONS_DDL = """
CREATE TABLE IF NOT EXISTS kb_temporal_assertions (
    temporal_id          TEXT PRIMARY KEY,
    domain               TEXT NOT NULL,
    backing              TEXT NOT NULL,
    assertion_kind       TEXT NOT NULL,
    assertion_id         TEXT NOT NULL,
    valid_from_ms        BIGINT,
    valid_to_ms          BIGINT,
    observed_at_ms       BIGINT NOT NULL,
    ingested_at_ms       BIGINT NOT NULL,
    retracted_at_ms      BIGINT,
    valid_time_precision TEXT NOT NULL,
    valid_time_approximate BOOLEAN NOT NULL,
    source_reported      BOOLEAN NOT NULL,
    inferred             BOOLEAN NOT NULL,
    source_document_id   TEXT,
    visibility           TEXT NOT NULL,
    payload_json         TEXT NOT NULL,
    temporal_provenance_json TEXT NOT NULL,
    recorded_at_ms       BIGINT NOT NULL
)
"""

_TIME_FIELDS = {
    "publication_at_ms": ("publication_at", "published_at", "created_at"),
    "effective_from_ms": ("effective_from", "effective_at", "effective_date"),
    "effective_to_ms": ("effective_to", "expiry_at", "expires_at"),
    "event_from_ms": ("event_from", "event_at", "event_start"),
    "event_to_ms": ("event_to", "event_end"),
    "revision_at_ms": ("revision_at", "revised_at", "updated_at"),
    "correction_at_ms": ("correction_at", "corrected_at"),
    "retracted_at_ms": ("retracted_at", "withdrawn_at"),
    "ingested_at_ms": ("ingested_at",),
}


class TemporalError(ValueError):
    """Stable temporal-contract error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def ensure_temporal_schema(conn: Any) -> None:
    """Create the additive temporal stores and their query indexes."""

    conn.execute(_DOCUMENT_TIMES_DDL)
    conn.execute(_QUARANTINE_DDL)
    conn.execute(_ASSERTIONS_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_temporal_domain_observed "
        "ON kb_temporal_assertions (domain, observed_at_ms)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_temporal_object "
        "ON kb_temporal_assertions (domain, assertion_kind, assertion_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_temporal_validity "
        "ON kb_temporal_assertions (domain, valid_from_ms, valid_to_ms)"
    )


def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    raw = payload.get("metadata")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def _first(payload: Mapping[str, Any], metadata: Mapping[str, Any], names: tuple[str, ...]):
    for name in names:
        if payload.get(name) is not None:
            return payload[name], name
        if metadata.get(name) is not None:
            return metadata[name], f"metadata.{name}"
    return None, None


def _precision(raw: str) -> str:
    value = raw.strip()
    if re.fullmatch(r"\d{4}", value):
        return "year"
    if re.fullmatch(r"\d{4}-\d{2}", value):
        return "month"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return "day"
    if re.search(r"T\d{2}:\d{2}(?:Z|[+-]\d{2}:?\d{2})?$", value):
        return "minute"
    if re.search(r"T\d{2}:\d{2}:\d{2}", value):
        return "millisecond" if re.search(r"\.\d+", value) else "second"
    return "millisecond"


def parse_source_time(
    value: Any,
    *,
    field: str,
    source_timezone: str | None = None,
) -> tuple[int, dict[str, Any]]:
    """Parse a source timestamp without hiding its original precision/zone."""

    if isinstance(value, bool):
        raise TemporalError("malformed_time", f"{field} cannot be boolean")
    if isinstance(value, (int, float)):
        millis = int(value)
        if millis < 0:
            raise TemporalError("malformed_time", f"{field} must be non-negative")
        return millis, {
            "original": str(value),
            "precision": "millisecond",
            "timezone_assumption": None,
            "parser": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "approximate": False,
        }
    if isinstance(value, (datetime, date)):
        raw = value.isoformat()
    elif isinstance(value, str):
        raw = value.strip()
    else:
        raise TemporalError("malformed_time", f"{field} has an unsupported timestamp type")
    if not raw:
        raise TemporalError("malformed_time", f"{field} is empty")

    approximate = bool(
        re.match(r"^(?:~|circa\s+|about\s+)", raw, re.IGNORECASE)
    )
    cleaned = re.sub(
        r"^(?:~|circa\s+|about\s+)", "", raw, flags=re.IGNORECASE
    )
    precision = _precision(cleaned)
    if re.fullmatch(r"\d{4}", cleaned):
        cleaned = f"{cleaned}-01-01"
    elif re.fullmatch(r"\d{4}-\d{2}", cleaned):
        cleaned = f"{cleaned}-01"
    normalized = cleaned[:-1] + "+00:00" if cleaned.endswith("Z") else cleaned
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TemporalError("malformed_time", f"{field} is not a valid ISO-8601 timestamp") from exc

    assumption = None
    if parsed.tzinfo is None:
        zone_name = source_timezone or "UTC"
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(zone_name))
        except ZoneInfoNotFoundError as exc:
            raise TemporalError("malformed_time", f"unknown source timezone {zone_name!r}") from exc
        assumption = f"source timestamp had no offset; interpreted as {zone_name}"
    millis = int(parsed.astimezone(UTC).timestamp() * 1000)
    if millis < 0:
        raise TemporalError("malformed_time", f"{field} must be non-negative")
    return millis, {
        "original": raw,
        "precision": precision,
        "timezone_assumption": assumption,
        "parser": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "approximate": approximate,
    }


def normalize_document_times(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize heterogeneous document times and surface every uncertainty."""

    metadata = _metadata(payload)
    zone = metadata.get("source_timezone") or payload.get("source_timezone")
    normalized: dict[str, Any] = {field: None for field in _TIME_FIELDS}
    provenance: dict[str, Any] = {}
    errors: list[dict[str, str]] = []
    for target, aliases in _TIME_FIELDS.items():
        value, source_field = _first(payload, metadata, aliases)
        if value is None:
            continue
        try:
            millis, receipt = parse_source_time(
                value, field=source_field or target, source_timezone=str(zone) if zone else None
            )
            normalized[target] = millis
            provenance[target] = {"source_field": source_field, **receipt}
        except TemporalError as exc:
            errors.append({"field": source_field or target, "code": exc.code, "message": str(exc)})

    for start, end, label in (
        ("effective_from_ms", "effective_to_ms", "effective"),
        ("event_from_ms", "event_to_ms", "event"),
    ):
        if normalized[start] is not None and normalized[end] is not None and normalized[start] >= normalized[end]:
            errors.append(
                {
                    "field": label,
                    "code": "impossible_interval",
                    "message": f"{label} interval must satisfy start < end",
                }
            )
    publication = normalized["publication_at_ms"]
    for field in ("revision_at_ms", "correction_at_ms", "retracted_at_ms"):
        if publication is not None and normalized[field] is not None and normalized[field] < publication:
            errors.append(
                {
                    "field": field,
                    "code": "impossible_interval",
                    "message": f"{field} cannot precede publication_at_ms",
                }
            )
    return {
        **normalized,
        "status": "quarantined" if errors else "normalized",
        "provenance": provenance,
        "errors": errors,
    }


def store_document_times(conn: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Persist normalized document times; malformed metadata is quarantined."""

    ensure_temporal_schema(conn)
    document_id = str(payload.get("document_id") or "").strip()
    if not document_id:
        raise TemporalError("bad_request", "document_id is required for temporal normalization")
    result = normalize_document_times(payload)
    fields = list(_TIME_FIELDS)
    existing = conn.execute(
        "SELECT publication_at_ms, effective_from_ms, effective_to_ms, "
        "event_from_ms, event_to_ms, revision_at_ms, correction_at_ms, "
        "retracted_at_ms, ingested_at_ms, provenance_json "
        "FROM kb_document_times WHERE document_id = ?",
        [document_id],
    ).fetchone()
    if existing is not None and result["status"] == "normalized":
        for index, field in enumerate(fields):
            if result[field] is None:
                result[field] = existing[index]
        old_provenance = json.loads(existing[9] or "{}")
        result["provenance"] = {**old_provenance, **result["provenance"]}
    # A malformed later revision must not destroy previously normalized time.
    # Its raw payload still lands in quarantine below for inspection/replay.
    if existing is None or result["status"] == "normalized":
        conn.execute(
            f"INSERT OR REPLACE INTO kb_document_times VALUES ({','.join(['?'] * 13)})",
            [
                document_id,
                *(result[field] for field in fields),
                result["status"],
                json.dumps(result["provenance"], sort_keys=True),
                json.dumps(result["errors"], sort_keys=True),
            ],
        )
    if result["errors"]:
        material = json.dumps(
            {"document_id": document_id, "errors": result["errors"]}, sort_keys=True
        )
        quarantine_id = "tq:" + hashlib.sha256(material.encode()).hexdigest()[:24]
        conn.execute(
            "INSERT OR REPLACE INTO kb_temporal_quarantine VALUES (?, ?, ?, ?, ?)",
            [
                quarantine_id,
                document_id,
                json.dumps(dict(payload), sort_keys=True, default=str),
                json.dumps(result["errors"], sort_keys=True),
                int(time.time() * 1000),
            ],
        )
        result["quarantine_id"] = quarantine_id
    return result


def record_revision_time(
    conn: Any,
    document_id: str,
    fetched_at_ms: int | None,
    change_class: str,
) -> None:
    """Attach a source revision/correction/retraction time to a document."""

    if fetched_at_ms is None:
        return
    metadata: dict[str, Any] = {"revision_at": int(fetched_at_ms)}
    if change_class == "correction_notice":
        metadata["correction_at"] = int(fetched_at_ms)
    if change_class in {"retraction", "takedown"}:
        metadata["retracted_at"] = int(fetched_at_ms)
    store_document_times(
        conn,
        {
            "document_id": document_id,
            "ingested_at": int(fetched_at_ms),
            "metadata": metadata,
        },
    )


def record_temporal_assertion(
    conn: Any,
    *,
    domain: str,
    backing: str,
    assertion_kind: str,
    assertion_id: str,
    payload: Mapping[str, Any],
    observed_at_ms: int,
    ingested_at_ms: int | None = None,
    valid_from_ms: int | None = None,
    valid_to_ms: int | None = None,
    retracted_at_ms: int | None = None,
    valid_time_precision: str = "unknown",
    valid_time_approximate: bool = False,
    source_reported: bool = True,
    inferred: bool = False,
    source_document_id: str | None = None,
    visibility: str = "public",
    temporal_provenance: Mapping[str, Any] | None = None,
) -> str:
    """Append one immutable bitemporal assertion and return its stable id."""

    ensure_temporal_schema(conn)
    kind = str(assertion_kind)
    if kind not in KINDS:
        raise TemporalError("bad_request", f"assertion_kind must be one of {sorted(KINDS)}")
    if visibility not in {"public", "private"}:
        raise TemporalError("bad_request", "visibility must be public or private")
    if backing not in {"corpus-view", "namespace"}:
        raise TemporalError("bad_request", "backing must be corpus-view or namespace")
    if valid_time_precision not in PRECISIONS:
        raise TemporalError("bad_request", f"invalid valid_time_precision {valid_time_precision!r}")
    if not str(domain).strip() or not str(assertion_id).strip():
        raise TemporalError("bad_request", "domain and assertion_id are required")
    try:
        observed = int(observed_at_ms)
        ingested = int(ingested_at_ms if ingested_at_ms is not None else observed)
        valid_from = int(valid_from_ms) if valid_from_ms is not None else None
        valid_to = int(valid_to_ms) if valid_to_ms is not None else None
        retracted = int(retracted_at_ms) if retracted_at_ms is not None else None
    except (TypeError, ValueError) as exc:
        raise TemporalError("bad_time", "temporal bounds must be integer milliseconds") from exc
    if min(observed, ingested, *(value for value in (valid_from, valid_to, retracted) if value is not None)) < 0:
        raise TemporalError("bad_time", "temporal bounds must be non-negative")
    if valid_from is not None and valid_to is not None and valid_from >= valid_to:
        raise TemporalError("impossible_interval", "valid interval must satisfy valid_from < valid_to")
    if retracted is not None and retracted < observed:
        raise TemporalError("impossible_interval", "retracted_at cannot precede observed_at")
    if valid_from is None and valid_to is None:
        valid_time_precision = "unknown"
        source_reported = False

    payload_json = json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), default=str)
    provenance_json = json.dumps(
        dict(temporal_provenance or {}), sort_keys=True, separators=(",", ":"), default=str
    )
    identity = "|".join(
        [
            str(domain), kind, str(assertion_id), str(observed), str(valid_from),
            str(valid_to), str(retracted), str(source_document_id), payload_json,
        ]
    )
    temporal_id = "ta:" + hashlib.sha256(identity.encode()).hexdigest()[:28]
    conn.execute(
        "INSERT OR IGNORE INTO kb_temporal_assertions VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            temporal_id,
            str(domain),
            backing,
            kind,
            str(assertion_id),
            valid_from,
            valid_to,
            observed,
            ingested,
            retracted,
            valid_time_precision,
            bool(valid_time_approximate),
            bool(source_reported),
            bool(inferred),
            str(source_document_id) if source_document_id else None,
            visibility,
            payload_json,
            provenance_json,
            int(time.time() * 1000),
        ],
    )
    return temporal_id


def _document_time_map(conn: Any, documents: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for document in documents:
        document_id = str(document.get("document_id") or "")
        normalized = store_document_times(conn, document)
        if normalized["status"] == "normalized":
            result[document_id] = normalized
    return result


def _valid_basis(times: Mapping[str, Any]) -> tuple[int | None, int | None, str, bool]:
    for field, end in (
        ("effective_from_ms", "effective_to_ms"),
        ("event_from_ms", "event_to_ms"),
        ("publication_at_ms", None),
    ):
        if times.get(field) is not None:
            receipt = times.get("provenance", {}).get(field, {})
            return (
                int(times[field]),
                int(times[end]) if end and times.get(end) is not None else None,
                str(receipt.get("precision") or "millisecond"),
                bool(receipt.get("approximate")),
            )
    return None, None, "unknown", False


def backfill_backing(backing: Any) -> dict[str, int]:
    """Idempotently project one corpus-view or namespace backing into history."""

    conn = backing.conn
    ensure_temporal_schema(conn)
    domain = backing.definition.name
    visibility = "private" if "private" in {str(tag).casefold() for tag in backing.definition.tags} else "public"
    counts = {kind: 0 for kind in KINDS}
    documents = backing.documents(limit=100_000)
    time_map = _document_time_map(conn, documents)
    domain_observed = 0
    for document in documents:
        document_id = str(document.get("document_id") or "")
        times = time_map.get(document_id)
        if not document_id or times is None:
            continue
        observed = times.get("ingested_at_ms")
        inferred = observed is None
        observed = int(observed or 0)
        domain_observed = max(domain_observed, observed)
        valid_from, valid_to, precision, approximate = _valid_basis(times)
        record_temporal_assertion(
            conn,
            domain=domain,
            backing=backing.backing_type,
            assertion_kind="document",
            assertion_id=document_id,
            payload=document,
            observed_at_ms=observed,
            valid_from_ms=valid_from,
            valid_to_ms=valid_to,
            retracted_at_ms=times.get("retracted_at_ms"),
            valid_time_precision=precision,
            valid_time_approximate=approximate,
            source_reported=valid_from is not None,
            inferred=inferred,
            source_document_id=document_id,
            visibility=visibility,
            temporal_provenance=times.get("provenance"),
        )
        counts["document"] += 1

    claim_evidence: dict[str, dict[str, Any]] = {}
    for cluster in backing.claims(limit=100_000):
        for claim in cluster.get("citations", []):
            claim_id = str(claim.get("claim_id") or "")
            document_id = str(claim.get("document_id") or "")
            if not claim_id:
                continue
            claim_evidence[claim_id] = {
                "claim_id": claim_id,
                "document_id": document_id or None,
                "source": claim.get("source"),
            }
            times = time_map.get(document_id, {})
            observed = int(times.get("ingested_at_ms") or claim.get("ingested_at") or 0)
            valid_from, valid_to, precision, approximate = _valid_basis(times)
            record_temporal_assertion(
                conn,
                domain=domain,
                backing=backing.backing_type,
                assertion_kind="claim",
                assertion_id=claim_id,
                payload=claim,
                observed_at_ms=observed,
                valid_from_ms=valid_from,
                valid_to_ms=valid_to,
                retracted_at_ms=times.get("retracted_at_ms"),
                valid_time_precision=precision,
                valid_time_approximate=approximate,
                source_reported=valid_from is not None,
                inferred=not bool(times),
                source_document_id=document_id or None,
                visibility=visibility,
                temporal_provenance=times.get("provenance"),
            )
            counts["claim"] += 1

    for entity in backing.entities():
        entity_id = str(entity.get("canonical_id") or entity.get("entity") or entity.get("name") or "")
        if not entity_id:
            continue
        record_temporal_assertion(
            conn,
            domain=domain,
            backing=backing.backing_type,
            assertion_kind="entity",
            assertion_id=entity_id,
            payload=entity,
            observed_at_ms=domain_observed,
            valid_time_precision="unknown",
            source_reported=False,
            inferred=True,
            visibility=visibility,
        )
        counts["entity"] += 1

    if _table_exists(conn, "claim_links"):
        columns = {row[1] for row in conn.execute("PRAGMA table_info('claim_links')").fetchall()}
        created = "created_at" if "created_at" in columns else "0"
        rows = conn.execute(
            f"SELECT domain_a, claim_a, domain_b, claim_b, relation, method, "
            f"prediction_mode, confidence, model_version, run_id, {created} FROM claim_links "
            "WHERE domain_a = ? OR domain_b = ? ORDER BY claim_a, claim_b, relation",
            [domain, domain],
        ).fetchall()
        for row in rows:
            relation_id = f"{row[1]}:{row[4]}:{row[3]}"
            payload = {
                "domain_a": row[0], "claim_a": row[1], "domain_b": row[2],
                "claim_b": row[3], "relation": row[4], "method": row[5],
                "prediction_mode": row[6], "confidence": row[7],
                "model_version": row[8], "run_id": row[9],
                "transition_evidence": [
                    claim_evidence[claim_id]
                    for claim_id in (str(row[1]), str(row[3]))
                    if claim_id in claim_evidence
                ],
            }
            record_temporal_assertion(
                conn,
                domain=domain,
                backing=backing.backing_type,
                assertion_kind="relation",
                assertion_id=relation_id,
                payload=payload,
                observed_at_ms=int(row[10] or 0),
                valid_time_precision="unknown",
                source_reported=False,
                inferred=False,
                visibility=visibility,
            )
            counts["relation"] += 1

    counts["observation"] += _backfill_observations(
        conn, domain, backing.backing_type, visibility
    )
    return counts


def _table_exists(conn: Any, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone() is not None


def _backfill_observations(conn: Any, domain: str, backing: str, visibility: str) -> int:
    if not (_table_exists(conn, "dataset_series") and _table_exists(conn, "dataset_observations")):
        return 0
    series_rows = conn.execute("SELECT series_id, metadata FROM dataset_series").fetchall()
    eligible = []
    for series_id, raw_metadata in series_rows:
        metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else dict(raw_metadata or {})
        domains = metadata.get("domains") or ([metadata["domain"]] if metadata.get("domain") else [])
        if domain in domains:
            eligible.append(str(series_id))
    count = 0
    for series_id in eligible:
        rows = conn.execute(
            "SELECT period, as_of, value FROM dataset_observations WHERE series_id = ? "
            "ORDER BY as_of, period",
            [series_id],
        ).fetchall()
        for period, as_of, value in rows:
            try:
                valid_from, receipt = parse_source_time(str(period), field="period")
            except TemporalError:
                valid_from, receipt = None, {"precision": "unknown", "original": str(period)}
            record_temporal_assertion(
                conn,
                domain=domain,
                backing=backing,
                assertion_kind="observation",
                assertion_id=f"{series_id}:{period}",
                payload={"series_id": series_id, "period": str(period), "value": value, "as_of": int(as_of)},
                observed_at_ms=int(as_of),
                valid_from_ms=valid_from,
                valid_time_precision=str(receipt.get("precision") or "unknown"),
                source_reported=valid_from is not None,
                inferred=False,
                visibility=visibility,
                temporal_provenance={"valid_from_ms": receipt},
            )
            count += 1
    return count


def classify_temporal_relation(
    base_relation: str,
    *,
    same_source: bool,
    observed_a_ms: int,
    observed_b_ms: int,
    newer_change_class: str | None = None,
) -> str:
    """Turn a model relation into correction/supersession/retraction semantics."""

    if not same_source or observed_a_ms == observed_b_ms:
        return base_relation
    change = str(newer_change_class or "").casefold()
    if change in {"retraction", "takedown"}:
        return "retracts"
    if change == "correction_notice":
        return "corrects"
    if base_relation == "contradicts":
        return "supersedes"
    return base_relation


def _query_time(value: Any, name: str) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, str) and not value.strip().isdigit():
            return parse_source_time(value, field=name)[0]
        parsed = int(value)
    except (TypeError, ValueError, TemporalError) as exc:
        raise TemporalError("bad_time", f"{name} must be epoch milliseconds or ISO-8601") from exc
    if parsed < 0:
        raise TemporalError("bad_time", f"{name} must be non-negative")
    return parsed


def _fingerprint(parts: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(dict(parts), sort_keys=True).encode()).hexdigest()[:20]


def _decode_cursor(
    cursor: str | None, fingerprint: str
) -> tuple[int, int | None, int | None]:
    if not cursor:
        return 0, None, None
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode() + b"=" * (-len(cursor) % 4))
        payload = json.loads(decoded)
        if payload.get("fingerprint") != fingerprint:
            raise TemporalError("cursor_stale", "cursor does not match the temporal query")
        offset = int(payload["offset"])
        if offset < 0:
            raise ValueError
        observed_before = payload.get("observed_before_ms")
        recorded_before = payload.get("recorded_before_ms")
        return (
            offset,
            int(observed_before) if observed_before is not None else None,
            int(recorded_before) if recorded_before is not None else None,
        )
    except TemporalError:
        raise
    except Exception as exc:
        raise TemporalError("bad_cursor", "cursor is malformed") from exc


def _encode_cursor(
    offset: int,
    fingerprint: str,
    observed_before_ms: int,
    recorded_before_ms: int,
) -> str:
    raw = json.dumps(
        {
            "offset": offset,
            "fingerprint": fingerprint,
            "observed_before_ms": observed_before_ms,
            "recorded_before_ms": recorded_before_ms,
        },
        sort_keys=True,
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def query_temporal(
    backing: Any,
    *,
    assertion_kind: str | None = None,
    assertion_id: str | None = None,
    as_of: Any = None,
    valid_at: Any = None,
    observed_before: Any = None,
    history: bool = False,
    include_retracted: bool = False,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Query one authorized domain on the valid-time and observation-time axes."""

    if assertion_kind is not None and assertion_kind not in KINDS:
        raise TemporalError("bad_request", f"assertion_kind must be one of {sorted(KINDS)}")
    try:
        page_size = int(limit)
    except (TypeError, ValueError) as exc:
        raise TemporalError("bad_request", "limit must be an integer") from exc
    if not 1 <= page_size <= 100:
        raise TemporalError("bad_request", "limit must be between 1 and 100")

    backfill = backfill_backing(backing)
    requested_as_of = _query_time(as_of, "as_of")
    requested_valid = _query_time(valid_at, "valid_at")
    requested_observed = _query_time(observed_before, "observed_before")
    effective_valid = requested_valid if requested_valid is not None else requested_as_of
    effective_observed = (
        requested_observed
        if requested_observed is not None
        else requested_as_of
        if requested_as_of is not None
        else int(time.time() * 1000)
    )
    query_identity = {
        "domain": backing.definition.name,
        "kind": assertion_kind,
        "id": assertion_id,
        "valid_at": requested_valid if requested_valid is not None else requested_as_of,
        "observed_before": requested_observed if requested_observed is not None else requested_as_of,
        "history": bool(history),
        "include_retracted": bool(include_retracted),
        "limit": page_size,
    }
    fingerprint = _fingerprint(query_identity)
    offset, cursor_observed, cursor_recorded = _decode_cursor(cursor, fingerprint)
    if cursor_observed is not None:
        effective_observed = cursor_observed
    recorded_before = cursor_recorded or int(time.time() * 1000)

    # Backing is intentionally not a filter: promotion changes where a domain
    # is served, not the identity or visibility of its retained history.
    clauses = ["domain = ?", "observed_at_ms <= ?", "recorded_at_ms <= ?"]
    params: list[Any] = [
        backing.definition.name,
        effective_observed,
        recorded_before,
    ]
    if assertion_kind:
        clauses.append("assertion_kind = ?")
        params.append(assertion_kind)
    if assertion_id:
        clauses.append("assertion_id = ?")
        params.append(str(assertion_id))
    rows = backing.conn.execute(
        "SELECT temporal_id, assertion_kind, assertion_id, valid_from_ms, valid_to_ms, "
        "observed_at_ms, ingested_at_ms, retracted_at_ms, valid_time_precision, "
        "valid_time_approximate, source_reported, inferred, source_document_id, "
        "visibility, payload_json, temporal_provenance_json FROM kb_temporal_assertions "
        f"WHERE {' AND '.join(clauses)} ORDER BY observed_at_ms DESC, assertion_kind, assertion_id, temporal_id",
        params,
    ).fetchall()

    visible = []
    unknown_excluded = 0
    for row in rows:
        valid_from, valid_to, retracted = row[3], row[4], row[7]
        if effective_valid is not None:
            if valid_from is None and valid_to is None:
                unknown_excluded += 1
                continue
            if valid_from is not None and effective_valid < valid_from:
                continue
            if valid_to is not None and effective_valid >= valid_to:
                continue
        if not history and not include_retracted and retracted is not None and retracted <= effective_observed:
            continue
        visible.append(row)

    if not history:
        latest: dict[tuple[str, str], int] = {}
        for row in visible:
            key = (row[1], row[2])
            latest[key] = max(latest.get(key, -1), int(row[5]))
        visible = [row for row in visible if int(row[5]) == latest[(row[1], row[2])]]

    page = visible[offset : offset + page_size]
    items = [
        {
            "temporal_id": row[0],
            "assertion_kind": row[1],
            "assertion_id": row[2],
            "valid_from_ms": row[3],
            "valid_to_ms": row[4],
            "observed_at_ms": int(row[5]),
            "ingested_at_ms": int(row[6]),
            "retracted_at_ms": row[7],
            "valid_time_precision": row[8],
            "valid_time_approximate": bool(row[9]),
            "source_reported": bool(row[10]),
            "inferred": bool(row[11]),
            "source_document_id": row[12],
            "visibility": row[13],
            "payload": json.loads(row[14]),
            "temporal_provenance": json.loads(row[15]),
        }
        for row in page
    ]
    next_offset = offset + len(page)
    limitations = []
    if unknown_excluded:
        limitations.append("assertions with unknown valid time were excluded from valid_at filtering")
    if any(item["inferred"] for item in items):
        limitations.append("some temporal values were provenance-aware migration defaults")
    if effective_valid is None:
        limitations.append("no valid-time filter was requested")
    return {
        "temporal_contract": TEMPORAL_CONTRACT,
        "query_mode": "history" if history else "snapshot",
        "temporal_basis": {
            "requested": {
                "as_of": requested_as_of,
                "valid_at": requested_valid,
                "observed_before": requested_observed,
            },
            "effective": {
                "valid_at_ms": effective_valid,
                "observed_before_ms": effective_observed,
                "recorded_before_ms": recorded_before,
            },
            "precedence": "explicit valid_at/observed_before override the corresponding as_of axis",
            "valid_interval_boundary": "half-open [valid_from_ms, valid_to_ms)",
            "observation_boundary": "inclusive observed_at_ms <= observed_before_ms",
            "backing": backing.backing_type,
            "domain": backing.definition.name,
            "coverage_limitations": limitations,
            "unknown_valid_time_excluded": unknown_excluded,
            "migration_projection": backfill,
        },
        "items": items,
        "page": {
            "limit": page_size,
            "returned": len(items),
            "next_cursor": (
                _encode_cursor(
                    next_offset,
                    fingerprint,
                    effective_observed,
                    recorded_before,
                )
                if next_offset < len(visible)
                else None
            ),
        },
        "n": len(items),
    }


__all__ = [
    "KINDS",
    "PARSER_VERSION",
    "TEMPORAL_CONTRACT",
    "TemporalError",
    "backfill_backing",
    "classify_temporal_relation",
    "ensure_temporal_schema",
    "normalize_document_times",
    "parse_source_time",
    "query_temporal",
    "record_revision_time",
    "record_temporal_assertion",
    "store_document_times",
]
