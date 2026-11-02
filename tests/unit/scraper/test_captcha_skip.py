"""CAPTCHA pages are skipped without a paid solver (#2858, FA19).

Offline: every HTTP session, browser page and solver is a local fake.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from scraper.captcha_solver import (
    CAPTCHA_REQUIRED,
    CaptchaRequired,
    CaptchaSolver,
    build_captcha_solver,
    detect_captcha_marker,
)

RECAPTCHA = '<html><div class="g-recaptcha" data-sitekey="k"></div></html>'
HCAPTCHA = '<html><script src="https://hcaptcha.com/1/api.js"></script></html>'
TURNSTILE = (
    '<html><script src="https://challenges.cloudflare.com/turnstile/v0/api.js">'
    "</script></html>"
)
CF_INTERSTITIAL = "<html><script>window._cf_chl_opt={cvId:'3'};</script></html>"
ARTICLE_ABOUT_CAPTCHAS = (
    "<html><h1>Why CAPTCHA puzzles are getting harder</h1>"
    "<p>Researchers studied captcha usability.</p></html>"
)


def make_source(name="Walled", **over):
    from scraper.async_scraper_engine import NewsSource

    base = {
        "name": name,
        "base_url": "https://walled.example/news",
        "article_selectors": {"title": "h1", "content": "p"},
        "link_patterns": [r"/news/.*"],
    }
    base.update(over)
    return NewsSource(**base)


class _Resp:
    def __init__(self, text, status=200):
        self.status, self._text = status, text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def engine():
    # Engine paths need the scraper's optional runtime and async test support;
    # detection, solver gating and retry tests above/below run without them.
    for module in (
        "aiohttp",
        "bs4",
        "psutil",
        "aiofiles",
        "playwright",
        "pytest_asyncio",
    ):
        pytest.importorskip(module)
    from scraper.async_scraper_engine import AsyncNewsScraperEngine

    eng = AsyncNewsScraperEngine(enable_monitoring=False)
    yield eng
    eng.monitor.stop()


def _session(engine, html, status=200):
    session = MagicMock()
    session.get = MagicMock(side_effect=lambda *a, **k: _Resp(html, status))
    engine.session = session
    return session


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "html, marker",
    [
        (RECAPTCHA, "g-recaptcha"),
        (HCAPTCHA, "hcaptcha.com/1/api.js"),
        (TURNSTILE, "challenges.cloudflare.com"),
        (CF_INTERSTITIAL, "cf_chl_opt"),
        ('<div class="cf-turnstile"></div>', "cf-turnstile"),
    ],
)
def test_detects_challenge_widgets(html, marker):
    assert detect_captcha_marker(html) == marker


@pytest.mark.parametrize("html", [ARTICLE_ABOUT_CAPTCHAS, "", None, b"g-recaptcha"])
def test_mentions_of_captcha_are_not_challenges(html):
    assert detect_captcha_marker(html) is None


# --------------------------------------------------------------------------
# No paid solver unless explicitly configured
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", [None, "", "   ", "YOUR_2CAPTCHA_API_KEY", 123])
def test_no_solver_without_explicit_key(key):
    with patch("scraper.captcha_solver.CaptchaSolver") as solver_cls:
        assert build_captcha_solver(key) is None
        solver_cls.assert_not_called()


def test_solver_rejects_empty_key():
    with pytest.raises(ValueError):
        CaptchaSolver("")


def test_engine_default_has_no_solver(engine):
    assert engine.captcha_solver is None
    assert engine.get_performance_stats()["captcha"]["solver_configured"] is False


# --------------------------------------------------------------------------
# Engine: record "captcha-required, skipped", no retry, run continues
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_captcha_skips_source_without_retry_or_escalation(engine):
    session = _session(engine, RECAPTCHA)
    engine.browser = MagicMock()  # escalation would be possible
    engine.scrape_js_source = AsyncMock(return_value=[])
    paid = MagicMock()

    with patch("aiohttp.ClientSession.post", paid):
        assert await engine.scrape_source_async(make_source()) == []
        # A second pass in the same run does not touch the source again.
        assert await engine.scrape_source_async(make_source()) == []

    assert session.get.call_count == 1
    engine.scrape_js_source.assert_not_called()
    paid.assert_not_called()
    assert engine.discovery_failures["Walled"] == CAPTCHA_REQUIRED
    report = engine.get_performance_stats()["captcha"]
    assert report["skipped_sources"] == {"Walled": "captcha-required, skipped"}
    (entry,) = report["skips"]
    assert entry["reason"] == "captcha-required" and entry["outcome"] == "skipped"
    assert entry["stage"] == "discovery" and entry["marker"] == "g-recaptcha"
    assert entry["solver_configured"] is False


@pytest.mark.asyncio
async def test_other_sources_continue(engine):
    walled, open_source = (
        make_source(),
        make_source(name="Open", base_url="https://open.example/news"),
    )
    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda url, **k: _Resp(
            RECAPTCHA if "walled" in url else "<html><a href='/news/1'>x</a></html>"
        )
    )
    engine.session = session
    engine.extract_links_from_html = MagicMock(return_value=[])

    await engine.scrape_sources_async([walled, open_source])

    assert engine.captcha_blocked_sources == {"Walled": "captcha-required, skipped"}
    engine.extract_links_from_html.assert_called_once()  # "Open" was processed
    assert "Open" not in engine.discovery_failures


@pytest.mark.asyncio
async def test_article_captcha_is_not_retryable(engine):
    _session(engine, TURNSTILE)
    url = "https://walled.example/news/1"
    result = await engine._attempt_article(
        "http", make_source(), url, asyncio.Semaphore(1)
    )
    assert result is None
    assert engine.url_states[url] == "captcha-skipped"
    assert url in engine.captcha_skipped_urls
    assert engine.captcha_blocked_sources["Walled"] == "captcha-required, skipped"
    assert engine.captcha_skips[0]["stage"] == "article"


@pytest.mark.asyncio
async def test_browser_page_captcha_is_skipped(engine):
    page = MagicMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.content = AsyncMock(return_value=HCAPTCHA)
    page.evaluate = AsyncMock()
    page.close = AsyncMock()
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)

    assert await engine.get_article_links_js(context, make_source()) == []
    page.evaluate.assert_not_called()
    assert engine.captcha_skips[0]["backend"] == "browser"


@pytest.mark.asyncio
async def test_configured_solver_is_recorded_but_not_called(engine):
    engine.captcha_solver = MagicMock()
    _session(engine, RECAPTCHA)
    url = "https://walled.example/news/2"
    sem = asyncio.Semaphore(1)
    assert await engine.scrape_article_http(sem, make_source(), url) is None
    assert engine.captcha_skips[0]["solver_configured"] is True
    # Article-level hit with a solver configured blocks only that URL.
    assert "Walled" not in engine.captcha_blocked_sources
    assert not engine.captcha_solver.method_calls


# --------------------------------------------------------------------------
# Retry manager
# --------------------------------------------------------------------------


def test_retry_manager_does_not_retry_captcha_required():
    pytest.importorskip("boto3")  # cloudwatch/dynamodb modules import it
    from scraper.enhanced_retry_manager import (
        EnhancedRetryManager,
        RetryConfig,
        RetryReason,
    )

    manager = EnhancedRetryManager(retry_config=RetryConfig(max_retries=3))
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        raise CaptchaRequired("https://walled.example/a", "g-recaptcha", "Walled")

    with (
        patch("asyncio.sleep", AsyncMock()) as slept,
        pytest.raises(CaptchaRequired) as info,
    ):
        asyncio.run(manager.retry_with_backoff(fetch, url="https://walled.example/a"))
    assert calls == 1
    slept.assert_not_called()
    assert info.value.reason == "captcha-required"
    assert str(info.value).startswith("captcha-required, skipped")
    assert manager.skipped == {"https://walled.example/a": "captcha-required"}
    assert "https://walled.example/a" not in manager.circuit_breaker_state
    assert (
        manager._determine_retry_reason(info.value, {}) is RetryReason.CAPTCHA_REQUIRED
    )
