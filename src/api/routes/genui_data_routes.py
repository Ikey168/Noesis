"""Data-plane proxy route (MCP rearchitecture plan, R12 / Stage 3 gate).

``POST /api/v1/ui/data`` lets the browser have the API invoke a data-mode MCP
tool on its behalf (the browser never speaks MCP). It is a *prototype behind a
feature flag*: with ``NOESIS_GENUI_DATA_PROXY`` off, every request is refused.

Guardrails (R12 #620): only tools on the discovered data-mode allowlist are
callable; each client is rate-limited; request and response sizes are capped.
``GET /api/v1/ui/data/tools`` exposes the allowlist so the frontend knows which
panels can be served through the proxy.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from src.genui.dataplane import (
    DataPlaneError,
    check_rate,
    check_request_size,
    data_mode_tools,
    data_proxy_enabled,
    encode_payload,
    invoke_data_tool,
)

router = APIRouter(prefix="/api/v1/ui", tags=["generative_ui_data"])


class DataRequest(BaseModel):
    """Body for POST /api/v1/ui/data."""

    server: str = Field(..., max_length=128, description="MCP server name")
    tool: str = Field(..., max_length=128, description="Data-mode tool name")
    arguments: Optional[Dict[str, Any]] = Field(
        default=None, description="Tool arguments"
    )


def _client_key(request: Request) -> str:
    client = request.client.host if request.client else "unknown"
    return client


@router.get("/data/tools")
def data_tools() -> Dict[str, Any]:
    """The data-mode allowlist the proxy will serve (empty when the flag is off
    or the host is down), so the frontend can decide which panels to fetch
    live."""
    if not data_proxy_enabled():
        return {"enabled": False, "tools": []}
    tools = [
        {"server": server, "tool": tool, **meta}
        for (server, tool), meta in sorted(data_mode_tools().items())
    ]
    return {"enabled": True, "tools": tools, "count": len(tools)}


@router.post("/data")
def ui_data(request: DataRequest, http_request: Request) -> Response:
    """Invoke an allowlisted data-mode MCP tool and return its payload.

    Refuses when the flag is off (404), when the tool is not allowlisted (403),
    when the client is over its rate limit (429), or when a size cap is
    exceeded (413). Sync on purpose: the blocking MCP call runs in the
    threadpool.

    The response uses the lighter data-plane encoding (M2.1): compact JSON,
    gzip-compressed when the client accepts it and the payload is large enough
    to benefit, which shrinks the cold-path transfer.
    """
    if not data_proxy_enabled():
        raise HTTPException(
            status_code=404,
            detail="data-plane proxy is disabled (set NOESIS_GENUI_DATA_PROXY=on)",
        )
    try:
        check_rate(_client_key(http_request))
        check_request_size(request.arguments)
        payload = invoke_data_tool(request.server, request.tool, request.arguments)
    except DataPlaneError as err:
        raise HTTPException(status_code=err.status, detail=err.message)
    body = {"server": request.server, "tool": request.tool, "data": payload}
    data_bytes, encoding = encode_payload(
        body, http_request.headers.get("accept-encoding", "")
    )
    headers = {"Content-Length": str(len(data_bytes))}
    if encoding:
        headers["Content-Encoding"] = encoding
        headers["Vary"] = "Accept-Encoding"
    return Response(content=data_bytes, media_type="application/json", headers=headers)
