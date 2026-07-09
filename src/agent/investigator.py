"""
Investigator agent (M10.3).

A variant of the analyst tuned for investigations. It opens an investigation - a
provisioned KG namespace (R11) - and drives the R11 investigation surface over
the runtime:

* **entity_dossier** - a cited brief per entity,
* **relationship_path** - how two entities are connected, edge by cited edge,
* **timeline_reconstruct** - a dated, corroboration-weighted event sequence,
* **trace_artifact** - the provenance chain behind a claim.

Because it runs entirely through the
runtime, it inherits the review gate: it only ever calls the ungated R11 tools,
and the runtime would refuse a gated tool (``geolocate_claims`` /
``narrative_coordination``) while the gate is closed. The run is auditable via
``investigation_audit`` on the same namespace. Stdlib-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.agent.analyst import kg_name_for
from src.agent.runtime import (
    AgentRuntime,
    PLANE_OSINT,
    PLANE_PROVISIONING,
)

# The gated OSINT tools an investigator must never invoke while the gate is off.
GATED_TOOLS = frozenset({"geolocate_claims", "narrative_coordination"})


@dataclass
class InvestigationResult:
    """The outcome of an investigator run."""

    title: str
    kg: Dict[str, Any]
    surface: List[Dict[str, Any]] = field(default_factory=list)
    audit: Optional[Dict[str, Any]] = None
    steps: int = 0
    gated_calls: int = 0

    @property
    def findings(self) -> int:
        return sum(1 for f in self.surface if f.get("ok"))


class InvestigatorAgent:
    """Drives open-investigation -> R11 surface over the runtime,
    respecting the review gate."""

    def __init__(self, runtime: AgentRuntime):
        self._rt = runtime

    def run(
        self,
        title: str,
        *,
        entities: Optional[List[str]] = None,
        related_pair: Optional[Tuple[str, str]] = None,
        topic: Optional[str] = None,
        claim_id: Optional[str] = None,
        sources: Optional[List[str]] = None,
    ) -> InvestigationResult:
        # 1) Open the investigation (its KG namespace): select or provision.
        name = kg_name_for(title)
        listing = self._rt.call(PLANE_PROVISIONING, "kg_list", {})
        existing = {k.get("name") for k in (listing.get("kgs") or [])}
        provisioned = False
        if name not in existing:
            self._rt.call(
                PLANE_PROVISIONING, "kg_deploy",
                {"name": name, "description": f"investigation: {title}", "approve": True},
            )
            if sources:
                self._rt.call(PLANE_PROVISIONING, "kg_attach_sources", {"name": name, "sources": sources})
            self._rt.call(PLANE_PROVISIONING, "kg_ingest", {"name": name})
            provisioned = True

        # 2) Drive the R11 investigation surface (all ungated).
        surface: List[Dict[str, Any]] = []
        for entity in entities or []:
            surface.append(self._surface("entity_dossier", {"entity": entity}))
        if related_pair:
            a, b = related_pair
            surface.append(self._surface("relationship_path", {"a": a, "b": b}))
        if topic:
            surface.append(self._surface("timeline_reconstruct", {"topic": topic}))
        if claim_id:
            surface.append(self._surface("trace_artifact", {"claim_id": claim_id}))

        # 3) The investigation is auditable from its namespace.
        audit = self._rt.call(PLANE_PROVISIONING, "kg_lineage", {"name": name})

        gated_calls = sum(1 for c in self._rt.transcript() if c.tool in GATED_TOOLS)
        return InvestigationResult(
            title=title,
            kg={"name": name, "provisioned": provisioned},
            surface=surface,
            audit=audit,
            steps=self._rt.steps_used,
            gated_calls=gated_calls,
        )

    def _surface(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        result = self._rt.call(PLANE_OSINT, tool, args)
        ok = not (isinstance(result, dict) and result.get("error"))
        return {"tool": tool, "ok": ok, "result": result}


__all__ = ["InvestigatorAgent", "InvestigationResult", "GATED_TOOLS"]
