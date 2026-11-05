"""Hosted vs self-hosted Context7 endpoint selection (#2860, FA21).

Offline: the self-hosted server is a loopback FastMCP fixture; the hosted
endpoint is never contacted (clients connect lazily, on first call).
"""

import socket
import subprocess
import sys
import time

import pytest

from src.integrations.documentation import Context7Research
from src.integrations.mcp import PRESETS, federation_adapter

HOSTED = PRESETS["context7"]["endpoint"]
HOSTED_KEY = "hosted-secret-fixture"
SELF_KEY = "self-hosted-secret-fixture"


def _env(**values):
    return values.get


def _adapter(env, endpoint=None):
    return federation_adapter(
        "context7",
        endpoint=endpoint,
        secret_resolver=env,
        endpoint_resolver=env,
    )


def test_default_is_hosted_with_hosted_key():
    env = _env(NOESIS_CONTEXT7_API_KEY=HOSTED_KEY)
    adapter = _adapter(env)
    assert adapter.client.endpoint == HOSTED
    assert adapter.client.headers == {"CONTEXT7_API_KEY": HOSTED_KEY}
    assert adapter.describe()["endpoint"] == {
        "mode": "hosted",
        "url": HOSTED,
        "credential": "NOESIS_CONTEXT7_API_KEY",
    }


def test_hosted_anonymous_records_anonymous():
    adapter = _adapter(_env())
    assert adapter.client.headers == {}
    assert adapter.describe()["endpoint"]["credential"] == "anonymous"


def test_self_hosted_url_never_receives_hosted_key():
    url = "https://context7.internal.example/mcp"
    adapter = _adapter(
        _env(NOESIS_CONTEXT7_URL=url, NOESIS_CONTEXT7_API_KEY=HOSTED_KEY)
    )
    assert adapter.client.endpoint == url
    assert adapter.client.headers == {}
    assert HOSTED_KEY not in repr(adapter.describe())
    assert adapter.describe()["endpoint"] == {
        "mode": "self-hosted",
        "url": url,
        "credential": "anonymous",
    }


def test_self_hosted_key_is_separate_and_explicit():
    url = "https://context7.internal.example/mcp"
    adapter = _adapter(
        _env(
            NOESIS_CONTEXT7_URL=url,
            NOESIS_CONTEXT7_API_KEY=HOSTED_KEY,
            NOESIS_CONTEXT7_SELF_HOSTED_API_KEY=SELF_KEY,
        )
    )
    assert adapter.client.headers == {"CONTEXT7_API_KEY": SELF_KEY}
    assert (
        adapter.describe()["endpoint"]["credential"]
        == "NOESIS_CONTEXT7_SELF_HOSTED_API_KEY"
    )
    assert SELF_KEY not in repr(adapter.describe())


def test_explicit_endpoint_argument_wins_over_environment():
    env = _env(NOESIS_CONTEXT7_URL="https://from-env.example/mcp")
    adapter = _adapter(env, endpoint="http://127.0.0.1:8080/mcp")
    assert adapter.client.endpoint == "http://127.0.0.1:8080/mcp"


def test_endpoint_mode_changes_capability_hash():
    hosted = _adapter(_env()).describe()
    self_hosted = _adapter(
        _env(NOESIS_CONTEXT7_URL="https://context7.internal.example/mcp")
    ).describe()
    assert hosted["capability_hash"] != self_hosted["capability_hash"]


def test_configuring_hosted_url_explicitly_stays_hosted():
    adapter = _adapter(
        _env(NOESIS_CONTEXT7_URL=HOSTED, NOESIS_CONTEXT7_API_KEY=HOSTED_KEY)
    )
    assert adapter.describe()["endpoint"]["mode"] == "hosted"
    assert adapter.client.headers == {"CONTEXT7_API_KEY": HOSTED_KEY}


@pytest.mark.parametrize(
    "url",
    [
        "http://context7.internal.example/mcp",  # plain HTTP off loopback
        "http://10.0.0.5/mcp",  # private but not loopback
        "ftp://127.0.0.1/mcp",
        "https://user:pass@context7.internal.example/mcp",
        "https://context7.internal.example/mcp?key=x",
        "https://context7.internal.example/mcp#frag",
        "https:///mcp",
        "not a url",
    ],
)
def test_invalid_self_hosted_urls_are_rejected(url):
    with pytest.raises(ValueError, match="endpoint"):
        _adapter(_env(NOESIS_CONTEXT7_URL=url))


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:3000/mcp", "http://localhost:3000/mcp", "http://[::1]:3000/mcp"],
)
def test_loopback_http_is_accepted(url):
    assert _adapter(_env(NOESIS_CONTEXT7_URL=url)).describe()["endpoint"]["url"] == url


def test_other_hosted_presets_stay_pinned():
    with pytest.raises(ValueError, match="fixed"):
        federation_adapter(
            "github",
            endpoint="https://example.org/mcp",
            secret_resolver=_env(NOESIS_GITHUB_MCP_TOKEN="t"),
        )


# --------------------------------------------------------------------------
# End to end against a loopback "self-hosted" Context7-compatible server
# --------------------------------------------------------------------------


@pytest.fixture()
def self_hosted(tmp_path):
    pytest.importorskip("mcp")
    pytest.importorskip("httpx")
    pytest.importorskip("uvicorn")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    path = tmp_path / "context7_fixture.py"
    path.write_text(
        """import sys
from mcp.server.fastmcp import FastMCP, Context
app = FastMCP("context7-selfhosted", host="127.0.0.1", port=int(sys.argv[1]),
              json_response=True)
def _key(ctx):
    request = getattr(ctx.request_context, "request", None)
    return request.headers.get("context7_api_key", "none") if request else "none"
@app.tool(name="resolve-library-id")
def resolve(libraryName: str, query: str, ctx: Context) -> str:
    return "- Context7-compatible library ID: /unit/pint\\nkey=" + _key(ctx)
@app.tool(name="query-docs")
def query_docs(libraryId: str, query: str, ctx: Context) -> str:
    return ("Source: https://docs.example.org/pint\\nSnippet: " + libraryId
            + "\\nkey=" + _key(ctx))
app.run(transport="streamable-http")
"""
    )
    process = subprocess.Popen(
        [sys.executable, str(path), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(200):
            if process.poll() is not None:
                pytest.fail("Context7 fixture exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("Context7 fixture did not start")
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_research_against_self_hosted_records_endpoint(self_hosted):
    adapter = _adapter(
        _env(NOESIS_CONTEXT7_URL=self_hosted, NOESIS_CONTEXT7_API_KEY=HOSTED_KEY)
    )
    try:
        client = Context7Research(adapter)
        resolved = client.resolve("pint", "units", scopes={"operator"})
        assert resolved["library_ids"] == ["/unit/pint"]
        snippets = client.query("/unit/pint", "units", scopes={"operator"})
    finally:
        adapter.client.close()
    assert snippets["source_links"] == ["https://docs.example.org/pint"]
    assert "key=none" in snippets["text"]  # hosted key not sent
    assert HOSTED_KEY not in snippets["text"]
    assert snippets["endpoint"] == {
        "mode": "self-hosted",
        "url": self_hosted,
        "credential": "anonymous",
    }
    assert (
        snippets["raw"]["provenance"]["capability_hash"]
        == adapter.describe()["capability_hash"]
    )


def test_self_hosted_explicit_key_reaches_server(self_hosted):
    adapter = _adapter(
        _env(
            NOESIS_CONTEXT7_URL=self_hosted,
            NOESIS_CONTEXT7_SELF_HOSTED_API_KEY=SELF_KEY,
        )
    )
    try:
        snippets = Context7Research(adapter).query(
            "/unit/pint", "units", scopes={"operator"}
        )
    finally:
        adapter.client.close()
    assert "key=" + SELF_KEY in snippets["text"]
