"""One bounded cache probe; used by persistent-cache local-server regression."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scrapy
from scrapy.crawler import CrawlerProcess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    results = []

    class Probe(scrapy.Spider):
        name = "cache_probe"

        async def start(self):
            yield scrapy.Request(
                args.url,
                callback=self.parse,
                errback=self.fail,
                meta={"handle_httpstatus_all": True},
            )

        def parse(self, response):
            results.append(
                {
                    "status": response.status,
                    "text": response.text,
                    "provenance": response.meta.get("acquisition_provenance"),
                }
            )

        def fail(self, failure):
            results.append({"failure_type": failure.value.__class__.__name__})

    process = CrawlerProcess(
        {
            "LOG_ENABLED": False,
            "HTTPCACHE_ENABLED": True,
            "HTTPCACHE_DIR": args.cache,
            "HTTPCACHE_POLICY": "scrapy.extensions.httpcache.RFC2616Policy",
            "HTTPCACHE_EXPIRATION_SECS": 0 if args.offline else 300,
            "HTTPCACHE_IGNORE_HTTP_CODES": [429, 500, 502, 503, 504],
            "DOWNLOADER_MIDDLEWARES": {
                "scrapy.downloadermiddlewares.httpcache.HttpCacheMiddleware": None,
                "src.scraper.cache_provenance.ProvenanceCacheMiddleware": 900,
            },
            "NOESIS_OFFLINE_REPLAY": args.offline,
            "DOWNLOAD_TIMEOUT": 5,
            "RETRY_ENABLED": False,
        }
    )
    process.crawl(Probe)
    process.start()
    args.out.write_text(json.dumps(results))


if __name__ == "__main__":
    main()
