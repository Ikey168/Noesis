"""Versioned dataset catalog, bounded tabular ingestion, slices, joins, and lineage."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

CATALOG_CONTRACT = "noesis-dataset-catalog-v1"
RELEASE_CONTRACT = "noesis-dataset-release-v1"
RECEIPT_CONTRACT = "noesis-tabular-ingestion-receipt-v1"
SLICE_CONTRACT = "noesis-dataset-slice-v1"
JOIN_CONTRACT = "noesis-dataset-join-v1"
READ_SCOPE = "knowledge:dataset:read"
WRITE_SCOPE = "knowledge:dataset:write"
INGEST_SCOPE = "knowledge:dataset:ingest"
CALCULATE_SCOPE = "knowledge:dataset:calculate"
NULL_SEMANTICS = {"missing", "not-applicable", "suppressed", "not-collected"}
FORMATS = {"csv", "json", "jsonl", "parquet", "tabular-api"}

_DDL = """
CREATE TABLE IF NOT EXISTS dataset_catalog_identities (
  dataset_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, publisher_id TEXT NOT NULL,
  native_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,publisher_id,native_id)
);
CREATE TABLE IF NOT EXISTS dataset_catalog_revisions (
  dataset_revision_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, namespace TEXT NOT NULL,
  semantic_version TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
  license_json TEXT NOT NULL, tables_json TEXT NOT NULL, code_lists_json TEXT NOT NULL,
  partitions_json TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
  predecessor_revision_id TEXT, generation BIGINT NOT NULL, valid_from_ms BIGINT,
  valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL,
  policy_json TEXT NOT NULL, provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(dataset_id,semantic_version)
);
CREATE TABLE IF NOT EXISTS dataset_catalog_current (
  dataset_id TEXT PRIMARY KEY, dataset_revision_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_releases (
  release_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, dataset_revision_id TEXT NOT NULL,
  namespace TEXT NOT NULL, native_release_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  revision_of TEXT, published_at_ms BIGINT, retrieved_at_ms BIGINT NOT NULL,
  valid_from_ms BIGINT, valid_to_ms BIGINT, generation BIGINT NOT NULL,
  provenance_json TEXT NOT NULL, release_hash TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(namespace,dataset_id,native_release_id)
);
CREATE TABLE IF NOT EXISTS dataset_partitions (
  partition_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, release_id TEXT NOT NULL,
  table_id TEXT NOT NULL, partition_key_json TEXT NOT NULL, row_count BIGINT NOT NULL,
  content_hash TEXT NOT NULL, status TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,release_id,table_id,partition_key_json)
);
CREATE TABLE IF NOT EXISTS dataset_rows_v2 (
  row_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, partition_id TEXT NOT NULL,
  row_index BIGINT NOT NULL, row_key_json TEXT NOT NULL, values_json TEXT NOT NULL,
  null_semantics_json TEXT NOT NULL, provenance_json TEXT NOT NULL, row_hash TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(partition_id,row_index)
);
CREATE TABLE IF NOT EXISTS dataset_ingestion_receipts (
  receipt_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, release_id TEXT NOT NULL,
  table_id TEXT NOT NULL, partition_id TEXT, format TEXT NOT NULL, input_hash TEXT NOT NULL,
  status TEXT NOT NULL, receipt_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(namespace,release_id,table_id,input_hash)
);
CREATE TABLE IF NOT EXISTS dataset_quarantine_v2 (
  quarantine_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, receipt_id TEXT NOT NULL,
  row_index BIGINT NOT NULL, row_hash TEXT NOT NULL, error_json TEXT NOT NULL,
  preview_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_join_transformations (
  transformation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, left_release_id TEXT NOT NULL,
  right_release_id TEXT NOT NULL, left_table_id TEXT NOT NULL, right_table_id TEXT NOT NULL,
  join_json TEXT NOT NULL, preview_hash TEXT NOT NULL, lineage_json TEXT NOT NULL,
  derived_table_id TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,preview_hash)
);
CREATE TABLE IF NOT EXISTS dataset_intelligence_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


class DatasetIntelligenceError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise DatasetIntelligenceError(
            "unauthorized", f"missing required scope {required}"
        )


def _bounded(limit: int, maximum: int) -> int:
    return min(max(int(limit), 1), maximum)


class DatasetIntelligenceStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        audit_id = (
            "dataset-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO dataset_intelligence_audit VALUES (?,?,?,?,?,?,?)",
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

    def _previous_schema(
        self, namespace: str, dataset_id: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT r.tables_json FROM dataset_catalog_current c JOIN dataset_catalog_revisions r ON r.dataset_revision_id=c.dataset_revision_id WHERE r.namespace=? AND c.dataset_id=?",
            [namespace, dataset_id],
        ).fetchone()
        return (
            None
            if not row
            else {table["table_id"]: table for table in _load(row[0], [])}
        )

    def register_dataset(
        self,
        namespace: str,
        publisher_id: str,
        native_id: str,
        semantic_version: str,
        title: str,
        description: str,
        license: Mapping[str, Any],
        tables: Sequence[Mapping[str, Any]],
        code_lists: Sequence[Mapping[str, Any]],
        partitions: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
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
        if not all(
            str(value).strip()
            for value in (namespace, publisher_id, native_id, semantic_version, title)
        ):
            raise DatasetIntelligenceError(
                "invalid_dataset", "dataset identity, version, and title are required"
            )
        if not tables:
            raise DatasetIntelligenceError(
                "invalid_dataset", "at least one table is required"
            )
        dataset_id = "dataset:" + _digest([namespace, publisher_id, native_id])[:24]
        previous = self._previous_schema(namespace, dataset_id) or {}
        normalized_tables = []
        for table in tables:
            table_value = dict(table)
            table_identity = str(
                table_value.get("identity") or table_value.get("name") or ""
            ).strip()
            if not table_identity:
                raise DatasetIntelligenceError(
                    "invalid_schema", "every table requires a stable identity or name"
                )
            table_id = "dataset-table:" + _digest([dataset_id, table_identity])[:24]
            previous_table = previous.get(table_id, {})
            previous_columns = {
                item["name"]: item for item in previous_table.get("columns", [])
            }
            columns = []
            seen_names = set()
            for column in table_value.get("columns", []):
                value = dict(column)
                name = str(value.get("name") or "").strip()
                if not name or name in seen_names:
                    raise DatasetIntelligenceError(
                        "invalid_schema", "column names must be unique and non-empty"
                    )
                seen_names.add(name)
                renamed_from = value.get("renamed_from")
                identity = str(value.get("identity") or renamed_from or name)
                if renamed_from and renamed_from in previous_columns:
                    column_id = previous_columns[renamed_from]["column_id"]
                else:
                    column_id = "dataset-column:" + _digest([table_id, identity])[:24]
                columns.append(
                    {
                        "column_id": column_id,
                        "name": name,
                        "identity": identity,
                        "renamed_from": renamed_from,
                        "type": str(value.get("type", "string")),
                        "nullable": bool(value.get("nullable", True)),
                        "unit": value.get("unit"),
                        "semantic_role": value.get("semantic_role"),
                        "code_list_id": value.get("code_list_id"),
                        "native_id": value.get("native_id", name),
                    }
                )
            if not columns:
                raise DatasetIntelligenceError(
                    "invalid_schema", "every table requires columns"
                )
            normalized_tables.append(
                {
                    "table_id": table_id,
                    "name": str(table_value.get("name", table_identity)),
                    "identity": table_identity,
                    "native_id": table_value.get("native_id", table_identity),
                    "frequency": table_value.get("frequency"),
                    "primary_key": list(table_value.get("primary_key", [])),
                    "columns": columns,
                }
            )
        normalized_codes = []
        for item in code_lists:
            value = dict(item)
            identity = str(
                value.get("identity")
                or value.get("native_id")
                or value.get("name")
                or ""
            )
            normalized_codes.append(
                {
                    **value,
                    "code_list_id": value.get("code_list_id")
                    or "dataset-code-list:" + _digest([dataset_id, identity])[:24],
                }
            )
        now = self.now()
        stable = {
            "dataset_id": dataset_id,
            "namespace": namespace,
            "publisher_id": publisher_id,
            "native_id": native_id,
            "semantic_version": semantic_version,
            "title": title,
            "description": description,
            "license": dict(license),
            "tables": normalized_tables,
            "code_lists": normalized_codes,
            "partitions": [dict(item) for item in partitions],
            "predecessor_revision_id": predecessor_revision_id,
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-dataset-intelligence", "version": "1.0.0"}
            ),
            "policy": dict(policy or {}),
            "provenance": dict(provenance or {}),
        }
        content_hash = _digest(
            {k: v for k, v in stable.items() if k != "observed_at_ms"}
        )
        revision_id = "dataset-revision:" + _digest([dataset_id, semantic_version])[:24]
        existing = self.conn.execute(
            "SELECT content_hash FROM dataset_catalog_revisions WHERE dataset_revision_id=?",
            [revision_id],
        ).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise DatasetIntelligenceError(
                    "immutable_version", "dataset schema version has different content"
                )
            return {
                **self.dataset(
                    namespace, dataset_id, revision_id=revision_id, scopes={READ_SCOPE}
                ),
                "idempotent": True,
            }
        current = self.conn.execute(
            "SELECT dataset_revision_id FROM dataset_catalog_current WHERE dataset_id=?",
            [dataset_id],
        ).fetchone()
        if current and predecessor_revision_id != current[0]:
            raise DatasetIntelligenceError(
                "version_conflict", "schema evolution must name the current predecessor"
            )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO dataset_catalog_identities VALUES (?,?,?,?,?)",
                [dataset_id, namespace, publisher_id, native_id, now],
            )
            self.conn.execute(
                "INSERT INTO dataset_catalog_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    dataset_id,
                    namespace,
                    semantic_version,
                    title,
                    description,
                    _canonical(dict(license)),
                    _canonical(normalized_tables),
                    _canonical(normalized_codes),
                    _canonical([dict(item) for item in partitions]),
                    content_hash,
                    "active",
                    predecessor_revision_id,
                    generation,
                    valid_from_ms,
                    valid_to_ms,
                    stable["observed_at_ms"],
                    _canonical(stable["producer"]),
                    _canonical(stable["policy"]),
                    _canonical(stable["provenance"]),
                    principal_id,
                    now,
                ],
            )
            if predecessor_revision_id:
                self.conn.execute(
                    "UPDATE dataset_catalog_revisions SET status='superseded' WHERE namespace=? AND dataset_revision_id=?",
                    [namespace, predecessor_revision_id],
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO dataset_catalog_current VALUES (?,?)",
                [dataset_id, revision_id],
            )
            self._audit(
                namespace, "register-dataset", revision_id, principal_id, {}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.dataset(
            namespace, dataset_id, revision_id=revision_id, scopes={READ_SCOPE}
        )

    def dataset(
        self,
        namespace: str,
        dataset_id: str,
        *,
        scopes: set[str],
        revision_id: str | None = None,
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        if revision_id is None:
            row = self.conn.execute(
                "SELECT dataset_revision_id FROM dataset_catalog_current WHERE dataset_id=?",
                [dataset_id],
            ).fetchone()
            revision_id = None if not row else row[0]
        row = self.conn.execute(
            "SELECT i.publisher_id,i.native_id,r.semantic_version,r.title,r.description,r.license_json,r.tables_json,r.code_lists_json,r.partitions_json,r.content_hash,r.status,r.predecessor_revision_id,r.generation,r.valid_from_ms,r.valid_to_ms,r.observed_at_ms,r.producer_json,r.policy_json,r.provenance_json,r.principal_id,r.created_at_ms FROM dataset_catalog_revisions r JOIN dataset_catalog_identities i ON i.dataset_id=r.dataset_id WHERE r.namespace=? AND r.dataset_id=? AND r.dataset_revision_id=?",
            [namespace, dataset_id, revision_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": CATALOG_CONTRACT,
            "dataset_id": dataset_id,
            "dataset_revision_id": revision_id,
            "namespace": namespace,
            "publisher_id": row[0],
            "native_id": row[1],
            "semantic_version": row[2],
            "title": row[3],
            "description": row[4],
            "license": _load(row[5], {}),
            "tables": _load(row[6], []),
            "code_lists": _load(row[7], []),
            "partitions": _load(row[8], []),
            "content_hash": row[9],
            "status": row[10],
            "predecessor_revision_id": row[11],
            "generation": int(row[12]),
            "valid_from_ms": row[13],
            "valid_to_ms": row[14],
            "observed_at_ms": int(row[15]),
            "producer": _load(row[16], {}),
            "policy": _load(row[17], {}),
            "provenance": _load(row[18], {}),
            "principal_id": row[19],
            "created_at_ms": int(row[20]),
        }

    def search(
        self,
        namespace: str,
        query: str,
        *,
        scopes: set[str],
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        size = _bounded(limit, 500)
        pattern = f"%{query.casefold()}%"
        rows = self.conn.execute(
            "SELECT r.dataset_id,r.dataset_revision_id FROM dataset_catalog_current c JOIN dataset_catalog_revisions r ON r.dataset_revision_id=c.dataset_revision_id WHERE r.namespace=? AND (lower(r.title) LIKE ? OR lower(r.description) LIKE ? OR lower(r.tables_json) LIKE ?) ORDER BY r.title,r.dataset_id LIMIT ? OFFSET ?",
            [namespace, pattern, pattern, pattern, size + 1, max(0, int(offset))],
        ).fetchall()
        items = [
            self.dataset(namespace, row[0], revision_id=row[1], scopes={READ_SCOPE})
            for row in rows[:size]
        ]
        return {
            "items": items,
            "next_offset": offset + size if len(rows) > size else None,
            "result_hash": _digest([item["dataset_revision_id"] for item in items]),
        }

    def register_release(
        self,
        namespace: str,
        dataset_id: str,
        native_release_id: str,
        vintage_id: str,
        *,
        retrieved_at_ms: int,
        principal_id: str,
        scopes: set[str],
        revision_of: str | None = None,
        published_at_ms: int | None = None,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        generation: int = 0,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        catalog = self.dataset(namespace, dataset_id, scopes={READ_SCOPE})
        if not catalog:
            raise DatasetIntelligenceError(
                "dataset_not_found", "dataset does not exist"
            )
        if revision_of and not self.release(
            namespace, revision_of, scopes={READ_SCOPE}
        ):
            raise DatasetIntelligenceError(
                "release_not_found", "corrected release does not exist"
            )
        stable = {
            "namespace": namespace,
            "dataset_id": dataset_id,
            "dataset_revision_id": catalog["dataset_revision_id"],
            "native_release_id": native_release_id,
            "vintage_id": vintage_id,
            "revision_of": revision_of,
            "published_at_ms": published_at_ms,
            "retrieved_at_ms": int(retrieved_at_ms),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "generation": int(generation),
            "provenance": dict(provenance or {}),
        }
        release_hash = _digest(stable)
        release_id = (
            "dataset-release:"
            + _digest([namespace, dataset_id, native_release_id])[:24]
        )
        existing = self.conn.execute(
            "SELECT release_hash,created_at_ms FROM dataset_releases WHERE release_id=? AND namespace=?",
            [release_id, namespace],
        ).fetchone()
        if existing:
            if existing[0] != release_hash:
                raise DatasetIntelligenceError(
                    "duplicate_release_conflict",
                    "release identity has different content",
                )
            return {
                **self.release(namespace, release_id, scopes={READ_SCOPE}),
                "idempotent": True,
            }
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO dataset_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    release_id,
                    dataset_id,
                    catalog["dataset_revision_id"],
                    namespace,
                    native_release_id,
                    vintage_id,
                    revision_of,
                    published_at_ms,
                    retrieved_at_ms,
                    valid_from_ms,
                    valid_to_ms,
                    generation,
                    _canonical(stable["provenance"]),
                    release_hash,
                    principal_id,
                    now,
                ],
            )
            self._audit(
                namespace, "register-release", release_id, principal_id, {}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.release(namespace, release_id, scopes={READ_SCOPE})

    def release(
        self, namespace: str, release_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT dataset_id,dataset_revision_id,native_release_id,vintage_id,revision_of,published_at_ms,retrieved_at_ms,valid_from_ms,valid_to_ms,generation,provenance_json,release_hash,principal_id,created_at_ms FROM dataset_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone()
        if not row:
            return None
        return {
            "contract": RELEASE_CONTRACT,
            "release_id": release_id,
            "namespace": namespace,
            "dataset_id": row[0],
            "dataset_revision_id": row[1],
            "native_release_id": row[2],
            "vintage_id": row[3],
            "revision_of": row[4],
            "published_at_ms": row[5],
            "retrieved_at_ms": int(row[6]),
            "valid_from_ms": row[7],
            "valid_to_ms": row[8],
            "generation": int(row[9]),
            "provenance": _load(row[10], {}),
            "release_hash": row[11],
            "principal_id": row[12],
            "created_at_ms": int(row[13]),
        }

    def _table(
        self, namespace: str, release_id: str, table_id: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        release = self.release(namespace, release_id, scopes={READ_SCOPE})
        if not release:
            raise DatasetIntelligenceError(
                "release_not_found", "dataset release does not exist"
            )
        catalog = self.dataset(
            namespace,
            release["dataset_id"],
            revision_id=release["dataset_revision_id"],
            scopes={READ_SCOPE},
        )
        table = next(
            (item for item in catalog["tables"] if item["table_id"] == table_id), None
        )
        if not table:
            raise DatasetIntelligenceError(
                "table_not_found", "table does not belong to the pinned dataset schema"
            )
        return release, table

    @staticmethod
    def _parse(format: str, content: str, encoding: str) -> list[dict[str, Any]]:
        if format not in FORMATS:
            raise DatasetIntelligenceError(
                "format_unsupported", "unsupported tabular format"
            )
        try:
            raw = (
                base64.b64decode(content, validate=True)
                if format == "parquet"
                else content.encode(encoding)
            )
            if format != "parquet":
                text = raw.decode(encoding)
        except (ValueError, UnicodeError) as exc:
            raise DatasetIntelligenceError(
                "encoding_invalid", "tabular payload cannot be decoded"
            ) from exc
        if format == "csv":
            return [dict(row) for row in csv.DictReader(io.StringIO(text))]
        if format in {"json", "tabular-api"}:
            value = json.loads(text)
            rows = (
                value.get("data", value.get("items", []))
                if isinstance(value, dict)
                else value
            )
            if not isinstance(rows, list):
                raise DatasetIntelligenceError(
                    "format_invalid", "JSON tabular payload must contain a row list"
                )
            return [dict(row) for row in rows]
        if format == "jsonl":
            return [
                dict(json.loads(line)) for line in text.splitlines() if line.strip()
            ]
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq

            return pq.read_table(pa.BufferReader(raw)).to_pylist()
        except Exception as exc:
            raise DatasetIntelligenceError(
                "format_invalid", "Parquet payload is invalid"
            ) from exc

    @staticmethod
    def _value(raw: Any, kind: str) -> Any:
        if raw is None or raw == "":
            return None
        if kind == "string":
            return str(raw)
        if kind == "integer":
            return int(raw)
        if kind == "number":
            return float(raw)
        if kind == "boolean":
            if isinstance(raw, bool):
                return raw
            token = str(raw).casefold()
            if token not in {"true", "false", "1", "0"}:
                raise ValueError("invalid boolean")
            return token in {"true", "1"}
        if kind == "date":
            return date.fromisoformat(str(raw)).isoformat()
        if kind == "json":
            return raw if isinstance(raw, (dict, list)) else json.loads(str(raw))
        raise ValueError(f"unsupported type {kind}")

    def validate_batch(self, namespace, release_id, table_id, rows, *, scopes, checks=None):
        """Preflight a batch with Pandera; validation does not publish or coerce rows."""
        _require(scopes, INGEST_SCOPE)
        release, table = self._table(namespace, release_id, table_id)
        from src.integrations.validation import validate_rows
        checks = checks or {}
        extra = checks.get("columns", {})
        declared = {column["name"] for column in table["columns"]}
        if set(extra) - declared or any(
            set(options) - {"minimum", "maximum", "allowed"}
            for options in extra.values()
        ):
            raise ValueError("Checks may constrain declared columns, not override their schema")
        columns = [{**column, **extra.get(column["name"], {})} for column in table["columns"]]
        return validate_rows(rows, columns, unique=table.get("primary_key"),
                             comparisons=checks.get("comparisons", ()))

    def ingest(
        self,
        namespace: str,
        release_id: str,
        table_id: str,
        format: str,
        content: str,
        partition_key: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        encoding: str = "utf-8",
        row_limit: int = 10_000,
        inference_limit: int = 1_000,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require(scopes, INGEST_SCOPE)
        release, table = self._table(namespace, release_id, table_id)
        input_hash = _digest(
            [release_id, table_id, format, content, partition_key, encoding, row_limit]
        )
        receipt_id = "tabular-receipt:" + input_hash[:24]
        existing = self.conn.execute(
            "SELECT receipt_json FROM dataset_ingestion_receipts WHERE namespace=? AND receipt_id=?",
            [namespace, receipt_id],
        ).fetchone()
        if existing:
            return {**_load(existing[0], {}), "idempotent": True}
        if cancel_requested:
            result = {
                "contract": RECEIPT_CONTRACT,
                "receipt_id": receipt_id,
                "namespace": namespace,
                "release_id": release_id,
                "table_id": table_id,
                "partition_id": None,
                "format": format,
                "status": "cancelled",
                "counts": {"read": 0, "inserted": 0, "quarantined": 0},
                "input_hash": input_hash,
                "output_hash": _digest([]),
                "truncated": False,
            }
            return result
        try:
            rows = self._parse(format, content, encoding)
        except (json.JSONDecodeError, csv.Error) as exc:
            raise DatasetIntelligenceError(
                "format_invalid", "tabular payload is malformed"
            ) from exc
        limit = _bounded(row_limit, 10_000)
        inference = _bounded(inference_limit, 1_000)
        if (
            rows[:inference]
            and len({key for row in rows[:inference] for key in row}) > 100
        ):
            raise DatasetIntelligenceError(
                "schema_too_wide", "schema inference is limited to 100 columns"
            )
        columns = {item["name"]: item for item in table["columns"]}
        unknown = sorted(
            {key for row in rows[:inference] for key in row if key not in columns}
        )
        now = self.now()
        partition_json = _canonical(dict(partition_key))
        partition_id = (
            "dataset-partition:"
            + _digest([namespace, release_id, table_id, partition_key])[:24]
        )
        normalized, quarantine = [], []
        for index, row in enumerate(rows[:limit]):
            try:
                if unknown:
                    raise ValueError("schema drift: " + ",".join(unknown))
                values, nulls = {}, {}
                for name, column in columns.items():
                    raw = row.get(name)
                    semantic = None
                    if isinstance(raw, dict) and "null_semantic" in raw:
                        semantic = raw.get("null_semantic")
                        raw = raw.get("value")
                    value = self._value(raw, column["type"])
                    if value is None:
                        semantic = semantic or "missing"
                        if semantic not in NULL_SEMANTICS:
                            raise ValueError("invalid null semantic")
                        if not column["nullable"]:
                            raise ValueError(f"column {name} is not nullable")
                        nulls[name] = semantic
                    values[name] = value
                row_key = {
                    name: values.get(name) for name in table.get("primary_key", [])
                }
                row_hash = _digest([row_key, values, nulls])
                normalized.append((index, row_key, values, nulls, row_hash))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                quarantine.append(
                    (
                        index,
                        _digest(row),
                        {"code": "row_invalid", "message": str(exc)[:200]},
                        {key: row[key] for key in list(row)[:10]},
                    )
                )
        output_hash = _digest([item[4] for item in normalized])
        counts = {
            "read": min(len(rows), limit),
            "inserted": len(normalized),
            "quarantined": len(quarantine),
        }
        result = {
            "contract": RECEIPT_CONTRACT,
            "receipt_id": receipt_id,
            "namespace": namespace,
            "release_id": release_id,
            "table_id": table_id,
            "partition_id": partition_id,
            "format": format,
            "status": "completed" if not quarantine else "partial",
            "counts": counts,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "truncated": len(rows) > limit,
            "schema_drift": unknown,
        }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO dataset_partitions VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    partition_id,
                    namespace,
                    release_id,
                    table_id,
                    partition_json,
                    len(normalized),
                    output_hash,
                    result["status"],
                    now,
                ],
            )
            for index, row_key, values, nulls, row_hash in normalized:
                row_id = "dataset-row:" + _digest([partition_id, index, row_hash])[:24]
                self.conn.execute(
                    "INSERT INTO dataset_rows_v2 VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        row_id,
                        namespace,
                        partition_id,
                        index,
                        _canonical(row_key),
                        _canonical(values),
                        _canonical(nulls),
                        _canonical(
                            {"release_id": release_id, "source": release["provenance"]}
                        ),
                        row_hash,
                        now,
                    ],
                )
            for index, row_hash, error, preview in quarantine:
                quarantine_id = (
                    "dataset-quarantine:" + _digest([receipt_id, index, row_hash])[:24]
                )
                self.conn.execute(
                    "INSERT INTO dataset_quarantine_v2 VALUES (?,?,?,?,?,?,?,?)",
                    [
                        quarantine_id,
                        namespace,
                        receipt_id,
                        index,
                        row_hash,
                        _canonical(error),
                        _canonical(preview),
                        now,
                    ],
                )
            self.conn.execute(
                "INSERT INTO dataset_ingestion_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    receipt_id,
                    namespace,
                    release_id,
                    table_id,
                    partition_id,
                    format,
                    input_hash,
                    result["status"],
                    _canonical(result),
                    principal_id,
                    now,
                ],
            )
            self._audit(namespace, "ingest", receipt_id, principal_id, counts, now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def replay_ingestion(
        self, namespace: str, receipt_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT receipt_json FROM dataset_ingestion_receipts WHERE namespace=? AND receipt_id=?",
            [namespace, receipt_id],
        ).fetchone()
        if not row:
            raise DatasetIntelligenceError(
                "receipt_not_found", "ingestion receipt does not exist"
            )
        receipt = _load(row[0], {})
        hashes = [
            item[0]
            for item in self.conn.execute(
                "SELECT row_hash FROM dataset_rows_v2 WHERE namespace=? AND partition_id=? ORDER BY row_index",
                [namespace, receipt["partition_id"]],
            ).fetchall()
        ]
        replayed = _digest(hashes)
        return {
            "receipt_id": receipt_id,
            "stored_hash": receipt["output_hash"],
            "replayed_hash": replayed,
            "deterministic": replayed == receipt["output_hash"],
        }

    def slice(
        self,
        namespace: str,
        release_id: str,
        table_id: str,
        *,
        scopes: set[str],
        partition_key: Mapping[str, Any] | None = None,
        offset: int = 0,
        limit: int = 100,
        columns: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        self._table(namespace, release_id, table_id)
        size = _bounded(limit, 1_000)
        partition_filter = (
            None if partition_key is None else _canonical(dict(partition_key))
        )
        rows = self.conn.execute(
            "SELECT r.row_id,r.row_index,r.row_key_json,r.values_json,r.null_semantics_json,r.provenance_json FROM dataset_rows_v2 r JOIN dataset_partitions p ON p.partition_id=r.partition_id WHERE r.namespace=? AND p.release_id=? AND p.table_id=? AND (? IS NULL OR p.partition_key_json=?) ORDER BY p.partition_key_json,r.row_index,r.row_id LIMIT ? OFFSET ?",
            [
                namespace,
                release_id,
                table_id,
                partition_filter,
                partition_filter,
                size + 1,
                max(0, int(offset)),
            ],
        ).fetchall()
        selected = set(columns or [])
        items = []
        for row in rows[:size]:
            values = _load(row[3], {})
            nulls = _load(row[4], {})
            if selected:
                values = {
                    key: value for key, value in values.items() if key in selected
                }
                nulls = {key: value for key, value in nulls.items() if key in selected}
            items.append(
                {
                    "row_id": row[0],
                    "row_index": int(row[1]),
                    "row_key": _load(row[2], {}),
                    "values": values,
                    "null_semantics": nulls,
                    "provenance": _load(row[5], {}),
                }
            )
        return {
            "contract": SLICE_CONTRACT,
            "namespace": namespace,
            "release_id": release_id,
            "table_id": table_id,
            "items": items,
            "next_offset": offset + size if len(rows) > size else None,
            "result_hash": _digest([item["row_id"] for item in items]),
        }

    def compare_releases(
        self,
        namespace: str,
        earlier_release_id: str,
        later_release_id: str,
        table_id: str,
        *,
        scopes: set[str],
        limit: int = 1_000,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        left = self.slice(
            namespace, earlier_release_id, table_id, scopes={READ_SCOPE}, limit=limit
        )["items"]
        right = self.slice(
            namespace, later_release_id, table_id, scopes={READ_SCOPE}, limit=limit
        )["items"]
        left_values = {_canonical(item["row_key"]): item for item in left}
        right_values = {_canonical(item["row_key"]): item for item in right}
        changes = []
        for key in sorted(set(left_values) | set(right_values)):
            before, after = left_values.get(key), right_values.get(key)
            if (
                before
                and after
                and before["values"] == after["values"]
                and before["null_semantics"] == after["null_semantics"]
            ):
                continue
            changes.append(
                {"row_key": _load(key, {}), "before": before, "after": after}
            )
        return {
            "namespace": namespace,
            "earlier_release_id": earlier_release_id,
            "later_release_id": later_release_id,
            "table_id": table_id,
            "changes": changes[: _bounded(limit, 1_000)],
            "comparison_hash": _digest(changes),
        }

    def suggest_joins(
        self,
        namespace: str,
        left_dataset_id: str,
        right_dataset_id: str,
        *,
        scopes: set[str],
        limit: int = 50,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        left = self.dataset(namespace, left_dataset_id, scopes={READ_SCOPE})
        right = self.dataset(namespace, right_dataset_id, scopes={READ_SCOPE})
        if not left or not right:
            raise DatasetIntelligenceError(
                "dataset_not_found", "both datasets must exist in the namespace"
            )
        suggestions = []
        for left_table in left["tables"]:
            for right_table in right["tables"]:
                for lcol in left_table["columns"]:
                    for rcol in right_table["columns"]:
                        role_match = lcol.get("semantic_role") and lcol.get(
                            "semantic_role"
                        ) == rcol.get("semantic_role")
                        code_match = lcol.get("code_list_id") and lcol.get(
                            "code_list_id"
                        ) == rcol.get("code_list_id")
                        name_match = lcol["name"].casefold() == rcol["name"].casefold()
                        if not (role_match or code_match or name_match):
                            continue
                        warnings = []
                        if (
                            lcol.get("unit")
                            and rcol.get("unit")
                            and lcol["unit"] != rcol["unit"]
                        ):
                            warnings.append("unit-mismatch")
                        if lcol.get("semantic_role") == "time" and left_table.get(
                            "frequency"
                        ) != right_table.get("frequency"):
                            warnings.append("temporal-mismatch")
                        suggestions.append(
                            {
                                "left_table_id": left_table["table_id"],
                                "right_table_id": right_table["table_id"],
                                "left_column_id": lcol["column_id"],
                                "right_column_id": rcol["column_id"],
                                "left_column": lcol["name"],
                                "right_column": rcol["name"],
                                "basis": "code-list"
                                if code_match
                                else "semantic-role"
                                if role_match
                                else "name",
                                "warnings": warnings,
                            }
                        )
        suggestions.sort(
            key=lambda item: (
                len(item["warnings"]),
                item["left_table_id"],
                item["right_table_id"],
                item["left_column_id"],
            )
        )
        return {
            "items": suggestions[: _bounded(limit, 500)],
            "result_hash": _digest(suggestions),
        }

    def preview_join(
        self,
        namespace: str,
        left_release_id: str,
        right_release_id: str,
        left_table_id: str,
        right_table_id: str,
        keys: Sequence[Mapping[str, str]],
        *,
        scopes: set[str],
        limit: int = 100,
    ) -> dict[str, Any]:
        _require(scopes, CALCULATE_SCOPE)
        _, left_table = self._table(namespace, left_release_id, left_table_id)
        _, right_table = self._table(namespace, right_release_id, right_table_id)
        left_rows = self.slice(
            namespace, left_release_id, left_table_id, scopes={READ_SCOPE}, limit=1_000
        )["items"]
        right_rows = self.slice(
            namespace,
            right_release_id,
            right_table_id,
            scopes={READ_SCOPE},
            limit=1_000,
        )["items"]
        if not keys:
            raise DatasetIntelligenceError(
                "invalid_join", "at least one join key is required"
            )
        left_names = [item["left"] for item in keys]
        right_names = [item["right"] for item in keys]
        left_schema = {item["name"]: item for item in left_table["columns"]}
        right_schema = {item["name"]: item for item in right_table["columns"]}
        if not set(left_names) <= set(left_schema) or not set(right_names) <= set(
            right_schema
        ):
            raise DatasetIntelligenceError(
                "invalid_join", "join key is not in the pinned schema"
            )
        warnings = []
        for left_name, right_name in zip(left_names, right_names):
            left_column, right_column = left_schema[left_name], right_schema[right_name]
            if (
                left_column.get("unit")
                and right_column.get("unit")
                and left_column["unit"] != right_column["unit"]
            ):
                warnings.append("unit-mismatch")
            if left_column.get("semantic_role") == "time" and left_table.get(
                "frequency"
            ) != right_table.get("frequency"):
                warnings.append("temporal-mismatch")

        def key(row, names):
            return tuple(row["values"].get(name) for name in names)

        left_counts = Counter(key(row, left_names) for row in left_rows)
        right_counts = Counter(key(row, right_names) for row in right_rows)
        if any(value > 1 for value in left_counts.values()) and any(
            value > 1 for value in right_counts.values()
        ):
            cardinality = "many-to-many"
            warnings.append("many-to-many")
        elif any(value > 1 for value in left_counts.values()):
            cardinality = "many-to-one"
        elif any(value > 1 for value in right_counts.values()):
            cardinality = "one-to-many"
        else:
            cardinality = "one-to-one"
        right_index: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in right_rows:
            right_index.setdefault(key(row, right_names), []).append(row)
        joined = []
        for left_row in left_rows:
            for right_row in right_index.get(key(left_row, left_names), []):
                joined.append(
                    {
                        "left_row_id": left_row["row_id"],
                        "right_row_id": right_row["row_id"],
                        "left": left_row["values"],
                        "right": right_row["values"],
                    }
                )
        canonical = {
            "namespace": namespace,
            "left_release_id": left_release_id,
            "right_release_id": right_release_id,
            "left_table_id": left_table_id,
            "right_table_id": right_table_id,
            "keys": [dict(item) for item in keys],
            "cardinality": cardinality,
            "warnings": sorted(set(warnings)),
            "matches": len(joined),
        }
        preview_hash = _digest(canonical)
        return {
            "contract": JOIN_CONTRACT,
            **canonical,
            "items": joined[: _bounded(limit, 1_000)],
            "preview_hash": preview_hash,
            "accepted": False,
        }

    def accept_join(
        self,
        namespace: str,
        preview: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        expected = self.preview_join(
            namespace,
            preview["left_release_id"],
            preview["right_release_id"],
            preview["left_table_id"],
            preview["right_table_id"],
            preview["keys"],
            scopes={CALCULATE_SCOPE},
            limit=len(preview.get("items", [])) or 100,
        )
        if expected["preview_hash"] != preview.get("preview_hash"):
            raise DatasetIntelligenceError(
                "preview_drift", "join preview no longer matches pinned inputs"
            )
        transformation_id = "dataset-transformation:" + expected["preview_hash"][:24]
        derived_table_id = (
            "dataset-derived-table:" + _digest([namespace, transformation_id])[:24]
        )
        lineage = {
            "inputs": [expected["left_release_id"], expected["right_release_id"]],
            "tables": [expected["left_table_id"], expected["right_table_id"]],
            "operation": "join",
            "keys": expected["keys"],
        }
        existing = self.conn.execute(
            "SELECT created_at_ms FROM dataset_join_transformations WHERE namespace=? AND transformation_id=?",
            [namespace, transformation_id],
        ).fetchone()
        now = self.now()
        if not existing:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute(
                    "INSERT INTO dataset_join_transformations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        transformation_id,
                        namespace,
                        expected["left_release_id"],
                        expected["right_release_id"],
                        expected["left_table_id"],
                        expected["right_table_id"],
                        _canonical(
                            {
                                key: value
                                for key, value in expected.items()
                                if key != "items"
                            }
                        ),
                        expected["preview_hash"],
                        _canonical(lineage),
                        derived_table_id,
                        principal_id,
                        now,
                    ],
                )
                self._audit(
                    namespace, "accept-join", transformation_id, principal_id, {}, now
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return {
            **expected,
            "transformation_id": transformation_id,
            "derived_table_id": derived_table_id,
            "lineage": lineage,
            "accepted": True,
            "created_at_ms": int(existing[0]) if existing else now,
            "idempotent": bool(existing),
        }

    def lineage(
        self, namespace: str, transformation_id: str, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT preview_hash,lineage_json,derived_table_id,principal_id,created_at_ms FROM dataset_join_transformations WHERE namespace=? AND transformation_id=?",
            [namespace, transformation_id],
        ).fetchone()
        if not row:
            return None
        return {
            "transformation_id": transformation_id,
            "namespace": namespace,
            "preview_hash": row[0],
            "lineage": _load(row[1], {}),
            "derived_table_id": row[2],
            "principal_id": row[3],
            "created_at_ms": int(row[4]),
        }
