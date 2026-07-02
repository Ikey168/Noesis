"""Mock-MCP tests for the R4 grounded planning loop (src/genui/llm.py).

The loop is exercised entirely against a fake host and a scripted
conversation injected through ``_make_conversation`` — no provider SDK, no
network, no subprocess. Covers the exit criterion (plans reflect inspected
data), budget exhaustion, tool errors, allowlist rejection and the
malformed-JSON fallback.
"""

import json

import pytest

import src.genui.llm as llm
from src.genui.llm import (
    INSPECTION_ALLOWLIST,
    LLMTurn,
    ToolInvocation,
    ToolResult,
    _advertise_tools,
    _execute_tool_calls,
    plan_with_llm,
)

CONFIG = {"provider": "anthropic", "model": "m", "api_key": "k"}


class FakeHost:
    """Cached tool host: canned results, records executed calls."""

    def __init__(self, tools_by_server, results):
        self._tools = tools_by_server
        self._results = results
        self.calls = []

    def tools(self, server=None):
        if server is not None:
            return {server: self._tools.get(server, [])}
        return dict(self._tools)

    def call_tool_cached(self, server, tool, arguments=None, timeout=10.0, ttl=None):
        self.calls.append((server, tool, arguments))
        result = self._results[(server, tool)]
        if isinstance(result, Exception):
            raise result
        return result


def tool_entry(name, schema=None):
    return {
        "name": name,
        "description": f"{name} description",
        "meta": {},
        "has_output_schema": True,
        "input_schema": schema or {"type": "object"},
    }


ARG_TOOLS = [
    tool_entry("am_stats"),
    tool_entry("list_stances"),
    tool_entry("trigger_actor_batch"),  # RW: must never be advertised
]


class ScriptedConversation(llm.Conversation):
    """Returns a fixed list of turns; records how it was driven."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.sends = []

    def send(self, results, allow_tools):
        self.sends.append((results, allow_tools))
        return self._turns.pop(0)


class GroundedModel(llm.Conversation):
    """A fake model that actually reacts to am_stats: it asks for the stats,
    then includes a stance panel only if source_stances has rows."""

    def send(self, results, allow_tools):
        if results is None:
            return LLMTurn(
                tool_calls=[ToolInvocation("c1", "neuronews-arguments__am_stats", {})]
            )
        am = results[0].content
        panels = [
            {"id": "p1", "type": "note", "title": "Plan", "span": 12, "priority": 1.0, "body": "x"}
        ]
        if am.get("source_stances", 0) > 0:
            panels.append(
                {"id": "p2", "type": "stance", "title": "Stance", "span": 6, "priority": 0.8}
            )
        else:
            panels.append(
                {"id": "p2", "type": "kpi_row", "title": "Summary", "span": 12, "priority": 0.8}
            )
        return LLMTurn(final_text=json.dumps({"title": "T", "panels": panels}))


@pytest.fixture
def loop_env(monkeypatch):
    """Force the LLM path on and route the host/config through fakes."""
    monkeypatch.setattr(llm, "llm_config", lambda: CONFIG)
    monkeypatch.delenv("NOESIS_GENUI_LOOP", raising=False)
    monkeypatch.delenv("NOESIS_GENUI_LOOP_BUDGET_MS", raising=False)

    def _install(host, conversation):
        monkeypatch.setattr(llm, "_planning_host", lambda: host)
        monkeypatch.setattr(
            llm, "_make_conversation", lambda *a, **k: conversation
        )
        return host, conversation

    return _install


# ---------------------------------------------------------------------------
# Tool advertising + allowlist
# ---------------------------------------------------------------------------


def test_advertise_only_allowlisted_tools():
    host = FakeHost({"neuronews-arguments": ARG_TOOLS}, {})
    specs, name_map = _advertise_tools(host)
    names = {s["name"] for s in specs}
    assert names == {
        "neuronews-arguments__am_stats",
        "neuronews-arguments__list_stances",
    }
    assert "neuronews-arguments__trigger_actor_batch" not in names
    assert name_map["neuronews-arguments__am_stats"] == ("neuronews-arguments", "am_stats")


def test_execute_rejects_non_allowlisted_call():
    host = FakeHost({}, {})
    _, name_map = _advertise_tools(FakeHost({"neuronews-arguments": ARG_TOOLS}, {}))
    calls = [ToolInvocation("c1", "neuronews-arguments__trigger_actor_batch", {})]
    results = _execute_tool_calls(host, calls, name_map)
    assert results[0].is_error
    assert "not permitted" in results[0].content["error"]
    assert host.calls == []  # never reached the host


def test_execute_wraps_tool_exceptions():
    host = FakeHost(
        {"neuronews-arguments": ARG_TOOLS},
        {("neuronews-arguments", "am_stats"): RuntimeError("boom")},
    )
    _, name_map = _advertise_tools(host)
    calls = [ToolInvocation("c1", "neuronews-arguments__am_stats", {})]
    results = _execute_tool_calls(host, calls, name_map)
    assert results[0].is_error
    assert "RuntimeError" in results[0].content["error"]


def test_execute_flags_error_payloads():
    host = FakeHost(
        {"neuronews-arguments": ARG_TOOLS},
        {("neuronews-arguments", "am_stats"): {"error": "warehouse locked"}},
    )
    _, name_map = _advertise_tools(host)
    calls = [ToolInvocation("c1", "neuronews-arguments__am_stats", {})]
    results = _execute_tool_calls(host, calls, name_map)
    assert results[0].is_error is True


# ---------------------------------------------------------------------------
# Exit criterion: plans reflect inspected data
# ---------------------------------------------------------------------------


def test_plan_includes_stance_when_data_present(loop_env):
    host = FakeHost(
        {"neuronews-arguments": ARG_TOOLS},
        {("neuronews-arguments", "am_stats"): {"source_stances": 5}},
    )
    loop_env(host, GroundedModel())
    spec = plan_with_llm("what is the stance on ai")
    types = {p.type for p in spec.panels}
    assert "stance" in types
    assert host.calls == [("neuronews-arguments", "am_stats", {})]


def test_plan_skips_stance_when_no_data(loop_env):
    host = FakeHost(
        {"neuronews-arguments": ARG_TOOLS},
        {("neuronews-arguments", "am_stats"): {"source_stances": 0}},
    )
    loop_env(host, GroundedModel())
    spec = plan_with_llm("what is the stance on ai")
    types = {p.type for p in spec.panels}
    assert "stance" not in types  # the loop grounded the plan in empty data
    assert spec.generated_by == "llm"


# ---------------------------------------------------------------------------
# Budget + degradation
# ---------------------------------------------------------------------------


def test_final_without_tools_returns_spec(loop_env):
    host = FakeHost({"neuronews-arguments": ARG_TOOLS}, {})
    convo = ScriptedConversation(
        [
            LLMTurn(
                final_text=json.dumps(
                    {
                        "title": "T",
                        "panels": [
                            {"id": "p1", "type": "note", "title": "Plan", "span": 12, "priority": 1.0, "body": "x"}
                        ],
                    }
                )
            )
        ]
    )
    loop_env(host, convo)
    spec = plan_with_llm("overview")
    assert spec is not None and spec.generated_by == "llm"
    # First send offers tools (allow_tools True) but the model answered directly.
    assert convo.sends[0][1] is True


def test_small_budget_skips_loop_and_uses_one_shot(loop_env, monkeypatch):
    monkeypatch.setenv("NOESIS_GENUI_LOOP_BUDGET_MS", "100")  # < MIN_LOOP_BUDGET_MS
    host = FakeHost({"neuronews-arguments": ARG_TOOLS}, {})
    # The loop must not run; a scripted conversation that would explode if used.
    exploding = ScriptedConversation([])
    loop_env(host, exploding)
    one_shot = json.dumps(
        {"title": "T", "panels": [{"id": "p1", "type": "note", "title": "P", "span": 12, "priority": 1.0, "body": "x"}]}
    )
    monkeypatch.setattr(llm, "_complete", lambda config, prompt: one_shot)
    spec = plan_with_llm("overview")
    assert spec is not None
    assert exploding.sends == []  # loop never entered
    assert host.calls == []


def test_loop_disabled_uses_one_shot(loop_env, monkeypatch):
    monkeypatch.setenv("NOESIS_GENUI_LOOP", "off")
    host = FakeHost({"neuronews-arguments": ARG_TOOLS}, {})
    loop_env(host, ScriptedConversation([]))
    monkeypatch.setattr(
        llm,
        "_complete",
        lambda config, prompt: json.dumps(
            {"title": "T", "panels": [{"id": "p1", "type": "note", "title": "P", "span": 12, "priority": 1.0, "body": "x"}]}
        ),
    )
    assert plan_with_llm("overview") is not None


def test_no_host_uses_one_shot(loop_env, monkeypatch):
    loop_env(None, ScriptedConversation([]))
    called = {}
    one_shot = json.dumps(
        {"title": "T", "panels": [{"id": "p1", "type": "note", "title": "P", "span": 12, "priority": 1.0, "body": "x"}]}
    )

    def fake_complete(config, prompt):
        called["hit"] = True
        return one_shot

    monkeypatch.setattr(llm, "_complete", fake_complete)
    assert plan_with_llm("overview") is not None
    assert called.get("hit")


def test_model_ignores_force_final_falls_back(loop_env, monkeypatch):
    # A misbehaving model that always demands tools; when the loop forces a
    # final answer it still asks for a tool, so the loop gives up and the
    # one-shot path takes over.
    host = FakeHost(
        {"neuronews-arguments": ARG_TOOLS},
        {("neuronews-arguments", "am_stats"): {"source_stances": 1}},
    )

    class AlwaysTools(llm.Conversation):
        def send(self, results, allow_tools):
            return LLMTurn(
                tool_calls=[ToolInvocation("c1", "neuronews-arguments__am_stats", {})]
            )

    loop_env(host, AlwaysTools())
    one_shot = json.dumps(
        {"title": "T", "panels": [{"id": "p1", "type": "note", "title": "P", "span": 12, "priority": 1.0, "body": "x"}]}
    )
    monkeypatch.setattr(llm, "_complete", lambda config, prompt: one_shot)
    spec = plan_with_llm("overview")
    assert spec is not None  # degraded to one-shot after exhausting rounds


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_malformed_final_json_falls_back_to_heuristic(loop_env, monkeypatch):
    host = FakeHost({"neuronews-arguments": ARG_TOOLS}, {})
    loop_env(host, ScriptedConversation([LLMTurn(final_text="not json at all")]))
    # One-shot also returns junk, so the whole LLM path yields None and the
    # caller falls back to the heuristic planner.
    monkeypatch.setattr(llm, "_complete", lambda config, prompt: "still not json")
    assert plan_with_llm("overview") is None


def test_conversation_exception_falls_back(loop_env, monkeypatch):
    host = FakeHost({"neuronews-arguments": ARG_TOOLS}, {})

    class Boom(llm.Conversation):
        def send(self, results, allow_tools):
            raise RuntimeError("provider down")

    loop_env(host, Boom())
    monkeypatch.setattr(llm, "_complete", lambda config, prompt: None)
    assert plan_with_llm("overview") is None


def test_no_config_returns_none(monkeypatch):
    monkeypatch.setattr(llm, "llm_config", lambda: None)
    assert plan_with_llm("overview") is None


def test_loop_enforces_usage_signals(loop_env):
    """Pins/mutes are applied to the loop's output like the heuristic path."""
    host = FakeHost({"neuronews-arguments": ARG_TOOLS}, {})
    spec_json = json.dumps(
        {
            "title": "T",
            "panels": [
                {"id": "p1", "type": "note", "title": "Plan", "span": 12, "priority": 1.0, "body": "x"},
                {"id": "p2", "type": "trending", "title": "Trending", "span": 6, "priority": 0.8},
            ],
        }
    )
    loop_env(host, ScriptedConversation([LLMTurn(final_text=spec_json)]))
    spec = plan_with_llm(
        "overview",
        signals={"pinned": ["claims"], "dismissed": ["trending"], "weights": {}},
    )
    types = {p.type for p in spec.panels}
    assert "trending" not in types  # muted
    assert "claims" in types  # pinned


# ---------------------------------------------------------------------------
# Provider adapters (fake SDK modules; no network)
# ---------------------------------------------------------------------------

import sys  # noqa: E402
import types as _types  # noqa: E402
from types import SimpleNamespace  # noqa: E402

ADVERTISED_TOOLS = [
    {"name": "neuronews-arguments__am_stats", "description": "d", "input_schema": {"type": "object"}},
]


def _install_fake_module(monkeypatch, name, client_cls):
    mod = _types.ModuleType(name)
    mod.instances = []
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def test_anthropic_conversation_normalizes_tool_then_final(monkeypatch):
    mod = _install_fake_module(monkeypatch, "anthropic", None)
    responses = [
        SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", id="tu1", name="neuronews-arguments__am_stats", input={"limit": 1})]
        ),
        SimpleNamespace(content=[SimpleNamespace(type="text", text='{"title":"T","panels":[]}')]),
    ]

    class Client:
        def __init__(self, api_key, timeout=None):
            mod.instances.append(self)
            self.messages = self
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return responses.pop(0)

    mod.Anthropic = Client

    convo = llm._AnthropicConversation(CONFIG, "sys", "user prompt", ADVERTISED_TOOLS, 5.0)
    turn = convo.send(None, allow_tools=True)
    assert turn.final_text is None
    assert turn.tool_calls[0].advertised_name == "neuronews-arguments__am_stats"
    assert turn.tool_calls[0].arguments == {"limit": 1}
    # First request carried tools; system prompt passed through.
    first = mod.instances[0].calls[0]
    assert first["tools"] == ADVERTISED_TOOLS
    assert first["system"] == "sys"

    results = [ToolResult("tu1", "neuronews-arguments__am_stats", {"n": 3}, is_error=False)]
    turn2 = convo.send(results, allow_tools=False)
    assert turn2.final_text == '{"title":"T","panels":[]}'
    # Force-final turn omits tools; a tool_result user message was fed back.
    second = mod.instances[0].calls[1]
    assert "tools" not in second
    tool_results = [
        b
        for m in second["messages"]
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]
    assert tool_results and tool_results[0]["tool_use_id"] == "tu1"


def test_openai_conversation_normalizes_tool_then_final(monkeypatch):
    mod = _install_fake_module(monkeypatch, "openai", None)
    tool_call = SimpleNamespace(
        id="tc1",
        function=SimpleNamespace(name="neuronews-arguments__am_stats", arguments='{"limit": 1}'),
    )
    responses = [
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))]),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"title":"T","panels":[]}', tool_calls=None))]
        ),
    ]

    class Client:
        def __init__(self, api_key, timeout=None):
            mod.instances.append(self)
            self.chat = SimpleNamespace(completions=self)
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return responses.pop(0)

    mod.OpenAI = Client
    config = {"provider": "openai", "model": "gpt", "api_key": "k"}

    convo = llm._OpenAIConversation(config, "sys", "user prompt", ADVERTISED_TOOLS, 5.0)
    turn = convo.send(None, allow_tools=True)
    assert turn.final_text is None
    assert turn.tool_calls[0].arguments == {"limit": 1}
    first = mod.instances[0].calls[0]
    assert first["tools"][0]["function"]["name"] == "neuronews-arguments__am_stats"

    results = [ToolResult("tc1", "neuronews-arguments__am_stats", {"n": 3})]
    turn2 = convo.send(results, allow_tools=False)
    assert turn2.final_text == '{"title":"T","panels":[]}'
    tool_msgs = [
        m
        for m in mod.instances[0].calls[1]["messages"]
        if isinstance(m, dict) and m.get("role") == "tool"
    ]
    assert tool_msgs and tool_msgs[0]["tool_call_id"] == "tc1"


def test_openai_bad_tool_arguments_default_to_empty(monkeypatch):
    mod = _install_fake_module(monkeypatch, "openai", None)
    tool_call = SimpleNamespace(
        id="tc1", function=SimpleNamespace(name="neuronews-arguments__am_stats", arguments="{not json")
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[tool_call]))]
    )

    class Client:
        def __init__(self, api_key, timeout=None):
            mod.instances.append(self)
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kwargs):
            return response

    mod.OpenAI = Client
    convo = llm._OpenAIConversation(
        {"provider": "openai", "model": "g", "api_key": "k"}, "s", "u", ADVERTISED_TOOLS, 5.0
    )
    turn = convo.send(None, allow_tools=True)
    assert turn.tool_calls[0].arguments == {}


def test_make_conversation_dispatches_by_provider(monkeypatch):
    mod_a = _install_fake_module(monkeypatch, "anthropic", None)
    mod_a.Anthropic = lambda api_key, timeout=None: SimpleNamespace(messages=None)
    conv = llm._make_conversation(CONFIG, "s", "u", [], 5.0)
    assert isinstance(conv, llm._AnthropicConversation)

    mod_o = _install_fake_module(monkeypatch, "openai", None)
    mod_o.OpenAI = lambda api_key, timeout=None: SimpleNamespace(chat=SimpleNamespace(completions=None))
    conv2 = llm._make_conversation(
        {"provider": "openai", "model": "g", "api_key": "k"}, "s", "u", [], 5.0
    )
    assert isinstance(conv2, llm._OpenAIConversation)
