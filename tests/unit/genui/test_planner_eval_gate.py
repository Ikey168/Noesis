"""M5.3: the CI regression gate. The planner-eval gate passes on the current
baseline and fails when the heuristic score regresses below the threshold."""

from src.genui.planner_eval import SCORE_THRESHOLD, evaluate, gate_passes, main


def test_gate_passes_on_the_current_baseline():
    assert gate_passes(evaluate()) is True


def test_gate_fails_on_a_regression_below_threshold():
    regressed = {"heuristic": {"score": SCORE_THRESHOLD - 0.01}, "llm": None}
    assert gate_passes(regressed) is False


def test_gate_fails_when_heuristic_is_unavailable():
    assert gate_passes({"heuristic": None, "llm": None}) is False


def test_main_exits_zero_on_the_baseline():
    assert main() == 0
