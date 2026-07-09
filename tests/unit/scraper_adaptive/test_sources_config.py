"""Unit tests for the canonical async-sources config loader (#881) — offline.

Deliberately does NOT import the async engine (aiohttp/playwright); the loader
is stdlib-only so this file runs in the curated CI gate.
"""

from __future__ import annotations

import json

import pytest

from src.scraper.sources_config import (
    CONFIG_PATH,
    SourcesConfigError,
    enabled_sources,
    load_sources,
)


def _write(tmp_path, payload) -> str:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def _entry(**overrides):
    base = {
        "name": "Example",
        "base_url": "https://example.com",
        "article_selectors": {"title": "h1", "content": "article p"},
        "link_patterns": ["/news/.*"],
        "requires_js": False,
        "rate_limit": 1.5,
        "enabled": True,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# The repo's real config is the single source of truth — it must always load.
# --------------------------------------------------------------------------- #


def test_repo_config_loads_and_validates():
    sources = load_sources()
    assert CONFIG_PATH.exists()
    assert len(sources) >= 5
    names = {s["name"] for s in sources}
    assert "BBC" in names
    for s in sources:
        assert s["article_selectors"]["title"]
        assert s["article_selectors"]["content"]


def test_enabled_sources_filters_disabled(tmp_path):
    path = _write(tmp_path, {"sources": [
        _entry(name="On"),
        _entry(name="Off", enabled=False),
        _entry(name="Implicit"),
    ]})
    assert [s["name"] for s in load_sources(path)] == ["On", "Off", "Implicit"]
    assert [s["name"] for s in enabled_sources(path)] == ["On", "Implicit"]


# --------------------------------------------------------------------------- #
# Malformed config fails loudly, pointing at the offending entry.
# --------------------------------------------------------------------------- #


def test_missing_file_raises():
    with pytest.raises(SourcesConfigError, match="cannot read"):
        load_sources("/nonexistent/config.json")


def test_invalid_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SourcesConfigError, match="not valid JSON"):
        load_sources(str(p))


def test_missing_sources_list_raises(tmp_path):
    with pytest.raises(SourcesConfigError, match="non-empty 'sources'"):
        load_sources(_write(tmp_path, {"sources": []}))


@pytest.mark.parametrize("break_it, message", [
    (lambda e: e.pop("name"), "missing required field 'name'"),
    (lambda e: e.pop("article_selectors"), "missing required field 'article_selectors'"),
    (lambda e: e.__setitem__("article_selectors", "h1"), "must be an object"),
    (lambda e: e["article_selectors"].pop("content"), "non-empty 'content' selector"),
    (lambda e: e.__setitem__("rate_limit", "fast"), "'rate_limit' must be a number"),
    (lambda e: e.__setitem__("requires_js", "yes"), "'requires_js' must be a boolean"),
    (lambda e: e.__setitem__("link_patterns", "/news/"), "'link_patterns' must be a list"),
])
def test_invalid_entry_raises_with_location(tmp_path, break_it, message):
    entry = _entry()
    break_it(entry)
    path = _write(tmp_path, {"sources": [_entry(name="fine"), entry]})
    with pytest.raises(SourcesConfigError, match="sources\\[1\\]"):
        try:
            load_sources(path)
        except SourcesConfigError as exc:
            assert message in str(exc)
            raise
