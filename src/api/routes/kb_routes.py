"""
REST mirror of the ``noesis-kb-v1`` contract (see contracts/noesis-kb-v1.md).

Thin adapters over :mod:`src.kb.contract` — the same single implementation
the MCP server uses, so the two surfaces cannot drift. Contract errors map
to HTTP: ``unknown_domain`` → 404, ``bad_request``/``bad_since`` → 400.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException

from src.kb import contract
from src.kb.contract import KBContractError

router = APIRouter(prefix="/api/v1/kb", tags=["knowledge-base"])

_STATUS = {"unknown_domain": 404, "bad_request": 400, "bad_since": 400}


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
def brief(
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


@router.get("/{domain}/search")
def search(domain: str, q: str, limit: int = 20):
    return _run(contract.kb_search, domain, q, limit)


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
