"""Archive byte publication and verified transactional checkpoint recovery."""

import hashlib
import json
import os
from pathlib import Path
import tempfile

from src.kb.knowledge_retention import ARCHIVE_CONTRACT, KnowledgeRetentionError, _canon, _hash

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024


class FilesystemArchiveBackend:
    @staticmethod
    def _path(storage):
        if storage.get("driver") != "filesystem" or not isinstance(storage.get("uri"), str) or not storage["uri"]:
            raise KnowledgeRetentionError("unsupported_backend", "configure a filesystem archive URI")
        return Path(storage["uri"]).expanduser()

    def read(self, storage):
        with self._path(storage).open("rb") as stream:
            data = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(data) > MAX_ARCHIVE_BYTES:
            raise KnowledgeRetentionError("archive_too_large", "archive exceeds the byte limit")
        return data

    def write(self, storage, data):
        path = self._path(storage)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if self.read(storage) != data:
                raise KnowledgeRetentionError("checksum_mismatch", "archive path already contains different bytes")
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".noesis-archive-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)  # publish without overwriting another archive
            except FileExistsError:
                if self.read(storage) != data:
                    raise KnowledgeRetentionError("checksum_mismatch", "archive publication raced with different bytes")
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def _save_archive(store, manifest, status):
    store.conn.execute("INSERT OR REPLACE INTO retention_archives VALUES (?,?,?,?,?,?,?,?,?)",
                       [manifest["archive_id"], manifest["namespace"], manifest["checkpoint_id"],
                        _canon(manifest["storage"]), _canon(manifest["encryption"]), manifest["content_hash"],
                        status, _canon(manifest), store.now()])


def archive_checkpoint(store, namespace, checkpoint_id, storage, *, encryption, cancel_requested,
                       storage_available, partial, principal_id):
    if partial:
        raise KnowledgeRetentionError("unsupported_simulation", "partial outcomes are determined by byte I/O")
    if encryption:
        raise KnowledgeRetentionError("unsupported_encryption", "an encryption declaration cannot substitute for encrypted bytes")
    row = store.conn.execute("""SELECT generation_start,generation_end,schema_version,content_hash,
        records_json,tombstones_json,status FROM retention_checkpoints WHERE namespace=? AND checkpoint_id=?""",
        [namespace, checkpoint_id]).fetchone()
    if not row or row[6] != "complete":
        raise KnowledgeRetentionError("checkpoint_unavailable", "a complete checkpoint is required")
    content = {"generation_start": row[0], "generation_end": row[1], "schema_version": row[2],
               "records": json.loads(row[4]), "tombstones": json.loads(row[5])}
    if _hash(content) != row[3]:
        raise KnowledgeRetentionError("checksum_mismatch", "checkpoint bytes do not match its receipt")
    manifest = {"contract": ARCHIVE_CONTRACT, "archive_id": "archive:" + _hash([namespace, checkpoint_id, storage])[:24],
                "namespace": namespace, "checkpoint_id": checkpoint_id, "storage": dict(storage),
                "encryption": {}, "content_hash": row[3], "identity_verified": True, "archive_format": 1}
    data = _canon({"manifest": manifest, "checkpoint": content}).encode()
    if len(data) > MAX_ARCHIVE_BYTES:
        raise KnowledgeRetentionError("archive_too_large", "split the checkpoint before archiving")
    manifest["archive_hash"] = hashlib.sha256(data).hexdigest()
    manifest["byte_count"] = len(data)
    backend = store.archive_backend or FilesystemArchiveBackend()
    status = "cancelled" if cancel_requested else "unavailable" if not storage_available else "archived"
    if status == "archived":
        try:
            backend.write(storage, data)
            persisted = backend.read(storage)
            if persisted != data:
                raise KnowledgeRetentionError("checksum_mismatch", "persisted archive verification failed")
        except Exception as exc:
            status = "partial" if isinstance(exc, KnowledgeRetentionError) and exc.code == "checksum_mismatch" else "unavailable"
            _save_archive(store, manifest, status)
            store._audit(namespace, "archive", manifest["archive_id"], principal_id, {"status": status})
            raise KnowledgeRetentionError(getattr(exc, "code", "archive_unavailable"), str(exc)) from exc
    _save_archive(store, manifest, status)
    store._audit(namespace, "archive", manifest["archive_id"], principal_id, {"status": status})
    return {**manifest, "status": status}


def restore_archive(store, namespace, archive_id, *, principal_id, storage_available, manifest=None,
                    supported_schema_versions=("1", "2", "3")):
    row = store.conn.execute("SELECT manifest_json FROM retention_archives WHERE namespace=? AND archive_id=?", [namespace, archive_id]).fetchone()
    saved = json.loads(row[0]) if row else None
    manifest = dict(manifest or saved or {})
    if not manifest:
        raise KnowledgeRetentionError("archive_not_found", "supply the saved manifest when restoring a fresh database")
    if not storage_available:
        raise KnowledgeRetentionError("archive_unavailable", "archive retrieval disabled")
    if manifest.get("namespace") != namespace or manifest.get("archive_id") != archive_id or manifest.get("contract") != ARCHIVE_CONTRACT:
        raise KnowledgeRetentionError("archive_identity_mismatch", "archive namespace or identity differs")
    if not manifest.get("archive_hash"):
        raise KnowledgeRetentionError("archive_unverified", "legacy metadata-only archives must be republished from their checkpoint")
    if saved and saved.get("archive_hash") != manifest["archive_hash"]:
        raise KnowledgeRetentionError("archive_identity_mismatch", "manifest conflicts with the saved archive receipt")
    try:
        data = (store.archive_backend or FilesystemArchiveBackend()).read(manifest["storage"])
    except Exception as exc:
        raise KnowledgeRetentionError("archive_unavailable", str(exc)) from exc
    if len(data) > MAX_ARCHIVE_BYTES or hashlib.sha256(data).hexdigest() != manifest["archive_hash"]:
        raise KnowledgeRetentionError("checksum_mismatch", "archive byte checksum differs")
    try:
        package = json.loads(data)
        embedded, content = package["manifest"], package["checkpoint"]
        if set(content) != {"generation_start", "generation_end", "schema_version", "records", "tombstones"}:
            raise ValueError("unsupported checkpoint fields")
        if content["schema_version"] not in supported_schema_versions:
            raise ValueError("unsupported checkpoint schema version")
        if any(type(content[k]) is not int or content[k] < 0 for k in ("generation_start", "generation_end")) or content["generation_end"] < content["generation_start"]:
            raise ValueError("invalid generation range")
        if any(not isinstance(content[k], list) or len(content[k]) > 1000 for k in ("records", "tombstones")):
            raise ValueError("invalid or oversized checkpoint records")
        for key in ("contract", "archive_id", "namespace", "checkpoint_id", "content_hash", "archive_format", "encryption"):
            if embedded.get(key) != manifest.get(key):
                raise ValueError("embedded archive manifest differs")
        if embedded.get("archive_format") != 1 or embedded.get("encryption"):
            raise ValueError("unsupported archive encoding")
        digest = _hash(content)
        valid_ids = {"retention-checkpoint:" + _hash([namespace, digest])[:24],
                     "retention-checkpoint:" + digest[:24]}  # legacy checkpoint identities
        if digest != manifest["content_hash"] or manifest["checkpoint_id"] not in valid_ids:
            raise ValueError("checkpoint identity differs")
        if "archive:" + _hash([namespace, manifest["checkpoint_id"], embedded["storage"]])[:24] != archive_id:
            raise ValueError("archive identity differs")
    except (ValueError, TypeError, KeyError) as exc:
        raise KnowledgeRetentionError("invalid_archive", str(exc)) from exc
    store.conn.execute("BEGIN TRANSACTION")
    try:
        existing = store.conn.execute("SELECT namespace,content_hash FROM retention_checkpoints WHERE checkpoint_id=?", [manifest["checkpoint_id"]]).fetchone()
        if existing and existing != (namespace, digest):
            raise KnowledgeRetentionError("checkpoint_conflict", "checkpoint identity already belongs to different state")
        store.conn.execute("INSERT OR REPLACE INTO retention_checkpoints VALUES (?,?,?,?,?,?,?,?,?,?)",
            [manifest["checkpoint_id"], namespace, content["generation_start"], content["generation_end"],
             content["schema_version"], digest, _canon(content["records"]), _canon(content["tombstones"]), "complete", store.now()])
        _save_archive(store, manifest, "restored")
        store._audit(namespace, "restore", archive_id, principal_id)
        store.conn.execute("COMMIT")
    except Exception:
        store.conn.execute("ROLLBACK")
        raise
    return {**manifest, "status": "restored", "restored_atomically": True,
            "record_count": len(content["records"]), "tombstone_count": len(content["tombstones"])}
