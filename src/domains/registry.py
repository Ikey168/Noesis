"""
Domain-pack registry and feature-flag loader.

Call :func:`load_config` once at application startup to read
``config/domain_packs.json`` and enable the listed packs. Every built-in
pack should call :func:`register_pack` on import (the news pack does this
in ``src.domains.news``).

Thread-safety note: packs are registered at startup and never mutated
at runtime, so plain dicts are fine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.domains.base import DomainPack

_REGISTRY: Dict[str, DomainPack] = {}
_ENABLED: set = set()
# Composition authority (C05.5): after a bundle is cut over to composition
# management the lifecycle coordinator is the only authority for its
# enablement. Legacy calls for such bundles delegate to it or raise
# CompositionAuthorityError; no second enabled-state ledger is kept.
_AUTHORITY: Optional[Any] = None


class CompositionAuthorityError(RuntimeError):
    """A legacy enablement call targeted a composition-managed bundle."""

    code = "composition_managed"


def set_authority(authority: Optional[Any]) -> None:
    """Install (or clear) the composition authority.

    The object provides ``manages(name) -> bool`` and ``legacy_enable(name)`` /
    ``legacy_disable(name)``, which either leave the state the coordinator
    would produce or raise :class:`CompositionAuthorityError`.
    """
    global _AUTHORITY
    _AUTHORITY = authority


def _managed(name: str) -> bool:
    return _AUTHORITY is not None and bool(_AUTHORITY.manages(name))


def apply_enabled(name: str, enabled: bool) -> None:
    """Set enablement on behalf of the composition coordinator (its only writer)."""
    if enabled:
        _ENABLED.add(name)
    else:
        _ENABLED.discard(name)


_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "domain_packs.json"


def register_pack(pack: DomainPack) -> None:
    """Add a :class:`DomainPack` to the registry.

    If a pack with the same name is already registered it is replaced.
    """
    _REGISTRY[pack.name] = pack


def enable_pack(name: str) -> None:
    """Mark the named pack as enabled (without requiring it to be registered yet)."""
    if _managed(name):
        _AUTHORITY.legacy_enable(name)
        return
    _ENABLED.add(name)


def disable_pack(name: str) -> None:
    """Mark the named pack as disabled."""
    if _managed(name):
        _AUTHORITY.legacy_disable(name)
        return
    _ENABLED.discard(name)


def is_pack_enabled(name: str) -> bool:
    """Return True if the named pack is both registered and enabled."""
    return name in _ENABLED and name in _REGISTRY


def get_pack(name: str) -> Optional[DomainPack]:
    """Return the registered pack or None."""
    return _REGISTRY.get(name)


def get_enabled_packs() -> List[DomainPack]:
    """Return all registered packs that are currently enabled."""
    return [_REGISTRY[n] for n in _ENABLED if n in _REGISTRY]


def load_config(path: Optional[str] = None) -> List[str]:
    """Read ``config/domain_packs.json`` and enable the listed packs.

    The JSON format is::

        {
          "enabled_packs": ["news"]
        }

    Returns the list of pack names that were enabled. Packs listed in the
    config that have not been registered yet are still added to the enabled
    set; they will become active once :func:`register_pack` is called (which
    happens when the pack module is imported).

    Falls back to enabling ``news`` if the config file is absent so that a
    fresh checkout keeps working without any extra setup.
    """
    config_path = Path(path) if path else _DEFAULT_CONFIG_PATH
    try:
        with open(config_path) as fh:
            data = json.load(fh)
        enabled = data.get("enabled_packs", ["news"])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        enabled = ["news"]

    # Also honour the NOESIS_ENABLED_PACKS env var (NEURONEWS_ENABLED_PACKS is
    # a retained alias), comma-separated, useful for test fixtures and CI.
    from src.config.env import enabled_packs as _enabled_packs

    env_override = _enabled_packs().strip()
    if env_override:
        enabled = [p.strip() for p in env_override.split(",") if p.strip()]

    # The config file is authoritative only for bundles still under legacy
    # authority; composition-managed enablement is left as the coordinator set it.
    managed_enabled = {name for name in _ENABLED if _managed(name)}
    _ENABLED.clear()
    _ENABLED.update(managed_enabled)
    for name in enabled:
        if not _managed(name):
            enable_pack(name)
    return list(enabled)


def reset() -> None:
    """Clear all registered packs and enabled flags. For test use only."""
    _REGISTRY.clear()
    _ENABLED.clear()
    set_authority(None)
