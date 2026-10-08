"""Bulk-release runner (FA01).

One run processes one release of one adapter::

    state_dir/<adapter>/<release_id>/
        manifest.json          provider, release, files (size, ETag, Last-Modified, checksum), licence
        files/<name>.json      completion marker per file (bytes, digests, records)
        quarantine/<name>      files whose checksum did not match (kept for inspection)
        subset/<table>.jsonl.gz  the filtered records that are kept
        receipts/<ts>.json     one receipt per run (status, counts, budgets, errors)

Runs are resumable: completed files are skipped, a half-downloaded file resumes
from its ``.part`` file. With ``incremental="file"`` adapters, files whose
fingerprint (URL, ETag, Last-Modified, size, checksum) matches a completed file
of an earlier release are skipped too. Budgets (bytes, files, seconds) stop a run
cleanly with status ``budget_exhausted``; an exhausted free-tier quota stops it
with status ``deferred`` and a retry time. Raw files are deleted after
processing unless ``keep_raw`` is set; the scratch directory is not meant to be
backed up, the state directory is.
"""
from __future__ import annotations

import hashlib
import io
import json
import shutil
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from services.ingest.common.document_model import Document
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile
from src.ingestion.bulk.http import BulkHttp
from src.ingestion.bulk.sinks import Sink, SubsetSink
from src.ingestion.connectors.base import PermanentFetchError
from src.ingestion.quota import QuotaDeferred

CONTRACT = "noesis-bulk-release-run-v1"
MANIFEST_CONTRACT = "noesis-bulk-release-manifest-v1"
WRITE_BATCH = 2_000


@dataclass
class Budgets:
    max_bytes: int = 5_000_000_000
    max_files: int = 1_000
    max_seconds: int = 6 * 3600


class ChecksumMismatch(PermanentFetchError):
    pass


class _CountingRaw(io.RawIOBase):
    """Counts streamed bytes and enforces the byte budget for ``stream`` files."""

    def __init__(self, response: Any, cap: int):
        self.response, self.cap, self.count = response, cap, 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        data = self.response.read(len(buffer))
        n = len(data)
        buffer[:n] = data
        self.count += n
        if self.count > self.cap:
            raise PermanentFetchError(f"stream exceeds the byte budget ({self.cap})")
        return n

    def close(self) -> None:
        try:
            self.response.close()
        finally:
            super().close()


_HEX_ALGOS = {32: "md5", 40: "sha1", 64: "sha256"}


def _with_published_checksum(f: ReleaseFile, text: str) -> ReleaseFile:
    """Attach a checksum published as a small text file (e.g. ``MD5(file)= <hex>``)."""
    import dataclasses
    import re as _re
    match = _re.search(r"\b([0-9a-fA-F]{64}|[0-9a-fA-F]{40}|[0-9a-fA-F]{32})\b", text or "")
    if not match:
        raise ChecksumMismatch(f"no checksum found at {f.metadata.get('checksum_url')}")
    digest = match.group(1).lower()
    return dataclasses.replace(f, checksum=(_HEX_ALGOS[len(digest)], digest))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:200]


class BulkRunner:
    def __init__(self, adapter: BulkAdapter, *, state_dir: Path, work_dir: Path,
                 sinks: Sequence[Sink] = (), http: Optional[BulkHttp] = None,
                 budgets: Optional[Budgets] = None, keep_raw: bool = False,
                 clock: Callable[[], float] = time.monotonic):
        self.adapter = adapter
        self.state_root = Path(state_dir) / _safe_name(adapter.name)
        self.work_root = Path(work_dir) / _safe_name(adapter.name)
        self.extra_sinks = list(sinks)
        self.http = http or BulkHttp(adapter.allowed_hosts)
        self.budgets = budgets or Budgets()
        self.keep_raw = keep_raw
        self.clock = clock

    # -- public ------------------------------------------------------------------ #
    def run(self, params: Optional[Mapping[str, Any]] = None, *, dry_run: bool = False,
            full: bool = False, release: Optional[Release] = None) -> Dict[str, Any]:
        params = self.adapter.validate_params(params or {})
        started, started_iso = self.clock(), _now_iso()
        receipt: Dict[str, Any] = {"contract": CONTRACT, "adapter": self.adapter.name, "params": params,
                                   "started_at": started_iso, "dry_run": dry_run, "full": full,
                                   "budgets": asdict(self.budgets), "files": [], "errors": []}
        try:
            release = release or self.adapter.list_release(self.http, params)
        except QuotaDeferred as deferred:
            return {**receipt, **deferred.as_dict(), "finished_at": _now_iso()}
        except Exception as exc:  # noqa: BLE001 - a failed listing is a failed run, reported in the receipt
            return {**receipt, "status": "failed", "finished_at": _now_iso(),
                    "errors": [{"stage": "list_release", "error": f"{type(exc).__name__}: {str(exc)[:300]}"}]}
        release_dir = self.state_root / _safe_name(release.release_id)
        manifest = self._manifest(release, release_dir)
        receipt.update({"release_id": release.release_id, "manifest_id": manifest["manifest_id"],
                        "release_dir": str(release_dir)})
        previous = {} if full else self._previous_fingerprints(release.release_id)

        if dry_run:
            for f in release.files:
                skip = "unchanged" if self._unchanged(f, previous) else None
                receipt["files"].append({"name": f.name, "size": f.size, "mode": f.mode, "action": skip or "process"})
            receipt.update(status="dry-run", finished_at=_now_iso())
            return receipt

        sinks: List[Sink] = [SubsetSink(release_dir / "subset"), *self.extra_sinks]
        for sink in sinks:
            sink.begin(self.adapter, release, manifest)
        status, used_bytes, used_files = "complete", 0, 0
        for f in release.files:
            marker = release_dir / "files" / (_safe_name(f.name) + ".json")
            if marker.exists():
                receipt["files"].append({"name": f.name, "action": "already-done"})
                continue
            if self._unchanged(f, previous):
                receipt["files"].append({"name": f.name, "action": "unchanged"})
                continue
            if used_files >= self.budgets.max_files or self.clock() - started >= self.budgets.max_seconds:
                status = "budget_exhausted"
                break
            if f.size and used_bytes + f.size > self.budgets.max_bytes:
                status = "budget_exhausted"
                break
            try:
                result = self._process_file(f, release_dir, params, sinks,
                                            byte_cap=self.budgets.max_bytes - used_bytes)
            except QuotaDeferred as deferred:
                receipt.update(deferred.as_dict())
                status = "deferred"
                break
            except ChecksumMismatch as exc:
                receipt["files"].append({"name": f.name, "action": "quarantined", "reason": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 - one bad file must not hide the others
                receipt["errors"].append({"file": f.name, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
                continue
            used_bytes += result["bytes"]
            used_files += 1
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps(result, indent=1))
            receipt["files"].append({"name": f.name, "action": "processed", "bytes": result["bytes"],
                                     "records": result["records"]})
        if status == "complete" and receipt["errors"]:
            status = "partial"
        outputs = {}
        for sink in sinks:
            outputs.update(sink.finish() or {})
        receipt.update(status=status, outputs=outputs, bytes=used_bytes, files_processed=used_files,
                       finished_at=_now_iso())
        receipts = release_dir / "receipts"
        receipts.mkdir(parents=True, exist_ok=True)
        (receipts / (started_iso.replace(":", "") + ".json")).write_text(json.dumps(receipt, indent=1, default=str))
        return receipt

    # -- internals ------------------------------------------------------------------ #
    def _manifest(self, release: Release, release_dir: Path) -> Dict[str, Any]:
        path = release_dir / "manifest.json"
        if path.exists():
            return json.loads(path.read_text())
        body = {"contract": MANIFEST_CONTRACT, "adapter": self.adapter.name, "provider": release.provider,
                "publisher": self.adapter.publisher, "release_id": release.release_id,
                "published_at": release.published_at, "licence": dict(release.licence),
                "metadata": dict(release.metadata), "created_at": _now_iso(),
                "files": [{**asdict(f), "checksum": list(f.checksum) if f.checksum else None} for f in release.files]}
        body["manifest_id"] = "bulk-manifest:" + hashlib.sha256(
            json.dumps({k: v for k, v in body.items() if k != "created_at"}, sort_keys=True,
                       default=str).encode()).hexdigest()[:24]
        release_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body, indent=1, default=str))
        return body

    def _previous_fingerprints(self, current_release: str) -> Dict[str, Any]:
        if self.adapter.incremental != "file" or not self.state_root.exists():
            return {}
        seen: Dict[str, Any] = {}
        for marker in sorted(self.state_root.glob("*/files/*.json")):
            if marker.parent.parent.name == _safe_name(current_release):
                continue
            data = json.loads(marker.read_text())
            seen[data["name"]] = tuple(data["fingerprint"])
        return seen

    @staticmethod
    def _unchanged(f: ReleaseFile, previous: Mapping[str, Any]) -> bool:
        fp = previous.get(f.name)
        return fp is not None and fp == tuple(f.fingerprint()) and any(x for x in fp[1:])

    def _process_file(self, f: ReleaseFile, release_dir: Path, params: Mapping[str, Any],
                      sinks: Sequence[Sink], byte_cap: int) -> Dict[str, Any]:
        digests: Dict[str, str] = {}
        nbytes = 0
        local: Optional[Path] = None
        if f.mode == "download":
            if f.checksum is None and f.metadata.get("checksum_url"):
                f = _with_published_checksum(f, self.http.get_text(f.metadata["checksum_url"], limit=4096))
            local = self.work_root / _safe_name(release_dir.name) / _safe_name(f.name)
            nbytes, digests = self.http.download(f.url, local, max_bytes=max(1, byte_cap))
            if f.checksum:
                algo, expected = f.checksum
                actual = digests.get(algo.lower())
                if actual is not None and actual.lower() != expected.lower():
                    quarantine = release_dir / "quarantine"
                    quarantine.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(local), quarantine / _safe_name(f.name))
                    raise ChecksumMismatch(f"{algo} mismatch: expected {expected}, got {actual}")
            source = FileSource(file=f, path=local, http=self.http)
        elif f.mode == "stream":
            counters: List[_CountingRaw] = []

            def open_counted() -> Any:
                raw = _CountingRaw(self.http.open_stream(f.url), max(1, byte_cap))
                counters.append(raw)
                return io.BufferedReader(raw, buffer_size=1024 * 1024)

            source = FileSource(file=f, open_stream=open_counted, http=self.http)
        else:
            source = FileSource(file=f, http=self.http)

        counts: Dict[str, int] = {}
        batches: Dict[str, List[Any]] = {}

        def flush(table: str) -> None:
            for sink in sinks:
                sink.write(f, table, batches[table])
            batches[table] = []

        try:
            for record in self.adapter.process(source, params):
                if isinstance(record, Document):
                    table, item = "documents", record
                else:
                    table, item = record
                batches.setdefault(table, []).append(item)
                counts[table] = counts.get(table, 0) + 1
                if len(batches[table]) >= WRITE_BATCH:
                    flush(table)
            for table in list(batches):
                if batches[table]:
                    flush(table)
            for sink in sinks:
                sink.end_file(f)
        finally:
            if local is not None and local.exists() and not self.keep_raw:
                local.unlink()
        if f.mode == "stream":
            nbytes = sum(c.count for c in counters)
        return {"name": f.name, "fingerprint": list(f.fingerprint()), "bytes": nbytes,
                "digests": digests, "records": counts, "completed_at": _now_iso()}
