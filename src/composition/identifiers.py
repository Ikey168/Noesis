"""Preserved-identifier inventory (C01.4, #1801).

The composed catalog must keep emitting every MCP server name, alias, tool ID
and required-data label that the legacy catalog emits today, and every HTTP
route module a code-registered domain pack mounts. :func:`preserved_identifiers`
derives that list from a built catalog so it can be committed as a fixture and
compared against the composed catalog (C04.1).
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "tests/fixtures/composition/preserved_identifiers.json"
CODE_REGISTERED_DOMAINS = (
    "news", "research", "legal", "economic", "political", "technical", "market",
)


def code_registered_packs() -> dict[str, Any]:
    """Import every built-in domain module and return the registered packs."""

    from src.domains import registry

    for module in CODE_REGISTERED_DOMAINS:
        importlib.import_module(f"src.domains.{module}")
    return dict(registry._REGISTRY)


def preserved_identifiers(catalog: Mapping[str, Any]) -> dict[str, Any]:
    """Identifiers the composed catalog must keep emitting unchanged."""

    from src.mcp_host.catalog import STATES

    servers = {
        server["name"]: {
            "aliases": sorted(server.get("aliases", [])),
            "pack": server.get("pack"),
            "kind": server.get("kind"),
        }
        for server in catalog["servers"]
    }
    tools = {
        tool["id"]: {
            "required_data": list(tool["required_data"]),
            "required_scopes": list(tool["required_scopes"]),
            "mutability": tool["mutability"],
        }
        for tool in catalog["tools"]
    }
    domain_packs = {
        name: {
            "route_modules": list(pack.route_modules),
            "ui_flags": sorted(pack.ui_flags),
            "enrichers": [enricher.name for enricher in pack.enrichers],
        }
        for name, pack in sorted(code_registered_packs().items())
    }
    return {
        "contract": "noesis-composition-preserved-identifiers-v1",
        "states": sorted(STATES),
        "servers": dict(sorted(servers.items())),
        "tools": dict(sorted(tools.items())),
        "domain_packs": domain_packs,
    }


def build_reference_catalog(**overrides: Any) -> dict[str, Any]:
    """The catalog exactly as ``scripts/generate_mcp_catalog.py`` builds it."""

    import json

    from src.mcp_host.catalog import PACK_CONFIG, build_catalog_sync

    pack_payload = json.loads(PACK_CONFIG.read_text(encoding="utf-8"))
    arguments = {
        "granted_scopes": {"public", "knowledge:read", "operator"},
        "enabled_pack_names": pack_payload.get("enabled_packs", []),
        "configured_backends": set(),
        "include_unusable": True,
    }
    arguments.update(overrides)
    return build_catalog_sync(**arguments)


__all__ = [
    "CODE_REGISTERED_DOMAINS",
    "FIXTURE",
    "build_reference_catalog",
    "code_registered_packs",
    "preserved_identifiers",
]
