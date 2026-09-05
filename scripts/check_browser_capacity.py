"""Exercise included-page cleanup and handler shutdown using real Scrapy Chromium."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrapy
from scrapy.crawler import CrawlerProcess
from scrapy_playwright.page import PageMethod
from src.scraper.spiders.playwright_spider import PlaywrightNewsSpider


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<article>Evidence</article>"
                if "/ok/" in self.path
                else b"<html><body>Waiting</body></html>"
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    pages, browsers, successes = [], [], []

    class Probe(PlaywrightNewsSpider):
        name = "noesis_capacity_probe"
        allowed_domains = ["127.0.0.1"]

        async def start(self):
            for index in range(8):
                yield scrapy.Request(
                    base + ("/fail/" if index < 6 else "/ok/") + str(index),
                    callback=self.complete,
                    errback=self.close_failed_page,
                    meta={
                        "playwright": True,
                        "playwright_include_page": True,
                        "playwright_context": "source-a" if index % 2 else "source-b",
                        "playwright_page_methods": [
                            PageMethod("wait_for_selector", "article", timeout=150)
                        ],
                    },
                )

        async def close_page(self, page):
            pages.append(page)
            browsers.append(page.context.browser)
            await super().close_page(page)

        async def complete(self, response):
            try:
                successes.append(response.url)
            finally:
                await self.close_page(response.meta["playwright_page"])

    process = CrawlerProcess(
        {
            "LOG_ENABLED": False,
            "DOWNLOAD_HANDLERS": {
                "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
                "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
            },
            "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
            "PLAYWRIGHT_MAX_CONTEXTS": 2,
            "PLAYWRIGHT_MAX_PAGES_PER_CONTEXT": 1,
            "CONCURRENT_REQUESTS": 4,
            "RETRY_ENABLED": False,
            "DOWNLOAD_TIMEOUT": 5,
        }
    )
    crawler = process.create_crawler(Probe)
    try:
        process.crawl(crawler)
        process.start()
        assert len(pages) == 8 and len(successes) == 2, (len(pages), successes)
        assert all(page.is_closed() for page in pages)
        assert all(
            browser is not None and not browser.is_connected() for browser in browsers
        )
        stats = crawler.stats.get_stats()
        assert stats.get("browser/request_failed") == 6
        assert stats.get("browser/cleanup_failed", 0) == 0
        args.out.write_text(
            json.dumps(
                {
                    "status": "passed",
                    "included_pages_closed": len(pages),
                    "failed_requests": 6,
                    "successful_requests": 2,
                    "contexts_limit": 2,
                    "pages_per_context_limit": 1,
                    "browser_disconnected_after_handler_shutdown": True,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
