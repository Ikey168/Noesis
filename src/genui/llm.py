"""
Optional LLM planner.

When a provider key is configured, layout planning is delegated to an LLM
that composes panels from the same catalog; its JSON output is validated
against the ui-spec-v1 contract and any failure — missing SDK, no key,
network error, malformed or invalid JSON — falls back to the heuristic
planner. Mirrors the graceful-degradation convention used across the
codebase: callers never know which planner ran.

Configuration (all optional):

* ``NOESIS_GENUI_LLM``       — ``auto`` (default) or ``off``.
* ``NOESIS_GENUI_PROVIDER``  — ``anthropic`` or ``openai``; auto-detected
  from which API key is present when unset.
* ``NOESIS_GENUI_MODEL``     — model id override.
* ``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.genui.catalog import get_panel_def, panel_catalog_dict
from src.genui.spec import (
    MAX_PANELS,
    MAX_SPAN,
    MIN_SPAN,
    UISpec,
    spec_from_dict,
    validate_spec,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-5",
    "openai": "gpt-4o-mini",
}

_MAX_OUTPUT_TOKENS = 2000

# ---------------------------------------------------------------------------
# Grounded planning loop config (R4 / Stage 2).
#
# When the MCP host is up the LLM planner runs as a bounded tool-use loop: it
# may call a small allowlist of read-only inspection tools to learn what data
# exists, then emits the final ui-spec-v1 JSON. The loop is budgeted (call
# count + wall-clock) and degrades to one-shot planning when the budget would
# be blown; every path funnels through the same sanitize/validate/signal
# enforcement, so the output contract is identical to the one-shot planner.
# ---------------------------------------------------------------------------

# Max tool-use rounds before the model is forced to answer. Each round is one
# LLM turn plus its tool executions.
MAX_TOOL_ROUNDS = 3

# Total wall-clock budget for the whole loop (env-overridable). p95 target for
# the /generate endpoint with the loop enabled; blown budget => one-shot.
DEFAULT_LOOP_BUDGET_MS = 9000
# Per-LLM-turn timeout.
LOOP_TURN_TIMEOUT = 6.0
# Below this much remaining budget we don't start the loop at all.
MIN_LOOP_BUDGET_MS = 1500

# Read-only inspection tools the loop may call, per server. Deliberately a
# curated allowlist (not "everything read-only"): grounding needs the stats
# and listing tools, never the trigger_/run_/compute_/subscribe_ RW tools.
INSPECTION_ALLOWLIST: Dict[str, frozenset] = {
    "neuronews-arguments": frozenset(
        {
            "am_stats",
            "list_claims",
            "list_stances",
            "list_frames",
            "list_drift_events",
            "list_outlet_scores",
            "list_outlet_clusters",
            "actor_summary",
            "list_actors",
            "list_unsourced_claims",
        }
    ),
    "neuronews-pipeline": frozenset(
        {
            "article_stats",
            "document_stats",
            "latest_articles",
            "sentiment_by_topic",
            "sentiment_heatmap",
            "coverage_clusters",
            "query_positions",
            "query_conflicts",
        }
    ),
    "neuronews-kg": frozenset({"kg_stats", "list_entities", "evolving_topics"}),
    "neuronews-domain-packs": frozenset({"get_ui_flags", "pack_status"}),
    "neuronews-sources": frozenset(
        {"compare_sources", "list_trustworthiness", "get_source_profile"}
    ),
}


def loop_enabled() -> bool:
    """Whether the grounded tool-use loop may run (env kill switch)."""
    return os.getenv("NOESIS_GENUI_LOOP", "auto").strip().lower() not in (
        "off",
        "0",
        "false",
    )


def _loop_budget_ms() -> int:
    raw = os.getenv("NOESIS_GENUI_LOOP_BUDGET_MS", "").strip()
    try:
        return int(raw) if raw else DEFAULT_LOOP_BUDGET_MS
    except ValueError:
        return DEFAULT_LOOP_BUDGET_MS


def llm_config() -> Optional[Dict[str, str]]:
    """Resolve provider configuration from the environment, or None."""
    if os.getenv("NOESIS_GENUI_LLM", "auto").strip().lower() in ("off", "0", "false"):
        return None
    provider = os.getenv("NOESIS_GENUI_PROVIDER", "").strip().lower()
    if not provider:
        if os.getenv("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        else:
            return None
    if provider not in _DEFAULT_MODELS:
        return None
    key_var = "ANTHROPIC_API_KEY" if provider == "anthropic" else "OPENAI_API_KEY"
    api_key = os.getenv(key_var, "").strip()
    if not api_key:
        return None
    model = os.getenv("NOESIS_GENUI_MODEL", "").strip() or _DEFAULT_MODELS[provider]
    return {"provider": provider, "model": model, "api_key": api_key}


def _build_prompt(
    intent: str,
    source_type: Optional[str],
    availability: Optional[Dict[str, bool]],
    ui_flags: Optional[Dict[str, bool]],
) -> str:
    catalog = panel_catalog_dict()
    context: Dict[str, Any] = {"panel_catalog": catalog}
    if availability is not None:
        context["table_has_data"] = availability
    if ui_flags:
        context["ui_flags"] = ui_flags
    if source_type:
        context["source_type_filter"] = source_type
    return (
        "You are the layout planner for Noesis, a news-intelligence "
        "dashboard. Compose a dashboard layout answering the analyst's "
        "intent, using ONLY panel types from the catalog below.\n\n"
        f"Context:\n{json.dumps(context, indent=2)}\n\n"
        f"Analyst intent: {intent!r}\n\n"
        "Reply with ONLY a JSON object (no prose, no code fences) shaped as:\n"
        "{\n"
        '  "title": str, "subtitle": str, "facets": [str],\n'
        '  "topic": str|null, "source_type": str|null,\n'
        '  "panels": [{"id": str, "type": str, "title": str, "span": 3-12,\n'
        '              "priority": 0-1, "rationale": str,\n'
        '              "params": {}, "body": str}]\n'
        "}\n"
        "Rules: 2-8 panels; the first panel must be type 'note' with a "
        "one-sentence 'body' explaining the layout; skip panels whose "
        "tables have no data or whose ui_flag is false; put topic / "
        "source_type / days filters in each panel's params using the "
        "catalog's parameter names."
    )


def _complete_anthropic(config: Dict[str, str], prompt: str) -> Optional[str]:
    import anthropic  # lazy: optional dependency

    client = anthropic.Anthropic(api_key=config["api_key"])
    response = client.messages.create(
        model=config["model"],
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return "".join(parts) or None


def _complete_openai(config: Dict[str, str], prompt: str) -> Optional[str]:
    import openai  # lazy: optional dependency

    client = openai.OpenAI(api_key=config["api_key"])
    response = client.chat.completions.create(
        model=config["model"],
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content or None


def _complete(config: Dict[str, str], prompt: str) -> Optional[str]:
    if config["provider"] == "anthropic":
        return _complete_anthropic(config, prompt)
    return _complete_openai(config, prompt)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Parse the first JSON object in a completion, tolerating fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _sanitize(spec: UISpec, intent: str) -> UISpec:
    """Clamp and repair LLM output so honest near-misses still validate."""
    spec.intent = intent[:500]
    spec.generated_by = "llm"
    spec.spec_version = "ui-spec-v1"
    if not spec.title:
        spec.title = "Adaptive Canvas"
    repaired = []
    seen = set()
    for panel in spec.panels[:MAX_PANELS]:
        pdef = get_panel_def(panel.type)
        if pdef is None:
            continue
        panel.endpoint = pdef.endpoint
        panel.span = max(MIN_SPAN, min(MAX_SPAN, panel.span))
        panel.priority = max(0.0, min(1.0, panel.priority))
        if not panel.title:
            panel.title = pdef.title
        # Params flow straight into frontend hooks — keep scalars only.
        panel.params = {
            k: v
            for k, v in panel.params.items()
            if isinstance(k, str) and isinstance(v, (str, int, float, bool))
        }
        if not panel.id or panel.id in seen:
            n = len(repaired) + 1
            while f"p{n}" in seen:
                n += 1
            panel.id = f"p{n}"
        seen.add(panel.id)
        repaired.append(panel)
    spec.panels = repaired
    return spec


def _apply_usage_signals(spec: UISpec, signals: Optional[Dict[str, Any]]) -> None:
    """Fold client usage signals into an LLM layout, mirroring plan().

    The LLM never sees pins/mutes; they are hard preferences, so enforce
    them here: muted types drop out, pinned types appear and get boosted,
    weights re-rank. The note panel stays first.
    """
    from src.genui.adaptivity import apply_signals, normalize_signals

    normalized = normalize_signals(signals)
    note = [p for p in spec.panels if p.type == "note"][:1]
    rest = [p for p in spec.panels if p.type != "note"]
    rest, _ = apply_signals(rest, normalized)
    for panel in rest:
        if panel.endpoint is None:
            pdef = get_panel_def(panel.type)
            if pdef is not None:
                panel.endpoint = pdef.endpoint
    spec.panels = (note + rest)[:MAX_PANELS]
    for i, panel in enumerate(spec.panels):
        panel.id = f"p{i + 1}"


# ---------------------------------------------------------------------------
# Bounded tool-use loop (R4 #592).
# ---------------------------------------------------------------------------


@dataclass
class ToolInvocation:
    """One tool call the model asked for, resolved to a server+tool."""

    call_id: str
    advertised_name: str
    arguments: Dict[str, Any]


@dataclass
class ToolResult:
    """The outcome of a tool call, fed back to the model."""

    call_id: str
    advertised_name: str
    content: Any
    is_error: bool = False


@dataclass
class LLMTurn:
    """One model response: either a final answer or a batch of tool calls."""

    final_text: Optional[str] = None
    tool_calls: List[ToolInvocation] = field(default_factory=list)


def _advertise_tools(host) -> Tuple[List[Dict[str, Any]], Dict[str, Tuple[str, str]]]:
    """Build the provider tool list from the host's cached discovery,
    filtered to the read-only allowlist. Returns (tool specs, name map
    advertised_name -> (server, tool)). Advertised names are ``server__tool``
    (a valid provider tool name; hyphens and underscores are allowed)."""
    specs: List[Dict[str, Any]] = []
    name_map: Dict[str, Tuple[str, str]] = {}
    try:
        by_server = host.tools()
    except Exception:
        return specs, name_map
    for server, tools in sorted(by_server.items()):
        allowed = INSPECTION_ALLOWLIST.get(server, frozenset())
        for tool in tools:
            name = tool.get("name")
            if name not in allowed:
                continue
            advertised = f"{server}__{name}"
            name_map[advertised] = (server, name)
            specs.append(
                {
                    "name": advertised,
                    "description": tool.get("description", "")[:400],
                    "input_schema": tool.get("input_schema") or {"type": "object"},
                }
            )
    return specs, name_map


def _execute_tool_calls(
    host, calls: List[ToolInvocation], name_map: Dict[str, Tuple[str, str]]
) -> List[ToolResult]:
    """Run each requested tool through the shared cache, enforcing the
    allowlist. A rejected or failed call becomes an error result the model
    can react to — it never aborts the loop."""
    results: List[ToolResult] = []
    for call in calls:
        target = name_map.get(call.advertised_name)
        if target is None:
            results.append(
                ToolResult(
                    call.call_id,
                    call.advertised_name,
                    {"error": f"tool {call.advertised_name!r} is not permitted"},
                    is_error=True,
                )
            )
            continue
        server, tool = target
        try:
            content = host.call_tool_cached(server, tool, call.arguments)
            results.append(
                ToolResult(
                    call.call_id,
                    call.advertised_name,
                    content,
                    is_error=isinstance(content, dict) and "error" in content,
                )
            )
        except Exception as err:
            results.append(
                ToolResult(
                    call.call_id,
                    call.advertised_name,
                    {"error": f"{type(err).__name__}: {err}"},
                    is_error=True,
                )
            )
    return results


def _loop_system_prompt() -> str:
    return (
        "You are the layout planner for Noesis, a knowledge-intelligence "
        "dashboard. You may call the provided read-only inspection tools to "
        "check what data actually exists (row counts, available topics, "
        "outlets) before composing the layout. Call tools only when it "
        "changes the layout; a couple of calls is plenty. When you have "
        "enough signal, STOP calling tools and reply with ONLY the final "
        "JSON layout object (no prose, no code fences). Omit panels whose "
        "underlying data is empty."
    )


class Conversation:
    """Provider-neutral multi-turn tool-use conversation.

    ``send(results, allow_tools)`` advances the exchange: it feeds back the
    previous turn's tool results (None on the first call) and returns the
    model's next :class:`LLMTurn`. When ``allow_tools`` is False the model is
    offered no tools, forcing a final text answer (the budget-exhaustion
    degrade path).
    """

    def send(
        self, results: Optional[List[ToolResult]], allow_tools: bool
    ) -> LLMTurn:  # pragma: no cover - interface
        raise NotImplementedError


class _AnthropicConversation(Conversation):
    def __init__(self, config, system, user_prompt, tools, timeout):
        import anthropic  # lazy: optional dependency

        self._client = anthropic.Anthropic(api_key=config["api_key"], timeout=timeout)
        self._model = config["model"]
        self._system = system
        self._tools = tools
        self._messages: List[Dict[str, Any]] = [
            {"role": "user", "content": user_prompt}
        ]

    def send(self, results, allow_tools):
        if results is not None:
            self._messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.call_id,
                            "content": json.dumps(r.content, default=str)[:4000],
                            "is_error": r.is_error,
                        }
                        for r in results
                    ],
                }
            )
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "system": self._system,
            "messages": self._messages,
        }
        if allow_tools and self._tools:
            kwargs["tools"] = self._tools
        response = self._client.messages.create(**kwargs)
        self._messages.append({"role": "assistant", "content": response.content})
        text_parts, calls = [], []
        for block in response.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                calls.append(
                    ToolInvocation(block.id, block.name, dict(block.input or {}))
                )
        if calls:
            return LLMTurn(tool_calls=calls)
        return LLMTurn(final_text="".join(text_parts) or None)


class _OpenAIConversation(Conversation):
    def __init__(self, config, system, user_prompt, tools, timeout):
        import openai  # lazy: optional dependency

        self._client = openai.OpenAI(api_key=config["api_key"], timeout=timeout)
        self._model = config["model"]
        self._tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        self._messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_prompt},
        ]

    def send(self, results, allow_tools):
        if results is not None:
            for r in results:
                self._messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": r.call_id,
                        "content": json.dumps(r.content, default=str)[:4000],
                    }
                )
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": _MAX_OUTPUT_TOKENS,
            "messages": self._messages,
        }
        if allow_tools and self._tools:
            kwargs["tools"] = self._tools
        response = self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        self._messages.append(message)
        calls = []
        for tc in getattr(message, "tool_calls", None) or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            calls.append(ToolInvocation(tc.id, tc.function.name, args))
        if calls:
            return LLMTurn(tool_calls=calls)
        return LLMTurn(final_text=message.content or None)


def _make_conversation(config, system, user_prompt, tools, timeout) -> Conversation:
    """Build the provider conversation. Monkeypatched in tests to inject a
    scripted driver, so the loop logic is exercised without any SDK."""
    if config["provider"] == "anthropic":
        return _AnthropicConversation(config, system, user_prompt, tools, timeout)
    return _OpenAIConversation(config, system, user_prompt, tools, timeout)


def _run_grounded_loop(
    config: Dict[str, str],
    intent: str,
    source_type: Optional[str],
    availability: Optional[Dict[str, bool]],
    ui_flags: Optional[Dict[str, bool]],
    host,
    budget_ms: int,
) -> Optional[str]:
    """Drive the bounded tool-use loop; return the final layout JSON text, or
    None to signal the caller should fall back to the heuristic planner."""
    tools, name_map = _advertise_tools(host)
    user_prompt = _build_prompt(intent, source_type, availability, ui_flags)
    convo = _make_conversation(
        config, _loop_system_prompt(), user_prompt, tools, LOOP_TURN_TIMEOUT
    )

    deadline = time.monotonic() + budget_ms / 1000.0
    results: Optional[List[ToolResult]] = None
    for step in range(MAX_TOOL_ROUNDS + 1):
        # Force a final answer when out of rounds or out of time.
        force_final = step == MAX_TOOL_ROUNDS or time.monotonic() >= deadline
        try:
            turn = convo.send(results, allow_tools=not force_final)
        except Exception:
            logger.warning("genui planning loop turn failed", exc_info=True)
            return None
        if turn.final_text is not None:
            return turn.final_text
        if not turn.tool_calls or force_final:
            # No usable answer within budget: degrade to the heuristic planner.
            return None
        results = _execute_tool_calls(host, turn.tool_calls, name_map)
    return None


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def _finalize(parsed: Dict[str, Any], intent: str, signals) -> Optional[UISpec]:
    """Sanitize -> enforce usage signals -> validate. None on any failure."""
    try:
        spec = _sanitize(spec_from_dict(parsed), intent)
        _apply_usage_signals(spec, signals)
        errors = validate_spec(spec.to_dict())
    except Exception:
        logger.warning("genui LLM spec could not be repaired; using heuristic planner", exc_info=True)
        return None
    if errors:
        logger.warning("genui LLM spec failed validation: %s", "; ".join(errors[:5]))
        return None
    return spec


def _one_shot(config, intent, source_type, availability, ui_flags) -> Optional[str]:
    """Single completion, no tools — the pre-R4 path and the degrade target."""
    try:
        return _complete(config, _build_prompt(intent, source_type, availability, ui_flags))
    except Exception:
        logger.warning("genui LLM completion failed; using heuristic planner", exc_info=True)
        return None


def plan_with_llm(
    intent: str,
    source_type: Optional[str] = None,
    availability: Optional[Dict[str, bool]] = None,
    ui_flags: Optional[Dict[str, bool]] = None,
    signals: Optional[Dict[str, Any]] = None,
) -> Optional[UISpec]:
    """Plan a layout with the configured LLM; None on any failure.

    With the MCP host up and the loop enabled, runs the bounded grounded
    tool-use loop (R4); otherwise, or when the latency budget is too small,
    falls back to a single one-shot completion (the pre-R4 behavior). Both
    paths funnel through the same sanitize/validate/signal enforcement.
    """
    config = llm_config()
    if config is None:
        return None

    text: Optional[str] = None
    host = _planning_host()
    budget_ms = _loop_budget_ms()
    if (
        loop_enabled()
        and host is not None
        and budget_ms >= MIN_LOOP_BUDGET_MS
        and _advertise_tools(host)[0]
    ):
        text = _run_grounded_loop(
            config, intent, source_type, availability, ui_flags, host, budget_ms
        )
    if text is None:
        text = _one_shot(config, intent, source_type, availability, ui_flags)
    if not text:
        return None

    parsed = _extract_json(text)
    if parsed is None:
        logger.warning("genui LLM returned unparseable output; using heuristic planner")
        return None
    return _finalize(parsed, intent, signals)


def _planning_host():
    """The MCP host to ground planning against, or None. Isolated so tests
    can stub it without touching the singleton."""
    try:
        from src.mcp_host import get_host

        return get_host()
    except Exception:
        return None
