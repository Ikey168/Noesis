"""
CAPTCHA detection and solving for NeuroNews scraper.
Integrates with 2Captcha and other services for automated bypass.
"""

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

#: Reason recorded when a source or URL is skipped because it serves a CAPTCHA
#: and no paid solver is explicitly configured (#2858).
CAPTCHA_REQUIRED = "captcha-required"
#: Outcome recorded alongside :data:`CAPTCHA_REQUIRED`; the run continues.
CAPTCHA_SKIPPED = "skipped"

# Markers of real challenge widgets/interstitials. A bare "captcha" substring is
# deliberately not a marker: articles that merely mention CAPTCHAs must not be
# skipped. Order matters only for which marker is reported first.
CAPTCHA_MARKERS = (
    # Google reCAPTCHA (v2 widget, v3/enterprise script)
    "g-recaptcha",
    "grecaptcha",
    "google.com/recaptcha",
    "recaptcha.net/recaptcha",
    "recaptcha-v2",
    # hCaptcha
    "h-captcha",
    "hcaptcha.com/1/api.js",
    "hcaptcha-iframe",
    # Cloudflare Turnstile and managed challenge interstitials
    "cf-turnstile",
    "turnstile-widget",
    "challenges.cloudflare.com",
    "cf-challenge",
    "cf_chl_opt",
    "cf-chl-widget",
    # Generic widget site key (reCAPTCHA/hCaptcha/Turnstile all use it)
    "data-sitekey",
)

# Placeholder values shipped in example configs are not a configured solver.
_PLACEHOLDER_KEYS = frozenset({"your_2captcha_api_key", "changeme", "none", "null"})


def detect_captcha_marker(page_content: str | None) -> str | None:
    """Return the first CAPTCHA marker found in ``page_content``, else ``None``.

    Pure and offline: inspects only the supplied HTML.
    """
    if not isinstance(page_content, str) or not page_content:
        return None
    page_lower = page_content.lower()
    for marker in CAPTCHA_MARKERS:
        if marker in page_lower:
            return marker
    return None


def solver_key_configured(api_key: str | None) -> bool:
    """True only for an explicitly supplied, non-placeholder solver key."""
    if not isinstance(api_key, str):
        return False
    stripped = api_key.strip()
    return bool(stripped) and stripped.lower() not in _PLACEHOLDER_KEYS


def build_captcha_solver(api_key: str | None) -> "CaptchaSolver | None":
    """Return a paid solver only when a key is explicitly configured.

    No solver object exists (so no paid request can be made) otherwise.
    """
    if not solver_key_configured(api_key):
        return None
    return CaptchaSolver(api_key.strip())


class CaptchaRequired(Exception):
    """Typed skip outcome: the page needs a CAPTCHA and no solver is configured.

    Retry managers treat it as non-retryable: re-fetching cannot succeed
    without a solver, and retrying only adds load on the protected source.
    """

    reason = CAPTCHA_REQUIRED

    def __init__(self, url: str, marker: str | None = None, source: str = ""):
        self.url, self.marker, self.source = url, marker, source
        super().__init__(
            f"{CAPTCHA_REQUIRED}, {CAPTCHA_SKIPPED}: {url} "
            f"(marker: {marker or 'unknown'})"
        )


@dataclass(frozen=True)
class CaptchaSkip:
    """Run-record entry for a URL/source skipped because of a CAPTCHA."""

    source: str
    url: str
    marker: str | None
    stage: str  # "discovery" or "article"
    backend: str  # "http" or "browser"
    solver_configured: bool = False
    reason: str = CAPTCHA_REQUIRED
    outcome: str = CAPTCHA_SKIPPED
    observed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CaptchaSolver:
    """Automated CAPTCHA detection and solving using the paid 2Captcha API.

    Construct only through an explicit key (see :func:`build_captcha_solver`);
    an empty key is rejected so a solver can never exist by accident.
    """

    def get_captcha_result(self):
        """Stub for test patching."""
        return None  # Return None in normal operation

    def __init__(self, api_key: str):
        if not solver_key_configured(api_key):
            raise ValueError("An explicit 2Captcha API key is required")
        self.api_key = api_key
        self.logger = logging.getLogger(__name__)

    async def detect_captcha(self, page_content: str) -> bool:
        """Detect if page contains CAPTCHA elements."""
        marker = detect_captcha_marker(page_content)
        if marker:
            self.logger.info("CAPTCHA detected: %s", marker)
            return True
        return False

    async def solve_recaptcha_v2(
        self, site_key: str, url: str, timeout: int = 120
    ) -> Optional[str]:
        """Solve Google reCAPTCHA v2 using 2Captcha."""
        import aiohttp  # lazy: detection/skip paths must not need aiohttp

        async with aiohttp.ClientSession() as session:
            # Submit CAPTCHA
            data = {
                "key": self.api_key,
                "method": "userrecaptcha",
                "googlekey": site_key,
                "pageurl": url,
                "json": 1,
            }
            async with session.post("https://2captcha.com/in.php", data=data) as resp:
                result = await resp.json()
                if result.get("status") != 1:
                    self.logger.error("2Captcha error: {0}".format(result))
                    return None
                captcha_id = result["request"]

            # Poll for result
            for _ in range(timeout // 5):
                await asyncio.sleep(5)
                result_token = self.get_captcha_result()
                if result_token:
                    return result_token
                    
                async with session.get(
                    "https://2captcha.com/res.php?key={0}&action=get&id={1}&json=1".format(
                        self.api_key, captcha_id
                    )
                ) as resp:
                    result = await resp.json()
                    if result.get("status") == 1:
                        self.logger.info("CAPTCHA solved!")
                        return result["request"]
                    elif result.get("request") == "CAPCHA_NOT_READY":
                        continue
                    else:
                        self.logger.error("2Captcha polling error: {0}".format(result))
                        return None
            self.logger.error("2Captcha timeout")
            return None

    async def solve_hcaptcha(
        self, site_key: str, url: str, timeout: int = 120
    ) -> Optional[str]:
        """Solve hCaptcha using 2Captcha."""
        import aiohttp  # lazy: detection/skip paths must not need aiohttp

        async with aiohttp.ClientSession() as session:
            data = {
                "key": self.api_key,
                "method": "hcaptcha",
                "sitekey": site_key,
                "pageurl": url,
                "json": 1,
            }
            async with session.post("https://2captcha.com/in.php", data=data) as resp:
                result = await resp.json()
                if result.get("status") != 1:
                    self.logger.error("2Captcha error: {0}".format(result))
                    return None
                captcha_id = result["request"]

            for _ in range(timeout // 5):
                await asyncio.sleep(5)
                async with session.get(
                    "https://2captcha.com/res.php?key={0}&action=get&id={1}&json=1".format(
                        self.api_key, captcha_id
                    )
                ) as resp:
                    result = await resp.json()
                    if result.get("status") == 1:
                        self.logger.info("hCaptcha solved!")
                        return result["request"]
                    elif result.get("request") == "CAPCHA_NOT_READY":
                        continue
                    else:
                        self.logger.error("2Captcha polling error: {0}".format(result))
                        return None
            self.logger.error("2Captcha timeout")
            return None
