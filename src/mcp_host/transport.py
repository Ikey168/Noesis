"""
Shared transport runner for the MCP tool servers (#819).

Noesis is consumed through its MCP servers, so they need to be reachable from
other projects — not just spawnable over stdio. Every ``tools/*_mcp/server.py``
ends by calling :func:`run_server`, which keeps **stdio as the default** and
adds an opt-in Streamable HTTP transport behind env vars (the pattern the
the first HTTP-capable server established):

* ``NOESIS_MCP_TRANSPORT`` — ``stdio`` (default) or ``http``.
* ``NOESIS_MCP_HTTP_HOST`` — bind host, default ``127.0.0.1`` (localhost-only
  unless the operator deliberately widens it).
* ``NOESIS_MCP_HTTP_PORT`` — bind port, default ``8100``. The supported
  ``noesis serve`` path uses this for the single curated gateway endpoint;
  specialist servers may still bind separate ports when explicitly deployed.
* ``NOESIS_MCP_AUTH_TOKEN`` — when set, every HTTP request must present the
  token as a Bearer credential. **Fail-closed**: if the installed fastmcp
  version offers no supported token-verification API, startup *raises* rather
  than serving unauthenticated — an operator who asked for auth never silently
  gets an open server. Unset means open, for the localhost-only default.
* ``NOESIS_MCP_AUTH_TOKENS_FILE`` — optional private JSON file mapping opaque
  bearer tokens to distinct ``client_id`` and ``scopes`` values. Mutually
  exclusive with the single legacy token. Intake tools use these authenticated
  caller identities on HTTP and deny HTTP requests without one.

Import-safe: no fastmcp import at module load (the auth provider is resolved
lazily, only when a token is configured), so this module follows the tool
servers' stdlib-only-at-import discipline.
"""

from __future__ import annotations

from typing import Any

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8100

TRANSPORT_ENV = "NOESIS_MCP_TRANSPORT"
HOST_ENV = "NOESIS_MCP_HTTP_HOST"
PORT_ENV = "NOESIS_MCP_HTTP_PORT"
TOKEN_ENV = "NOESIS_MCP_AUTH_TOKEN"
TOKENS_FILE_ENV = "NOESIS_MCP_AUTH_TOKENS_FILE"


class TransportConfigError(RuntimeError):
    """The transport env configuration cannot be served safely."""


def resolve_transport() -> dict:
    """Read the transport configuration from the environment."""
    from src.config.env import resolve_env

    transport = (resolve_env("MCP_TRANSPORT", "stdio") or "stdio").strip().lower()
    if transport not in ("stdio", "http"):
        raise TransportConfigError(
            f"{TRANSPORT_ENV}={transport!r} is not supported (use 'stdio' or 'http')"
        )
    cfg: dict = {"transport": transport}
    if transport == "http":
        cfg["host"] = (
            resolve_env("MCP_HTTP_HOST", DEFAULT_HTTP_HOST) or DEFAULT_HTTP_HOST
        ).strip()
        raw_port = (resolve_env("MCP_HTTP_PORT", str(DEFAULT_HTTP_PORT)) or "").strip()
        try:
            cfg["port"] = int(raw_port)
        except ValueError:
            raise TransportConfigError(f"{PORT_ENV}={raw_port!r} is not a valid port")
        cfg["token"] = (resolve_env("MCP_AUTH_TOKEN", "") or "").strip() or None
        cfg["tokens_file"] = (
            resolve_env("MCP_AUTH_TOKENS_FILE", "") or ""
        ).strip() or None
        if cfg["token"] and cfg["tokens_file"]:
            raise TransportConfigError(
                f"set only one of {TOKEN_ENV} and {TOKENS_FILE_ENV}"
            )
    return cfg


def _build_token_verifier(tokens: dict[str, dict[str, Any]]) -> Any:
    """A FastMCP static-token verifier for the configured caller map.

    Resolved lazily against the import paths fastmcp has shipped it under.
    Raises :class:`TransportConfigError` when none is available — the caller
    must NOT fall back to serving without auth.
    """
    for path in (
        "fastmcp.server.auth",
        "fastmcp.server.auth.providers.jwt",
        "fastmcp.server.auth.verifiers",
    ):
        try:
            module = __import__(path, fromlist=["StaticTokenVerifier"])
            verifier_cls = getattr(module, "StaticTokenVerifier", None)
            if verifier_cls is not None:
                return verifier_cls(tokens=tokens)
        except Exception:  # noqa: BLE001, S112 - try the next known location without logging credentials
            continue
    raise TransportConfigError(
        "the installed fastmcp exposes no supported StaticTokenVerifier; "
        "refusing to serve HTTP without the requested auth"
    )


def _load_token_map(path: str) -> dict[str, dict[str, Any]]:
    """Load explicit caller identities without logging credential material."""
    import json
    import stat
    from pathlib import Path

    source = Path(path)
    try:
        metadata = source.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise ValueError("token file must be private and regular")
        raw = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not 1 <= len(raw) <= 1000:
            raise ValueError("token map must contain one to 1000 callers")
        clients = set()
        for token, identity in raw.items():
            if (
                not isinstance(token, str)
                or len(token) < 32
                or not isinstance(identity, dict)
                or set(identity) != {"client_id", "scopes"}
            ):
                raise ValueError("invalid token identity")
            client_id, scopes = identity["client_id"], identity["scopes"]
            if (
                not isinstance(client_id, str)
                or not client_id.strip()
                or client_id in clients
                or not isinstance(scopes, list)
                or not scopes
                or any(not isinstance(scope, str) or not scope for scope in scopes)
            ):
                raise ValueError("invalid caller identity or scopes")
            clients.add(client_id)
        return raw
    except (OSError, ValueError) as exc:
        raise TransportConfigError(
            f"{TOKENS_FILE_ENV} cannot be loaded as a private caller map"
        ) from exc


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

    tokens = None
    if cfg["token"]:
        tokens = {cfg["token"]: {"client_id": "noesis-operator", "scopes": []}}
    elif cfg["tokens_file"]:
        tokens = _load_token_map(cfg["tokens_file"])
    if tokens:
        verifier = _build_token_verifier(tokens)
        # FastMCP reads server auth from its `auth` attribute (constructor
        # arg); the servers construct `mcp` at import, so attach before run.
        mcp.auth = verifier

    mcp.run(transport="http", host=cfg["host"], port=cfg["port"])
