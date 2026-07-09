"""
Review-gated imagery tools (Track C / C4).

The external imagery tier: ``reverse_image_search`` and ``geolocate_image``.
These point at the outside world, so they ship only behind the OSINT review gate
(``docs/security/osint-review-gate.md``) and the imagery abuse analysis
(``docs/security/osint-abuse-analysis.md``, "Imagery"), off by default. This module is
the enforcement, not just the docs:

* **Corpus images only.** Both tools take a corpus asset ``sha256`` that must
  exist in ``image_assets`` — never an operator-supplied photo of a person.
* **No person identification, ever.** A permanent non-goal (guardrail 2): there
  is no person parameter and no identity output. The tools reason about images
  and places, never subjects.
* **Suggestions are not evidence.** Every result enters a review queue as
  ``cited = False`` (the flagged state the evidence discipline renders) and
  becomes citable only when an operator confirms it via :func:`confirm_suggestion`.
* **No default provider.** ``reverse_image_search`` needs an injected provider;
  with none configured the tier is inert (``no_provider_configured``).
* **Least privilege for the queue.** The corpus asset is read from a *read-only*
  warehouse connection; the review-queue write goes to a separate ``queue_conn``
  (a dedicated store) so the gated imagery tier never holds write access to the
  corpus warehouse. ``queue_conn`` defaults to ``conn`` for single-store callers
  (e.g. tests); the served tools pass a read-only corpus conn and a distinct
  read-write queue conn.

See ``docs/architecture/OSINT_IMAGERY_PLAN.md`` §3.3.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Permanent non-goal, asserted in code so it is not silently changed.
PERSON_IDENTIFICATION_SUPPORTED = False

_QUEUE_DDL = """
CREATE TABLE IF NOT EXISTS imagery_review_queue (
    suggestion_id   TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    sha256          TEXT NOT NULL,
    suggestion      JSON NOT NULL,
    cited           BOOLEAN NOT NULL DEFAULT FALSE,
    confirmed_by    TEXT,
    confirmed_at    BIGINT,
    created_at      BIGINT
)
"""


def _ensure_queue(conn) -> None:
    conn.execute(_QUEUE_DDL)


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def _asset_bytes(conn, sha256: str) -> Optional[bytes]:
    """Read a corpus asset's bytes by sha256, or None if it is not a corpus
    image. This is the corpus-images-only guard: an unknown sha256 gets nothing."""
    if not _table_exists(conn, "image_assets"):
        return None
    row = conn.execute("SELECT path FROM image_assets WHERE sha256 = ?", [sha256]).fetchone()
    if row is None or not row[0]:
        return None
    path = os.path.abspath(row[0])
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _queue_suggestion(conn, kind: str, sha256: str, suggestion: Dict[str, Any], now_ms: Optional[int]) -> str:
    _ensure_queue(conn)
    key = f"{kind}|{sha256}|{json.dumps(suggestion, sort_keys=True)}"
    suggestion_id = "sug:" + hashlib.md5(key.encode()).hexdigest()[:16]
    conn.execute(
        """
        INSERT INTO imagery_review_queue (suggestion_id, kind, sha256, suggestion, cited, created_at)
        VALUES (?, ?, ?, ?, FALSE, ?)
        ON CONFLICT (suggestion_id) DO NOTHING
        """,
        [suggestion_id, kind, sha256, json.dumps(suggestion), now_ms],
    )
    return suggestion_id


# Provider signatures (injected; no default ships):
#   ReverseSearchProvider(image_bytes) -> List[{"url", "title"?, "published"?}]
#   GeoVLM(image_bytes) -> List[{"landmark", "place"?, "confidence"?}]
ReverseSearchProvider = Callable[[bytes], List[Dict[str, Any]]]
GeoVLM = Callable[[bytes], List[Dict[str, Any]]]


def reverse_image_search(
    conn,
    sha256: str,
    provider: Optional[ReverseSearchProvider] = None,
    now_ms: Optional[int] = None,
    queue_conn=None,
) -> Dict[str, Any]:
    """Queue reverse-image-search *suggestions* for a corpus asset.

    No default provider ships; with none configured the tier is inert. Results
    are queued uncited — evidence only after operator confirmation. The corpus
    asset is read from ``conn`` (read-only); the queue write goes to
    ``queue_conn`` (defaults to ``conn``).
    """
    queue_conn = conn if queue_conn is None else queue_conn
    if provider is None:
        return {"status": "no_provider_configured", "sha256": sha256,
                "note": "reverse image search has no default provider; supply one to enable"}
    image_bytes = _asset_bytes(conn, sha256)
    if image_bytes is None:
        return {"status": "not_a_corpus_image", "sha256": sha256,
                "note": "reverse search accepts corpus asset hashes only"}
    try:
        hits = provider(image_bytes) or []
    except Exception as exc:  # noqa: BLE001
        return {"status": "provider_error", "error": str(exc)}
    queued = []
    for hit in hits:
        suggestion = {"url": hit.get("url"), "title": hit.get("title"), "cited": False}
        sid = _queue_suggestion(queue_conn, "reverse_image_search", sha256, suggestion, now_ms)
        queued.append({"suggestion_id": sid, **suggestion})
    return {
        "status": "queued",
        "sha256": sha256,
        "suggestions": queued,
        "count": len(queued),
        "note": "suggestions are uncited until an operator confirms them",
    }


def geolocate_image(
    conn,
    sha256: str,
    vlm: Optional[GeoVLM] = None,
    now_ms: Optional[int] = None,
    queue_conn=None,
) -> Dict[str, Any]:
    """Queue visible-landmark geolocation *hypotheses* for a corpus asset.

    Suggestion-grade, never auto-cited. Reasons about the place in the scene,
    never the subject (no person identification). The corpus asset is read from
    ``conn`` (read-only); the queue write goes to ``queue_conn`` (defaults to
    ``conn``).
    """
    queue_conn = conn if queue_conn is None else queue_conn
    if vlm is None:
        return {"status": "no_backend_configured", "sha256": sha256,
                "note": "geolocation assist needs a vision backend"}
    image_bytes = _asset_bytes(conn, sha256)
    if image_bytes is None:
        return {"status": "not_a_corpus_image", "sha256": sha256}
    try:
        hypotheses = vlm(image_bytes) or []
    except Exception as exc:  # noqa: BLE001
        return {"status": "backend_error", "error": str(exc)}
    queued = []
    for h in hypotheses:
        suggestion = {
            "landmark": h.get("landmark"),
            "place": h.get("place"),
            "confidence": h.get("confidence"),
            "grade": "suggestion",
            "cited": False,
        }
        sid = _queue_suggestion(queue_conn, "geolocate_image", sha256, suggestion, now_ms)
        queued.append({"suggestion_id": sid, **suggestion})
    return {
        "status": "queued",
        "sha256": sha256,
        "hypotheses": queued,
        "count": len(queued),
        "note": "visible-landmark hypotheses about the scene, not the subject; uncited until confirmed",
    }


def list_review_queue(conn, cited: Optional[bool] = None, kind: Optional[str] = None) -> Dict[str, Any]:
    """The imagery review queue. By default lists everything; filter by cited
    state or kind."""
    if not _table_exists(conn, "imagery_review_queue"):
        return {"items": [], "count": 0, "note": "review queue empty"}
    clauses: List[str] = []
    params: List[Any] = []
    if cited is not None:
        clauses.append("cited = ?")
        params.append(cited)
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT suggestion_id, kind, sha256, suggestion, cited, confirmed_by FROM imagery_review_queue{where} ORDER BY created_at NULLS LAST, suggestion_id",
        params,
    ).fetchall()
    items = []
    for sid, k, sha, sug, is_cited, by in rows:
        items.append({
            "suggestion_id": sid,
            "kind": k,
            "sha256": sha,
            "suggestion": json.loads(sug) if isinstance(sug, str) else sug,
            "cited": bool(is_cited),
            "confirmed_by": by,
        })
    return {"items": items, "count": len(items)}


def confirm_suggestion(conn, suggestion_id: str, operator: str, now_ms: Optional[int] = None) -> Dict[str, Any]:
    """Operator confirmation — the *only* thing that makes a suggestion citable."""
    if not operator:
        return {"status": "rejected", "note": "confirmation requires an operator identity"}
    if not _table_exists(conn, "imagery_review_queue"):
        return {"status": "not_found"}
    row = conn.execute("SELECT suggestion_id FROM imagery_review_queue WHERE suggestion_id = ?", [suggestion_id]).fetchone()
    if row is None:
        return {"status": "not_found", "suggestion_id": suggestion_id}
    conn.execute(
        "UPDATE imagery_review_queue SET cited = TRUE, confirmed_by = ?, confirmed_at = ? WHERE suggestion_id = ?",
        [operator, now_ms, suggestion_id],
    )
    return {"status": "confirmed", "suggestion_id": suggestion_id, "confirmed_by": operator, "cited": True}
