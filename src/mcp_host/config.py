"""
MCP host configuration: which servers the API process supervises.

The single source of truth is the repo's ``.mcp.json`` (the same file
developer MCP hosts read), filtered down to the project's own stdio
servers — the ``tools/*_mcp`` Python processes. Third-party servers in
that file (npx-launched memory/playwright/postgres helpers) are for
interactive tooling and are never supervised by the API.

Stdlib-only on purpose: importing this module must never fail, otherwise
the feature-flag route registration in src/api/app.py silently disables
the endpoints that report host health.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MCP_JSON = REPO_ROOT / ".mcp.json"

_PYTHON_COMMANDS = ("python", "python3")

LEGACY_SERVER_ALIASES = {
    "neuronews-pipeline": "noesis-pipeline",
    "neuronews-contracts": "noesis-contracts",
    "neuronews-lineage": "noesis-lineage",
    "neuronews-blog-feeds": "noesis-blog-feeds",
    "neuronews-domain-packs": "noesis-domain-packs",
    "neuronews-research": "noesis-research",
    "neuronews-provisioning": "noesis-provisioning",
    "neuronews-osint": "noesis-osint",
    "neuronews-dataset": "noesis-dataset",
    "neuronews-statistics": "noesis-statistics",
    "neuronews-arguments": "noesis-arguments",
    "neuronews-kg": "noesis-kg",
    "neuronews-sources": "noesis-sources",
    "neuronews-security": "noesis-security",
    "neuronews-monitoring": "noesis-monitoring",
}
LEGACY_SERVER_REMOVAL_TARGET = "Noesis 2.0 (not before 2027-09-01)"


@dataclass(frozen=True)
class ServerSpec:
    """One supervised stdio MCP server."""

    name: str
    command: str
    args: Tuple[str, ...]
    env: Dict[str, str] = field(default_factory=dict)
    cwd: str = str(REPO_ROOT)
    aliases: Tuple[str, ...] = ()


def resolve_server_name(name: str, *, warn: bool = True) -> str:
    """Resolve a supported legacy public server name to its canonical name."""

    canonical = LEGACY_SERVER_ALIASES.get(str(name), str(name))
    if warn and canonical != name:
        warnings.warn(
            f"MCP server name {name!r} is deprecated; use {canonical!r}. "
            f"Removal target: {LEGACY_SERVER_REMOVAL_TARGET}.",
            DeprecationWarning,
            stacklevel=2,
        )
    return canonical


def _is_project_server(entry: dict) -> bool:
    """Keep only the repo's own python stdio servers (tools/*_mcp)."""
    if entry.get("type", "stdio") != "stdio":
        return False
    if entry.get("command") not in _PYTHON_COMMANDS:
        return False
    args = entry.get("args") or []
    return bool(args) and isinstance(args[0], str) and args[0].startswith("tools/")


def load_server_specs(path: Optional[Path] = None) -> List[ServerSpec]:
    """Parse .mcp.json into the list of servers the host supervises.

    Never raises: a missing or malformed file yields an empty list (the
    host then reports zero servers rather than breaking API startup).
    """
    mcp_json = Path(path) if path is not None else DEFAULT_MCP_JSON
    try:
        raw = json.loads(mcp_json.read_text(encoding="utf-8"))
        servers = raw.get("mcpServers", {})
        if not isinstance(servers, dict):
            return []
    except (OSError, ValueError):
        return []

    specs: List[ServerSpec] = []
    seen: set[str] = set()
    aliases = dict(LEGACY_SERVER_ALIASES)
    configured_aliases = raw.get("compatibilityAliases") or {}
    if isinstance(configured_aliases, dict):
        aliases.update(
            {str(alias): str(target) for alias, target in configured_aliases.items()}
        )
    for name, entry in servers.items():
        if not isinstance(entry, dict) or not _is_project_server(entry):
            continue
        canonical = aliases.get(str(name), str(name))
        if canonical != name:
            warnings.warn(
                f"MCP server name {name!r} is deprecated; use {canonical!r}. "
                f"Removal target: {LEGACY_SERVER_REMOVAL_TARGET}.",
                DeprecationWarning,
                stacklevel=2,
            )
        if canonical in seen:
            continue
        seen.add(canonical)
        env = entry.get("env") or {}
        specs.append(
            ServerSpec(
                name=canonical,
                command=str(entry["command"]),
                args=tuple(str(a) for a in entry.get("args", [])),
                env={str(k): str(v) for k, v in env.items()},
                cwd=str(mcp_json.parent),
                aliases=tuple(
                    sorted(alias for alias, target in aliases.items() if target == canonical)
                ),
            )
        )
    return specs
