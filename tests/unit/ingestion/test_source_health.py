"""Unit tests for source extraction-health tracking (#878, #879) — offline."""

from __future__ import annotations

from src.ingestion.source_health import (
    DEFAULT_BASE_INTERVAL_MS,
    DEFAULT_MAX_INTERVAL_MS,
    MIN_RUNS,
    QUARANTINE_AFTER,
    SourceHealthTracker,
    field_fill_rates,
)

_GOOD_FILL = {"title": 1.0, "content": 1.0}
_MIN = 60 * 1000


def _seed_healthy(tracker, source="bbc", runs=10, articles=10, start_ms=0):
    """Record a healthy history; returns the timestamp after the last run."""
    t = start_ms
    for _ in range(runs):
        tracker.record_run(source, articles, _GOOD_FILL, now_ms=t)
        t += 30 * _MIN
    return t


# --------------------------------------------------------------------------- #
# Drift detection (#878)
# --------------------------------------------------------------------------- #


def test_unknown_until_min_runs():
    tracker = SourceHealthTracker()
    for i in range(MIN_RUNS - 1):
        assert tracker.record_run("bbc", 10, _GOOD_FILL, now_ms=i) == "unknown"
    assert tracker.record_run("bbc", 10, _GOOD_FILL, now_ms=99) == "healthy"


def test_healthy_source_stays_healthy():
    tracker = SourceHealthTracker()
    _seed_healthy(tracker)
    assert tracker.status("bbc") == "healthy"
    base = tracker.baseline("bbc")
    assert base is not None and base["yield"] == 10


def test_zero_yield_runs_flag_degraded_not_no_news():
    """The core blind spot: 200-OK-but-zero-extracted becomes a distinct signal."""
    tracker = SourceHealthTracker()
    t = _seed_healthy(tracker)
    # Selector breaks: fetches keep "succeeding" with zero articles.
    for i in range(3):
        status = tracker.record_run("bbc", 0, now_ms=t + i * 30 * _MIN)
    assert status == "degraded"


def test_low_new_content_is_not_degraded():
    """A quiet news day (some yield) must NOT look like breakage."""
    tracker = SourceHealthTracker()
    t = _seed_healthy(tracker, articles=10)
    for i in range(3):
        status = tracker.record_run("bbc", 4, _GOOD_FILL, now_ms=t + i * 30 * _MIN)
    assert status == "healthy"  # 40% of baseline is above the 25% collapse line


def test_field_fill_collapse_flags_degraded():
    """Yield intact but the title selector broke -> fill-rate drift fires."""
    tracker = SourceHealthTracker()
    t = _seed_healthy(tracker)
    status = "healthy"
    for i in range(3):
        status = tracker.record_run(
            "bbc", 10, {"title": 0.1, "content": 1.0}, now_ms=t + i * 30 * _MIN
        )
    assert status == "degraded"


def test_quarantine_after_persistent_degradation_and_recovery():
    tracker = SourceHealthTracker()
    t = _seed_healthy(tracker)
    status = "healthy"
    runs_needed = 0
    while status != "quarantined":
        status = tracker.record_run("bbc", 0, now_ms=t)
        t += 30 * _MIN
        runs_needed += 1
        assert runs_needed < 20, "never quarantined"
    assert tracker.is_quarantined("bbc")
    # One healthy run fully recovers the source.
    assert tracker.record_run("bbc", 10, _GOOD_FILL, now_ms=t) == "healthy"


def test_sources_tracked_independently():
    tracker = SourceHealthTracker()
    t = _seed_healthy(tracker, "bbc")
    _seed_healthy(tracker, "cnn")
    for i in range(3):
        tracker.record_run("bbc", 0, now_ms=t + i)
    assert tracker.status("bbc") == "degraded"
    assert tracker.status("cnn") == "healthy"


def test_field_fill_rates_helper():
    articles = [
        {"title": "a", "content": "x"},
        {"title": "", "content": "y"},
        {"title": "c", "content": ""},
    ]
    rates = field_fill_rates(articles)
    assert abs(rates["title"] - 2 / 3) < 1e-9
    assert abs(rates["content"] - 2 / 3) < 1e-9
    # An empty run reports full fill so zero-yield runs don't fake fill drift.
    assert field_fill_rates([]) == {"title": 1.0, "content": 1.0}


# --------------------------------------------------------------------------- #
# Adaptive scheduling (#879)
# --------------------------------------------------------------------------- #


def test_unknown_source_always_due():
    tracker = SourceHealthTracker()
    assert tracker.due("new-source", now_ms=0)


def test_productive_source_uses_base_interval():
    tracker = SourceHealthTracker()
    t = _seed_healthy(tracker)  # last run at t - 30min
    last = t - 30 * _MIN
    assert not tracker.due("bbc", now_ms=last + DEFAULT_BASE_INTERVAL_MS - 1)
    assert tracker.due("bbc", now_ms=last + DEFAULT_BASE_INTERVAL_MS)


def test_empty_runs_back_off_exponentially_bounded():
    tracker = SourceHealthTracker()
    tracker.record_run("quiet", 0, now_ms=0)     # empty_streak=1 -> 2x base
    next_at = tracker.next_due_ms("quiet")
    assert next_at == 2 * DEFAULT_BASE_INTERVAL_MS
    tracker.record_run("quiet", 0, now_ms=next_at)  # streak=2 -> 4x
    assert tracker.next_due_ms("quiet") == next_at + 4 * DEFAULT_BASE_INTERVAL_MS
    # Bounded: a long streak never exceeds the max interval.
    for i in range(12):
        tracker.record_run("quiet", 0, now_ms=1_000_000 + i)
    assert (
        tracker.next_due_ms("quiet") - 1_000_011
    ) <= DEFAULT_MAX_INTERVAL_MS


def test_productive_run_resets_backoff():
    tracker = SourceHealthTracker()
    for i in range(4):
        tracker.record_run("s", 0, now_ms=i)
    tracker.record_run("s", 7, _GOOD_FILL, now_ms=100)
    assert tracker.next_due_ms("s") == 100 + DEFAULT_BASE_INTERVAL_MS


def test_quarantined_source_probes_at_max_interval():
    tracker = SourceHealthTracker()
    t = _seed_healthy(tracker)
    for i in range(QUARANTINE_AFTER + 3):
        tracker.record_run("bbc", 0, now_ms=t + i)
    assert tracker.is_quarantined("bbc")
    last = t + QUARANTINE_AFTER + 2
    assert tracker.next_due_ms("bbc") == last + DEFAULT_MAX_INTERVAL_MS
    # Never permanently starved: due again once the probe interval elapses.
    assert tracker.due("bbc", now_ms=last + DEFAULT_MAX_INTERVAL_MS)


# --------------------------------------------------------------------------- #
# Persistence + reporting
# --------------------------------------------------------------------------- #


def test_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "health.json")
    tracker = SourceHealthTracker(path=path)
    t = _seed_healthy(tracker)
    for i in range(3):
        tracker.record_run("bbc", 0, now_ms=t + i)

    reloaded = SourceHealthTracker(path=path)
    assert reloaded.status("bbc") == "degraded"
    assert reloaded.baseline("bbc")["yield"] == 10


def test_report_shape():
    tracker = SourceHealthTracker()
    _seed_healthy(tracker)
    (entry,) = tracker.report()
    assert entry["source_id"] == "bbc"
    assert entry["status"] == "healthy"
    assert entry["runs"] == 10
    assert entry["baseline"]["yield"] == 10
