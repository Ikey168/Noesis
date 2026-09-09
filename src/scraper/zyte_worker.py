"""One-shot Scrapy/Zyte job invoked only after an explicit durable reservation."""

from __future__ import annotations

import importlib.metadata
import json

from tenacity import AsyncRetrying, retry_never, stop_after_attempt

from src.evaluation.runtime_errors import BackendError
from src.ingestion.provider_execution import _resolve, _safe_url


class NoRetryPolicy(AsyncRetrying):
    def __init__(self, *args, **kwargs):
        super().__init__(retry=retry_never, stop=stop_after_attempt(1), reraise=True)


def settings_for(credential, timeout_s, max_bytes):
    return {
        "ADDONS": {"scrapy_zyte_api.Addon": 500},
        "ZYTE_API_KEY": credential,
        "ZYTE_API_TRANSPARENT_MODE": False,
        "ZYTE_API_MAX_REQUESTS": 1,
        "ZYTE_API_RETRY_POLICY": "src.scraper.zyte_worker.NoRetryPolicy",
        "ZYTE_API_SESSION_ENABLED": False,
        "ZYTE_API_USE_ENV_PROXY": False,
        "ROBOTSTXT_OBEY": True,
        "REDIRECT_ENABLED": False,
        "RETRY_ENABLED": False,
        "COOKIES_ENABLED": False,
        "CONCURRENT_REQUESTS": 1,
        "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_TIMEOUT": timeout_s,
        "DOWNLOAD_MAXSIZE": max_bytes,
        "CLOSESPIDER_TIMEOUT": timeout_s,
        "LOG_ENABLED": False,
        "TELNETCONSOLE_ENABLED": False,
        "HTTPCACHE_ENABLED": False,
    }


def fetch_once(payload):
    if (
        payload.get("reservation_authorized") is not True
        or not isinstance(payload.get("credential"), str)
        or not payload["credential"]
    ):
        raise BackendError(
            "authorization_required",
            "the Zyte worker requires a pre-reserved explicit request",
        )
    url = payload.get("url")
    from urllib.parse import urlsplit

    _safe_url(url, {urlsplit(str(url)).hostname}, resolver=_resolve)
    timeout, size = payload.get("timeout_s", 30), payload.get("max_bytes", 4_000_000)
    if not 0.1 <= timeout <= 60 or type(size) is not int or not 1 <= size <= 8_000_000:
        raise ValueError("invalid worker timeout/response bounds")
    import scrapy
    import scrapy_zyte_api  # noqa: F401 - verify optional native integration before any requests
    from scrapy.crawler import CrawlerProcess

    outcomes = []

    class ReservedSpider(scrapy.Spider):
        name = "noesis_reserved_zyte_evaluation"

        async def start(self):
            yield scrapy.Request(
                url,
                callback=self.parse,
                errback=self.failed,
                meta={
                    "zyte_api": {"httpResponseBody": True, "httpResponseHeaders": True},
                    "handle_httpstatus_all": True,
                    "dont_retry": True,
                },
            )

        def parse(self, response):
            native = getattr(response, "raw_api_response", None)
            if not isinstance(native, dict) or len(json.dumps(native).encode()) > size:
                outcomes.append({"error": "missing_or_oversized_native_response"})
            else:
                outcomes.append(native)

        def failed(self, failure):
            outcomes.append({"error": "zyte_or_source_failure"})

    process = CrawlerProcess(settings_for(payload["credential"], timeout, size))
    process.crawl(ReservedSpider)
    process.start(install_signal_handlers=False)
    if len(outcomes) != 1 or "error" in outcomes[0]:
        raise BackendError(
            "zyte_fetch_failed",
            "the reserved request did not produce one usable native response",
        )
    if payload["credential"] in json.dumps(outcomes[0]):
        raise BackendError(
            "credential_echo", "Zyte response contained credential material"
        )
    return {
        **outcomes[0],
        "noesis_transport_version": importlib.metadata.version("scrapy-zyte-api"),
    }
