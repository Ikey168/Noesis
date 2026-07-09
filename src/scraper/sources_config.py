"""
Canonical async-scraper source configuration loader (#881).

The async engine's sources were defined twice — ``config_async.json`` and the
``ASYNC_NEWS_SOURCES`` literal in ``async_scraper_engine.py`` — and the copies
drifted. This stdlib-only module makes the JSON file the single source of
truth: the engine builds its ``NewsSource`` list through :func:`load_sources`,
and adaptivity features that read or patch selectors (drift detection #878,
selector repair #882) have one canonical place to work with.

Import-safe by design (no aiohttp/playwright), so it is unit-testable in the
curated CI gate. Malformed config fails loudly with a path to the offending
entry — never a silent partial load.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_PATH = Path(__file__).parent / "config_async.json"

_REQUIRED = ("name", "base_url", "article_selectors")
_SELECTOR_FIELDS = ("title", "content")  # selectors an entry must at least define


class SourcesConfigError(ValueError):
    """Raised when the sources config is missing, unparsable, or invalid."""


def load_sources(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load and validate the ``sources[]`` list from the async config JSON.

    Returns every entry (including disabled ones) as validated dicts; use
    :func:`enabled_sources` for the fetchable subset. Raises
    :class:`SourcesConfigError` with a precise message on any problem.
    """
    cfg_path = Path(path) if path else CONFIG_PATH
    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SourcesConfigError(f"cannot read sources config {cfg_path}: {exc}") from exc
    try:
        config = json.loads(raw)
    except ValueError as exc:
        raise SourcesConfigError(f"sources config {cfg_path} is not valid JSON: {exc}") from exc

    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise SourcesConfigError(f"{cfg_path}: expected a non-empty 'sources' list")

    for i, entry in enumerate(sources):
        _validate_entry(entry, f"{cfg_path} sources[{i}]")
    return sources


def enabled_sources(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """The subset of :func:`load_sources` entries with ``enabled`` != false."""
    return [s for s in load_sources(path) if s.get("enabled", True)]


def _validate_entry(entry: Any, where: str) -> None:
    if not isinstance(entry, dict):
        raise SourcesConfigError(f"{where}: entry must be an object")
    for field in _REQUIRED:
        if not entry.get(field):
            raise SourcesConfigError(f"{where}: missing required field '{field}'")
    selectors = entry["article_selectors"]
    if not isinstance(selectors, dict):
        raise SourcesConfigError(f"{where}: 'article_selectors' must be an object")
    for field in _SELECTOR_FIELDS:
        if not isinstance(selectors.get(field), str) or not selectors[field].strip():
            raise SourcesConfigError(
                f"{where}: article_selectors must define a non-empty '{field}' selector"
            )
    if "rate_limit" in entry and not isinstance(entry["rate_limit"], (int, float)):
        raise SourcesConfigError(f"{where}: 'rate_limit' must be a number")
    if "requires_js" in entry and not isinstance(entry["requires_js"], bool):
        raise SourcesConfigError(f"{where}: 'requires_js' must be a boolean")
    if "link_patterns" in entry and not isinstance(entry["link_patterns"], list):
        raise SourcesConfigError(f"{where}: 'link_patterns' must be a list")
