"""Version-neutral pieces of the official MCP client SDK (MC03, #2758).

``src/integrations/mcp.py`` and ``src/kb/mcp_transport.py`` open Streamable
HTTP sessions through these helpers so they run on the pinned ``mcp`` 1.x line
and on ``mcp`` 2.x. Every import is lazy: the SDK stays an optional extra.

What differs between the majors, and how it is handled:

* ``mcp.client.streamable_http.streamablehttp_client`` was removed in mcp 2;
  ``streamable_http_client(url, http_client=...)`` exists from mcp 1.24 on, so
  the clients call it unconditionally (no fallback to the old name).
* mcp 2 drives ``httpx2`` instead of ``httpx``; :func:`streamable_http` returns
  the HTTP module the installed SDK uses so callers build a compatible client.
* ``streamable_http_client`` yields ``(read, write, get_session_id)`` on 1.x and
  ``(read, write)`` on 2.x; :func:`session_streams` takes the first two.
* ``ClientSession(read_timeout_seconds=...)`` takes a ``timedelta`` on 1.x and
  float seconds on 2.x; :func:`read_timeout` returns the accepted form.
* mcp 2 result models use snake_case attributes (``server_info``,
  ``next_cursor``, ``input_schema``, ``is_error``) with camelCase aliases;
  :func:`field` reads either spelling and :func:`wire` dumps the camelCase
  wire keys (``inputSchema``, ``isError``, ``structuredContent``) that the
  federation adapters and evidence decoders read on both majors.
* ``ClientSession.list_tools(cursor)`` lost its positional cursor in mcp 2;
  :func:`page_params` builds the ``params=`` form both majors accept.

mcp 2 behaviour reviewed against both clients (no change needed):

* Redirects: mcp 2 follows a redirect itself only when it stays on the
  endpoint's origin and keeps the method (307/308 for POST), whatever the
  client's ``follow_redirects`` says; cross-origin redirects still fail. Both
  clients pin their endpoint (official GitHub/Context7 host or loopback) and
  keep ``follow_redirects=False`` for 1.x, so credentials never leave the
  configured origin on either major; byte budgets still apply because the SDK
  sends through the supplied client's transport.
* Session expiry: a 404 on an established session surfaces as a JSON-RPC
  ``Session terminated`` error on both majors; the SDK does not re-initialise.
  ``StreamableMCPClient`` already drops the session on any failure and
  reconnects on the next call; ``MCPHTTPClient`` reports
  ``remote_call_failed`` and the caller builds a new client, as before.
* OAuth issuer checks (RFC 9207 / metadata issuer validation) apply only to the
  SDK's OAuth providers. Both clients send an operator-supplied static bearer
  or API-key header and never run an OAuth flow, so nothing changes.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from typing import Any


def streamable_http() -> tuple[Any, Any]:
    """Return ``(streamable_http_client, http_module)`` for the installed SDK."""
    from mcp.client import streamable_http as sdk

    http = getattr(sdk, "httpx2", None)
    if http is None:
        import httpx as http
    return sdk.streamable_http_client, http


def session_streams(streams: tuple[Any, ...]) -> tuple[Any, Any]:
    """``(read, write)`` from the 3-tuple (mcp 1.x) or 2-tuple (mcp 2.x)."""
    return streams[0], streams[1]


def read_timeout(seconds: float) -> Any:
    """``ClientSession`` read timeout in the type the installed SDK accepts."""
    from mcp import ClientSession

    parameter = inspect.signature(ClientSession.__init__).parameters.get(
        "read_timeout_seconds"
    )
    if parameter is not None and "timedelta" in str(parameter.annotation):
        return timedelta(seconds=seconds)
    return float(seconds)


def field(model: Any, wire_name: str) -> Any:
    """Read a result attribute by its camelCase wire name on either major."""
    if hasattr(model, wire_name):
        return getattr(model, wire_name)
    from pydantic.alias_generators import to_snake

    return getattr(model, to_snake(wire_name))


def wire(model: Any, **kwargs: Any) -> Any:
    """``model_dump(mode="json")`` keyed by MCP wire (camelCase) names."""
    if getattr(model, "model_config", {}).get("alias_generator") is not None:
        kwargs.setdefault("by_alias", True)
    return model.model_dump(mode="json", **kwargs)


def page_params(cursor: str | None) -> Any:
    """``params=`` for a paginated list request (``None`` for the first page)."""
    if not cursor:
        return None
    from mcp.types import PaginatedRequestParams

    return PaginatedRequestParams(cursor=cursor)
