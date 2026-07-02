"""
Adaptivity layer: the three inputs that reshape a generated layout.

* :func:`resolve_availability` maps each catalog table to whether it holds
  rows. Since R3 the primary source is the MCP servers' own stats tools
  (``am_stats``, ``article_stats``, ``document_stats``, ...) through the
  host runtime; the direct DuckDB probe (:func:`data_availability`) is the
  fallback, so with servers down behavior is identical to before. Both
  sources degrade to ``None`` ("unknown") rather than failing — a missing
  warehouse must never break UI generation.
* :func:`resolve_ui_flags` merges the ``ui_flags`` of all enabled domain
  packs, preferring the domain-packs MCP server's ``get_ui_flags`` tool
  and falling back to the in-process registry
  (:func:`merged_ui_flags`).
* :func:`apply_signals` folds client usage signals — pinned, dismissed and
  interaction weights persisted by the frontend — into panel priorities.

Overview panels anchor on the virtual ``documents`` corpus (Track N2):
its availability is the union of the ``documents`` table and
``news_articles``, so a corpus with zero news still gets a live overview.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.genui.catalog import PANEL_CATALOG, get_panel_def
from src.genui.spec import PanelSpec

# The "documents" anchor is a corpus, not one table: available when either
# the documents table or the legacy news_articles table has rows.
DOCUMENT_CORPUS_TABLES: Tuple[str, ...] = ("documents", "news_articles")

# Tables the catalog references; probed in one round-trip. The corpus
# tables are always included so the union can be computed.
_PROBE_TABLES: Tuple[str, ...] = tuple(
    sorted({t for p in PANEL_CATALOG for t in p.tables} | set(DOCUMENT_CORPUS_TABLES))
)

MAX_SIGNAL_WEIGHT = 20
PIN_BOOST = 0.3
WEIGHT_STEP = 0.01


def normalize_signals(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Coerce a client-supplied signals payload into a safe shape."""
    known = {p.type for p in PANEL_CATALOG}
    if not isinstance(raw, dict):
        raw = {}
    pinned = [t for t in raw.get("pinned") or [] if isinstance(t, str) and t in known]
    dismissed = [
        t for t in raw.get("dismissed") or [] if isinstance(t, str) and t in known
    ]
    weights: Dict[str, int] = {}
    raw_weights = raw.get("weights")
    if isinstance(raw_weights, dict):
        for key, value in raw_weights.items():
            if key in known and isinstance(value, (int, float)):
                weights[key] = int(max(0, min(MAX_SIGNAL_WEIGHT, value)))
    return {"pinned": pinned, "dismissed": dismissed, "weights": weights}


def apply_signals(
    panels: List[PanelSpec], signals: Dict[str, Any]
) -> Tuple[List[PanelSpec], List[str]]:
    """Re-rank panels by usage signals.

    Dismissed types are removed (a pin wins over a dismissal), pinned types
    are boosted and appended if absent, and interaction weights nudge
    priorities. Returns ``(panels, dismissed_types_removed)``.
    """
    pinned = set(signals.get("pinned", []))
    dismissed = set(signals.get("dismissed", [])) - pinned
    weights = signals.get("weights", {})

    kept: List[PanelSpec] = []
    removed: List[str] = []
    for panel in panels:
        if panel.type in dismissed:
            removed.append(panel.type)
            continue
        kept.append(panel)

    present = {p.type for p in kept}
    for ptype in sorted(pinned):
        if ptype in present:
            continue
        pdef = get_panel_def(ptype)
        if pdef is None or pdef.type == "note":
            continue
        kept.append(
            PanelSpec(
                id=ptype,
                type=ptype,
                title=pdef.title,
                span=pdef.default_span,
                priority=0.6,
                rationale="pinned by you",
            )
        )

    for panel in kept:
        if panel.type in pinned:
            panel.priority = min(1.0, panel.priority + PIN_BOOST)
            if "pinned" not in panel.rationale:
                panel.rationale = (panel.rationale + "; pinned by you").strip("; ")
        boost = weights.get(panel.type, 0)
        if boost:
            panel.priority = min(1.0, panel.priority + boost * WEIGHT_STEP)

    kept.sort(key=lambda p: -p.priority)
    return kept, removed


def panel_available(
    panel_type: str, availability: Optional[Dict[str, bool]]
) -> bool:
    """Whether a panel's warehouse tables hold data (unknown counts as yes)."""
    if availability is None:
        return True
    pdef = get_panel_def(panel_type)
    if pdef is None or not pdef.tables:
        return True
    return all(availability.get(table, False) for table in pdef.tables)


def panel_flag_enabled(
    panel_type: str, ui_flags: Optional[Dict[str, bool]]
) -> bool:
    """Whether a panel's domain-pack ui_flag allows it (no flag = allowed)."""
    pdef = get_panel_def(panel_type)
    if pdef is None or pdef.ui_flag is None or not ui_flags:
        return True
    return bool(ui_flags.get(pdef.ui_flag, True))


def filter_panels(
    panels: List[PanelSpec],
    availability: Optional[Dict[str, bool]],
    ui_flags: Optional[Dict[str, bool]],
) -> Tuple[List[PanelSpec], List[str]]:
    """Drop panels whose data is absent or whose pack flag is off."""
    kept: List[PanelSpec] = []
    dropped: List[str] = []
    for panel in panels:
        if not panel_flag_enabled(panel.type, ui_flags):
            dropped.append(panel.type)
            continue
        if not panel_available(panel.type, availability):
            dropped.append(panel.type)
            continue
        kept.append(panel)
    return kept, dropped


def _default_probe() -> Dict[str, int]:
    """Row counts for catalog tables from the shared DuckDB warehouse."""
    from src.database.local_analytics_connector import _LOCK, get_shared_connection

    conn = get_shared_connection()
    counts: Dict[str, int] = {}
    # The shared connection is not safe for concurrent use — serialize with
    # the connector's lock like every other caller (table names come from the
    # frozen catalog, never from user input).
    with _LOCK:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()
        existing = {r[0] for r in rows}
        for table in _PROBE_TABLES:
            if table not in existing:
                counts[table] = 0
                continue
            result = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = int(result[0]) if result else 0
    return counts


def _availability_from_counts(counts: Dict[str, int]) -> Dict[str, bool]:
    """Counts -> availability map, with the documents-corpus union applied."""
    availability = {table: counts.get(table, 0) > 0 for table in _PROBE_TABLES}
    availability["documents"] = any(
        availability.get(t, False) for t in DOCUMENT_CORPUS_TABLES
    )
    return availability


def data_availability(
    probe: Optional[Callable[[], Dict[str, int]]] = None,
) -> Optional[Dict[str, bool]]:
    """Map each catalog table to whether it currently holds rows, from the
    direct DuckDB warehouse probe (the servers-down fallback path).

    Returns ``None`` when the warehouse cannot be reached — callers treat
    unknown availability as "keep every panel".
    """
    try:
        return _availability_from_counts((probe or _default_probe)())
    except Exception:
        return None


def merged_ui_flags() -> Dict[str, bool]:
    """Merge ui_flags across enabled domain packs (later packs win)."""
    try:
        from src.domains.registry import get_enabled_packs

        flags: Dict[str, bool] = {}
        for pack in get_enabled_packs():
            flags.update(pack.ui_flags)
        return flags
    except Exception:
        return {}


# --------------------------------------------------------------------------
# R3: tool-sourced adaptivity. The MCP servers' stats tools are the primary
# source for availability and ui_flags; everything above is the fallback.
# Results are cached briefly so a burst of generates does not hammer the
# servers (R4 generalizes this cache and shares it with the planning loop).
# --------------------------------------------------------------------------

TOOL_STATS_TTL = 30.0

# Servers whose stats tools cover every catalog table.
_STATS_SERVERS = ("neuronews-arguments", "neuronews-pipeline")
_FLAGS_SERVER = "neuronews-domain-packs"

# am_stats reports row counts for these catalog tables directly.
_AM_STATS_TABLES = (
    "argument_claims",
    "claim_conflicts",
    "source_stances",
    "stance_drift_events",
    "policy_positions",
    "document_frames",
)
# Tables probed via a limit-1 list call ({"count": 0|1} tells presence).
_LIMIT_PROBE_TOOLS = (
    ("outlet_scores", "list_outlet_scores"),
    ("outlet_clusters", "list_outlet_clusters"),
    ("document_actors", "actor_summary"),
)

_cache: Dict[str, Any] = {"availability": None, "flags": None}


def reset_tool_cache() -> None:
    """Drop cached tool-sourced results (tests and pack toggles)."""
    _cache["availability"] = None
    _cache["flags"] = None


def _get_host():
    from src.mcp_host import get_host

    return get_host()


def _servers_connected(host, names) -> bool:
    servers = host.status().get("servers", {})
    return all(servers.get(n, {}).get("state") == "connected" for n in names)


def _cached_call(host, server: str, tool: str, arguments=None) -> Dict[str, Any]:
    """Read a stats tool through the host's shared cache (R4 #593) when
    available, so the adaptivity layer and the LLM planning loop don't each
    re-inspect. Falls back to a plain call on older hosts."""
    cached = getattr(host, "call_tool_cached", None)
    if callable(cached):
        return cached(server, tool, arguments)
    return host.call_tool(server, tool, arguments)


def _tool_counts(host) -> Dict[str, int]:
    """Row counts for every catalog table via the servers' stats tools."""
    counts: Dict[str, int] = {}

    am = _cached_call(host, "neuronews-arguments", "am_stats")
    if "error" in am:
        raise RuntimeError(am["error"])
    for table in _AM_STATS_TABLES:
        value = am.get(table)
        counts[table] = value if isinstance(value, int) else 0

    articles = _cached_call(host, "neuronews-pipeline", "article_stats")
    if "error" in articles:
        raise RuntimeError(articles["error"])
    counts["news_articles"] = int(articles.get("total_articles", 0))

    documents = _cached_call(host, "neuronews-pipeline", "document_stats")
    if "error" in documents:
        raise RuntimeError(documents["error"])
    counts["documents"] = int(documents.get("total_documents", 0))

    for table, tool in _LIMIT_PROBE_TOOLS:
        result = _cached_call(host, "neuronews-arguments", tool, {"limit": 1})
        if "error" in result:
            raise RuntimeError(result["error"])
        counts[table] = int(result.get("count", 0))
    return counts


def resolve_availability() -> Tuple[Optional[Dict[str, bool]], str]:
    """``(availability, source)`` with source one of ``tools`` /
    ``warehouse`` / ``unknown``.

    Tool-sourced when the host runtime is up and the stats servers are
    connected; the DuckDB probe otherwise, keeping servers-down behavior
    identical to pre-R3.
    """
    cached = _cache["availability"]
    if cached is not None and time.time() < cached[1]:
        return cached[0], "tools"
    try:
        host = _get_host()
        if host is not None and _servers_connected(host, _STATS_SERVERS):
            availability = _availability_from_counts(_tool_counts(host))
            _cache["availability"] = (availability, time.time() + TOOL_STATS_TTL)
            return availability, "tools"
    except Exception:
        pass
    availability = data_availability()
    return availability, ("warehouse" if availability is not None else "unknown")


def resolve_ui_flags() -> Tuple[Dict[str, bool], str]:
    """``(ui_flags, source)`` with source ``tools`` or ``packs``.

    Prefers the domain-packs server's ``get_ui_flags`` tool (server
    presence + tool result); falls back to the in-process registry merge.
    """
    cached = _cache["flags"]
    if cached is not None and time.time() < cached[1]:
        return cached[0], "tools"
    try:
        host = _get_host()
        if host is not None and _servers_connected(host, (_FLAGS_SERVER,)):
            result = _cached_call(host, _FLAGS_SERVER, "get_ui_flags")
            flags = result.get("flags")
            if isinstance(flags, dict):
                flags = {str(k): bool(v) for k, v in flags.items()}
                _cache["flags"] = (flags, time.time() + TOOL_STATS_TTL)
                return flags, "tools"
    except Exception:
        pass
    return merged_ui_flags(), "packs"
