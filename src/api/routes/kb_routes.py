"""
REST mirror of the ``noesis-kb-v1`` contract (see contracts/noesis-kb-v1.md).

Thin adapters over :mod:`src.kb.contract` — the same single implementation
the MCP server uses, so the two surfaces cannot drift. Contract errors map
to HTTP: ``unknown_domain`` → 404, ``bad_request``/``bad_since`` → 400.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.api.auth.jwt_auth import require_auth
from src.kb import contract
from src.kb.contract import KBContractError

router = APIRouter(prefix="/api/v1/kb", tags=["knowledge-base"])

_STATUS = {
    "unknown_domain": 404,
    "not_found": 404,
    "watch_not_found": 404,
    "unauthorized": 403,
    "bad_request": 400,
    "bad_selector": 400,
    "bad_since": 400,
    "confirmation_required": 400,
    "cursor_stale": 409,
    "watermark_conflict": 409,
    "watermark_uncommitted": 409,
    "watch_deleted": 409,
}


class WatchCreateRequest(BaseModel):
    domain: str
    selector: dict
    event_types: Optional[list[str]] = None
    stale_after_ms: int = Field(default=86_400_000, gt=0)


class WatchScanRequest(BaseModel):
    watermark: int = Field(gt=0)
    observed_at_ms: Optional[int] = None


class WatchReplayRequest(BaseModel):
    from_watermark: int = Field(gt=0)
    to_watermark: int = Field(gt=0)


def _watch_principal(current_user: dict) -> str:
    principal = current_user.get("sub") or current_user.get("user_id")
    if not principal:
        raise HTTPException(status_code=401, detail="authenticated principal is missing")
    return str(principal)


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KBContractError as exc:
        raise HTTPException(
            status_code=_STATUS.get(exc.code, 500),
            detail={"code": exc.code, "message": str(exc)},
        ) from exc


@router.get("/domains")
def list_domains():
    return _run(contract.kb_domains)


@router.get("/brief")
async def brief(
    domains: Optional[str] = None,
    since: Optional[str] = None,
    budget: int = 15,
):
    """The daily brief (markdown + sections + meta) for external consumers.

    ``domains`` is a comma-separated list; omit for all configured domains.
    """
    domain_list = (
        [name.strip() for name in domains.split(",") if name.strip()]
        if domains
        else None
    )
    return _run(contract.kb_brief, domain_list, since, budget)


@router.post("/watches")
def create_watch(
    request: WatchCreateRequest,
    current_user: dict = Depends(require_auth),
):
    principal_id = _watch_principal(current_user)
    return _run(
        contract.watch_create,
        request.domain,
        principal_id,
        request.selector,
        request.event_types,
        request.stale_after_ms,
    )


@router.get("/watches")
def watches(
    domain: Optional[str] = None,
    current_user: dict = Depends(require_auth),
):
    principal_id = _watch_principal(current_user)
    return _run(contract.watch_list, principal_id, domain)


@router.get("/watches/metrics")
def watch_metrics(_current_user: dict = Depends(require_auth)):
    return _run(contract.watch_observability)


@router.get("/watches/{watch_id}/events")
def poll_watch(
    watch_id: str,
    cursor: Optional[str] = None,
    limit: int = 50,
    event_types: Optional[str] = None,
    current_user: dict = Depends(require_auth),
):
    principal_id = _watch_principal(current_user)
    selected = (
        [item.strip() for item in event_types.split(",") if item.strip()]
        if event_types is not None
        else None
    )
    return _run(
        contract.watch_poll,
        watch_id,
        principal_id,
        cursor,
        limit,
        selected,
    )


@router.post("/watches/{watch_id}/pause")
def pause_watch(
    watch_id: str,
    current_user: dict = Depends(require_auth),
):
    principal_id = _watch_principal(current_user)
    return _run(contract.watch_pause, watch_id, principal_id)


@router.post("/watches/{watch_id}/resume")
def resume_watch(
    watch_id: str,
    current_user: dict = Depends(require_auth),
):
    principal_id = _watch_principal(current_user)
    return _run(contract.watch_resume, watch_id, principal_id)


@router.post("/watches/{watch_id}/replay")
def replay_watch(
    watch_id: str,
    request: WatchReplayRequest,
    current_user: dict = Depends(require_auth),
):
    principal_id = _watch_principal(current_user)
    return _run(
        contract.watch_replay,
        watch_id,
        principal_id,
        request.from_watermark,
        request.to_watermark,
    )


@router.delete("/watches/{watch_id}")
def remove_watch(
    watch_id: str,
    confirm: bool = False,
    current_user: dict = Depends(require_auth),
):
    principal_id = _watch_principal(current_user)
    return _run(contract.watch_delete, watch_id, principal_id, confirm)


@router.post("/watches/scan")
def scan_watches(
    request: WatchScanRequest,
    current_user: dict = Depends(require_auth),
):
    principal_id = _watch_principal(current_user)
    return _run(
        contract.watch_scan,
        principal_id,
        request.watermark,
        request.observed_at_ms,
    )


@router.get("/{domain}/search")
def search(domain: str, q: str, limit: int = 20):
    return _run(contract.kb_search, domain, q, limit)


@router.get("/{domain}/answer")
def answer(
    domain: str,
    q: str,
    limit: int = 5,
    minimum_relevance: float = 0.34,
):
    """Structured offline answer; ``q`` is the question to evidence-plan."""
    return _run(contract.kb_answer, domain, q, limit, minimum_relevance)


@router.get("/{domain}/documents")
def documents(domain: str, since: Optional[str] = None, limit: int = 50):
    return _run(contract.kb_documents, domain, since, limit)


@router.get("/{domain}/claims")
def claims(domain: str, since: Optional[str] = None, limit: int = 50):
    return _run(contract.kb_claims, domain, since, limit)


@router.get("/{domain}/entities")
def entities(domain: str, name: Optional[str] = None):
    return _run(contract.kb_entities, domain, name)


@router.get("/{domain}/contradictions")
def contradictions(domain: str, since: Optional[str] = None):
    return _run(contract.kb_contradictions, domain, since)


@router.get("/{domain}/diff")
def diff(domain: str, since: str):
    return _run(contract.kb_diff, domain, since)


@router.get("/{domain}/coverage")
def coverage(domain: str):
    return _run(contract.kb_coverage, domain)


@router.get("/{domain}/integrity")
def integrity(domain: str, document_id: Optional[str] = None, limit: int = 100):
    return _run(contract.kb_integrity, domain, document_id, limit)
