"""Unit tests for the optional LLM planner (src/genui/llm.py).

The provider SDKs are never imported: completions are faked by patching
src.genui.llm._complete (or the provider dispatch targets).
"""

import json

import pytest

import src.genui.llm as llm_mod
from src.genui.llm import (
    _build_prompt,
    _extract_json,
    _sanitize,
    llm_config,
    plan_with_llm,
)
from src.genui.planner import plan
from src.genui.spec import MAX_PANELS, spec_from_dict, validate_spec

ENV_VARS = (
    "NOESIS_GENUI_LLM",
    "NOESIS_GENUI_PROVIDER",
    "NOESIS_GENUI_MODEL",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
)


@pytest.fixture()
def clean_env(monkeypatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


def _llm_payload(intent):
    """A completion built from a real planner spec, marked as non-llm output.

    _sanitize must repair generated_by back to 'llm'.
    """
    data = plan(intent).to_dict()
    data["generated_by"] = "client"
    return json.dumps(data)


class TestLlmConfig:
    def test_no_keys_returns_none(self, clean_env):
        assert llm_config() is None

    @pytest.mark.parametrize("switch", ["off", "0", "false", " OFF "])
    def test_kill_switch_wins_over_key(self, clean_env, switch):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        clean_env.setenv("NOESIS_GENUI_LLM", switch)
        assert llm_config() is None

    def test_anthropic_key_autodetected(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        config = llm_config()
        assert config == {
            "provider": "anthropic",
            "model": "claude-sonnet-5",
            "api_key": "test-key",
        }

    def test_openai_key_autodetected(self, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "sk-test")
        config = llm_config()
        assert config["provider"] == "openai"
        assert config["model"] == "gpt-4o-mini"

    def test_anthropic_preferred_when_both_keys_present(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "a-key")
        clean_env.setenv("OPENAI_API_KEY", "o-key")
        assert llm_config()["provider"] == "anthropic"

    def test_explicit_provider_selects_openai(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "a-key")
        clean_env.setenv("OPENAI_API_KEY", "o-key")
        clean_env.setenv("NOESIS_GENUI_PROVIDER", "openai")
        config = llm_config()
        assert config["provider"] == "openai"
        assert config["api_key"] == "o-key"

    def test_model_override(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        clean_env.setenv("NOESIS_GENUI_MODEL", "claude-fable-5")
        assert llm_config()["model"] == "claude-fable-5"

    def test_unknown_provider_returns_none(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        clean_env.setenv("NOESIS_GENUI_PROVIDER", "cohere")
        assert llm_config() is None

    def test_provider_without_matching_key_returns_none(self, clean_env):
        clean_env.setenv("NOESIS_GENUI_PROVIDER", "openai")
        clean_env.setenv("ANTHROPIC_API_KEY", "a-key")
        assert llm_config() is None


class TestExtractJson:
    def test_plain_json(self):
        assert _extract_json('{"title": "X"}') == {"title": "X"}

    def test_fenced_json_block(self):
        text = '```json\n{"title": "X", "panels": []}\n```'
        assert _extract_json(text) == {"title": "X", "panels": []}

    def test_plain_fence_without_language_tag(self):
        assert _extract_json('```\n{"a": 1}\n```') == {"a": 1}

    def test_prose_wrapped_json(self):
        text = 'Here is your layout:\n{"title": "X"}\nHope that helps!'
        assert _extract_json(text) == {"title": "X"}

    def test_garbage_returns_none(self):
        assert _extract_json("no json here at all") is None

    def test_malformed_json_returns_none(self):
        assert _extract_json("{not: valid json}") is None

    def test_non_object_json_returns_none(self):
        assert _extract_json("[1, 2, 3]") is None


class TestSanitize:
    def test_repairs_near_misses(self):
        raw = {
            "title": "",
            "generated_by": "heuristic",
            "spec_version": "bogus",
            "panels": [
                {"id": "p1", "type": "articles", "title": "", "span": 99, "priority": 5},
                {"id": "p1", "type": "claims", "title": "Claims", "span": 1, "priority": -2},
                {"id": "p3", "type": "hologram", "title": "H", "span": 6, "priority": 0.5},
            ],
        }
        spec = _sanitize(spec_from_dict(raw), "x" * 600)
        assert spec.generated_by == "llm"
        assert spec.spec_version == "ui-spec-v1"
        assert spec.title == "Adaptive Canvas"
        assert spec.intent == "x" * 500
        assert [p.type for p in spec.panels] == ["articles", "claims"]
        first, second = spec.panels
        assert first.span == 12 and first.priority == 1.0
        assert first.title == "Latest documents"
        assert first.endpoint == "/api/v1/news/articles"
        assert second.span == 3 and second.priority == 0.0
        assert second.id == "p2"  # duplicate id repaired

    def test_caps_panels_at_max(self):
        raw = {
            "title": "Big",
            "panels": [
                {"id": f"p{i}", "type": "articles", "title": "Docs", "span": 6, "priority": 0.5}
                for i in range(MAX_PANELS + 3)
            ],
        }
        spec = _sanitize(spec_from_dict(raw), "intent")
        assert len(spec.panels) == MAX_PANELS


class TestBuildPrompt:
    def test_includes_catalog_intent_and_context(self):
        prompt = _build_prompt(
            "climate policy", "blog", {"news_articles": True}, {"trending": False}
        )
        assert '"panel_catalog"' in prompt
        assert "'climate policy'" in prompt
        assert '"table_has_data"' in prompt
        assert '"ui_flags"' in prompt
        assert '"source_type_filter"' in prompt

    def test_omits_optional_context(self):
        prompt = _build_prompt("x", None, None, None)
        assert '"panel_catalog"' in prompt
        assert '"table_has_data"' not in prompt
        assert '"ui_flags"' not in prompt
        assert '"source_type_filter"' not in prompt


class TestPlanWithLlm:
    def test_no_config_returns_none(self, clean_env):
        assert plan_with_llm("anything") is None

    def test_valid_completion_returns_llm_spec(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        intent = "compare outlet framing on climate policy"
        clean_env.setattr(llm_mod, "_complete", lambda config, prompt: _llm_payload(intent))
        result = plan_with_llm(intent)
        assert result is not None
        assert result.generated_by == "llm"
        assert result.intent == intent
        assert validate_spec(result.to_dict()) == []

    def test_invalid_json_returns_none(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        clean_env.setattr(llm_mod, "_complete", lambda config, prompt: "utter nonsense")
        assert plan_with_llm("anything") is None

    def test_empty_completion_returns_none(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        clean_env.setattr(llm_mod, "_complete", lambda config, prompt: "")
        assert plan_with_llm("anything") is None

    def test_unknown_panel_types_only_returns_none(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        payload = json.dumps(
            {
                "title": "Bad",
                "panels": [
                    {"id": "p1", "type": "hologram", "title": "H", "span": 6, "priority": 0.5}
                ],
            }
        )
        clean_env.setattr(llm_mod, "_complete", lambda config, prompt: payload)
        assert plan_with_llm("anything") is None

    def test_completion_exception_returns_none(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")

        def broken(config, prompt):
            raise RuntimeError("provider down")

        clean_env.setattr(llm_mod, "_complete", broken)
        assert plan_with_llm("anything") is None

    def test_dispatch_routes_to_anthropic(self, clean_env):
        clean_env.setenv("ANTHROPIC_API_KEY", "test-key")
        seen = {}

        def fake_anthropic(config, prompt):
            seen["provider"] = config["provider"]
            return _llm_payload("overview")

        clean_env.setattr(llm_mod, "_complete_anthropic", fake_anthropic)
        result = plan_with_llm("overview")
        assert seen["provider"] == "anthropic"
        assert result is not None and result.generated_by == "llm"

    def test_dispatch_routes_to_openai(self, clean_env):
        clean_env.setenv("OPENAI_API_KEY", "sk-test")
        seen = {}

        def fake_openai(config, prompt):
            seen["provider"] = config["provider"]
            return _llm_payload("overview")

        clean_env.setattr(llm_mod, "_complete_openai", fake_openai)
        result = plan_with_llm("overview")
        assert seen["provider"] == "openai"
        assert result is not None and result.generated_by == "llm"
