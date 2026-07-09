"""
Offline data-privacy endpoints — Issue #64.

DELETE /user/data          Remove all user-linked rows; return deletion receipt.
GET    /user/data/export   Download user data as a ZIP of JSON files.
GET    /user/privacy       Get privacy preferences.
PATCH  /user/privacy       Update privacy preferences.
"""
from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.api.auth.jwt_auth import require_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/user", tags=["privacy"])

# Tables that contain user-generated or curated data that can be erased.
# (the documents corpus, source_stances, and argument_claims as stated in the
#  issue; plus the derived/linked tables that reference them.) news_articles is
#  now a view over documents (#909), so the base tables documents +
#  document_enrichments are erased instead.
_ERASABLE_TABLES = [
    "documents",
    "document_enrichments",
    "argument_claims",
    "claim_evidence",
    "source_stances",
    "stance_drift_events",
    "policy_positions",
    "position_updates",
    "claim_conflicts",
    "document_actors",
    "document_frames",
    "outlet_clusters",
    "outlet_scores",
]

# Tables exported in the data report (read-only; superset of erasable).
_EXPORT_TABLES = _ERASABLE_TABLES + ["user_privacy_prefs"]


def _get_conn():
    from src.database.local_analytics_connector import get_shared_connection
    return get_shared_connection()


def _ensure_prefs_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_privacy_prefs (
            pref_key   VARCHAR PRIMARY KEY,
            pref_value VARCHAR NOT NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)


# ---------------------------------------------------------------------------
# DELETE /user/data
# ---------------------------------------------------------------------------

@router.delete("/data", summary="Right to be Forgotten — delete all local user data")
async def delete_user_data(current_user: dict = Depends(require_auth)) -> Dict[str, Any]:
    """
    Remove all rows from user-linked tables in the local DuckDB warehouse.

    Returns a deletion receipt with the timestamp and per-table row counts
    deleted. No data is sent off-device.
    """
    conn = _get_conn()
    receipt: Dict[str, int] = {}

    for table in _ERASABLE_TABLES:
        try:
            # Count before delete
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            conn.execute(f"DELETE FROM {table}")  # noqa: S608
            receipt[table] = count
        except Exception as exc:
            # Table may not exist yet (fresh install) — record 0 and continue.
            logger.debug("Could not delete from %s: %s", table, exc)
            receipt[table] = 0

    return {
        "deleted_at": datetime.now(timezone.utc).isoformat(),
        "tables": receipt,
        "total_rows_deleted": sum(receipt.values()),
        "note": "All data was stored locally. Nothing was sent off-device.",
    }


# ---------------------------------------------------------------------------
# GET /user/data/export
# ---------------------------------------------------------------------------

@router.get("/data/export", summary="Download a ZIP of all local user data as JSON")
async def export_user_data(current_user: dict = Depends(require_auth)):
    """
    Stream a ZIP archive containing one JSON file per table with user data.

    Works entirely from the local DuckDB file — no network calls.
    """
    conn = _get_conn()
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for table in _EXPORT_TABLES:
            try:
                rows = conn.execute(f"SELECT * FROM {table}").fetchdf()  # noqa: S608
                records = rows.to_dict(orient="records")
            except Exception as exc:
                logger.debug("Could not export %s: %s", table, exc)
                records = []
            zf.writestr(f"{table}.json", json.dumps(records, default=str, indent=2))

        # Add a manifest
        manifest = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "tables": _EXPORT_TABLES,
            "note": "All data originates from this local machine. Nothing was sent off-device.",
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    buf.seek(0)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"neuronews_data_export_{timestamp}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# GET /user/privacy
# ---------------------------------------------------------------------------

@router.get("/privacy", summary="Get privacy preferences")
async def get_privacy_prefs(current_user: dict = Depends(require_auth)) -> Dict[str, Any]:
    """Return all stored privacy preferences as a key→value dict."""
    conn = _get_conn()
    _ensure_prefs_table(conn)

    rows = conn.execute(
        "SELECT pref_key, pref_value, updated_at FROM user_privacy_prefs ORDER BY pref_key"
    ).fetchall()

    prefs = {r[0]: {"value": r[1], "updated_at": str(r[2])} for r in rows}
    return {"preferences": prefs}


# ---------------------------------------------------------------------------
# PATCH /user/privacy
# ---------------------------------------------------------------------------

class PrivacyPrefUpdate(BaseModel):
    preferences: Dict[str, str]


@router.patch("/privacy", summary="Update privacy preferences")
async def update_privacy_prefs(
    body: PrivacyPrefUpdate,
    current_user: dict = Depends(require_auth),
) -> Dict[str, Any]:
    """Upsert one or more privacy preference key/value pairs."""
    conn = _get_conn()
    _ensure_prefs_table(conn)

    now = datetime.now(timezone.utc).isoformat()
    updated: list[str] = []
    for key, value in body.preferences.items():
        if not key or len(key) > 128:
            raise HTTPException(status_code=422, detail=f"Invalid pref_key: {key!r}")
        conn.execute(
            """
            INSERT INTO user_privacy_prefs (pref_key, pref_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT (pref_key) DO UPDATE SET pref_value = excluded.pref_value,
                                                  updated_at = excluded.updated_at
            """,
            [key, str(value), now],
        )
        updated.append(key)

    return {"updated": updated, "updated_at": now}
