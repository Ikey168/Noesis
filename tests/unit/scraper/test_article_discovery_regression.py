from types import SimpleNamespace
from src.scraper.article_links import extract_links, scoped_url
import pytest


def test_scoped_stable_discovery_and_empty_invalid_pages():
    source=SimpleNamespace(base_url='https://example.org/section/',link_patterns=[r'/news/'])
    html='<a href="/news/one#top">one</a><a href="/news/one">duplicate</a><a href="https://elsewhere.org/news/two">other</a><a href="mailto:x@example.org">email</a><a href="/about">about</a>'
    assert extract_links(html,source)==['https://example.org/news/one']
    assert extract_links('<html><body></body></html>',source)==[]
    with pytest.raises(ValueError,match='discovery_invalid_html'):extract_links('not html',source)
    assert scoped_url(source.base_url,'../news/next')=='https://example.org/news/next'


def test_http_failure_allows_one_browser_attempt():
    import asyncio
    from unittest.mock import AsyncMock
    from src.scraper.async_scraper_engine import AsyncNewsScraperEngine, Article, NewsSource
    async def run():
        engine=AsyncNewsScraperEngine(enable_monitoring=False)
        source=NewsSource('local','https://example.org',{},[r'/news/'])
        url='https://example.org/news/one'
        engine.get_article_links_http=AsyncMock(return_value=[url,url])
        engine.get_article_links_js=AsyncMock(return_value=[url,url])
        engine.scrape_article_http=AsyncMock(return_value=None)
        expected=Article('Title',url,'Body','Author',None,'local','2026-09-05')
        engine.scrape_article_js=AsyncMock(return_value=expected)
        engine.browser_contexts=[object()]
        try:
            assert await engine.scrape_http_source(source)==[]
            assert engine.url_states[url]=='retryable-failed'
            assert await engine.scrape_js_source(source)==[expected]
            assert await engine.scrape_js_source(source)==[]
            assert engine.scrape_article_http.await_count==1
            assert engine.scrape_article_js.await_count==1
        finally:
            engine.browser_contexts=[]
            await engine.close()
    asyncio.run(run())
