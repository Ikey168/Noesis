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

# Track C (#771): every document an asset appears in, so a recycled image is
# visible as one asset with multiple appearances.
_APPEARANCES_DDL = """
CREATE TABLE IF NOT EXISTS image_appearances (
    sha256          TEXT NOT NULL,
    document_id     TEXT NOT NULL,
    first_seen_at   BIGINT,
    context         TEXT,
    PRIMARY KEY (sha256, document_id)
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
        self._conn.execute(_APPEARANCES_DDL)

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

    # --- Track C (#771): appearances + provenance extraction ---------------

    def record_appearance(
        self,
        sha256: str,
        document_id: str,
        context: Optional[str] = None,
        now_ms: Optional[int] = None,
    ) -> None:
        """Record that an asset appears in a document (idempotent per pair)."""
        self._conn.execute(
            """
            INSERT INTO image_appearances (sha256, document_id, first_seen_at, context)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (sha256, document_id) DO NOTHING
            """,
            [sha256, document_id, now_ms, context],
        )

    def appearances(self, sha256: str) -> List[Dict[str, Any]]:
        """Every document an asset appears in, earliest first."""
        rows = self._conn.execute(
            """
            SELECT sha256, document_id, first_seen_at, context
            FROM image_appearances WHERE sha256 = ?
            ORDER BY first_seen_at NULLS LAST, document_id
            """,
            [sha256],
        ).fetchall()
        keys = ["sha256", "document_id", "first_seen_at", "context"]
        return [dict(zip(keys, r)) for r in rows]

    def enrich(
        self,
        sha256: str,
        phash: Optional[str] = None,
        exif: Optional[Dict[str, Any]] = None,
        c2pa: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Populate the reserved provenance columns for an asset (only the
        provided ones; None leaves a column unchanged)."""
        import json

        sets: List[str] = []
        params: List[Any] = []
        if phash is not None:
            sets.append("phash = ?")
            params.append(phash)
        if exif is not None:
            sets.append("exif = ?")
            params.append(json.dumps(exif))
        if c2pa is not None:
            sets.append("c2pa = ?")
            params.append(json.dumps(c2pa))
        if not sets:
            return
        params.append(sha256)
        self._conn.execute(f"UPDATE image_assets SET {', '.join(sets)} WHERE sha256 = ?", params)

    def get_provenance(self, sha256: str) -> Optional[Dict[str, Any]]:
        """Return the provenance columns (phash/exif/c2pa) for an asset."""
        import json

        row = self._conn.execute(
            "SELECT phash, exif, c2pa FROM image_assets WHERE sha256 = ?", [sha256]
        ).fetchone()
        if row is None:
            return None
        phash, exif, c2pa = row
        return {
            "phash": phash,
            "exif": json.loads(exif) if isinstance(exif, str) else exif,
            "c2pa": json.loads(c2pa) if isinstance(c2pa, str) else c2pa,
        }

    def ingest(
        self,
        data: bytes,
        document_id: Optional[str] = None,
        context: Optional[str] = None,
        now_ms: Optional[int] = None,
        mime_hint: Optional[str] = None,
        extract: bool = True,
    ) -> ImageAsset:
        """Store bytes, record the appearance, and (by default) extract phash +
        EXIF. The one call a connector uses per encountered image: dedupes by
        content, tracks every source the image appears in, and populates
        provenance the first time.
        """
        asset = self.put(data, parent_document_id=document_id, now_ms=now_ms, mime_hint=mime_hint)
        if document_id is not None:
            self.record_appearance(asset.sha256, document_id, context=context, now_ms=now_ms)
        if extract and self.get_provenance(asset.sha256).get("phash") is None:
            from src.ingestion.assets.provenance import extract_exif, perceptual_hash

            self.enrich(asset.sha256, phash=perceptual_hash(data), exif=extract_exif(data))
        return asset

    def backfill_provenance(self, limit: Optional[int] = None) -> int:
        """Compute phash + EXIF for stored assets missing a phash. Returns the
        number enriched. The batch path for assets ingested before C1."""
        from src.ingestion.assets.provenance import extract_exif, perceptual_hash

        clause = f" LIMIT {int(limit)}" if limit else ""
        rows = self._conn.execute(
            f"SELECT sha256 FROM image_assets WHERE phash IS NULL{clause}"
        ).fetchall()
        enriched = 0
        for (sha256,) in rows:
            data = self.read_bytes(sha256)
            if data is None:
                continue
            self.enrich(sha256, phash=perceptual_hash(data), exif=extract_exif(data))
            enriched += 1
        return enriched
