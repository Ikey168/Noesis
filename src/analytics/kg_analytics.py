"""
Knowledge-graph analytics payloads (R6 / #601).

Honesty-wrapped ``kg_communities`` and ``kg_centrality`` over a co-mention
graph (an edge list + node-name map the KG tools build from the store). The
graph algorithms live in :mod:`src.analytics.graph`; this module shapes
their output for the canvas — communities to colour the entity graph,
PageRank centrality to size its nodes.

Every payload takes an optional ``kg`` namespace label so Track-P
provisioned graphs get the same analytics for free.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from src.analytics.graph import degree, label_propagation, pagerank
from src.analytics.honesty import analytic_envelope

COMMUNITIES_METHOD = "label-propagation community detection (Leiden fallback)"
COMMUNITIES_ASSUMPTIONS = [
    "communities are label-propagation clusters, not modularity-optimal",
    "undirected co-mention graph; edge weights are ignored",
    "deterministic (seeded scan order) for reproducible colouring",
]

CENTRALITY_METHOD = "PageRank centrality on the co-mention graph"
CENTRALITY_ASSUMPTIONS = [
    "undirected graph with damping 0.85",
    "centrality is structural (co-mention), not semantic importance",
]

MAX_NODES = 60


def kg_communities_payload(
    nodes: List[str],
    edges: List[Tuple[str, str]],
    names: Optional[Dict[str, str]] = None,
    kg: Optional[str] = None,
) -> Dict[str, Any]:
    """Community assignments over the co-mention graph."""
    names = names or {}
    communities = label_propagation(nodes, edges)
    grouped: Dict[int, List[str]] = {}
    for node, cid in communities.items():
        grouped.setdefault(cid, []).append(node)
    summary = [
        {
            "community": cid,
            "size": len(members),
            "members": [names.get(m, m) for m in members[:5]],
        }
        for cid, members in sorted(grouped.items(), key=lambda kv: -len(kv[1]))
    ]
    assignments = [
        {"node": names.get(n, n), "community": communities[n]}
        for n in list(communities)[:MAX_NODES]
    ]
    return analytic_envelope(
        n=len(nodes),
        method=COMMUNITIES_METHOD,
        assumptions=COMMUNITIES_ASSUMPTIONS,
        kg=kg,
        community_count=len(grouped),
        communities=summary,
        assignments=assignments,
    )


def kg_centrality_payload(
    nodes: List[str],
    edges: List[Tuple[str, str]],
    names: Optional[Dict[str, str]] = None,
    kg: Optional[str] = None,
    top: int = 20,
) -> Dict[str, Any]:
    """Top nodes by PageRank centrality, with degree and community colour."""
    names = names or {}
    rank = pagerank(nodes, edges)
    deg = degree(nodes, edges)
    communities = label_propagation(nodes, edges)
    ordered = sorted(rank, key=lambda n: -rank[n])[: max(1, top)]
    top_nodes = [
        {
            "node": names.get(n, n),
            "centrality": round(rank[n], 6),
            "degree": deg.get(n, 0),
            "community": communities.get(n, 0),
        }
        for n in ordered
    ]
    return analytic_envelope(
        n=len(nodes),
        method=CENTRALITY_METHOD,
        assumptions=CENTRALITY_ASSUMPTIONS,
        kg=kg,
        edges=len(list(edges)),
        nodes_ranked=top_nodes,
    )
