"""
NeuroNews Domain-packs MCP server.

Developer tool for managing the domain-pack system (issue #520) without
restarting the API server. Packs bundle the enrichers, FastAPI routes, and
UI feature flags that belong to one knowledge domain. The ``news`` pack ships
enabled; future packs (research, legal, finance, …) follow the same pattern.

Tools
-----
list_packs()
    All registered packs with their enabled status, enricher names,
    route modules, and UI flags.

pack_status(name)
    Detailed view of one pack: description, source_types, enrichers
    (with applies_to), route_modules, ui_flags, enabled.

enable_pack(name)
    Enable a pack by name. Writes ``config/domain_packs.json`` so the
    change persists across API restarts.

disable_pack(name)
    Disable a pack by name. Same persistence behaviour.

run_enrichers(document, pack?)
    Run all enabled-pack enrichers (or just one pack) against a document
    dict and return each enricher's output. Useful for testing a new
    enricher without wiring it into a full pipeline run.

get_ui_flags(pack?)
    Return the merged UI feature-flag map for all enabled packs (or one
    specific pack), suitable for pasting into a frontend config.

Design constraints (same as other MCP servers in this repo):
  * Lazy imports inside every tool body — the server starts in <100 ms
    and never triggers torch/transformers import graphs.
  * Summaries, not payloads — enricher output is returned as-is (it is
    usually a small dict), but content fields in document input are
    previewed at 300 chars to avoid flooding the context.
  * ``config/domain_packs.json`` is the single source of truth for
    persisted state; in-memory enable/disable also works for the current
    process lifetime.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CONFIG_PATH = REPO_ROOT / "config" / "domain_packs.json"

mcp = FastMCP("neuronews-domain-packs")

CONTENT_PREVIEW = 300


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _registry():
    """Lazy-import the domain registry after ensuring built-in packs are registered."""
    import src.domains.news  # noqa: F401 — triggers register_pack(NewsDomainPack)
    import src.domains.research  # noqa: F401 — triggers register_pack(ResearchDomainPack)
    import src.domains.technical  # noqa: F401 — registers TechnicalDomainPack
    from src.domains import registry
    return registry


def _load_and_sync():
    """Load config file into the in-process registry so the two stay in sync."""
    reg = _registry()
    reg.load_config(str(CONFIG_PATH))
    return reg


def _write_config(enabled_names: list) -> None:
    """Persist the enabled pack list to config/domain_packs.json."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"enabled_packs": enabled_names}, indent=2) + "\n")


def _pack_summary(pack, reg) -> dict:
    return {
        "name": pack.name,
        "enabled": reg.is_pack_enabled(pack.name),
        "description": pack.description,
        "source_types": pack.source_types,
        "enrichers": [e.name for e in pack.enrichers],
        "route_modules": pack.route_modules,
        "ui_flags": pack.ui_flags,
    }


def _preview_doc(document: dict) -> dict:
    """Return a copy with the content field truncated for display."""
    d = dict(document)
    if "content" in d and isinstance(d["content"], str) and len(d["content"]) > CONTENT_PREVIEW:
        d["content"] = d["content"][:CONTENT_PREVIEW] + f"… [{len(document['content'])} chars total]"
    return d


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool
def list_packs() -> dict:
    """List all registered domain packs with their enabled status and capabilities.

    Returns a dict with:
      count         - total registered packs
      enabled_count - number currently enabled
      packs         - list of pack summaries (name, enabled, source_types,
                      enricher names, route_modules, ui_flags)
    """
    reg = _load_and_sync()
    all_packs = list(reg._REGISTRY.values())
    summaries = [_pack_summary(p, reg) for p in all_packs]
    return {
        "count": len(all_packs),
        "enabled_count": sum(1 for s in summaries if s["enabled"]),
        "config_path": str(CONFIG_PATH),
        "packs": summaries,
    }


@mcp.tool
def pack_status(name: str) -> dict:
    """Return the full status of a single domain pack.

    Args:
        name: Pack name, e.g. "news".

    Returns a dict with all DomainPack fields plus enabled status and each
    enricher's source_type allowlist and description.
    """
    reg = _load_and_sync()
    pack = reg.get_pack(name)
    if pack is None:
        registered = sorted(reg._REGISTRY.keys())
        return {
            "error": f"Pack {name!r} is not registered.",
            "registered_packs": registered,
        }
    return {
        "name": pack.name,
        "enabled": reg.is_pack_enabled(name),
        "description": pack.description,
        "source_types": pack.source_types,
        "enrichers": [
            {
                "name": e.name,
                "description": e.description,
                "applies_to": e.source_types if e.source_types else ["all"],
            }
            for e in pack.enrichers
        ],
        "route_modules": pack.route_modules,
        "ui_flags": pack.ui_flags,
    }


@mcp.tool
def enable_pack(name: str) -> dict:
    """Enable a domain pack and persist the change to config/domain_packs.json.

    Args:
        name: Pack name, e.g. "news".

    The API server reads domain_packs.json at startup; to apply the change to
    a running server without restart, call load_config() from the app.
    """
    reg = _load_and_sync()
    if reg.get_pack(name) is None:
        return {
            "error": f"Pack {name!r} is not registered — cannot enable.",
            "registered_packs": sorted(reg._REGISTRY.keys()),
        }
    reg.enable_pack(name)
    enabled = sorted(reg._ENABLED)
    _write_config(enabled)
    return {
        "status": "enabled",
        "pack": name,
        "enabled_packs": enabled,
        "config_path": str(CONFIG_PATH),
    }


@mcp.tool
def disable_pack(name: str) -> dict:
    """Disable a domain pack and persist the change to config/domain_packs.json.

    Args:
        name: Pack name, e.g. "news".

    After disabling, the news-specific API routes (sentiment, events, veracity,
    influence) will not be mounted on the next API startup. Generic document
    routes remain available.
    """
    reg = _load_and_sync()
    if reg.get_pack(name) is None:
        return {
            "error": f"Pack {name!r} is not registered.",
            "registered_packs": sorted(reg._REGISTRY.keys()),
        }
    reg.disable_pack(name)
    enabled = sorted(reg._ENABLED)
    _write_config(enabled)
    return {
        "status": "disabled",
        "pack": name,
        "enabled_packs": enabled,
        "config_path": str(CONFIG_PATH),
    }


@mcp.tool
def run_enrichers(
    document: dict,
    pack: Optional[str] = None,
) -> dict:
    """Run domain-pack enrichers against a document dict and return their outputs.

    Useful for testing an enricher against sample content without wiring it into
    a full pipeline run. Heavy ML deps (torch, transformers, …) are imported
    lazily by each enricher; if a dep is missing the enricher returns None and
    is listed under ``skipped``.

    Args:
        document: A dict with at least ``document_id``, ``source_type``, and
                  ``content`` fields (mirrors the Document dataclass).
                  Example::
                    {
                      "document_id": "test-001",
                      "source_type": "news",
                      "content": "Breaking news: markets surge..."
                    }
        pack: Optional pack name to restrict enrichment to one pack.
              If omitted, enrichers from ALL enabled packs run.

    Returns:
        results   - {enricher_name: output_dict} for enrichers that returned a result
        skipped   - [enricher_name, ...] for enrichers that returned None
        errors    - [enricher_name, ...] if the Enricher.run() exception handler fired
        document_preview - the input document with content truncated for display
        source_type - the source_type used for applies_to filtering
    """
    reg = _load_and_sync()

    if not isinstance(document, dict):
        return {"error": "document must be a JSON object (dict)"}

    source_type = document.get("source_type", "")
    results: dict = {}
    skipped: list = []

    if pack is not None:
        target = reg.get_pack(pack)
        if target is None:
            return {
                "error": f"Pack {pack!r} is not registered.",
                "registered_packs": sorted(reg._REGISTRY.keys()),
            }
        packs_to_run = [target]
    else:
        packs_to_run = reg.get_enabled_packs()

    for p in packs_to_run:
        for enricher in p.enrichers:
            if not enricher.applies_to(source_type):
                continue
            out = enricher.run(document)
            if out is None:
                skipped.append(enricher.name)
            else:
                results[enricher.name] = out

    return {
        "document_preview": _preview_doc(document),
        "source_type": source_type,
        "results": results,
        "skipped": skipped,
        "summary": (
            f"{len(results)} enricher(s) produced output; "
            f"{len(skipped)} skipped (dep missing or no content)."
        ),
    }


@mcp.tool
def get_ui_flags(pack: Optional[str] = None) -> dict:
    """Return the merged UI feature-flag map for enabled domain packs.

    Args:
        pack: Optional pack name. If provided, returns flags for that pack only
              (regardless of enabled status). If omitted, merges flags from ALL
              enabled packs.

    Returns a dict with:
      flags         - merged {flag_name: bool} map
      contributing  - list of pack names whose flags were included
    """
    reg = _load_and_sync()

    if pack is not None:
        target = reg.get_pack(pack)
        if target is None:
            return {
                "error": f"Pack {pack!r} is not registered.",
                "registered_packs": sorted(reg._REGISTRY.keys()),
            }
        packs = [target]
    else:
        packs = reg.get_enabled_packs()

    merged: dict = {}
    contributing = []
    for p in packs:
        merged.update(p.ui_flags)
        contributing.append(p.name)

    return {
        "flags": merged,
        "contributing_packs": contributing,
        "usage": (
            "Paste these flags into your frontend config to gate news-only UI "
            "sections (timeline, clusters, trending, watchlists, sentiment_dashboard, "
            "influence_graph) behind the news domain pack."
        ),
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http
