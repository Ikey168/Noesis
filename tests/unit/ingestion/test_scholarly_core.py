"""CORE scholarly connector: query shaping, key header, date fallback (offline)."""
import json
import urllib.parse

from src.ingestion.connectors.registry import get_connector
from src.ingestion.connectors.scholarly.sources import CoreConnector


class FakeCore:
    """Returns ``results`` once, recording each request."""

    def __init__(self, results):
        self.results = results
        self.requests = []

    def __call__(self, url, headers):
        parsed = urllib.parse.urlparse(url)
        self.requests.append((parsed.path, dict(urllib.parse.parse_qsl(parsed.query)), dict(headers)))
        return json.dumps({"totalHits": len(self.results), "results": self.results}).encode()


def _harvest(fake, query, key="test-key"):
    conn = CoreConnector(http_get=fake, dns_resolver=lambda host: ["93.184.216.34"], api_key=key)
    ref = next(iter(conn.discover(query)))
    return conn.parse(conn.fetch(ref))


def _work(i, **dates):
    return {"id": i, "title": f"Paper {i}", "abstract": "An abstract.", "authors": [{"name": "A"}], **dates}


def test_registered_under_core():
    assert isinstance(get_connector("core"), CoreConnector)


def test_query_filters_by_year_on_canonical_path():
    fake = FakeCore([])
    _harvest(fake, {"topic": "ChatGPT AND tutor", "since": "2022-11-30", "until": "2026-10-31", "limit": 50})
    path, params, headers = fake.requests[0]
    assert path == "/v3/search/works/"
    assert params["q"] == "(ChatGPT AND tutor) AND yearPublished>=2022 AND yearPublished<=2026"
    assert "publishedDate" not in params["q"]
    assert params["limit"] == "50"
    assert headers["Authorization"] == "Bearer test-key"


def test_year_only_records_are_kept_and_window_is_exact():
    fake = FakeCore([
        _work(1, publishedDate="2024-05-01T00:00:00"),
        _work(2, yearPublished=2025),
        _work(3, yearPublished=2021),
        _work(4, publishedDate="2022-06-01"),
        _work(5),
    ])
    docs = _harvest(fake, {"topic": "LLM", "since": "2022-11-30", "until": "2026-10-31"})
    assert sorted(d.title for d in docs) == ["Paper 1", "Paper 2"]
