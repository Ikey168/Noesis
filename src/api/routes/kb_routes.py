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
    "bad_time": 400,
    "malformed_time": 400,
    "impossible_interval": 400,
    "bad_cursor": 400,
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


class CrossDomainSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=5000)
    domains: Optional[list[str]] = None
    all_authorized: bool = False
    limit: int = Field(default=20, ge=1, le=100)
    per_domain_limit: int = Field(default=20, ge=1, le=100)


class CrossDomainAnswerRequest(BaseModel):
    question: str = Field(min_length=1, max_length=5000)
    domains: Optional[list[str]] = None
    all_authorized: bool = False
    limit: int = Field(default=5, ge=1, le=20)
    per_domain_limit: int = Field(default=5, ge=1, le=20)
    minimum_relevance: float = Field(default=0.34, ge=0, le=1)


class CrossDomainLinksRequest(BaseModel):
    domains: Optional[list[str]] = None
    all_authorized: bool = False
    kind: Optional[str] = None
    relation: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=100)


class TemporalQueryRequest(BaseModel):
    domain: str = Field(min_length=1)
    assertion_kind: Optional[str] = None
    assertion_id: Optional[str] = None
    as_of: Optional[int | str] = None
    valid_at: Optional[int | str] = None
    observed_before: Optional[int | str] = None
    history: bool = False
    include_retracted: bool = False
    limit: int = Field(default=50, ge=1, le=100)
    cursor: Optional[str] = None


class PoliticalQueryRequest(BaseModel):
    domain: str = Field(min_length=1)
    query_type: str = Field(min_length=1)
    jurisdiction: str = Field(min_length=1)
    at: Optional[int | str] = None
    observed_before: Optional[int | str] = None
    office_id: Optional[str] = None
    proposal_id: Optional[str] = None
    actor_id: Optional[str] = None
    institution_id: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=100)


class EconomicQueryRequest(BaseModel):
    domain: str = Field(min_length=1)
    query_type: str = Field(min_length=1)
    series_ids: Optional[list[str]] = None
    indicator_id: Optional[str] = None
    claim_id: Optional[str] = None
    period_from: Optional[str] = None
    period_to: Optional[str] = None
    observed_before: Optional[int | str] = None
    comparison_mode: str = "same_scope"
    include_bundle: bool = False
    limit: int = Field(default=100, ge=1, le=1000)


class TechnicalQueryRequest(BaseModel):
    domain: str = Field(min_length=1)
    query_type: str = Field(min_length=1)
    coordinate: Optional[str] = None
    version: Optional[str] = None
    target_id: Optional[str] = None
    include_optional: bool = False
    max_depth: int = Field(default=8, ge=1, le=32)
    observed_before: Optional[int | str] = None
    limit: int = Field(default=100, ge=1, le=1000)


class ContextAssemblyRequest(BaseModel):
    task: str = Field(min_length=1, max_length=5000)
    token_budget: int = Field(ge=1, le=1_000_000)
    query: Optional[str] = Field(default=None, max_length=5000)
    domains: Optional[list[str]] = None
    namespace_scope: Optional[list[str]] = None
    all_authorized: bool = False
    evidence_policy: Optional[dict] = None
    recency_after_ms: Optional[int] = Field(default=None, ge=0)
    diversity: Optional[dict] = None
    required_object_types: Optional[list[str]] = None
    allowed_surfaces: Optional[list[str]] = None
    max_candidates: int = Field(default=200, ge=1, le=5000)


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


@router.get("/policy-monitor")
def policy_monitor_public():
    """Cited public status with no redaction markers or hidden-corpus counts."""
    return _run(contract.policy_monitor_status)


@router.get("/policy-monitor/private")
def policy_monitor_private(current_user: dict = Depends(require_auth)):
    """Compare private guidance only for an explicitly granted principal."""
    return _run(
        contract.policy_monitor_status,
        _watch_principal(current_user),
        True,
    )


@router.get("/policy-monitor/bundle")
def policy_monitor_public_bundle():
    """Export the default public-only verifiable evidence bundle."""
    return _run(contract.policy_monitor_bundle)


@router.post("/cross-domain/search")
def cross_domain_search(request: CrossDomainSearchRequest):
    """Search explicit or all public domains with rank-fusion provenance."""
    return _run(
        contract.kb_search_domains,
        request.query,
        request.domains,
        request.all_authorized,
        request.limit,
        request.per_domain_limit,
    )


@router.post("/cross-domain/answer")
def cross_domain_answer(request: CrossDomainAnswerRequest):
    """Build one cited answer from an explicit or all-public domain scope."""
    return _run(
        contract.kb_answer_domains,
        request.question,
        request.domains,
        request.all_authorized,
        request.limit,
        request.per_domain_limit,
        request.minimum_relevance,
    )


@router.post("/cross-domain/links")
def cross_domain_links(request: CrossDomainLinksRequest):
    """Inspect public entity equivalences and cross-domain claim links."""
    return _run(
        contract.kb_cross_links,
        request.domains,
        request.all_authorized,
        request.kind,
        request.relation,
        request.limit,
    )


@router.post("/cross-domain/private/search")
def private_cross_domain_search(
    request: CrossDomainSearchRequest,
    current_user: dict = Depends(require_auth),
):
    return _run(
        contract.kb_search_domains,
        request.query,
        request.domains,
        request.all_authorized,
        request.limit,
        request.per_domain_limit,
        _watch_principal(current_user),
        True,
    )


@router.post("/cross-domain/private/answer")
def private_cross_domain_answer(
    request: CrossDomainAnswerRequest,
    current_user: dict = Depends(require_auth),
):
    return _run(
        contract.kb_answer_domains,
        request.question,
        request.domains,
        request.all_authorized,
        request.limit,
        request.per_domain_limit,
        request.minimum_relevance,
        _watch_principal(current_user),
        True,
    )


@router.post("/cross-domain/private/links")
def private_cross_domain_links(
    request: CrossDomainLinksRequest,
    current_user: dict = Depends(require_auth),
):
    return _run(
        contract.kb_cross_links,
        request.domains,
        request.all_authorized,
        request.kind,
        request.relation,
        request.limit,
        _watch_principal(current_user),
        True,
    )


@router.post("/temporal")
def temporal_query(request: TemporalQueryRequest):
    """Query public domain history with independent valid/system-time axes."""
    return _run(
        contract.kb_temporal,
        request.domain,
        request.assertion_kind,
        request.assertion_id,
        request.as_of,
        request.valid_at,
        request.observed_before,
        request.history,
        request.include_retracted,
        request.limit,
        request.cursor,
    )


@router.post("/temporal/private")
def private_temporal_query(
    request: TemporalQueryRequest,
    current_user: dict = Depends(require_auth),
):
    """Query grant-authorized private history without leaking other domains."""
    return _run(
        contract.kb_temporal,
        request.domain,
        request.assertion_kind,
        request.assertion_id,
        request.as_of,
        request.valid_at,
        request.observed_before,
        request.history,
        request.include_retracted,
        request.limit,
        request.cursor,
        _watch_principal(current_user),
        True,
    )


@router.post("/political")
def political_query(request: PoliticalQueryRequest):
    """Run a public cited political-research query."""
    return _run(
        contract.kb_political,
        request.domain,
        request.query_type,
        request.jurisdiction,
        request.at,
        request.observed_before,
        request.office_id,
        request.proposal_id,
        request.actor_id,
        request.institution_id,
        request.limit,
    )


@router.post("/political/private")
def private_political_query(
    request: PoliticalQueryRequest,
    current_user: dict = Depends(require_auth),
):
    """Run the same query against a grant-authorized private domain."""
    return _run(
        contract.kb_political,
        request.domain,
        request.query_type,
        request.jurisdiction,
        request.at,
        request.observed_before,
        request.office_id,
        request.proposal_id,
        request.actor_id,
        request.institution_id,
        request.limit,
        _watch_principal(current_user),
        True,
    )


@router.post("/economic")
def economic_query(request: EconomicQueryRequest):
    """Run a public cited economic trend, comparison, vintage, or claim query."""
    return _run(
        contract.kb_economic,
        request.domain,
        request.query_type,
        request.series_ids,
        request.indicator_id,
        request.claim_id,
        request.period_from,
        request.period_to,
        request.observed_before,
        request.comparison_mode,
        request.include_bundle,
        request.limit,
    )


@router.post("/economic/private")
def private_economic_query(
    request: EconomicQueryRequest,
    current_user: dict = Depends(require_auth),
):
    """Run the same query against a grant-authorized private domain."""
    return _run(
        contract.kb_economic,
        request.domain,
        request.query_type,
        request.series_ids,
        request.indicator_id,
        request.claim_id,
        request.period_from,
        request.period_to,
        request.observed_before,
        request.comparison_mode,
        request.include_bundle,
        request.limit,
        _watch_principal(current_user),
        True,
    )


@router.post("/technical")
def technical_query(request: TechnicalQueryRequest):
    """Run a public cited technical-knowledge graph query."""
    return _run(
        contract.kb_technical,
        request.domain,
        request.query_type,
        request.coordinate,
        request.version,
        request.target_id,
        request.include_optional,
        request.max_depth,
        request.observed_before,
        request.limit,
    )


@router.post("/technical/private")
def private_technical_query(
    request: TechnicalQueryRequest,
    current_user: dict = Depends(require_auth),
):
    """Run the same query against a grant-authorized private domain."""
    return _run(
        contract.kb_technical,
        request.domain,
        request.query_type,
        request.coordinate,
        request.version,
        request.target_id,
        request.include_optional,
        request.max_depth,
        request.observed_before,
        request.limit,
        _watch_principal(current_user),
        True,
    )


@router.post("/context")
def assemble_public_context(request: ContextAssemblyRequest):
    """Assemble cited context from public domains and namespaces."""
    return _run(
        contract.kb_context,
        request.task,
        request.token_budget,
        request.query,
        request.domains,
        request.namespace_scope,
        request.all_authorized,
        request.evidence_policy,
        request.recency_after_ms,
        request.diversity,
        request.required_object_types,
        request.allowed_surfaces,
        request.max_candidates,
    )


@router.post("/context/private")
def assemble_private_context(
    request: ContextAssemblyRequest,
    current_user: dict = Depends(require_auth),
):
    """Assemble context with explicit grant-authorized private scope."""
    return _run(
        contract.kb_context,
        request.task,
        request.token_budget,
        request.query,
        request.domains,
        request.namespace_scope,
        request.all_authorized,
        request.evidence_policy,
        request.recency_after_ms,
        request.diversity,
        request.required_object_types,
        request.allowed_surfaces,
        request.max_candidates,
        _watch_principal(current_user),
        True,
    )


@router.get("/policy-monitor/private/bundle")
def policy_monitor_private_bundle(current_user: dict = Depends(require_auth)):
    """Export private evidence only for an explicitly granted principal."""
    return _run(
        contract.policy_monitor_bundle,
        _watch_principal(current_user),
        True,
    )


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


@router.get("/{domain}/claims/{claim_id}/corroboration")
def corroboration(domain: str, claim_id: str):
    """Origin-aware publication, probable-origin, and unresolved counts."""
    return _run(contract.kb_corroborate, domain, claim_id)


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
