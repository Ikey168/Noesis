"""API-key permissions for KB domain reads and document ingest (issue #1784).

Permission strings carried by an API key:

* ``kb:read:<domain>``: read one knowledge domain; ``kb:read:*`` reads all.
* ``documents:ingest``: ``POST /documents/ingest`` for any source type;
  ``documents:ingest:<source_type>`` limits it to one source type.

Only requests authenticated with an API key are checked. JWT users and
unauthenticated calls to public routes keep their current behaviour.

Migration: a key that carries no ``kb:``/``documents:`` permission at all is
a *legacy* key. It is denied by default. Setting
``NOESIS_API_KEY_LEGACY_ACCESS=allow`` restores the previous unrestricted
behaviour for such keys while their permissions are being assigned.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from typing import Any

KB_READ_PREFIX = "kb:read:"
KB_READ_ALL = "kb:read:*"
DOCUMENTS_INGEST = "documents:ingest"
LEGACY_ENV = "NOESIS_API_KEY_LEGACY_ACCESS"
_SCOPED_PREFIXES = ("kb:", "documents:")


class PermissionDenied(Exception):
    """An API key lacks the permission a route requires."""

    code = "unauthorized"

    def __init__(self, message: str, *, required: str) -> None:
        super().__init__(message)
        self.required = required


def api_key_permissions(request: Any) -> list[str] | None:
    """The key's permissions, or ``None`` when the request did not use an API key."""

    if request is None:
        return None
    state = getattr(request, "state", None)
    if not getattr(state, "api_key_auth", False):
        return None
    return [str(p) for p in (getattr(state, "api_key_permissions", None) or [])]


def legacy_access_allowed() -> bool:
    return os.getenv(LEGACY_ENV, "deny").strip().lower() in {"allow", "1", "true", "yes", "on"}


def _legacy(permissions: Iterable[str]) -> bool:
    return not any(p.startswith(_SCOPED_PREFIXES) for p in permissions)


def can_read_domain(permissions: list[str] | None, domain: str) -> bool:
    if permissions is None:
        return True
    if _legacy(permissions):
        return legacy_access_allowed()
    return KB_READ_ALL in permissions or f"{KB_READ_PREFIX}{domain}" in permissions


def can_ingest(permissions: list[str] | None, source_type: str) -> bool:
    if permissions is None:
        return True
    if _legacy(permissions):
        return legacy_access_allowed()
    return DOCUMENTS_INGEST in permissions or f"{DOCUMENTS_INGEST}:{source_type}" in permissions


def require_domain(request: Any, domain: str) -> None:
    if not can_read_domain(api_key_permissions(request), domain):
        raise PermissionDenied(f"API key lacks {KB_READ_PREFIX}{domain}", required=f"{KB_READ_PREFIX}{domain}")


def require_domains(request: Any, domains: Iterable[str] | None) -> None:
    for domain in domains or []:
        require_domain(request, str(domain).strip())


def domain_filter(request: Any) -> Callable[[str], bool] | None:
    """A predicate for ``all_authorized`` scopes, or ``None`` when nothing is filtered."""

    permissions = api_key_permissions(request)
    if permissions is None:
        return None
    return lambda domain: can_read_domain(permissions, domain)


def require_ingest(request: Any, source_type: str) -> None:
    if not can_ingest(api_key_permissions(request), source_type):
        raise PermissionDenied(f"API key lacks {DOCUMENTS_INGEST} for source_type {source_type!r}",
                               required=DOCUMENTS_INGEST)
