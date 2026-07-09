"""
NOESIS_* environment resolution with NEURONEWS_* fallbacks (R13 / Track N4).

The project is renaming from NeuroNews to Noesis. Configuration follows an
**alias-first** rule so nothing breaks during the transition: every setting is
read as ``NOESIS_<NAME>`` first and falls back to the legacy
``NEURONEWS_<NAME>``. Set either; the ``NOESIS_*`` form is canonical and the
``NEURONEWS_*`` form is a supported, deprecated alias.

This is the one shared resolver, so the whole config surface aliases
identically. Stdlib-only and import-safe: the MCP tool servers import it at
module load, so it must never pull a heavy dependency.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# The canonical prefix and its retained legacy alias.
CANONICAL_PREFIX = "NOESIS_"
LEGACY_PREFIX = "NEURONEWS_"


def resolve_env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Resolve one setting by its suffix (the part after the prefix).

    Reads ``NOESIS_<name>`` first, then ``NEURONEWS_<name>``, then ``default``.
    An empty string is a real value and is returned as-is; only an unset
    variable falls through.

    Args:
        name: the setting suffix, e.g. ``"DB_PATH"`` (no prefix).
        default: value when neither prefix is set.
    """
    suffix = name[len(CANONICAL_PREFIX):] if name.startswith(CANONICAL_PREFIX) else name
    suffix = suffix[len(LEGACY_PREFIX):] if suffix.startswith(LEGACY_PREFIX) else suffix
    value = os.environ.get(CANONICAL_PREFIX + suffix)
    if value is not None:
        return value
    value = os.environ.get(LEGACY_PREFIX + suffix)
    if value is not None:
        return value
    return default


def warehouse_path(default: Optional[str] = None) -> Optional[str]:
    """The DuckDB warehouse path (``NOESIS_DB_PATH`` / ``NEURONEWS_DB_PATH``).

    Falls back to ``<repo>/data/neuronews.duckdb`` when neither is set and no
    ``default`` is given. The default filename keeps the legacy on-disk name so
    existing warehouses keep working.
    """
    resolved = resolve_env("DB_PATH")
    if resolved is not None:
        return resolved
    if default is not None:
        return default
    return str(Path(__file__).resolve().parents[2] / "data" / "neuronews.duckdb")


def imagery_queue_path(default: Optional[str] = None) -> Optional[str]:
    """The dedicated OSINT imagery review-queue store path
    (``NOESIS_IMAGERY_QUEUE_PATH`` / ``NEURONEWS_IMAGERY_QUEUE_PATH``).

    The gated imagery tier reads corpus assets from the warehouse *read-only* but
    must write review-queue suggestions somewhere; per least privilege those
    writes go to this separate DuckDB file (holding only the review queue), never
    to the read-write corpus warehouse. Falls back to
    ``<repo>/data/osint_imagery_queue.duckdb`` when neither var nor ``default``
    is set.
    """
    resolved = resolve_env("IMAGERY_QUEUE_PATH")
    if resolved is not None:
        return resolved
    if default is not None:
        return default
    return str(Path(__file__).resolve().parents[2] / "data" / "osint_imagery_queue.duckdb")


def enabled_packs(default: str = "") -> str:
    """The enabled domain packs, comma-separated
    (``NOESIS_ENABLED_PACKS`` / ``NEURONEWS_ENABLED_PACKS``)."""
    return resolve_env("ENABLED_PACKS", default) or default
