"""
Adaptivity layer: the three inputs that reshape a generated layout.

* :func:`data_availability` probes the DuckDB warehouse for which tables
  actually hold rows, so the planner can hide panels that would render
  empty. The probe degrades to ``None`` ("unknown") on any failure — a
  missing warehouse must never break UI generation.
* :func:`merged_ui_flags` merges the ``ui_flags`` of all enabled domain
  packs (same merge the domain-packs MCP server exposes) so pack-gated
  panels disappear when their pack is off.
* :func:`apply_signals` folds client usage signals — pinned, dismissed and
  interaction weights persisted by the frontend — into panel priorities.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from src.genui.catalog import PANEL_CATALOG, get_panel_def
from src.genui.spec import PanelSpec

# Tables the catalog references; probed in one round-trip.
_PROBE_TABLES: Tuple[str, ...] = tuple(
    sorted({t for p in PANEL_CATALOG for t in p.tables})
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


def data_availability(
    probe: Optional[Callable[[], Dict[str, int]]] = None,
) -> Optional[Dict[str, bool]]:
    """Map each catalog table to whether it currently holds rows.

    Returns ``None`` when the warehouse cannot be reached — callers treat
    unknown availability as "keep every panel".
    """
    try:
        counts = (probe or _default_probe)()
        return {table: counts.get(table, 0) > 0 for table in _PROBE_TABLES}
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
