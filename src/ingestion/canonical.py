"""
URL canonicalization and content hashing for ingest-time dedup (#895).

The news path dedups by *exact* URL, so tracking-param and fragment variants of
the same page look like new documents, and the same body syndicated under
different URLs is only merged downstream. These pure helpers give the document
sink (#894) two stable dedup keys:

- :func:`canonicalize_url` — a URL stripped of tracking noise and normalized,
  so ``…/story?utm_source=x#top`` and ``…/story`` collapse.
- :func:`content_hash` — a SHA-256 over normalized text, so the same article
  under two URLs hashes identically (URL-independent near-dup collapse).

Dependency-free (stdlib only), so it is import-safe in the CI gate.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never identify content — always dropped.
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "gclsrc", "dclid", "msclkid", "yclid", "mc_cid",
    "mc_eid", "igshid", "ref", "ref_src", "ref_url", "cmpid", "cid", "spm",
    "_hsenc", "_hsmi", "vero_id", "wt_mc", "s_kwcid", "ncid", "at_medium",
    "at_campaign",
})
# Prefixes whose whole family is tracking (utm_source, utm_medium, …).
_TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "matomo_", "hsa_")

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def canonicalize_url(url: str) -> str:
    """Return a canonical form of ``url`` for dedup.

    Lowercases scheme/host, drops the default port, removes tracking query
    params and the fragment, sorts the remaining query keys, and trims a
    trailing slash on non-root paths. Returns the input unchanged if it does
    not parse as an absolute URL.
    """
    if not url:
        return url
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url

    scheme = parts.scheme.lower()

    host = parts.hostname or ""
    host = host.lower()
    port = parts.port
    netloc = host
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"
    if parts.username:
        cred = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{cred}@{netloc}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(k)
        and not (host in {"theguardian.com", "www.theguardian.com"} and k.casefold() == "cmp")
    ]
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))  # fragment dropped


def _is_tracking(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_PARAMS or any(k.startswith(p) for p in _TRACKING_PREFIXES)


def content_hash(text: str) -> str:
    """Stable SHA-256 over normalized text for URL-independent dedup.

    Normalization lowercases and collapses all whitespace so trivial
    formatting differences (re-wrapping, extra spaces) do not defeat the
    duplicate check. Empty/whitespace-only input hashes the empty string.
    """
    normalized = re.sub(r"\s+", " ", (text or "")).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
