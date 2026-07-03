"""Generative-UI routes: adaptive layout planning for the Noesis canvas.

POST /api/v1/ui/generate turns a natural-language intent into a validated
``ui-spec-v1`` layout; GET /api/v1/ui/context exposes the adaptive inputs
(merged domain-pack ui_flags, warehouse data availability, LLM planner
status, MCP host health); GET /api/v1/ui/panels exposes the panel catalog
the frontend renderer mirrors.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.genui.adaptivity import resolve_availability, resolve_ui_flags
from src.genui.discovery import merged_catalog_dict
from src.genui.llm import llm_config, plan_with_llm
from src.genui.planner import plan
from src.genui.refine import refine_to_diff
from src.genui.spec import MAX_INTENT_LENGTH, SOURCE_TYPES, spec_from_dict, validate_spec
from src.genui.spec_diff import apply_diff
from src.genui.telemetry import pack_telemetry
from src.mcp_host import host_status

router = APIRouter(prefix="/api/v1/ui", tags=["generative_ui"])


class UsageSignals(BaseModel):
    """Client-side usage signals persisted by the frontend."""

    pinned: List[str] = Field(default_factory=list, max_length=32)
    dismissed: List[str] = Field(default_factory=list, max_length=32)
    weights: Dict[str, int] = Field(default_factory=dict)


class GenerateUiRequest(BaseModel):
    """Body for POST /api/v1/ui/generate."""

    intent: str = Field("", max_length=MAX_INTENT_LENGTH, description="Analyst intent, free text")
    source_type: Optional[str] = Field(None, description="Optional source-type filter")
    signals: Optional[UsageSignals] = Field(None, description="Usage signals for adaptive re-ranking")

    @field_validator("source_type")
    @classmethod
    def _check_source_type(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in SOURCE_TYPES:
            raise ValueError(f"source_type must be one of {SOURCE_TYPES}")
        return value


@router.post("/generate")
def generate_ui(request: GenerateUiRequest) -> Dict[str, Any]:
    """Generate an adaptive ui-spec-v1 layout for an intent.

    Sync on purpose: FastAPI runs it in the threadpool, so the blocking
    warehouse probe and the (optional) LLM completion never stall the
    event loop.
    """
    try:
        availability, availability_source = resolve_availability()
        ui_flags, _ = resolve_ui_flags()
        signals = request.signals.model_dump() if request.signals else None

        spec = plan_with_llm(
            request.intent,
            source_type=request.source_type,
            availability=availability,
            ui_flags=ui_flags,
            signals=signals,
        )
        if spec is None:
            spec = plan(
                request.intent,
                source_type=request.source_type,
                signals=signals,
                availability=availability,
                ui_flags=ui_flags,
            )

        spec_dict = spec.to_dict()
        errors = validate_spec(spec_dict)
        if errors:
            raise HTTPException(
                status_code=500,
                detail=f"Generated spec failed validation: {'; '.join(errors[:3])}",
            )
        # R12/#639 cold-path lever: when the data proxy is on, warm the data-mode
        # cache for this spec's panels in the background, so the browser's first
        # /ui/data fetch is a cache hit rather than a cold MCP round-trip. Never
        # blocks the response; a no-op when the flag is off.
        try:
            from src.genui.dataplane import prewarm_from_spec

            prewarm_from_spec(spec_dict)
        except Exception:
            pass
        return {
            "spec": spec_dict,
            "meta": {
                "generated_by": spec.generated_by,
                "availability_known": availability is not None,
                "availability_source": availability_source,
                "ui_flags": ui_flags,
            },
        }
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"UI generation failed: {err}")


@router.get("/context")
def ui_context() -> Dict[str, Any]:
    """Expose the adaptive inputs the planner uses (sync: blocking probe).

    R3: availability and ui_flags come from the servers' stats tools when
    the host runtime is up (source ``tools``); the DuckDB probe / registry
    fallbacks report ``warehouse`` / ``packs``.
    """
    try:
        availability, availability_source = resolve_availability()
        ui_flags, ui_flags_source = resolve_ui_flags()
        config = llm_config()
        return {
            "ui_flags": ui_flags,
            "ui_flags_source": ui_flags_source,
            "availability": availability,
            "availability_source": availability_source,
            "availability_known": availability is not None,
            "llm": {
                "enabled": config is not None,
                "provider": config["provider"] if config else None,
            },
            # R1: per-server MCP host health. A pure snapshot read - a hung
            # or dead server can never stall this endpoint.
            "mcp": host_status(),
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"UI context failed: {err}")


@router.get("/telemetry")
def ui_telemetry() -> Dict[str, Any]:
    """Ambient empty-canvas telemetry supplied by the enabled packs (R3).

    Sync on purpose (warehouse reads run in the threadpool). With the news
    pack disabled the payload still carries the engine's library telemetry
    (recently ingested documents), never an empty gap.
    """
    try:
        return pack_telemetry()
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"UI telemetry failed: {err}")


@router.get("/panels")
async def ui_panels() -> Dict[str, Any]:
    """Expose the panel catalog the frontend renderer mirrors.

    R2: discovery-derived defs from annotated MCP tools merge over the
    static catalog; with no servers connected the payload is byte-identical
    to the static catalog. Reads the host's cache only, so this stays
    non-blocking in the event loop.
    """
    try:
        panels = merged_catalog_dict()
        return {"panels": panels, "count": len(panels)}
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"UI panel catalog failed: {err}")


class RefineUiRequest(BaseModel):
    """Body for POST /api/v1/ui/refine (M6 in-canvas refinement)."""

    spec: Dict[str, Any] = Field(..., description="The current ui-spec-v1 layout")
    instruction: str = Field("", max_length=MAX_INTENT_LENGTH, description="Refinement instruction")


@router.post("/refine")
def refine_ui(request: RefineUiRequest) -> Dict[str, Any]:
    """Apply a natural-language refinement to an existing canvas (M6): turn the
    instruction into a spec diff and apply it in place, so a follow-up mutates
    the current layout instead of regenerating it.

    Returns the refined spec, the diff that was applied, and any per-op errors.
    An unrecognized instruction is a no-op (``changed`` is false, spec unchanged).
    """
    errors = validate_spec(request.spec)
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"spec failed validation: {'; '.join(errors[:3])}",
        )
    try:
        spec = spec_from_dict(request.spec)
        diff = refine_to_diff(spec, request.instruction)
        # An unrecognized instruction changes nothing: return the input untouched.
        if not diff:
            return {"spec": request.spec, "diff": [], "errors": [], "changed": False}
        new_spec, apply_errors = apply_diff(spec, diff)
        new_dict = new_spec.to_dict()
        # The diff engine already caps/validates, but double-check the contract;
        # on any violation fall back to the original spec rather than emit an
        # invalid layout.
        if validate_spec(new_dict):
            return {"spec": request.spec, "diff": [], "errors": apply_errors, "changed": False}
        return {
            "spec": new_dict,
            "diff": diff,
            "errors": apply_errors,
            "changed": True,
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"UI refinement failed: {err}")
