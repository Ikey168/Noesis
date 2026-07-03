"""M5.2: the planner eval scorer. Scores facet precision/recall and panel recall
over the golden set for the heuristic (and LLM, when configured) planner."""

from src.genui.planner_eval import (
    SCORE_THRESHOLD,
    _metrics_for_case,
    evaluate,
    heuristic_planner,
    load_golden,
    score_planner,
)


def test_metrics_for_a_perfect_case():
    case = {"facets": ["claims"], "must_panels": ["corroboration", "claims"]}
    m = _metrics_for_case(["claims"], ["corroboration", "claims", "frames"], case)
    assert m["facet_precision"] == 1.0
    assert m["facet_recall"] == 1.0
    assert m["panel_recall"] == 1.0


def test_metrics_penalize_missing_panels_and_extra_facets():
    case = {"facets": ["claims"], "must_panels": ["corroboration", "claims"]}
    # Extra facet lowers precision; a missing must-panel lowers panel recall.
    m = _metrics_for_case(["claims", "sources"], ["claims"], case)
    assert m["facet_precision"] == 0.5
    assert m["facet_recall"] == 1.0
    assert m["panel_recall"] == 0.5


def test_heuristic_planner_meets_the_threshold():
    cases = load_golden()
    result = score_planner(cases, heuristic_planner)
    assert result is not None
    assert result["n"] == len(cases)
    for key in ("facet_precision", "facet_recall", "panel_recall", "score"):
        assert 0.0 <= result[key] <= 1.0
    assert result["score"] >= SCORE_THRESHOLD, result["score"]


def test_unavailable_planner_scores_none():
    assert score_planner(load_golden(), lambda intent: None) is None


def test_evaluate_returns_both_planner_slots():
    out = evaluate()
    assert out["heuristic"] is not None
    assert "llm" in out  # None here (no LLM configured), present as a slot
