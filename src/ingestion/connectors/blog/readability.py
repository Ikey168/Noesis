"""Full-text article body extraction for blog posts.

The extraction itself lives in the shared site-agnostic cascade
(``src.ingestion.extract``, #877): trafilatura → readability-lxml → bs4
heuristic, each optional dependency degrading gracefully. This module keeps
the blog connector's fetch + string-result contract.
"""

from __future__ import annotations

from urllib.request import Request, urlopen

from src.ingestion.extract import extract_article

_USER_AGENT = "NeuroNewsBot/1.0 (+https://github.com/Ikey168/NeuroNews)"
_HTTP_TIMEOUT = 15


def fetch_full_text(url: str, _http_get=None) -> str:
    """Fetch ``url`` and extract the main readable text.

    Returns an empty string on any network or parse error so callers can
    fall back to the feed summary without crashing.
    """
    try:
        getter = _http_get or _default_http_get
        html = getter(url)
    except Exception:
        return ""

    if not html:
        return ""

    result = extract_article(html, url=url)
    return result.text if result is not None else ""


def _default_http_get(url: str) -> bytes:
    req = Request(
        url,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
    )
    with urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return resp.read()
