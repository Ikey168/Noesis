import asyncio
import socket
import threading
import time

import pytest

from src.kb.federation import FederationError
from src.kb.mcp_transport import MCPHTTPClient


def test_actual_sdk_streamable_http_session_and_cleanup():
    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("mcp")
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("local-contract-fixture", json_response=True)

    @server.tool()
    async def issue_read(owner: str, repo: str, issue_number: int, method: str):
        if method == "slow":
            await asyncio.sleep(30)
        if method == "echo":
            return {"body": "never-export-this-token"}
        return {
            "id": 123,
            "number": issue_number,
            "owner": owner,
            "repo": repo,
            "method": method,
        }

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    instance = uvicorn.Server(
        uvicorn.Config(
            server.streamable_http_app(), log_level="critical", lifespan="on"
        )
    )
    thread = threading.Thread(target=lambda: instance.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not instance.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert instance.started
        with MCPHTTPClient(
            f"http://127.0.0.1:{port}/mcp",
            kind="github",
            timeout_s=5,
            token="never-export-this-token",
        ) as client:
            assert client.list_tools()[0]["name"] == "issue_read"
            result = client.call_tool(
                "issue_read",
                {"owner": "org", "repo": "repo", "issue_number": 7, "method": "get"},
            )
            from src.kb.mcp_research import decode_tool

            assert decode_tool(result)["id"] == 123
            with pytest.raises(FederationError) as echoed:
                client.call_tool(
                    "issue_read",
                    {
                        "owner": "org",
                        "repo": "repo",
                        "issue_number": 7,
                        "method": "echo",
                    },
                )
            assert echoed.value.code == "credential_echo"
            assert "never-export-this-token" not in str(echoed.value)
            from src.kb.federation import RemoteMCPAdapter

            adapter = RemoteMCPAdapter("cancel-test", client, tools=["issue_read"])
            cancelled = threading.Event()
            timer = threading.Timer(0.2, cancelled.set)
            timer.start()
            started = time.monotonic()
            try:
                with pytest.raises(FederationError) as failure:
                    adapter.query(
                        {
                            "kind": "tool",
                            "name": "issue_read",
                            "arguments": {
                                "owner": "org",
                                "repo": "repo",
                                "issue_number": 7,
                                "method": "slow",
                            },
                        },
                        scopes={"operator"},
                        cancelled=cancelled.is_set,
                    )
                assert failure.value.code == "cancelled"
                assert time.monotonic() - started < 2
            finally:
                timer.cancel()
            with pytest.raises(FederationError, match="allowlisted"):
                client.call_tool("issue_write", {})
        assert not client._thread.is_alive()
    finally:
        instance.should_exit = True
        thread.join(7)
        sock.close()
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/mcp",
        "http://192.168.1.1/mcp",
        "http://127.0.0.1/mcp?secret=one",
    ],
)
def test_transport_rejects_undeclared_endpoints(endpoint):
    with pytest.raises(ValueError):
        MCPHTTPClient(endpoint, kind="browser")
