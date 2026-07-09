"""
AIMD adaptive per-source rate limiting (#884).

Scrapy's side gets AUTOTHROTTLE; the async engine used a static per-source
``rate_limit``. This limiter adapts the inter-request delay per source from
observed outcomes, AIMD-style (inverted for delays):

- **multiplicative increase** on pressure — HTTP 429/503, request errors, or
  responses slower than ``slow_threshold_s`` — bounded by ``max_delay``;
- **additive decrease** on success, decaying back toward the source's base
  delay (never below it).

Per-source isolation is inherent: one hostile source backing off never slows
the others. Pure logic, injectable-free, no async imports — the engine simply
``sleep``s ``next_delay(source)`` before a request and calls ``record()``
after it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

PRESSURE_STATUSES = frozenset({429, 503})


class AdaptiveRateLimiter:
    """Outcome-adaptive inter-request delays, one lane per source."""

    def __init__(
        self,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        backoff_factor: float = 2.0,
        recovery_step: float = 0.25,
        slow_threshold_s: float = 5.0,
    ):
        if base_delay <= 0 or max_delay < base_delay or backoff_factor <= 1:
            raise ValueError("invalid rate-limiter parameters")
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.recovery_step = recovery_step
        self.slow_threshold_s = slow_threshold_s
        self._delays: Dict[str, float] = {}
        self._bases: Dict[str, float] = {}

    def set_base(self, source_id: str, base_delay: float) -> None:
        """Give a source its own configured floor (from rate_limit config)."""
        self._bases[source_id] = max(base_delay, 0.01)

    def _base(self, source_id: str) -> float:
        return self._bases.get(source_id, self.base_delay)

    def next_delay(self, source_id: str) -> float:
        """Seconds to wait before the next request to this source."""
        return self._delays.get(source_id, self._base(source_id))

    def record(
        self,
        source_id: str,
        status: Optional[int] = None,
        latency_s: Optional[float] = None,
        error: bool = False,
    ) -> float:
        """Feed back one request outcome; returns the updated delay."""
        current = self.next_delay(source_id)
        pressured = (
            error
            or (status is not None and status in PRESSURE_STATUSES)
            or (latency_s is not None and latency_s > self.slow_threshold_s)
        )
        if pressured:
            updated = min(current * self.backoff_factor, self.max_delay)
        else:
            updated = max(current - self.recovery_step, self._base(source_id))
        self._delays[source_id] = updated
        return updated

    def report(self) -> Dict[str, Any]:
        return {
            source_id: {"delay": round(delay, 3), "base": self._base(source_id)}
            for source_id, delay in sorted(self._delays.items())
        }
