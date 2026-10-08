"""Where bulk records go (FA01).

* :class:`SubsetSink` — always on: the filtered subset as gzipped JSONL per table
  (or ``documents``) inside the release directory. This is what is kept and
  backed up; the raw dump is not.
* :class:`DatasetSink` — rows adapters: registers the dataset and the release in
  the dataset store and ingests rows in chunks of at most 10,000 (the store's
  per-call bound), partitioned by file and chunk.
* :class:`DocumentSink` — documents adapters: commits papers through the
  gateway's ingest workflow in batches, so DOI-based de-duplication applies.
"""
from __future__ import annotations

import dataclasses
import gzip
import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from services.ingest.common.document_model import Document
from src.ingestion.bulk.base import BulkAdapter, Release, ReleaseFile

DATASET_CHUNK = 10_000


class Sink:
    def begin(self, adapter: BulkAdapter, release: Release, manifest: Mapping[str, Any]) -> None: ...
    def write(self, file: ReleaseFile, table: str, records: List[Any]) -> None: ...
    def end_file(self, file: ReleaseFile) -> None: ...
    def finish(self) -> Dict[str, Any]:
        return {}


class SubsetSink(Sink):
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.counts: Dict[str, int] = {}

    def begin(self, adapter, release, manifest):
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(self, file, table, records):
        path = self.directory / f"{table}.jsonl.gz"
        with gzip.open(path, "at", encoding="utf-8") as out:
            for record in records:
                if isinstance(record, Document):
                    record = dataclasses.asdict(record)
                out.write(json.dumps({"_file": file.name, **record}, ensure_ascii=False, default=str) + "\n")
        self.counts[table] = self.counts.get(table, 0) + len(records)

    def finish(self):
        return {"subset_dir": str(self.directory), "records": dict(self.counts)}


class DatasetSink(Sink):
    """Writes rows into ``DatasetIntelligenceStore``; ``connect()`` returns a DuckDB connection."""

    def __init__(self, namespace: str, connect: Callable[[], Any], principal_id: str = "bulk-runner"):
        self.namespace, self.connect, self.principal = namespace, connect, principal_id
        self.release_id: Optional[str] = None
        self.table_ids: Dict[str, str] = {}
        self.buffers: Dict[str, List[Dict[str, Any]]] = {}
        self.chunk_no: Dict[tuple, int] = {}
        self.receipts: List[str] = []

    def _store(self, conn):
        from src.kb.dataset_intelligence import DatasetIntelligenceStore
        return DatasetIntelligenceStore(conn)

    def begin(self, adapter, release, manifest):
        spec = adapter.dataset_spec(release)
        scopes = {"knowledge:dataset:write", "knowledge:dataset:read", "knowledge:dataset:ingest"}
        conn = self.connect()
        try:
            store = self._store(conn)
            dataset = store.register_dataset(self.namespace, spec["publisher_id"], spec["native_id"],
                                             spec["semantic_version"], spec["title"], spec["description"],
                                             spec["license"], spec["tables"], spec["code_lists"],
                                             spec["partitions"], principal_id=self.principal, scopes=scopes)
            self.table_ids = {t["identity"]: t["table_id"] for t in dataset["tables"]}
            published = _ms(release.published_at)
            registered = store.register_release(self.namespace, dataset["dataset_id"], release.release_id,
                                                release.release_id, retrieved_at_ms=int(time.time() * 1000),
                                                published_at_ms=published, principal_id=self.principal,
                                                scopes=scopes, provenance={"bulk_manifest_id": manifest["manifest_id"],
                                                                           "provider": release.provider})
            self.release_id = registered["release_id"]
        finally:
            conn.close()

    def write(self, file, table, records):
        buffer = self.buffers.setdefault(table, [])
        buffer.extend(records)
        while len(buffer) >= DATASET_CHUNK:
            self._flush(file, table, buffer[:DATASET_CHUNK])
            del buffer[:DATASET_CHUNK]

    def end_file(self, file):
        for table, buffer in self.buffers.items():
            if buffer:
                self._flush(file, table, buffer)
                buffer.clear()

    def _flush(self, file, table, rows):
        key = (file.name, table)
        self.chunk_no[key] = self.chunk_no.get(key, 0) + 1
        content = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        conn = self.connect()
        try:
            receipt = self._store(conn).ingest(
                self.namespace, self.release_id, self.table_ids[table], "jsonl", content,
                {"file": file.name, "chunk": self.chunk_no[key]}, principal_id=self.principal,
                scopes={"knowledge:dataset:ingest", "knowledge:dataset:read"}, row_limit=DATASET_CHUNK)
            self.receipts.append(receipt["receipt_id"])
        finally:
            conn.close()

    def finish(self):
        return {"dataset_release_id": self.release_id, "ingestion_receipts": len(self.receipts)}


class DocumentSink(Sink):
    """Commits documents via ``src.gateway.ingest_documents`` in batches."""

    def __init__(self, domain: str, batch: int = 500, ingest: Optional[Callable[..., Dict[str, Any]]] = None):
        self.domain, self.batch = domain, batch
        self._ingest = ingest
        self.pending: List[Document] = []
        self.runs: List[str] = []
        self.manifest_id = ""

    def begin(self, adapter, release, manifest):
        self.manifest_id = manifest["manifest_id"]

    def write(self, file, table, records):
        if table != "documents":
            return  # side tables (e.g. PubMed deletions) stay in the subset files
        self.pending.extend(records)
        if len(self.pending) >= self.batch:
            self._flush()

    def end_file(self, file):
        if self.pending:
            self._flush()

    def _flush(self):
        ingest = self._ingest
        if ingest is None:
            from src.gateway import ingest_documents
            from src.noesis_cli.config import load_config
            ingest = lambda docs, **kw: ingest_documents(load_config(), docs, **kw)  # noqa: E731
        result = ingest(self.pending, domain=self.domain, source_identity=self.manifest_id)
        self.runs.append(str((result.get("processing") or {}).get("workflow_run_id")))
        self.pending = []

    def finish(self):
        return {"domain": self.domain, "ingest_workflow_runs": self.runs}


def _ms(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None
