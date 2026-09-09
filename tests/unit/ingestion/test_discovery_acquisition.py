import json

import duckdb
import pytest

from src.ingestion.brave_discovery import BraveDiscovery
from src.ingestion.connectors.base import SourceRef
from src.ingestion.discovery_acquisition import acquire_candidates


def test_brave_german_candidates_enter_normal_acquisition_and_replay(tmp_path):
    search_calls, fetch_calls = [], []

    def search(**kw):
        search_calls.append(kw["params"])
        return {
            "content": json.dumps(
                {
                    "web": {
                        "results": [
                            {
                                "url": "https://berlin.example/report",
                                "description": "UNVERIFIED SEARCH SNIPPET",
                            }
                        ]
                    }
                }
            )
        }

    client = BraveDiscovery(
        api_key="unused", transport=search, per_request_cost_micros=1, budget_micros=1
    )
    refs = list(
        client.discover("Berliner Forschungsförderung", language="de", country="DE")
    )
    body = (
        "Der Berliner Forschungsbericht beschreibt die Förderung öffentlicher Wissenschaft. "
        * 8
    )

    def fetch(**kw):
        fetch_calls.append(kw)
        return {
            "content": "<article><h1>Forschungsbericht</h1><p>"
            + body
            + "</p></article>"
        }

    kwargs = {
        "acquisition_id": "german-discovery",
        "allowed_hosts": ["berlin.example"],
        "language": "de",
        "transport": fetch,
        "dns_resolver": lambda _: ["8.8.8.8"],
    }
    conn = duckdb.connect(str(tmp_path / "acquired.duckdb"))
    result = acquire_candidates(conn, refs, **kwargs)
    assert result["results"][0]["status"] == "acquired"
    content, metadata, published = conn.execute(
        "SELECT content, metadata, created_at FROM documents"
    ).fetchone()
    assert "UNVERIFIED" not in content and "Forschungsbericht" in content
    assert (
        json.loads(json.loads(metadata)["discovery_json"])["discovery_provider"]
        == "brave"
    )
    assert published is None
    assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone() == (1,)
    conn.close()
    conn = duckdb.connect(str(tmp_path / "acquired.duckdb"))
    try:
        assert acquire_candidates(conn, refs, **kwargs) == result
        assert len(fetch_calls) == 1
        assert (
            search_calls[0]["search_lang"] == "de"
            and search_calls[0]["country"] == "DE"
        )
        with pytest.raises(ValueError, match="different inputs"):
            acquire_candidates(
                conn, [SourceRef("https://berlin.example/other")], **kwargs
            )
    finally:
        conn.close()


def test_candidate_limits_failures_and_dedup():
    conn = duckdb.connect()
    calls = []

    def fetch(**kw):
        calls.append(kw)
        return {"status": 503}

    options = {
        "acquisition_id": "errors",
        "allowed_hosts": ["berlin.example"],
        "language": "de",
        "transport": fetch,
        "dns_resolver": lambda _: ["8.8.8.8"],
    }
    with pytest.raises(ValueError, match="limit"):
        acquire_candidates(
            conn,
            [SourceRef("https://berlin.example/")] * 2,
            max_candidates=1,
            **options,
        )
    assert not calls
    refs = [
        SourceRef(url)
        for url in [
            "https://berlin.example/a",
            "https://berlin.example/a?utm_source=x",
            "https://other.example/a",
        ]
    ]
    results = acquire_candidates(conn, refs, **options)["results"]
    assert [r["status"] for r in results] == ["failed", "duplicate_candidate", "failed"]
    assert len(calls) == 1
    assert conn.execute("SELECT count(*) FROM documents").fetchone() == (0,)
    conn.close()
