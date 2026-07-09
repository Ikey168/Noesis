"""
Figure evidence panel query (Track B / B3).

Panel-facing read over the ``documents`` corpus for figure documents (those the
describers emitted with ``metadata.modality = "image"``). Returns each figure's
description, its ``content_ref`` (for the image preview), and its
``parent_document_id`` (the citation), so the ``figure_evidence`` panel can show
figures matching a topic, each cited to its parent.

Defensive: a warehouse without a ``documents`` table degrades to an empty but
valid payload rather than raising.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_FIGURES = 60


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def figure_evidence(conn, topic: Optional[str] = None, limit: int = MAX_FIGURES) -> Dict[str, Any]:
    """Figure documents matching an optional topic, each cited to its parent.

    A figure is a corpus document whose ``metadata.modality`` is ``image``. The
    topic filter is a case-insensitive substring over the figure content/title.
    """
    if not _table_exists(conn, "documents"):
        return {"figures": [], "count": 0, "note": "no documents corpus"}

    capped = max(1, min(limit, MAX_FIGURES))
    clauses = ["json_extract_string(metadata, '$.modality') = 'image'"]
    params: List[Any] = []
    if topic:
        clauses.append("(LOWER(content) LIKE ? OR LOWER(title) LIKE ?)")
        needle = f"%{topic.lower()}%"
        params.extend([needle, needle])
    where = " AND ".join(clauses)
    try:
        rows = conn.execute(
            f"""
            SELECT document_id, source_type, title, content, content_ref,
                   json_extract_string(metadata, '$.parent_document_id') AS parent_document_id,
                   json_extract_string(metadata, '$.figure_label') AS figure_label
            FROM documents
            WHERE {where}
            ORDER BY document_id
            LIMIT {capped + 1}
            """,
            params,
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if "does not exist" in msg or "not found" in msg.lower():
            return {"figures": [], "count": 0, "note": "no documents corpus"}
        raise

    truncated = len(rows) > capped
    keys = ["document_id", "source_type", "title", "content", "content_ref", "parent_document_id", "figure_label"]
    figures = [dict(zip(keys, r)) for r in rows[:capped]]
    return {"figures": figures, "count": len(figures), "truncated": truncated}
