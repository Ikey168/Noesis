"""The MCP client SDK symbols the HTTP clients import lazily (MC03, #2758)."""

import inspect
import re
from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from src.integrations.mcp_sdk import read_timeout, session_streams, streamable_http

ROOT = Path(__file__).resolve().parents[3]
CLIENTS = ("src/kb/mcp_transport.py", "src/integrations/mcp.py")


def test_streamable_http_client_resolves_from_the_installed_sdk():
    client, http = streamable_http()
    from mcp.client import streamable_http as sdk

    assert client is sdk.streamable_http_client
    assert "http_client" in inspect.signature(client).parameters
    for name in ("AsyncClient", "AsyncBaseTransport", "AsyncByteStream", "AsyncHTTPTransport", "Timeout"):
        assert hasattr(http, name), name


def test_read_timeout_matches_the_session_signature():
    from mcp import ClientSession

    annotation = str(
        inspect.signature(ClientSession.__init__).parameters["read_timeout_seconds"].annotation
    )
    value = read_timeout(5)
    if "timedelta" in annotation:
        assert value == timedelta(seconds=5)
    else:
        assert value == 5.0 and isinstance(value, float)


def test_session_streams_accept_both_tuple_shapes():
    assert session_streams(("r", "w", lambda: None)) == ("r", "w")
    assert session_streams(("r", "w")) == ("r", "w")


@pytest.mark.parametrize("path", CLIENTS)
def test_clients_do_not_use_the_removed_client_name(path):
    source = (ROOT / path).read_text(encoding="utf-8")
    assert not re.search(r"import streamablehttp_client|streamablehttp_client\(", source)
    assert "streamable_http()" in source
