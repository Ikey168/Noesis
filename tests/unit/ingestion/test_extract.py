"""Unit tests for the generic article-extraction cascade (#877) — offline.

The CI gate installs neither trafilatura nor readability-lxml, so these tests
exercise the always-available bs4 stage plus the cascade's fall-through and
degradation contracts. When the optional libraries ARE installed locally, the
same assertions still hold (the cascade just answers from an earlier stage
with higher confidence).
"""

from __future__ import annotations

import pytest

pytest.importorskip("bs4")

from src.ingestion.extract import MIN_CHARS, ExtractResult, extract_article

_BODY = "Parliament voted on the budget today. " * 12  # comfortably > MIN_CHARS


def _page(body_html: str) -> str:
    return f"""
    <html><head><title>t</title><script>var x = 1;</script></head>
    <body>
      <nav>Home News Sport Weather More Links Nav Nav Nav</nav>
      {body_html}
      <footer>Copyright Notice And A Lot Of Boilerplate Text Here</footer>
    </body></html>
    """


def test_extracts_article_tag():
    html = _page(f"<article><h2>Budget vote</h2><p>{_BODY}</p></article>")
    result = extract_article(html)
    assert result is not None
    assert "Parliament voted" in result.text
    assert result.confidence > 0
    assert result.method in {"trafilatura", "readability", "bs4-heuristic"}


def test_boilerplate_excluded_from_bs4_stage():
    html = _page(f"<main><p>{_BODY}</p></main>")
    result = extract_article(html)
    assert result is not None
    assert "Copyright Notice" not in result.text
    assert "Home News Sport" not in result.text


def test_h1_reported_as_title():
    html = _page(f"<h1>Budget approved</h1><article><p>{_BODY}</p></article>")
    result = extract_article(html)
    assert result is not None
    if result.method == "bs4-heuristic":  # optional libs may title differently
        assert result.title == "Budget approved"
    else:
        assert result.title  # some title extracted


def test_largest_div_fallback_without_semantic_tags():
    html = _page(f"<div class='x'>short</div><div class='y'><p>{_BODY}</p></div>")
    result = extract_article(html)
    assert result is not None
    assert "Parliament voted" in result.text


def test_short_content_returns_none():
    assert extract_article(_page("<article><p>too short</p></article>")) is None


def test_empty_input_returns_none():
    assert extract_article("") is None
    assert extract_article(b"") is None
    assert extract_article(None) is None


def test_bytes_input_accepted():
    html = _page(f"<article><p>{_BODY}</p></article>").encode("utf-8")
    result = extract_article(html)
    assert result is not None and "Parliament voted" in result.text


def test_confidence_tiers_are_ordered():
    # The contract downstream consumers rely on: generic methods rank below
    # selector extraction (1.0) and are ordered by expected quality.
    html = _page(f"<article><p>{_BODY}</p></article>")
    result = extract_article(html)
    assert result is not None
    assert 0 < result.confidence < 1.0


def test_result_is_frozen_dataclass():
    r = ExtractResult(text="x", method="bs4-heuristic", confidence=0.5)
    with pytest.raises(AttributeError):
        r.text = "y"  # type: ignore[misc]


def test_min_chars_constant_sane():
    assert 50 <= MIN_CHARS <= 1000


def test_blog_readability_delegates_to_cascade():
    """The blog connector's fetch_full_text now answers via the shared cascade."""
    from src.ingestion.connectors.blog.readability import fetch_full_text

    html = _page(f"<article><p>{_BODY}</p></article>").encode("utf-8")
    text = fetch_full_text("https://example.com/post", _http_get=lambda url: html)
    assert "Parliament voted" in text

    # Network failure still degrades to empty string, never raises.
    def boom(url):
        raise ConnectionError("down")

    assert fetch_full_text("https://example.com/post", _http_get=boom) == ""
