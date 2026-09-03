"""Canonical JSON and SHA-256 helpers for evidence bundles.

``noesis-json-c14n-v1`` is deliberately small and implementable with the
Python standard library.  It is not advertised as RFC 8785: numbers retain
Python's JSON rendering.  The contract accepts JSON values only, rejects
NaN/infinity, sorts object keys, emits UTF-8 directly, and removes insignificant
whitespace.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CANONICALIZATION = "noesis-json-c14n-v1"
HASH_ALGORITHM = "sha256"


def canonical_bytes(value: Any) -> bytes:
    """Return the canonical UTF-8 representation of a JSON value."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_digest(value: Any) -> str:
    """Return a contract-formatted digest of a JSON value."""
    return f"sha256:{hashlib.sha256(canonical_bytes(value)).hexdigest()}"


def sha256_bytes(value: bytes) -> str:
    """Return a contract-formatted digest of arbitrary bytes."""
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading untrusted adjacent content into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"
