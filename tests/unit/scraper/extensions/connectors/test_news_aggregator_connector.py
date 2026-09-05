"""
Test cases for news aggregator connector functionality.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import aiohttp
from aioresponses import aioresponses

from src.scraper.extensions.connectors.news_aggregator_connector import NewsAggregatorConnector
from src.scraper.extensions.connectors.base import ConnectionError, AuthenticationError


class TestNewsAggregatorConnector:
    """Test cases for NewsAggregatorConnector class."""

    def test_init(self):
        """Test news aggregator connector initialization."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "test_key"}
        
        connector = NewsAggregatorConnector(config, auth_config)
        
        assert connector.config == config
        assert connector.auth_config == auth_config
        assert connector.service == "newsapi"
        assert connector._session is None

    def test_validate_config_newsapi_success(self):
        """Test successful config validation for NewsAPI."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        
        result = connector.validate_config()
        assert result is True

    def test_validate_config_guardian_success(self):
        """Test successful config validation for Guardian API."""
        config = {"service": "guardian"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        
        result = connector.validate_config()
        assert result is True

    def test_validate_config_reuters_success(self):
        """Test successful config validation for Reuters (no API key required)."""
        config = {"service": "reuters"}
        connector = NewsAggregatorConnector(config)
        
        result = connector.validate_config()
        assert result is True

    def test_validate_config_google_news_success(self):
        """Test successful config validation for Google News."""
        config = {"service": "google_news"}
        connector = NewsAggregatorConnector(config)
        
        result = connector.validate_config()
        assert result is True

    def test_validate_config_missing_service(self):
        """Test config validation with missing service."""
        config = {}
        connector = NewsAggregatorConnector(config)
        
        result = connector.validate_config()
        assert result is False

    def test_validate_config_newsapi_missing_key(self):
        """Test NewsAPI config validation with missing API key."""
        config = {"service": "newsapi"}
        auth_config = {}
        connector = NewsAggregatorConnector(config, auth_config)
        
        result = connector.validate_config()
        assert result is False

    @pytest.mark.asyncio
    async def test_connect_newsapi_success(self):
        """Test successful NewsAPI connection."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        
        result = await connector.connect()
        
        assert result is True
        assert connector.is_connected
        assert connector._headers["X-API-Key"] == "test_key"

    @pytest.mark.asyncio
    async def test_connect_guardian_success(self):
        """Test successful Guardian API connection."""
        config = {"service": "guardian"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        
        result = await connector.connect()
        
        assert result is True
        assert connector.is_connected

    @pytest.mark.asyncio
    async def test_connect_reuters_success(self):
        """Test successful Reuters RSS connection."""
        config = {"service": "reuters"}
        connector = NewsAggregatorConnector(config)
        
        result = await connector.connect()
        
        assert result is True
        assert connector.is_connected
        assert "rss" in connector._headers["Accept"].lower()

    @pytest.mark.asyncio
    async def test_disconnect(self):
        """Test news aggregator disconnection."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        
        # Create a mock session
        mock_session = AsyncMock()
        mock_session.closed = False
        connector._session = mock_session
        connector._connected = True
        
        await connector.disconnect()
        
        assert not connector.is_connected
        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_data_newsapi_success(self):
        """Test successful NewsAPI data fetching."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        connector._connected = True
        connector._session = _real_http_session()
        
        newsapi_response = {
            "articles": [
                {
                    "title": "Test Article",
                    "description": "Test description",
                    "content": "Test content",
                    "url": "https://example.com/article",
                    "publishedAt": "2024-01-01T00:00:00Z",
                    "author": "Test Author",
                    "source": {"name": "Test Source"},
                    "urlToImage": "https://example.com/image.jpg"
                }
            ]
        }
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://newsapi.org/v2/top-headlines"),
                  status=200, payload=newsapi_response)
            
            data = await connector.fetch_data()
            
            assert len(data) == 1
            assert data[0]["title"] == "Test Article"
            assert data[0]["service"] == "newsapi"

    @pytest.mark.asyncio
    async def test_fetch_data_newsapi_with_query(self):
        """Test NewsAPI data fetching with search query."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        connector._connected = True
        connector._session = _real_http_session()
        
        newsapi_response = {
            "articles": [
                {
                    "title": "Search Result Article",
                    "description": "Search description",
                    "url": "https://example.com/search-article",
                    "publishedAt": "2024-01-01T00:00:00Z",
                    "source": {"name": "Search Source"}
                }
            ]
        }
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://newsapi.org/v2/everything"),
                  status=200, payload=newsapi_response)
            
            data = await connector.fetch_data(query="test search")
            
            assert len(data) == 1
            assert data[0]["title"] == "Search Result Article"

    @pytest.mark.asyncio
    async def test_fetch_data_guardian_success(self):
        """Test successful Guardian API data fetching."""
        config = {"service": "guardian"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        connector._connected = True
        connector._session = _real_http_session()
        
        guardian_response = {
            "response": {
                "results": [
                    {
                        "webTitle": "Guardian Article",
                        "webUrl": "https://theguardian.com/article",
                        "id": "world/2026/sep/01/article",
                        "webPublicationDate": "2024-01-01T00:00:00Z",
                        "sectionName": "World",
                        "fields": {
                            "headline": "Guardian Headline",
                            "trailText": "Guardian trail text",
                            "body": "Guardian body",
                            "byline": "Guardian Author",
                            "thumbnail": "https://theguardian.com/image.jpg"
                        },
                        "tags": [
                            {"type": "contributor", "webTitle": "Test Author"}
                        ]
                    }
                ]
            }
        }
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://content.guardianapis.com/search"),
                  status=200, payload=guardian_response)
            
            data = await connector.fetch_data()
            
            assert len(data) == 1
            assert data[0]["title"] == "Guardian Headline"
            assert data[0]["service"] == "guardian"
            assert data[0]["author"] == "Test Author"

    @pytest.mark.asyncio
    async def test_fetch_data_reuters_success(self):
        """Test successful Reuters RSS data fetching."""
        config = {"service": "reuters"}
        connector = NewsAggregatorConnector(config)
        connector._connected = True
        connector._session = _real_http_session()
        
        rss_content = """<?xml version="1.0"?>
        <rss version="2.0">
            <channel>
                <title>Reuters</title>
                <item>
                    <title>Reuters Article</title>
                    <link>https://reuters.com/article</link>
                    <description>Reuters description</description>
                    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
                    <author>Reuters Author</author>
                </item>
            </channel>
        </rss>"""
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://feeds.reuters.com/reuters/worldNews"),
                  status=200, body=rss_content)
            
            data = await connector.fetch_data()
            
            assert len(data) == 1
            assert data[0]["title"] == "Reuters Article"
            assert data[0]["service"] == "reuters"

    @pytest.mark.asyncio
    async def test_fetch_data_google_news_success(self):
        """Test successful Google News RSS data fetching."""
        config = {"service": "google_news"}
        connector = NewsAggregatorConnector(config)
        connector._connected = True
        connector._session = _real_http_session()
        
        google_rss_content = """<?xml version="1.0"?>
        <rss version="2.0">
            <channel>
                <title>Google News</title>
                <item>
                    <title>Google News Article</title>
                    <link>https://news.google.com/article</link>
                    <description>Google News description</description>
                    <pubDate>Mon, 01 Jan 2024 00:00:00 GMT</pubDate>
                    <source url="https://example.com">Example Source</source>
                </item>
            </channel>
        </rss>"""
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://news.google.com/rss?hl=en&gl=us&ceid=us:en"),
                  status=200, body=google_rss_content)
            
            data = await connector.fetch_data()
            
            assert len(data) == 1
            assert data[0]["title"] == "Google News Article"
            assert data[0]["service"] == "google_news"

    @pytest.mark.asyncio
    async def test_fetch_data_not_connected(self):
        """Test fetching data when not connected."""
        config = {"service": "newsapi"}
        connector = NewsAggregatorConnector(config)
        
        with pytest.raises(ConnectionError, match="Not connected to newsapi"):
            await connector.fetch_data()

    @pytest.mark.asyncio
    async def test_fetch_data_auth_error(self):
        """Test fetching data with authentication error."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "invalid_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        connector._connected = True
        connector._session = _real_http_session()
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://newsapi.org/v2/top-headlines"), status=401)
            
            with pytest.raises(AuthenticationError, match="NewsAPI authentication failed"):
                await connector.fetch_data()

    @pytest.mark.asyncio
    async def test_fetch_data_rate_limit_error(self):
        """Test fetching data with rate limit error."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        connector._connected = True
        connector._session = _real_http_session()
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://newsapi.org/v2/top-headlines"), status=429)
            
            with pytest.raises(ConnectionError, match="NewsAPI rate limit exceeded"):
                await connector.fetch_data()

    @pytest.mark.asyncio
    async def test_fetch_data_unsupported_service(self):
        """Test fetching data from unsupported service."""
        config = {"service": "unsupported"}
        connector = NewsAggregatorConnector(config)
        connector._connected = True
        
        with pytest.raises(ConnectionError, match="Unsupported service"):
            await connector.fetch_data()

    def test_validate_data_format_success(self):
        """Test successful data format validation."""
        config = {"service": "newsapi"}
        connector = NewsAggregatorConnector(config)
        
        data = {
            "title": "Test Article",
            "url": "https://example.com/article",
            "service": "newsapi"
        }
        
        result = connector.validate_data_format(data)
        assert result is True

    def test_validate_data_format_missing_required_fields(self):
        """Test data format validation with missing required fields."""
        config = {"service": "newsapi"}
        connector = NewsAggregatorConnector(config)
        
        data = {"title": "Test Article"}  # Missing url and service
        
        result = connector.validate_data_format(data)
        assert result is False

    def test_validate_data_format_empty_required_fields(self):
        """Test data format validation with empty required fields."""
        config = {"service": "newsapi"}
        connector = NewsAggregatorConnector(config)
        
        data = {
            "title": "",  # Empty title
            "url": "https://example.com/article",
            "service": "newsapi"
        }
        
        result = connector.validate_data_format(data)
        assert result is False

    def test_validate_data_format_non_dict(self):
        """Test data format validation with non-dictionary data."""
        config = {"service": "newsapi"}
        connector = NewsAggregatorConnector(config)
        
        result = connector.validate_data_format("not a dict")
        assert result is False

    @pytest.mark.asyncio
    async def test_get_available_categories_newsapi(self):
        """Test getting available categories for NewsAPI."""
        config = {"service": "newsapi"}
        connector = NewsAggregatorConnector(config)
        
        categories = await connector.get_available_categories()
        
        assert "business" in categories
        assert "technology" in categories
        assert "sports" in categories

    @pytest.mark.asyncio
    async def test_get_available_categories_guardian(self):
        """Test getting available categories for Guardian."""
        config = {"service": "guardian"}
        connector = NewsAggregatorConnector(config)
        
        categories = await connector.get_available_categories()
        
        assert "world" in categories
        assert "politics" in categories
        assert "tech" in categories

    @pytest.mark.asyncio
    async def test_get_available_categories_unknown_service(self):
        """Test getting categories for unknown service."""
        config = {"service": "unknown"}
        connector = NewsAggregatorConnector(config)
        
        categories = await connector.get_available_categories()
        
        assert categories == []

    @pytest.mark.asyncio
    async def test_get_service_limits_newsapi(self):
        """Test getting service limits for NewsAPI."""
        config = {"service": "newsapi"}
        connector = NewsAggregatorConnector(config)
        
        limits = await connector.get_service_limits()
        
        assert "requests_per_day" in limits
        assert limits["requests_per_day"] == 1000
        assert limits["requires_api_key"] is True

    @pytest.mark.asyncio
    async def test_get_service_limits_reuters(self):
        """Test getting service limits for Reuters."""
        config = {"service": "reuters"}
        connector = NewsAggregatorConnector(config)
        
        limits = await connector.get_service_limits()
        
        assert limits["requires_api_key"] is False
        assert limits["requests_per_day"] == "unlimited"

    @pytest.mark.asyncio
    async def test_get_service_limits_unknown_service(self):
        """Test getting limits for unknown service."""
        config = {"service": "unknown"}
        connector = NewsAggregatorConnector(config)
        
        limits = await connector.get_service_limits()
        
        assert limits == {}

    @pytest.mark.asyncio
    async def test_fetch_data_with_date_filters(self):
        """Test fetching data with date filters."""
        config = {"service": "newsapi"}
        auth_config = {"api_key": "test_key"}
        connector = NewsAggregatorConnector(config, auth_config)
        connector._connected = True
        connector._session = _real_http_session()
        
        newsapi_response = {"articles": []}
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://newsapi.org/v2/everything"),
                  status=200, payload=newsapi_response)
            
            data = await connector.fetch_data(
                query="test",
                from_date="2024-01-01",
                to_date="2024-01-31"
            )
            
            assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_fetch_data_reuters_with_category(self):
        """Test fetching Reuters data with specific category."""
        config = {"service": "reuters"}
        connector = NewsAggregatorConnector(config)
        connector._connected = True
        connector._session = _real_http_session()
        
        rss_content = """<?xml version="1.0"?>
        <rss version="2.0">
            <channel>
                <title>Reuters Business</title>
                <item>
                    <title>Business Article</title>
                    <link>https://reuters.com/business-article</link>
                    <description>Business description</description>
                </item>
            </channel>
        </rss>"""
        
        with aioresponses() as m:
            _mock_get(m, _match_url("https://feeds.reuters.com/reuters/businessNews"),
                  status=200, body=rss_content)
            
            data = await connector.fetch_data(category="business")
            
            assert len(data) == 1
            assert data[0]["title"] == "Business Article"


# Real aiohttp sessions are required for aioresponses to intercept requests.
import aiohttp
import re
import pytest_asyncio
_HTTP_SESSIONS = []

def _real_http_session():
    session = aiohttp.ClientSession()
    _HTTP_SESSIONS.append(session)
    return session

def _match_url(url):
    if '?' in url:
        from aioresponses.core import normalize_url
        return normalize_url(url)
    return re.compile('^' + re.escape(url) + r'(?:\?.*)?$')

@pytest_asyncio.fixture(autouse=True)
async def _close_http_sessions():
    yield
    while _HTTP_SESSIONS:
        await _HTTP_SESSIONS.pop().close()


class _MockClientResponse(aiohttp.ClientResponse):
    def __init__(self, *args, **kwargs):
        import inspect
        if 'stream_writer' in inspect.signature(aiohttp.ClientResponse).parameters:
            from types import SimpleNamespace
            kwargs.setdefault('stream_writer', SimpleNamespace(output_size=0))
        super().__init__(*args, **kwargs)

def _mock_get(mock, *args, **kwargs):
    return mock.get(*args, response_class=_MockClientResponse, **kwargs)
