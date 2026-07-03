"""Agent host routes (M10, live entry point).

Expose the M10 analyst and investigator agents over HTTP so an operator (and the
frontend) can launch an agent on a goal at runtime and replay its audit trail:

    GET  /api/v1/agent                    whether the agent API is enabled
    POST /api/v1/agent/analyst            run the analyst on a goal
    POST /api/v1/agent/investigator       run an investigation
    GET  /api/v1/agent/runs/{run_id}      replay a run from the audit trail

An agent provisions KGs and drives the whole surface, so the run endpoints are
gated behind ``NOESIS_AGENT_API`` (off by default). Each run is driven over the
in-process backend against the shared warehouse, with the provisioning audit
sink attached so every call is recorded and replayable (M10.4).
"""

import os
import secrets
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.agent.analyst import AnalystAgent
from src.agent.audit import provisioning_audit_sink, replay_run
from src.agent.investigator import InvestigatorAgent
from src.agent.local_backend import build_local_caller
from src.agent.runtime import AgentRuntime, Budget, live_caller

router = APIRouter(prefix="/api/v1/agent", tags=["agent"])

# Transports an agent run can drive:
#   "local" - the in-process backend against the shared warehouse (default),
#   "live"  - the real MCP servers via the supervised host (runtime.live_caller).
TRANSPORTS = ("local", "live")


def agent_enabled() -> bool:
    """Whether the agent run endpoints are enabled (``NOESIS_AGENT_API``)."""
    return os.getenv("NOESIS_AGENT_API", "off").lower() in ("on", "1", "true")


def default_transport() -> str:
    """The default run transport (``NOESIS_AGENT_TRANSPORT``); ``local`` unless
    explicitly set to ``live``."""
    value = os.getenv("NOESIS_AGENT_TRANSPORT", "local").lower()
    return value if value in TRANSPORTS else "local"


def _build_caller(conn, transport: Optional[str]):
    """Resolve the tool caller for a run. ``live`` drives the real MCP surface
    (and fails loudly if the host is down); anything else is the in-process
    backend against ``conn``."""
    chosen = (transport or default_transport()).lower()
    if chosen == "live":
        return live_caller(), "live"
    return build_local_caller(conn), "local"


def _require_enabled() -> None:
    if not agent_enabled():
        raise HTTPException(
            status_code=404,
            detail="agent API is disabled (set NOESIS_AGENT_API=on)",
        )


def _conn():
    from src.database.local_analytics_connector import _LOCK, get_shared_connection

    return get_shared_connection(), _LOCK


class AnalystRequest(BaseModel):
    goal: str = Field(..., max_length=500)
    kg_name: Optional[str] = Field(default=None, max_length=64)
    sources: Optional[List[str]] = None
    topic: Optional[str] = None
    entity: Optional[str] = None
    claim_id: Optional[str] = None
    source: Optional[str] = None
    max_steps: int = Field(default=24, ge=1, le=100)
    transport: Optional[str] = Field(default=None, description="local | live")


class InvestigatorRequest(BaseModel):
    title: str = Field(..., max_length=200)
    entities: Optional[List[str]] = None
    related_pair: Optional[Tuple[str, str]] = None
    topic: Optional[str] = None
    claim_id: Optional[str] = None
    sources: Optional[List[str]] = None
    max_steps: int = Field(default=24, ge=1, le=100)
    transport: Optional[str] = Field(default=None, description="local | live")


@router.get("")
def status() -> Dict[str, Any]:
    """Whether the agent run endpoints are enabled."""
    return {"enabled": agent_enabled()}


@router.post("/analyst")
def run_analyst(request: AnalystRequest) -> Dict[str, Any]:
    """Run the analyst agent on a goal (goal -> KG -> OSINT -> canvas), recording
    every call to the audit trail under a fresh run id."""
    _require_enabled()
    conn, lock = _conn()
    run_id = "analyst-" + secrets.token_urlsafe(6)
    caller, transport = _build_caller(conn, request.transport)
    try:
        with lock:
            runtime = AgentRuntime(
                caller,
                budget=Budget(max_steps=request.max_steps),
                audit_sink=provisioning_audit_sink(conn, run_id),
            )
            result = AnalystAgent(runtime).run(
                request.goal,
                kg_name=request.kg_name,
                sources=request.sources,
                topic=request.topic,
                entity=request.entity,
                claim_id=request.claim_id,
                source=request.source,
            )
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"analyst run failed: {err}")
    return {
        "run_id": run_id,
        "transport": transport,
        "goal": result.goal,
        "kg": result.kg,
        "osint": result.osint,
        "canvas": result.canvas,
        "steps": result.steps,
        "findings": result.findings,
    }


@router.post("/investigator")
def run_investigator(request: InvestigatorRequest) -> Dict[str, Any]:
    """Run an investigation (open KG -> R11 surface -> canvas), respecting the
    review gate and recording every call to the audit trail."""
    _require_enabled()
    conn, lock = _conn()
    run_id = "investigation-" + secrets.token_urlsafe(6)
    caller, transport = _build_caller(conn, request.transport)
    try:
        with lock:
            runtime = AgentRuntime(
                caller,
                budget=Budget(max_steps=request.max_steps),
                audit_sink=provisioning_audit_sink(conn, run_id),
            )
            result = InvestigatorAgent(runtime).run(
                request.title,
                entities=request.entities,
                related_pair=request.related_pair,
                topic=request.topic,
                claim_id=request.claim_id,
                sources=request.sources,
            )
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"investigation run failed: {err}")
    return {
        "run_id": run_id,
        "transport": transport,
        "title": result.title,
        "kg": result.kg,
        "surface": result.surface,
        "audit": result.audit,
        "canvas": result.canvas,
        "steps": result.steps,
        "gated_calls": result.gated_calls,
        "findings": result.findings,
    }


@router.get("/runs/{run_id}")
def run_audit(run_id: str) -> Dict[str, Any]:
    """Replay a run from the audit trail: its ordered tool calls."""
    conn, lock = _conn()
    try:
        with lock:
            calls = replay_run(conn, run_id)
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"run replay failed: {err}")
    if not calls:
        raise HTTPException(status_code=404, detail=f"no audit trail for run {run_id!r}")
    return {"run_id": run_id, "calls": calls, "count": len(calls)}
