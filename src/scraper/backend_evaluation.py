"""Evaluation-only adapters. No production backend is selected automatically."""

import asyncio
import hashlib
import time


class AcquiredHTML(str):
    """HTML with evaluation-only acquisition diagnostics."""


def captured_html(html, url, backend, *, final_url=None, markdown=None, native=None):
    if not isinstance(html, str) or len(html.encode()) > 2_000_000:
        raise ValueError("bounded captured HTML required")
    result = AcquiredHTML(html)
    result.acquisition_metadata = {
        "source_url": url,
        "final_url": final_url or url,
        "backend": backend,
        "observed_at_ms": int(time.time() * 1000),
        "sha256": hashlib.sha256(html.encode()).hexdigest(),
        "representation": "transport-decoded-http-body"
        if backend == "scrapy"
        else "browser-rendered-html",
        "original_wire_bytes": False,
        "native": native,
    }
    result.markdown = markdown
    return result


def _require_success_status(status):
    if type(status) is not int or not 200 <= status < 300:
        raise ValueError("acquisition returned non-success HTTP status")


async def browser_fetch(url, backend):
    if backend == "playwright":
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page()
                response = await page.goto(url, timeout=10000)
                _require_success_status(response.status if response else None)
                await page.wait_for_selector("article", timeout=1000)
                return captured_html(
                    await page.content(), url, backend, final_url=page.url
                )
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
            _require_success_status(result.status_code)
            markdown = getattr(result.markdown, "raw_markdown", result.markdown)
            return captured_html(
                result.html,
                url,
                backend,
                final_url=getattr(result, "redirected_url", None) or result.url,
                markdown=markdown,
            )
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
        html = captured_html(results[-1], url, backend)
        html.acquisition_metadata["native"] = {
            key: getattr(crawler.statistics.state, key)
            for key in (
                "http_only_request_handler_runs",
                "browser_request_handler_runs",
                "rendering_type_mispredictions",
            )
        }
        # Adaptive HTML is serialized by its static parser in either mode.
        # Keep the transport selection separate from that representation.
        html.acquisition_metadata["representation"] = "static-parser-serialized-html"
        stats = html.acquisition_metadata["native"]
        html.acquisition_metadata["acquisition_mode"] = (
            "browser"
            if stats["browser_request_handler_runs"]
            else "http"
            if stats["http_only_request_handler_runs"]
            else "unknown"
        )
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
            _require_success_status(context.response.status)
            await context.page.wait_for_selector("article", timeout=1000)
            results.append(
                captured_html(
                    await context.page.content(),
                    url,
                    backend,
                    final_url=context.page.url,
                )
            )

        await crawler.run([url])
        if not results:
            raise ValueError("Crawlee returned no page")
        return results[0]
    raise ValueError("unknown browser backend")


def fetch_backend(
    url,
    backend,
    *,
    hosted_client=None,
    observation=None,
    allowed_source_hosts=(),
    public_url_approved=False,
):
    if backend in {"zyte", "firecrawl"}:
        if (
            hosted_client is None
            or not observation
            or hosted_client.http.provider != backend
        ):
            raise ValueError(
                "hosted benchmarking requires an explicit client, observation and preconfigured request/spend limits"
            )
        result = hosted_client.scrape(
            url,
            observation,
            allowed_source_hosts=allowed_source_hosts,
            public_url_approved=public_url_approved,
        )
        html = AcquiredHTML(result["html"])
        html.acquisition_metadata = {
            key: value
            for key, value in result.items()
            if key not in {"html", "markdown"}
        }
        html.markdown = result.get("markdown")
        return html
    if backend in ("playwright", "crawl4ai", "crawlee", "crawlee-adaptive"):
        return asyncio.run(browser_fetch(url, backend))
    if backend == "scrapy":
        settings = {
            "LOG_ENABLED": False,
            "ROBOTSTXT_OBEY": True,
            "DOWNLOAD_TIMEOUT": 10,
            "DOWNLOAD_MAXSIZE": 2_000_000,
            "RETRY_ENABLED": False,
            "CONCURRENT_REQUESTS": 1,
        }
        import scrapy
        from scrapy.crawler import CrawlerProcess

        results = []

        class BenchmarkSpider(scrapy.Spider):
            name = "bounded_benchmark"

            async def start(self):
                yield scrapy.Request(
                    url,
                    callback=self.parse,
                    meta={},
                )

            def parse(self, response):
                results.append(
                    captured_html(
                        response.text,
                        url,
                        backend,
                        final_url=response.url,
                        native={
                            "status": response.status,
                            "response_body_sha256": hashlib.sha256(
                                response.body
                            ).hexdigest(),
                        },
                    )
                )

        process = CrawlerProcess(settings)
        process.crawl(BenchmarkSpider)
        process.start()
        if not results:
            raise ValueError("Scrapy returned no page")
        return results[0]
    raise ValueError("unknown backend")
