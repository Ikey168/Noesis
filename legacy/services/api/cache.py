"""
Query Result Cache for /ask Endpoint
Issue #240: Query result caching + rate limits

Caches results for identical queries using Redis (if available),
fallback to in-memory cache if Redis is not configured.
"""

import os
import hashlib
import json
import time
from typing import Any, Dict, Optional

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

# Default TTL for cache entries (seconds)
CACHE_TTL = int(os.getenv("CACHE_TTL", "60"))

class QueryCache:
    def __init__(self):
        self.enabled = bool(os.getenv("CACHE_ENABLED", "1") == "1")
        self.ttl = CACHE_TTL
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.use_redis = REDIS_AVAILABLE and os.getenv("USE_REDIS", "1") == "1"
        self._cache = {}  # fallback in-memory cache
        if self.use_redis:
            self.client = redis.Redis.from_url(self.redis_url)
        else:
            self.client = None

    def _make_key(self, query: str, filters: Dict[str, Any], provider: str, k: int, rerank: bool) -> str:
        key_data = {
            "query": query,
            "filters": filters,
            "provider": provider,
            "k": k,
            "rerank": rerank
        }
        key_str = json.dumps(key_data, sort_keys=True)
        return hashlib.sha256(key_str.encode()).hexdigest()

    def get(self, query: str, filters: Dict[str, Any], provider: str, k: int, rerank: bool) -> Optional[Any]:
        if not self.enabled:
            return None
        key = self._make_key(query, filters, provider, k, rerank)
        if self.use_redis:
            result = self.client.get(key)
            if result:
                return json.loads(result)
        else:
            entry = self._cache.get(key)
            if entry and time.time() < entry[1]:
                return entry[0]
        return None

    def set(self, query: str, filters: Dict[str, Any], provider: str, k: int, rerank: bool, value: Any):
        if not self.enabled:
            return
        key = self._make_key(query, filters, provider, k, rerank)
        if self.use_redis:
            self.client.setex(key, self.ttl, json.dumps(value))
        else:
            self._cache[key] = (value, time.time() + self.ttl)

    def clear(self):
        if self.use_redis:
            self.client.flushdb()
        else:
            self._cache.clear()


# Shared default cache instance (avoids circular imports between
# legacy.services.api.main and route modules).
_default_query_cache = None


def get_query_cache() -> "QueryCache":
    """Return the process-wide QueryCache instance."""
    global _default_query_cache
    if _default_query_cache is None:
        _default_query_cache = QueryCache()
    return _default_query_cache
