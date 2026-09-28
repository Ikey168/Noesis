"""Record owner for ``noesis-linguistic-record-v1`` (LG02, #2180): sightings, revision chains and as-of selection.

Every acquired record is a **sighting**: the record key, the source revision it
was seen in, the normalised content hash, the source's own revision date and
the observation time. Distinct contents are stored once per record key
(``ling_bodies``). Revisions are derived at read time from the sightings in
source order, so:

* re-acquiring the same source revision with the same content adds nothing;
* a new source revision whose content equals the one before it (in source
  order) adds a sighting and no revision;
* a changed definition, gloss, form or classification appends a revision; a
  change back (A, B, A) is a new revision, because each revision is compared
  only with the one immediately before it;
* data arriving late (an older extract or release after a newer one) takes its
  place in the chain by the source's own revision date; it never becomes
  "current" and never shifts the ids of other revisions: a revision id is
  ``digest(namespace, record key, source revision, content)``.

"Current" is the last sighting in source order: the source's revision date,
then its revision identifier, then observation order. As-of selection uses the
revision date (a date is in force for its whole day), and an optional
acquisition cutoff limits it to sightings acquired by then. Callers select the
current record per source key first and filter afterwards.

Deleted source records (a deleted Wikidata lexeme) are sightings with
``body.status == "deleted"``; nothing is removed.

The same store serves the source-pack runtime through
:class:`LinguisticsProjector` (mapping ``noesis-linguistic-record-v1``).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.linguistics_records import (
    CONTRACT,
    LinguisticsError,
    canonical,
    content_hash,
    date_key,
    digest,
    iso_from_ms,
    record_key,
    revision_order,
    validate_record,
)

DEFAULT_NAMESPACE = "linguistics"
_DDL = """
CREATE TABLE IF NOT EXISTS ling_bodies (
  namespace TEXT NOT NULL, record_key TEXT NOT NULL, content_hash TEXT NOT NULL, kind TEXT NOT NULL,
  provider TEXT NOT NULL, source_id TEXT NOT NULL, body_json TEXT NOT NULL,
  PRIMARY KEY(namespace, record_key, content_hash)
);
CREATE TABLE IF NOT EXISTS ling_sightings (
  namespace TEXT NOT NULL, record_key TEXT NOT NULL, source_revision TEXT NOT NULL, content_hash TEXT NOT NULL,
  kind TEXT NOT NULL, provider TEXT NOT NULL, revision_order TEXT NOT NULL, revision_date TEXT,
  observed_at_ms BIGINT NOT NULL, seq BIGINT NOT NULL, run_id TEXT NOT NULL, source_json TEXT NOT NULL,
  PRIMARY KEY(namespace, record_key, source_revision, content_hash)
);
CREATE TABLE IF NOT EXISTS ling_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, provider TEXT NOT NULL, document TEXT NOT NULL,
  receipt_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, provider, document)
);
"""
TABLES = ("ling_bodies", "ling_sightings", "ling_receipts")


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
    )


class LinguisticsStore:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ readiness

    def ready(self) -> bool:
        return table_exists(self.conn, "ling_sightings")

    def require_ready(self, provider: str | None = None) -> None:
        """``not_ready`` until the store exists (and, with ``provider``, until that source ran)."""
        if not self.ready():
            raise LinguisticsError(
                "not_ready",
                "no linguistic source has been acquired yet; run the linguistics "
                "source pack first",
            )
        if (
            provider is not None
            and not self.conn.execute(
                "SELECT 1 FROM ling_sightings WHERE provider=? LIMIT 1", [provider]
            ).fetchone()
        ):
            raise LinguisticsError(
                "not_ready", f"no {provider} source run has been recorded yet"
            )

    # ------------------------------------------------------------ writes

    def apply(
        self,
        namespace: str,
        records: Iterable[Mapping[str, Any]],
        *,
        run_id: str,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        """Store a page of records as sightings; returns counts and the changed keys."""
        counts = {
            "records": 0,
            "duplicate": 0,
            "new": 0,
            "revised": 0,
            "unchanged": 0,
            "historical": 0,
            "conflicting": 0,
        }
        changed: list[str] = []
        validated = [validate_record(r) for r in records]
        self.conn.execute("BEGIN")
        try:
            seq = int(
                self.conn.execute(
                    "SELECT coalesce(max(seq), 0) FROM ling_sightings WHERE namespace=?",
                    [namespace],
                ).fetchone()[0]
            )
            for record in validated:
                counts["records"] += 1
                key, kind, provider = (
                    record_key(record),
                    record["kind"],
                    record["provider"],
                )
                source = {
                    **record["source"],
                    "retrieved_at": iso_from_ms(observed_at_ms),
                }
                chash = content_hash(kind, record["body"])
                revision = str(source["revision"])
                if self.conn.execute(
                    "SELECT 1 FROM ling_sightings WHERE namespace=? AND record_key=? AND source_revision=? "
                    "AND content_hash=?",
                    [namespace, key, revision, chash],
                ).fetchone():
                    counts["duplicate"] += 1
                    continue
                if self.conn.execute(
                    "SELECT 1 FROM ling_sightings WHERE namespace=? AND record_key=? AND source_revision=?",
                    [namespace, key, revision],
                ).fetchone():
                    counts["conflicting"] += (
                        1  # kept side by side and flagged at read time
                    )
                self.conn.execute(
                    "INSERT INTO ling_bodies VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [
                        namespace,
                        key,
                        chash,
                        kind,
                        provider,
                        str(source["source_id"]),
                        canonical(record["body"]),
                    ],
                )
                seq += 1
                self.conn.execute(
                    "INSERT INTO ling_sightings VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        key,
                        revision,
                        chash,
                        kind,
                        provider,
                        revision_order(revision),
                        source.get("revision_date"),
                        int(observed_at_ms),
                        seq,
                        run_id,
                        canonical(source),
                    ],
                )
                ordered = self._ordered(namespace, key)
                index = next(
                    i
                    for i, s in enumerate(ordered)
                    if s["source_revision"] == revision and s["content_hash"] == chash
                )
                if index < len(ordered) - 1:
                    counts["historical"] += 1
                elif index == 0:
                    counts["new"] += 1
                    changed.append(key)
                elif ordered[index - 1]["content_hash"] == chash:
                    counts["unchanged"] += 1
                else:
                    counts["revised"] += 1
                    changed.append(key)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**counts, "changed": changed}

    def record_receipt(
        self,
        namespace: str,
        *,
        run_id: str,
        provider: str,
        document: str,
        receipt: Mapping[str, Any],
        observed_at_ms: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO ling_receipts VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [
                namespace,
                run_id,
                provider,
                document,
                canonical(dict(receipt)),
                int(observed_at_ms),
            ],
        )

    # ------------------------------------------------------------ reads

    @staticmethod
    def _order(sighting: Mapping[str, Any]) -> tuple:
        effective = sighting["revision_date"] or iso_from_ms(sighting["observed_at_ms"])
        return (
            date_key(effective),
            sighting["revision_order"],
            int(sighting["observed_at_ms"]),
            int(sighting["seq"]),
        )

    def _ordered(
        self, namespace: str, key: str, *, acquired_by_ms: int | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.source_revision, s.content_hash, s.kind, s.provider, s.revision_order, s.revision_date, "
            "s.observed_at_ms, s.seq, s.run_id, s.source_json, b.body_json, b.source_id FROM ling_sightings s "
            "JOIN ling_bodies b ON b.namespace=s.namespace AND b.record_key=s.record_key "
            "AND b.content_hash=s.content_hash WHERE s.namespace=? AND s.record_key=? "
            "AND (? IS NULL OR s.observed_at_ms <= ?)",
            [namespace, key, acquired_by_ms, acquired_by_ms],
        ).fetchall()
        sightings = [
            {
                "record_key": key,
                "source_revision": r[0],
                "content_hash": r[1],
                "kind": r[2],
                "provider": r[3],
                "revision_order": r[4],
                "revision_date": r[5],
                "observed_at_ms": int(r[6]),
                "seq": int(r[7]),
                "run_id": r[8],
                "source": json.loads(r[9]),
                "body": json.loads(r[10]),
                "source_id": r[11],
            }
            for r in rows
        ]
        return sorted(sightings, key=self._order)

    @staticmethod
    def revision_id(namespace: str, key: str, source_revision: str, chash: str) -> str:
        return "ling-rev:" + digest([namespace, key, source_revision, chash])[:24]

    def _shape(
        self, namespace: str, sighting: Mapping[str, Any], **extra: Any
    ) -> dict[str, Any]:
        return {
            "contract": CONTRACT,
            "record_key": sighting["record_key"],
            "kind": sighting["kind"],
            "provider": sighting["provider"],
            "source_id": sighting["source_id"],
            "revision_id": self.revision_id(
                namespace,
                sighting["record_key"],
                sighting["source_revision"],
                sighting["content_hash"],
            ),
            "source_revision": sighting["source_revision"],
            "revision_date": sighting["revision_date"],
            "observed_at_ms": sighting["observed_at_ms"],
            "content_hash": sighting["content_hash"],
            "source": sighting["source"],
            "body": sighting["body"],
            **extra,
        }

    def _chain(
        self, namespace: str, ordered: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        revisions: list[dict[str, Any]] = []
        by_revision: dict[str, set[str]] = {}
        for sighting in ordered:
            by_revision.setdefault(sighting["source_revision"], set()).add(
                sighting["content_hash"]
            )
        for sighting in ordered:
            if revisions and revisions[-1]["content_hash"] == sighting["content_hash"]:
                revisions[-1]["restated_in"].append(sighting["source_revision"])
                revisions[-1]["last_seen"] = {
                    "source_revision": sighting["source_revision"],
                    "revision_date": sighting["revision_date"],
                    "retrieved_at": sighting["source"].get("retrieved_at"),
                }
                continue
            conflict = len(by_revision[sighting["source_revision"]]) > 1
            revisions.append(
                self._shape(
                    namespace,
                    sighting,
                    restated_in=[],
                    last_seen={
                        "source_revision": sighting["source_revision"],
                        "revision_date": sighting["revision_date"],
                        "retrieved_at": sighting["source"].get("retrieved_at"),
                    },
                    previous_revision_id=revisions[-1]["revision_id"]
                    if revisions
                    else None,
                    **(
                        {"conflict": "the source revision states more than one content"}
                        if conflict
                        else {}
                    ),
                )
            )
        return revisions

    def history(
        self, namespace: str, key: str, *, acquired_by_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """The revision chain: one entry per change in source order, with the later revisions that restated it."""
        return self._chain(
            namespace, self._ordered(namespace, key, acquired_by_ms=acquired_by_ms)
        )

    def current(
        self,
        namespace: str,
        key: str,
        *,
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """The revision in force: the last change in source order among sightings dated on or before ``as_of``."""
        ordered = self._ordered(namespace, key, acquired_by_ms=acquired_by_ms)
        if as_of is not None:
            cutoff = date_key(as_of, end_of_day=True)
            ordered = [s for s in ordered if self._order(s)[0] <= cutoff]
        chain = self._chain(namespace, ordered)
        return chain[-1] if chain else None

    def keys(
        self, namespace: str, *, kind: str | None = None, provider: str | None = None
    ) -> list[str]:
        if not self.ready():
            return []
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT record_key FROM ling_sightings WHERE namespace=? AND (? IS NULL OR kind=?) "
                "AND (? IS NULL OR provider=?) ORDER BY record_key",
                [namespace, kind, kind, provider, provider],
            ).fetchall()
        ]

    def currents(
        self,
        namespace: str,
        *,
        kind: str,
        provider: str | None = None,
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """The current record of every key of a kind (per source first; callers filter afterwards)."""
        out = []
        for key in self.keys(namespace, kind=kind, provider=provider):
            value = self.current(
                namespace, key, as_of=as_of, acquired_by_ms=acquired_by_ms
            )
            if value is not None:
                out.append(value)
        return out

    def providers(self, namespace: str) -> dict[str, dict[str, Any]]:
        if not self.ready():
            return {}
        return {
            r[0]: {
                "sightings": int(r[1]),
                "last_observed_at_ms": int(r[2]),
                "revisions": sorted(json.loads(r[3])),
            }
            for r in self.conn.execute(
                "SELECT provider, count(*), max(observed_at_ms), to_json(list(DISTINCT source_revision)) "
                "FROM ling_sightings WHERE namespace=? GROUP BY provider ORDER BY provider",
                [namespace],
            ).fetchall()
        }

    def snapshot(self, namespace: str) -> dict[str, Any]:
        """A snapshot id that changes whenever any source adds a sighting (a new revision or a restatement)."""
        rows = (
            []
            if not self.ready()
            else self.conn.execute(
                "SELECT record_key, source_revision, content_hash FROM ling_sightings WHERE namespace=? "
                "ORDER BY record_key, source_revision, content_hash",
                [namespace],
            ).fetchall()
        )
        return {
            "snapshot_id": "ling-snapshot:"
            + digest([namespace, [list(r) for r in rows]])[:24],
            "sightings": len(rows),
            "providers": self.providers(namespace),
        }

    def receipts(
        self, namespace: str, provider: str | None = None
    ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ling_receipts"):
            return []
        return [
            {
                "run_id": r[0],
                "provider": r[1],
                "document": r[2],
                "receipt": json.loads(r[3]),
                "observed_at_ms": int(r[4]),
            }
            for r in self.conn.execute(
                "SELECT run_id, provider, document, receipt_json, observed_at_ms FROM ling_receipts "
                "WHERE namespace=? AND (? IS NULL OR provider=?) ORDER BY observed_at_ms, run_id, document",
                [namespace, provider, provider],
            ).fetchall()
        ]


class LinguisticsProjector:
    """Source-pack runtime projector for ``noesis-linguistic-record-v1`` pages (incremental: one page at a time)."""

    def __init__(self, conn: Any) -> None:
        self.store = LinguisticsStore(conn)

    def project_page(
        self,
        *,
        run_id,
        manifest,
        source,
        records,
        documents,
        page_receipt,
        principal_id,
    ):
        del manifest, principal_id
        declared = dict(source.get("linguistics") or {})
        namespace = str(declared.get("namespace") or DEFAULT_NAMESPACE)
        observed = max(
            (
                int(d["ingested_at"])
                for d in documents or []
                if d.get("ingested_at") is not None
            ),
            default=self.store.now(),
        )
        payload = [
            dict(item["linguistic_record"])
            for item in records
            if item.get("linguistic_record")
        ]
        try:
            counts = self.store.apply(
                namespace, payload, run_id=run_id, observed_at_ms=observed
            )
        except LinguisticsError as exc:
            from src.ingestion.source_packs import SourcePackError

            raise SourcePackError("mapping_failed", str(exc)) from exc
        receipt = dict(page_receipt or {})
        self.store.record_receipt(
            namespace,
            run_id=run_id,
            provider=str(declared.get("provider") or ""),
            document=str(receipt.get("document") or ""),
            receipt={
                **receipt,
                "stored": {k: v for k, v in counts.items() if k != "changed"},
            },
            observed_at_ms=observed,
        )
        return {k: v for k, v in counts.items() if k != "changed"}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}
