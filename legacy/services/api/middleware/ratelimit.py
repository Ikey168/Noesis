"""
Sliding Window Rate Limiting Middleware
Issue #240: Query result caching + rate limits

Implements per-IP/user sliding window rate limiting for FastAPI endpoints.
Returns 429 Too Many Requests if limit exceeded.
"""

import time
import os
from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Optional

try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False

RATE_LIMIT = int(os.getenv("RATE_LIMIT", "10"))  # requests per window
WINDOW_SIZE = int(os.getenv("RATE_LIMIT_WINDOW", "60"))  # seconds

class SlidingWindowRateLimiter(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.use_redis = REDIS_AVAILABLE and os.getenv("USE_REDIS", "1") == "1"
        if self.use_redis:
            self.client = redis.Redis.from_url(self.redis_url)
        else:
            self._requests = {}  # fallback in-memory

    def _get_key(self, request: Request) -> str:
        ip = request.client.host if request.client else "unknown"
        user = request.headers.get("X-User-ID", "")
        return f"rate:{user or ip}"

    async def dispatch(self, request: Request, call_next):
        key = self._get_key(request)
        now = int(time.time())
        window_start = now - WINDOW_SIZE
        allowed = True
        count = 0
        if self.use_redis:
            pipe = self.client.pipeline()
            pipe.zremrangebyscore(key, 0, window_start)
            pipe.zadd(key, {str(now): now})
            pipe.zcount(key, window_start, now)
            pipe.expire(key, WINDOW_SIZE)
            _, _, count, _ = pipe.execute()
            allowed = count <= RATE_LIMIT
        else:
            # In-memory fallback
            reqs = self._requests.setdefault(key, [])
            reqs = [t for t in reqs if t > window_start]
            reqs.append(now)
            self._requests[key] = reqs
            count = len(reqs)
            allowed = count <= RATE_LIMIT
        if not allowed:
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(RATE_LIMIT)
        response.headers["X-RateLimit-Remaining"] = str(max(0, RATE_LIMIT - count))
        response.headers["X-RateLimit-Window"] = str(WINDOW_SIZE)
        return response
