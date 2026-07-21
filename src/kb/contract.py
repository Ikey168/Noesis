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


def kb_coverage(domain: str, conn=None, config_path=None) -> Dict[str, Any]:
    backing = _backing(domain, conn, config_path)
    return _envelope(domain, backing.coverage())
