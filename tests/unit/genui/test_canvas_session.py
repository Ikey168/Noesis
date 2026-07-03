"""M6.2: multi-turn canvas session. The session carries the canvas across turns,
applying each refinement as a spec diff, grounds the refiner on the current
canvas, and degrades gracefully when no LLM is configured."""

from src.genui.canvas_session import CanvasSession, live_data_context, start_session


def _types(spec):
    return [p.type for p in spec.panels if p.type != "note"]


def test_session_carries_the_canvas_across_turns():
    session = start_session("who are the key actors")
    assert "actors" in _types(session.spec)

    # Turn 1: add a panel.
    spec1, errors = session.refine([{"op": "add", "panel": {"type": "corroboration"}}])
    assert errors == [] and "corroboration" in _types(spec1)

    # Turn 2: remove one; the session state reflects both turns.
    spec2, errors = session.refine([{"op": "remove", "type": "actors"}])
    assert errors == []
    assert "corroboration" in _types(spec2) and "actors" not in _types(spec2)
    assert len(session.turns) == 2 and all(t["ok"] for t in session.turns)


def test_a_bad_diff_leaves_the_canvas_unchanged():
    session = start_session("who are the key actors")
    before = _types(session.spec)
    spec, errors = session.refine([{"op": "add", "panel": {"type": "not_a_panel"}}])
    assert errors and _types(spec) == before  # unchanged
    assert session.turns[-1]["ok"] is False


def test_live_data_context_summarizes_the_canvas():
    session = start_session("sentiment for energy")
    ctx = live_data_context(session.spec)
    assert "panels" in ctx and ctx["panels"]
    assert all("type" in p for p in ctx["panels"])
    assert "facets" in ctx


def test_refine_with_pluggable_refiner_is_grounded():
    session = start_session("who are the key actors")
    seen = {}

    def refiner(spec, instruction, context):
        seen["context"] = context
        seen["instruction"] = instruction
        return [{"op": "add", "panel": {"type": "reliability_card"}}]

    spec, errors = session.refine_with("add source reliability", refiner)
    assert errors == [] and "reliability_card" in _types(spec)
    # The refiner was handed the grounding context and the instruction.
    assert seen["instruction"] == "add source reliability"
    assert "panels" in seen["context"]


def test_refine_with_llm_degrades_gracefully_without_a_key(monkeypatch):
    # No LLM configured -> canvas unchanged, a clear reason returned.
    import src.genui.llm as llm

    monkeypatch.setattr(llm, "llm_config", lambda: None)
    session = start_session("who are the key actors")
    before = _types(session.spec)
    spec, errors = session.refine_with_llm("focus on energy")
    assert errors and _types(spec) == before


def test_refiner_returning_none_leaves_canvas_unchanged():
    session = start_session("who are the key actors")
    before = _types(session.spec)
    spec, errors = session.refine_with("do nothing", lambda s, i, c: None)
    assert errors == ["refiner produced no diff"]
    assert _types(spec) == before
