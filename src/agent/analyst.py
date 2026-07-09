"""
Analyst agent (M10.2).

Given a goal, the analyst drives the full Noesis loop end to end, entirely
through the agent runtime (M10.1) and thus over the MCP surface:

1. **KG** - select an existing provisioned KG for the goal, or provision one
   (deploy, attach sources, ingest) on the **provisioning** plane;
2. **OSINT** - run a composition sweep on the **osint** plane (contradiction
   scan, plus corroboration / dossier / reliability when the goal names a claim,
   entity or source).

The agent only ever calls ``runtime.call(plane, tool, args)``, so every step is
allowlisted, budgeted and audited by the runtime, and the agent is identical
whether it runs over live MCP or the in-process backend. Stdlib-only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.agent.runtime import (
    AgentRuntime,
    PLANE_OSINT,
    PLANE_PROVISIONING,
)


def kg_name_for(goal: str) -> str:
    """A valid KG name derived from a goal ([a-z][a-z0-9_]{1,30})."""
    slug = re.sub(r"[^a-z0-9]+", "_", goal.lower()).strip("_")
    if not slug or not slug[0].isalpha():
        slug = "kg_" + slug
    slug = slug[:30].rstrip("_")
    return slug or "kg_default"


@dataclass
class AnalystResult:
    """The end-to-end outcome of an analyst run."""

    goal: str
    kg: Dict[str, Any]
    osint: List[Dict[str, Any]] = field(default_factory=list)
    steps: int = 0

    @property
    def findings(self) -> int:
        """How many OSINT tools returned a usable (non-error) result."""
        return sum(1 for f in self.osint if f.get("ok"))


class AnalystAgent:
    """Drives goal -> KG -> OSINT over the runtime."""

    def __init__(self, runtime: AgentRuntime):
        self._rt = runtime

    def run(
        self,
        goal: str,
        *,
        kg_name: Optional[str] = None,
        sources: Optional[List[str]] = None,
        topic: Optional[str] = None,
        entity: Optional[str] = None,
        claim_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> AnalystResult:
        # 1) Select or provision a KG.
        name = kg_name or kg_name_for(goal)
        listing = self._rt.call(PLANE_PROVISIONING, "kg_list", {})
        existing = {k.get("name") for k in (listing.get("kgs") or [])}
        provisioned = False
        if name not in existing:
            self._rt.call(
                PLANE_PROVISIONING, "kg_deploy",
                {"name": name, "description": goal, "approve": True},
            )
            if sources:
                self._rt.call(
                    PLANE_PROVISIONING, "kg_attach_sources",
                    {"name": name, "sources": sources},
                )
            self._rt.call(PLANE_PROVISIONING, "kg_ingest", {"name": name})
            provisioned = True
        status = self._rt.call(PLANE_PROVISIONING, "kg_status", {"name": name})

        # 2) OSINT composition sweep. contradiction_scan always; the targeted
        #    tools when the goal supplies a subject.
        osint: List[Dict[str, Any]] = []
        osint.append(self._osint("contradiction_scan", {"topic": topic or goal}))
        if claim_id:
            osint.append(self._osint("corroborate", {"claim_id": claim_id}))
        if entity:
            osint.append(self._osint("entity_dossier", {"entity": entity}))
        if source:
            osint.append(self._osint("source_reliability", {"source": source}))

        return AnalystResult(
            goal=goal,
            kg={"name": name, "provisioned": provisioned, "status": status},
            osint=osint,
            steps=self._rt.steps_used,
        )

    def _osint(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = self._rt.call(PLANE_OSINT, tool, args)
        ok = not (isinstance(result, dict) and result.get("error"))
        return {"tool": tool, "ok": ok, "result": result}


__all__ = ["AnalystAgent", "AnalystResult", "kg_name_for"]
