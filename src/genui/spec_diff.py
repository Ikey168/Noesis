"""
Spec-diff apply path (M6.1): refine a canvas by applying a small diff instead of
regenerating the whole ui-spec.

A diff is a list of operations, each a dict:

  {"op": "add", "panel": {"type": "contradiction_ledger", "title"?, "span"?, "params"?}}
  {"op": "remove", "type": "forecast"}          # or {"id": "p3"}
  {"op": "retarget", "type": "articles", "params": {"topic": "energy"}, "span"?: 8}

:func:`apply_diff` applies the ops in order onto an existing :class:`UISpec` and
returns ``(new_spec, errors)``. Invalid ops are skipped and reported; the note
panel is preserved and panels are re-numbered. The returned spec is validated
against the ui-spec contract, so a caller can reject a diff that would produce an
invalid canvas. Stdlib-only; untrusted (LLM-produced) input never raises.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple

from src.genui.catalog import PANEL_TYPES, get_panel_def
from src.genui.spec import MAX_PANELS, PanelSpec, UISpec, validate_spec

_MAX_DATA_PANELS = MAX_PANELS - 1  # one slot is the plan note


def _data_panels(panels: List[PanelSpec]) -> List[PanelSpec]:
    return [p for p in panels if p.type != "note"]


def apply_diff(spec: UISpec, diff: List[Dict[str, Any]]) -> Tuple[UISpec, List[str]]:
    """Apply a spec diff to ``spec``. Returns ``(new_spec, errors)``; ``errors``
    is empty when every op applied and the result validates."""
    errors: List[str] = []
    panels: List[PanelSpec] = [deepcopy(p) for p in spec.panels]

    if not isinstance(diff, list):
        return spec, ["diff must be a list of operations"]

    for i, op in enumerate(diff):
        where = f"diff[{i}]"
        if not isinstance(op, dict):
            errors.append(f"{where}: not an object")
            continue
        kind = op.get("op")
        if kind == "add":
            errors += _apply_add(op, panels, where)
        elif kind == "remove":
            errors += _apply_remove(op, panels, where)
        elif kind == "retarget":
            errors += _apply_retarget(op, panels, where)
        else:
            errors.append(f"{where}: unknown op {kind!r}")

    # Keep the note first, cap data panels, re-number ids.
    note = [p for p in panels if p.type == "note"][:1]
    rest = _data_panels(panels)[:_MAX_DATA_PANELS]
    ordered = note + rest
    for j, panel in enumerate(ordered):
        panel.id = f"p{j + 1}"

    new_spec = UISpec(
        intent=spec.intent,
        title=spec.title,
        subtitle=spec.subtitle,
        generated_by=spec.generated_by,
        facets=list(spec.facets),
        topic=spec.topic,
        source_type=spec.source_type,
        panels=ordered,
    )
    return new_spec, errors + validate_spec(new_spec.to_dict())


def _apply_add(op: Dict[str, Any], panels: List[PanelSpec], where: str) -> List[str]:
    panel = op.get("panel")
    if not isinstance(panel, dict) or not panel.get("type"):
        return [f"{where}: add requires a panel with a type"]
    ptype = str(panel["type"])
    if ptype not in PANEL_TYPES:
        return [f"{where}: unknown panel type {ptype!r}"]
    if len(_data_panels(panels)) >= _MAX_DATA_PANELS:
        return [f"{where}: canvas is full ({_MAX_DATA_PANELS} panels)"]
    pdef = get_panel_def(ptype)
    panels.append(
        PanelSpec(
            id="tmp",
            type=ptype,
            title=str(panel.get("title") or (pdef.title if pdef else ptype)),
            span=int(panel.get("span") or (pdef.default_span if pdef else 6)),
            priority=float(panel.get("priority") or 0.6),
            rationale=str(panel.get("rationale") or "added by refinement"),
            params=panel.get("params") if isinstance(panel.get("params"), dict) else {},
        )
    )
    return []


def _apply_remove(op: Dict[str, Any], panels: List[PanelSpec], where: str) -> List[str]:
    target_type, target_id = op.get("type"), op.get("id")
    if not target_type and not target_id:
        return [f"{where}: remove requires a type or id"]
    before = len(panels)
    panels[:] = [
        p
        for p in panels
        if p.type == "note"
        or not ((target_type and p.type == target_type) or (target_id and p.id == target_id))
    ]
    return [] if len(panels) < before else [f"{where}: no panel matched remove"]


def _apply_retarget(op: Dict[str, Any], panels: List[PanelSpec], where: str) -> List[str]:
    target_type = op.get("type")
    if not target_type:
        return [f"{where}: retarget requires a type"]
    new_params = op.get("params") if isinstance(op.get("params"), dict) else {}
    matched = False
    for p in panels:
        if p.type == target_type and p.type != "note":
            p.params = {**p.params, **new_params}
            if "span" in op:
                p.span = int(op["span"])
            matched = True
    return [] if matched else [f"{where}: no panel matched retarget {target_type!r}"]
