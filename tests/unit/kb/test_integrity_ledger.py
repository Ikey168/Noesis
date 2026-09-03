"""Unified integrity ledger tests (#1003)."""
from __future__ import annotations

import json

import duckdb

from src.analytics.honesty import validate_analytic_output
from src.ingestion.assets.store import ImageAssetStore
from src.ingestion.corrections import record_revision
from src.ingestion.document_store import DocumentStore
from src.ingestion.snapshots import SnapshotStore
from src.integrity.ledger import document_integrity, integrity_ledger
from services.ingest.common.document_model import Document


def _world(tmp_path):
    conn = duckdb.connect()
    docs = DocumentStore(conn)
    docs.upsert([
        Document("d1", "news", "en", 1000, source_id="wire-a",
                 url="https://source.test/story", content="Revenue was 10 million."),
        Document("d2", "news", "en", 1100, source_id="wire-b",
                 url="https://other.test/story", content="A related story."),
    ], validate=False)
    SnapshotStore(conn).snapshot("https://source.test/story", "<p>version one</p>", 1000)
    record_revision(conn, "d1", "Revenue was 10 million.", fetched_at=1000)
    record_revision(conn, "d1", "Revenue was 30 million.", fetched_at=2000)
    assets = ImageAssetStore(conn, root=str(tmp_path / "assets"))
    asset = assets.put(b"not-a-real-image", parent_document_id="d1", now_ms=1000)
    assets.record_appearance(asset.sha256, "d1", "hero", 1000)
    assets.record_appearance(asset.sha256, "d2", "old event", 1100)
    assets.enrich(asset.sha256, phash="0" * 16,
                  c2pa={"status": "no_credentials", "note": "neutral"})
    return conn


def test_silent_edit_cites_both_versions(tmp_path):
    conn = _world(tmp_path)
    result = document_integrity(conn, "d1")
    assert validate_analytic_output(result) == []
    edit = next(f for f in result["findings"] if f["kind"] == "document_revision")
    assert edit["change_class"] == "silent_substantive"
    assert len(edit["evidence"]) == 2
    assert {e["revision"] for e in edit["evidence"]} == {0, 1}
    assert all(e["cited"] and e["content_hash"] for e in edit["evidence"])


def test_media_reuse_and_c2pa_neutrality(tmp_path):
    conn = _world(tmp_path)
    result = document_integrity(conn, "d1")
    reuse = next(f for f in result["findings"] if f["kind"] == "image_reuse")
    assert len(reuse["evidence"]) == 2
    assert result["assets"][0]["c2pa_status"] == "no_credentials"
    assert not any(f["kind"] == "invalid_content_credentials" for f in result["findings"])


def test_aggregate_preserves_honesty_envelope(tmp_path):
    conn = _world(tmp_path)
    result = integrity_ledger(conn, ["d1", "d2"])
    assert validate_analytic_output(result) == []
    assert result["document_count"] == 2
    assert result["finding_count"] >= 2
