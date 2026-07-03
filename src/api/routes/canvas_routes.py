"""Persisted-canvas routes (M8).

A generated canvas is ephemeral by default. These routes let a canvas be saved
server-side and reopened later with its live data bindings intact (M8.1):

    POST   /api/v1/ui/canvas          save a canvas (create or update)
    GET    /api/v1/ui/canvas          list the owner's saved canvases
    GET    /api/v1/ui/canvas/{id}     reopen one canvas (owner-scoped)
    DELETE /api/v1/ui/canvas/{id}     delete one canvas (owner-scoped)

Owner identity for this prototype comes from the ``X-Canvas-Owner`` header
(default ``local``); the access model M8.3 formalises it. The persisted spec is
validated against the ``ui-spec-v1`` contract on save, so a malformed layout is
never stored.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.genui import canvas_store
from src.genui.spec import validate_spec

router = APIRouter(prefix="/api/v1/ui", tags=["generative_ui_canvas"])

DEFAULT_OWNER = "local"


def _owner(header_value: Optional[str]) -> str:
    """The owner for a request, from the ``X-Canvas-Owner`` header."""
    value = (header_value or "").strip()
    return value[:128] if value else DEFAULT_OWNER


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn():
    """The shared, writable warehouse connection with its serialising lock."""
    from src.database.local_analytics_connector import _LOCK, get_shared_connection

    return get_shared_connection(), _LOCK


class SaveCanvasRequest(BaseModel):
    """Body for POST /api/v1/ui/canvas."""

    spec: Dict[str, Any] = Field(..., description="The ui-spec-v1 layout to persist")
    data_bindings: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Per-panel live data bindings (server/tool/arguments/snapshot)",
    )
    title: Optional[str] = Field(default=None, max_length=200)
    id: Optional[str] = Field(
        default=None, max_length=64, description="Existing canvas id to update in place"
    )


@router.post("/canvas")
def save_canvas(
    request: SaveCanvasRequest,
    x_canvas_owner: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Save a canvas (its spec plus live data bindings) and return the stored
    record. Creates a new canvas, or updates one the owner already owns when
    ``id`` is given. Sync on purpose: the warehouse write runs in the
    threadpool.
    """
    errors = validate_spec(request.spec)
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"spec failed validation: {'; '.join(errors[:3])}",
        )
    owner = _owner(x_canvas_owner)
    conn, lock = _conn()
    try:
        with lock:
            canvas_store.ensure_schema(conn)
            saved = canvas_store.save_canvas(
                conn,
                owner=owner,
                spec=request.spec,
                now=_now(),
                data_bindings=request.data_bindings,
                title=request.title,
                canvas_id=request.id,
            )
    except canvas_store.CanvasStoreError as err:
        raise HTTPException(status_code=413, detail=str(err))
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"canvas save failed: {err}")
    return {"canvas": saved}


@router.get("/canvas")
def list_canvases(
    x_canvas_owner: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """List the owner's saved canvases (lightweight summaries)."""
    owner = _owner(x_canvas_owner)
    conn, lock = _conn()
    try:
        with lock:
            canvas_store.ensure_schema(conn)
            canvases = canvas_store.list_canvases(conn, owner)
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"canvas list failed: {err}")
    return {"canvases": canvases, "count": len(canvases)}


@router.get("/canvas/{canvas_id}")
def get_canvas(
    canvas_id: str,
    x_canvas_owner: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Reopen a saved canvas by id, with its spec and live data bindings intact.
    Scoped to the owner: another owner's canvas reads back as 404."""
    owner = _owner(x_canvas_owner)
    conn, lock = _conn()
    try:
        with lock:
            canvas_store.ensure_schema(conn)
            canvas = canvas_store.get_canvas(conn, canvas_id, owner=owner)
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"canvas read failed: {err}")
    if canvas is None:
        raise HTTPException(status_code=404, detail="canvas not found")
    return {"canvas": canvas}


@router.delete("/canvas/{canvas_id}")
def delete_canvas(
    canvas_id: str,
    x_canvas_owner: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Delete one of the owner's canvases."""
    owner = _owner(x_canvas_owner)
    conn, lock = _conn()
    try:
        with lock:
            canvas_store.ensure_schema(conn)
            removed = canvas_store.delete_canvas(conn, canvas_id, owner)
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"canvas delete failed: {err}")
    if not removed:
        raise HTTPException(status_code=404, detail="canvas not found")
    return {"deleted": canvas_id}
