"""Tests for uncovered async fetch/scrape/session logic of
src/scraper/async_scraper_engine.py.

These exercise the aiohttp-backed code paths (link discovery, article fetching,
retry/error handling, the async context manager) by mocking the ClientSession,
and feed concrete HTML to the parser to assert on parsed Article fields.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

pytest.importorskip("aiohttp")
pytest.importorskip("bs4")

from scraper.async_scraper_engine import (  # noqa: E402
    Article,
    AsyncNewsScraperEngine,
    NewsSource,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ARTICLE_HTML = """
<html><body>
  <h1>A Genuinely Long And Descriptive Headline About Technology</h1>
  <div class="author">Jane Reporter</div>
  <time>2026-01-01</time>
  <article>
    <p>{para}</p>
    <p>Second paragraph with even more substantial textual content here.</p>
  </article>
</body></html>
""".format(para="This is the first paragraph of body text. " * 10)


def make_source(**over):
    base = dict(
        name="TestSource",
        base_url="https://example.com/news",
        article_selectors={
            "title": "h1",
            "content": "article p",
            "author": ".author",
            "date": "time",
        },
        link_patterns=[r"/news/.*"],
        requires_js=False,
        rate_limit=1.0,
    )
    base.update(over)
    return NewsSource(**base)


class _FakeResponse:
    """Async-context-manager mimicking aiohttp response."""

    def __init__(self, status=200, text=""):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def install_fake_session(engine, response=None, side_effect=None):
    """Attach a fake aiohttp session whose .get() returns/raises as configured."""
    session = MagicMock()
    if side_effect is not None:
        session.get = MagicMock(side_effect=side_effect)
    else:
        session.get = MagicMock(return_value=response)
    session.close = AsyncMock()
    engine.session = session
    return session


@pytest.fixture
def engine():
    eng = AsyncNewsScraperEngine(enable_monitoring=False)
    yield eng
    eng.monitor.stop()


# ---------------------------------------------------------------------------
# parse_article_html
# ---------------------------------------------------------------------------

class TestParseArticleHtml:
    def test_parses_valid_article(self, engine):
        src = make_source()
        art = engine.parse_article_html(
            ARTICLE_HTML, "https://example.com/news/tech/123", src
        )
        assert isinstance(art, Article)
        assert art.title == "A Genuinely Long And Descriptive Headline About Technology"
        assert "first paragraph" in art.content
        assert art.author == "Jane Reporter"
        assert art.published_date == "2026-01-01"
        assert art.source == "TestSource"
        assert art.url == "https://example.com/news/tech/123"
        assert art.category == "Technology"  # from "tech" in url
        assert art.word_count > 0
        assert art.reading_time >= 1
        assert art.validation_score >= 50
        assert art.content_quality in {"high", "medium", "low"}

    def test_missing_title_returns_none(self, engine):
        src = make_source()
        html = "<html><body><article><p>Some content paragraph here.</p></article></body></html>"
        assert engine.parse_article_html(html, "https://example.com/x", src) is None

    def test_missing_content_returns_none(self, engine):
        src = make_source()
        html = "<html><body><h1>Headline With Sufficient Length Here</h1></body></html>"
        assert engine.parse_article_html(html, "https://example.com/x", src) is None

    def test_low_quality_filtered_out(self, engine):
        # A parsed article whose validation score falls below the 50 threshold
        # is dropped (returns None). The normal scoring of a present-title +
        # present-content article never drops below 50, so force a low score to
        # exercise the `validation_score >= 50` rejection branch directly.
        src = make_source()
        engine.validate_article = MagicMock(return_value=10)
        html = ARTICLE_HTML
        assert engine.parse_article_html(html, "https://example.com/x", src) is None
        assert engine.validate_article.called

    def test_default_author_when_missing(self, engine):
        src = make_source()
        html = """<html><body>
          <h1>A Genuinely Long And Descriptive Headline Without Any Author</h1>
          <article><p>{p}</p></article>
        </body></html>""".format(p="Substantial body content text here. " * 10)
        art = engine.parse_article_html(html, "https://example.com/news/1", src)
        assert art is not None
        assert art.author == "TestSource Sta"


# ---------------------------------------------------------------------------
# extract_content_by_selector
# ---------------------------------------------------------------------------

class TestExtractContentBySelector:
    def test_joins_multiple_elements(self, engine):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<div><p>One</p><p>Two</p></div>", "html.parser")
        assert engine.extract_content_by_selector(soup, "p") == "One Two"

    def test_empty_when_no_match(self, engine):
        from bs4 import BeautifulSoup

        soup = BeautifulSoup("<div></div>", "html.parser")
        assert engine.extract_content_by_selector(soup, "p") == ""


# ---------------------------------------------------------------------------
# get_article_links_http
# ---------------------------------------------------------------------------

class TestGetArticleLinksHttp:
    @pytest.mark.asyncio
    async def test_returns_links_on_200(self, engine):
        src = make_source(base_url="https://example.com")
        html = """<html><body>
          <a href="https://example.com/news/1">One</a>
          <a href="https://example.com/news/2">Two</a>
          <a href="https://example.com/about">Skip</a>
        </body></html>"""
        # Patch the (CPU-bound) link extractor to a deterministic value rather
        # than depend on its internal regex behaviour.
        engine.extract_links_from_html = MagicMock(
            return_value=["https://example.com/news/1", "https://example.com/news/2"]
        )
        install_fake_session(engine, response=_FakeResponse(200, html))

        links = await engine.get_article_links_http(src)
        assert links == [
            "https://example.com/news/1",
            "https://example.com/news/2",
        ]
        engine.extract_links_from_html.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_200_returns_empty(self, engine):
        src = make_source()
        install_fake_session(engine, response=_FakeResponse(404, ""))
        assert await engine.get_article_links_http(src) == []

    @pytest.mark.asyncio
    async def test_captcha_short_circuits(self, engine):
        src = make_source()
        engine.captcha_solver = MagicMock()  # presence triggers captcha branch
        install_fake_session(
            engine, response=_FakeResponse(200, '<html><div class="g-recaptcha"></div></html>')
        )
        assert await engine.get_article_links_http(src) == []

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, engine):
        src = make_source()
        install_fake_session(engine, side_effect=RuntimeError("boom"))
        assert await engine.get_article_links_http(src) == []


# ---------------------------------------------------------------------------
# scrape_article_http
# ---------------------------------------------------------------------------

class TestScrapeArticleHttp:
    @pytest.mark.asyncio
    async def test_returns_parsed_article(self, engine):
        src = make_source()
        install_fake_session(engine, response=_FakeResponse(200, ARTICLE_HTML))
        sem = asyncio.Semaphore(1)
        art = await engine.scrape_article_http(
            sem, src, "https://example.com/news/tech/1"
        )
        assert isinstance(art, Article)
        assert art.title.startswith("A Genuinely Long")

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self, engine):
        src = make_source()
        install_fake_session(engine, response=_FakeResponse(500, ""))
        sem = asyncio.Semaphore(1)
        assert await engine.scrape_article_http(sem, src, "https://example.com/x") is None

    @pytest.mark.asyncio
    async def test_captcha_returns_none(self, engine):
        src = make_source()
        engine.captcha_solver = MagicMock()
        install_fake_session(
            engine, response=_FakeResponse(200, '<html><div class="cf-turnstile"></div></html>')
        )
        sem = asyncio.Semaphore(1)
        assert await engine.scrape_article_http(sem, src, "https://example.com/x") is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, engine):
        src = make_source()
        install_fake_session(engine, side_effect=RuntimeError("network down"))
        sem = asyncio.Semaphore(1)
        assert await engine.scrape_article_http(sem, src, "https://example.com/x") is None


# ---------------------------------------------------------------------------
# scrape_http_source  (end-to-end with mocked sub-steps)
# ---------------------------------------------------------------------------

class TestScrapeHttpSource:
    @pytest.mark.asyncio
    async def test_collects_valid_articles(self, engine):
        src = make_source()
        links = ["https://example.com/news/1", "https://example.com/news/2"]
        engine.get_article_links_http = AsyncMock(return_value=links)

        good = Article(
            title="A Genuinely Long Headline", url="u", content="c" * 200,
            author="A", published_date="d", source=src.name, scraped_date="s",
        )

        async def fake_scrape(sem, source, link):
            return good

        engine.scrape_article_http = AsyncMock(side_effect=fake_scrape)

        result = await engine.scrape_http_source(src)
        assert len(result) == 2
        assert all(isinstance(a, Article) for a in result)
        # both new links should be marked seen
        assert links[0] in engine.seen_urls

    @pytest.mark.asyncio
    async def test_dedupes_seen_urls(self, engine):
        src = make_source()
        engine.seen_urls.add("https://example.com/news/1")
        engine.get_article_links_http = AsyncMock(
            return_value=["https://example.com/news/1"]
        )
        engine.scrape_article_http = AsyncMock(
            return_value=Article(
                title="t", url="u", content="c", author="a",
                published_date="d", source="s", scraped_date="s",
            )
        )
        result = await engine.scrape_http_source(src)
        assert result == []
        engine.scrape_article_http.assert_not_called()

    @pytest.mark.asyncio
    async def test_filters_non_articles(self, engine):
        src = make_source()
        engine.get_article_links_http = AsyncMock(
            return_value=["https://example.com/news/1", "https://example.com/news/2"]
        )

        results = [
            Article(title="t", url="u", content="c", author="a",
                    published_date="d", source="s", scraped_date="s"),
            None,
        ]

        async def fake(sem, source, link):
            return results.pop(0)

        engine.scrape_article_http = AsyncMock(side_effect=fake)
        out = await engine.scrape_http_source(src)
        assert len(out) == 1

    @pytest.mark.asyncio
    async def test_link_discovery_exception_returns_empty(self, engine):
        src = make_source()
        engine.get_article_links_http = AsyncMock(side_effect=RuntimeError("fail"))
        assert await engine.scrape_http_source(src) == []


# ---------------------------------------------------------------------------
# scrape_source_async / scrape_sources_async  routing
# ---------------------------------------------------------------------------

class TestScrapeSourceRouting:
    @pytest.mark.asyncio
    async def test_http_route(self, engine):
        src = make_source(requires_js=False)
        engine.scrape_http_source = AsyncMock(return_value=["http-article"])
        engine.scrape_js_source = AsyncMock(return_value=["js-article"])
        result = await engine.scrape_source_async(src)
        assert result == ["http-article"]
        engine.scrape_js_source.assert_not_called()

    @pytest.mark.asyncio
    async def test_js_route(self, engine):
        src = make_source(requires_js=True)
        engine.scrape_http_source = AsyncMock(return_value=["http-article"])
        engine.scrape_js_source = AsyncMock(return_value=["js-article"])
        result = await engine.scrape_source_async(src)
        assert result == ["js-article"]
        engine.scrape_http_source.assert_not_called()

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self, engine):
        src = make_source(requires_js=False)
        engine.scrape_http_source = AsyncMock(side_effect=RuntimeError("oops"))
        assert await engine.scrape_source_async(src) == []

    @pytest.mark.asyncio
    async def test_scrape_sources_aggregates(self, engine):
        s1, s2 = make_source(name="S1"), make_source(name="S2")
        engine.scrape_source_async = AsyncMock(
            side_effect=[["a1"], ["a2", "a3"]]
        )
        out = await engine.scrape_sources_async([s1, s2])
        assert out == ["a1", "a2", "a3"]

    @pytest.mark.asyncio
    async def test_scrape_sources_handles_exception(self, engine):
        s1, s2 = make_source(name="S1"), make_source(name="S2")

        async def boom(source):
            if source.name == "S1":
                raise RuntimeError("nope")
            return ["ok"]

        engine.scrape_source_async = AsyncMock(side_effect=boom)
        out = await engine.scrape_sources_async([s1, s2])
        assert out == ["ok"]


# ---------------------------------------------------------------------------
# get_rate_limiter
# ---------------------------------------------------------------------------

class TestRateLimiter:
    def test_creates_and_caches_semaphore(self, engine):
        src = make_source(rate_limit=2.0)
        sem1 = engine.get_rate_limiter(src)
        sem2 = engine.get_rate_limiter(src)
        assert isinstance(sem1, asyncio.Semaphore)
        assert sem1 is sem2  # cached by source name

    def test_minimum_one_permit(self, engine):
        src = make_source(rate_limit=0.0)
        sem = engine.get_rate_limiter(src)
        # rate_limit 0 -> max(1, 0) -> at least one permit
        assert sem._value >= 1


# ---------------------------------------------------------------------------
# async context manager (__aenter__ / __aexit__)
# ---------------------------------------------------------------------------

class TestAsyncContextManager:
    @pytest.mark.asyncio
    async def test_aenter_calls_start_and_returns_self(self, engine):
        engine.start = AsyncMock()
        result = await engine.__aenter__()
        assert result is engine
        engine.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_aexit_calls_close(self, engine):
        engine.close = AsyncMock()
        await engine.__aexit__(None, None, None)
        engine.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_closes_session_and_pool(self, engine):
        session = MagicMock()
        session.close = AsyncMock()
        engine.session = session
        engine.browser_contexts = []
        engine.browser = None
        engine.playwright = None
        engine.thread_pool = MagicMock()

        await engine.close()
        session.close.assert_awaited_once()
        engine.thread_pool.shutdown.assert_called_once()


# ---------------------------------------------------------------------------
# scrape_js_source guard (no browser contexts available)
# ---------------------------------------------------------------------------

class TestScrapeJsSourceGuard:
    @pytest.mark.asyncio
    async def test_no_contexts_returns_empty(self, engine):
        src = make_source(requires_js=True)
        engine.browser_contexts = []
        assert await engine.scrape_js_source(src) == []


# ---------------------------------------------------------------------------
# asyncio.sleep patched (guards against accidental real delays in routing)
# ---------------------------------------------------------------------------

class TestNoRealSleep:
    @pytest.mark.asyncio
    async def test_routing_does_not_sleep(self, engine):
        src = make_source(requires_js=False)
        engine.scrape_http_source = AsyncMock(return_value=[])
        with patch("asyncio.sleep", new=AsyncMock()) as slept:
            await engine.scrape_source_async(src)
        # routing path performs no sleeps
        slept.assert_not_called()
