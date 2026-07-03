"""
Multi-turn canvas session (M6.2).

A :class:`CanvasSession` carries a canvas across turns: it holds the current
ui-spec and applies a refinement each turn as a spec diff (M6.1), so the canvas
evolves instead of being regenerated. The refinement can come from an explicit
diff, the M6.3 heuristic, or a grounded LLM refiner.

The LLM refiner is *grounded on live data*: it is given a summary of the current
canvas (its panels, topics, and any live data bound to them) so its diff is
relative to what is on screen, not a blind re-plan. It degrades gracefully: with
no LLM configured, ``refine_with_llm`` reports it is unavailable and leaves the
canvas unchanged.

Stdlib-only; the LLM and data-plane hooks are imported lazily.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from src.genui.spec import UISpec
from src.genui.spec_diff import apply_diff


def live_data_context(spec: UISpec) -> Dict[str, Any]:
    """A compact grounding summary of the current canvas for the refiner: each
    panel's type and topic, plus a live-data snapshot for the panels a data-mode
    tool can serve (when the data proxy is on). Best-effort: any failure yields
    the structural summary alone, never raises."""
    panels = [
        {"type": p.type, "topic": (p.params or {}).get("topic")}
        for p in spec.panels
        if p.type != "note"
    ]
    context: Dict[str, Any] = {
        "topic": spec.topic,
        "facets": list(spec.facets),
        "panels": panels,
    }
    try:
        from src.genui.dataplane import data_proxy_enabled, warm_data_plane

        if data_proxy_enabled():
            context["live"] = warm_data_plane()
    except Exception:
        pass
    return context


class CanvasSession:
    """Holds the evolving canvas for one conversation and applies refinements."""

    def __init__(self, spec: UISpec):
        self.spec = spec
        self.turns: List[Dict[str, Any]] = []

    def refine(self, diff: List[Dict[str, Any]], label: str = "") -> Tuple[UISpec, List[str]]:
        """Apply a spec diff. On success the session advances to the new canvas;
        on any error the current canvas is kept unchanged and the errors are
        returned, so a bad refinement never corrupts the session."""
        new_spec, errors = apply_diff(self.spec, diff)
        if errors:
            self.turns.append({"label": label, "diff": diff, "ok": False, "errors": errors})
            return self.spec, errors
        self.spec = new_spec
        self.turns.append({"label": label, "diff": diff, "ok": True, "errors": []})
        return self.spec, []

    def refine_with(
        self, instruction: str, refiner: Callable[[UISpec, str, Dict[str, Any]], Optional[List[Dict[str, Any]]]]
    ) -> Tuple[UISpec, List[str]]:
        """Refine using a pluggable refiner ``(spec, instruction, context) ->
        diff``. The refiner is handed the live-data grounding context. A refiner
        that returns None (e.g. no diff derivable) leaves the canvas unchanged."""
        context = live_data_context(self.spec)
        diff = refiner(self.spec, instruction, context)
        if not diff:
            return self.spec, ["refiner produced no diff"]
        return self.refine(diff, label=instruction)

    def refine_with_llm(self, instruction: str) -> Tuple[UISpec, List[str]]:
        """Refine via the grounded LLM refiner. Degrades gracefully: with no LLM
        configured, the canvas is unchanged and the reason is returned."""
        return self.refine_with(instruction, llm_refiner)


def llm_refiner(
    spec: UISpec, instruction: str, context: Dict[str, Any]
) -> Optional[List[Dict[str, Any]]]:
    """Produce a spec diff for ``instruction`` grounded on ``context`` using the
    configured LLM. Returns None when no LLM is configured (the caller then
    leaves the canvas unchanged)."""
    try:
        from src.genui.llm import llm_config
    except Exception:
        return None
    if llm_config() is None:
        return None
    # The one-shot completion path: prompt the model with the canvas grounding
    # context and the instruction, parse a diff. Kept minimal here; the grounded
    # tool-use loop (R4) can enrich the context further.
    return _llm_diff(spec, instruction, context)


def _llm_diff(
    spec: UISpec, instruction: str, context: Dict[str, Any]
) -> Optional[List[Dict[str, Any]]]:  # pragma: no cover - requires a live LLM
    import json

    try:
        from src.genui.llm import _complete, llm_config

        config = llm_config()
        if config is None:
            return None
        prompt = (
            "You are refining an on-screen analytics canvas. Given the current canvas "
            "and a user instruction, return ONLY a JSON list of spec-diff operations "
            '(each {"op":"add|remove|retarget", ...}).\n\n'
            f"Current canvas: {json.dumps(context)}\n"
            f"Instruction: {instruction}\n"
        )
        text = _complete(config, prompt)
        data = json.loads(text) if text else None
        return data if isinstance(data, list) else None
    except Exception:
        return None


def start_session(intent: str, **plan_kwargs: Any) -> CanvasSession:
    """Plan the initial canvas for an intent and open a session on it."""
    from src.genui.planner import plan

    return CanvasSession(plan(intent, **plan_kwargs))
