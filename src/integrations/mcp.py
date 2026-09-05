"""Official MCP clients adapted to Noesis's existing synchronous federation."""

import asyncio
import json
import os
from datetime import timedelta
from urllib.parse import urlsplit

from .common import IntegrationError

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
        "tools": ["browser_navigate", "browser_snapshot"],
    },
}


class StreamableMCPClient:
    def __init__(
        self, endpoint, *, headers=None, timeout_seconds=10, max_bytes=1_000_000
    ):
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.timeout = timeout_seconds
        self.max_bytes = max_bytes
        self.version = "not-yet-initialized"

    async def _call(self, method, *args):
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with asyncio.timeout(self.timeout):
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.timeout, follow_redirects=False
            ) as http:
                async with streamable_http_client(self.endpoint, http_client=http) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=self.timeout),
                    ) as session:
                        initialized = await session.initialize()
                        self.version = initialized.serverInfo.version
                        try:
                            result = await getattr(session, method)(*args)
                        except Exception as exc:
                            if (
                                method == "list_resources"
                                and getattr(getattr(exc, "error", None), "code", None)
                                == -32601
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

    def _sync(self, method, *args):
        try:
            return asyncio.run(self._call(method, *args))
        except IntegrationError:
            raise
        except Exception:  # noqa: BLE001 - normalize optional transport failures at adapter boundary
            raise IntegrationError(
                "remote_unavailable",
                "MCP request failed; check endpoint, credentials and timeout",
            ) from None

    def list_tools(self):
        return self._sync("list_tools")

    def list_resources(self):
        return self._sync("list_resources")

    def read_resource(self, uri):
        return self._sync("read_resource", uri)

    def call_tool(self, name, arguments):
        return self._sync("call_tool", name, arguments)


def federation_adapter(name, *, endpoint=None, secret_resolver=None):
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
        StreamableMCPClient(target, headers=headers),
        tools=preset["tools"],
        limits={"max_results": 100, "timeout_ms": 30000, "max_bytes": 1_000_000},
    )
