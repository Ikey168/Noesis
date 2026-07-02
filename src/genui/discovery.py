"""
Discovery-derived panel catalog (MCP rearchitecture plan, R2 / Stage 1).

Maps annotated MCP tools into ``PanelDef``s and merges them over the
static catalog. The annotation format is decided in
``docs/architecture/ADR-001-tool-panel-annotation.md``: a tool declares a
``panel`` block inside its MCP ``_meta`` and MUST declare an
``outputSchema``; everything else in ``tools/list`` is invisible to the
catalog by design.

Merge semantics: the static catalog (``catalog.py``) remains the fallback
and defines the order; a discovered annotation for an existing type
overrides it in place, and new types append after it. With no host, no
connected servers, or no annotated tools, the merged catalog is exactly
the static one — byte-identical output, which is the R2 litmus.

The planner and spec validation still read the static catalog: planning
from discovered defs is R3 (adaptivity from tools). This module only
feeds ``GET /api/v1/ui/panels`` and future consumers.

Import-safe on purpose (stdlib + src.genui + src.mcp_host only).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.genui.catalog import PANEL_CATALOG, PanelDef, panel_catalog_dict

logger = logging.getLogger(__name__)

ANNOTATION_KEY = "panel"

_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MIN_SPAN, _MAX_SPAN = 3, 12


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def panel_def_from_annotation(
    server: str, tool: Dict[str, Any]
) -> Optional[PanelDef]:
    """Validate one cached tool's ``meta.panel`` block into a PanelDef.

    Returns None (with a warning) for anything malformed — a bad
    annotation must never break the catalog, it just stays invisible.
    Tools without an ``outputSchema`` are rejected: the ADR makes the
    schema part of the annotation contract.
    """
    meta = tool.get("meta")
    block = meta.get(ANNOTATION_KEY) if isinstance(meta, dict) else None
    if block is None:
        return None
    name = f"{server}:{tool.get('name')}"
    if not isinstance(block, dict):
        logger.warning("genui discovery: %s has a non-dict panel block", name)
        return None
    if not tool.get("has_output_schema"):
        logger.warning(
            "genui discovery: %s annotated but declares no outputSchema; skipped",
            name,
        )
        return None

    panel_type = block.get("type")
    if not isinstance(panel_type, str) or not _TYPE_RE.match(panel_type):
        logger.warning("genui discovery: %s has invalid panel type %r", name, panel_type)
        return None

    facets = block.get("facets")
    if (
        not isinstance(facets, (list, tuple))
        or not facets
        or not all(isinstance(f, str) and f for f in facets)
    ):
        logger.warning("genui discovery: %s has invalid facets %r", name, facets)
        return None

    span = block.get("default_span", 6)
    if not isinstance(span, int) or isinstance(span, bool) or not (
        _MIN_SPAN <= span <= _MAX_SPAN
    ):
        logger.warning("genui discovery: %s has invalid default_span %r", name, span)
        return None

    tables = block.get("tables", [])
    if not isinstance(tables, (list, tuple)) or not all(
        isinstance(t, str) and t for t in tables
    ):
        logger.warning("genui discovery: %s has invalid tables %r", name, tables)
        return None

    max_days = block.get("max_days")
    if max_days is not None and (
        not isinstance(max_days, int) or isinstance(max_days, bool) or max_days < 1
    ):
        logger.warning("genui discovery: %s has invalid max_days %r", name, max_days)
        return None

    return PanelDef(
        type=panel_type,
        title=_str_or_none(block.get("title")) or panel_type.replace("_", " ").title(),
        description=_str_or_none(block.get("description"))
        or _str_or_none(tool.get("description"))
        or "",
        endpoint=_str_or_none(block.get("endpoint")),
        facets=tuple(facets),
        tables=tuple(tables),
        ui_flag=_str_or_none(block.get("ui_flag")),
        default_span=span,
        topic_param=_str_or_none(block.get("topic_param")),
        source_type_param=_str_or_none(block.get("source_type_param")),
        days_param=_str_or_none(block.get("days_param")),
        max_days=max_days,
    )


def discovered_panel_defs() -> Dict[str, Tuple[PanelDef, str]]:
    """Panel type -> (PanelDef, source server) from the host's tool cache.

    Reads the R1 discovery cache only (never a live round-trip). Empty when
    the host is disabled, not started, or nothing is annotated. On duplicate
    types the first annotation wins, scanning servers in sorted order.
    """
    try:
        from src.mcp_host import get_host

        host = get_host()
    except Exception:  # pragma: no cover - defensive import guard
        return {}
    if host is None:
        return {}

    defs: Dict[str, Tuple[PanelDef, str]] = {}
    for server in sorted(host.tools()):
        for tool in host.tools(server).get(server, []):
            panel_def = panel_def_from_annotation(server, tool)
            if panel_def is None:
                continue
            if panel_def.type in defs:
                logger.warning(
                    "genui discovery: duplicate panel type %r from %s ignored "
                    "(kept the one from %s)",
                    panel_def.type,
                    server,
                    defs[panel_def.type][1],
                )
                continue
            defs[panel_def.type] = (panel_def, server)
    return defs


def merged_catalog() -> List[Tuple[PanelDef, Optional[str]]]:
    """Static catalog with discovered overrides in place and new types
    appended; the second tuple item is the source server (None = static)."""
    discovered = discovered_panel_defs()
    merged: List[Tuple[PanelDef, Optional[str]]] = []
    for panel in PANEL_CATALOG:
        if panel.type in discovered:
            override, server = discovered.pop(panel.type)
            merged.append((override, server))
        else:
            merged.append((panel, None))
    for panel_type in sorted(discovered):
        merged.append(discovered[panel_type])
    return merged


def merged_catalog_dict() -> List[Dict[str, Any]]:
    """JSON-ready merged catalog for ``GET /api/v1/ui/panels``.

    With nothing discovered this returns ``panel_catalog_dict()`` verbatim
    (the servers-down litmus: byte-identical to the static catalog). When
    discovery contributes, every entry gains a ``source`` field: the server
    name for discovered defs, ``"static"`` otherwise.
    """
    merged = merged_catalog()
    if all(server is None for _, server in merged):
        return panel_catalog_dict()
    return [
        {
            "type": p.type,
            "title": p.title,
            "description": p.description,
            "endpoint": p.endpoint,
            "facets": list(p.facets),
            "tables": list(p.tables),
            "ui_flag": p.ui_flag,
            "default_span": p.default_span,
            "topic_param": p.topic_param,
            "source_type_param": p.source_type_param,
            "days_param": p.days_param,
            "max_days": p.max_days,
            "source": server or "static",
        }
        for p, server in merged
    ]
