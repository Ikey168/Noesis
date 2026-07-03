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
from typing import Dict, List, Optional

from src.domains.base import DomainPack

_REGISTRY: Dict[str, DomainPack] = {}
_ENABLED: set = set()

_DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "domain_packs.json"


def register_pack(pack: DomainPack) -> None:
    """Add a :class:`DomainPack` to the registry.

    If a pack with the same name is already registered it is replaced.
    """
    _REGISTRY[pack.name] = pack


def enable_pack(name: str) -> None:
    """Mark the named pack as enabled (without requiring it to be registered yet)."""
    _ENABLED.add(name)


def disable_pack(name: str) -> None:
    """Mark the named pack as disabled."""
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

    _ENABLED.clear()
    for name in enabled:
        enable_pack(name)
    return list(enabled)


def reset() -> None:
    """Clear all registered packs and enabled flags. For test use only."""
    _REGISTRY.clear()
    _ENABLED.clear()
