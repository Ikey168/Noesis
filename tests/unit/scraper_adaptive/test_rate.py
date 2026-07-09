"""Unit tests for the AIMD adaptive rate limiter (#884) — offline."""

from __future__ import annotations

import pytest

from src.scraper.adaptive.rate import AdaptiveRateLimiter


def test_starts_at_base_delay():
    limiter = AdaptiveRateLimiter(base_delay=1.0)
    assert limiter.next_delay("bbc") == 1.0


def test_429_pressure_multiplies_delay():
    limiter = AdaptiveRateLimiter(base_delay=1.0, backoff_factor=2.0)
    limiter.record("bbc", status=429)
    assert limiter.next_delay("bbc") == 2.0
    limiter.record("bbc", status=429)
    assert limiter.next_delay("bbc") == 4.0


def test_503_error_and_slow_response_all_pressure():
    limiter = AdaptiveRateLimiter(base_delay=1.0, slow_threshold_s=5.0)
    limiter.record("a", status=503)
    limiter.record("b", error=True)
    limiter.record("c", status=200, latency_s=9.0)
    assert limiter.next_delay("a") == 2.0
    assert limiter.next_delay("b") == 2.0
    assert limiter.next_delay("c") == 2.0


def test_delay_bounded_by_max():
    limiter = AdaptiveRateLimiter(base_delay=1.0, max_delay=8.0)
    for _ in range(10):
        limiter.record("bbc", status=429)
    assert limiter.next_delay("bbc") == 8.0


def test_success_decays_additively_back_to_base():
    limiter = AdaptiveRateLimiter(base_delay=1.0, recovery_step=0.5)
    for _ in range(3):
        limiter.record("bbc", status=429)  # -> 8.0
    recoveries = 0
    while limiter.next_delay("bbc") > 1.0:
        limiter.record("bbc", status=200, latency_s=0.1)
        recoveries += 1
        assert recoveries < 100
    assert limiter.next_delay("bbc") == 1.0  # never below the base
    assert recoveries > 1  # additive recovery, not an instant snap


def test_per_source_isolation():
    limiter = AdaptiveRateLimiter(base_delay=1.0)
    for _ in range(5):
        limiter.record("hostile", status=429)
    assert limiter.next_delay("hostile") > 1.0
    assert limiter.next_delay("friendly") == 1.0


def test_per_source_base_floor():
    limiter = AdaptiveRateLimiter(base_delay=1.0)
    limiter.set_base("fast", 0.2)
    assert limiter.next_delay("fast") == 0.2
    limiter.record("fast", status=429)
    limiter.record("fast", status=200)
    while limiter.next_delay("fast") > 0.2:
        limiter.record("fast", status=200)
    assert limiter.next_delay("fast") == 0.2


def test_invalid_parameters_rejected():
    with pytest.raises(ValueError):
        AdaptiveRateLimiter(base_delay=0)
    with pytest.raises(ValueError):
        AdaptiveRateLimiter(base_delay=2.0, max_delay=1.0)
    with pytest.raises(ValueError):
        AdaptiveRateLimiter(backoff_factor=1.0)


def test_report_shape():
    limiter = AdaptiveRateLimiter()
    limiter.record("bbc", status=429)
    assert limiter.report()["bbc"]["delay"] == 2.0
