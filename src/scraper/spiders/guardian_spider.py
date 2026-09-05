"""
The Guardian news spider for NeuroNews.
"""

from datetime import datetime, timezone
import json
import re
from urllib.parse import urlsplit
from ..article_links import scoped_url

import scrapy

from ..items import NewsItem


class GuardianSpider(scrapy.Spider):
    """Spider for crawling The Guardian articles."""

    name = "guardian"
    allowed_domains = ["theguardian.com"]
    start_urls = ["https://www.theguardian.com/"]

    def parse(self, response):
        """Parse the main Guardian page to find article links."""
        seen = set()
        for link in response.css('a::attr(href)').getall():
            link = scoped_url(response.url, link)
            if link and re.search(r'/\d{4}/[a-z]{3}/\d{1,2}/[^/]+', urlsplit(link).path) and link not in seen:
                seen.add(link)
                yield scrapy.Request(url=link, callback=self.parse_article)

    def parse_article(self, response):
        """Parse a Guardian article page."""
        item = NewsItem()

        # Extract title
        item["title"] = (
            response.css('h1[data-gu-name="headline"]::text').get()
            or response.css("h1.content__headline::text").get()
            or response.css("h1::text").get()
        )

        item["url"] = response.url
        item["source"] = "The Guardian"

        # Extract content
        content_paragraphs = (
            response.css('div[data-gu-name="body"] p::text').getall()
            or response.css(".content__article-body p::text").getall()
            or response.css("article p::text").getall()
        )

        item["content"] = (
            " ".join(content_paragraphs).strip() if content_paragraphs else ""
        )

        # Extract publication date
        date_element = (
            response.css("time::attr(datetime)").get()
            or response.css(
                'meta[property="article:published_time"]::attr(content)'
            ).get()
        )

        if date_element:
            item["published_date"] = date_element
        else:
            item["published_date"] = None

        item["scraped_date"] = datetime.now(timezone.utc).isoformat()
        item["acquisition_provenance_json"] = json.dumps(getattr(response,"meta",{}).get("acquisition_provenance", {}), sort_keys=True)

        # Extract author
        author = (
            response.css('a[rel="author"]::text').get()
            or response.css(".byline a::text").get()
            or response.css('meta[name="author"]::attr(content)').get()
        )
        item["author"] = author.strip() if author else "Guardian Sta"

        # Extract category
        category = self._extract_category(response.url, response)
        item["category"] = category

        if item["title"] and item["content"]:
            yield item

    def _extract_category(self, url, response):
        """Extract category from URL or page structure."""
        url_lower = url.lower()
        if "/politics/" in url_lower:
            return "Politics"
        elif "/business/" in url_lower:
            return "Business"
        elif "/technology/" in url_lower:
            return "Technology"
        elif "/world/" in url_lower:
            return "World"
        elif "/sport/" in url_lower:
            return "Sports"
        elif "/environment/" in url_lower:
            return "Environment"
        elif "/science/" in url_lower:
            return "Science"
        elif "/culture/" in url_lower:
            return "Culture"
        else:
            # Try to extract from section path
            path_parts = url.split("/")
            if len(path_parts) > 3:
                section = path_parts[3]
                return section.replace("-", " ").title()
            return "News"
