"""
Generic article-content extraction cascade (#877).

Per-site CSS selectors are brittle: a site redesign silently breaks them and
articles vanish with no error. This module is the safety net — a site-agnostic
extraction cascade callers run when (or before) selector-based extraction
fails, so a layout change degrades to "generic extraction, lower confidence"
instead of silent loss.

Stages, best first, each optional dependency degrading gracefully:

1. ``trafilatura``     — state-of-the-art article extraction (optional)
2. ``readability-lxml``— classic readability algorithm (optional)
3. bs4 heuristic       — largest semantic block; always available

Each stage must clear ``MIN_CHARS`` of extracted text or the cascade falls
through; ``None`` is returned only when every stage fails. Results carry the
``method`` and a coarse ``confidence`` so downstream consumers can distinguish
selector-extracted from generic-extracted content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Optional

# Minimum characters for extracted text to be considered a real article body.
MIN_CHARS = 200

# Tags whose content is always boilerplate — stripped before heuristic scoring.
_NOISE_TAGS = {
    "script", "style", "nav", "header", "footer", "aside",
    "form", "iframe", "button", "noscript", "figure", "figcaption",
}

# Semantic containers tried (in order) before the largest-<div> fallback.
_SEMANTIC_SELECTORS = (
    "article", "main", '[role="main"]',
    ".post-content", ".entry-content", ".article-body",
)

# Coarse confidence per extraction method: selector-based extraction elsewhere
# in the pipeline is implicitly 1.0; these are the degraded tiers below it.
_CONFIDENCE = {"trafilatura": 0.9, "readability": 0.7, "bs4-heuristic": 0.5}


@dataclass(frozen=True)
class ExtractResult:
    """Outcome of a successful cascade stage."""

    text: str
    method: str            # trafilatura | readability | bs4-heuristic
    confidence: float      # coarse tier, see _CONFIDENCE
    title: Optional[str] = None
    score_semantics: str = "heuristic_method_tier_not_probability"
    metadata: dict = field(default_factory=dict)


def extract_article(html, url: Optional[str] = None) -> Optional[ExtractResult]:
    """Extract the main article text from raw HTML, site-agnostically.

    ``html`` may be ``str`` or ``bytes``. Returns ``None`` only when every
    stage fails to produce ``MIN_CHARS`` of text — callers treat that as a
    genuine empty/non-article page.
    """
    if not html:
        return None
    text_html = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html

    result = _try_trafilatura(text_html, url) or _try_readability(text_html) or _try_bs4_heuristic(text_html)
    if result is None:
        return None
    from src.ingestion.structured_metadata import extract_metadata
    import importlib.metadata
    distribution = {'trafilatura':'trafilatura','readability':'readability-lxml','bs4-heuristic':'beautifulsoup4'}[result.method]
    try:
        version = importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        version = 'unavailable'
    metadata = extract_metadata(text_html, url)
    metadata['body_extractor'] = {'name':result.method,'version':version,'locator':'extracted-body; exact source spans unavailable'}
    return replace(result, metadata=metadata)


def _try_trafilatura(html: str, url: Optional[str]) -> Optional[ExtractResult]:
    try:
        import trafilatura  # type: ignore
    except ImportError:
        return None
    try:
        text = trafilatura.extract(html, url=url, include_comments=False)
    except Exception:  # noqa: BLE001 - a stage failure falls through, never raises
        return None
    text = _normalize(text or "")
    if len(text) < MIN_CHARS:
        return None
    title = None
    try:
        meta = trafilatura.extract_metadata(html)
        title = getattr(meta, "title", None) or None
    except Exception:  # noqa: BLE001
        pass
    return ExtractResult(text=text, method="trafilatura",
                         confidence=_CONFIDENCE["trafilatura"], title=title)


def _try_readability(html: str) -> Optional[ExtractResult]:
    try:
        from readability import Document  # type: ignore
    except ImportError:
        return None
    try:
        doc = Document(html)
        text = _normalize(_strip_tags(doc.summary()))
        title = (doc.short_title() or "").strip() or None
    except Exception:  # noqa: BLE001
        return None
    if len(text) < MIN_CHARS:
        return None
    return ExtractResult(text=text, method="readability",
                         confidence=_CONFIDENCE["readability"], title=title)


def _try_bs4_heuristic(html: str) -> Optional[ExtractResult]:
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a hard dep of the scrapers
        return None
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001
        return None

    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    title = None
    h1 = soup.find("h1")
    if h1 is not None:
        title = _normalize(h1.get_text(separator=" ")) or None

    for selector in _SEMANTIC_SELECTORS:
        el = soup.select_one(selector)
        if el:
            text = _normalize(el.get_text(separator=" "))
            if len(text) >= MIN_CHARS:
                return ExtractResult(text=text, method="bs4-heuristic",
                                     confidence=_CONFIDENCE["bs4-heuristic"], title=title)

    # Largest <div> by text length — last resort.
    best = ""
    for div in soup.find_all("div"):
        t = _normalize(div.get_text(separator=" "))
        if len(t) > len(best):
            best = t
    if len(best) >= MIN_CHARS:
        return ExtractResult(text=best, method="bs4-heuristic",
                             confidence=_CONFIDENCE["bs4-heuristic"], title=title)
    return None


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
