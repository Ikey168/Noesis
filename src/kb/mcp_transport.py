"""Persistent SDK MCP transport for explicitly configured research sources.

One owned event loop/session per client; deadline cancellation never creates a
replacement browser or leaks credentials into captured evidence.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import json
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

from src.kb.federation import FederationError

GITHUB_TOOLS = frozenset({"issue_read", "pull_request_read"})
BROWSER_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_wait_for",
        "browser_take_screenshot",
        "browser_close",
    }
)


class MCPHTTPClient:
    """Managed streamable-HTTP MCP session, compatible with RemoteMCPAdapter.

    GitHub is restricted to its official remote service or an explicit loopback
    server. Browser servers must be loopback and separately configured with the
    supplied network guard. No arbitrary stdio commands are accepted here.
    """

    supports_cancellation = True

    def __init__(
        self, endpoint, *, kind, token=None, timeout_s=15, max_bytes=4_000_000
    ):
        parsed = urlsplit(endpoint)
        try:
            loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        official = (
            kind == "github"
            and parsed.scheme == "https"
            and parsed.hostname == "api.githubcopilot.com"
            and parsed.port in (None, 443)
        )
        if (
            kind not in {"github", "browser"}
            or not (official or loopback and parsed.scheme in {"http", "https"})
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "explicit official GitHub or loopback MCP endpoint required"
            )
        if (
            type(timeout_s) not in {int, float}
            or not 0.1 <= timeout_s <= 60
            or type(max_bytes) is not int
            or not 1024 <= max_bytes <= 16_000_000
        ):
            raise ValueError("invalid MCP timeout/byte budget")
        if official and not token:
            raise ValueError(
                "official GitHub MCP requires an explicitly supplied credential"
            )
        self.endpoint, self.kind, self.timeout, self.max_bytes = (
            endpoint,
            kind,
            timeout_s,
            max_bytes,
        )
        self.allowed_tools = GITHUB_TOOLS if kind == "github" else BROWSER_TOOLS
        self._token = token
        self._ready = threading.Event()
        self._closed, self._error, self._session, self._loop, self._stop = (
            False,
            None,
            None,
            None,
            None,
        )
        self._schemas = {}
        self.version = "uninitialized"
        self._thread = threading.Thread(
            target=self._thread_main, name="noesis-mcp-" + kind, daemon=True
        )
        self._thread.start()
        if not self._ready.wait(timeout_s + 1) or self._error:
            self.close()
            raise FederationError(
                "mcp_connect_failed",
                "MCP initialization failed; no credentials or raw server errors retained",
            )

    def _thread_main(self):
        try:
            asyncio.run(self._serve())
        except BaseException:  # noqa: BLE001 - report thread cancellation/startup failures without credential-bearing tracebacks
            self._error = "mcp_session_failed"
            self._ready.set()

    async def _serve(self):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        headers = {}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        if self.kind == "github":
            headers.update(
                {
                    "X-MCP-Readonly": "true",
                    "X-MCP-Tools": ",".join(sorted(self.allowed_tools)),
                }
            )
        limit = self.max_bytes

        class LimitedStream(httpx.AsyncByteStream):
            def __init__(self, stream):
                self.stream = stream

            async def __aiter__(self):
                total = 0
                async for chunk in self.stream:
                    total += len(chunk)
                    if total > limit:
                        raise FederationError(
                            "result_too_large", "MCP transport exceeded byte budget"
                        )
                    yield chunk

            async def aclose(self):
                await self.stream.aclose()

        class LimitedTransport(httpx.AsyncBaseTransport):
            def __init__(self):
                self.inner = httpx.AsyncHTTPTransport(retries=0)

            async def handle_async_request(self, request):
                response = await self.inner.handle_async_request(request)
                response.stream = LimitedStream(response.stream)
                return response

            async def aclose(self):
                await self.inner.aclose()

        def factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                auth=auth,
                transport=LimitedTransport(),
                follow_redirects=False,
                trust_env=False,
            )

        async with (
            streamablehttp_client(
                self.endpoint,
                headers=headers,
                timeout=self.timeout,
                sse_read_timeout=self.timeout,
                httpx_client_factory=factory,
            ) as (reader, writer, _),
            ClientSession(
                reader, writer, read_timeout_seconds=timedelta(seconds=self.timeout)
            ) as session,
        ):
            initialized = await session.initialize()
            self.version = initialized.serverInfo.version
            self._session = session
            self._ready.set()
            await self._stop.wait()

    def _submit(self, operation, *, cancelled=None):
        if self._closed or self._error or self._session is None:
            operation.close()
            raise FederationError(
                "mcp_session_unavailable", "MCP client is closed or disconnected"
            )
        future = asyncio.run_coroutine_threadsafe(operation, self._loop)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                if cancelled and cancelled():
                    raise FederationError("cancelled", "MCP request cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise FederationError(
                        "source_timeout", "MCP request deadline exceeded"
                    )
                try:
                    return future.result(timeout=min(0.05, remaining))
                except concurrent.futures.TimeoutError:
                    continue
        except FederationError:
            future.cancel()
            raise
        except Exception:  # noqa: BLE001 - sanitize unknown remote SDK exceptions
            future.cancel()
            raise FederationError(
                "remote_call_failed",
                "MCP call failed; inspect server-side diagnostics without exposing credentials",
            ) from None

    def _bounded(self, value):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(raw.encode()) > self.max_bytes:
            raise FederationError(
                "result_too_large", "MCP output exceeds evidence budget"
            )
        if self._token and self._token in raw:
            raise FederationError(
                "credential_echo", "MCP response contains credential material"
            )
        return value

    async def _tools(self):
        values, cursor, seen = [], None, set()
        for _ in range(10):
            page = await self._session.list_tools(cursor=cursor)
            for tool in page.tools:
                if tool.name in self.allowed_tools:
                    self._schemas[tool.name] = tool.inputSchema
                    values.append(tool.model_dump(mode="json"))
            cursor = page.nextCursor
            if not cursor:
                return self._bounded(values)
            if cursor in seen:
                break
            seen.add(cursor)
        raise FederationError(
            "pagination_limit", "MCP capability pagination failed to terminate"
        )

    def list_tools(self):
        return self._submit(self._tools())

    def list_resources(self):
        # These integrations deliberately expose no arbitrary MCP resources,
        # prompts, roots, sampling or elicitation control surfaces.
        return []

    def call_tool(self, name, arguments, *, cancelled=None):
        if name not in self.allowed_tools:
            raise FederationError(
                "remote_operation_forbidden", "MCP tool is not allowlisted"
            )
        self._bounded(arguments)
        if name not in self._schemas:
            self.list_tools()
        if name not in self._schemas:
            raise FederationError("schema_drift", "required MCP tool is not advertised")
        from jsonschema import Draft202012Validator

        Draft202012Validator(self._schemas[name]).validate(arguments)

        async def call():
            result = await self._session.call_tool(name, arguments)
            if result.isError:
                raise FederationError(
                    "remote_tool_error",
                    "MCP tool returned an error, not source evidence",
                )
            return self._bounded(result.model_dump(mode="json", exclude_none=True))

        return self._submit(call(), cancelled=cancelled)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if (
            self._loop is not None
            and self._stop is not None
            and self._loop.is_running()
        ):
            self._loop.call_soon_threadsafe(self._stop.set)
        if threading.current_thread() != self._thread:
            self._thread.join(timeout=self.timeout + 1)
        self._token = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
