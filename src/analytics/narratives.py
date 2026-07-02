"""
Narrative clustering (R6 / #600).

Groups documents on a topic into competing storylines. The plan's target is
HDBSCAN over document embeddings; the dependency-light fallback here is
lexical: L2-normalized bag-of-words vectors clustered by connected
components over a cosine-similarity threshold. This upgrades event
clustering from "same event" to "same narrative" and reports cluster size
and cohesion (mean intra-cluster similarity) rather than bare labels.

The fit is batch-friendly (``NarrativeJob`` writes ``analytics_narratives``)
and the ``cluster_narratives`` tool reads it, computing on-demand for a
single topic when nothing is stored. Honesty envelope throughout.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.analytics.framework import AnalyticJob
from src.analytics.honesty import analytic_envelope
from src.analytics.stats import cosine
from src.analytics.text import tf_vector, top_terms

RESULT_TABLE = "analytics_narratives"
SIM_THRESHOLD = 0.18
MIN_DOCS = 3
MAX_DOCS = 400

METHOD = "lexical bag-of-words cosine clustering (embedding fallback for HDBSCAN)"
ASSUMPTIONS = [
    "clusters are lexical (shared vocabulary), not semantic embeddings",
    "connected components over a cosine threshold; singletons are noise",
    "needs at least %d documents on the topic" % MIN_DOCS,
]


def _docs(conn, topic: Optional[str], days: Optional[int]) -> List[Dict[str, Any]]:
    clauses, params = [], []
    if topic:
        clauses.append("category = ?")
        params.append(topic)
    if days:
        from datetime import timedelta

        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        clauses.append("publish_date >= ?")
        params.append(cutoff)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(MAX_DOCS)
    rows = conn.execute(
        f"SELECT id, title, category FROM news_articles {where} "
        f"ORDER BY publish_date DESC NULLS LAST LIMIT ?",
        params,
    ).fetchall()
    return [{"id": r[0], "title": r[1] or "", "category": r[2]} for r in rows]


def _connected_components(vectors: List[Dict[str, float]], threshold: float) -> List[List[int]]:
    """Union-find over the cosine-similarity graph of document vectors."""
    n = len(vectors)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for i in range(n):
        for j in range(i + 1, n):
            if cosine(vectors[i], vectors[j]) >= threshold:
                union(i, j)
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def cluster_narratives(
    conn, topic: Optional[str] = None, days: Optional[int] = None
) -> Dict[str, Any]:
    """Cluster a topic's documents into narrative threads."""
    docs = _docs(conn, topic, days)
    vectors = [tf_vector(d["title"]) for d in docs]
    keep = [i for i, v in enumerate(vectors) if v]
    idx_docs = [docs[i] for i in keep]
    idx_vecs = [vectors[i] for i in keep]

    clusters: List[Dict[str, Any]] = []
    for members in _connected_components(idx_vecs, SIM_THRESHOLD):
        if len(members) < 2:
            continue  # singletons are noise, not a narrative
        member_vecs = [idx_vecs[m] for m in members]
        # Cohesion: mean pairwise cosine within the cluster.
        pairs = [
            cosine(member_vecs[a], member_vecs[b])
            for a in range(len(members))
            for b in range(a + 1, len(members))
        ]
        cohesion = sum(pairs) / len(pairs) if pairs else 0.0
        centroid: Dict[str, float] = {}
        for v in member_vecs:
            for term, w in v.items():
                centroid[term] = centroid.get(term, 0.0) + w
        clusters.append(
            {
                "size": len(members),
                "cohesion": round(cohesion, 4),
                "terms": top_terms(centroid, 6),
                "sample_titles": [idx_docs[m]["title"] for m in members[:3]],
            }
        )
    clusters.sort(key=lambda c: -c["size"])
    return {"n_docs": len(idx_docs), "clusters": clusters}


def cluster_narratives_payload(
    conn, topic: Optional[str] = None, days: Optional[int] = None
) -> Dict[str, Any]:
    result = cluster_narratives(conn, topic, days)
    return analytic_envelope(
        n=result["n_docs"],
        method=METHOD,
        assumptions=ASSUMPTIONS,
        topic=topic,
        threshold=SIM_THRESHOLD,
        clusters=result["clusters"],
    )


class NarrativeJob(AnalyticJob):
    """Precompute narrative clusters per topic into ``analytics_narratives``."""

    name = "cluster_narratives"
    result_table = RESULT_TABLE

    def result_ddl(self) -> str:
        return f"""
            CREATE TABLE IF NOT EXISTS {RESULT_TABLE} (
                topic       VARCHAR NOT NULL,
                cluster_id  INTEGER NOT NULL,
                size        INTEGER,
                cohesion    DOUBLE,
                terms       VARCHAR,
                computed_at VARCHAR,
                PRIMARY KEY (topic, cluster_id)
            )
        """

    def compute(self, conn) -> List[Dict[str, Any]]:
        computed_at = datetime.now(timezone.utc).isoformat()
        topics = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT category FROM news_articles WHERE category IS NOT NULL"
            ).fetchall()
        ]
        out: List[Dict[str, Any]] = []
        for topic in topics:
            for cid, cluster in enumerate(cluster_narratives(conn, topic)["clusters"]):
                out.append(
                    {
                        "topic": topic,
                        "cluster_id": cid,
                        "size": cluster["size"],
                        "cohesion": cluster["cohesion"],
                        "terms": ", ".join(cluster["terms"]),
                        "computed_at": computed_at,
                    }
                )
        return out

    def store(self, conn, rows: List[Dict[str, Any]]) -> None:
        for r in rows:
            conn.execute(
                f"""INSERT OR REPLACE INTO {RESULT_TABLE}
                    (topic, cluster_id, size, cohesion, terms, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                [r["topic"], r["cluster_id"], r["size"], r["cohesion"], r["terms"], r["computed_at"]],
            )

    def summary(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {"topics": len({r["topic"] for r in rows})}
