"""Append-only revisions of OSS Ecosystems records (OS02).

One store owns ``noesis-oss-ecosystem-record-v1`` (namespace-scoped,
revision-addressable, one owner). It references Technology package and
version identities (``package_object_id`` / ``immutable_artifact_id``) and
never writes ``technical_objects``, vulnerability or inventory tables.

Revision rules:

* **Keys** are ``record type + source + canonical coordinate (+ normalised
  version)`` - ecosystem-qualified and PEP 503-normalised on PyPI - and never
  change when older data arrives later. Revision ids derive from the record,
  the content and the revision's order key, not from arrival position.
* **Order.** A revision's order key is the source's own modification date
  when it states one, falling back to the observation time, then the arrival
  sequence. "Current" is the revision with the greatest key.
* **Idempotency.** A statement is compared with the revision *in effect at its
  own order key*; equal content adds nothing. A reversion to earlier content is
  a new revision (a correction), never a conflict.
* **Late data.** A revision whose key is older than the current one is kept as
  history and marked ``late``; it never becomes current and never produces a
  change event.
* **Listings.** A complete release listing marks a release the source listed
  before but no longer lists as ``not_observed`` (never ``unpublished``, which
  only a source statement records). A late listing never marks releases first
  seen after it.
* **Licences.** Declarations keep the published fields; the SPDX normalisation
  per pinned list release is attached beside the revision, so acquiring a new
  list never rewrites a declaration.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb import oss_ecosystem_records as rec
from src.kb.oss_ecosystem_records import READ_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.oss_spdx import SpdxList, normalise_declaration

_DDL = """
CREATE SEQUENCE IF NOT EXISTS oss_revision_sequence START 1;
CREATE TABLE IF NOT EXISTS oss_records(
 record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_type TEXT NOT NULL, source TEXT NOT NULL,
 record_key_json TEXT NOT NULL, ecosystem TEXT, coordinate TEXT, version TEXT, repository_key TEXT,
 created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS oss_revisions(
 revision_id TEXT PRIMARY KEY, record_id TEXT NOT NULL, namespace TEXT NOT NULL, content_hash TEXT NOT NULL,
 statement_json TEXT NOT NULL, order_ms BIGINT NOT NULL, source_modified_ms BIGINT, observed_at_ms BIGINT NOT NULL,
 seq BIGINT NOT NULL, late BOOLEAN NOT NULL, run_id TEXT NOT NULL, document_id TEXT);
CREATE TABLE IF NOT EXISTS oss_licence_normalisations(
 revision_id TEXT NOT NULL, list_version TEXT NOT NULL, normalisation_json TEXT NOT NULL,
 PRIMARY KEY(revision_id, list_version));
CREATE TABLE IF NOT EXISTS oss_provider_state(
 namespace TEXT NOT NULL, source TEXT NOT NULL, last_success_ms BIGINT, last_execution TEXT, last_run_id TEXT,
 PRIMARY KEY(namespace, source));
CREATE INDEX IF NOT EXISTS idx_oss_records_coordinate ON oss_records(namespace, coordinate, record_type);
CREATE INDEX IF NOT EXISTS idx_oss_revisions_record ON oss_revisions(record_id, order_ms);
"""
TABLES = (
    "oss_records",
    "oss_revisions",
    "oss_licence_normalisations",
    "oss_provider_state",
)


class OssStoreError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    """The OSS scope plus current namespace access (operator bypasses)."""

    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise OssStoreError(
            "unauthorized", f"{required} and namespace access are required"
        )


def record_id(namespace: str, key: list[Any]) -> str:
    return "oss:" + digest([namespace, *key])[:24]


def _order(revision: Mapping[str, Any]) -> tuple[int, int, int]:
    return (
        int(revision["order_ms"]),
        int(revision["observed_at_ms"]),
        int(revision["seq"]),
    )


class OssEcosystemStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Any = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ readiness

    def ready(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='oss_revisions'"
            ).fetchone()
        )

    def require_ready(
        self, namespace: str, sources: Iterable[str] | None = None
    ) -> None:
        """``not_ready`` until an OSS source has run in the namespace (never a raw database error)."""

        if not self.ready():
            raise OssStoreError("not_ready", "no OSS Ecosystems source has run yet")
        wanted = list(sources or [])
        rows = self.conn.execute(
            "SELECT source FROM oss_provider_state WHERE namespace=? AND last_success_ms IS NOT NULL",
            [namespace],
        ).fetchall()
        ran = {r[0] for r in rows}
        if not ran or (wanted and not ran & set(wanted)):
            raise OssStoreError(
                "not_ready",
                f"no OSS Ecosystems source has run in namespace {namespace!r}",
            )

    # ------------------------------------------------------------ writes

    def apply(
        self,
        namespace: str,
        statements: Iterable[Mapping[str, Any]],
        *,
        run_id: str,
        scopes: Iterable[str],
        observed_at_ms: int | None = None,
        execution: str = "injected",
        document_ids: Mapping[int, str] | None = None,
    ) -> dict[str, int]:
        """Apply validated statements; unchanged content is a no-op, changes are new revisions."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        counts = {
            "revisions": 0,
            "unchanged": 0,
            "late": 0,
            "not_observed": 0,
            "normalisations": 0,
        }
        sources: set[str] = set()
        values = [rec.validate(dict(s)) for s in statements]
        # SPDX lists first so declarations in the same batch are normalised against them.
        values.sort(
            key=lambda v: (
                v["record_type"] != "spdx_list_release",
                v["record_type"] == rec.LISTING,
            )
        )
        for index, value in enumerate(values):
            sources.add(value["source"])
            document = (document_ids or {}).get(index)
            if value["record_type"] == rec.LISTING:
                counts["not_observed"] += self._listing(
                    namespace, value, observed=observed, run_id=run_id
                )
                continue
            outcome = self._apply_one(
                namespace, value, observed=observed, run_id=run_id, document_id=document
            )
            counts[outcome] += 1
            if outcome in {"revisions", "late"}:
                if value["record_type"] == "spdx_list_release":
                    counts["normalisations"] += self._normalise_all(
                        namespace, value["list_version"]
                    )
                elif value["record_type"] == "licence_declaration_revision":
                    counts["normalisations"] += self._normalise_new(namespace, value)
        for source in sorted(sources):
            self.conn.execute(
                "INSERT INTO oss_provider_state VALUES (?,?,?,?,?) ON CONFLICT (namespace, source) DO UPDATE SET "
                "last_success_ms=excluded.last_success_ms, last_execution=excluded.last_execution, "
                "last_run_id=excluded.last_run_id",
                [namespace, source, observed, execution, run_id],
            )
        return counts

    def _record(self, namespace: str, value: Mapping[str, Any]) -> str:
        key = rec.record_key(dict(value))
        rid = record_id(namespace, key)
        if not self.conn.execute(
            "SELECT 1 FROM oss_records WHERE record_id=?", [rid]
        ).fetchone():
            self.conn.execute(
                "INSERT INTO oss_records VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    rid,
                    namespace,
                    value["record_type"],
                    value["source"],
                    canonical(key),
                    value.get("ecosystem"),
                    value.get("coordinate"),
                    value.get("version") or value.get("list_version"),
                    value.get("repository_key"),
                    self.now(),
                ],
            )
        return rid

    def _revisions(self, rid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision_id, content_hash, statement_json, order_ms, source_modified_ms, observed_at_ms, seq, late, "
            "run_id, document_id FROM oss_revisions WHERE record_id=? ORDER BY order_ms, observed_at_ms, seq",
            [rid],
        ).fetchall()
        return [
            {
                "revision_id": r[0],
                "content_hash": r[1],
                "statement": json.loads(r[2]),
                "order_ms": r[3],
                "source_modified_ms": r[4],
                "observed_at_ms": r[5],
                "seq": r[6],
                "late": bool(r[7]),
                "run_id": r[8],
                "document_id": r[9],
            }
            for r in rows
        ]

    def _insert(
        self,
        namespace: str,
        rid: str,
        value: Mapping[str, Any],
        *,
        observed: int,
        run_id: str,
        document_id: str | None,
    ) -> str:
        content_hash = digest(rec.content_of(dict(value)))
        source_modified = rec.ms(value.get("source_modified_at"))
        order_ms = source_modified if source_modified is not None else observed
        history = self._revisions(rid)
        seq = int(
            self.conn.execute("SELECT nextval('oss_revision_sequence')").fetchone()[0]
        )
        key = (order_ms, observed, seq)
        in_effect = [r for r in history if _order(r) <= key]
        if in_effect and in_effect[-1]["content_hash"] == content_hash:
            return "unchanged"
        late = bool(history) and key < _order(history[-1])
        revision_id = "oss-rev:" + digest([rid, content_hash, order_ms, observed])[:24]
        if self.conn.execute(
            "SELECT 1 FROM oss_revisions WHERE revision_id=?", [revision_id]
        ).fetchone():
            return "unchanged"
        self.conn.execute(
            "INSERT INTO oss_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                rid,
                namespace,
                content_hash,
                canonical(dict(value)),
                order_ms,
                source_modified,
                observed,
                seq,
                late,
                run_id,
                document_id,
            ],
        )
        return "late" if late else "revisions"

    def _apply_one(
        self,
        namespace: str,
        value: Mapping[str, Any],
        *,
        observed: int,
        run_id: str,
        document_id: str | None,
    ) -> str:
        if value["record_type"] == "archive_provenance" and value[
            "repository_key"
        ] not in self.asserted_repositories(namespace):
            raise OssStoreError(
                "origin_not_asserted",
                "archive provenance is recorded only for origins a repository link assertion names",
            )
        rid = self._record(namespace, value)
        return self._insert(
            namespace,
            rid,
            value,
            observed=observed,
            run_id=run_id,
            document_id=document_id,
        )

    def _listing(
        self, namespace: str, listing: Mapping[str, Any], *, observed: int, run_id: str
    ) -> int:
        listed = set(listing["versions"])
        source_modified = rec.ms(listing.get("source_modified_at"))
        listing_key = source_modified if source_modified is not None else observed
        added = 0
        rows = self.conn.execute(
            "SELECT record_id, version FROM oss_records WHERE namespace=? AND record_type='release_state_revision' "
            "AND source=? AND coordinate=? ORDER BY version",
            [namespace, listing["source"], listing["coordinate"]],
        ).fetchall()
        for rid, version in rows:
            if version in listed:
                continue
            earlier = [r for r in self._revisions(rid) if r["order_ms"] < listing_key]
            if not earlier or earlier[-1]["statement"]["state"] in {
                "unpublished",
                "not_observed",
            }:
                continue  # first seen after this listing, or already recorded as gone
            previous = earlier[-1]["statement"]
            value = {
                k: v
                for k, v in previous.items()
                if k not in {"state", "reason", "state_stated_at"}
            }
            value.update(
                {
                    "state": "not_observed",
                    "reason": "absent from the source's complete release listing; the source does not "
                    "state that the release was removed",
                }
            )
            if listing.get("source_modified_at"):
                value["source_modified_at"] = listing["source_modified_at"]
            else:
                value.pop("source_modified_at", None)
            outcome = self._insert(
                namespace,
                rid,
                value,
                observed=observed,
                run_id=run_id,
                document_id=None,
            )
            added += outcome != "unchanged"
        return added

    # ------------------------------------------------------------ SPDX normalisation

    def spdx_versions(self, namespace: str) -> list[str]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT version FROM oss_records WHERE namespace=? AND record_type='spdx_list_release'",
            [namespace],
        ).fetchall()

        def key(value: str) -> tuple[int, ...]:
            return tuple(int(p) if p.isdigit() else 0 for p in value.split("."))

        return sorted({r[0] for r in rows if r[0]}, key=key)

    def spdx_list(self, namespace: str, version: str | None = None) -> SpdxList | None:
        versions = self.spdx_versions(namespace)
        if not versions:
            return None
        chosen = version or versions[-1]
        rid = record_id(namespace, ["spdx_list_release", "spdx", chosen])
        history = self._revisions(rid)
        if not history:
            raise OssStoreError(
                "unknown_spdx_list", f"SPDX License List {chosen} has not been acquired"
            )
        return SpdxList.from_content(history[-1]["statement"])

    def _normalise(
        self, revision_id: str, statement: Mapping[str, Any], spdx: SpdxList
    ) -> int:
        if self.conn.execute(
            "SELECT 1 FROM oss_licence_normalisations WHERE revision_id=? AND list_version=?",
            [revision_id, spdx.version],
        ).fetchone():
            return 0
        result = normalise_declaration(
            statement["raw"], spdx, ecosystem=statement["ecosystem"]
        )
        self.conn.execute(
            "INSERT INTO oss_licence_normalisations VALUES (?,?,?)",
            [revision_id, spdx.version, canonical(result)],
        )
        return 1

    def _normalise_new(self, namespace: str, value: Mapping[str, Any]) -> int:
        rid = record_id(namespace, rec.record_key(dict(value)))
        count = 0
        for version in self.spdx_versions(namespace):
            spdx = self.spdx_list(namespace, version)
            for revision in self._revisions(rid):
                count += self._normalise(
                    revision["revision_id"], revision["statement"], spdx
                )
        return count

    def _normalise_all(self, namespace: str, list_version: str) -> int:
        spdx = self.spdx_list(namespace, list_version)
        rows = self.conn.execute(
            "SELECT r.revision_id, r.statement_json FROM oss_revisions r JOIN oss_records o USING(record_id) "
            "WHERE o.namespace=? AND o.record_type='licence_declaration_revision'",
            [namespace],
        ).fetchall()
        return sum(self._normalise(r[0], json.loads(r[1]), spdx) for r in rows)

    def normalisation(
        self, revision_id: str, list_version: str | None
    ) -> dict[str, Any]:
        if list_version is None:
            return {
                "status": "no_spdx_list",
                "reason": "no SPDX License List release has been acquired",
            }
        row = self.conn.execute(
            "SELECT normalisation_json FROM oss_licence_normalisations WHERE revision_id=? AND "
            "list_version=?",
            [revision_id, list_version],
        ).fetchone()
        return (
            json.loads(row[0])
            if row
            else {
                "status": "no_spdx_list",
                "reason": f"not normalised under list {list_version}",
            }
        )

    # ------------------------------------------------------------ reads

    def records(
        self,
        namespace: str,
        *,
        record_type: str | None = None,
        source: str | None = None,
        coordinate: str | None = None,
        version: str | None = None,
        repository_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, record_type, source, ecosystem, coordinate, version, repository_key FROM oss_records "
            "WHERE namespace=? AND (? IS NULL OR record_type=?) AND (? IS NULL OR source=?) AND "
            "(? IS NULL OR coordinate=?) AND (? IS NULL OR version=?) AND (? IS NULL OR repository_key=?) "
            "ORDER BY record_type, source, coordinate, version, record_id",
            [
                namespace,
                record_type,
                record_type,
                source,
                source,
                coordinate,
                coordinate,
                version,
                version,
                repository_key,
                repository_key,
            ],
        ).fetchall()
        return [
            dict(
                zip(
                    (
                        "record_id",
                        "record_type",
                        "source",
                        "ecosystem",
                        "coordinate",
                        "version",
                        "repository_key",
                    ),
                    r,
                )
            )
            for r in rows
        ]

    def history(
        self, record: str, *, acquired_by_ms: int | None = None
    ) -> list[dict[str, Any]]:
        """Revisions in order; only those observed by ``acquired_by_ms`` when given."""

        return [
            r
            for r in self._revisions(record)
            if acquired_by_ms is None or r["observed_at_ms"] <= acquired_by_ms
        ]

    def current(
        self,
        record: str,
        *,
        acquired_by_ms: int | None = None,
        at_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """The revision in effect at ``at_ms`` among those acquired by ``acquired_by_ms`` (latest by default)."""

        history = self.history(record, acquired_by_ms=acquired_by_ms)
        if at_ms is not None:
            history = [r for r in history if r["order_ms"] <= at_ms]
        return history[-1] if history else None

    def revision(self, revision_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT record_id FROM oss_revisions WHERE revision_id=?", [revision_id]
        ).fetchone()
        if row is None:
            return None
        return next(
            r for r in self._revisions(row[0]) if r["revision_id"] == revision_id
        )

    def asserted_repositories(self, namespace: str) -> dict[str, list[dict[str, Any]]]:
        """repository key -> the current link assertions naming it (every source, every package)."""

        result: dict[str, list[dict[str, Any]]] = {}
        for record in self.records(namespace, record_type="repository_link_assertion"):
            revision = self.current(record["record_id"])
            for link in (revision or {}).get("statement", {}).get("links") or []:
                result.setdefault(link["repository_key"], []).append(
                    {
                        "coordinate": record["coordinate"],
                        "source": record["source"],
                        "version": record["version"] or None,
                        "url": link["url"],
                        "field": link["field"],
                        "revision_id": revision["revision_id"],
                    }
                )
        return result

    def provider_state(self, namespace: str) -> dict[str, dict[str, Any]]:
        if not self.ready():
            return {}
        rows = self.conn.execute(
            "SELECT source, last_success_ms, last_execution, last_run_id FROM oss_provider_state "
            "WHERE namespace=? ORDER BY source",
            [namespace],
        ).fetchall()
        return {
            r[0]: {"last_success_ms": r[1], "last_execution": r[2], "last_run_id": r[3]}
            for r in rows
        }

    def generation(self, namespace: str, *, acquired_by_ms: int | None = None) -> str:
        """Changes whenever any source adds a revision (per source: count and newest sequence)."""

        if not self.ready():
            return "oss-gen:" + digest([namespace, []])[:24]
        rows = self.conn.execute(
            "SELECT o.source, count(*), max(r.seq) FROM oss_revisions r JOIN oss_records o USING(record_id) "
            "WHERE o.namespace=? AND (? IS NULL OR r.observed_at_ms<=?) GROUP BY o.source ORDER BY o.source",
            [namespace, acquired_by_ms, acquired_by_ms],
        ).fetchall()
        return "oss-gen:" + digest([namespace, [list(r) for r in rows]])[:24]


class OssEcosystemProjector:
    """Source-pack runtime projector for ``noesis-oss-ecosystem-record-v1`` pages (idempotent on replay)."""

    SERVICE_SCOPES = frozenset({WRITE_SCOPE, READ_SCOPE, "operator"})

    def __init__(self, conn: Any) -> None:
        self.store = OssEcosystemStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("oss") or {}).get("namespace") or "global")

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
        del manifest, page_receipt, principal_id
        by_record = {
            str(d["metadata"].get("source_pack_record_id")): d for d in documents or []
        }
        statements, observed, document_ids = [], None, {}
        for record in records:
            statement = record.get("oss_record")
            if not isinstance(statement, Mapping):
                raise OssStoreError(
                    "invalid_record", "page record is not an OSS ecosystem statement"
                )
            document = by_record.get(str(record.get("id")))
            if document:
                document_ids[len(statements)] = document["document_id"]
                if document.get("ingested_at"):
                    observed = max(observed or 0, int(document["ingested_at"]))
            statements.append(dict(statement))
        counts = self.store.apply(
            self._namespace(source),
            statements,
            run_id=run_id,
            scopes=self.SERVICE_SCOPES,
            observed_at_ms=observed,
            execution="runtime",
            document_ids=document_ids,
        )
        return [{"status": "applied", **counts}]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, status, principal_id
        return None


__all__ = [
    "TABLES",
    "OssEcosystemProjector",
    "OssEcosystemStore",
    "OssStoreError",
    "authorize",
    "record_id",
]
