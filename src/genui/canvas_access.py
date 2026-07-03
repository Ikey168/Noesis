"""
Canvas access model (M8.3).

The single, declarative authority for who may do what to a persisted canvas.
M8.1 gave canvases an owner and M8.2 gave them read-only share links; this module
states the resulting permission model in one place so it is enforced uniformly
rather than re-derived at each call site.

Three roles, one permission matrix:

* **owner** - the identity that saved the canvas: may read, write, share, delete.
* **viewer** - anyone holding a valid read-only share link: may read only.
* **none** - everyone else: no access at all.

:func:`authorize` is the one enforcement entry point; the store consults it for
every owner-scoped read, and the shared-link read path grants the viewer role.
Stdlib-only, no I/O: it decides on an already-loaded canvas record.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

ROLE_OWNER = "owner"
ROLE_VIEWER = "viewer"  # holds a valid read-only share link
ROLE_NONE = "none"

READ = "read"
WRITE = "write"
SHARE = "share"
DELETE = "delete"
ACTIONS = (READ, WRITE, SHARE, DELETE)

# The permission matrix. Owners do everything; viewers read only; nobody else
# touches the canvas.
_PERMISSIONS = {
    ROLE_OWNER: frozenset({READ, WRITE, SHARE, DELETE}),
    ROLE_VIEWER: frozenset({READ}),
    ROLE_NONE: frozenset(),
}


def role_for(
    canvas: Dict[str, Any], requester: Optional[str], via_share_token: bool = False
) -> str:
    """The role ``requester`` holds on ``canvas``. Ownership wins: the saving
    identity is the owner. A requester who arrived via a valid share link (and is
    not the owner) is a viewer. Everyone else is ``none``."""
    if requester is not None and canvas.get("owner") == requester:
        return ROLE_OWNER
    if via_share_token:
        return ROLE_VIEWER
    return ROLE_NONE


def can(role: str, action: str) -> bool:
    """Whether ``role`` may perform ``action`` (one of :data:`ACTIONS`)."""
    return action in _PERMISSIONS.get(role, frozenset())


def authorize(
    canvas: Dict[str, Any],
    requester: Optional[str],
    action: str,
    via_share_token: bool = False,
) -> bool:
    """The one enforcement decision: may ``requester`` perform ``action`` on
    ``canvas``? Resolves the role, then checks the matrix."""
    return can(role_for(canvas, requester, via_share_token=via_share_token), action)


__all__ = [
    "ROLE_OWNER",
    "ROLE_VIEWER",
    "ROLE_NONE",
    "READ",
    "WRITE",
    "SHARE",
    "DELETE",
    "ACTIONS",
    "role_for",
    "can",
    "authorize",
]
