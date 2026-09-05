"""
Playwright-based spider for JavaScript-heavy news sites.
"""

from datetime import datetime, timezone
import json
import asyncio
from ..article_links import scoped_url

import scrapy
from scrapy.http import Request
from scrapy_playwright.page import PageMethod

from ..items import NewsItem
from ..settings import SCRAPING_SOURCES


class PlaywrightNewsSpider(scrapy.Spider):
    """Spider for crawling JavaScript-heavy news websites using Playwright."""

    name = "playwright_news"
    allowed_domains = [url.split("/")[2] for url in SCRAPING_SOURCES]

    readiness_timeout_ms = 10000

    @classmethod
    def from_crawler(cls, crawler, *args, **kwargs):
        spider = super().from_crawler(crawler, *args, **kwargs)
        spider.readiness_timeout_ms = max(1, min(60000, crawler.settings.getint('PLAYWRIGHT_CONTENT_TIMEOUT', 10000)))
        return spider

    async def start(self):
        for request in self.start_requests():
            yield request

    async def close_page(self, page):
        try:
            await asyncio.wait_for(page.close(), timeout=5)
        except Exception:
            if getattr(self, 'crawler', None):
                self.crawler.stats.inc_value('browser/cleanup_failed')
            raise

    async def close_failed_page(self, failure):
        if getattr(self, 'crawler', None):
            self.crawler.stats.inc_value('browser/request_failed')
            if 'Timeout' in type(failure.value).__name__:
                self.crawler.stats.inc_value('browser/readiness_or_navigation_timeout')
        page = failure.request.meta.get('playwright_page')
        if page is not None:
            await self.close_page(page)

    def start_requests(self):
        """Generate initial requests for each source URL."""
        for url in SCRAPING_SOURCES:
            yield Request(
                url=url,
                callback=self.parse,
                errback=self.close_failed_page,
                meta={
                    "playwright": True,
                    "playwright_include_page": True,
                    "playwright_page_methods": [
                        PageMethod(
                            "wait_for_selector",
                            "article, .article, .content, .news-item, .story",
                            timeout=self.readiness_timeout_ms,
                        ),

                    ],
                },
            )

    async def parse(self, response):
        """Parse the main page and extract article links."""
        page = response.meta["playwright_page"]

        try:
            # Extract article links
            article_links = []

            # Common article link selectors
            link_selectors = [
                "a.article-link",
                "a.headline",
                ".article a",
                ".news-item a",
                "article a",
                ".story a",
                "a[href*='/article/']",
                "a[href*='/news/']",
            ]

            for selector in link_selectors:
                links = await page.query_selector_all(selector)
                for link in links:
                    href = await link.get_attribute("href")
                    if href:
                        # Convert relative URLs to absolute
                        href = scoped_url(response.url, href)
                        article_links.append(href) if href else None

            # If no links found with specific selectors, try a more generic
            # approach
            if not article_links:
                # Get all links
                all_links = await page.query_selector_all("a")
                for link in all_links:
                    href = await link.get_attribute("href")
                    if href:
                        # Filter for likely article URLs
                        if any(
                            keyword in href
                            for keyword in ["/article/", "/news/", "/story/", "/post/"]
                        ):
                            # Convert relative URLs to absolute
                            href = scoped_url(response.url, href)
                            article_links.append(href) if href else None

            # Remove duplicates while preserving order
            article_links = list(dict.fromkeys(article_links))

            # Follow article links
            for article_url in article_links[
                :10
            ]:  # Limit to 10 articles per source for testing
                yield Request(
                    url=article_url,
                    callback=self.parse_article,
                    errback=self.close_failed_page,
                    meta={
                        "playwright": True,
                        "playwright_include_page": True,
                        "playwright_page_methods": [
                            PageMethod(
                                "wait_for_selector", "article, .article, .content, p", timeout=self.readiness_timeout_ms
                            ),

                        ],
                    },
                )
        finally:
            await self.close_page(page)

    async def parse_article(self, response):
        """Parse a news article page using Playwright."""
        page = response.meta["playwright_page"]

        try:
            # Create a new NewsItem
            item = NewsItem()

            # Extract URL
            item["url"] = response.url

            # Extract title
            title_selectors = ["h1", "h1.headline", ".article-title", ".title"]
            for selector in title_selectors:
                title_element = await page.query_selector(selector)
                if title_element:
                    item["title"] = await title_element.text_content()
                    break

            if "title" not in item:
                # Fallback to document title
                item["title"] = await page.title()

            # Extract content
            content_selectors = [
                "article p",
                ".article-body p",
                ".content p",
                "#main-content p",
                ".story-content p",
            ]

            for selector in content_selectors:
                content_elements = await page.query_selector_all(selector)
                if content_elements:
                    paragraphs = []
                    for element in content_elements:
                        text = await element.text_content()
                        if text and text.strip():
                            paragraphs.append(text.strip())

                    if paragraphs:
                        item["content"] = " ".join(paragraphs)
                        break

            if "content" not in item:
                # Fallback to all paragraphs
                all_paragraphs = await page.query_selector_all("p")
                paragraphs = []
                for p in all_paragraphs:
                    text = await p.text_content()
                    if text and text.strip():
                        paragraphs.append(text.strip())

                if paragraphs:
                    item["content"] = " ".join(paragraphs)
                else:
                    item["content"] = "No content extracted"

            # Extract publication date
            date_selectors = [
                "time",
                ".date",
                ".published-date",
                "meta[property='article:published_time']",
            ]

            for selector in date_selectors:
                date_element = await page.query_selector(selector)
                if date_element:
                    if selector.startswith("meta"):
                        item["published_date"] = await date_element.get_attribute(
                            "content"
                        )
                    else:
                        # Try to get datetime attribute first
                        date_attr = await date_element.get_attribute("datetime")
                        if date_attr:
                            item["published_date"] = date_attr
                        else:
                            # Fall back to text content
                            item["published_date"] = await date_element.text_content()
                    break

            if "published_date" not in item:
                item["published_date"] = None
            item["scraped_date"] = datetime.now(timezone.utc).isoformat()
            item["acquisition_provenance_json"] = json.dumps(response.meta.get("acquisition_provenance", {}), sort_keys=True)

            # Extract source (domain name)
            item["source"] = response.url.split("/")[2]

            # Extract author
            author_selectors = [
                ".author",
                ".byline",
                "meta[name='author']",
                ".article-author",
                ".writer",
            ]

            for selector in author_selectors:
                author_element = await page.query_selector(selector)
                if author_element:
                    if selector.startswith("meta"):
                        item["author"] = await author_element.get_attribute("content")
                    else:
                        item["author"] = await author_element.text_content()

                    if item["author"]:
                        item["author"] = item["author"].strip()
                        break

            if "author" not in item or not item["author"]:
                item["author"] = "Unknown"

            # Extract category
            category_selectors = [
                ".category",
                ".section",
                "meta[property='article:section']",
                ".article-category",
                ".topic",
            ]

            for selector in category_selectors:
                category_element = await page.query_selector(selector)
                if category_element:
                    if selector.startswith("meta"):
                        item["category"] = await category_element.get_attribute(
                            "content"
                        )
                    else:
                        item["category"] = await category_element.text_content()

                    if item["category"]:
                        item["category"] = item["category"].strip()
                        break

            if "category" not in item or not item["category"]:
                # Try to infer category from URL
                url_path = response.url.split("/")
                for category in [
                    "news",
                    "tech",
                    "politics",
                    "business",
                    "sports",
                    "entertainment",
                ]:
                    if category in url_path:
                        item["category"] = category
                        break
                else:
                    item["category"] = "General"

            return item
        finally:
            await self.close_page(page)
