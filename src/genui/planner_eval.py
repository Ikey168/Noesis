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
_MULTITURN = Path(__file__).resolve().parents[2] / "tests" / "data" / "planner_multiturn_golden.json"

# The multi-turn agentic flow (M6.4) must satisfy at least this fraction of the
# per-turn checks. Gated by CI alongside the single-turn score.
MULTITURN_THRESHOLD = 0.9

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


def load_multiturn(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    return json.loads((path or _MULTITURN).read_text())["scenarios"]


def score_multiturn(scenarios: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Run each scenario through a CanvasSession driven by the M6.3 heuristic
    refiner and score the fraction of per-turn checks (expect_present /
    expect_absent / expect_topic) satisfied. This scores the agentic flow, not a
    one-shot plan."""
    from src.genui.canvas_session import start_session
    from src.genui.refine import refine_to_diff

    scenarios = scenarios if scenarios is not None else load_multiturn()
    total, ok = 0, 0
    per_scenario: List[Dict[str, Any]] = []
    for scenario in scenarios:
        session = start_session(scenario["intent"])
        turns: List[Dict[str, Any]] = []
        for turn in scenario["turns"]:
            session.refine_with(turn["instruction"], refine_to_diff)
            types = {p.type for p in session.spec.panels if p.type != "note"}
            topics = {(p.params or {}).get("topic") for p in session.spec.panels if p.type != "note"}
            turn_ok = True
            for panel in turn.get("expect_present", []):
                total += 1
                if panel in types:
                    ok += 1
                else:
                    turn_ok = False
            for panel in turn.get("expect_absent", []):
                total += 1
                if panel not in types:
                    ok += 1
                else:
                    turn_ok = False
            if "expect_topic" in turn:
                total += 1
                if turn["expect_topic"] in topics:
                    ok += 1
                else:
                    turn_ok = False
            turns.append({"instruction": turn["instruction"], "ok": turn_ok})
        per_scenario.append({"intent": scenario["intent"], "turns": turns})
    return {
        "scenarios": len(scenarios),
        "checks": total,
        "score": (ok / total) if total else 1.0,
        "per_scenario": per_scenario,
    }


def evaluate(path: Optional[Path] = None) -> Dict[str, Any]:
    """Score the heuristic and LLM planners on the golden set. The LLM entry is
    None when no LLM is configured."""
    cases = load_golden(path)
    return {
        "heuristic": score_planner(cases, heuristic_planner),
        "llm": score_planner(cases, llm_planner),
        "multiturn": score_multiturn(),
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
    """The CI gate (M5.3, M6.4): the heuristic single-turn score and the
    multi-turn agentic-flow score must not regress below their thresholds. The
    LLM planner is reported but never gates (optional, may be unavailable)."""
    heur = out.get("heuristic")
    mt = out.get("multiturn")
    if heur is None or heur["score"] < SCORE_THRESHOLD:
        return False
    if mt is not None and mt["score"] < MULTITURN_THRESHOLD:
        return False
    return True


def main() -> int:
    out = evaluate()
    print("Planner eval (golden set)\n")
    for name in ("heuristic", "llm"):
        print(f"  {name:<10} {_fmt(out[name])}")
    mt = out.get("multiturn")
    if mt is not None:
        print(f"  {'multiturn':<10} score={mt['score']:.3f}  "
              f"({mt['scenarios']} scenarios, {mt['checks']} checks)")
    passed = gate_passes(out)
    print(f"\nthresholds single={SCORE_THRESHOLD} multiturn={MULTITURN_THRESHOLD}: "
          f"{'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    import sys

    sys.exit(main())
