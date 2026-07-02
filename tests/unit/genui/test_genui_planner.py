"""Unit tests for the heuristic intent planner (src/genui/planner.py)."""

import pytest

from src.genui.catalog import get_panel_def
from src.genui.planner import (
    detect_days,
    detect_source_type,
    extract_topic,
    plan,
    score_facets,
)
from src.genui.planner import _apply_params
from src.genui.spec import MAX_PANELS, PanelSpec, validate_spec


class TestScoreFacets:
    def test_outlet_comparison_scores_sources(self):
        assert score_facets("compare outlet framing") == {"sources": 3}

    def test_disagreement_scores_actors_and_stance(self):
        assert score_facets("who disagrees about AI regulation") == {
            "actors": 1,
            "stance": 1,
        }

    def test_multi_word_phrase_matches_as_substring(self):
        scores = score_facets("how did sentiment change over time")
        assert scores == {"sentiment": 1, "trend": 1}

    def test_hyphenated_keyword_matches(self):
        assert score_facets("fact-check the claims") == {"claims": 2}

    def test_empty_intent_scores_nothing(self):
        assert score_facets("") == {}


class TestDetectSourceType:
    def test_blogs_maps_to_blog(self):
        assert detect_source_type("what's happening in blogs") == "blog"

    def test_papers_maps_to_paper(self):
        assert detect_source_type("recent papers on fusion") == "paper"

    def test_no_mention_returns_none(self):
        assert detect_source_type("climate policy coverage") is None


class TestDetectDays:
    def test_explicit_day_count(self):
        assert detect_days("last 14 days") == 14

    def test_month_word(self):
        assert detect_days("this month") == 30

    def test_day_count_clamped_to_a_year(self):
        assert detect_days("last 900 days") == 365

    def test_day_count_clamped_to_at_least_one(self):
        assert detect_days("0 days ago") == 1

    def test_today_is_one_day(self):
        assert detect_days("today") == 1

    def test_no_time_mention_returns_none(self):
        assert detect_days("climate policy") is None


class TestExtractTopic:
    def test_strips_facet_and_stopwords(self):
        assert extract_topic("compare outlet framing on climate policy") == "climate policy"

    def test_strips_time_and_source_type_words(self):
        assert extract_topic("sentiment in blogs last 14 days") is None

    def test_only_noise_returns_none(self):
        assert extract_topic("show trends") is None
        assert extract_topic("") is None

    def test_topic_capped_at_five_tokens(self):
        topic = extract_topic("alpha beta gamma delta epsilon zeta eta")
        assert topic == "alpha beta gamma delta epsilon"


class TestPlan:
    @pytest.mark.parametrize(
        "intent",
        [
            "",
            "compare outlet framing on climate policy this month",
            "who disagrees about AI regulation",
            "sentiment in blogs last 14 days",
            "breaking events today",
            "claims and misinformation about vaccines",
            "which outlets are biased",
            "entity connections in the influence graph",
            "x" * 600,  # overlong intent gets truncated but must still validate
        ],
        ids=lambda i: (i[:30] or "<empty>"),
    )
    def test_plan_output_validates(self, intent):
        spec = plan(intent)
        assert validate_spec(spec.to_dict()) == []
        assert len(spec.panels) <= MAX_PANELS
        ids = [p.id for p in spec.panels]
        assert len(ids) == len(set(ids))

    def test_note_panel_is_always_first_with_body(self):
        for intent in ("", "compare outlet framing on climate policy"):
            spec = plan(intent)
            note = spec.panels[0]
            assert note.type == "note"
            assert note.id == "p1"
            assert note.span == 12
            assert note.priority == 1.0
            assert note.body

    def test_empty_intent_defaults_to_overview_briefing(self):
        spec = plan("")
        assert spec.facets == ["overview"]
        assert spec.title == "Adaptive Briefing"
        assert spec.topic is None
        assert spec.source_type is None
        assert "Default adaptive briefing" in spec.panels[0].body

    def test_topic_param_lands_on_topic_panels(self):
        spec = plan("claims about climate policy")
        assert spec.topic == "climate policy"
        topic_panels = [
            p for p in spec.panels if get_panel_def(p.type).topic_param
        ]
        assert topic_panels
        for panel in topic_panels:
            pdef = get_panel_def(panel.type)
            assert panel.params[pdef.topic_param] == "climate policy"

    def test_source_type_and_days_params_land_on_matching_panels(self):
        spec = plan("sentiment in blogs last 14 days")
        assert spec.source_type == "blog"
        assert spec.topic is None
        matched_days = matched_type = 0
        for panel in spec.panels:
            pdef = get_panel_def(panel.type)
            if pdef.days_param:
                assert panel.params[pdef.days_param] == 14
                matched_days += 1
            if pdef.source_type_param:
                assert panel.params[pdef.source_type_param] == "blog"
                matched_type += 1
        assert matched_days and matched_type

    def test_panels_get_catalog_endpoints(self):
        spec = plan("compare outlet framing on climate policy")
        for panel in spec.panels[1:]:
            assert panel.endpoint == get_panel_def(panel.type).endpoint

    def test_explicit_source_type_overrides_detection(self):
        spec = plan("claims in blogs", source_type="paper")
        assert spec.source_type == "paper"
        for panel in spec.panels:
            pdef = get_panel_def(panel.type)
            if pdef.source_type_param:
                assert panel.params[pdef.source_type_param] == "paper"

    def test_detected_source_type_used_when_no_override(self):
        assert plan("claims in blogs").source_type == "blog"

    def test_facets_capped_at_three(self):
        spec = plan("sentiment trends claims stance actors conflict entities events sources")
        assert spec.facets == ["actors", "claims", "conflict"]

    def test_panel_count_capped_at_max_panels(self):
        spec = plan("sentiment trends claims stance actors conflict entities events sources")
        assert 1 < len(spec.panels) <= MAX_PANELS
        assert validate_spec(spec.to_dict()) == []

    def test_intent_truncated_to_500_chars(self):
        spec = plan("y" * 600)
        assert spec.intent == "y" * 500

    def test_subtitle_mentions_filters(self):
        spec = plan("sentiment in blogs last 14 days")
        assert "sentiment" in spec.subtitle
        assert "blog" in spec.subtitle
        assert "14d window" in spec.subtitle

    def test_apply_params_skips_unknown_panel_types(self):
        panel = PanelSpec(id="x", type="hologram", title="H", span=6, priority=0.5)
        _apply_params([panel], "climate", "blog", 7)
        assert panel.params == {}
        assert panel.endpoint is None

    def test_narrative_mentions_topic_type_and_window(self):
        spec = plan("claims about climate policy in blogs last 14 days")
        body = spec.panels[0].body
        assert "climate policy" in body
        assert "blog" in body
        assert "14" in body
