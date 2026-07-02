"""Unit tests for the genui adaptivity layer (src/genui/adaptivity.py)."""

import pytest

from src.genui.adaptivity import (
    _PROBE_TABLES,
    apply_signals,
    data_availability,
    filter_panels,
    merged_ui_flags,
    normalize_signals,
    panel_available,
    panel_flag_enabled,
)
from src.genui.catalog import get_panel_def
from src.genui.planner import plan
from src.genui.spec import PanelSpec, validate_spec

EMPTY_SIGNALS = {"pinned": [], "dismissed": [], "weights": {}}


def _panel(ptype, priority=0.5, rationale=""):
    pdef = get_panel_def(ptype)
    return PanelSpec(
        id=ptype,
        type=ptype,
        title=pdef.title,
        span=pdef.default_span,
        priority=priority,
        rationale=rationale,
    )


@pytest.fixture()
def clean_registry():
    """Isolate src.domains.registry and restore its process-global state."""
    from src.domains import registry

    saved_packs = dict(registry._REGISTRY)
    saved_enabled = set(registry._ENABLED)
    registry.reset()
    try:
        yield registry
    finally:
        registry.reset()
        registry._REGISTRY.update(saved_packs)
        registry._ENABLED.update(saved_enabled)


class TestNormalizeSignals:
    @pytest.mark.parametrize("raw", [None, "junk", 42, ["pinned"]])
    def test_non_dict_becomes_empty_signals(self, raw):
        assert normalize_signals(raw) == EMPTY_SIGNALS

    def test_empty_dict_becomes_empty_signals(self):
        assert normalize_signals({}) == EMPTY_SIGNALS

    def test_unknown_types_filtered_and_weights_clamped(self):
        raw = {
            "pinned": ["claims", "hologram", 5],
            "dismissed": ["trending", "bogus", None],
            "weights": {
                "claims": 50,       # clamped to 20
                "stance": -3,       # clamped to 0
                "trending": 2.7,    # truncated to int
                "articles": "lots", # non-numeric dropped
                "hologram": 5,      # unknown type dropped
            },
        }
        assert normalize_signals(raw) == {
            "pinned": ["claims"],
            "dismissed": ["trending"],
            "weights": {"claims": 20, "stance": 0, "trending": 2},
        }

    def test_non_dict_weights_dropped(self):
        assert normalize_signals({"weights": ["claims"]})["weights"] == {}


class TestApplySignals:
    def test_dismissed_panels_removed_and_returned(self):
        panels = [_panel("claims"), _panel("stance")]
        kept, removed = apply_signals(panels, {"dismissed": ["stance"]})
        assert [p.type for p in kept] == ["claims"]
        assert removed == ["stance"]

    def test_pin_wins_over_dismissal(self):
        panels = [_panel("stance")]
        kept, removed = apply_signals(
            panels, {"pinned": ["stance"], "dismissed": ["stance"]}
        )
        assert [p.type for p in kept] == ["stance"]
        assert removed == []

    def test_pinned_absent_type_appended_with_rationale(self):
        kept, _ = apply_signals([_panel("claims", priority=0.5)], {"pinned": ["trending"]})
        appended = [p for p in kept if p.type == "trending"]
        assert len(appended) == 1
        assert appended[0].rationale == "pinned by you"
        # appended at 0.6 then boosted by the pin
        assert appended[0].priority == pytest.approx(0.9)

    def test_pin_boost_capped_at_one(self):
        panel = _panel("claims", priority=0.8, rationale="selected for the 'claims' facet")
        kept, _ = apply_signals([panel], {"pinned": ["claims"]})
        assert kept[0].priority == 1.0
        assert kept[0].rationale == "selected for the 'claims' facet; pinned by you"

    def test_pinned_note_or_unknown_type_not_appended(self):
        kept, _ = apply_signals([], {"pinned": ["note", "hologram"]})
        assert kept == []

    def test_weights_nudge_priority(self):
        kept, _ = apply_signals([_panel("claims", priority=0.5)], {"weights": {"claims": 10}})
        assert kept[0].priority == pytest.approx(0.6)

    def test_ordering_by_priority_desc(self):
        panels = [_panel("claims", priority=0.4), _panel("stance", priority=0.6)]
        kept, _ = apply_signals(panels, {"pinned": ["claims"]})
        assert [p.type for p in kept] == ["claims", "stance"]
        assert kept[0].priority >= kept[1].priority

    def test_empty_signals_are_a_noop(self):
        panels = [_panel("claims", priority=0.7), _panel("stance", priority=0.3)]
        kept, removed = apply_signals(panels, EMPTY_SIGNALS)
        assert [p.type for p in kept] == ["claims", "stance"]
        assert removed == []


class TestAvailabilityAndFlags:
    def test_unknown_availability_keeps_everything(self):
        assert panel_available("claims", None) is True

    def test_tableless_and_unknown_panels_always_available(self):
        assert panel_available("note", {}) is True
        assert panel_available("hologram", {}) is True

    def test_empty_table_makes_panel_unavailable(self):
        assert panel_available("claims", {"argument_claims": False}) is False
        assert panel_available("claims", {"argument_claims": True}) is True
        assert panel_available("claims", {}) is False

    def test_flag_defaults_allow(self):
        assert panel_flag_enabled("articles", {"trending": False}) is True  # no ui_flag
        assert panel_flag_enabled("trending", None) is True
        assert panel_flag_enabled("trending", {}) is True
        assert panel_flag_enabled("trending", {"other": False}) is True
        assert panel_flag_enabled("hologram", {"trending": False}) is True

    def test_flag_off_disables_panel(self):
        assert panel_flag_enabled("trending", {"trending": False}) is False
        assert panel_flag_enabled("trending", {"trending": True}) is True

    def test_filter_panels_drops_by_availability(self):
        panels = [_panel("note"), _panel("claims"), _panel("articles")]
        availability = {"argument_claims": False, "news_articles": True}
        kept, dropped = filter_panels(panels, availability, None)
        assert [p.type for p in kept] == ["note", "articles"]
        assert dropped == ["claims"]

    def test_filter_panels_drops_by_ui_flag(self):
        panels = [_panel("trending"), _panel("articles")]
        kept, dropped = filter_panels(panels, None, {"trending": False})
        assert [p.type for p in kept] == ["articles"]
        assert dropped == ["trending"]

    def test_filter_panels_none_inputs_keep_everything(self):
        panels = [_panel("trending"), _panel("claims")]
        kept, dropped = filter_panels(panels, None, None)
        assert [p.type for p in kept] == ["trending", "claims"]
        assert dropped == []


class TestDataAvailability:
    def test_injected_probe_counts_become_bools(self):
        result = data_availability(probe=lambda: {"news_articles": 3, "argument_claims": 0})
        assert set(result) == set(_PROBE_TABLES)
        assert result["news_articles"] is True
        assert result["argument_claims"] is False
        # tables the probe did not report default to no data
        assert result["outlet_scores"] is False

    def test_probe_failure_returns_none(self):
        def broken():
            raise RuntimeError("warehouse offline")

        assert data_availability(probe=broken) is None


class TestMergedUiFlags:
    def test_empty_registry_merges_to_empty(self, clean_registry):
        assert merged_ui_flags() == {}

    def test_enabled_pack_flags_merged(self, clean_registry):
        from src.domains.base import DomainPack

        clean_registry.register_pack(
            DomainPack(name="fake", ui_flags={"trending": False, "custom_panel": True})
        )
        clean_registry.enable_pack("fake")
        assert merged_ui_flags() == {"trending": False, "custom_panel": True}

    def test_registered_but_disabled_pack_ignored(self, clean_registry):
        from src.domains.base import DomainPack

        clean_registry.register_pack(DomainPack(name="fake", ui_flags={"trending": False}))
        assert merged_ui_flags() == {}

    def test_registry_failure_degrades_to_empty(self, clean_registry, monkeypatch):
        def broken():
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(clean_registry, "get_enabled_packs", broken)
        assert merged_ui_flags() == {}


class TestPlanIntegration:
    def test_empty_availability_falls_back_to_overview(self):
        availability = {table: False for table in _PROBE_TABLES}
        spec = plan("claims about vaccines", availability=availability)
        assert validate_spec(spec.to_dict()) == []
        assert spec.panels[0].type == "note"
        assert len(spec.panels) > 1
        for panel in spec.panels[1:]:
            assert panel.rationale == "fallback overview (no live data detected)"

    def test_ui_flag_false_removes_gated_panel(self):
        baseline = {p.type for p in plan("").panels}
        assert "trending" in baseline
        spec = plan("", ui_flags={"trending": False})
        types = {p.type for p in spec.panels}
        assert "trending" not in types
        assert "Hidden for now" in spec.panels[0].body

    def test_dismissed_signal_removes_panel_from_plan(self):
        spec = plan("", signals={"dismissed": ["trending"]})
        assert "trending" not in {p.type for p in spec.panels}
        assert "Hidden for now" in spec.panels[0].body

    def test_pinned_signal_adds_panel_to_plan(self):
        baseline = {p.type for p in plan("").panels}
        assert "claims" not in baseline
        spec = plan("", signals={"pinned": ["claims"]})
        assert "claims" in {p.type for p in spec.panels}
        assert validate_spec(spec.to_dict()) == []
