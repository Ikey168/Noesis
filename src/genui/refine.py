"""
Refinement intents to spec diffs (M6.3).

A heuristic that turns a natural refinement instruction ("add a contradiction
ledger", "drop the forecast", "focus on energy") into a spec diff (M6.1) against
the current canvas. It is the default refiner for :class:`CanvasSession` when no
LLM is configured, and a signature-compatible ``(spec, instruction, context) ->
diff`` refiner.

Matching is keyword-based over the panel catalog (type tokens plus title), so a
phrase like "contradiction ledger" resolves to ``contradiction_ledger``.
Multi-clause instructions ("add X and drop Y") are split on "and". Stdlib-only.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from src.genui.catalog import PANEL_TYPES, get_panel_def
from src.genui.spec import UISpec

_ADD = re.compile(r"\b(?:add|show|include|bring up|put)\b\s+(?:an?|the|a\s+new)?\s*(.+)")
_REMOVE = re.compile(r"\b(?:remove|drop|hide|delete|get rid of|take out|lose)\b\s+(?:the|that)?\s*(.+)")
_FOCUS = re.compile(r"\b(?:focus on|zoom in on|narrow to|only|filter to|about|for)\b\s+(.+)")

_STOP = {"panel", "panels", "chart", "the", "a", "an", "view", "please"}


@lru_cache(maxsize=1)
def _lookup() -> Dict[str, str]:
    """phrase -> panel type, over the catalog (type tokens + title)."""
    out: Dict[str, str] = {}
    for ptype in PANEL_TYPES:
        out[ptype] = ptype
        out[ptype.replace("_", " ")] = ptype
        pdef = get_panel_def(ptype)
        if pdef and pdef.title:
            out[pdef.title.lower()] = ptype
    return out


def match_panel(text: str) -> Optional[str]:
    """Resolve a phrase to a panel type by exact match, then best token overlap."""
    text = text.lower().strip().strip(".!?")
    lookup = _lookup()
    if text in lookup:
        return lookup[text]
    tokens = {t for t in re.findall(r"[a-z]+", text) if t not in _STOP}
    if not tokens:
        return None
    best, best_score = None, 0
    for phrase, ptype in lookup.items():
        ptoks = set(phrase.split())
        score = len(tokens & ptoks)
        if phrase in text:
            score += 2
        if score > best_score:
            best, best_score = ptype, score
    return best if best_score > 0 else None


def _data_panel_types(spec: UISpec) -> List[str]:
    seen: List[str] = []
    for p in spec.panels:
        if p.type != "note" and p.type not in seen:
            seen.append(p.type)
    return seen


def refine_to_diff(
    spec: UISpec, instruction: str, context: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """Turn a refinement instruction into a spec diff against ``spec``. Returns
    an empty list when nothing was recognized (a no-op refinement)."""
    diff: List[Dict[str, Any]] = []
    for clause in re.split(r"\s+and\s+|,\s*", (instruction or "").lower()):
        clause = clause.strip()
        if not clause:
            continue
        m = _REMOVE.search(clause)
        if m:
            panel = match_panel(m.group(1))
            if panel:
                diff.append({"op": "remove", "type": panel})
                continue
        m = _ADD.search(clause)
        if m:
            panel = match_panel(m.group(1))
            if panel:
                diff.append({"op": "add", "panel": {"type": panel}})
                continue
        m = _FOCUS.search(clause)
        if m:
            topic = m.group(1).strip().strip(".!?")
            # If the focus target is itself a panel name, treat it as an add.
            panel = match_panel(topic)
            if panel and topic in _lookup():
                diff.append({"op": "add", "panel": {"type": panel}})
            elif topic:
                for ptype in _data_panel_types(spec):
                    diff.append({"op": "retarget", "type": ptype, "params": {"topic": topic}})
    return diff
