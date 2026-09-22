"""HTTP-aware live cache and explicit offline replay with original fetch times."""

import time

from scrapy.downloadermiddlewares.httpcache import HttpCacheMiddleware
from scrapy.exceptions import IgnoreRequest


class ProvenanceCacheMiddleware(HttpCacheMiddleware):
    def process_request(self, request, spider=None):
        if self.crawler.settings.getbool("NOESIS_OFFLINE_REPLAY", False):
            cached = self.storage.retrieve_response(self.crawler.spider, request)
            if cached is None:
                raise IgnoreRequest("offline replay cache miss")
            cached.flags.append("cached")
            request.meta["noesis_cache_mode"] = "offline-replay"
            return cached
        return super().process_request(request)

    def process_response(self, request, response, spider=None):
        mode = request.meta.get("noesis_cache_mode")
        now = str(int(time.time() * 1000))
        fresh = "cached" not in response.flags and response.status != 304
        if fresh:
            response.headers["X-Noesis-Fetched-At"] = now
            response.headers["X-Noesis-Revalidated-At"] = now
        result = super().process_response(request, response)
        if response.status == 304 and "cached" in result.flags:
            result.headers["X-Noesis-Revalidated-At"] = now
            self.storage.store_response(self.crawler.spider, request, result)
            mode = "live-revalidated"
        metadata = {
            "mode": mode or ("cache-hit" if "cached" in result.flags else "live-fetch"),
            "original_fetched_at": result.headers.get("X-Noesis-Fetched-At"),
            "last_revalidated_at": result.headers.get("X-Noesis-Revalidated-At"),
        }
        request.meta["acquisition_provenance"] = {
            k: (v.decode() if isinstance(v, bytes) else v) for k, v in metadata.items()
        }
        return result
