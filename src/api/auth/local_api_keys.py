"""
DuckDB-backed local API key store — Issue #66.

Stores bcrypt-hashed API keys in the local_api_keys DuckDB table.
No cloud or external service required.
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import bcrypt

logger = logging.getLogger(__name__)

_KEY_PREFIX_LEN = 8
_KEY_BYTES = 32  # 256-bit random key → 64-char hex string
# Keys carry the "nn_" marker the API-key middleware recognizes in
# ``Authorization: Bearer nn_...`` / ``X-API-Key`` / ``?api_key=``.
KEY_MARKER = "nn_"


def _get_conn():
    from src.database.local_analytics_connector import get_shared_connection
    return get_shared_connection()


def _ensure_table(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS local_api_keys (
            key_id       VARCHAR PRIMARY KEY,
            key_hash     VARCHAR NOT NULL,
            key_prefix   VARCHAR NOT NULL,
            name         VARCHAR NOT NULL,
            role         VARCHAR NOT NULL DEFAULT 'viewer',
            status       VARCHAR NOT NULL DEFAULT 'active',
            created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at   TIMESTAMP,
            last_used_at TIMESTAMP,
            usage_count  INTEGER NOT NULL DEFAULT 0,
            permissions  VARCHAR
        )
    """)
    # Tables created before per-key permissions (issue #1784) lack the column.
    conn.execute("ALTER TABLE local_api_keys ADD COLUMN IF NOT EXISTS permissions VARCHAR")


def _permissions(value: Any) -> List[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in loaded] if isinstance(loaded, list) else []


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def create_api_key(
    name: str,
    role: str = "viewer",
    expires_in_days: Optional[int] = 365,
    permissions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Generate a new API key, store its bcrypt hash, and return the plaintext.

    The plaintext key is returned ONLY at creation time and never stored.
    ``permissions`` are strings such as ``kb:read:<domain>`` or
    ``documents:ingest`` (see ``src/api/auth/key_permissions.py``).
    Returns a dict with: key_id, raw_key, key_prefix, name, role, expires_at, permissions.
    """
    raw = KEY_MARKER + secrets.token_hex(_KEY_BYTES)
    granted = sorted({str(p).strip() for p in permissions or [] if str(p).strip()})
    prefix = raw[:_KEY_PREFIX_LEN]
    key_hash = bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()
    key_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(days=expires_in_days)).isoformat() if expires_in_days else None

    conn = _get_conn()
    _ensure_table(conn)
    conn.execute(
        """
        INSERT INTO local_api_keys
               (key_id, key_hash, key_prefix, name, role, status, created_at, expires_at, permissions)
        VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)
        """,
        [key_id, key_hash, prefix, name, role, now.isoformat(), expires_at, json.dumps(granted)],
    )
    logger.info("Created API key %s (%s) role=%s", prefix, key_id, role)
    return {
        "key_id": key_id,
        "raw_key": raw,
        "key_prefix": prefix,
        "name": name,
        "role": role,
        "expires_at": expires_at,
        "permissions": granted,
    }


def verify_api_key(raw: str) -> Optional[Dict[str, Any]]:
    """
    Verify a raw API key against stored bcrypt hashes.

    Returns the key record dict if valid and active, else None.
    Only compares keys whose prefix matches (avoids comparing every key).
    """
    prefix = raw[:_KEY_PREFIX_LEN]
    conn = _get_conn()
    _ensure_table(conn)

    rows = conn.execute(
        "SELECT key_id, key_hash, name, role, status, expires_at, usage_count, permissions "
        "FROM local_api_keys WHERE key_prefix = ? AND status = 'active'",
        [prefix],
    ).fetchall()

    now = datetime.now(timezone.utc)
    for row in rows:
        key_id, key_hash, name, role, status, expires_at, usage_count, permissions = row
        # Reject expired keys
        if expires_at:
            exp_dt = datetime.fromisoformat(str(expires_at))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if exp_dt < now:
                continue
        if bcrypt.checkpw(raw.encode(), key_hash.encode()):
            # Update last_used_at and usage_count
            conn.execute(
                "UPDATE local_api_keys SET last_used_at = ?, usage_count = usage_count + 1 "
                "WHERE key_id = ?",
                [now.isoformat(), key_id],
            )
            return {"key_id": key_id, "name": name, "role": role, "status": status,
                    "permissions": _permissions(permissions)}
    return None


def revoke_api_key(key_id: str) -> bool:
    """Set a key's status to 'revoked'. Returns True if the key existed."""
    conn = _get_conn()
    _ensure_table(conn)
    conn.execute(
        "UPDATE local_api_keys SET status = 'revoked' WHERE key_id = ?", [key_id]
    )
    rows = conn.execute(
        "SELECT COUNT(*) FROM local_api_keys WHERE key_id = ?", [key_id]
    ).fetchone()
    return bool(rows and rows[0] > 0)


def list_api_keys() -> List[Dict[str, Any]]:
    """Return all keys (hash omitted) ordered by creation date."""
    conn = _get_conn()
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT key_id, key_prefix, name, role, status, created_at, expires_at, "
        "last_used_at, usage_count, permissions FROM local_api_keys ORDER BY created_at DESC"
    ).fetchall()
    cols = ["key_id", "key_prefix", "name", "role", "status", "created_at",
            "expires_at", "last_used_at", "usage_count", "permissions"]
    return [{**dict(zip(cols, r)), "permissions": _permissions(r[-1])} for r in rows]


def get_api_key(key_id: str) -> Optional[Dict[str, Any]]:
    """Return a single key record by ID (hash omitted), or None."""
    conn = _get_conn()
    _ensure_table(conn)
    row = conn.execute(
        "SELECT key_id, key_prefix, name, role, status, created_at, expires_at, "
        "last_used_at, usage_count, permissions FROM local_api_keys WHERE key_id = ?",
        [key_id],
    ).fetchone()
    if not row:
        return None
    cols = ["key_id", "key_prefix", "name", "role", "status", "created_at",
            "expires_at", "last_used_at", "usage_count", "permissions"]
    return {**dict(zip(cols, row)), "permissions": _permissions(row[-1])}
