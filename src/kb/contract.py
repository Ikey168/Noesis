"""
``noesis-kb-v1``: the versioned query surface applications build against.

This module is **routing only**: every answer goes through the domain
registry to a :class:`~src.kb.backing.DomainBacking`, never around it — that
is what keeps a consumer unable to tell (or care) which backing serves it.
The MCP server (``tools/kb_mcp/server.py``) and the REST routes
(``src/api/routes/kb_routes.py``) are both thin adapters over these
functions, so the contract has exactly one implementation.

Envelope: every response carries ``contract``, ``domain``, ``as_of_ms``, and
``data``. Analytic entries inside ``data`` carry citations and
``prediction_mode``/confidence where a model produced them; ``coverage``
reports the backing as metadata, but answer *shapes* are identical across
backings. Errors raise ``KBContractError`` with a stable ``code``.

The human-readable contract lives in ``contracts/noesis-kb-v1.md``; the
cross-backing shape guarantee is enforced by
``tests/unit/kb/test_contract.py``, which runs one suite against a
view-backed and a namespace-backed fixture domain and diffs the shapes.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

CONTRACT_VERSION = "noesis-kb-v1"


class KBContractError(Exception):
    """Contract-level error with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _registry(config_path=None):
    from src.kb.registry import load_registry

    return load_registry(config_path)


def _backing(domain: str, conn=None, config_path=None):
    from src.kb.registry import DomainConfigError

    try:
        return _registry(config_path).resolve(domain, conn=conn)
    except DomainConfigError as exc:
        raise KBContractError("unknown_domain", str(exc)) from exc


def _envelope(domain: Optional[str], data: Any) -> Dict[str, Any]:
    return {
        "contract": CONTRACT_VERSION,
        "domain": domain,
        "as_of_ms": int(time.time() * 1000),
        "data": data,
    }


def kb_domains(conn=None, config_path=None) -> Dict[str, Any]:
    """Configured domains with their backing (list; no per-domain reads)."""
    registry = _registry(config_path)
    return _envelope(
        None,
        [
            {
                "name": definition.name,
                "backing": definition.backing,
                "description": definition.description,
                "embedding_model": definition.embedding_model,
            }
            for definition in registry.domains()
        ],
    )


def kb_search(
    domain: str, query: str, limit: int = 20, conn=None, config_path=None
) -> Dict[str, Any]:
    if not query or not query.strip():
        raise KBContractError("bad_request", "query must be non-empty")
    backing = _backing(domain, conn, config_path)
    return _envelope(domain, backing.search(query, limit=int(limit)))


def kb_answer(
    domain: str,
    question: str,
    limit: int = 5,
    minimum_relevance: float = 0.34,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """One question in, one structured and machine-verifiable answer out.

    This is an additive ``noesis-kb-v1`` operation.  Its ``data`` payload is
    versioned independently as ``noesis-answer-v1`` so existing KB response
    shapes stay unchanged while answer semantics can evolve explicitly.
    """
    if not isinstance(question, str) or not question.strip():
        raise KBContractError("bad_request", "question must be non-empty")
    if len(question) > 5_000:
        raise KBContractError("bad_request", "question must be at most 5000 characters")
    try:
        limit = int(limit)
        minimum_relevance = float(minimum_relevance)
    except (TypeError, ValueError) as exc:
        raise KBContractError(
            "bad_request", "limit and minimum_relevance must be numeric"
        ) from exc
    if not 1 <= limit <= 20:
        raise KBContractError("bad_request", "limit must be between 1 and 20")
    if not 0.0 <= minimum_relevance <= 1.0:
        raise KBContractError(
            "bad_request", "minimum_relevance must be between 0 and 1"
        )

    from src.kb.answer import build_answer

    backing = _backing(domain, conn, config_path)
    return _envelope(
        domain,
        build_answer(
            backing,
            question,
            limit=limit,
            minimum_relevance=minimum_relevance,
        ),
    )


def kb_corroborate(
    domain: str,
    claim_id: str,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """Origin-aware corroboration for one claim visible in the domain."""
    if not isinstance(claim_id, str) or not claim_id.strip():
        raise KBContractError("bad_request", "claim_id must be non-empty")
    backing = _backing(domain, conn, config_path)
    visible = {
        str(citation.get("claim_id"))
        for cluster in backing.claims(limit=100_000)
        for citation in cluster.get("citations", [])
        if citation.get("claim_id")
    }
    if claim_id not in visible:
        raise KBContractError(
            "not_found", f"claim {claim_id!r} is not a member of domain {domain!r}"
        )
    from src.osint.corroboration import corroborate

    return _envelope(domain, corroborate(backing.conn, claim_id))


def _watch_connection(conn=None):
    if conn is not None:
        return conn
    from src.database.local_analytics_connector import get_shared_connection

    return get_shared_connection()


def _watch_call(fn, *args, **kwargs):
    from src.kb.watches import WatchError

    try:
        return fn(*args, **kwargs)
    except WatchError as exc:
        raise KBContractError(exc.code, str(exc)) from exc


def watch_create(
    domain: str,
    principal_id: str,
    selector: Dict[str, Any],
    event_types: Optional[List[str]] = None,
    stale_after_ms: int = 86_400_000,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """Create one durable, principal-bound watch in a resolved domain."""
    from src.kb.watches import create_watch

    backing = _backing(domain, conn, config_path)
    payload = _watch_call(
        create_watch,
        backing,
        principal_id,
        selector,
        event_types,
        stale_after_ms=stale_after_ms,
    )
    return _envelope(domain, payload)


def watch_list(
    principal_id: str,
    domain: Optional[str] = None,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """List watches owned by the principal, optionally within one domain."""
    from src.kb.watches import list_watches

    connection = _watch_connection(conn)
    if domain is not None:
        backing = _backing(domain, connection, config_path)
        from src.kb.watches import _authorize_domain

        _watch_call(_authorize_domain, connection, backing, str(principal_id))
    payload = _watch_call(
        list_watches, connection, principal_id, domain=domain
    )
    return _envelope(domain, payload)


def watch_poll(
    watch_id: str,
    principal_id: str,
    cursor: Optional[str] = None,
    limit: int = 50,
    event_types: Optional[List[str]] = None,
    conn=None,
) -> Dict[str, Any]:
    """Poll immutable events after an opaque cursor."""
    from src.kb.watches import poll_watch

    payload = _watch_call(
        poll_watch,
        _watch_connection(conn),
        principal_id,
        watch_id,
        cursor=cursor,
        limit=limit,
        event_types=event_types,
    )
    return _envelope(payload["domain"], payload)


def watch_pause(
    watch_id: str, principal_id: str, conn=None
) -> Dict[str, Any]:
    from src.kb.watches import set_watch_status

    payload = _watch_call(
        set_watch_status,
        _watch_connection(conn),
        principal_id,
        watch_id,
        "paused",
    )
    return _envelope(payload["domain"], payload)


def watch_resume(
    watch_id: str, principal_id: str, conn=None
) -> Dict[str, Any]:
    from src.kb.watches import set_watch_status

    payload = _watch_call(
        set_watch_status,
        _watch_connection(conn),
        principal_id,
        watch_id,
        "active",
    )
    return _envelope(payload["domain"], payload)


def watch_delete(
    watch_id: str, principal_id: str, confirm: bool = False, conn=None
) -> Dict[str, Any]:
    from src.kb.watches import delete_watch

    payload = _watch_call(
        delete_watch,
        _watch_connection(conn),
        principal_id,
        watch_id,
        confirm=confirm,
    )
    return _envelope(payload["domain"], payload)


def watch_scan(
    principal_id: str,
    watermark: int,
    observed_at_ms: Optional[int] = None,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """Run the deterministic matcher for one principal at a committed watermark."""
    from src.kb.watches import run_watch_matcher

    connection = _watch_connection(conn)
    payload = _watch_call(
        run_watch_matcher,
        connection,
        _registry(config_path),
        watermark,
        principal_id=principal_id,
        observed_at_ms=observed_at_ms,
    )
    return _envelope(None, payload)


def watch_replay(
    watch_id: str,
    principal_id: str,
    from_watermark: int,
    to_watermark: int,
    conn=None,
) -> Dict[str, Any]:
    """Audit deterministic logical events over retained watermark snapshots."""
    from src.kb.watches import replay_watch

    payload = _watch_call(
        replay_watch,
        _watch_connection(conn),
        principal_id,
        watch_id,
        from_watermark=from_watermark,
        to_watermark=to_watermark,
    )
    return _envelope(payload["domain"], payload)


def watch_observability(conn=None) -> Dict[str, Any]:
    """Text-free watch volume, lag, retry, and dead-letter metrics."""
    from src.kb.watches import watch_metrics

    return _envelope(None, watch_metrics(_watch_connection(conn)))


def kb_documents(
    domain: str,
    since: Optional[str] = None,
    limit: int = 50,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    backing = _backing(domain, conn, config_path)
    try:
        documents = backing.documents(limit=int(limit), since=since)
    except ValueError as exc:
        raise KBContractError("bad_since", str(exc)) from exc
    return _envelope(domain, documents)


def kb_claims(
    domain: str,
    since: Optional[str] = None,
    limit: int = 50,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    backing = _backing(domain, conn, config_path)
    try:
        clusters = backing.claims(since=since, limit=int(limit))
    except ValueError as exc:
        raise KBContractError("bad_since", str(exc)) from exc
    return _envelope(domain, clusters)


def kb_entities(
    domain: str, name: Optional[str] = None, conn=None, config_path=None
) -> Dict[str, Any]:
    backing = _backing(domain, conn, config_path)
    return _envelope(domain, backing.entities(name=name))


def kb_contradictions(
    domain: str, since: Optional[str] = None, conn=None, config_path=None
) -> Dict[str, Any]:
    """The domain's contradiction ledger (both sides cited)."""
    backing = _backing(domain, conn, config_path)
    since = since or "1970-01-01"
    try:
        diff = backing.diff(since=since)
    except ValueError as exc:
        raise KBContractError("bad_since", str(exc)) from exc
    return _envelope(domain, diff["new_contradictions"])


def kb_diff(
    domain: str, since: str, conn=None, config_path=None
) -> Dict[str, Any]:
    if not since:
        raise KBContractError("bad_request", "since is required")
    backing = _backing(domain, conn, config_path)
    try:
        return _envelope(domain, backing.diff(since=since))
    except ValueError as exc:
        raise KBContractError("bad_since", str(exc)) from exc


def kb_integrity(
    domain: str,
    document_id: Optional[str] = None,
    limit: int = 100,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """Integrity ledger for one document or the domain's recent documents."""
    backing = _backing(domain, conn, config_path)
    documents = backing.documents(limit=100_000)
    ids = [row["document_id"] for row in documents]
    if document_id is not None:
        if document_id not in set(ids):
            raise KBContractError(
                "not_found", f"document {document_id!r} is not a member of domain {domain!r}"
            )
        ids = [document_id]
    from src.integrity.ledger import integrity_ledger

    return _envelope(domain, integrity_ledger(backing.conn, ids[: int(limit)], limit=int(limit)))


def kb_brief(
    domains: Optional[List[str]] = None,
    since: Optional[str] = None,
    budget: int = 15,
    conn=None,
    config_path=None,
) -> Dict[str, Any]:
    """The daily brief as a contract call (markdown + sections + meta).

    External consumers (Modulo, dashboards) fetch this instead of composing
    diffs themselves; the envelope carries no single domain, so ``domain``
    is ``None`` and the per-domain breakdown lives in ``data.sections``.
    """
    from src.kb.brief import generate_brief
    from src.kb.registry import DomainConfigError

    try:
        brief = generate_brief(
            domains=domains, since=since, budget=int(budget),
            conn=conn, config_path=config_path,
        )
    except DomainConfigError as exc:
        raise KBContractError("unknown_domain", str(exc)) from exc
    except ValueError as exc:
        raise KBContractError("bad_since", str(exc)) from exc

    return _envelope(None, brief)


def kb_coverage(domain: str, conn=None, config_path=None) -> Dict[str, Any]:
    backing = _backing(domain, conn, config_path)
    payload = backing.coverage()
    # Honesty rider (#958): every coverage answer states what fraction of
    # the underlying analysis is model-grade or has unknown legacy provenance.
    from src.kb.evidence import evidence_quality_summary

    try:
        payload["evidence_quality"] = evidence_quality_summary(backing.conn)
    except Exception:
        payload["evidence_quality"] = None
    return _envelope(domain, payload)
