"""Unit tests for the hardened live ingestion path (#880) — offline.

Covers retry/backoff behavior of _http_get, the opt-in full-text upgrade via
the extraction cascade (#877), and the SourceHealthTracker wiring (#878/#879).
"""

from __future__ import annotations

from urllib.error import HTTPError, URLError

import pytest

import src.ingestion.scrapy_integration as si
from src.ingestion.source_health import SourceHealthTracker

_RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>Budget vote passes</title><link>https://ex.com/a1</link>
<description>Short summary.</description>
<pubDate>Mon, 06 Jul 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""

_FEED = si.Feed("Example", "https://ex.com/rss", "World")

_PAGE = (
    "<html><body><article><p>"
    + "The parliament approved the annual budget after a long debate. " * 10
    + "</p></article></body></html>"
).encode("utf-8")


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


# --------------------------------------------------------------------------- #
# _http_get retry behavior
# --------------------------------------------------------------------------- #


def test_http_get_retries_transient_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def opener(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise HTTPError(req.full_url, 503, "unavailable", None, None)
        return _Resp(b"ok")

    out = si._http_get("https://ex.com", _urlopen=opener, _sleep=sleeps.append)
    assert out == b"ok"
    assert calls["n"] == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0]  # exponential backoff


def test_http_get_permanent_error_raises_immediately():
    calls = {"n": 0}

    def opener(req, timeout=None):
        calls["n"] += 1
        raise HTTPError(req.full_url, 404, "not found", None, None)

    with pytest.raises(HTTPError):
        si._http_get("https://ex.com", _urlopen=opener, _sleep=lambda s: None)
    assert calls["n"] == 1  # no hammering on permanent errors


def test_http_get_exhausts_retries_and_raises_last_error():
    def opener(req, timeout=None):
        raise URLError("connection refused")

    with pytest.raises(URLError):
        si._http_get("https://ex.com", retries=2, _urlopen=opener, _sleep=lambda s: None)


def test_http_get_rotates_user_agent():
    uas = []

    def opener(req, timeout=None):
        uas.append(req.get_header("User-agent"))
        raise URLError("down")

    with pytest.raises(URLError):
        si._http_get("https://ex.com", retries=3, _urlopen=opener, _sleep=lambda s: None)
    assert len(set(uas)) > 1  # different attempts wear different UAs


# --------------------------------------------------------------------------- #
# Full-text upgrade (#880 via #877)
# --------------------------------------------------------------------------- #


def _getter(url: str) -> bytes:
    return _RSS if url.endswith("rss") else _PAGE


def test_full_text_upgrade_replaces_summary():
    articles = si.fetch_articles([_FEED], full_text=True, http_get=_getter)
    (a,) = articles
    assert "parliament approved the annual budget" in a.content
    assert len(a.content) > len("Short summary.")


def test_full_text_off_by_default():
    articles = si.fetch_articles([_FEED], http_get=_getter)
    (a,) = articles
    assert a.content == "Short summary."


def test_full_text_failure_keeps_summary():
    def getter(url: str) -> bytes:
        if url.endswith("rss"):
            return _RSS
        raise ConnectionError("article page down")

    articles = si.fetch_articles([_FEED], full_text=True, http_get=getter)
    (a,) = articles
    assert a.content == "Short summary."  # degraded, not dropped


def test_full_text_cap_limits_page_fetches():
    page_fetches = {"n": 0}

    def getter(url: str) -> bytes:
        if url.endswith("rss"):
            return _RSS
        page_fetches["n"] += 1
        return _PAGE

    si.fetch_articles([_FEED], full_text=True, full_text_cap=0, http_get=getter)
    assert page_fetches["n"] == 0


def test_full_text_env_flag(monkeypatch):
    monkeypatch.setenv(si.FULL_TEXT_ENV, "1")
    articles = si.fetch_articles([_FEED], http_get=_getter)
    assert "parliament approved" in articles[0].content


# --------------------------------------------------------------------------- #
# Health-tracker wiring (#878/#879)
# --------------------------------------------------------------------------- #


def test_health_records_every_pass():
    tracker = SourceHealthTracker()
    si.fetch_articles([_FEED], http_get=_getter, health=tracker, now_ms=0)
    (entry,) = tracker.report()
    assert entry["source_id"] == "Example"
    assert entry["last_articles"] == 1


def test_health_records_fetch_failure_as_zero_yield():
    def getter(url: str) -> bytes:
        raise ConnectionError("feed down")

    tracker = SourceHealthTracker()
    si.fetch_articles([_FEED], http_get=getter, health=tracker, now_ms=0)
    (entry,) = tracker.report()
    assert entry["last_articles"] == 0


def test_health_skips_feed_not_due():
    tracker = SourceHealthTracker()
    tracker.record_run("Example", 5, now_ms=0)  # just fetched
    fetches = {"n": 0}

    def getter(url: str) -> bytes:
        fetches["n"] += 1
        return _RSS

    # One minute later: not due yet -> skipped entirely.
    out = si.fetch_articles([_FEED], http_get=getter, health=tracker, now_ms=60_000)
    assert out == [] and fetches["n"] == 0

    # After the base interval: due again.
    out = si.fetch_articles(
        [_FEED], http_get=getter, health=tracker,
        now_ms=60_000 + 30 * 60 * 1000,
    )
    assert len(out) == 1 and fetches["n"] == 1


def test_no_health_tracker_means_no_behavior_change():
    assert len(si.fetch_articles([_FEED], http_get=_getter)) == 1
