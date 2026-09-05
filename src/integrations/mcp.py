"""Official MCP clients adapted to Noesis's existing synchronous federation."""

import asyncio
import json
import os
from contextlib import ExitStack, suppress
from datetime import timedelta
from threading import RLock
from urllib.parse import urlsplit

from .common import IntegrationError, finite

PRESETS = {
    "github": {
        "endpoint": "https://api.githubcopilot.com/mcp/",
        "secret_env": "NOESIS_GITHUB_MCP_TOKEN",
        "tools": [
            "get_file_contents",
            "issue_read",
            "pull_request_read",
            "search_code",
            "search_issues",
        ],
    },
    "context7": {
        "endpoint": "https://mcp.context7.com/mcp",
        "secret_env": "NOESIS_CONTEXT7_API_KEY",
        "tools": ["resolve-library-id", "query-docs"],
    },
    "playwright": {
        "endpoint": None,
        "secret_env": None,
        "tools": ["browser_navigate", "browser_snapshot", "browser_wait_for"],
    },
}


class StreamableMCPClient:
    def __init__(
        self,
        endpoint,
        *,
        headers=None,
        timeout_seconds=10,
        max_bytes=1_000_000,
        navigation_origins=None,
    ):
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.timeout = finite(timeout_seconds, "MCP timeout", 0.1, 60)
        if type(max_bytes) is not int or not 1 <= max_bytes <= 10_000_000:
            raise ValueError("MCP result byte limit must be between 1 and 10000000")
        self.navigation_origins = navigation_origins
        self.max_bytes = max_bytes
        self.version = "not-yet-initialized"
        self._lock = RLock()
        self._stack = self._portal = self._session = None
        self._closed = False

    def _connect(self):
        import httpx
        from anyio.from_thread import start_blocking_portal
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        if self._closed:
            raise IntegrationError("client_closed", "MCP session has been closed")
        if self._stack is not None:
            return
        stack = ExitStack()
        try:
            portal = stack.enter_context(start_blocking_portal())
            http = stack.enter_context(
                portal.wrap_async_context_manager(
                    httpx.AsyncClient(
                        headers=self.headers,
                        timeout=self.timeout,
                        follow_redirects=False,
                    )
                )
            )
            read, write, _ = stack.enter_context(
                portal.wrap_async_context_manager(
                    streamable_http_client(self.endpoint, http_client=http)
                )
            )
            session = stack.enter_context(
                portal.wrap_async_context_manager(
                    ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=self.timeout),
                    )
                )
            )
            self._portal, self._session = portal, session
            initialized = portal.call(self._invoke, "initialize")
            self.version = initialized.serverInfo.version
            self._stack = stack
        except BaseException:
            with suppress(Exception):
                stack.close()
            self._portal = self._session = None
            raise

    async def _invoke(self, method, *args):
        async with asyncio.timeout(self.timeout):
            return await getattr(self._session, method)(*args)

    def _sync(self, method, *args):
        if not self._lock.acquire(timeout=self.timeout):
            raise IntegrationError("session_busy", "MCP session is busy")
        try:
            self._connect()
            try:
                result = self._portal.call(self._invoke, method, *args)
            except Exception as exc:
                if (
                    method == "list_resources"
                    and getattr(getattr(exc, "error", None), "code", None) == -32601
                ):
                    return []
                raise
            data = result.model_dump(mode="json")
            if len(json.dumps(data).encode()) > self.max_bytes:
                raise IntegrationError(
                    "result_limit", "MCP result exceeds output budget"
                )
            if data.get("isError"):
                raise IntegrationError(
                    "remote_tool_error", "Remote MCP tool returned an error"
                )
            return data
        except IntegrationError:
            raise
        except Exception:  # noqa: BLE001 - normalize optional transport failures
            with suppress(Exception):
                self._disconnect()
            raise IntegrationError(
                "remote_unavailable",
                "MCP request failed; session reset after transport error or timeout",
            ) from None
        finally:
            self._lock.release()

    def _disconnect(self):
        stack, self._stack = self._stack, None
        try:
            if stack is not None:
                stack.close()
        finally:
            self._portal = self._session = None

    def close(self):
        """Release the server session, HTTP connections and portal thread."""
        with self._lock:
            try:
                self._disconnect()
            finally:
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def list_tools(self):
        return self._sync("list_tools")

    def list_resources(self):
        return self._sync("list_resources")

    def read_resource(self, uri):
        return self._sync("read_resource", uri)

    def call_tool(self, name, arguments):
        if len(json.dumps(arguments).encode()) > self.max_bytes:
            raise IntegrationError("input_limit", "MCP arguments exceed byte budget")
        if self.navigation_origins is not None:
            if name not in PRESETS["playwright"]["tools"]:
                raise IntegrationError(
                    "action_forbidden", "Browser action is not allowed"
                )
            if name == "browser_navigate":
                target = urlsplit(str(arguments.get("url") or ""))
                if (
                    target.scheme not in {"http", "https"}
                    or target.username
                    or target.password
                    or (target.scheme + "://" + target.netloc)
                    not in self.navigation_origins
                ):
                    raise IntegrationError(
                        "navigation_forbidden",
                        "Browser navigation origin is not allowed",
                    )
            if name == "browser_wait_for" and "time" in arguments:
                finite(arguments["time"], "Browser wait", 0, min(10, self.timeout))
        return self._sync("call_tool", name, arguments)


def federation_adapter(
    name, *, endpoint=None, secret_resolver=None, navigation_origins=()
):
    from src.kb.federation import RemoteMCPAdapter

    if name not in PRESETS:
        raise IntegrationError("unknown_preset", "Unknown MCP preset")
    preset = PRESETS[name]
    target = endpoint or preset["endpoint"]
    headers = {}
    if name == "playwright":
        parts = urlsplit(target or "")
        if parts.scheme != "http" or parts.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise IntegrationError(
                "invalid_endpoint",
                "Playwright preset requires an operator-started loopback HTTP server",
            )
        if not navigation_origins:
            raise IntegrationError(
                "navigation_policy_required",
                "Declare allowed browser navigation origins",
            )
        for origin in navigation_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise IntegrationError(
                    "invalid_navigation_policy",
                    "Navigation origins must be HTTP(S) origins without paths",
                )
    elif target != preset["endpoint"]:
        raise IntegrationError(
            "invalid_endpoint", "Hosted MCP preset endpoint is fixed"
        )
    resolver = secret_resolver or os.environ.get
    secret = resolver(preset["secret_env"]) if preset["secret_env"] else None
    if name == "github" and not secret:
        raise IntegrationError(
            "credential_unavailable", "Configure NOESIS_GITHUB_MCP_TOKEN"
        )
    if secret:
        headers["CONTEXT7_API_KEY" if name == "context7" else "Authorization"] = (
            secret if name == "context7" else "Bearer " + secret
        )
    if name == "github":
        headers["X-MCP-Readonly"] = "true"
    return RemoteMCPAdapter(
        name + "-mcp",
        StreamableMCPClient(
            target,
            headers=headers,
            navigation_origins=frozenset(navigation_origins)
            if name == "playwright"
            else None,
        ),
        tools=preset["tools"],
        limits={"max_results": 100, "timeout_ms": 30000, "max_bytes": 1_000_000},
    )
