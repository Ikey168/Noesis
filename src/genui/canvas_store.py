"""
Server-persisted canvas store (M8.1).

A generated canvas is normally ephemeral: it lives in the browser for one
session and is gone on reload. This store persists a canvas server-side so it
can be saved and reopened later, surviving the session. Two things are kept:

* the **ui-spec** (the ``ui-spec-v1`` layout document), and
* its **live data bindings** — the descriptors that tell each panel which
  data-mode tool feeds it (server / tool / arguments), plus any snapshot the
  client captured — so reopening rebinds to live data instead of a blank grid.

One warehouse-side table, ``saved_canvases``, outside the shared corpus. Every
canvas has an ``owner`` and an unguessable ``id``; reads can be scoped to an
owner so one owner never sees another's canvas (the access model M8.3 builds
on). Writes are keyed by id, so re-saving an existing canvas updates it in place
rather than duplicating.

Stdlib-only (``json`` + ``secrets``); the connection is injected.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Dict, List, Optional

from src.genui import canvas_access

# Size ceiling for a persisted canvas payload (spec + bindings, serialized), so a
# runaway spec cannot bloat the warehouse. Enforced by :func:`save_canvas`.
MAX_CANVAS_BYTES = 256 * 1024


class CanvasStoreError(ValueError):
    """A canvas could not be saved (e.g. it exceeds the size ceiling)."""


def ensure_schema(conn) -> None:
    """Create the ``saved_canvases`` table if absent (idempotent)."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS saved_canvases ("
        "id VARCHAR PRIMARY KEY, owner VARCHAR, title VARCHAR, "
        "spec VARCHAR, data_bindings VARCHAR, share_token VARCHAR, "
        "created_at TIMESTAMP, updated_at TIMESTAMP)"
    )
    # Migrate a store that predates the share column (M8.2 adds sharing).
    try:
        conn.execute("ALTER TABLE saved_canvases ADD COLUMN IF NOT EXISTS share_token VARCHAR")
    except Exception:
        pass


def schema_ready(conn) -> bool:
    """True once the ``saved_canvases`` table exists (reads may run before any
    canvas has been saved)."""
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name = 'saved_canvases'"
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def _loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except Exception:
        return fallback


def _row_to_canvas(row) -> Dict[str, Any]:
    return {
        "id": row[0],
        "owner": row[1],
        "title": row[2],
        "spec": _loads(row[3], {}),
        "data_bindings": _loads(row[4], {}),
        "share_token": row[5],
        "created_at": str(row[6]) if row[6] is not None else None,
        "updated_at": str(row[7]) if row[7] is not None else None,
    }


_COLUMNS = "id, owner, title, spec, data_bindings, share_token, created_at, updated_at"


def new_id() -> str:
    """An unguessable, URL-safe canvas id."""
    return secrets.token_urlsafe(9)


def new_share_token() -> str:
    """An unguessable, URL-safe share token (longer than a canvas id, since it
    is the only credential a read-only link carries)."""
    return secrets.token_urlsafe(24)


def save_canvas(
    conn,
    owner: str,
    spec: Dict[str, Any],
    now: Any,
    data_bindings: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
    canvas_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist a canvas (its spec plus live data bindings) and return the stored
    record.

    With no ``canvas_id`` a new canvas is created under ``owner``. With a
    ``canvas_id`` that ``owner`` already owns, the canvas is updated in place
    (spec, bindings, title refreshed; id, owner, ``created_at`` and any
    ``share_token`` preserved). A ``canvas_id`` the owner does not own is treated
    as a new canvas, so one owner can never overwrite another's.
    """
    bindings = data_bindings or {}
    spec_json = json.dumps(spec, default=str)
    bindings_json = json.dumps(bindings, default=str)
    if len(spec_json) + len(bindings_json) > MAX_CANVAS_BYTES:
        raise CanvasStoreError(
            f"canvas exceeds the {MAX_CANVAS_BYTES}-byte ceiling"
        )
    resolved_title = title if title is not None else (spec.get("title") or "Untitled canvas")

    existing = get_canvas(conn, canvas_id, owner=owner) if canvas_id else None
    if existing is None:
        cid = new_id()
        conn.execute(
            "INSERT INTO saved_canvases "
            "(id, owner, title, spec, data_bindings, share_token, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            [cid, owner, resolved_title, spec_json, bindings_json, now, now],
        )
        return get_canvas(conn, cid)
    conn.execute(
        "UPDATE saved_canvases SET title = ?, spec = ?, data_bindings = ?, updated_at = ? "
        "WHERE id = ?",
        [resolved_title, spec_json, bindings_json, now, existing["id"]],
    )
    return get_canvas(conn, existing["id"])


def get_canvas(
    conn, canvas_id: str, owner: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """The canvas record for ``canvas_id``, or None. When ``owner`` is given, a
    canvas owned by someone else reads back as None (owner isolation)."""
    if not schema_ready(conn) or not canvas_id:
        return None
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM saved_canvases WHERE id = ?", [canvas_id]
    ).fetchone()
    if not row:
        return None
    canvas = _row_to_canvas(row)
    # Owner-scoped read: the access model is the single authority. Only the owner
    # role can read, so a non-owner requester reads back as None (isolation).
    if owner is not None and not canvas_access.authorize(canvas, owner, canvas_access.READ):
        return None
    return canvas


def list_canvases(conn, owner: str) -> List[Dict[str, Any]]:
    """An owner's saved canvases, newest first. Spec and bindings are elided to a
    lightweight summary so a listing is cheap; reopen one by id for the full
    record."""
    if not schema_ready(conn):
        return []
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM saved_canvases WHERE owner = ? "
        "ORDER BY updated_at DESC NULLS LAST, id",
        [owner],
    ).fetchall()
    out = []
    for r in rows:
        c = _row_to_canvas(r)
        spec = c["spec"] if isinstance(c["spec"], dict) else {}
        out.append(
            {
                "id": c["id"],
                "title": c["title"],
                "topic": spec.get("topic"),
                "panel_count": len(spec.get("panels") or []),
                "shared": c["share_token"] is not None,
                "created_at": c["created_at"],
                "updated_at": c["updated_at"],
            }
        )
    return out


def delete_canvas(conn, canvas_id: str, owner: str) -> bool:
    """Delete a canvas the owner owns. Returns True if a row was removed."""
    if get_canvas(conn, canvas_id, owner=owner) is None:
        return False
    conn.execute("DELETE FROM saved_canvases WHERE id = ?", [canvas_id])
    return True


# --------------------------------------------------------------------------- #
# Read-only sharing (M8.2)
# --------------------------------------------------------------------------- #

def share_canvas(conn, canvas_id: str, owner: str) -> Optional[str]:
    """Mint (or return the existing) read-only share token for a canvas the
    owner owns. Idempotent: sharing an already-shared canvas returns the same
    token, so a shared link stays stable. Returns None if the owner does not own
    the canvas."""
    canvas = get_canvas(conn, canvas_id, owner=owner)
    if canvas is None:
        return None
    if canvas["share_token"]:
        return canvas["share_token"]
    token = new_share_token()
    conn.execute(
        "UPDATE saved_canvases SET share_token = ? WHERE id = ?", [token, canvas_id]
    )
    return token


def unshare_canvas(conn, canvas_id: str, owner: str) -> bool:
    """Revoke a canvas's share token so its read-only link stops resolving.
    Returns True if the owner owns the canvas (whether or not it was shared)."""
    canvas = get_canvas(conn, canvas_id, owner=owner)
    if canvas is None:
        return False
    conn.execute(
        "UPDATE saved_canvases SET share_token = NULL WHERE id = ?", [canvas_id]
    )
    return True


def get_shared_canvas(conn, share_token: str) -> Optional[Dict[str, Any]]:
    """Resolve a canvas by its read-only share token, for a viewer who is not the
    owner. Returns the record with ``read_only`` set and the owner elided, or
    None when the token is unknown or revoked. This path never mutates and never
    exposes the owner's identity."""
    if not schema_ready(conn) or not share_token:
        return None
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM saved_canvases WHERE share_token = ?",
        [share_token],
    ).fetchone()
    if not row:
        return None
    canvas = _row_to_canvas(row)
    return {
        "id": canvas["id"],
        "title": canvas["title"],
        "spec": canvas["spec"],
        "data_bindings": canvas["data_bindings"],
        "read_only": True,
        "created_at": canvas["created_at"],
        "updated_at": canvas["updated_at"],
    }
