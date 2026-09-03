"""
Noesis KB contract — MCP server (``noesis-kb-v1``).

The knowledge-base query surface applications build against: retrieve
(search / documents / claims / entities), reason (contradictions), diff
("what changed since T" — the primitive briefs and alerting reduce to), and
meta (coverage). Every answer is cited, carries as-of metadata, and is
identical in shape whether the domain is served from the shared corpus or a
provisioned namespace — the tools route through the domain registry and the
``DomainBacking`` interface, never around them.

Tools:
  kb_domains()                                -> configured domains + backing
  kb_search(domain, query, limit=20)          -> lexical search, cited rows
  kb_answer(domain, question, limit=5,
            minimum_relevance=.34)            -> structured extractive answer
  kb_documents(domain, since?, limit=50)      -> member documents, newest arrival first
  kb_claims(domain, since?, limit=50)         -> clustered, cited claims
  kb_entities(domain, name?)                  -> canonical entities, aliases folded
  kb_contradictions(domain, since?)           -> contradiction ledger, both sides cited
  kb_diff(domain, since)                      -> the change feed (six sections)
  kb_integrity(domain, document_id?, limit?)  -> snapshots/revisions/media ledger
  kb_coverage(domain)                         -> corpus stats, freshness, backing

Contract doc: contracts/noesis-kb-v1.md. Errors return
``{"error": {"code", "message"}}`` instead of raising.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastmcp import FastMCP

from src.mcp_host.transport import run_server

mcp = FastMCP("noesis-kb")


def _run(fn, *args: Any, **kwargs: Any) -> Dict[str, Any]:
    from src.kb.contract import KBContractError

    try:
        return fn(*args, **kwargs)
    except KBContractError as exc:
        return {"error": {"code": exc.code, "message": str(exc)}}
    except Exception as exc:  # noqa: BLE001 - tool boundary
        return {"error": {"code": "internal", "message": str(exc)}}


@mcp.tool()
def kb_domains() -> Dict[str, Any]:
    """List the configured knowledge domains and how each is backed."""
    from src.kb import contract

    return _run(contract.kb_domains)


@mcp.tool()
def kb_search(domain: str, query: str, limit: int = 20) -> Dict[str, Any]:
    """Search a domain's documents (lexical; wildcards are literals)."""
    from src.kb import contract

    return _run(contract.kb_search, domain, query, limit)


@mcp.tool()
def kb_answer(
    domain: str,
    question: str,
    limit: int = 5,
    minimum_relevance: float = 0.34,
) -> Dict[str, Any]:
    """Answer from a deterministic evidence plan. Every factual statement
    carries separate supporting and contradicting locators; insufficient
    evidence returns an explicit unverifiable refusal instead of synthesis."""
    from src.kb import contract

    return _run(
        contract.kb_answer,
        domain,
        question,
        limit,
        minimum_relevance,
    )


@mcp.tool()
def kb_documents(
    domain: str, since: Optional[str] = None, limit: int = 50
) -> Dict[str, Any]:
    """A domain's documents, newest arrival first. `since` is ISO-8601 UTC
    and filters on ingestion time (backfilled old publications count as new)."""
    from src.kb import contract

    return _run(contract.kb_documents, domain, since, limit)


@mcp.tool()
def kb_claims(
    domain: str, since: Optional[str] = None, limit: int = 50
) -> Dict[str, Any]:
    """Clustered, cited claims: representative + every citation,
    corroboration count, contradictions, supersedence flags."""
    from src.kb import contract

    return _run(contract.kb_claims, domain, since, limit)


@mcp.tool()
def kb_entities(domain: str, name: Optional[str] = None) -> Dict[str, Any]:
    """Canonical entities mentioned in the domain, alias mentions folded."""
    from src.kb import contract

    return _run(contract.kb_entities, domain, name)


@mcp.tool()
def kb_contradictions(
    domain: str, since: Optional[str] = None
) -> Dict[str, Any]:
    """Where the record disagrees with itself: contradicts-links touching
    the domain, both sides cited, with prediction_mode/confidence."""
    from src.kb import contract

    return _run(contract.kb_contradictions, domain, since)


@mcp.tool()
def kb_diff(domain: str, since: str) -> Dict[str, Any]:
    """What changed since T: new documents/sources, new clusters, gained
    corroboration, new contradictions, superseded claims, entity surges."""
    from src.kb import contract

    return _run(contract.kb_diff, domain, since)


@mcp.tool()
def kb_brief(
    domains: Optional[list] = None,
    since: Optional[str] = None,
    budget: int = 15,
) -> Dict[str, Any]:
    """The daily brief: per-domain changes since T under a hard item budget
    (dropped count reported), with a New-publications section for research
    domains. Returns {markdown, sections, meta}; every line cited."""
    from src.kb import contract

    return _run(contract.kb_brief, domains, since, budget)


@mcp.tool()
def kb_coverage(domain: str) -> Dict[str, Any]:
    """Corpus stats, freshness, sources, backing, embedding model —
    so a consumer can honestly say when coverage is thin."""
    from src.kb import contract

    return _run(contract.kb_coverage, domain)


@mcp.tool()
def kb_integrity(
    domain: str, document_id: Optional[str] = None, limit: int = 100
) -> Dict[str, Any]:
    """Per-document integrity: snapshots, refetch revisions (both versions),
    correction/retraction class, image reuse/C2PA, and cross-modal findings.
    Every finding contains evidence locators; absence of C2PA is neutral."""
    from src.kb import contract

    return _run(contract.kb_integrity, domain, document_id, limit)


if __name__ == "__main__":
    run_server(mcp)
