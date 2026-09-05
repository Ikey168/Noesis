"""Extra focused tests for NewsAggregatorConnector to cover gaps.

Targets the per-service _fetch_* parsing methods, connect/_setup_service
branches, disconnect, fetch_data dispatch + error wrapping, validate_config,
validate_data_format, and the metadata helpers (categories / limits).

Following the proven pattern from test_web_connector_extra.py: fake async
context-manager session/response doubles are injected instead of aioresponses
(which is broken in this env).
"""

import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

pytest.importorskip("aiohttp")
pytest.importorskip("feedparser")

from unittest.mock import AsyncMock  # noqa: E402

from scraper.extensions.connectors.news_aggregator_connector import (  # noqa: E402
    NewsAggregatorConnector,
)
from scraper.extensions.connectors.base import (  # noqa: E402
    ConnectionError,
    AuthenticationError,
)


# ---------------------------------------------------------------------------
# Test doubles for aiohttp session / response (async context managers).
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, *, status=200, json_data=None, text_data=""):
        self.status = status
        self._json = json_data if json_data is not None else {}
        self._text = text_data

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Returns a fixed response for every GET; records call args."""

    def __init__(self, response):
        self._response = response
        self.closed = False
        self.calls = []

    def get(self, url, headers=None, params=None):
        self.calls.append((url, headers, params))
        return self._response

    async def close(self):
        self.closed = True


class ExplodingSession:
    def __init__(self):
        self.closed = False

    def get(self, url, headers=None, params=None):
        raise RuntimeError("boom")

    async def close(self):
        self.closed = True


def make(service, auth=None, config_extra=None):
    config = {"service": service}
    if config_extra:
        config.update(config_extra)
    return NewsAggregatorConnector(config, auth_config=auth or {})


def connected(service, response, auth=None):
    c = make(service, auth=auth)
    c._session = FakeSession(response)
    c._connected = True
    return c


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------
def test_validate_config_missing_service_field():
    c = NewsAggregatorConnector({}, auth_config={})
    assert c.validate_config() is False


def test_validate_config_newsapi_requires_api_key():
    assert make("newsapi", auth={}).validate_config() is False
    assert make("newsapi", auth={"api_key": "k"}).validate_config() is True


def test_validate_config_guardian_and_nytimes_require_api_key():
    assert make("guardian", auth={}).validate_config() is False
    assert make("guardian", auth={"api_key": "k"}).validate_config() is True
    assert make("nytimes", auth={}).validate_config() is False
    assert make("nytimes", auth={"api_key": "k"}).validate_config() is True


def test_validate_config_rss_services_no_key():
    assert make("reuters").validate_config() is True
    assert make("google_news").validate_config() is True


def test_validate_config_bing_requires_subscription_key():
    assert make("bing_news", auth={}).validate_config() is False
    assert make("bing_news", auth={"subscription_key": "s"}).validate_config() is True


def test_validate_config_unknown_service_defaults_true():
    assert make("something_else").validate_config() is True


# ---------------------------------------------------------------------------
# connect() / _setup_service
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_connect_returns_early_when_session_open():
    c = make("reuters")
    sess = FakeSession(FakeResponse())
    c._session = sess
    assert await c.connect() is True
    assert c.is_connected is True
    assert sess.calls == []  # early return, no setup HTTP needed


@pytest.mark.asyncio
async def test_connect_sets_newsapi_header(monkeypatch):
    c = make("newsapi", auth={"api_key": "secret"})

    def fake_session_ctor(*a, **k):
        return FakeSession(FakeResponse())

    monkeypatch.setattr(
        "scraper.extensions.connectors.news_aggregator_connector.aiohttp.ClientSession",
        fake_session_ctor,
    )
    assert await c.connect() is True
    assert c._headers["X-API-Key"] == "secret"
    assert c.is_connected is True


@pytest.mark.asyncio
async def test_connect_sets_bing_header(monkeypatch):
    c = make("bing_news", auth={"subscription_key": "sub"})
    monkeypatch.setattr(
        "scraper.extensions.connectors.news_aggregator_connector.aiohttp.ClientSession",
        lambda *a, **k: FakeSession(FakeResponse()),
    )
    assert await c.connect() is True
    assert c._headers["Ocp-Apim-Subscription-Key"] == "sub"


@pytest.mark.asyncio
async def test_connect_rss_accept_header(monkeypatch):
    c = make("reuters")
    monkeypatch.setattr(
        "scraper.extensions.connectors.news_aggregator_connector.aiohttp.ClientSession",
        lambda *a, **k: FakeSession(FakeResponse()),
    )
    assert await c.connect() is True
    assert "rss" in c._headers["Accept"]


@pytest.mark.asyncio
async def test_connect_handles_exception(monkeypatch):
    c = make("newsapi", auth={"api_key": "k"})

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "scraper.extensions.connectors.news_aggregator_connector.aiohttp.ClientSession",
        boom,
    )
    assert await c.connect() is False
    assert c.is_connected is False
    assert isinstance(c.last_error, ConnectionError)
    assert "Failed to connect" in str(c.last_error)


@pytest.mark.asyncio
async def test_setup_service_missing_key_returns_false():
    # newsapi setup accesses auth_config['api_key']; missing -> KeyError -> False
    c = make("newsapi", auth={})
    result = await c._setup_service()
    assert result is False
    assert isinstance(c.last_error, AuthenticationError)


# ---------------------------------------------------------------------------
# disconnect()
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_disconnect_no_session():
    c = make("reuters")
    c._connected = True
    await c.disconnect()
    assert c.is_connected is False


@pytest.mark.asyncio
async def test_disconnect_closes_open_session():
    c = make("reuters")
    sess = AsyncMock()
    sess.closed = False
    c._session = sess
    c._connected = True
    await c.disconnect()
    sess.close.assert_awaited_once()
    assert c.is_connected is False


# ---------------------------------------------------------------------------
# fetch_data dispatch and error handling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_data_not_connected_raises():
    c = make("newsapi", auth={"api_key": "k"})
    with pytest.raises(ConnectionError, match="Not connected"):
        await c.fetch_data()


@pytest.mark.asyncio
async def test_fetch_data_unsupported_service_wrapped():
    c = connected("weirdservice", FakeResponse())
    with pytest.raises(ConnectionError, match="Failed to fetch"):
        await c.fetch_data()
    assert c.last_error is not None


@pytest.mark.asyncio
async def test_fetch_data_wraps_internal_exception():
    c = connected("reuters", None)  # session.get on None -> AttributeError path
    c._session = ExplodingSession()
    with pytest.raises(ConnectionError, match="Failed to fetch reuters data"):
        await c.fetch_data()


# ---------------------------------------------------------------------------
# _fetch_newsapi_data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_newsapi_parses_articles():
    payload = {
        "articles": [
            {
                "title": "T1",
                "description": "D1",
                "content": "C1",
                "url": "http://x/1",
                "publishedAt": "2024-01-01",
                "author": "A1",
                "source": {"name": "Src"},
                "urlToImage": "http://img/1",
            }
        ]
    }
    c = connected("newsapi", FakeResponse(json_data=payload), auth={"api_key": "k"})
    arts = await c.fetch_data(query="ai", category="tech", from_date="x", to_date="y")
    assert len(arts) == 1
    a = arts[0]
    assert a["title"] == "T1"
    assert a["source"] == "Src"
    assert a["url_to_image"] == "http://img/1"
    assert a["service"] == "newsapi"
    # query path uses the everything endpoint
    assert c._session.calls[0][0].endswith("/everything")


@pytest.mark.asyncio
async def test_fetch_newsapi_top_headlines_when_no_query():
    c = connected("newsapi", FakeResponse(json_data={"articles": []}), auth={"api_key": "k"})
    arts = await c.fetch_data()
    assert arts == []
    assert c._session.calls[0][0].endswith("/top-headlines")


@pytest.mark.asyncio
async def test_fetch_newsapi_auth_error():
    c = connected("newsapi", FakeResponse(status=401), auth={"api_key": "k"})
    with pytest.raises(AuthenticationError, match="authentication failed"):
        await c.fetch_data()
    assert isinstance(c.last_error, AuthenticationError)


@pytest.mark.asyncio
async def test_fetch_newsapi_rate_limit():
    c = connected("newsapi", FakeResponse(status=429), auth={"api_key": "k"})
    with pytest.raises(ConnectionError):
        await c.fetch_data()
    assert "rate limit" in str(c.last_error)


@pytest.mark.asyncio
async def test_fetch_newsapi_other_error():
    c = connected("newsapi", FakeResponse(status=500), auth={"api_key": "k"})
    with pytest.raises(ConnectionError):
        await c.fetch_data()
    assert "NewsAPI error" in str(c.last_error)


# ---------------------------------------------------------------------------
# _fetch_guardian_data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_guardian_parses_articles_with_contributor():
    payload = {
        "response": {
            "results": [
                {
                    "webTitle": "WT",
                    "webUrl": "http://g/1",
                    "id": "world/2026/sep/01/one",
                    "webPublicationDate": "2024-02-02",
                    "sectionName": "World",
                    "fields": {
                        "headline": "Headline",
                        "trailText": "trail",
                        "body": "body text",
                        "thumbnail": "http://g/img",
                        "byline": "byline",
                    },
                    "tags": [
                        {"type": "contributor", "webTitle": "Jane Doe"},
                        {"type": "keyword", "webTitle": "ignored"},
                    ],
                }
            ]
        }
    }
    c = connected("guardian", FakeResponse(json_data=payload), auth={"api_key": "k"})
    arts = await c.fetch_data(query="ai", category="world")
    a = arts[0]
    assert a["title"] == "Headline"
    assert a["author"] == "Jane Doe"
    assert a["source"] == "The Guardian"
    assert a["content"] == "body text"
    assert a["service"] == "guardian"


@pytest.mark.asyncio
async def test_fetch_guardian_byline_fallback_when_no_contributor():
    payload = {
        "response": {
            "results": [
                {
                    "webTitle": "WT",
                    "webUrl": "http://g/2",
                    "id": "world/2026/sep/01/two",
                    "fields": {"byline": "Fallback Author"},
                    "tags": [],
                }
            ]
        }
    }
    c = connected("guardian", FakeResponse(json_data=payload), auth={"api_key": "k"})
    arts = await c.fetch_data()
    assert arts[0]["author"] == "Fallback Author"
    assert arts[0]["title"] == "WT"  # falls back to webTitle


@pytest.mark.asyncio
async def test_fetch_guardian_auth_error():
    c = connected("guardian", FakeResponse(status=401), auth={"api_key": "k"})
    with pytest.raises(AuthenticationError):
        await c.fetch_data()
    assert isinstance(c.last_error, AuthenticationError)


# ---------------------------------------------------------------------------
# _fetch_nytimes_data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_nytimes_article_search_with_query():
    payload = {
        "response": {
            "docs": [
                {
                    "headline": {"main": "NYT Title"},
                    "abstract": "abs",
                    "lead_paragraph": "lead",
                    "web_url": "http://nyt/1",
                    "pub_date": "2024-03-03",
                    "section_name": "Tech",
                    "byline": {"person": [{"firstname": "John", "lastname": "Smith"}]},
                }
            ]
        }
    }
    c = connected("nytimes", FakeResponse(json_data=payload), auth={"api_key": "k"})
    arts = await c.fetch_data(query="ai", limit=10)
    a = arts[0]
    assert a["title"] == "NYT Title"
    assert "John Smith" in a["author"]
    assert a["section"] == "Tech"
    assert "articlesearch" in c._session.calls[0][0]


@pytest.mark.asyncio
async def test_fetch_nytimes_top_stories_no_query():
    payload = {
        "results": [
            {
                "title": "Top Title",
                "abstract": "abs",
                "url": "http://nyt/2",
                "published_date": "2024-04-04",
                "byline": "By Someone",
                "section": "World",
                "multimedia": [{"url": "http://nyt/img"}],
            }
        ]
    }
    c = connected("nytimes", FakeResponse(json_data=payload), auth={"api_key": "k"})
    arts = await c.fetch_data()
    a = arts[0]
    assert a["title"] == "Top Title"
    assert a["url_to_image"] == "http://nyt/img"
    assert "topstories" in c._session.calls[0][0]


@pytest.mark.asyncio
async def test_fetch_nytimes_top_stories_no_multimedia():
    payload = {"results": [{"title": "X", "url": "http://nyt/3", "multimedia": []}]}
    c = connected("nytimes", FakeResponse(json_data=payload), auth={"api_key": "k"})
    arts = await c.fetch_data()
    assert arts[0]["url_to_image"] is None


@pytest.mark.asyncio
async def test_fetch_nytimes_rate_limit():
    c = connected("nytimes", FakeResponse(status=429), auth={"api_key": "k"})
    with pytest.raises(ConnectionError):
        await c.fetch_data()
    assert "rate limit" in str(c.last_error)


# ---------------------------------------------------------------------------
# _fetch_reuters_data (RSS / feedparser)
# ---------------------------------------------------------------------------
RSS_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Reuters World</title>
  <item>
    <title>Reuters Story</title>
    <link>http://reuters/1</link>
    <description>desc body</description>
    <summary>sum</summary>
    <author>R Author</author>
    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""


@pytest.mark.asyncio
async def test_fetch_reuters_parses_feed():
    c = connected("reuters", FakeResponse(text_data=RSS_FEED))
    arts = await c.fetch_data(category="world", limit=5)
    assert len(arts) == 1
    a = arts[0]
    assert a["title"] == "Reuters Story"
    assert a["url"] == "http://reuters/1"
    assert a["source"] == "Reuters"
    assert a["service"] == "reuters"
    # default category 'world' selected from rss map
    assert "worldNews" in c._session.calls[0][0]


@pytest.mark.asyncio
async def test_fetch_reuters_unknown_category_defaults_world():
    c = connected("reuters", FakeResponse(text_data=RSS_FEED))
    await c.fetch_data(category="nonexistent")
    assert "worldNews" in c._session.calls[0][0]


@pytest.mark.asyncio
async def test_fetch_reuters_http_error():
    c = connected("reuters", FakeResponse(status=503))
    with pytest.raises(ConnectionError):
        await c.fetch_data()
    assert "Reuters RSS error" in str(c.last_error)


# ---------------------------------------------------------------------------
# _fetch_google_news_data (RSS)
# ---------------------------------------------------------------------------
GOOGLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <title>Google News</title>
  <item>
    <title>GN Story</title>
    <link>http://gnews/1</link>
    <summary>gsum</summary>
    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""


@pytest.mark.asyncio
async def test_fetch_google_news_with_query():
    c = connected("google_news", FakeResponse(text_data=GOOGLE_FEED))
    arts = await c.fetch_data(query="ai", country="us", language="en", limit=5)
    assert arts[0]["title"] == "GN Story"
    assert arts[0]["service"] == "google_news"
    assert "search?q=ai" in c._session.calls[0][0]


@pytest.mark.asyncio
async def test_fetch_google_news_no_query():
    c = connected("google_news", FakeResponse(text_data=GOOGLE_FEED))
    await c.fetch_data()
    url = c._session.calls[0][0]
    assert "search?q=" not in url
    assert url.startswith("https://news.google.com/rss?")


@pytest.mark.asyncio
async def test_fetch_google_news_http_error():
    c = connected("google_news", FakeResponse(status=500))
    with pytest.raises(ConnectionError):
        await c.fetch_data()
    assert "Google News RSS error" in str(c.last_error)


# ---------------------------------------------------------------------------
# _fetch_bing_news_data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fetch_bing_parses_articles():
    payload = {
        "value": [
            {
                "name": "Bing Story",
                "description": "bd",
                "url": "http://bing/1",
                "datePublished": "2024-05-05",
                "provider": [{"name": "Provider"}],
                "image": {"thumbnail": {"contentUrl": "http://bing/img"}},
            }
        ]
    }
    c = connected("bing_news", FakeResponse(json_data=payload), auth={"subscription_key": "s"})
    arts = await c.fetch_data(query="ai", category="tech", country="us")
    a = arts[0]
    assert a["title"] == "Bing Story"
    assert a["source"] == "Provider"
    assert a["url_to_image"] == "http://bing/img"
    assert a["service"] == "bing_news"


@pytest.mark.asyncio
async def test_fetch_bing_auth_error():
    c = connected("bing_news", FakeResponse(status=401), auth={"subscription_key": "s"})
    with pytest.raises(AuthenticationError):
        await c.fetch_data()
    assert isinstance(c.last_error, AuthenticationError)


@pytest.mark.asyncio
async def test_fetch_bing_other_error():
    c = connected("bing_news", FakeResponse(status=500), auth={"subscription_key": "s"})
    with pytest.raises(ConnectionError):
        await c.fetch_data()
    assert "Bing News API error" in str(c.last_error)


# ---------------------------------------------------------------------------
# validate_data_format
# ---------------------------------------------------------------------------
def test_validate_data_format_non_dict():
    assert make("reuters").validate_data_format("notadict") is False


def test_validate_data_format_missing_required():
    c = make("reuters")
    assert c.validate_data_format({"title": "t", "url": "u"}) is False  # no service
    assert c.validate_data_format({"title": "", "url": "u", "service": "s"}) is False


def test_validate_data_format_valid():
    c = make("reuters")
    assert c.validate_data_format(
        {"title": "t", "url": "http://x", "service": "reuters"}
    ) is True


# ---------------------------------------------------------------------------
# metadata helpers
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_available_categories():
    assert "technology" in await make("newsapi").get_available_categories()
    assert await make("unknown").get_available_categories() == []


@pytest.mark.asyncio
async def test_get_service_limits():
    limits = await make("newsapi").get_service_limits()
    assert limits["requires_api_key"] is True
    assert await make("unknown").get_service_limits() == {}
    reuters_limits = await make("reuters").get_service_limits()
    assert reuters_limits["requires_api_key"] is False
