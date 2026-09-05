import hashlib
import json

import duckdb
import pytest

from src.kb.archive_io import FilesystemArchiveBackend
from src.kb.knowledge_retention import KnowledgeRetentionStore, KnowledgeRetentionError, EXECUTE_SCOPE, READ_SCOPE

AUTH = {"principal_id": "researcher", "scopes": {EXECUTE_SCOPE}}


def checkpoint(store):
    return store.checkpoint("research", 1, 2, [{"id": "a", "text": "Original source Ω"}],
                            schema_version="1", tombstones=["deleted:b"], **AUTH)


def test_restore_bytes_after_original_database_is_removed(tmp_path):
    path = tmp_path / "original.duckdb"
    conn = duckdb.connect(str(path))
    store = KnowledgeRetentionStore(conn)
    cp = checkpoint(store)
    storage = {"driver": "filesystem", "uri": str(tmp_path / "cold" / "archive.json")}
    archived = store.archive("research", cp["checkpoint_id"], storage, **AUTH)
    raw = (tmp_path / "cold" / "archive.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == archived["archive_hash"]
    assert store.archive("research", cp["checkpoint_id"], storage, **AUTH)["archive_hash"] == archived["archive_hash"]
    conn.close()
    path.unlink()
    restored_conn = duckdb.connect(str(tmp_path / "fresh.duckdb"))
    fresh = KnowledgeRetentionStore(restored_conn)
    restored = fresh.restore("research", archived["archive_id"], manifest=archived, **AUTH)
    assert restored["restored_atomically"] and restored["record_count"] == restored["tombstone_count"] == 1
    assert fresh.verify_checkpoint("research", cp["checkpoint_id"], scopes={READ_SCOPE})["verified"]
    records, tombstones = restored_conn.execute("SELECT records_json,tombstones_json FROM retention_checkpoints").fetchone()
    assert json.loads(records) == [{"id": "a", "text": "Original source Ω"}]
    assert json.loads(tombstones) == ["deleted:b"]
    restored_conn.close()


def test_interrupted_publication_never_marks_archive_success(tmp_path, monkeypatch):
    import src.kb.archive_io as module
    store = KnowledgeRetentionStore(duckdb.connect())
    cp = checkpoint(store)
    destination = tmp_path / "archive.json"
    def interrupted(_):
        raise OSError("interrupted fsync")
    monkeypatch.setattr(module.os, "fsync", interrupted)
    with pytest.raises(KnowledgeRetentionError, match="interrupted fsync"):
        store.archive("research", cp["checkpoint_id"], {"driver": "filesystem", "uri": str(destination)}, **AUTH)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []
    assert store.conn.execute("SELECT status FROM retention_archives").fetchone()[0] == "unavailable"


def test_backend_checksum_verification_and_corrupt_restore(tmp_path):
    class CorruptBackend:
        def write(self, storage, data):
            self.data = data
        def read(self, storage):
            return self.data[:-1]
    store = KnowledgeRetentionStore(duckdb.connect(), archive_backend=CorruptBackend())
    cp = checkpoint(store)
    storage = {"driver": "filesystem", "uri": str(tmp_path / "archive.json")}
    with pytest.raises(KnowledgeRetentionError, match="persisted archive"):
        store.archive("research", cp["checkpoint_id"], storage, **AUTH)
    assert store.conn.execute("SELECT status FROM retention_archives").fetchone()[0] == "partial"
    store.archive_backend = None
    archive = store.archive("research", cp["checkpoint_id"], storage, **AUTH)
    (tmp_path / "archive.json").write_bytes(b"corrupted")
    fresh = KnowledgeRetentionStore(duckdb.connect())
    with pytest.raises(KnowledgeRetentionError, match="checksum"):
        fresh.restore("research", archive["archive_id"], manifest=archive, **AUTH)
    assert fresh.conn.execute("SELECT count(*) FROM retention_checkpoints").fetchone()[0] == 0


def test_restore_schema_identity_and_transaction_failure(tmp_path, monkeypatch):
    source = KnowledgeRetentionStore(duckdb.connect())
    cp = checkpoint(source)
    manifest = source.archive("research", cp["checkpoint_id"], {"driver": "filesystem", "uri": str(tmp_path / "archive.json")}, **AUTH)
    target = KnowledgeRetentionStore(duckdb.connect())
    with pytest.raises(KnowledgeRetentionError, match="namespace"):
        target.restore("other", manifest["archive_id"], manifest=manifest, **AUTH)
    with pytest.raises(KnowledgeRetentionError, match="schema"):
        target.restore("research", manifest["archive_id"], manifest=manifest, supported_schema_versions=("future",), **AUTH)
    def failure(*args, **kwargs):
        raise RuntimeError("interrupted restore publication")
    monkeypatch.setattr(target, "_audit", failure)
    with pytest.raises(RuntimeError, match="interrupted restore"):
        target.restore("research", manifest["archive_id"], manifest=manifest, **AUTH)
    assert target.conn.execute("SELECT count(*) FROM retention_checkpoints").fetchone()[0] == 0
    assert target.conn.execute("SELECT count(*) FROM retention_archives").fetchone()[0] == 0


def test_identical_checkpoints_in_different_namespaces_keep_separate_identity():
    store = KnowledgeRetentionStore(duckdb.connect())
    first = checkpoint(store)
    second = store.checkpoint("other", 1, 2, [{"id": "a", "text": "Original source Ω"}],
                              schema_version="1", tombstones=["deleted:b"], **AUTH)
    assert first["content_hash"] == second["content_hash"]
    assert first["checkpoint_id"] != second["checkpoint_id"]
    assert store.verify_checkpoint("other", second["checkpoint_id"], scopes={READ_SCOPE})["verified"]
