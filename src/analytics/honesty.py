"""
The statistical-honesty convention (MCP rearchitecture plan, R5 / Track DS).

Every analytic tool on the canvas reports *how much data* it saw, *what
method* produced the number, and *what assumptions* that method makes — and
never ships a headline number without an interval around it. This module is
the shared contract:

* :func:`analytic_envelope` builds the required-fields wrapper
  (``n`` / ``method`` / ``assumptions``) that every analytic output carries.
* :func:`interval` builds an ``{value, lo, hi, level}`` interval; a bare
  point estimate is a contract violation for any *headline* figure.
* :func:`validate_analytic_output` is the gate the contract tests (and any
  caller) use: it rejects outputs missing their uncertainty fields.
* :func:`honesty_output_schema` merges the required fields into a tool's MCP
  ``outputSchema`` so the shape is advertised through discovery.

Stdlib-only on purpose: the MCP tool servers import this at module load, so
it must never pull a heavy dependency.
"""

from __future__ import annotations

from numbers import Real
from typing import Any, Dict, Iterable, List, Optional

# The three fields every analytic output must carry at the top level.
REQUIRED_FIELDS = ("n", "method", "assumptions")

DEFAULT_LEVEL = 0.95


def interval(
    value: float, lo: float, hi: float, level: float = DEFAULT_LEVEL
) -> Dict[str, float]:
    """An estimate with an uncertainty interval. ``lo <= value <= hi`` and
    ``0 < level <= 1`` are enforced by :func:`is_interval`; this builder
    orders lo/hi defensively so callers can pass them either way."""
    lo, hi = (lo, hi) if lo <= hi else (hi, lo)
    return {"value": float(value), "lo": float(lo), "hi": float(hi), "level": float(level)}


def is_interval(obj: Any) -> bool:
    """True when ``obj`` is a well-formed interval dict."""
    if not isinstance(obj, dict):
        return False
    keys = ("value", "lo", "hi", "level")
    if any(k not in obj for k in keys):
        return False
    if any(isinstance(obj[k], bool) or not isinstance(obj[k], Real) for k in keys):
        return False
    if not (0.0 < obj["level"] <= 1.0):
        return False
    return obj["lo"] <= obj["value"] <= obj["hi"]


def analytic_envelope(
    n: int,
    method: str,
    assumptions: Iterable[str],
    **fields: Any,
) -> Dict[str, Any]:
    """Wrap analytic results with the mandatory honesty fields.

    Args:
        n: sample size the analysis ran on (documents, windows, resamples…).
        method: the technique, human-readable ("robust z-score (median/MAD)").
        assumptions: the caveats the method carries.
        **fields: the analytic's own result payload (rows, intervals, …).
    """
    return {
        "n": int(n),
        "method": str(method),
        "assumptions": [str(a) for a in assumptions],
        **fields,
    }


def validate_analytic_output(
    payload: Any,
    *,
    interval_fields: Iterable[str] = (),
) -> List[str]:
    """Return a list of contract violations ("" == valid).

    Enforces the honesty contract: ``n`` is a non-negative int, ``method`` is
    a non-empty string, ``assumptions`` is a list of strings, and every field
    named in ``interval_fields`` (a headline estimate) is interval-shaped.
    Callers with an ``error`` payload are exempt — a tool reporting an error
    is allowed to skip the fields.
    """
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["output is not an object"]
    if "error" in payload:
        return []  # an error response is not an analytic result

    n = payload.get("n")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        errors.append("missing or invalid 'n' (non-negative int required)")
    method = payload.get("method")
    if not isinstance(method, str) or not method.strip():
        errors.append("missing or empty 'method'")
    assumptions = payload.get("assumptions")
    if not isinstance(assumptions, list) or not all(
        isinstance(a, str) for a in assumptions
    ):
        errors.append("'assumptions' must be a list of strings")

    for field in interval_fields:
        if field not in payload:
            errors.append(f"missing interval field {field!r}")
        elif not is_interval(payload[field]):
            errors.append(f"field {field!r} is not a well-formed interval")
    return errors


# JSON Schema fragment for the honesty fields, reused in every tool's
# outputSchema (advertised through R2 discovery).
_HONESTY_SCHEMA_FIELDS: Dict[str, Any] = {
    "n": {"type": "integer", "minimum": 0, "description": "sample size the analysis ran on"},
    "method": {"type": "string", "description": "the analytical technique used"},
    "assumptions": {
        "type": "array",
        "items": {"type": "string"},
        "description": "assumptions/caveats the method carries",
    },
}

INTERVAL_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "point estimate with an uncertainty interval",
    "properties": {
        "value": {"type": "number"},
        "lo": {"type": "number"},
        "hi": {"type": "number"},
        "level": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["value", "lo", "hi", "level"],
}


def honesty_output_schema(
    properties: Optional[Dict[str, Any]] = None,
    required: Iterable[str] = (),
) -> Dict[str, Any]:
    """Build an MCP ``outputSchema`` that requires the honesty fields plus the
    caller's own ``properties``. Used as ``output_schema=`` on analytic
    tools so the contract is advertised through discovery."""
    props = {**_HONESTY_SCHEMA_FIELDS, **(properties or {})}
    return {
        "type": "object",
        "properties": props,
        "required": [*REQUIRED_FIELDS, *required],
        "additionalProperties": True,
    }
