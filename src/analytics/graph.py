"""
Graph analytics for the knowledge graph (R6 / Track DS Wave 1b, #601).

Pure-stdlib PageRank centrality and label-propagation community detection
over an undirected co-mention graph — the dependency-light house style, so
the KG tools stay import-safe without networkx. Both take a plain edge list
so they work over the default KG or any Track-P-provisioned namespace.

Communities: a deterministic, seeded variant of label propagation (each
node adopts the most common label among its neighbours, ties broken by the
lowest label id, iterated to convergence). PageRank: power iteration on the
symmetric adjacency with the standard damping factor. Both are cheap and
good enough to *colour and size* an entity graph; they are not a substitute
for Leiden at research scale, which the assumptions disclose.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List, Tuple

_EPS = 1e-9


def _adjacency(
    nodes: Iterable[str], edges: Iterable[Tuple[str, str]]
) -> Dict[str, set]:
    """Undirected adjacency map; self-loops and unknown endpoints dropped."""
    adj: Dict[str, set] = {n: set() for n in nodes}
    for a, b in edges:
        if a == b or a not in adj or b not in adj:
            continue
        adj[a].add(b)
        adj[b].add(a)
    return adj


def pagerank(
    nodes: Iterable[str],
    edges: Iterable[Tuple[str, str]],
    damping: float = 0.85,
    iterations: int = 100,
    tol: float = 1e-9,
) -> Dict[str, float]:
    """PageRank over the undirected co-mention graph (power iteration).

    Isolated nodes keep the teleport mass; the vector sums to 1. Returns
    ``{}`` for an empty graph.
    """
    adj = _adjacency(nodes, edges)
    node_list = list(adj)
    n = len(node_list)
    if n == 0:
        return {}
    rank = {node: 1.0 / n for node in node_list}
    base = (1.0 - damping) / n
    for _ in range(iterations):
        dangling = damping * sum(rank[u] for u in node_list if not adj[u]) / n
        nxt = {}
        for node in node_list:
            incoming = sum(rank[u] / len(adj[u]) for u in adj[node]) if adj[node] else 0.0
            nxt[node] = base + dangling + damping * incoming
        delta = sum(abs(nxt[node] - rank[node]) for node in node_list)
        rank = nxt
        if delta < tol:
            break
    total = sum(rank.values()) or 1.0
    return {node: value / total for node, value in rank.items()}


def label_propagation(
    nodes: Iterable[str],
    edges: Iterable[Tuple[str, str]],
    iterations: int = 50,
) -> Dict[str, int]:
    """Deterministic label-propagation communities.

    Each node starts in its own community and repeatedly adopts the most
    common label among its neighbours (ties broken by the smallest label),
    scanning nodes in sorted order for reproducibility. Returns a
    node -> community-id map with community ids renumbered from 0 by size.
    """
    adj = _adjacency(nodes, edges)
    node_list = sorted(adj)
    if not node_list:
        return {}
    index = {node: i for i, node in enumerate(node_list)}
    labels = {node: index[node] for node in node_list}

    for _ in range(iterations):
        changed = False
        for node in node_list:
            if not adj[node]:
                continue
            counts: Dict[int, int] = defaultdict(int)
            for nbr in adj[node]:
                counts[labels[nbr]] += 1
            # Most frequent label, ties broken by the smallest label id.
            best = min(counts, key=lambda lab: (-counts[lab], lab))
            if best != labels[node]:
                labels[node] = best
                changed = True
        if not changed:
            break

    # Renumber communities 0..k-1, largest first (stable, presentable ids).
    groups: Dict[int, List[str]] = defaultdict(list)
    for node, lab in labels.items():
        groups[lab].append(node)
    order = sorted(groups, key=lambda lab: (-len(groups[lab]), lab))
    remap = {lab: i for i, lab in enumerate(order)}
    return {node: remap[lab] for node, lab in labels.items()}


def degree(nodes: Iterable[str], edges: Iterable[Tuple[str, str]]) -> Dict[str, int]:
    """Undirected degree per node."""
    adj = _adjacency(nodes, edges)
    return {node: len(neighbours) for node, neighbours in adj.items()}
