"""
Agent host runtime over the MCP surface (M10.1).

An agent drives Noesis by calling tools across three planes:

* **provisioning** (``neuronews-provisioning``) - deploy/attach/ingest/teardown a
  namespaced KG,
* **osint** (``neuronews-osint``) - corroboration, dossiers, relationship paths,
  timelines, provenance traces,
* **genui** (``noesis``) - turn a goal into a canvas (a ``ui-spec-v1`` layout).

This runtime is the disciplined gate between an agent and those planes. Reusing
the R4 tool-loop discipline (a per-server allowlist plus a call budget), it:

* refuses any tool not on its plane's allowlist,
* refuses a **gated** OSINT tool while the review gate is off, so an agent can
  never invoke ``geolocate_claims`` / ``narrative_coordination`` unless a human
  has opened the gate (M10.3 / the R11 review gate),
* enforces a step budget (total, and optionally per plane), so a runaway agent
  is bounded, and
* records every call to a transcript and an optional audit sink, so the whole
  run is reconstructable (M10.4 wires this to the provisioning audit trail).

The tool caller is injected: in production it wraps the live MCP host; tests and
the agents inject a dispatcher. Stdlib-only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

PLANE_PROVISIONING = "provisioning"
PLANE_OSINT = "osint"
PLANE_GENUI = "genui"

# The MCP server backing each plane.
_PLANE_SERVERS = {
    PLANE_PROVISIONING: "neuronews-provisioning",
    PLANE_OSINT: "neuronews-osint",
    PLANE_GENUI: "noesis",
}

_PROVISIONING_TOOLS = frozenset({
    "kg_deploy", "kg_attach_sources", "kg_attach_pipeline", "kg_ingest",
    "kg_status", "kg_list", "kg_view", "kg_lineage", "kg_teardown",
})
_OSINT_TOOLS = frozenset({
    "corroborate", "source_reliability", "contradiction_scan", "entity_dossier",
    "relationship_path", "timeline_reconstruct", "trace_artifact", "investigation_audit",
})
# The review-gated OSINT tools: allowlisted only when the gate is open.
_OSINT_GATED_TOOLS = frozenset({"geolocate_claims", "narrative_coordination"})
_GENUI_TOOLS = frozenset({"noesis_generate_view", "noesis_panels"})


def gated_tools_enabled() -> bool:
    """Whether the OSINT review gate is open (``NOESIS_OSINT_GATED_TOOLS``)."""
    return os.getenv("NOESIS_OSINT_GATED_TOOLS", "off").lower() in ("on", "1", "true")


def default_planes() -> Dict[str, Dict[str, Any]]:
    """The three planes, with per-plane server and tool allowlist. The OSINT
    plane admits the gated tools only while the review gate is open."""
    osint_tools = _OSINT_TOOLS | (_OSINT_GATED_TOOLS if gated_tools_enabled() else frozenset())
    return {
        PLANE_PROVISIONING: {"server": _PLANE_SERVERS[PLANE_PROVISIONING], "tools": _PROVISIONING_TOOLS},
        PLANE_OSINT: {"server": _PLANE_SERVERS[PLANE_OSINT], "tools": osint_tools, "gated": _OSINT_GATED_TOOLS},
        PLANE_GENUI: {"server": _PLANE_SERVERS[PLANE_GENUI], "tools": _GENUI_TOOLS},
    }


@dataclass
class Budget:
    """The agent's tool-use budget: a total step cap and optional per-plane caps."""

    max_steps: int = 24
    max_per_plane: Optional[Dict[str, int]] = None


@dataclass
class ToolCall:
    """One recorded plane call in the run transcript."""

    step: int
    plane: str
    server: str
    tool: str
    arguments: Dict[str, Any]
    ok: bool
    error: Optional[str] = None
    result: Any = None

    def summary(self) -> Dict[str, Any]:
        """A JSON-ready record for the audit trail (no bulky result payload)."""
        return {
            "step": self.step,
            "plane": self.plane,
            "server": self.server,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "error": self.error,
        }


class BudgetExceeded(RuntimeError):
    """The agent's step budget (total or per-plane) is exhausted."""


class NotAllowed(RuntimeError):
    """A call was refused: unknown plane, un-allowlisted tool, or a gated tool
    while the review gate is closed."""


class AgentRuntime:
    """A budgeted, allowlisted, audited gate between an agent and the MCP planes."""

    def __init__(
        self,
        caller: Callable[[str, str, Dict[str, Any]], Any],
        budget: Optional[Budget] = None,
        planes: Optional[Dict[str, Dict[str, Any]]] = None,
        audit_sink: Optional[Callable[[ToolCall], None]] = None,
    ):
        self._caller = caller
        self._budget = budget or Budget()
        self._planes = planes or default_planes()
        self._audit_sink = audit_sink
        self._transcript: List[ToolCall] = []
        self._plane_counts: Dict[str, int] = {}

    # -- introspection ---------------------------------------------------------

    @property
    def steps_used(self) -> int:
        return len(self._transcript)

    def steps_remaining(self) -> int:
        return max(0, self._budget.max_steps - self.steps_used)

    def plane_calls(self, plane: str) -> int:
        return self._plane_counts.get(plane, 0)

    def transcript(self) -> List[ToolCall]:
        return list(self._transcript)

    def planes(self) -> List[str]:
        return sorted(self._planes.keys())

    # -- the one call gate -----------------------------------------------------

    def call(self, plane: str, tool: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Invoke ``tool`` on ``plane`` through the runtime. Enforces the plane
        allowlist, the review gate, and the step budget; records the call.

        Policy violations (unknown plane, un-allowlisted or gated tool, budget
        exhausted) raise. A tool that *runs* but errors is recorded with
        ``ok=False`` and its error dict is returned, so an agent can react
        without the run aborting."""
        arguments = dict(arguments or {})
        plane_def = self._planes.get(plane)
        if plane_def is None:
            raise NotAllowed(f"unknown plane {plane!r}")
        if tool not in plane_def["tools"]:
            if tool in plane_def.get("gated", frozenset()):
                raise NotAllowed(
                    f"gated tool {tool!r} is disabled while the review gate is closed"
                )
            raise NotAllowed(f"tool {tool!r} is not allowlisted on the {plane!r} plane")
        if self.steps_used >= self._budget.max_steps:
            raise BudgetExceeded(f"step budget of {self._budget.max_steps} exhausted")
        cap = (self._budget.max_per_plane or {}).get(plane)
        if cap is not None and self.plane_calls(plane) >= cap:
            raise BudgetExceeded(f"per-plane budget for {plane!r} ({cap}) exhausted")

        server = plane_def["server"]
        step = self.steps_used + 1
        try:
            result = self._caller(server, tool, arguments)
            record = ToolCall(step, plane, server, tool, arguments, ok=True, result=result)
        except Exception as err:  # a tool that ran and failed: recorded, not raised
            result = {"error": str(err)}
            record = ToolCall(step, plane, server, tool, arguments, ok=False, error=str(err), result=result)

        self._transcript.append(record)
        self._plane_counts[plane] = self.plane_calls(plane) + 1
        if self._audit_sink is not None:
            try:
                self._audit_sink(record)
            except Exception:
                pass
        return result


def live_caller() -> Callable[[str, str, Dict[str, Any]], Any]:
    """A tool caller backed by the live MCP host. Raises if the host is down, so
    an agent fails loudly rather than silently no-op'ing."""
    from src.mcp_host import get_host

    def _call(server: str, tool: str, arguments: Dict[str, Any]) -> Any:
        host = get_host()
        if host is None:
            raise RuntimeError("MCP host is not running")
        return host.call_tool(server, tool, arguments)

    return _call


__all__ = [
    "PLANE_PROVISIONING",
    "PLANE_OSINT",
    "PLANE_GENUI",
    "Budget",
    "ToolCall",
    "BudgetExceeded",
    "NotAllowed",
    "AgentRuntime",
    "default_planes",
    "gated_tools_enabled",
    "live_caller",
]
