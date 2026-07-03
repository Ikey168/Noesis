"""M6.1: the spec-diff apply path. Applies add/remove/retarget diffs onto a
canvas and returns a validated ui-spec, so a refinement is a diff, not a
full regeneration."""

from src.genui.planner import plan
from src.genui.spec_diff import apply_diff


def _canvas():
    # A focused canvas (the actors facet yields 5 panels), leaving room to add.
    return plan("who are the key actors")


def _types(spec):
    return [p.type for p in spec.panels if p.type != "note"]


def test_add_panel_appends_and_validates():
    spec = _canvas()
    out, errors = apply_diff(spec, [{"op": "add", "panel": {"type": "contradiction_ledger"}}])
    assert errors == []
    assert "contradiction_ledger" in _types(out)
    # Ids are re-numbered and the note stays first.
    assert out.panels[0].type == "note"
    assert [p.id for p in out.panels] == [f"p{i + 1}" for i in range(len(out.panels))]


def test_remove_panel_by_type():
    spec = _canvas()
    assert "actors" in _types(spec)
    out, errors = apply_diff(spec, [{"op": "remove", "type": "actors"}])
    assert errors == []
    assert "actors" not in _types(out)


def test_retarget_merges_params():
    spec = _canvas()
    target = _types(spec)[0]
    out, errors = apply_diff(spec, [{"op": "retarget", "type": target, "params": {"topic": "nuclear"}}])
    assert errors == []
    panel = next(p for p in out.panels if p.type == target)
    assert panel.params.get("topic") == "nuclear"


def test_unknown_panel_type_is_reported_not_applied():
    spec = _canvas()
    out, errors = apply_diff(spec, [{"op": "add", "panel": {"type": "not_a_panel"}}])
    assert any("unknown panel type" in e for e in errors)
    assert "not_a_panel" not in _types(out)


def test_unknown_op_and_bad_shapes_are_reported():
    spec = _canvas()
    _, errors = apply_diff(spec, [{"op": "frobnicate"}, "nope", {"op": "remove"}])
    assert any("unknown op" in e for e in errors)
    assert any("not an object" in e for e in errors)
    assert any("requires a type or id" in e for e in errors)


def test_note_is_never_removed():
    spec = _canvas()
    out, _ = apply_diff(spec, [{"op": "remove", "type": "note"}])
    assert out.panels[0].type == "note"


def test_full_canvas_rejects_further_adds():
    spec = _canvas()
    # Fill to capacity, then one more add is rejected.
    # 5 base + 6 fillers = 11 (the data-panel cap).
    fillers = ["clusters", "timeline", "watchlists", "documents", "frames", "controversy"]
    diff = [{"op": "add", "panel": {"type": t}} for t in fillers]
    out, _ = apply_diff(spec, diff)
    assert len(_types(out)) == 11
    over, errors = apply_diff(out, [{"op": "add", "panel": {"type": "provenance_trace"}}])
    assert len(_types(over)) == 11
    assert any("canvas is full" in e for e in errors)


def test_multi_op_diff_applies_in_order_and_validates():
    spec = _canvas()
    out, errors = apply_diff(
        spec,
        [
            {"op": "add", "panel": {"type": "corroboration"}},
            {"op": "remove", "type": "positions"},
        ],
    )
    assert errors == []
    types = _types(out)
    assert "corroboration" in types and "positions" not in types
