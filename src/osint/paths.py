"""
``relationship_path(a, b)`` - how is A connected to B (R11 #616).

The shortest path between two entities across the corpus, over the co-mention
graph built from ``document_actors``: two entities are adjacent when they are
named in the same document, and that edge *carries its evidence*, the shared
documents (with citations) that establish it. Nothing on the path renders
without the documents that back it.

Resolution ambiguity is surfaced, not silently collapsed: when a name matches
several distinct entity ids, the candidates are reported rather than an
arbitrary one being chosen.

Pure composition, stdlib-only BFS; the connection is injected read-only.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from src.osint import common, evidence

MAX_NODES = 4000


def _resolve(conn, name: str) -> Dict[str, Any]:
    """Resolve a name to the entity used in the graph, surfacing ambiguity."""
    if not common.table_exists(conn, "document_actors"):
        return {"entity": name, "candidates": []}
    rows = conn.execute(
        "SELECT DISTINCT entity_id FROM document_actors WHERE actor_name = ? AND entity_id IS NOT NULL",
        [name],
    ).fetchall()
    candidates = [r[0] for r in rows if r[0]]
    return {"entity": name, "candidates": candidates, "ambiguous": len(candidates) > 1}


def _adjacency(conn) -> Tuple[Dict[str, set], Dict[Tuple[str, str], List[str]]]:
    """Co-mention adjacency plus, for each undirected pair, the documents that
    establish the edge."""
    adj: Dict[str, set] = {}
    edge_docs: Dict[Tuple[str, str], List[str]] = {}
    if not common.table_exists(conn, "document_actors"):
        return adj, edge_docs
    rows = conn.execute(
        "SELECT document_id, actor_name FROM document_actors "
        "WHERE actor_name IS NOT NULL"
    ).fetchall()
    by_doc: Dict[str, List[str]] = {}
    for document_id, actor in rows:
        by_doc.setdefault(document_id, [])
        if actor not in by_doc[document_id]:
            by_doc[document_id].append(actor)
    for document_id, actors in by_doc.items():
        for i in range(len(actors)):
            for j in range(i + 1, len(actors)):
                a, b = actors[i], actors[j]
                adj.setdefault(a, set()).add(b)
                adj.setdefault(b, set()).add(a)
                key = (a, b) if a <= b else (b, a)
                edge_docs.setdefault(key, [])
                if document_id not in edge_docs[key]:
                    edge_docs[key].append(document_id)
    return adj, edge_docs


def _bfs(adj: Dict[str, set], start: str, goal: str) -> Optional[List[str]]:
    if start not in adj or goal not in adj:
        return None
    if start == goal:
        return [start]
    prev: Dict[str, str] = {start: start}
    q = deque([start])
    seen = 0
    while q and seen < MAX_NODES:
        cur = q.popleft()
        seen += 1
        for nxt in adj.get(cur, ()):  # deterministic order below
            if nxt not in prev:
                prev[nxt] = cur
                if nxt == goal:
                    path = [goal]
                    while path[-1] != start:
                        path.append(prev[path[-1]])
                    return list(reversed(path))
                q.append(nxt)
    return None


def relationship_path(conn, a: str, b: str) -> Dict[str, Any]:
    """The shortest co-mention path from ``a`` to ``b`` with cited evidence on
    every edge, or a clear "no path" when they are not connected."""
    if not common.table_exists(conn, "document_actors"):
        return {"error": "no entity-mention layer available"}

    resolve_a = _resolve(conn, a)
    resolve_b = _resolve(conn, b)

    adj, edge_docs = _adjacency(conn)
    # Sort neighbours for deterministic shortest paths.
    adj = {k: sorted(v) for k, v in adj.items()}
    path = _bfs(adj, a, b)
    if path is None:
        return {
            "connected": False,
            "a": a,
            "b": b,
            "resolution": {"a": resolve_a, "b": resolve_b},
            "note": "no co-mention path found in the ingested corpus",
        }

    edges = []
    for i in range(len(path) - 1):
        x, y = path[i], path[i + 1]
        key = (x, y) if x <= y else (y, x)
        doc_ids = edge_docs.get(key, [])
        cites = evidence.document_citations(conn, doc_ids)
        edge_citations = [cites[d] for d in doc_ids if d in cites]
        edges.append(
            {
                "from": x,
                "to": y,
                "shared_documents": len(doc_ids),
                "evidence": edge_citations[:10],
                "uncited_count": evidence.uncited_count(edge_citations),
            }
        )

    return {
        "connected": True,
        "a": a,
        "b": b,
        "path": path,
        "hops": len(path) - 1,
        "edges": edges,
        "resolution": {"a": resolve_a, "b": resolve_b},
        "ambiguous": bool(resolve_a.get("ambiguous") or resolve_b.get("ambiguous")),
    }
