"""
Content-addressed image asset store (B0 — shared substrate for Tracks B and C).

Both the vision describer (Track B, figures/images become citable documents) and
image provenance (Track C, EXIF/perceptual-hash/C2PA) need one place where image
bytes live and one identity for an image. This is that place:

* Bytes are written under ``artifacts/figures/`` at a path derived from the
  SHA-256 of the content, so the same image is stored once no matter how many
  sources it arrives from (content addressing).
* An ``image_assets`` DuckDB table indexes each asset (sha256, path, mime,
  dimensions, first-seen parent). The Track C columns (``phash``/``exif``/
  ``c2pa``) are present but nullable, so C1 populates them without a migration.

``content_ref`` is the stable, relative pointer a figure ``Document`` carries;
surfaces dereference it to render the actual image next to a citation.

Multi-parent tracking (the same asset appearing in several documents) is Track
C's ``image_appearances`` table; here an asset records only its first-seen
parent, and re-``put`` of the same bytes is idempotent (it never overwrites it).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.ingestion.assets.imageinfo import extension_for, sniff

_ASSETS_DDL = """
CREATE TABLE IF NOT EXISTS image_assets (
    sha256              TEXT PRIMARY KEY,
    path                TEXT NOT NULL,
    mime                TEXT,
    width               INTEGER,
    height              INTEGER,
    parent_document_id  TEXT,
    first_seen_at       BIGINT,
    -- Reserved for Track C (#771); nullable so C1 needs no migration.
    phash               TEXT,
    exif                JSON,
    c2pa                JSON
)
"""


@dataclass
class ImageAsset:
    """An indexed image asset."""

    sha256: str
    path: str  # relative content_ref, e.g. artifacts/figures/ab/ab12...png
    mime: Optional[str]
    width: Optional[int]
    height: Optional[int]
    parent_document_id: Optional[str]
    first_seen_at: Optional[int]

    @property
    def content_ref(self) -> str:
        """The stable pointer a figure Document carries in ``content_ref``."""
        return self.path


class ImageAssetStore:
    """Content-addressed image store: filesystem for bytes, DuckDB for the index."""

    def __init__(self, conn: Any, root: str = "artifacts/figures"):
        """Wrap an open DuckDB connection and a filesystem root. The caller owns
        both lifecycles (in-memory DB + tmp root in tests; a file DB + repo path
        in production)."""
        self._conn = conn
        self._root = root
        os.makedirs(self._root, exist_ok=True)
        self._conn.execute(_ASSETS_DDL)

    @classmethod
    def open(cls, db_path: str = ":memory:", root: str = "artifacts/figures") -> "ImageAssetStore":
        import duckdb

        return cls(duckdb.connect(db_path), root=root)

    @staticmethod
    def digest(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _rel_path(self, sha256: str, mime: Optional[str]) -> str:
        # Shard by the first two hex chars to keep any one directory small.
        return os.path.join(self._root, sha256[:2], f"{sha256}.{extension_for(mime)}")

    def put(
        self,
        data: bytes,
        parent_document_id: Optional[str] = None,
        now_ms: Optional[int] = None,
        mime_hint: Optional[str] = None,
    ) -> ImageAsset:
        """Store image bytes and index them, returning the asset.

        Idempotent by content: storing the same bytes again returns the existing
        asset unchanged (the original ``parent_document_id`` and ``first_seen_at``
        are preserved), so the same image from a second source does not create a
        second asset or clobber provenance.
        """
        sha256 = self.digest(data)
        existing = self.get(sha256)
        if existing is not None:
            return existing

        mime, width, height = sniff(data)
        mime = mime or mime_hint
        rel_path = self._rel_path(sha256, mime)
        abs_path = os.path.abspath(rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        if not os.path.exists(abs_path):
            with open(abs_path, "wb") as f:
                f.write(data)

        self._conn.execute(
            """
            INSERT INTO image_assets (sha256, path, mime, width, height, parent_document_id, first_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (sha256) DO NOTHING
            """,
            [sha256, rel_path, mime, width, height, parent_document_id, now_ms],
        )
        stored = self.get(sha256)
        assert stored is not None  # just inserted
        return stored

    def get(self, sha256: str) -> Optional[ImageAsset]:
        row = self._conn.execute(
            """
            SELECT sha256, path, mime, width, height, parent_document_id, first_seen_at
            FROM image_assets WHERE sha256 = ?
            """,
            [sha256],
        ).fetchone()
        if row is None:
            return None
        return ImageAsset(*row)

    def exists(self, sha256: str) -> bool:
        return self.get(sha256) is not None

    def read_bytes(self, sha256: str) -> Optional[bytes]:
        """Return the stored bytes for an asset, or None if unknown/missing."""
        asset = self.get(sha256)
        if asset is None:
            return None
        abs_path = os.path.abspath(asset.path)
        if not os.path.exists(abs_path):
            return None
        with open(abs_path, "rb") as f:
            return f.read()

    def list_assets(self, parent_document_id: Optional[str] = None) -> List[Dict[str, Any]]:
        clause = " WHERE parent_document_id = ?" if parent_document_id is not None else ""
        params = [parent_document_id] if parent_document_id is not None else []
        rows = self._conn.execute(
            f"SELECT sha256, path, mime, width, height, parent_document_id, first_seen_at FROM image_assets{clause} ORDER BY sha256",
            params,
        ).fetchall()
        keys = ["sha256", "path", "mime", "width", "height", "parent_document_id", "first_seen_at"]
        return [dict(zip(keys, row)) for row in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM image_assets").fetchone()[0]
