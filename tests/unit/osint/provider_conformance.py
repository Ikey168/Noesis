"""Reverse-image-search adapter conformance harness (OX11, #2051).

Importable by any deployment that wants to wire a ``ReverseSearchProvider``
into ``tools/osint_mcp/server.py`` (see "Deploying a reverse-image provider" in
``docs/security/osint-review-gate.md``). No provider ships with Noesis; this
harness is the bar an operator-supplied adapter must clear first.

An adapter is supplied as a *factory* ``factory(transport, corpus_conn)`` that
returns the callable provider. The harness injects a fake transport, so no
network is used, and a spy corpus connection, so any write is caught.

An adapter conforms when it:

1. accepts image bytes only: exactly one required parameter, and no parameter
   for a URL, query, person, face or name;
2. returns a list of hits, each with ``url``, ``provider`` and ``seen_at``, and
   no identity fields (a person's name, face, profile, account, e-mail ...);
3. declares ``max_bytes`` and ``timeout_s`` (at most 60 s), refuses an image
   above its byte cap without calling the transport, and passes its timeout to
   the transport;
4. raises on a transport failure rather than retrying (exactly one call);
5. never writes to the corpus connection.

Adapters that return person identities or accept face queries fail
conformance and must not be wired: person identification is a permanent
non-goal (``docs/security/osint-abuse-analysis.md``).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Dict, List

REQUIRED_HIT_FIELDS = ("url", "provider", "seen_at")
IDENTITY_FIELDS = frozenset(
    {
        "name",
        "person",
        "person_name",
        "full_name",
        "identity",
        "face",
        "faces",
        "face_id",
        "face_match",
        "profile",
        "profile_url",
        "username",
        "user",
        "handle",
        "account",
        "email",
        "phone",
        "subject",
        "celebrity",
        "people",
        "person_id",
    }
)
FORBIDDEN_PARAMETERS = frozenset(
    {
        "url",
        "image_url",
        "query",
        "q",
        "text",
        "person",
        "person_id",
        "name",
        "face",
        "face_query",
        "subject",
    }
)
_WRITE_PREFIXES = (
    "insert",
    "update",
    "delete",
    "create",
    "drop",
    "alter",
    "copy",
    "attach",
    "replace",
    "merge",
)
SAMPLE_IMAGE = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class SpyConnection:
    """A corpus-connection stand-in that records every statement."""

    def __init__(self) -> None:
        self.statements: List[str] = []

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> "SpyConnection":
        self.statements.append(str(sql))
        return self

    def fetchall(self) -> list:
        return []

    def fetchone(self) -> None:
        return None

    @property
    def writes(self) -> List[str]:
        return [
            s for s in self.statements if s.strip().lower().startswith(_WRITE_PREFIXES)
        ]


class FakeTransport:
    """Records calls; replies with ``hits`` or raises ``error``."""

    def __init__(
        self, hits: List[Dict[str, Any]] | None = None, error: Exception | None = None
    ) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.hits = (
            hits
            if hits is not None
            else [
                {
                    "url": "https://elsewhere.example/story",
                    "seen_at": "2026-01-02T03:04:05Z",
                }
            ]
        )
        self.error = error

    def __call__(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"status": 200, "hits": list(self.hits)}


def check_adapter(
    factory: Callable[..., Callable[[bytes], List[Dict[str, Any]]]],
) -> List[str]:
    """Run every conformance check; return the violations (empty = conforms)."""
    violations: List[str] = []

    corpus = SpyConnection()
    transport = FakeTransport()
    adapter = factory(transport, corpus)

    # 1. Image bytes only.
    params = [
        p
        for p in inspect.signature(adapter).parameters.values()
        if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
    ]
    required = [p for p in params if p.default is p.empty]
    if len(required) != 1:
        violations.append(
            "adapter must take exactly one required argument: the image bytes"
        )
    forbidden = sorted({p.name.lower() for p in params} & FORBIDDEN_PARAMETERS)
    if forbidden:
        violations.append(f"adapter accepts forbidden parameters: {forbidden}")
    try:
        adapter("https://example.org/photo.jpg")  # a URL is not image bytes
        violations.append("adapter accepted a URL string instead of image bytes")
    except (TypeError, ValueError):
        pass
    transport.calls.clear()

    # 3. Declared bounds.
    max_bytes = getattr(adapter, "max_bytes", None)
    timeout_s = getattr(adapter, "timeout_s", None)
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        violations.append("adapter must declare a positive integer max_bytes")
    if not isinstance(timeout_s, (int, float)) or not 0 < timeout_s <= 60:
        violations.append("adapter must declare timeout_s in (0, 60]")

    # 2. Hit shape and no identity fields.
    try:
        hits = adapter(SAMPLE_IMAGE)
    except Exception as exc:  # noqa: BLE001
        violations.append(f"adapter failed on a valid image: {exc!r}")
        hits = []
    if not isinstance(hits, list):
        violations.append("adapter must return a list of hits")
        hits = []
    for hit in hits:
        missing = [
            k for k in REQUIRED_HIT_FIELDS if not (isinstance(hit, dict) and hit.get(k))
        ]
        if missing:
            violations.append(f"hit is missing required fields {missing}")
        leaked = sorted(IDENTITY_FIELDS & {str(k).lower() for k in (hit or {})})
        if leaked:
            violations.append(f"hit carries identity fields {leaked}")
    if transport.calls and isinstance(timeout_s, (int, float)):
        if transport.calls[0].get("timeout") != timeout_s:
            violations.append("adapter must pass its declared timeout to the transport")
    if isinstance(max_bytes, int) and max_bytes > 0:
        before = len(transport.calls)
        try:
            adapter(b"\x00" * (max_bytes + 1))
            violations.append("adapter accepted an image above its byte cap")
        except Exception:  # noqa: BLE001 - refusing is the conformant behaviour
            pass
        if len(transport.calls) != before:
            violations.append(
                "adapter called the provider with an image above its byte cap"
            )

    # 4. Raise rather than retry.
    failing = FakeTransport(error=TimeoutError("provider timed out"))
    failing_adapter = factory(failing, corpus)
    try:
        failing_adapter(SAMPLE_IMAGE)
        violations.append("adapter swallowed a transport failure instead of raising")
    except Exception:  # noqa: BLE001
        pass
    if len(failing.calls) != 1:
        violations.append(
            f"adapter retried: {len(failing.calls)} transport calls for one failure"
        )

    # 5. Never write to the corpus.
    if corpus.writes:
        violations.append(
            f"adapter wrote to the corpus connection: {corpus.writes[:3]}"
        )
    return violations


def assert_conformant(
    factory: Callable[..., Callable[[bytes], List[Dict[str, Any]]]],
) -> None:
    violations = check_adapter(factory)
    assert not violations, "reverse-image adapter is not conformant:\n- " + "\n- ".join(
        violations
    )
