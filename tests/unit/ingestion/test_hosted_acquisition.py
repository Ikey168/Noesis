import base64
import json

import duckdb
import pytest

from src.ingestion.hosted_acquisition import HOSTS, HostedClient, OpenAlexFullText
from src.ingestion.provider_execution import DurableHTTP, ProviderError


def transport_client(provider, response, *, requests=2):
    calls = []
    conn = duckdb.connect()

    def transport(**kwargs):
        calls.append(kwargs)
        return {
            "content": json.dumps(response) if isinstance(response, dict) else response,
            "status": 200,
        }

    http = DurableHTTP(
        conn,
        provider=provider,
        principal_id="alice",
        budget_id="budget",
        reuse_notice="fixture-use",
        allowed_hosts=HOSTS[provider],
        transport=transport,
        max_requests=requests,
        max_usd_micros=1000,
    )
    return conn, calls, http


@pytest.mark.parametrize("provider", ["exa", "tavily"])
def test_native_discovery_payloads_and_durable_secret_free_replay(provider):
    conn, calls, http = transport_client(
        provider,
        {
            "results": [
                {
                    "url": "https://www.berlin.de/news",
                    "title": "Berlin",
                    "content": "summary",
                    "id": "native",
                }
            ]
        },
    )
    client = HostedClient(
        http,
        principal_id="alice",
        enabled=True,
        credential="long-fixture-secret",
        max_cost_per_request_micros=100,
    )
    result = client.search(
        "Berlin evidence",
        "obs",
        public_query_approved=True,
        domains=["www.berlin.de"],
        from_date="2026-01-01",
        limit=2,
    )
    assert not result["references"][0].metadata["provider_summary_is_evidence"]
    body = calls[0]["body"]
    assert body["numResults" if provider == "exa" else "max_results"] == 2
    assert ("startPublishedDate" if provider == "exa" else "start_date") in body
    assert "long-fixture-secret" not in json.dumps(result["receipt"])
    http.resolver = lambda host: (_ for _ in ()).throw(RuntimeError("offline DNS"))
    assert client.search(
        "Berlin evidence",
        "obs",
        public_query_approved=True,
        domains=["www.berlin.de"],
        from_date="2026-01-01",
        limit=2,
    )["receipt"]["replayed"]
    assert len(calls) == 1
    conn.close()


def test_firecrawl_remains_transformed_and_requires_authorized_budgets():
    conn, calls, http = transport_client(
        "firecrawl",
        {
            "success": True,
            "data": {
                "rawHtml": "<p>evidence</p>",
                "markdown": "evidence",
                "metadata": {
                    "sourceURL": "https://www.berlin.de/page",
                    "statusCode": 200,
                },
            },
        },
    )
    with pytest.raises(ValueError):
        HostedClient(http, principal_id="alice", enabled=False)
    client = HostedClient(
        http,
        principal_id="alice",
        enabled=True,
        credential="secret",
        max_cost_per_request_micros=100,
    )
    with pytest.raises(ValueError):
        client.scrape(
            "https://www.berlin.de/page", "obs", allowed_source_hosts=["www.berlin.de"]
        )
    result = client.scrape(
        "https://www.berlin.de/page",
        "obs",
        allowed_source_hosts=["www.berlin.de"],
        public_url_approved=True,
    )
    assert calls[0]["body"]["proxy"] == "basic"
    assert calls[0]["body"]["parsers"] == []
    assert calls[0]["body"]["storeInCache"] is False
    assert result["representation"] == "provider-transformed-html-not-original-response"
    assert result["actual_billed_usd_micros"] is None
    from src.scraper.backend_evaluation import fetch_backend

    with pytest.raises(ValueError, match="explicit"):
        fetch_backend("https://www.berlin.de/page", "firecrawl")
    replay = fetch_backend(
        "https://www.berlin.de/page",
        "firecrawl",
        hosted_client=client,
        observation="obs",
        allowed_source_hosts=["www.berlin.de"],
        public_url_approved=True,
    )
    assert replay.markdown == "evidence"
    assert len(calls) == 1
    conn.close()


def test_openalex_native_format_fields_identity_and_restart_ledger():
    tei = b'<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><p coords="1,1,2,3,4">German evidence body.</p></div></body></text></TEI>'
    conn, calls, http = transport_client("openalex-content", tei)
    api = OpenAlexFullText(
        http,
        principal_id="alice",
        credential="long-secret",
        enabled=True,
        max_cost_per_download_micros=100,
    )
    work = {
        "id": "https://openalex.org/W123",
        "has_content": {"pdf": False, "grobid_xml": True},
        "content_urls": {
            "pdf": None,
            "grobid_xml": "https://content.openalex.org/works/W123.grobid-xml",
        },
        "language": "de",
        "best_oa_location": {"license": "cc-by"},
    }
    assert (
        api.acquire(
            work, "obs", representation="pdf", namespace="r", scopes={"operator"}
        )["status"]
        == "unavailable"
    )
    result = api.acquire(work, "obs", namespace="r", scopes={"operator"})
    assert (
        result["status"] == "acquired"
        and result["sections"][0]["coords"] == "1,1,2,3,4"
    )
    assert api.acquire(work, "obs", namespace="r", scopes={"operator"})["receipt"][
        "replayed"
    ]
    assert (
        len(calls) == 1
        and conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    )
    changed = tei.replace(b"German evidence body.", b"Revised German evidence body.")
    http.transport = lambda **kwargs: {"content": changed}
    newer = api.acquire(work, "newer", namespace="r", scopes={"operator"})
    replay = api.acquire(work, "obs", namespace="r", scopes={"operator"})
    from src.ingestion.document_store import DocumentStore

    assert replay["source_ref"] == result["source_ref"]
    assert (
        DocumentStore(conn).revisions.revision(result["document_id"])["revision_id"]
        == newer["source_ref"]["revision_id"]
    )
    with pytest.raises(ProviderError, match="bound"):
        api.acquire(
            {**work, "title": "Changed work metadata"},
            "obs",
            namespace="r",
            scopes={"operator"},
        )
    with pytest.raises(ProviderError, match="URL"):
        api.acquire(
            {
                **work,
                "content_urls": {
                    "grobid_xml": "https://content.openalex.org/works/W999.grobid-xml"
                },
            },
            "wrong",
            namespace="r",
            scopes={"operator"},
        )
    conn.close()


def test_zyte_preserves_native_response_body_and_cost_limits():
    conn, calls, http = transport_client(
        "zyte",
        {
            "url": "https://www.berlin.de/page",
            "statusCode": 200,
            "httpResponseBody": base64.b64encode(b"<p>Evidence</p>").decode(),
        },
        requests=1,
    )
    client = HostedClient(
        http,
        principal_id="alice",
        credential="secret",
        enabled=True,
        max_cost_per_request_micros=100,
    )
    result = client.scrape(
        "https://www.berlin.de/page",
        "obs",
        allowed_source_hosts=["www.berlin.de"],
        public_url_approved=True,
    )
    assert result["html"] == "<p>Evidence</p>"
    with pytest.raises(ProviderError, match="budget"):
        client.scrape(
            "https://www.berlin.de/page",
            "another",
            allowed_source_hosts=["www.berlin.de"],
            public_url_approved=True,
        )
    assert len(calls) == 1
    conn.close()


@pytest.mark.parametrize("status", [None, 403, 404, 500])
def test_firecrawl_api_success_does_not_mask_source_failure(status):
    conn, _, http = transport_client(
        "firecrawl",
        {
            "success": True,
            "data": {"rawHtml": "<p>Not found</p>", "metadata": {"statusCode": status}},
        },
    )
    client = HostedClient(
        http,
        principal_id="alice",
        enabled=True,
        credential="secret",
        max_cost_per_request_micros=100,
    )
    with pytest.raises(ProviderError, match="source status"):
        client.scrape(
            "https://www.berlin.de/page",
            "failed",
            allowed_source_hosts=["www.berlin.de"],
            public_url_approved=True,
        )
    conn.close()


def test_reader_requires_authoritative_source_observation_and_replays():
    from src.ingestion.snapshots import SnapshotStore

    url = "https://www.berlin.de/page"
    conn, calls, http = transport_client(
        "jina", {"code": 200, "data": {"url": url, "content": "# Deutsche Quelle"}}
    )
    original = SnapshotStore(conn).snapshot_bytes(
        url, b"<h1>Deutsche Quelle</h1>", 1, content_type="text/html", final_url=url
    )
    client = HostedClient(http, principal_id="alice", enabled=True)
    options = {"allowed_source_hosts": ["www.berlin.de"], "public_url_approved": True}
    for changed in [{"fetched_at": 2}, {"url": "https://www.berlin.de/other"}]:
        with pytest.raises(ValueError, match="source observation"):
            client.reader(
                url, "test", original_snapshot={**original, **changed}, **options
            )
    assert not calls
    result = client.reader(url, "test", original_snapshot=original, **options)
    assert result["representation"] == "jina-markdown"
    assert result["precise_locators"] is False
    assert (
        client.reader(url, "test", original_snapshot=original, **options)["text"]
        == result["text"]
    )
    assert len(calls) == 1
    conn.close()
