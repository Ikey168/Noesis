"""Safe, metered HTTP for bulk downloads (FA01).

Same rules as the scholarly connectors: HTTPS only, the host must be on the
adapter's allowlist, and it must resolve to a public address. Every request goes
through the shared free-tier ledger (``src.ingestion.quota``); a provider 429
blocks the host. Downloads resume from a ``.part`` file with an HTTP Range
request when the server supports it, stop at a byte cap, and return SHA-256 and
MD5 digests so the runner can compare them with published checksums.
"""
from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from src.ingestion import quota
from src.ingestion.connectors.base import PermanentFetchError
from src.ingestion.connectors.scholarly.base import _assert_public_https

TIMEOUT_S = 120
CHUNK = 1024 * 1024
TEXT_LIMIT = 50 * 1024 * 1024


def _user_agent() -> str:
    contact = os.getenv("NOESIS_SCHOLARLY_CONTACT") or os.getenv("NOESIS_RESEARCH_CONTACT") or "noesis@example.org"
    return f"noesis-bulk/1.0 (mailto:{contact})"


class BulkHttp:
    """HTTP client bound to an allowlist. ``opener(request, timeout)`` and the resolver are injectable."""

    def __init__(self, allowed_hosts: Sequence[str], resolver: Optional[Callable[[str], List[str]]] = None,
                 opener: Optional[Callable[..., Any]] = None, headers: Optional[Mapping[str, str]] = None):
        self.allowed = tuple(h.lower() for h in allowed_hosts)
        self._resolver = resolver
        self._open = opener or (lambda request, timeout: urllib.request.urlopen(request, timeout=timeout))
        self.headers = {"User-Agent": _user_agent(), **dict(headers or {})}

    # -- guards ----------------------------------------------------------------- #
    def check(self, url: str) -> str:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        match = next((a for a in self.allowed if host == a or host.endswith("." + a)), None)
        if match is None:
            raise PermanentFetchError(f"host {host!r} is not allowlisted for this bulk source")
        _assert_public_https(url, match, self._resolver)
        return host

    def _request(self, url: str, method: str = "GET", extra: Optional[Mapping[str, str]] = None):
        self.check(url)
        quota.acquire(url)
        request = urllib.request.Request(url, method=method, headers={**self.headers, **dict(extra or {})})
        try:
            return self._open(request, TIMEOUT_S)
        except urllib.error.HTTPError as exc:
            quota.note_response(url, exc.code, dict(exc.headers or {}))
            raise

    # -- small requests --------------------------------------------------------- #
    def head(self, url: str) -> Dict[str, str]:
        with self._request(url, "HEAD") as response:
            return {k.lower(): v for k, v in dict(response.headers).items()}

    def get_text(self, url: str, limit: int = TEXT_LIMIT, encoding: str = "utf-8") -> str:
        return self.get_bytes(url, limit).decode(encoding, "replace")

    def get_bytes(self, url: str, limit: int = TEXT_LIMIT) -> bytes:
        with self._request(url) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise PermanentFetchError(f"{url} exceeds {limit} bytes")
        quota.note_response(url, 200, None, len(data))
        return data

    def open_stream(self, url: str) -> BinaryIO:
        """Open a response for streaming; the caller closes it."""
        return self._request(url)

    # -- downloads -------------------------------------------------------------- #
    def download(self, url: str, dest: Path, max_bytes: int) -> Tuple[int, Dict[str, str]]:
        """Download to ``dest`` (resuming ``dest.part``); returns (bytes, {"sha256", "md5"})."""
        dest = Path(dest)
        part = dest.with_name(dest.name + ".part")
        part.parent.mkdir(parents=True, exist_ok=True)
        offset = part.stat().st_size if part.exists() else 0
        extra = {"Range": f"bytes={offset}-"} if offset else {}
        response = self._request(url, extra=extra)
        with response:
            status = getattr(response, "status", 200)
            if offset and status != 206:      # server ignored the range: start over
                offset = 0
            mode = "ab" if offset else "wb"
            written = offset
            with open(part, mode) as out:
                while True:
                    chunk = response.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise PermanentFetchError(f"{url} exceeds the byte budget ({max_bytes})")
                    out.write(chunk)
        quota.note_response(url, 200, None, written - (offset if mode == "ab" else 0))
        digests = _digests(part)
        part.replace(dest)
        return written, digests


def _digests(path: Path) -> Dict[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5()  # noqa: S324 - MD5 only to match published checksums
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            sha.update(chunk)
            md5.update(chunk)
    return {"sha256": sha.hexdigest(), "md5": md5.hexdigest()}
