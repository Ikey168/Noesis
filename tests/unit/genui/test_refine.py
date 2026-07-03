"""M6.3: refinement intents to spec diffs. A natural instruction turns into a
spec diff against the current canvas, and applying it through a session produces
the expected canvas."""

from src.genui.canvas_session import start_session
from src.genui.planner import plan
from src.genui.refine import match_panel, refine_to_diff


def _types(spec):
    return [p.type for p in spec.panels if p.type != "note"]


def _canvas():
    return plan("who are the key actors")


def test_match_panel_resolves_phrases():
    assert match_panel("contradiction ledger") == "contradiction_ledger"
    assert match_panel("the forecast panel") == "forecast"
    assert match_panel("reliability card") == "reliability_card"
    assert match_panel("nonsense words here") is None


def test_add_instruction_becomes_add_op():
    diff = refine_to_diff(_canvas(), "add a contradiction ledger")
    assert diff == [{"op": "add", "panel": {"type": "contradiction_ledger"}}]


def test_drop_instruction_becomes_remove_op():
    diff = refine_to_diff(_canvas(), "drop the forecast")
    assert diff == [{"op": "remove", "type": "forecast"}]


def test_focus_instruction_retargets_all_data_panels():
    spec = _canvas()
    diff = refine_to_diff(spec, "focus on energy")
    assert diff and all(op["op"] == "retarget" for op in diff)
    assert all(op["params"] == {"topic": "energy"} for op in diff)
    assert {op["type"] for op in diff} == set(_types(spec))


def test_multi_clause_instruction():
    diff = refine_to_diff(_canvas(), "add corroboration and remove actors")
    assert {"op": "add", "panel": {"type": "corroboration"}} in diff
    assert {"op": "remove", "type": "actors"} in diff


def test_unrecognized_instruction_is_a_noop_diff():
    assert refine_to_diff(_canvas(), "make it pretty please") == []


def test_diff_applies_cleanly_through_a_session():
    session = start_session("who are the key actors")
    spec, errors = session.refine_with("add a contradiction ledger", refine_to_diff)
    assert errors == [] and "contradiction_ledger" in _types(spec)

    spec, errors = session.refine_with("focus on nuclear", refine_to_diff)
    assert errors == []
    topics = {(p.params or {}).get("topic") for p in spec.panels if p.type != "note"}
    assert topics == {"nuclear"}
