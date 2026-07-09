"""Unit tests for LLM-assisted selector self-repair (#882) — offline, fake LLM."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("bs4")

from src.scraper.adaptive.selector_repair import (
    RepairResult,
    SelectorRepairError,
    apply_to_config,
    build_prompt,
    propose_selectors,
    repair_source,
    validate_selectors,
)
from src.scraper.sources_config import load_sources

_BODY = "The committee approved the infrastructure spending bill today. " * 10

# A "redesigned" page: article content now lives under new class names.
_HTML = f"""
<html><head><script>tracking();</script></head><body>
  <nav class="menu">Home Politics Business Sport Weather Subscribe Login</nav>
  <div class="story-wrap">
    <h1 class="story-hed">Spending bill approved</h1>
    <div class="story-body"><p>{_BODY}</p></div>
  </div>
  <footer>About Us Contact Careers Advertise Privacy Terms</footer>
</body></html>
"""

_GOOD = {"title": "h1.story-hed", "content": ".story-body p"}
_SOURCE = {"name": "Example", "base_url": "https://ex.com",
           "article_selectors": {"title": "h1.old-hed", "content": ".old-body p"}}


def _llm_returning(payload) -> callable:
    return lambda prompt: payload if isinstance(payload, str) else json.dumps(payload)


# --------------------------------------------------------------------------- #
# Proposal
# --------------------------------------------------------------------------- #


def test_prompt_contains_fields_and_reduced_html():
    prompt = build_prompt(_HTML, fields=("title", "content"))
    assert "title, content" in prompt
    assert "story-hed" in prompt
    assert "tracking()" not in prompt  # scripts stripped from the LLM view


def test_prompt_truncates_huge_pages():
    huge = "<html><body>" + "<div>x</div>" * 100_000 + "</body></html>"
    assert len(build_prompt(huge)) < 25_000


def test_propose_parses_strict_json():
    assert propose_selectors(_HTML, _llm_returning(_GOOD)) == _GOOD


def test_propose_recovers_json_from_chatty_response():
    chatty = "Sure! Here are the selectors:\n" + json.dumps(_GOOD) + "\nGood luck!"
    assert propose_selectors(_HTML, _llm_returning(chatty)) == _GOOD


def test_propose_drops_non_string_and_unrequested_fields():
    payload = {"title": "h1", "content": 42, "junk": "div", "author": "  "}
    out = propose_selectors(_HTML, _llm_returning(payload))
    assert out == {"title": "h1"}


def test_propose_unparseable_raises():
    with pytest.raises(SelectorRepairError):
        propose_selectors(_HTML, _llm_returning("I cannot help with that."))


# --------------------------------------------------------------------------- #
# Validation — the generic-cascade text is ground truth
# --------------------------------------------------------------------------- #


def test_correct_selectors_validate_against_reference():
    results = validate_selectors(_HTML, _GOOD, reference_text=_BODY)
    assert results["title"]["ok"]
    assert results["content"]["ok"]
    assert results["content"]["overlap"] > 0.9


def test_wrong_block_selector_fails_validation():
    """A candidate that grabs the nav must be rejected (#882 acceptance)."""
    results = validate_selectors(
        _HTML, {"content": "nav.menu"}, reference_text=_BODY
    )
    assert not results["content"]["ok"]


def test_invalid_css_and_no_match_reported():
    results = validate_selectors(
        _HTML, {"title": "h1..[", "content": ".does-not-exist"},
        reference_text=_BODY,
    )
    assert not results["title"]["ok"] and "invalid selector" in results["title"]["error"]
    assert not results["content"]["ok"] and results["content"]["error"] == "selector matched nothing"


# --------------------------------------------------------------------------- #
# End-to-end repair (no reference supplied -> cascade provides it)
# --------------------------------------------------------------------------- #


def test_repair_source_accepts_good_proposal():
    result = repair_source(_SOURCE, _HTML, _llm_returning(_GOOD))
    assert isinstance(result, RepairResult)
    assert result.accepted
    assert result.selectors == _GOOD
    assert result.report["source"] == "Example"


def test_repair_source_rejects_nav_grabbing_proposal():
    bad = {"title": "h1.story-hed", "content": "footer"}
    result = repair_source(_SOURCE, _HTML, _llm_returning(bad))
    assert not result.accepted
    assert "content" not in result.selectors


def test_repair_source_survives_unparseable_llm():
    result = repair_source(_SOURCE, _HTML, _llm_returning("no json here"))
    assert not result.accepted
    assert "error" in result.report


def test_repair_never_writes_config(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"sources": [dict(_SOURCE, enabled=True)]}))
    before = cfg.read_text()
    repair_source(_SOURCE, _HTML, _llm_returning(_GOOD))
    assert cfg.read_text() == before  # repair proposes; only apply_to_config persists


# --------------------------------------------------------------------------- #
# Explicit persistence
# --------------------------------------------------------------------------- #


def test_apply_to_config_merges_and_roundtrips(tmp_path):
    cfg = tmp_path / "config.json"
    entry = {
        "name": "Example", "base_url": "https://ex.com",
        "article_selectors": {"title": "h1.old", "content": ".old p", "date": "time"},
        "enabled": True,
    }
    cfg.write_text(json.dumps({"sources": [entry]}))

    apply_to_config(str(cfg), "Example", _GOOD)

    (loaded,) = load_sources(str(cfg))  # still valid per the canonical loader (#881)
    assert loaded["article_selectors"]["title"] == "h1.story-hed"
    assert loaded["article_selectors"]["date"] == "time"  # untouched fields kept


def test_apply_to_config_unknown_source_raises(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"sources": []}))
    with pytest.raises(KeyError):
        apply_to_config(str(cfg), "Nope", _GOOD)
