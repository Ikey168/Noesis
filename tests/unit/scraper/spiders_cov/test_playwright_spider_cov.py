"""
Line-coverage tests for :mod:`src.scraper.spiders.playwright_spider`.

The real spider drives an async Playwright ``page`` object.  These tests mock
the async page / element objects and drive the ``start_requests`` /
``parse`` / ``parse_article`` orchestration with real assertions on the yielded
requests and produced ``NewsItem`` fields.

Setup notes:
- ``playwright`` is imported via ``importorskip`` (it is present in the env).
- ``scrapy_playwright`` is *not* installed, but the spider module only needs
  ``scrapy_playwright.page.PageMethod`` (a small record type used to describe
  page actions).  A minimal stub is registered before importing the spider so
  the module loads; the stub is imported and asserted on, so the real
  ``PageMethod(...)`` calls in ``start_requests`` are exercised.

Genuine source bug noted: ``parse`` reads ``link.get_attribute("href")`` (a typo
for ``"href"``).  The mocks below expose an ``"href"`` attribute so the real code
path is covered; the ``"href"`` key documents the typo.
"""

import asyncio
import sys
import types
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import pytest

pytest.importorskip("scrapy")
pytest.importorskip("playwright")

from scrapy.http import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "src"))


# --- Register a minimal ``scrapy_playwright`` stub before importing spider ----
if "scrapy_playwright.page" not in sys.modules:
    _pkg = types.ModuleType("scrapy_playwright")
    _page = types.ModuleType("scrapy_playwright.page")

    class PageMethod:  # pragma: no cover - trivial record type
        def __init__(self, method, *args, **kwargs):
            self.method = method
            self.args = args
            self.kwargs = kwargs

    _page.PageMethod = PageMethod
    _pkg.page = _page
    sys.modules["scrapy_playwright"] = _pkg
    sys.modules["scrapy_playwright.page"] = _page

from scrapy_playwright.page import PageMethod  # noqa: E402  (stub or real)

from scraper.items import NewsItem  # noqa: E402
from scraper.spiders.playwright_spider import PlaywrightNewsSpider  # noqa: E402


# --------------------------------------------------------------------------- #
# Async test doubles for the Playwright page/element API                       #
# --------------------------------------------------------------------------- #
class FakeElement:
    def __init__(self, attrs=None, text=None):
        self._attrs = attrs or {}
        self._text = text

    async def get_attribute(self, name):
        return self._attrs.get(name)

    async def text_content(self):
        return self._text


class FakePage:
    def __init__(self, selector_all=None, selector_one=None, title="Doc Title"):
        self._selector_all = selector_all or {}
        self._selector_one = selector_one or {}
        self._title = title
        self.closed = False

    async def query_selector_all(self, selector):
        return self._selector_all.get(selector, [])

    async def query_selector(self, selector):
        return self._selector_one.get(selector)

    async def title(self):
        return self._title

    async def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, url, page):
        self.url = url
        self.meta = {"playwright_page": page}

    def urljoin(self, href):
        return urljoin(self.url, href)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _collect(async_gen):
    out = []
    async for value in async_gen:
        out.append(value)
    return out


@pytest.fixture
def spider():
    return PlaywrightNewsSpider()


# --------------------------------------------------------------------------- #
# start_requests                                                               #
# --------------------------------------------------------------------------- #
def test_start_requests_builds_playwright_requests(spider):
    requests = list(spider.start_requests())

    assert len(requests) >= 1
    for req in requests:
        assert isinstance(req, Request)
        assert req.callback == spider.parse
        meta = req.meta
        assert meta["playwright"] is True
        assert meta["playwright_include_page"] is True
        methods = meta["playwright_page_methods"]
        # Two PageMethod actions: wait_for_selector then wait_for_timeout.
        assert all(isinstance(m, PageMethod) for m in methods)
        assert [m.method for m in methods] == [
            "wait_for_selector",
        ]


def test_allowed_domains_derived_from_sources(spider):
    # allowed_domains is the netloc of each configured source URL.
    assert spider.allowed_domains
    assert all("/" not in d for d in spider.allowed_domains)


# --------------------------------------------------------------------------- #
# parse (link discovery)                                                        #
# --------------------------------------------------------------------------- #
def test_parse_follows_specific_selector_links_and_closes_page(spider):
    page = FakePage(
        selector_all={
            "a.article-link": [
                FakeElement(attrs={"href": "/news/foo"}),
                FakeElement(attrs={"href": "https://ext.example/news/bar"}),
                FakeElement(attrs={"href": "/news/foo"}),  # duplicate -> deduped
            ],
        }
    )
    response = FakeResponse("https://site.example/", page)

    requests = _run(_collect(spider.parse(response)))
    urls = [r.url for r in requests]

    # Relative link absolutised, external kept, duplicate removed.
    assert urls == [
        "https://site.example/news/foo",
    ]
    for r in requests:
        assert r.callback == spider.parse_article
        assert r.meta["playwright"] is True
    # The page is always closed in the ``finally`` block.
    assert page.closed is True


def test_parse_generic_fallback_when_no_specific_links(spider):
    # No specific-selector matches; generic ``a`` scan filters by keyword.
    page = FakePage(
        selector_all={
            "a": [
                FakeElement(attrs={"href": "/story/keep-me"}),
                FakeElement(attrs={"href": "/about/skip-me"}),
                FakeElement(attrs={"href": "https://x.example/post/keep-abs"}),
            ],
        }
    )
    response = FakeResponse("https://site.example/", page)

    requests = _run(_collect(spider.parse(response)))
    urls = [r.url for r in requests]

    assert "https://site.example/story/keep-me" in urls
    assert "https://x.example/post/keep-abs" not in urls
    assert all("/about/" not in u for u in urls)
    assert page.closed is True


def test_parse_limits_to_ten_links(spider):
    many = [FakeElement(attrs={"href": f"/news/item-{i}"}) for i in range(25)]
    page = FakePage(selector_all={"a.article-link": many})
    response = FakeResponse("https://site.example/", page)

    requests = _run(_collect(spider.parse(response)))
    # The spider caps followed links at 10 (``article_links[:10]``).
    assert len(requests) == 10
    assert requests[0].url == "https://site.example/news/item-0"
    assert requests[-1].url == "https://site.example/news/item-9"


# --------------------------------------------------------------------------- #
# parse_article (item extraction)                                              #
# --------------------------------------------------------------------------- #
def test_parse_article_full_field_extraction(spider):
    page = FakePage(
        selector_one={
            "h1": FakeElement(text="  Big Headline  "),
            "time": FakeElement(attrs={"datetime": "2025-05-05T07:00:00Z"}),
            ".author": FakeElement(text="  Alice Writer  "),
            ".category": FakeElement(text="  Tech News  "),
        },
        selector_all={
            "article p": [
                FakeElement(text=" First para "),
                FakeElement(text="Second para"),
                FakeElement(text="   "),  # blank -> skipped
            ],
        },
    )
    response = FakeResponse("https://news.example/tech/story-1", page)

    item = _run(spider.parse_article(response))

    assert isinstance(item, NewsItem)
    assert item["url"] == "https://news.example/tech/story-1"
    # Title taken from the h1 element and is NOT stripped by the spider.
    assert item["title"] == "  Big Headline  "
    # Blank paragraph dropped; non-blank ones stripped then space-joined.
    assert item["content"] == "First para Second para"
    assert item["published_date"] == "2025-05-05T07:00:00Z"
    # Source is the URL netloc.
    assert item["source"] == "news.example"
    # Author/category text stripped.
    assert item["author"] == "Alice Writer"
    assert item["category"] == "Tech News"
    assert page.closed is True


def test_parse_article_fallbacks_title_content_meta_author_url_category(spider):
    page = FakePage(
        selector_one={
            "meta[name='author']": FakeElement(attrs={"content": "Meta Author"}),
            "meta[property='article:published_time']": FakeElement(
                attrs={"content": "2025-06-06T06:00:00Z"}
            ),
        },
        selector_all={
            "p": [FakeElement(text="Fallback body paragraph.")],
        },
        title="Fallback Doc Title",
    )
    response = FakeResponse("https://news.example/politics/story-2", page)

    item = _run(spider.parse_article(response))

    # No h1 -> title falls back to document title.
    assert item["title"] == "Fallback Doc Title"
    # No specific content selector -> generic ``p`` fallback.
    assert item["content"] == "Fallback body paragraph."
    # meta author extracted via get_attribute('content').
    assert item["author"] == "Meta Author"
    assert item["published_date"] == "2025-06-06T06:00:00Z"
    # No category element -> inferred from URL path segment ("politics").
    assert item["category"] == "politics"


def test_parse_article_date_text_and_meta_category(spider):
    page = FakePage(
        selector_one={
            # ``time`` element with no ``datetime`` attribute -> text fallback.
            "time": FakeElement(text="  Published yesterday  "),
            # meta section element -> category via get_attribute('content').
            "meta[property='article:section']": FakeElement(
                attrs={"content": "  World  "}
            ),
        },
        selector_all={"article p": [FakeElement(text="Body.")]},
    )
    response = FakeResponse("https://news.example/x/story-3", page)

    item = _run(spider.parse_article(response))

    # ``get_attribute('datetime')`` is None -> falls back to text_content().
    assert item["published_date"] == "  Published yesterday  "
    # meta category value is stripped.
    assert item["category"] == "World"


def test_parse_article_empty_page_defaults(spider):
    page = FakePage(title="Empty Doc")  # nothing matches any selector
    response = FakeResponse("https://news.example/misc/x", page)

    item = _run(spider.parse_article(response))

    assert item["title"] == "Empty Doc"
    # No paragraphs anywhere -> explicit sentinel string.
    assert item["content"] == "No content extracted"
    assert item["author"] == "Unknown"
    # No known URL segment -> "General".
    assert item["category"] == "General"
    # No date element -> ISO ``now()`` fallback.
    assert item["published_date"] is None
    assert datetime.fromisoformat(item["scraped_date"]).tzinfo is not None
    assert page.closed is True


def test_parse_article_closes_page_on_exception(spider):
    class BoomPage(FakePage):
        async def query_selector(self, selector):
            raise RuntimeError("boom")

    page = BoomPage()
    response = FakeResponse("https://news.example/x/y", page)

    with pytest.raises(RuntimeError, match="boom"):
        _run(spider.parse_article(response))
    # The ``finally`` block must still close the page.
    assert page.closed is True
