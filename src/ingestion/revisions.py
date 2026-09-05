"""Immutable document revisions and committed source-generation deltas.

The mutable ``documents`` table remains the compatibility/current projection;
this module is the authoritative history.  Source-pack observations are staged
against a run and only become queryable as a delta when the run watermark and
change set are committed together by :class:`SourcePackRuntime`.
"""

from __future__ import annotations

from src.kb.retention_coordination import coordinated

import base64
import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from src.ingestion.corrections import (
    CORRECTION_NOTICE,
    RETRACTION,
    TAKEDOWN,
    UNCHANGED,
    classify_change,
)

REVISION_CONTRACT = "noesis-document-revision-v1"
CHANGE_SET_CONTRACT = "noesis-document-change-set-v1"
DELTA_CONTRACT = "noesis-document-generation-delta-v1"
REPLAY_CONTRACT = "noesis-document-delta-replay-v1"

_DDL = """
CREATE TABLE IF NOT EXISTS document_revision_records (
  document_id TEXT NOT NULL, revision BIGINT NOT NULL, revision_id TEXT NOT NULL UNIQUE,
  predecessor_revision_id TEXT, source_id TEXT, pack_id TEXT, run_id TEXT,
  payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL, content_hash TEXT NOT NULL,
  metadata_hash TEXT NOT NULL, change_kind TEXT NOT NULL, change_class TEXT NOT NULL,
  lifecycle TEXT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, committed_watermark BIGINT, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(document_id, revision)
);
CREATE TABLE IF NOT EXISTS document_current_revisions (
  document_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL UNIQUE, revision BIGINT NOT NULL,
  lifecycle TEXT NOT NULL, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_change_records (
  change_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, pack_id TEXT NOT NULL,
  source_id TEXT, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  predecessor_revision_id TEXT, change_kind TEXT NOT NULL, content_hash TEXT NOT NULL,
  payload_hash TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  UNIQUE(run_id, document_id, payload_hash, change_kind)
);
CREATE TABLE IF NOT EXISTS document_change_sets (
  pack_id TEXT NOT NULL, watermark BIGINT NOT NULL, run_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL, item_count BIGINT NOT NULL, counts_json TEXT NOT NULL,
  change_hash TEXT NOT NULL, committed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(pack_id, watermark)
);
CREATE INDEX IF NOT EXISTS idx_revision_observed
  ON document_revision_records(document_id, observed_at_ms);
CREATE INDEX IF NOT EXISTS idx_revision_run ON document_revision_records(run_id);
CREATE INDEX IF NOT EXISTS idx_change_run ON document_change_records(run_id);
"""


class RevisionError(ValueError):
    """Stable validation error safe to expose through MCP."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cursor(value: Mapping[str, Any]) -> str:
    return base64.urlsafe_b64encode(_canonical(value).encode()).decode().rstrip("=")


def _decode_cursor(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
    except Exception as exc:
        raise RevisionError("invalid_cursor", "delta cursor is malformed") from exc


def _stable_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(payload)
    value.pop("ingested_at", None)
    metadata = dict(value.get("metadata") or {})
    # Observation/run identity is lineage, not document metadata.  Excluding it
    # prevents an unchanged source poll from manufacturing a revision.
    metadata.pop("source_pack_run_id", None)
    value["metadata"] = metadata
    return value


def _lifecycle(payload: Mapping[str, Any]) -> str:
    metadata = dict(payload.get("metadata") or {})
    requested = str(
        metadata.get("lifecycle") or metadata.get("status") or "active"
    ).lower()
    if metadata.get("tombstone") or requested in {"deleted", "tombstone", "removed"}:
        return "deleted"
    if requested in {"retracted", "withdrawn"}:
        return "retracted"
    return "active"


class DocumentRevisionStore:
    """Authoritative immutable revision and generation-delta store."""

    def __init__(self, conn: Any, *, initialize: bool = True) -> None:
        self.conn = conn
        if initialize:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(_DDL)
        self.migrate_current_documents()

    def migrate_current_documents(self) -> int:
        """Deterministically seed revision zero for pre-revision databases."""
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        if "documents" not in tables:
            return 0
        rows = self.conn.execute(
            "SELECT document_id,source_type,language,ingested_at,created_at,source_id,url,"
            "canonical_url,content_hash,title,content,content_ref,authors,metadata FROM documents "
            "ORDER BY document_id"
        ).fetchall()
        keys = (
            "document_id",
            "source_type",
            "language",
            "ingested_at",
            "created_at",
            "source_id",
            "url",
            "canonical_url",
            "content_hash",
            "title",
            "content",
            "content_ref",
            "authors",
            "metadata",
        )
        migrated = 0
        for row in rows:
            if self.conn.execute(
                "SELECT 1 FROM document_current_revisions WHERE document_id=?", [row[0]]
            ).fetchone():
                continue
            payload = dict(zip(keys, row))
            payload["authors"] = json.loads(payload["authors"] or "[]")
            payload["metadata"] = json.loads(payload["metadata"] or "{}")
            self.observe(payload, committed_watermark=0, stage_change=False)
            migrated += 1
        return migrated

    @coordinated
    def observe(
        self,
        payload: Mapping[str, Any],
        *,
        committed_watermark: int | None = None,
        stage_change: bool = True,
    ) -> dict[str, Any]:
        """Append a changed revision or stage an unchanged observation."""
        document_id = str(payload["document_id"])
        metadata = dict(payload.get("metadata") or {})
        pack_id = str(metadata.get("source_pack_id") or "direct")
        run_id = str(metadata.get("source_pack_run_id") or f"direct:{document_id}")
        source_id = str(payload.get("source_id") or "") or None
        if pack_id == "direct" and committed_watermark is None:
            committed_watermark = 0
        observed_at = int(payload.get("ingested_at") or payload.get("created_at") or 0)
        stable = _stable_payload(payload)
        payload_json = _canonical(dict(payload))
        payload_hash = _digest(stable)
        content_hash = _digest(str(payload.get("content") or ""))
        metadata_hash = _digest(stable.get("metadata") or {})
        lifecycle = _lifecycle(payload)
        prior = self.conn.execute(
            "SELECT r.revision,r.revision_id,r.payload_hash,"
            "json_extract_string(r.payload_json,'$.content'),r.lifecycle "
            "FROM document_current_revisions c JOIN document_revision_records r "
            "ON r.revision_id=c.revision_id WHERE c.document_id=?",
            [document_id],
        ).fetchone()
        if prior is None:
            revision, predecessor, change_class, change_kind = (
                0,
                None,
                UNCHANGED,
                "added",
            )
        else:
            revision, predecessor = int(prior[0]), str(prior[1])
            if str(prior[2]) == payload_hash and str(prior[4]) == lifecycle:
                result = {
                    "contract": REVISION_CONTRACT,
                    "document_id": document_id,
                    "revision": revision,
                    "revision_id": predecessor,
                    "predecessor_revision_id": None
                    if revision == 0
                    else self._predecessor(predecessor),
                    "change_kind": "unchanged",
                    "change_class": UNCHANGED,
                    "lifecycle": lifecycle,
                    "content_hash": content_hash,
                    "payload_hash": payload_hash,
                    "appended": False,
                }
                if stage_change and pack_id != "direct":
                    self._stage(result, run_id, pack_id, source_id, observed_at)
                return result
            diff = classify_change(prior[3], payload.get("content"))
            revision += 1
            if lifecycle == "deleted":
                change_kind = "deleted"
            elif lifecycle == "retracted" or diff.change_class in {
                RETRACTION,
                TAKEDOWN,
            }:
                lifecycle, change_kind = "retracted", "retracted"
            elif str(prior[3] or "") == str(payload.get("content") or ""):
                change_kind = "metadata_updated"
            elif diff.change_class == CORRECTION_NOTICE:
                change_kind = "corrected"
            else:
                change_kind = "updated"
            change_class = diff.change_class
        revision_id = (
            "document-revision:"
            + _digest([document_id, revision, predecessor, payload_hash, lifecycle])[
                :32
            ]
        )
        valid_from = payload.get("created_at")
        valid_to = metadata.get("valid_to_ms")
        self.conn.execute(
            "INSERT INTO document_revision_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                document_id,
                revision,
                revision_id,
                predecessor,
                source_id,
                pack_id,
                run_id,
                payload_json,
                payload_hash,
                content_hash,
                metadata_hash,
                change_kind,
                change_class,
                lifecycle,
                valid_from,
                valid_to,
                observed_at,
                committed_watermark,
                observed_at,
            ],
        )
        self.conn.execute(
            "INSERT INTO document_current_revisions VALUES (?,?,?,?,?) "
            "ON CONFLICT(document_id) DO UPDATE SET revision_id=excluded.revision_id,"
            "revision=excluded.revision,lifecycle=excluded.lifecycle,updated_at_ms=excluded.updated_at_ms",
            [document_id, revision_id, revision, lifecycle, observed_at],
        )
        result = {
            "contract": REVISION_CONTRACT,
            "document_id": document_id,
            "revision": revision,
            "revision_id": revision_id,
            "predecessor_revision_id": predecessor,
            "change_kind": change_kind,
            "change_class": change_class,
            "lifecycle": lifecycle,
            "content_hash": content_hash,
            "payload_hash": payload_hash,
            "appended": True,
        }
        if stage_change and pack_id != "direct":
            self._stage(result, run_id, pack_id, source_id, observed_at)
        return result

    def _predecessor(self, revision_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT predecessor_revision_id FROM document_revision_records WHERE revision_id=?",
            [revision_id],
        ).fetchone()
        return None if row is None else row[0]

    def _stage(
        self,
        revision: Mapping[str, Any],
        run_id: str,
        pack_id: str,
        source_id: str | None,
        observed_at: int,
    ) -> None:
        change_id = (
            "document-change:"
            + _digest(
                [
                    run_id,
                    revision["document_id"],
                    revision["payload_hash"],
                    revision["change_kind"],
                ]
            )[:32]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO document_change_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                change_id,
                run_id,
                pack_id,
                source_id,
                revision["document_id"],
                revision["revision_id"],
                revision.get("predecessor_revision_id"),
                revision["change_kind"],
                revision["content_hash"],
                revision["payload_hash"],
                observed_at,
            ],
        )

    def commit_change_set(
        self, pack_id: str, watermark: int, run_id: str, *, committed_at_ms: int
    ) -> dict[str, Any]:
        existing = self.conn.execute(
            "SELECT pack_id,watermark,status,item_count,counts_json,change_hash,committed_at_ms "
            "FROM document_change_sets WHERE run_id=?",
            [run_id],
        ).fetchone()
        if existing:
            if existing[0] != pack_id or int(existing[1]) != int(watermark):
                raise RevisionError(
                    "mixed_generation", "run is already committed to another watermark"
                )
            return self._change_set(pack_id, watermark, run_id, existing[2:])
        prior = self.conn.execute(
            "SELECT MAX(watermark) FROM document_change_sets WHERE pack_id=?", [pack_id]
        ).fetchone()[0]
        expected = int(prior or 0) + 1
        if int(watermark) != expected:
            raise RevisionError(
                "watermark_gap", f"expected watermark {expected}, got {watermark}"
            )
        rows = self.conn.execute(
            "SELECT change_id,document_id,revision_id,predecessor_revision_id,change_kind,"
            "content_hash,payload_hash,source_id,observed_at_ms FROM document_change_records "
            "WHERE run_id=? AND pack_id=? ORDER BY document_id,change_id",
            [run_id, pack_id],
        ).fetchall()
        counts = Counter(row[4] for row in rows)
        change_hash = _digest([list(row) for row in rows])
        self.conn.execute(
            "INSERT INTO document_change_sets VALUES (?,?,?,?,?,?,?,?)",
            [
                pack_id,
                watermark,
                run_id,
                "committed",
                len(rows),
                _canonical(dict(sorted(counts.items()))),
                change_hash,
                committed_at_ms,
            ],
        )
        revision_ids = [row[2] for row in rows]
        if revision_ids:
            marks = ",".join("?" for _ in revision_ids)
            self.conn.execute(
                f"UPDATE document_revision_records SET committed_watermark=? WHERE revision_id IN ({marks}) "
                "AND committed_watermark IS NULL",
                [watermark, *revision_ids],
            )
        return {
            "contract": CHANGE_SET_CONTRACT,
            "pack_id": pack_id,
            "watermark": int(watermark),
            "run_id": run_id,
            "status": "committed",
            "item_count": len(rows),
            "counts": dict(sorted(counts.items())),
            "change_hash": change_hash,
            "committed_at_ms": committed_at_ms,
        }

    def _change_set(
        self, pack_id: str, watermark: int, run_id: str, row: Sequence[Any]
    ) -> dict[str, Any]:
        return {
            "contract": CHANGE_SET_CONTRACT,
            "pack_id": pack_id,
            "watermark": int(watermark),
            "run_id": run_id,
            "status": row[0],
            "item_count": int(row[1]),
            "counts": json.loads(row[2]),
            "change_hash": row[3],
            "committed_at_ms": int(row[4]),
        }

    def delta(
        self,
        pack_id: str,
        *,
        from_watermark: int | None = None,
        to_watermark: int | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Return deterministic committed changes for one watermark range."""
        bounds = self.conn.execute(
            "SELECT MIN(watermark),MAX(watermark) FROM document_change_sets "
            "WHERE pack_id=? AND status='committed'",
            [pack_id],
        ).fetchone()
        if bounds[1] is None:
            raise RevisionError(
                "generation_unavailable", f"no committed delta for {pack_id!r}"
            )
        end = int(bounds[1] if to_watermark is None else to_watermark)
        start = int(end if from_watermark is None else from_watermark)
        if start < int(bounds[0]) or end > int(bounds[1]):
            raise RevisionError(
                "generation_unavailable", "requested watermark is not committed"
            )
        if start > end:
            raise RevisionError(
                "reversed_range", "from_watermark must not exceed to_watermark"
            )
        expected = list(range(start, end + 1))
        actual = [
            int(row[0])
            for row in self.conn.execute(
                "SELECT watermark FROM document_change_sets WHERE pack_id=? AND watermark BETWEEN ? AND ? "
                "AND status='committed' ORDER BY watermark",
                [pack_id, start, end],
            ).fetchall()
        ]
        if actual != expected:
            raise RevisionError(
                "watermark_gap", "requested delta range contains an uncommitted gap"
            )
        set_rows = self.conn.execute(
            "SELECT watermark,item_count,counts_json,change_hash FROM document_change_sets "
            "WHERE pack_id=? AND watermark BETWEEN ? AND ? AND status='committed' "
            "ORDER BY watermark",
            [pack_id, start, end],
        ).fetchall()
        total_counts: Counter[str] = Counter()
        for set_row in set_rows:
            total_counts.update(json.loads(set_row[2]))
        total_items = sum(int(set_row[1]) for set_row in set_rows)
        range_hash = _digest([[int(set_row[0]), set_row[3]] for set_row in set_rows])
        decoded = _decode_cursor(cursor)
        if decoded and decoded != {
            "pack_id": pack_id,
            "from": start,
            "to": end,
            "offset": decoded.get("offset"),
        }:
            raise RevisionError(
                "invalid_cursor", "delta cursor belongs to another range"
            )
        offset = int(decoded.get("offset", 0))
        cap = min(max(int(limit), 1), 1000)
        rows = self.conn.execute(
            "SELECT s.watermark,s.run_id,c.change_id,c.document_id,c.revision_id,"
            "c.predecessor_revision_id,c.change_kind,c.content_hash,c.payload_hash,c.source_id,"
            "c.observed_at_ms FROM document_change_sets s JOIN document_change_records c "
            "ON c.run_id=s.run_id WHERE s.pack_id=? AND s.watermark BETWEEN ? AND ? "
            "AND s.status='committed' ORDER BY s.watermark,c.document_id,c.change_id LIMIT ? OFFSET ?",
            [pack_id, start, end, cap + 1, offset],
        ).fetchall()
        page, more = rows[:cap], len(rows) > cap
        keys = (
            "watermark",
            "run_id",
            "change_id",
            "document_id",
            "revision_id",
            "predecessor_revision_id",
            "change_kind",
            "content_hash",
            "payload_hash",
            "source_id",
            "observed_at_ms",
        )
        changes = [dict(zip(keys, row)) for row in page]
        return {
            "contract": DELTA_CONTRACT,
            "pack_id": pack_id,
            "from_watermark": start,
            "to_watermark": end,
            "changes": changes,
            "counts": dict(sorted(total_counts.items())),
            "item_count": total_items,
            "page_count": len(changes),
            "delta_hash": range_hash,
            "next_cursor": _cursor(
                {"pack_id": pack_id, "from": start, "to": end, "offset": offset + cap}
            )
            if more
            else None,
        }

    def replay(
        self, pack_id: str, from_watermark: int, to_watermark: int
    ) -> dict[str, Any]:
        delta = self.delta(
            pack_id,
            from_watermark=from_watermark,
            to_watermark=to_watermark,
            limit=1000,
        )
        if delta["next_cursor"]:
            all_changes = list(delta["changes"])
            cursor = delta["next_cursor"]
            while cursor:
                page = self.delta(
                    pack_id,
                    from_watermark=from_watermark,
                    to_watermark=to_watermark,
                    cursor=cursor,
                    limit=1000,
                )
                all_changes.extend(page["changes"])
                cursor = page["next_cursor"]
            delta["changes"] = all_changes
            delta["page_count"] = len(all_changes)
            delta["next_cursor"] = None
        missing = []
        for item in delta["changes"]:
            row = self.conn.execute(
                "SELECT payload_hash FROM document_revision_records WHERE revision_id=? "
                "AND committed_watermark IS NOT NULL",
                [item["revision_id"]],
            ).fetchone()
            if row is None or row[0] != item["payload_hash"]:
                missing.append(item["revision_id"])
        return {
            "contract": REPLAY_CONTRACT,
            "pack_id": pack_id,
            "from_watermark": from_watermark,
            "to_watermark": to_watermark,
            "verified": not missing,
            "missing_or_mismatched": sorted(missing),
            "item_count": delta["item_count"],
            "delta_hash": delta["delta_hash"],
        }

    def revision(
        self,
        document_id: str,
        *,
        revision: int | None = None,
        generation: int | None = None,
        valid_at: int | None = None,
        observed_before: int | None = None,
        include_retracted: bool = False,
    ) -> dict[str, Any] | None:
        """Select exactly one committed revision with explicit temporal semantics."""
        selectors = sum(
            value is not None
            for value in (revision, generation, valid_at, observed_before)
        )
        if selectors > 1:
            raise RevisionError(
                "mixed_generation",
                "revision/generation/valid_at/observed_before are mutually exclusive",
            )
        if generation is not None:
            pack = self.conn.execute(
                "SELECT pack_id FROM document_revision_records WHERE document_id=? "
                "ORDER BY revision DESC LIMIT 1",
                [document_id],
            ).fetchone()
            if pack and pack[0] == "direct" and int(generation) != 0:
                raise RevisionError(
                    "generation_unavailable",
                    "direct documents only have generation zero",
                )
            if (
                pack
                and pack[0] != "direct"
                and not self.conn.execute(
                    "SELECT 1 FROM document_change_sets WHERE pack_id=? AND watermark=? "
                    "AND status='committed'",
                    [pack[0], int(generation)],
                ).fetchone()
            ):
                raise RevisionError(
                    "generation_unavailable", "requested generation is not committed"
                )
        clauses = ["document_id=?", "committed_watermark IS NOT NULL"]
        params: list[Any] = [document_id]
        if revision is not None:
            clauses.append("revision=?")
            params.append(int(revision))
        if generation is not None:
            clauses.append("committed_watermark<=?")
            params.append(int(generation))
        if valid_at is not None:
            clauses.extend(
                [
                    "(valid_from_ms IS NULL OR valid_from_ms<=?)",
                    "(valid_to_ms IS NULL OR valid_to_ms>?)",
                ]
            )
            params.extend([int(valid_at), int(valid_at)])
        if observed_before is not None:
            clauses.append("observed_at_ms<=?")
            params.append(int(observed_before))
        row = self.conn.execute(
            "SELECT revision,revision_id,predecessor_revision_id,payload_json,payload_hash,"
            "content_hash,metadata_hash,change_kind,change_class,lifecycle,valid_from_ms,"
            "valid_to_ms,observed_at_ms,committed_watermark,pack_id,run_id,source_id "
            f"FROM document_revision_records WHERE {' AND '.join(clauses)} "
            "ORDER BY revision DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        keys = (
            "revision",
            "revision_id",
            "predecessor_revision_id",
            "payload",
            "payload_hash",
            "content_hash",
            "metadata_hash",
            "change_kind",
            "change_class",
            "lifecycle",
            "valid_from_ms",
            "valid_to_ms",
            "observed_at_ms",
            "generation",
            "pack_id",
            "run_id",
            "source_id",
        )
        result = dict(zip(keys, row))
        result["payload"] = json.loads(result["payload"])
        if result['payload'].get('_payload_reclaimed') is True:
            raise RevisionError('payload_reclaimed','revision payload was reclaimed; identity, hashes and lineage remain retained')
        result.update({"contract": REVISION_CONTRACT, "document_id": document_id})
        if not include_retracted and result["lifecycle"] != "active":
            return None
        return result

    def history(
        self, document_id: str, *, include_retracted: bool = True
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision FROM document_revision_records WHERE document_id=? "
            "AND committed_watermark IS NOT NULL ORDER BY revision",
            [document_id],
        ).fetchall()
        return [
            item
            for row in rows
            if (
                item := self.revision(
                    document_id,
                    revision=int(row[0]),
                    include_retracted=include_retracted,
                )
            )
            is not None
        ]

    def health(self) -> dict[str, Any]:
        counts = self.conn.execute(
            "SELECT COUNT(*),COUNT(DISTINCT document_id),COUNT(*) FILTER (WHERE committed_watermark IS NULL) "
            "FROM document_revision_records"
        ).fetchone()
        sets = self.conn.execute(
            "SELECT COUNT(*),MAX(committed_at_ms) FROM document_change_sets"
        ).fetchone()
        return {
            "contract": "noesis-document-revision-health-v1",
            "revisions": int(counts[0]),
            "documents": int(counts[1]),
            "uncommitted_revisions": int(counts[2]),
            "committed_change_sets": int(sets[0]),
            "last_change_set_at_ms": sets[1],
        }


__all__ = [
    "CHANGE_SET_CONTRACT",
    "DELTA_CONTRACT",
    "REPLAY_CONTRACT",
    "REVISION_CONTRACT",
    "DocumentRevisionStore",
    "RevisionError",
]
