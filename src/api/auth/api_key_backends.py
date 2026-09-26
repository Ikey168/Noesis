"""Resolve an ``nn_`` API key against the configured key store (issue #1784).

``NOESIS_API_KEY_BACKEND`` selects the store:

* ``local`` (default): bcrypt-hashed keys in the DuckDB ``local_api_keys``
  table (``src/api/auth/local_api_keys.py``), created via ``/security/api-keys``.
* ``dynamodb``: PBKDF2-hashed keys in the DynamoDB table managed by
  ``src/api/auth/api_key_manager.py``.

Both return the same shape so the middleware and permission checks do not
depend on the store.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field

BACKEND_ENV = "NOESIS_API_KEY_BACKEND"


@dataclass(frozen=True)
class ResolvedAPIKey:
    key_id: str
    user_id: str
    backend: str
    permissions: list[str] = field(default_factory=list)
    rate_limit: int | None = None


def backend_name() -> str:
    return os.getenv(BACKEND_ENV, "local").strip().lower() or "local"


async def resolve_api_key(raw: str) -> ResolvedAPIKey | None:
    if not raw or not raw.startswith("nn_"):
        return None
    if backend_name() == "dynamodb":
        from src.api.auth.api_key_manager import api_key_manager

        key = await api_key_manager.verify_api_key(raw)
        if key is None:
            return None
        return ResolvedAPIKey(key_id=key.key_id, user_id=key.user_id, backend="dynamodb",
                              permissions=list(key.permissions or []), rate_limit=key.rate_limit)
    from src.api.auth import local_api_keys

    record = await asyncio.to_thread(local_api_keys.verify_api_key, raw)  # bcrypt is CPU-bound
    if record is None:
        return None
    return ResolvedAPIKey(key_id=record["key_id"], user_id=f"api-key:{record['name']}", backend="local",
                          permissions=list(record.get("permissions") or []))
