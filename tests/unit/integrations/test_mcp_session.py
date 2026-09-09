import socket
import subprocess
import sys
import time

import pytest

pytest.importorskip("mcp")
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")

from src.integrations.mcp import StreamableMCPClient, federation_adapter


@pytest.fixture()
def server(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = tmp_path / "server.py"
    path.write_text("""import asyncio, sys
from mcp.server.fastmcp import FastMCP, Context
app = FastMCP("fixture", host="127.0.0.1", port=int(sys.argv[1]), json_response=True)
@app.tool()
def identity(ctx: Context) -> str:
    return str(id(ctx.session))
@app.tool()
async def slow() -> str:
    await asyncio.sleep(1)
    return "late"
app.run(transport="streamable-http")
""")
    process = subprocess.Popen(
        [sys.executable, str(path), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail("MCP fixture exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("MCP fixture did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_official_sdk_session_persists_and_closes(server):
    with StreamableMCPClient(server) as client:
        first = client.call_tool("identity", {})
        second = client.call_tool("identity", {})
        assert first["content"] == second["content"]
        assert client._stack is not None
    assert client._stack is None and client._portal is None
    with pytest.raises(ValueError, match="closed"):
        client.list_tools()


def test_deadline_resets_transport_and_can_reconnect(server):
    with StreamableMCPClient(server, timeout_seconds=0.2) as client:
        client.list_tools()
        started = time.monotonic()
        with pytest.raises(ValueError, match="session reset"):
            client.call_tool("slow", {})
        assert time.monotonic() - started < 3
        assert client._stack is None
        assert client.list_tools()["tools"]


def test_browser_profile_limits_actions_and_navigation_before_connecting():
    adapter = federation_adapter(
        "playwright",
        endpoint="http://localhost:8766/mcp",
        navigation_origins=["https://www.berlin.de"],
    )
    with pytest.raises(ValueError, match="origin"):
        adapter.client.call_tool("browser_navigate", {"url": "https://example.org"})
    with pytest.raises(ValueError, match="action"):
        adapter.client.call_tool("browser_evaluate", {"function": "() => 1"})
    with pytest.raises(ValueError, match="range"):
        adapter.client.call_tool("browser_wait_for", {"time": 999})
    assert adapter.client._stack is None
    adapter.client.close()
