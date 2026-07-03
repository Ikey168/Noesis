"""M6.4: multi-turn agentic-flow eval. Scores a sequence of refinements against
expected canvas states and gates on the score alongside the single-turn eval."""

from src.genui.planner_eval import (
    MULTITURN_THRESHOLD,
    evaluate,
    gate_passes,
    load_multiturn,
    score_multiturn,
)


def test_multiturn_golden_is_well_formed():
    scenarios = load_multiturn()
    assert scenarios
    for sc in scenarios:
        assert sc["intent"] and sc["turns"]
        for turn in sc["turns"]:
            assert turn["instruction"]
            assert any(k in turn for k in ("expect_present", "expect_absent", "expect_topic"))


def test_multiturn_flow_meets_threshold():
    result = score_multiturn()
    assert result["checks"] > 0
    assert 0.0 <= result["score"] <= 1.0
    assert result["score"] >= MULTITURN_THRESHOLD, result


def test_evaluate_includes_multiturn():
    out = evaluate()
    assert out["multiturn"] is not None
    assert out["multiturn"]["score"] >= MULTITURN_THRESHOLD


def test_gate_fails_on_multiturn_regression():
    regressed = {
        "heuristic": {"score": 1.0},
        "llm": None,
        "multiturn": {"score": MULTITURN_THRESHOLD - 0.1},
    }
    assert gate_passes(regressed) is False


def test_gate_passes_on_baseline():
    assert gate_passes(evaluate()) is True
