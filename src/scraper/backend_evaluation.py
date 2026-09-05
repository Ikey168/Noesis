"""Evaluation-only adapters. No production backend is selected automatically."""

import asyncio
import os


class AcquiredHTML(str):
    """HTML with evaluation-only acquisition diagnostics."""


async def browser_fetch(url, backend):
    if backend == "playwright":
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                await page.goto(url, timeout=10000)
                await page.wait_for_selector("article", timeout=1000)
                return await page.content()
            finally:
                await browser.close()
    if backend == "crawl4ai":
        from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig

        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(
                url,
                config=CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS,
                    page_timeout=10000,
                    wait_for="css:article",
                ),
            )
            if not result.success:
                raise ValueError("Crawl4AI acquisition failed")
            return result.html
    if backend == "crawlee-adaptive":
        from datetime import timedelta

        from crawlee import ConcurrencySettings
        from crawlee.crawlers import (
            AdaptivePlaywrightCrawler,
            RenderingTypePrediction,
            RenderingTypePredictor,
        )

        results = []

        class StaticFirst(RenderingTypePredictor):
            def predict(self, request):
                return RenderingTypePrediction("static", 0.0)

            def store_result(self, request, rendering_type):
                pass

        crawler = AdaptivePlaywrightCrawler.with_beautifulsoup_static_parser(
            rendering_type_predictor=StaticFirst(),
            max_requests_per_crawl=1,
            max_request_retries=0,
            concurrency_settings=ConcurrencySettings(
                min_concurrency=1, desired_concurrency=1, max_concurrency=1
            ),
            request_handler_timeout=timedelta(seconds=10),
        )

        @crawler.router.default_handler
        async def handle(context):
            article = await context.query_selector_one(
                "article", timeout=timedelta(seconds=1)
            )
            if article is None:
                raise ValueError("Adaptive crawler returned no article")
            html = str(await context.parse_with_static_parser())
            await context.push_data({"html": html})
            results.append(html)

        await crawler.run([url])
        if not results:
            raise ValueError("Adaptive crawler returned no page")
        html = AcquiredHTML(results[-1])
        html.acquisition_metadata = {
            key: getattr(crawler.statistics.state, key)
            for key in (
                "http_only_request_handler_runs",
                "browser_request_handler_runs",
                "rendering_type_mispredictions",
            )
        }
        html.acquisition_metadata["selection_policy"] = (
            "deterministic static-first fallback probe; not learned predictor evaluation"
        )
        return html
    if backend == "crawlee":
        from datetime import timedelta

        from crawlee import ConcurrencySettings
        from crawlee.crawlers import PlaywrightCrawler

        results = []
        crawler = PlaywrightCrawler(
            max_requests_per_crawl=1,
            max_request_retries=0,
            concurrency_settings=ConcurrencySettings(
                min_concurrency=1, desired_concurrency=1, max_concurrency=1
            ),
            request_handler_timeout=timedelta(seconds=15),
        )

        @crawler.router.default_handler
        async def handle(context):
            await context.page.wait_for_selector("article", timeout=1000)
            results.append(await context.page.content())

        await crawler.run([url])
        if not results:
            raise ValueError("Crawlee returned no page")
        return results[0]
    raise ValueError("unknown browser backend")


def fetch_backend(url, backend):
    if backend in ("playwright", "crawl4ai", "crawlee", "crawlee-adaptive"):
        return asyncio.run(browser_fetch(url, backend))
    if backend in ("scrapy", "zyte"):
        settings = {
            "LOG_ENABLED": False,
            "ROBOTSTXT_OBEY": True,
            "DOWNLOAD_TIMEOUT": 10,
            "DOWNLOAD_MAXSIZE": 2_000_000,
            "RETRY_ENABLED": False,
            "CONCURRENT_REQUESTS": 1,
        }
        if backend == "zyte":
            key = os.environ.get("NOESIS_ZYTE_API_KEY")
            if not key:
                raise ValueError("Zyte credential absent")
            settings.update(
                ADDONS={"scrapy_zyte_api.Addon": 500},
                ZYTE_API_KEY=key,
                ZYTE_API_TRANSPARENT_MODE=False,
            )
        import scrapy
        from scrapy.crawler import CrawlerProcess

        results = []

        class BenchmarkSpider(scrapy.Spider):
            name = "bounded_benchmark"

            async def start(self):
                yield scrapy.Request(
                    url,
                    callback=self.parse,
                    meta={"zyte_api": {"httpResponseBody": True}}
                    if backend == "zyte"
                    else {},
                )

            def parse(self, response):
                results.append(response.text)

        process = CrawlerProcess(settings)
        process.crawl(BenchmarkSpider)
        process.start()
        if not results:
            raise ValueError("Scrapy returned no page")
        return results[0]
    import httpx

    if backend == "firecrawl":
        key = os.environ.get("NOESIS_FIRECRAWL_API_KEY")
        if not key:
            raise ValueError("Firecrawl credential absent")
        response = httpx.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": "Bearer " + key},
            json={"url": url, "formats": ["rawHtml"]},
            timeout=20,
        )
        response.raise_for_status()
        return response.json()["data"]["rawHtml"]
    raise ValueError("unknown backend")
