import duckdb
import pytest

from src.ingestion.provider_execution import DurableHTTP, ProviderError


def client(conn, transport, **kwargs):
    return DurableHTTP(
        conn,
        budget_id="test",
        provider="example",
        principal_id="alice",
        allowed_hosts=["example.org"],
        reuse_notice="Authored test responses only",
        max_requests=2,
        max_usd_micros=100,
        transport=transport,
        **kwargs,
    )


def test_reserve_before_io_restart_replays_without_charge(tmp_path):
    path = str(tmp_path / "ledger.duckdb")
    conn = duckdb.connect(path)
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        assert (
            conn.execute(
                "SELECT used_requests FROM provider_execution_budgets"
            ).fetchone()[0]
            == 1
        )
        return {
            "content": '{"id":"native-1"}',
            "headers": {"Content-Type": "application/json"},
        }

    http = client(conn, transport)
    first = http.request(
        "one", "https://example.org/data", principal_id="alice", max_cost_micros=30
    )
    assert first.json() == {"id": "native-1"}
    conn.close()
    conn = duckdb.connect(path)
    http = client(conn, lambda **kwargs: pytest.fail("replay must not call transport"))
    replay = http.request(
        "one", "https://example.org/data", principal_id="alice", max_cost_micros=30
    )
    assert replay.content == first.content and replay.receipt["replayed"]
    assert replay.receipt["observed_at_ms"] == first.receipt["observed_at_ms"]
    assert http.inspect(principal_id="alice")["reserved_usd_micros"] == 30
    assert len(calls) == 1
    with pytest.raises(ProviderError, match="bound"):
        http.request(
            "one", "https://example.org/other", principal_id="alice", max_cost_micros=30
        )
    with pytest.raises(ProviderError, match="another principal"):
        http.inspect(principal_id="mallory")
    conn.close()


def test_credentials_are_not_persisted_and_failures_do_not_retry():
    conn = duckdb.connect()
    secret = "do-not-retain-this-token"

    def transport(**kwargs):
        assert kwargs["params"]["api_token"] == secret
        return {"status": 403, "content": secret}

    http = client(conn, transport)
    with pytest.raises(ProviderError) as exc:
        http.request(
            "bad",
            "https://example.org/data",
            principal_id="alice",
            secret_params={"api_token": secret},
        )
    assert exc.value.code == "http_403" and secret not in str(exc.value)
    stored = conn.execute(
        "SELECT receipt_json FROM provider_execution_requests"
    ).fetchone()[0]
    assert secret not in stored
    with pytest.raises(ProviderError, match="previous attempt"):
        http.request(
            "bad",
            "https://example.org/data",
            principal_id="alice",
            secret_params={"api_token": secret},
        )
    assert http.inspect(principal_id="alice")["used_requests"] == 1


def test_indeterminate_reservation_cannot_double_charge_and_limits_are_atomic():
    conn = duckdb.connect()
    http = client(conn, lambda **kwargs: {"content": b"abc"})
    http.request(
        "one", "https://example.org/data", principal_id="alice", max_cost_micros=80
    )
    conn.execute("UPDATE provider_execution_requests SET state='reserved'")
    with pytest.raises(ProviderError) as exc:
        http.request(
            "one", "https://example.org/data", principal_id="alice", max_cost_micros=80
        )
    assert exc.value.code == "indeterminate_request"
    with pytest.raises(ProviderError) as exc:
        http.request(
            "two", "https://example.org/data", principal_id="alice", max_cost_micros=30
        )
    assert exc.value.code == "budget_exhausted"
    assert http.inspect(principal_id="alice")["used_requests"] == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/a",
        "https://example.org:8443/a",
        "https://user:pass@example.org/a",
        "https://elsewhere.org/a",
        "https://example.org/a?api_token=oops",
    ],
)
def test_host_and_source_url_policy(url):
    conn = duckdb.connect()
    http = client(conn, lambda **kw: pytest.fail("unsafe URL must not fetch"))
    with pytest.raises(ProviderError):
        http.request("unsafe", url, principal_id="alice")


def test_nonpublic_dns_and_oversized_or_secret_echo_are_rejected():
    conn = duckdb.connect()
    http = client(
        conn,
        lambda **kw: {"content": "0123456789abcdef"},
        resolver=lambda _: ["127.0.0.1"],
    )
    with pytest.raises(ProviderError) as exc:
        http.request("unsafe", "https://example.org/a", principal_id="alice")
    assert exc.value.code == "ssrf_blocked"
    http.resolver = None
    with pytest.raises(ProviderError) as exc:
        http.request("big", "https://example.org/a", principal_id="alice", max_bytes=3)
    assert exc.value.code == "response_limit"
    with pytest.raises(ProviderError) as exc:
        http.request(
            "echo",
            "https://example.org/a",
            principal_id="alice",
            secret_headers={"Authorization": "Bearer 0123456789abcdef"},
        )
    assert exc.value.code == "credential_echo"
    assert (
        conn.execute("SELECT count(*) FROM provider_execution_blobs").fetchone()[0] == 0
    )


@pytest.mark.parametrize("params", [{}, {"api_token": "test-secret", "page": "2"}])
def test_native_transport_preserves_url_download_query(monkeypatch, params):
    import httpx

    from src.ingestion.provider_execution import _request

    seen = []
    original_client = httpx.Client

    def handle(request):
        seen.append(request)
        return httpx.Response(200, content=b"%PDF-selected-document")

    def client_with_transport(**kwargs):
        return original_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "Client", client_with_transport)
    result = _request(
        method="GET",
        url="https://www.bfarm.de/letter.pdf?__blob=publicationFile&v=2&tag=de&tag=en",
        params=params,
        body=None,
        headers={},
        timeout_s=2,
        max_bytes=1000,
    )
    assert result["content"].startswith(b"%PDF")
    query = seen[0].url.params
    assert query["__blob"] == "publicationFile"
    assert query["v"] == "2"
    assert query.get_list("tag") == ["de", "en"]
    for key, value in params.items():
        assert query[key] == value


def test_explicit_large_provider_capture_is_archived_and_replayable():
    from src.ingestion.snapshots import SnapshotStore

    raw = b"x" * 20_000_001
    conn = duckdb.connect(":memory:")
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {"content": raw}

    http = DurableHTTP(
        conn,
        budget_id="large-index",
        provider="example",
        principal_id="a",
        allowed_hosts=["example.org"],
        reuse_notice="fixture",
        max_requests=1,
        max_bytes=25_000_000,
        transport=transport,
    )
    first = http.request(
        "index", "https://example.org/index.xml", principal_id="a", max_bytes=25_000_000
    )
    assert first.receipt["snapshot"]["bytes"] == len(raw)
    assert (
        http.request(
            "index",
            "https://example.org/index.xml",
            principal_id="a",
            max_bytes=25_000_000,
        ).content
        == raw
    )
    assert len(calls) == 1
    with pytest.raises(ValueError, match="exceeds"):
        SnapshotStore(conn).snapshot_bytes(
            "https://example.org/default",
            raw,
            1,
            content_type="application/xml",
            final_url="https://example.org/default",
        )
    conn.close()
