"""
Embedding-based topic modelling over the corpus.

The lexical narrative clustering in ``narratives.py`` groups documents by
bag-of-words cosine; its own docstring names the intended upgrade as clustering
over *document embeddings*. This is that: cluster the vectors in
``document_embeddings`` (written by :func:`src.ingestion.embed.embed_documents`)
into topics, and label each with its salient terms.

Model-optional, as elsewhere. The default clusterer is offline and
deterministic — connected components over an embedding-cosine threshold (the
same shape as the lexical narrative graph, but in embedding space). When
``clusterer="hdbscan"`` and the library is installed, density-based HDBSCAN is
used instead (better with noise/outliers); it falls back to components if
unavailable. Read-only: reads the embedding sink and the corpus, writes nothing.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

import numpy as np

from src.analytics.text import tokenize, top_terms
from src.database.news_articles_compat import corpus_table

# Import json lazily-safe (stdlib) for reading stored vectors.
import json


def _has_embeddings(conn) -> bool:
    try:
        return bool(conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'document_embeddings'"
        ).fetchone())
    except Exception:
        return False


def _normalise(mat: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return mat / norms


def _load_matrix(conn, model: Optional[str]):
    if model is not None:
        rows = conn.execute(
            "SELECT document_id, vector FROM document_embeddings WHERE model = ?", [model]
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT document_id, vector FROM document_embeddings"
        ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return ids, np.empty((0, 0))
    return ids, np.asarray([json.loads(r[1]) for r in rows], dtype=np.float64)


def _components(sims: np.ndarray, threshold: float) -> List[List[int]]:
    n = sims.shape[0]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            if sims[i, j] >= threshold:
                parent[find(i)] = find(j)
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _hdbscan_labels(mat: np.ndarray, min_cluster_size: int) -> Optional[List[int]]:
    try:
        import hdbscan

        clusterer = hdbscan.HDBSCAN(min_cluster_size=max(2, min_cluster_size),
                                    metric="euclidean")
        # Cosine on L2-normalised vectors is monotonic with euclidean distance.
        return list(clusterer.fit_predict(_normalise(mat)))
    except Exception:
        return None


def _label(conn, tbl: str, ids: List[str]) -> Dict[str, Any]:
    """Salient terms + a representative title for a cluster of documents."""
    ph = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, title, content FROM {tbl} WHERE id IN ({ph})", ids
    ).fetchall()
    counter: Counter = Counter()
    titles: Dict[str, str] = {}
    for doc_id, title, content in rows:
        counter.update(tokenize(f"{title or ''} {content or ''}"))
        titles[doc_id] = title
    terms = top_terms(dict(counter), n=6)
    return {"terms": terms, "label": " / ".join(terms[:3]) if terms else "(untitled)",
            "representative_title": next((titles.get(i) for i in ids if titles.get(i)), None)}


def model_topics(
    conn, min_similarity: float = 0.35, min_cluster_size: int = 3,
    model: Optional[str] = None, max_docs: int = 2000, clusterer: Optional[str] = None,
) -> Dict[str, Any]:
    """Cluster document embeddings into labelled topics.

    Args:
        min_similarity: cosine threshold for the default (components) clusterer.
        min_cluster_size: drop clusters smaller than this.
        model: restrict to one embedding model/space.
        clusterer: ``"hdbscan"`` to use density clustering when available, else
            the default connected-components clusterer.
    """
    if not _has_embeddings(conn):
        return {"topics": [], "count": 0,
                "note": "no embeddings indexed; run embed_documents first"}
    ids, mat = _load_matrix(conn, model)
    if len(ids) < min_cluster_size:
        return {"topics": [], "count": 0, "note": "too few embedded documents"}
    truncated = len(ids) > max_docs
    if truncated:
        ids, mat = ids[:max_docs], mat[:max_docs]

    method = "connected-components over embedding cosine"
    clusters: List[List[int]]
    if clusterer == "hdbscan":
        labels = _hdbscan_labels(mat, min_cluster_size)
        if labels is not None:
            method = "hdbscan over document embeddings"
            grouped: Dict[int, List[int]] = {}
            for idx, lab in enumerate(labels):
                if lab == -1:  # noise
                    continue
                grouped.setdefault(lab, []).append(idx)
            clusters = list(grouped.values())
        else:
            clusters = _components(_normalise(mat) @ _normalise(mat).T, min_similarity)
    else:
        clusters = _components(_normalise(mat) @ _normalise(mat).T, min_similarity)

    tbl = corpus_table(conn)
    topics = []
    for members in clusters:
        if len(members) < min_cluster_size:
            continue
        member_ids = [ids[i] for i in members]
        info = _label(conn, tbl, member_ids)
        topics.append({
            "topic_id": len(topics),
            "label": info["label"],
            "terms": info["terms"],
            "size": len(member_ids),
            "document_ids": member_ids[:50],
            "representative_title": info["representative_title"],
        })
    topics.sort(key=lambda t: -t["size"])
    for i, t in enumerate(topics):
        t["topic_id"] = i
    return {
        "topics": topics,
        "count": len(topics),
        "method": method,
        "truncated": truncated,
        "caveat": "topics are unsupervised clusters over document embeddings; "
                  "labels are the clusters' salient terms, not curated categories",
    }
