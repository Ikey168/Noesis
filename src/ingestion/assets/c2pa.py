"""
C2PA content-credentials verification (Track C / C3).

Where an image carries C2PA content credentials, they are the strongest
provenance signal available — signer, edit history, capture assertion — and are
verifiable **locally**, no external calls. This module verifies them.

Two levels, so it is useful with or without the (optional, heavy) ``c2pa``
library:

* A cheap **marker scan** detects whether a C2PA/JUMBF manifest is embedded at
  all (stdlib only).
* When a verification **backend** is available (the ``c2pa`` library, injectable
  for tests), the manifest is cryptographically verified into a signer / edit
  history / validity result.

**The absence of credentials is a neutral state, never suspicion.** A stripped
image simply returns ``status = "no_credentials"``; nothing downstream may treat
that as evidence of tampering.

See ``docs/architecture/OSINT_IMAGERY_PLAN.md`` §3.1 (C3).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# JUMBF/C2PA embedding markers (the box type and the C2PA URN appear in the
# JUMBF superbox regardless of container). A cheap presence signal.
_MARKERS = (b"jumbf", b"c2pa", b"urn:uuid", b"contentauth")

STATUS_NO_CREDENTIALS = "no_credentials"
STATUS_PRESENT_UNVERIFIED = "present_unverified"
STATUS_VERIFIED = "verified"
STATUS_INVALID = "invalid"


def has_c2pa_marker(image_bytes: bytes) -> bool:
    """Cheap detection of an embedded C2PA/JUMBF manifest (no verification)."""
    if not image_bytes:
        return False
    window = image_bytes[:1_000_000].lower()  # manifests sit early; bound the scan
    hits = sum(1 for m in _MARKERS if m in window)
    # Require the JUMBF box plus a c2pa/contentauth token to avoid false hits on
    # arbitrary text like a stray "urn:uuid" in metadata.
    return b"jumbf" in window and hits >= 2


def _default_backend(image_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Verify with the c2pa library if installed, else None (unavailable)."""
    try:
        import c2pa  # lazy: optional heavy dependency
    except Exception:  # noqa: BLE001 - not installed -> unavailable
        return None
    try:
        reader = c2pa.Reader.from_bytes("image/jpeg", image_bytes)  # type: ignore[attr-defined]
        manifest_json = reader.json()
        return {"ok": True, "manifest": manifest_json}
    except Exception as exc:  # noqa: BLE001 - a present-but-invalid manifest
        return {"ok": False, "error": str(exc)}


def verify_c2pa(
    image_bytes: bytes,
    backend: Optional[Callable[[bytes], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Verify content credentials, returning a neutral, panel-ready result.

    Statuses: ``no_credentials`` (none embedded — neutral), ``present_unverified``
    (a manifest is embedded but no verification backend is available),
    ``verified`` (backend confirmed the chain), ``invalid`` (backend rejected it).
    """
    marker = has_c2pa_marker(image_bytes)
    if not marker:
        return {"status": STATUS_NO_CREDENTIALS, "note": "no content credentials embedded (neutral)"}

    backend = backend or _default_backend
    result = backend(image_bytes)
    if result is None:
        return {
            "status": STATUS_PRESENT_UNVERIFIED,
            "note": "content credentials present; verification backend (c2pa library) unavailable",
        }
    if result.get("ok"):
        return {"status": STATUS_VERIFIED, "manifest": result.get("manifest")}
    return {"status": STATUS_INVALID, "error": result.get("error")}
