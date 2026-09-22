"""
Claim clusters: the presentation-time merge over the link graph.

Storage never merges (#964 writes links only); this module derives the
merged reading experience consumers actually want. A **cluster** is a
connected component over ``duplicate`` links: one representative claim plus
every citation, with corroboration and contradiction as cluster-level
properties. This is where information overload actually drops — six copies
of a story collapse to one entry with a source list.

- **Cluster ids** are ``cl-<min claim_id in component>``: deterministic, and
  stable as long as the smallest member stays; when two clusters merge, the
  smaller id wins, so one side keeps its identity.
- **Representative** = recency blended with source quality, using the
  existing ``outlet_scores`` transparency scores as the quality input.
  Superseded claims are never chosen while a live member exists.
- **Corroboration** = probable reporting origins per cluster when lineage is
  materialized, otherwise the explicitly identified distinct-source fallback.
- **Contradictions** = ``contradicts`` links whose endpoints land in
  different clusters, cited on both sides.
- **Supersedence** honoured at presentation: superseded members are marked
  historical, never silently dropped.

Singleton claims (no duplicate links) are clusters of one at read time —
they get no ``claim_clusters`` row, keeping the table proportional to the
linked subgraph.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple

_CLUSTERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_clusters (
    claim_id    TEXT PRIMARY KEY,
    cluster_id  TEXT NOT NULL,
    run_id      TEXT,
    assigned_at BIGINT NOT NULL
)
"""


def ensure_cluster_schema(conn) -> None:
    from src.kb.claim_links import ensure_claim_link_schema

    ensure_claim_link_schema(conn)
    conn.execute(_CLUSTERS_SCHEMA)


class _UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            # Smaller id becomes the root so cluster ids stay deterministic.
            if root_b < root_a:
                root_a, root_b = root_b, root_a
            self.parent[root_b] = root_a


def run_clustering_pass(conn, run_id: Optional[str] = None) -> Dict[str, Any]:
    """(Re)derive clusters from the duplicate-link graph.

    Recomputes components over all duplicate links — the link graph is the
    source of truth, so this is idempotent and self-healing after
    :func:`src.kb.claim_links.delete_run`.
    """
    ensure_cluster_schema(conn)
    run_id = run_id or f"kb-clusters-{uuid.uuid4().hex[:12]}"

    links = conn.execute(
        "SELECT claim_a, claim_b FROM claim_links WHERE relation = 'duplicate'"
    ).fetchall()

    uf = _UnionFind()
    for claim_a, claim_b in links:
        uf.union(claim_a, claim_b)

    members = sorted(uf.parent.keys())
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM claim_clusters")
        now = int(time.time() * 1000)
        for claim_id in members:
            conn.execute(
                "INSERT INTO claim_clusters (claim_id, cluster_id, run_id, assigned_at)"
                " VALUES (?, ?, ?, ?)",
                [claim_id, f"cl-{uf.find(claim_id)}", run_id, now],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    clusters = {uf.find(claim_id) for claim_id in members}
    return {"run_id": run_id, "linked_claims": len(members), "clusters": len(clusters)}


def _outlet_quality(conn) -> Dict[str, float]:
    """Latest composite transparency score per source (normalized name)."""
    rows = conn.execute(
        """
        SELECT source, composite_score FROM outlet_scores
        WHERE (source, score_date) IN (
            SELECT source, MAX(score_date) FROM outlet_scores GROUP BY source
        )
        """
    ).fetchall()
    return {
        str(source).strip().lower(): float(score)
        for source, score in rows
        if score is not None
    }


def _superseded_claims(conn) -> Set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT claim_b FROM claim_links "
            "WHERE relation IN ('supersedes', 'corrects', 'retracts')"
        ).fetchall()
    }


def cluster_claims(
    conn,
    domain: Optional[str] = None,
    limit: int = 50,
    since: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Clustered, cited claims — the presentation merge.

    ``domain`` filters members through ``document_domains``; ``since`` is an
    epoch-ms floor on the member document's ingestion time (a cluster
    surfaces when *any* member passes). Clusters are ordered by their newest
    member's arrival, newest first.
    """
    ensure_cluster_schema(conn)

    domain_filter = ""
    params: List[Any] = []
    if domain:
        domain_filter = (
            "AND c.document_id IN"
            " (SELECT document_id FROM document_domains WHERE domain = ?)"
        )
        params.append(domain)

    rows = conn.execute(
        f"""
        SELECT c.claim_id, c.claim_text, c.document_id, c.confidence,
               COALESCE(c.prediction_mode, 'unknown') AS prediction_mode,
               COALESCE(cl.cluster_id, 'cl-' || c.claim_id) AS cluster_id,
               COALESCE(d.source_id, '') AS source_id,
               d.url, d.title, COALESCE(d.ingested_at, 0) AS ingested_at,
               d.source_type
        FROM argument_claims c
        LEFT JOIN claim_clusters cl ON cl.claim_id = c.claim_id
        LEFT JOIN documents d ON d.document_id = c.document_id
        WHERE 1 = 1 {domain_filter}
        """,
        params,
    ).fetchall()
    if not rows:
        return []

    quality = _outlet_quality(conn)
    superseded = _superseded_claims(conn)

    grouped: Dict[str, List[Tuple]] = {}
    for row in rows:
        grouped.setdefault(row[5], []).append(row)

    newest = {
        cluster_id: max(member[9] for member in members)
        for cluster_id, members in grouped.items()
    }
    if since is not None:
        grouped = {
            cluster_id: members
            for cluster_id, members in grouped.items()
            if newest[cluster_id] >= since
        }

    contradiction_rows = conn.execute(
        """
        SELECT l.claim_a, l.claim_b, l.confidence, l.prediction_mode,
               a.claim_text, b.claim_text
        FROM claim_links l
        JOIN argument_claims a ON a.claim_id = l.claim_a
        JOIN argument_claims b ON b.claim_id = l.claim_b
        WHERE l.relation = 'contradicts'
        """
    ).fetchall()

    clusters: List[Dict[str, Any]] = []
    for cluster_id, members in grouped.items():
        citations = []
        for (claim_id, text, document_id, confidence, prediction_mode, _cl, source_id,
             url, title, ingested_at, source_type) in sorted(
                members, key=lambda member: member[9], reverse=True):
            citations.append(
                {
                    "claim_id": claim_id,
                    "claim_text": text,
                    "document_id": document_id,
                    "source": source_id,
                    "url": url,
                    "title": title,
                    "ingested_at": ingested_at,
                    "confidence": confidence,
                    "prediction_mode": prediction_mode,
                    "source_type": source_type,
                    "superseded": claim_id in superseded,
                }
            )

        live = [c for c in citations if not c["superseded"]] or citations
        max_ingested = max(c["ingested_at"] for c in citations) or 1

        def rank(citation: Dict[str, Any]) -> float:
            recency = (
                citation["ingested_at"] / max_ingested if max_ingested else 0.0
            )
            source_quality = quality.get(
                str(citation["source"]).strip().lower(), 0.5
            )
            return 0.6 * recency + 0.4 * source_quality

        representative = max(live, key=rank)
        member_ids = {c["claim_id"] for c in citations}
        contradictions = []
        for (claim_a, claim_b, confidence, mode, text_a, text_b) in contradiction_rows:
            if claim_a in member_ids and claim_b not in member_ids:
                contradictions.append(
                    {"claim_id": claim_b, "claim_text": text_b,
                     "confidence": confidence, "prediction_mode": mode}
                )
            elif claim_b in member_ids and claim_a not in member_ids:
                contradictions.append(
                    {"claim_id": claim_a, "claim_text": text_a,
                     "confidence": confidence, "prediction_mode": mode}
                )

        from src.osint.independence import origin_summary

        independence = origin_summary(
            conn,
            [citation.get("document_id") for citation in citations],
            sources=[citation.get("source") for citation in citations],
        )
        corroboration = independence["independent_source_count"]
        clusters.append(
            {
                "cluster_id": cluster_id,
                "representative": representative,
                "citations": citations,
                "corroboration": corroboration,
                "independence": independence,
                "contradictions": contradictions,
                "size": len(citations),
                "last_ingested_ms": newest[cluster_id],
            }
        )

    clusters.sort(key=lambda cluster: cluster["last_ingested_ms"], reverse=True)
    return clusters[: int(limit)]
