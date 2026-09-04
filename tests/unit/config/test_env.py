"""Tests for the NOESIS_* / NEURONEWS_* env resolution (R13 #623).

The R13 exit criterion for the naming sweep: both env prefixes resolve
identically, alias-first (NOESIS_* canonical, NEURONEWS_* fallback).
"""

import pytest

from src.config.env import (
    enabled_packs,
    imagery_queue_path,
    resolve_env,
    warehouse_path,
)


def test_canonical_prefix_wins(monkeypatch):
    monkeypatch.setenv("NOESIS_DB_PATH", "/canonical.db")
    monkeypatch.setenv("NEURONEWS_DB_PATH", "/legacy.db")
    assert resolve_env("DB_PATH") == "/canonical.db"


def test_legacy_prefix_is_the_fallback(monkeypatch):
    monkeypatch.delenv("NOESIS_DB_PATH", raising=False)
    monkeypatch.setenv("NEURONEWS_DB_PATH", "/legacy.db")
    with pytest.warns(DeprecationWarning, match="NOESIS_DB_PATH"):
        assert resolve_env("DB_PATH") == "/legacy.db"


def test_both_prefixes_resolve_identically(monkeypatch):
    # Set only NOESIS_, then only NEURONEWS_: same resolved value.
    monkeypatch.delenv("NEURONEWS_ENABLED_PACKS", raising=False)
    monkeypatch.setenv("NOESIS_ENABLED_PACKS", "research")
    via_noesis = resolve_env("ENABLED_PACKS")

    monkeypatch.delenv("NOESIS_ENABLED_PACKS", raising=False)
    monkeypatch.setenv("NEURONEWS_ENABLED_PACKS", "research")
    with pytest.warns(DeprecationWarning, match="NOESIS_ENABLED_PACKS"):
        via_legacy = resolve_env("ENABLED_PACKS")

    assert via_noesis == via_legacy == "research"


def test_default_when_neither_set(monkeypatch):
    monkeypatch.delenv("NOESIS_MISSING", raising=False)
    monkeypatch.delenv("NEURONEWS_MISSING", raising=False)
    assert resolve_env("MISSING", "fallback") == "fallback"
    assert resolve_env("MISSING") is None


def test_suffix_accepts_a_full_prefixed_name(monkeypatch):
    monkeypatch.setenv("NOESIS_DB_PATH", "/x.db")
    # Passing the already-prefixed name resolves the same suffix.
    assert resolve_env("NOESIS_DB_PATH") == "/x.db"
    assert resolve_env("NEURONEWS_DB_PATH") == "/x.db"


def test_empty_string_is_a_real_value(monkeypatch):
    monkeypatch.setenv("NOESIS_ENABLED_PACKS", "")
    monkeypatch.setenv("NEURONEWS_ENABLED_PACKS", "news")
    # An explicit empty NOESIS_ value wins over the legacy fallback.
    assert resolve_env("ENABLED_PACKS") == ""


def test_warehouse_path_prefers_env_then_default(monkeypatch):
    monkeypatch.setenv("NEURONEWS_DB_PATH", "/data/wh.db")
    monkeypatch.delenv("NOESIS_DB_PATH", raising=False)
    with pytest.warns(DeprecationWarning, match="NOESIS_DB_PATH"):
        assert warehouse_path() == "/data/wh.db"
    monkeypatch.setenv("NOESIS_DB_PATH", "/data/noesis.db")
    assert warehouse_path() == "/data/noesis.db"


def test_warehouse_path_default_keeps_legacy_filename(monkeypatch):
    monkeypatch.delenv("NOESIS_DB_PATH", raising=False)
    monkeypatch.delenv("NEURONEWS_DB_PATH", raising=False)
    assert warehouse_path().endswith("data/neuronews.duckdb")
    assert warehouse_path("/override.db") == "/override.db"


def test_imagery_queue_path_prefers_env_then_default(monkeypatch):
    monkeypatch.delenv("NOESIS_IMAGERY_QUEUE_PATH", raising=False)
    monkeypatch.setenv("NEURONEWS_IMAGERY_QUEUE_PATH", "/data/q.db")
    with pytest.warns(DeprecationWarning, match="NOESIS_IMAGERY_QUEUE_PATH"):
        assert imagery_queue_path() == "/data/q.db"
    monkeypatch.setenv("NOESIS_IMAGERY_QUEUE_PATH", "/data/noesis-q.db")
    assert imagery_queue_path() == "/data/noesis-q.db"


def test_imagery_queue_path_defaults_to_a_separate_store(monkeypatch):
    monkeypatch.delenv("NOESIS_IMAGERY_QUEUE_PATH", raising=False)
    monkeypatch.delenv("NEURONEWS_IMAGERY_QUEUE_PATH", raising=False)
    # A dedicated file, distinct from the corpus warehouse (least privilege).
    assert imagery_queue_path().endswith("data/osint_imagery_queue.duckdb")
    assert imagery_queue_path() != warehouse_path()
    assert imagery_queue_path("/override-q.db") == "/override-q.db"


def test_enabled_packs_helper(monkeypatch):
    monkeypatch.delenv("NOESIS_ENABLED_PACKS", raising=False)
    monkeypatch.delenv("NEURONEWS_ENABLED_PACKS", raising=False)
    assert enabled_packs() == ""
    monkeypatch.setenv("NOESIS_ENABLED_PACKS", "research,legal")
    assert enabled_packs() == "research,legal"
