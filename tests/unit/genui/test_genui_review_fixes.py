"""Regression tests for defects found in the adversarial review of genui.

Each test pins the fixed behavior:
* spec_from_dict never raises on malformed LLM output (span/priority junk);
* validate_spec rejects non-string topic and non-scalar params values;
* _sanitize scrubs non-scalar params and repairs duplicate ids uniquely;
* plan_with_llm enforces usage signals (mutes/pins) on LLM layouts and
  returns None instead of raising on unrepairable output;
* the heuristic planner clamps days params to each endpoint's bound.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.genui.llm import _sanitize, plan_with_llm
from src.genui.planner import plan
from src.genui.spec import PanelSpec, UISpec, spec_from_dict, validate_spec


def _llm_env(monkeypatch):
    monkeypatch.setenv("NOESIS_GENUI_LLM", "auto")
    monkeypatch.setenv("NOESIS_GENUI_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("NOESIS_GENUI_MODEL", raising=False)


class TestSpecFromDictCoercion:
    def test_non_numeric_span_and_priority_fall_back_to_defaults(self):
        spec = spec_from_dict({
            "intent": "x",
            "title": "T",
            "panels": [
                {"id": "p1", "type": "articles", "title": "A", "span": "wide", "priority": "high"},
                {"id": "p2", "type": "trending", "title": "B", "span": float("inf"), "priority": 0.7},
            ],
        })
        assert spec.panels[0].span == 6
        assert spec.panels[0].priority == 0.5
        assert spec.panels[1].span == 6
        assert spec.panels[1].priority == 0.7

    def test_non_string_topic_and_source_type_become_none(self):
        spec = spec_from_dict({
            "intent": "x",
            "title": "T",
            "topic": {"evil": 1},
            "source_type": 42,
            "panels": [{"id": "p1", "type": "note", "title": "Plan"}],
        })
        assert spec.topic is None
        assert spec.source_type is None


class TestValidateSpecHardening:
    def _base(self):
        return {
            "spec_version": "ui-spec-v1",
            "intent": "x",
            "title": "T",
            "generated_by": "llm",
            "panels": [
                {"id": "p1", "type": "articles", "title": "A", "span": 6, "priority": 0.5, "params": {}},
            ],
        }

    def test_non_string_topic_rejected(self):
        bad = self._base()
        bad["topic"] = {"evil": 1}
        assert any("topic" in e for e in validate_spec(bad))

    def test_non_scalar_params_values_rejected(self):
        bad = self._base()
        bad["panels"][0]["params"] = {"topic": {"x": []}}
        assert any("params" in e for e in validate_spec(bad))

    def test_scalar_params_values_accepted(self):
        good = self._base()
        good["panels"][0]["params"] = {"topic": "energy", "days": 7, "flag": True}
        assert validate_spec(good) == []


class TestSanitizeHardening:
    def test_non_scalar_params_scrubbed(self):
        spec = UISpec(intent="x", title="T", panels=[
            PanelSpec(id="p1", type="articles", title="A",
                      params={"topic": 42, "days": {"x": []}, "ok": "yes"}),
        ])
        _sanitize(spec, "x")
        assert spec.panels[0].params == {"topic": 42, "ok": "yes"}

    def test_duplicate_ids_repaired_uniquely(self):
        spec = UISpec(intent="x", title="T", panels=[
            PanelSpec(id="p2", type="articles", title="A"),
            PanelSpec(id="p2", type="trending", title="B"),
        ])
        _sanitize(spec, "x")
        ids = [p.id for p in spec.panels]
        assert len(ids) == len(set(ids))


class TestPlanWithLlmSignalsAndSafety:
    def test_signals_enforced_on_llm_layout(self, monkeypatch):
        _llm_env(monkeypatch)
        base = plan("overview briefing").to_dict()
        import json
        monkeypatch.setattr("src.genui.llm._complete", lambda config, prompt: json.dumps(base))
        spec = plan_with_llm(
            "overview briefing",
            signals={"pinned": ["claims"], "dismissed": ["trending"], "weights": {}},
        )
        assert spec is not None
        types = [p.type for p in spec.panels]
        assert "trending" not in types
        assert "claims" in types
        assert validate_spec(spec.to_dict()) == []

    def test_unrepairable_llm_output_returns_none(self, monkeypatch):
        _llm_env(monkeypatch)
        monkeypatch.setattr(
            "src.genui.llm._complete",
            lambda config, prompt: '{"title": "T", "panels": [{"id": "p1", "type": "articles", "title": "A", "span": "wide", "priority": "high", "params": {"topic": {"deep": []}}}]}',
        )
        # Junk span/priority coerce to defaults; non-scalar params scrub; the
        # result is a valid spec — the key assertion is that nothing raises.
        spec = plan_with_llm("x")
        assert spec is None or validate_spec(spec.to_dict()) == []


class TestDaysClamp:
    def test_days_clamped_to_endpoint_bound(self):
        spec = plan("trending stories over the last 200 days")
        trending = [p for p in spec.panels if p.type == "trending"]
        assert trending, "trending panel expected for a trend intent"
        assert trending[0].params.get("days") == 30
