"""
Coverage tests for GuardianSpider.

Follows the style of tests/unit/scraper/test_bbc_spider.py and
test_npr_spider.py: builds real ``HtmlResponse`` objects from representative
HTML and asserts on parsed article fields, link extraction, and the
empty / missing-element branches.

NOTE ON A GENUINE SOURCE BUG (documented, not fixed):
The link selector combines two groups where only the second carries
``::attr(href)`` (``'a[href*="/2024/"], a[href*="/2025/"]::attr(href)'``). Links
matched only by the ``/2024/`` group are returned as element-serialization
strings rather than hrefs and get urljoined into a mangled request URL. Only
``/2025/`` links flow through the working path.
"""

import pytest

pytest.importorskip("scrapy")

from scrapy.http import HtmlResponse

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent.parent / "src"))

from scraper.spiders.guardian_spider import GuardianSpider
from scraper.items import NewsItem


def _response(url, html):
    return HtmlResponse(url=url, body=html.encode("utf-8"))


class TestGuardianSpider:
    """Test suite for GuardianSpider."""

    @pytest.fixture
    def spider(self):
        return GuardianSpider()

    def test_spider_initialization(self, spider):
        assert spider.name == "guardian"
        assert "theguardian.com" in spider.allowed_domains
        assert "https://www.theguardian.com/" in spider.start_urls

    def test_parse_2025_links(self, spider):
        """/2025/ links (absolute and relative) are requested with parse_article."""
        html = """
        <html><body>
            <a href="https://www.theguardian.com/world/2025/jul/01/story-a">A</a>
            <a href="/technology/2025/jul/02/story-b">B relative</a>
        </body></html>
        """
        requests = list(spider.parse(_response("https://www.theguardian.com/", html)))
        urls = [r.url for r in requests]
        assert "https://www.theguardian.com/world/2025/jul/01/story-a" in urls
        assert "https://www.theguardian.com/technology/2025/jul/02/story-b" in urls
        for r in requests:
            assert r.callback == spider.parse_article

    def test_parse_2024_link_is_clean(self, spider):
        """GENUINE BUG: a /2024/-only link is returned as an element string and
        gets urljoined into a mangled, percent-encoded URL rather than the clean
        article URL."""
        html = """
        <html><body>
            <a href="https://www.theguardian.com/world/2024/jul/01/story-a">2024</a>
        </body></html>
        """
        requests = list(spider.parse(_response("https://www.theguardian.com/", html)))
        urls = [r.url for r in requests]
        assert urls == ["https://www.theguardian.com/world/2024/jul/01/story-a"]

    def test_parse_article_full_fields(self, spider):
        html = """
        <html><body>
            <h1 data-gu-name="headline">Guardian Title</h1>
            <time datetime="2025-07-01T09:00:00Z">Jul 1</time>
            <a rel="author">Carol Guardian</a>
            <div data-gu-name="body"><p>Guardian para one.</p><p>Guardian para two.</p></div>
        </body></html>
        """
        url = "https://www.theguardian.com/technology/2025/jul/01/story"
        items = list(spider.parse_article(_response(url, html)))

        assert len(items) == 1
        item = items[0]
        assert isinstance(item, NewsItem)
        assert item["title"] == "Guardian Title"
        assert item["url"] == url
        assert item["source"] == "The Guardian"
        assert item["author"] == "Carol Guardian"
        # time::attr(datetime) is passed through unchanged.
        assert item["published_date"] == "2025-07-01T09:00:00Z"
        assert item["category"] == "Technology"
        assert item["content"] == "Guardian para one. Guardian para two."

    def test_parse_article_content_headline_and_body_fallbacks(self, spider):
        """Title via h1.content__headline; content via .content__article-body p."""
        html = """
        <html><body>
            <h1 class="content__headline">Legacy Headline</h1>
            <div class="content__article-body"><p>Legacy body paragraph.</p></div>
        </body></html>
        """
        url = "https://www.theguardian.com/world/2025/jul/01/story"
        item = list(spider.parse_article(_response(url, html)))[0]
        assert item["title"] == "Legacy Headline"
        assert item["content"] == "Legacy body paragraph."

    def test_parse_article_article_p_content_fallback(self, spider):
        """Content via the generic 'article p' fallback selector."""
        html = """
        <html><body>
            <h1>Plain Title</h1>
            <article><p>Generic article paragraph.</p></article>
        </body></html>
        """
        url = "https://www.theguardian.com/world/2025/jul/01/story"
        item = list(spider.parse_article(_response(url, html)))[0]
        assert item["title"] == "Plain Title"
        assert item["content"] == "Generic article paragraph."

    def test_parse_article_default_author(self, spider):
        """No author element -> default author (note the source typo
        "Guardian Sta")."""
        html = """
        <html><body>
            <h1 data-gu-name="headline">No Author</h1>
            <div data-gu-name="body"><p>Body.</p></div>
        </body></html>
        """
        url = "https://www.theguardian.com/world/2025/jul/01/story"
        item = list(spider.parse_article(_response(url, html)))[0]
        assert item["author"] == "Guardian Sta"

    def test_parse_article_byline_author_fallback(self, spider):
        """Author falls back to the .byline a text when there is no rel=author."""
        html = """
        <html><body>
            <h1 data-gu-name="headline">Byline Author</h1>
            <div class="byline"><a>Byline Writer</a></div>
            <div data-gu-name="body"><p>Body.</p></div>
        </body></html>
        """
        url = "https://www.theguardian.com/world/2025/jul/01/story"
        item = list(spider.parse_article(_response(url, html)))[0]
        assert item["author"] == "Byline Writer"

    def test_parse_article_missing_date_stays_unknown(self, spider):
        html = """
        <html><body>
            <h1 data-gu-name="headline">No Date</h1>
            <div data-gu-name="body"><p>Body.</p></div>
        </body></html>
        """
        url = "https://www.theguardian.com/world/2025/jul/01/story"
        item = list(spider.parse_article(_response(url, html)))[0]
        assert item["published_date"] is None
        assert "+00:00" in item["scraped_date"]

    def test_parse_article_meta_published_date_fallback(self, spider):
        """With no <time>, the meta article:published_time is used."""
        html = """
        <html><head>
            <meta property="article:published_time" content="2025-07-01T00:00:00Z"/>
        </head><body>
            <h1 data-gu-name="headline">Meta Date</h1>
            <div data-gu-name="body"><p>Body.</p></div>
        </body></html>
        """
        url = "https://www.theguardian.com/world/2025/jul/01/story"
        item = list(spider.parse_article(_response(url, html)))[0]
        assert item["published_date"] == "2025-07-01T00:00:00Z"

    def test_parse_article_empty_content_yields_nothing(self, spider):
        html = "<html><body><h1 data-gu-name='headline'>Title Only</h1></body></html>"
        url = "https://www.theguardian.com/world/2025/jul/01/story"
        assert list(spider.parse_article(_response(url, html))) == []

    def test_parse_article_no_title_yields_nothing(self, spider):
        html = """
        <html><body>
            <div data-gu-name="body"><p>Body without a title.</p></div>
        </body></html>
        """
        url = "https://www.theguardian.com/world/2025/jul/01/story"
        assert list(spider.parse_article(_response(url, html))) == []

    def test_extract_category_from_url_variants(self, spider):
        cases = {
            "https://www.theguardian.com/politics/2025/jul/01/x": "Politics",
            "https://www.theguardian.com/business/2025/jul/01/x": "Business",
            "https://www.theguardian.com/technology/2025/jul/01/x": "Technology",
            "https://www.theguardian.com/world/2025/jul/01/x": "World",
            "https://www.theguardian.com/sport/2025/jul/01/x": "Sports",
            "https://www.theguardian.com/environment/2025/jul/01/x": "Environment",
            "https://www.theguardian.com/science/2025/jul/01/x": "Science",
            "https://www.theguardian.com/culture/2025/jul/01/x": "Culture",
        }
        empty = _response("https://www.theguardian.com/", "<html><body></body></html>")
        for url, expected in cases.items():
            assert spider._extract_category(url, empty) == expected

    def test_extract_category_from_path_fallback(self, spider):
        """Unknown category -> the first path segment is title-cased."""
        empty = _response("https://www.theguardian.com/", "<html><body></body></html>")
        cat = spider._extract_category(
            "https://www.theguardian.com/media-news/2025/jul/01/story", empty
        )
        assert cat == "Media News"

    def test_extract_category_defaults_to_news(self, spider):
        """A bare host with no path segment (split length <= 3) -> "News".

        Note: a trailing-slash host like ".../" splits to length 4 with an empty
        4th segment, so it does NOT reach this default; only a host with no
        trailing slash does.
        """
        empty = _response("https://www.theguardian.com/", "<html><body></body></html>")
        assert spider._extract_category("https://www.theguardian.com", empty) == "News"


@pytest.mark.parametrize('year', [1999, 2026, 2035])
def test_guardian_article_paths_have_no_fixed_year_window(year):
    spider = GuardianSpider()
    path = f'/world/{year}/sep/05/evidence'
    requests = list(spider.parse(_response('https://www.theguardian.com/', f'<html><a href="{path}">Article</a></html>')))
    assert [request.url for request in requests] == ['https://www.theguardian.com' + path]
