"""Generative-UI routes: adaptive layout planning for the Noesis canvas.

POST /api/v1/ui/generate turns a natural-language intent into a validated
``ui-spec-v1`` layout; GET /api/v1/ui/context exposes the adaptive inputs
(merged domain-pack ui_flags, warehouse data availability, LLM planner
status); GET /api/v1/ui/panels exposes the panel catalog the frontend
renderer mirrors.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from src.genui.adaptivity import data_availability, merged_ui_flags
from src.genui.catalog import panel_catalog_dict
from src.genui.llm import llm_config, plan_with_llm
from src.genui.planner import plan
from src.genui.spec import MAX_INTENT_LENGTH, SOURCE_TYPES, validate_spec

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
        availability = data_availability()
        ui_flags = merged_ui_flags()
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
        return {
            "spec": spec_dict,
            "meta": {
                "generated_by": spec.generated_by,
                "availability_known": availability is not None,
                "ui_flags": ui_flags,
            },
        }
    except HTTPException:
        raise
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"UI generation failed: {err}")


@router.get("/context")
def ui_context() -> Dict[str, Any]:
    """Expose the adaptive inputs the planner uses (sync: blocking probe)."""
    try:
        availability = data_availability()
        config = llm_config()
        return {
            "ui_flags": merged_ui_flags(),
            "availability": availability,
            "availability_known": availability is not None,
            "llm": {
                "enabled": config is not None,
                "provider": config["provider"] if config else None,
            },
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"UI context failed: {err}")


@router.get("/panels")
async def ui_panels() -> Dict[str, Any]:
    """Expose the panel catalog the frontend renderer mirrors."""
    try:
        panels = panel_catalog_dict()
        return {"panels": panels, "count": len(panels)}
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"UI panel catalog failed: {err}")
