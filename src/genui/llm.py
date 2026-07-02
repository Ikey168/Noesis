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
from typing import Any, Dict, List, Optional

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


def plan_with_llm(
    intent: str,
    source_type: Optional[str] = None,
    availability: Optional[Dict[str, bool]] = None,
    ui_flags: Optional[Dict[str, bool]] = None,
    signals: Optional[Dict[str, Any]] = None,
) -> Optional[UISpec]:
    """Plan a layout with the configured LLM; None on any failure."""
    config = llm_config()
    if config is None:
        return None
    try:
        text = _complete(config, _build_prompt(intent, source_type, availability, ui_flags))
    except Exception:
        logger.warning("genui LLM completion failed; using heuristic planner", exc_info=True)
        return None
    if not text:
        return None
    parsed = _extract_json(text)
    if parsed is None:
        logger.warning("genui LLM returned unparseable output; using heuristic planner")
        return None
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
