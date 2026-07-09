"""
Shared transport runner for the MCP tool servers (#819).

Noesis is consumed through its MCP servers, so they need to be reachable from
other projects — not just spawnable over stdio. Every ``tools/*_mcp/server.py``
ends by calling :func:`run_server`, which keeps **stdio as the default** and
adds an opt-in Streamable HTTP transport behind env vars (the pattern the
retired ``noesis_mcp`` server established):

* ``NOESIS_MCP_TRANSPORT`` — ``stdio`` (default) or ``http``.
* ``NOESIS_MCP_HTTP_HOST`` — bind host, default ``127.0.0.1`` (localhost-only
  unless the operator deliberately widens it).
* ``NOESIS_MCP_HTTP_PORT`` — bind port, default ``8100``. Each server gets its
  own port; there is no shared default topology.
* ``NOESIS_MCP_AUTH_TOKEN`` — when set, every HTTP request must present the
  token as a Bearer credential. **Fail-closed**: if the installed fastmcp
  version offers no supported token-verification API, startup *raises* rather
  than serving unauthenticated — an operator who asked for auth never silently
  gets an open server. Unset means open, for the localhost-only default.

Import-safe: no fastmcp import at module load (the auth provider is resolved
lazily, only when a token is configured), so this module follows the tool
servers' stdlib-only-at-import discipline.
"""

from __future__ import annotations

import os
from typing import Any, Optional

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8100

TRANSPORT_ENV = "NOESIS_MCP_TRANSPORT"
HOST_ENV = "NOESIS_MCP_HTTP_HOST"
PORT_ENV = "NOESIS_MCP_HTTP_PORT"
TOKEN_ENV = "NOESIS_MCP_AUTH_TOKEN"


class TransportConfigError(RuntimeError):
    """The transport env configuration cannot be served safely."""


def resolve_transport() -> dict:
    """Read the transport configuration from the environment."""
    transport = (os.getenv(TRANSPORT_ENV, "stdio") or "stdio").strip().lower()
    if transport not in ("stdio", "http"):
        raise TransportConfigError(
            f"{TRANSPORT_ENV}={transport!r} is not supported (use 'stdio' or 'http')"
        )
    cfg: dict = {"transport": transport}
    if transport == "http":
        cfg["host"] = os.getenv(HOST_ENV, DEFAULT_HTTP_HOST).strip() or DEFAULT_HTTP_HOST
        raw_port = os.getenv(PORT_ENV, str(DEFAULT_HTTP_PORT)).strip()
        try:
            cfg["port"] = int(raw_port)
        except ValueError:
            raise TransportConfigError(f"{PORT_ENV}={raw_port!r} is not a valid port")
        cfg["token"] = os.getenv(TOKEN_ENV, "").strip() or None
    return cfg


def _build_token_verifier(token: str) -> Any:
    """A fastmcp static-token verifier for ``token``.

    Resolved lazily against the import paths fastmcp has shipped it under.
    Raises :class:`TransportConfigError` when none is available — the caller
    must NOT fall back to serving without auth.
    """
    last_error: Optional[Exception] = None
    for path in (
        "fastmcp.server.auth",
        "fastmcp.server.auth.providers.jwt",
        "fastmcp.server.auth.verifiers",
    ):
        try:
            module = __import__(path, fromlist=["StaticTokenVerifier"])
            verifier_cls = getattr(module, "StaticTokenVerifier", None)
            if verifier_cls is not None:
                return verifier_cls(
                    tokens={token: {"client_id": "noesis-operator", "scopes": []}}
                )
        except Exception as exc:  # noqa: BLE001 - try the next known location
            last_error = exc
    raise TransportConfigError(
        f"{TOKEN_ENV} is set but the installed fastmcp exposes no supported "
        "StaticTokenVerifier; refusing to serve HTTP without the requested auth. "
        f"(last import error: {last_error})"
    )


def run_server(mcp: Any) -> None:
    """Run a FastMCP server on the configured transport.

    stdio (the default) behaves exactly as before. ``http`` binds the
    configured host/port; with ``NOESIS_MCP_AUTH_TOKEN`` set, a static Bearer
    token verifier is attached first, and startup fails if it cannot be.
    """
    cfg = resolve_transport()
    if cfg["transport"] == "stdio":
        mcp.run()
        return

    if cfg["token"]:
        verifier = _build_token_verifier(cfg["token"])
        # FastMCP reads server auth from its `auth` attribute (constructor
        # arg); the servers construct `mcp` at import, so attach before run.
        mcp.auth = verifier

    mcp.run(transport="http", host=cfg["host"], port=cfg["port"])
