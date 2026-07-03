"""
Planner evaluation scorer (M5.2).

Runs the golden intents (``tests/data/planner_golden.json``, M5.1) through a
planner and scores its panel/facet selection: facet precision and recall against
the labeled facets, and panel recall against the labeled ``must_panels``. Works
for both the heuristic planner and the LLM planner; the LLM planner scores as
unavailable (``None``) when no LLM is configured, so the harness runs everywhere.

Run:  python -m src.genui.planner_eval
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# The regression threshold CI (M5.3) gates on: the combined score must not drop
# below this for the heuristic planner.
SCORE_THRESHOLD = 0.9

_GOLDEN = Path(__file__).resolve().parents[2] / "tests" / "data" / "planner_golden.json"

# A planner returns (facets, panel_types) for an intent, or None if unavailable.
PlannerFn = Callable[[str], Optional[Tuple[List[str], List[str]]]]


def load_golden(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    return json.loads((path or _GOLDEN).read_text())["cases"]


def _metrics_for_case(facets: List[str], panels: List[str], case: Dict[str, Any]) -> Dict[str, float]:
    exp, top = set(case["facets"]), set(facets)
    must, act = set(case["must_panels"]), set(panels)
    inter_f = exp & top
    return {
        "facet_precision": (len(inter_f) / len(top)) if top else 0.0,
        "facet_recall": (len(inter_f) / len(exp)) if exp else 1.0,
        "panel_recall": (len(must & act) / len(must)) if must else 1.0,
    }


def score_planner(cases: List[Dict[str, Any]], planner_fn: PlannerFn) -> Optional[Dict[str, Any]]:
    """Aggregate metrics over the golden cases, or None if the planner is
    unavailable (the first call returns None)."""
    per: List[Dict[str, Any]] = []
    for case in cases:
        result = planner_fn(case["intent"])
        if result is None:
            return None
        facets, panels = result
        per.append({"intent": case["intent"], **_metrics_for_case(facets, panels, case)})
    n = len(per)
    agg = {
        key: sum(p[key] for p in per) / n
        for key in ("facet_precision", "facet_recall", "panel_recall")
    }
    agg["score"] = 0.5 * agg["facet_recall"] + 0.5 * agg["panel_recall"]
    return {"n": n, **agg, "per_case": per}


def heuristic_planner(intent: str) -> Tuple[List[str], List[str]]:
    from src.genui.planner import plan

    spec = plan(intent)
    return list(spec.facets), [p.type for p in spec.panels if p.type != "note"]


def llm_planner(intent: str) -> Optional[Tuple[List[str], List[str]]]:
    from src.genui.llm import plan_with_llm

    spec = plan_with_llm(intent)
    if spec is None:
        return None
    return list(spec.facets), [p.type for p in spec.panels if p.type != "note"]


def evaluate(path: Optional[Path] = None) -> Dict[str, Any]:
    """Score the heuristic and LLM planners on the golden set. The LLM entry is
    None when no LLM is configured."""
    cases = load_golden(path)
    return {
        "heuristic": score_planner(cases, heuristic_planner),
        "llm": score_planner(cases, llm_planner),
    }


def _fmt(result: Optional[Dict[str, Any]]) -> str:
    if result is None:
        return "unavailable"
    return (
        f"score={result['score']:.3f}  facet_p={result['facet_precision']:.3f}  "
        f"facet_r={result['facet_recall']:.3f}  panel_r={result['panel_recall']:.3f}  "
        f"(n={result['n']})"
    )


def gate_passes(out: Dict[str, Any]) -> bool:
    """The CI gate (M5.3): the heuristic planner's score must not regress below
    the threshold. The LLM planner is reported but never gates (it is optional
    and may be unavailable in CI)."""
    heur = out.get("heuristic")
    return heur is not None and heur["score"] >= SCORE_THRESHOLD


def main() -> int:
    out = evaluate()
    print("Planner eval (golden set)\n")
    for name in ("heuristic", "llm"):
        print(f"  {name:<10} {_fmt(out[name])}")
    passed = gate_passes(out)
    print(f"\nthreshold {SCORE_THRESHOLD}: heuristic {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
