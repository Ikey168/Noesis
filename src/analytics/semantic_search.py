"""
Semantic search over the document embedding sink.

Brute-force cosine similarity over the vectors in ``document_embeddings``
(written by :func:`src.ingestion.embed.embed_documents`), joined back to the
corpus for citation metadata. No vector-index extension required — for a modest
corpus a numpy matrix-multiply is ample, mirroring the style of the existing
analytics (e.g. image-reuse union-find).

Three entry points:

* :func:`semantic_search` - documents most similar to a free-text query,
* :func:`similar_documents` - documents most similar to a given document,
* :func:`near_duplicates` - clusters of near-identical documents.

Query and corpus vectors must live in the same embedding space; pass the same
``provider``/``model`` used to index. Read-only: these functions never create or
write tables (the sink is read directly, guarded), so they run against a
read-only warehouse connection. Stdlib + numpy.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import numpy as np

from src.database.news_articles_compat import corpus_table


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


def _load_matrix(
    conn, model: Optional[str], document_ids: Optional[List[str]] = None
):
    clauses = []
    params: List[Any] = []
    if model is not None:
        clauses.append("(model = ? OR ends_with(model, ':' || ?))")
        params.extend([model, model])
    if document_ids is not None:
        if not document_ids:
            return [], np.empty((0, 0))
        placeholders = ", ".join("?" for _ in document_ids)
        clauses.append(f"document_id IN ({placeholders})")
        params.extend(document_ids)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        "SELECT document_id, vector FROM document_embeddings" + where,
        params,
    ).fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return ids, np.empty((0, 0))
    return ids, np.asarray([json.loads(r[1]) for r in rows], dtype=np.float64)


def _get_vector(conn, document_id: str):
    row = conn.execute(
        "SELECT model, vector FROM document_embeddings WHERE document_id = ?", [document_id]
    ).fetchone()
    if row is None:
        return None, None
    return row[0], np.asarray(json.loads(row[1]), dtype=np.float64)


def _metadata(conn, ids: List[str]) -> Dict[str, Dict[str, Any]]:
    if not ids:
        return {}
    tbl = corpus_table(conn)
    ph = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id, title, source, url FROM {tbl} WHERE id IN ({ph})", ids
    ).fetchall()
    return {r[0]: {"title": r[1], "source": r[2], "url": r[3]} for r in rows}


def _hits(conn, ids, sims, order, top_k, exclude=None):
    take = [ids[i] for i in order[: top_k + (1 if exclude else 0)]]
    meta = _metadata(conn, take)
    out = []
    for i in order:
        doc_id = ids[i]
        if exclude is not None and doc_id == exclude:
            continue
        m = meta.get(doc_id, {})
        out.append({
            "document_id": doc_id,
            "score": round(float(sims[i]), 4),
            "title": m.get("title"),
            "source": m.get("source") or "unknown",
            "url": m.get("url"),
        })
        if len(out) >= top_k:
            break
    return out


def semantic_search(
    conn, query: str, top_k: int = 10, provider: Optional[Any] = None,
    model: Optional[str] = None, document_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Documents most semantically similar to ``query``.

    Embeds ``query`` with ``provider`` (default: env-configured) and ranks the
    stored document vectors by cosine similarity. ``model`` filters the sink to
    one embedding space (recommended when several were indexed).
    ``document_ids`` constrains the candidate set before ranking, which lets
    domain and namespace backings preserve their authorization boundary."""
    if conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='document_chunk_embeddings'").fetchone():
        from src.ingestion.chunk_embeddings import search_document_chunks
        if provider is None:
            from services.embeddings.provider import get_embedding_provider
            provider = get_embedding_provider()
        if model and provider.name() != model and not provider.name().endswith(":" + model):
            return {"results": [], "count": 0, "error": "query provider differs from requested model", "code": "model_mismatch", "coverage": {"complete": False}}
        return search_document_chunks(conn, query, provider, top_k=top_k, document_ids=document_ids)
    if not _has_embeddings(conn):
        return {"results": [], "count": 0, "query": query,
                "note": "no embeddings indexed; run embed_documents first", "coverage": {"complete": False}}
    ids, mat = _load_matrix(conn, model, document_ids)
    if not ids:
        return {"results": [], "count": 0, "query": query,
                "note": "no embeddings indexed; run embed_documents first"}

    if provider is None:
        from services.embeddings.provider import get_embedding_provider

        provider = get_embedding_provider()
    qvec = np.asarray(getattr(provider, "embed_queries", provider.embed_texts)([query])[0], dtype=np.float64)
    if qvec.shape[0] != mat.shape[1]:
        return {"error": "query/corpus embedding dimensions differ; index and "
                         "query with the same model", "code": "dim_mismatch",
                "query_dim": int(qvec.shape[0]), "corpus_dim": int(mat.shape[1])}

    sims = _normalise(mat) @ (qvec / (np.linalg.norm(qvec) or 1.0))
    order = np.argsort(-sims)
    return {"results": _hits(conn, ids, sims, order, top_k), "count": min(top_k, len(ids)),
            "query": query, "model": model, "method": "cosine over document embeddings",
            "coverage": {"complete": True, "full_document": False}}


def similar_documents(
    conn, document_id: str, top_k: int = 10, model: Optional[str] = None,
) -> Dict[str, Any]:
    """Documents most similar to ``document_id`` (excludes the document itself)."""
    if not _has_embeddings(conn):
        return {"error": f"document {document_id!r} has no embedding", "code": "not_indexed",
                "document_id": document_id}
    doc_model, qvec = _get_vector(conn, document_id)
    if qvec is None:
        return {"error": f"document {document_id!r} has no embedding", "code": "not_indexed",
                "document_id": document_id}
    ids, mat = _load_matrix(conn, model or doc_model)
    sims = _normalise(mat) @ (qvec / (np.linalg.norm(qvec) or 1.0))
    order = np.argsort(-sims)
    return {"results": _hits(conn, ids, sims, order, top_k, exclude=document_id),
            "count": top_k, "document_id": document_id,
            "method": "cosine over document embeddings"}


def near_duplicates(
    conn, threshold: float = 0.9, model: Optional[str] = None, max_docs: int = 2000,
) -> Dict[str, Any]:
    """Clusters of near-identical documents (cosine >= ``threshold``).

    Pairwise cosine over the (capped) embedding set, connected components as
    clusters. ``threshold`` near 1.0 finds near-exact reuse; lower values find
    looser echoes. Note the ``max_docs`` cap keeps the O(n^2) scan bounded."""
    if not _has_embeddings(conn):
        return {"clusters": [], "count": 0, "note": "no embeddings indexed"}
    ids, mat = _load_matrix(conn, model)
    if len(ids) < 2:
        return {"clusters": [], "count": 0, "note": "fewer than two embedded documents"}
    truncated = len(ids) > max_docs
    if truncated:
        ids, mat = ids[:max_docs], mat[:max_docs]

    norm = _normalise(mat)
    sims = norm @ norm.T
    parent = list(range(len(ids)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if sims[i, j] >= threshold:
                parent[find(i)] = find(j)

    groups: Dict[int, List[int]] = {}
    for i in range(len(ids)):
        groups.setdefault(find(i), []).append(i)

    meta = _metadata(conn, ids)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        clusters.append({
            "document_ids": [ids[i] for i in members],
            "size": len(members),
            "sources": sorted({(meta.get(ids[i]) or {}).get("source") or "unknown"
                               for i in members}),
        })
    clusters.sort(key=lambda c: -c["size"])
    return {"clusters": clusters, "count": len(clusters), "threshold": threshold,
            "truncated": truncated,
            "note": "near-duplicate clusters by embedding cosine; shared vectors "
                    "can be coincidental (wire copy, quotations)"}
