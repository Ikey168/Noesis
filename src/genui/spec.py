"""
The ``ui-spec-v1`` document model.

A UISpec is the wire format between the planner (heuristic or LLM) and the
frontend spec renderer. The JSON-schema contract lives at
``contracts/schemas/jsonschema/ui-spec-v1.json``; :func:`validate_spec`
implements the same rules in pure Python so validation never depends on an
optional package (mirroring the double-validation convention used by the
ask endpoint, minus the jsonschema import).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.genui.catalog import FACETS, PANEL_TYPES, get_panel_def

SPEC_VERSION = "ui-spec-v1"

GENERATORS = ("heuristic", "llm", "client")

SOURCE_TYPES = ("news", "blog", "paper", "book", "transcript", "web", "note")

MIN_SPAN = 3
MAX_SPAN = 12
MAX_PANELS = 12
MAX_INTENT_LENGTH = 500


@dataclass
class PanelSpec:
    """One renderable panel in a generated layout."""

    id: str
    type: str
    title: str
    span: int = 6
    priority: float = 0.5
    rationale: str = ""
    endpoint: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    body: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "span": self.span,
            "priority": round(self.priority, 4),
            "rationale": self.rationale,
            "endpoint": self.endpoint,
            "params": self.params,
            "body": self.body,
        }


@dataclass
class UISpec:
    """A full generated layout."""

    intent: str
    title: str
    subtitle: str = ""
    generated_by: str = "heuristic"
    facets: List[str] = field(default_factory=list)
    topic: Optional[str] = None
    source_type: Optional[str] = None
    panels: List[PanelSpec] = field(default_factory=list)
    spec_version: str = SPEC_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec_version": self.spec_version,
            "intent": self.intent,
            "title": self.title,
            "subtitle": self.subtitle,
            "generated_by": self.generated_by,
            "facets": self.facets,
            "topic": self.topic,
            "source_type": self.source_type,
            "panels": [p.to_dict() for p in self.panels],
        }


def _as_int(value: Any, default: int) -> int:
    """Coerce untrusted (LLM-produced) input to an int, never raising."""
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result


def _as_float(value: Any, default: float) -> float:
    """Coerce untrusted (LLM-produced) input to a finite float, never raising."""
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if result != result or result in (float("inf"), float("-inf")):
        return default
    return result


def spec_from_dict(data: Dict[str, Any]) -> UISpec:
    """Build a UISpec from a plain dict (e.g. parsed LLM output).

    Performs light coercion only — it must never raise on malformed input;
    call :func:`validate_spec` on the result's ``to_dict()`` to enforce the
    contract.
    """
    panels = []
    for i, p in enumerate(data.get("panels") or []):
        if not isinstance(p, dict):
            continue
        panels.append(
            PanelSpec(
                id=str(p.get("id") or f"p{i + 1}"),
                type=str(p.get("type") or ""),
                title=str(p.get("title") or ""),
                span=_as_int(p.get("span") or 6, 6),
                priority=_as_float(p.get("priority") or 0.5, 0.5),
                rationale=str(p.get("rationale") or ""),
                endpoint=p.get("endpoint"),
                params=p.get("params") if isinstance(p.get("params"), dict) else {},
                body=str(p.get("body") or ""),
            )
        )
    topic = data.get("topic")
    source_type = data.get("source_type")
    return UISpec(
        intent=str(data.get("intent") or ""),
        title=str(data.get("title") or ""),
        subtitle=str(data.get("subtitle") or ""),
        generated_by=str(data.get("generated_by") or "heuristic"),
        facets=[str(f) for f in (data.get("facets") or [])],
        topic=topic if isinstance(topic, str) else None,
        source_type=source_type if isinstance(source_type, str) else None,
        panels=panels,
        spec_version=str(data.get("spec_version") or SPEC_VERSION),
    )


def validate_spec(data: Dict[str, Any]) -> List[str]:
    """Validate a spec dict against the ui-spec-v1 contract.

    Returns a list of human-readable errors; empty means valid.
    """
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["spec must be an object"]

    if data.get("spec_version") != SPEC_VERSION:
        errors.append(f"spec_version must be '{SPEC_VERSION}'")

    intent = data.get("intent")
    if not isinstance(intent, str):
        errors.append("intent must be a string")
    elif len(intent) > MAX_INTENT_LENGTH:
        errors.append(f"intent must be at most {MAX_INTENT_LENGTH} characters")

    if not isinstance(data.get("title"), str) or not data.get("title"):
        errors.append("title must be a non-empty string")

    if data.get("generated_by") not in GENERATORS:
        errors.append(f"generated_by must be one of {GENERATORS}")

    facets = data.get("facets", [])
    if not isinstance(facets, list) or any(f not in FACETS for f in facets):
        errors.append(f"facets must be a list drawn from {FACETS}")

    topic = data.get("topic")
    if topic is not None and not isinstance(topic, str):
        errors.append("topic must be null or a string")

    source_type = data.get("source_type")
    if source_type is not None and source_type not in SOURCE_TYPES:
        errors.append(f"source_type must be null or one of {SOURCE_TYPES}")

    panels = data.get("panels")
    if not isinstance(panels, list) or not panels:
        errors.append("panels must be a non-empty list")
        return errors
    if len(panels) > MAX_PANELS:
        errors.append(f"panels must contain at most {MAX_PANELS} entries")

    seen_ids = set()
    for i, panel in enumerate(panels):
        where = f"panels[{i}]"
        if not isinstance(panel, dict):
            errors.append(f"{where} must be an object")
            continue
        pid = panel.get("id")
        if not isinstance(pid, str) or not pid:
            errors.append(f"{where}.id must be a non-empty string")
        elif pid in seen_ids:
            errors.append(f"{where}.id '{pid}' is duplicated")
        else:
            seen_ids.add(pid)

        ptype = panel.get("type")
        if ptype not in PANEL_TYPES:
            errors.append(f"{where}.type '{ptype}' is not in the panel catalog")

        if not isinstance(panel.get("title"), str) or not panel.get("title"):
            errors.append(f"{where}.title must be a non-empty string")

        span = panel.get("span")
        if not isinstance(span, int) or not MIN_SPAN <= span <= MAX_SPAN:
            errors.append(f"{where}.span must be an integer in [{MIN_SPAN}, {MAX_SPAN}]")

        priority = panel.get("priority")
        if not isinstance(priority, (int, float)) or not 0 <= float(priority) <= 1:
            errors.append(f"{where}.priority must be a number in [0, 1]")

        params = panel.get("params", {})
        if not isinstance(params, dict):
            errors.append(f"{where}.params must be an object")
        else:
            # Params travel straight into frontend hooks and query strings;
            # only scalars are renderable.
            for key, value in params.items():
                if not isinstance(value, (str, int, float, bool)):
                    errors.append(f"{where}.params['{key}'] must be a scalar")

        endpoint = panel.get("endpoint")
        if endpoint is not None and not isinstance(endpoint, str):
            errors.append(f"{where}.endpoint must be null or a string")
        pdef = get_panel_def(ptype) if isinstance(ptype, str) else None
        if pdef is not None and isinstance(endpoint, str) and endpoint != pdef.endpoint:
            errors.append(
                f"{where}.endpoint '{endpoint}' does not match the catalog "
                f"endpoint for '{ptype}'"
            )

    return errors
