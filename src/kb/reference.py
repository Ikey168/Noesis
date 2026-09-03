"""
The reference domain: papers and books as a namespace-backed corpus.

``papers`` starts life as a corpus-view over arXiv RSS abstracts (#963).
This module stands it up as the first **namespace-backed** domain — the
first real exercise of the promotion pointer-flip — and gives it the
lifecycle a long-lived reference corpus needs:

- :func:`stand_up_reference` — promote the domain into its provisioned
  namespace (id-preserving copy + config flip, :mod:`src.kb.promotion`).
- :func:`ingest_documents_into_namespace` — namespace-native ingest for any
  connector's ``Document`` stream (books via the section-aware
  ``BookConnector``, full-text papers via the ``PaperConnector``), with
  enrichment parity: model-backed claim extraction into the namespace claims
  table and embeddings into the shared space so cross-backing similarity
  works. Citations point to the section: each book Document *is* a
  chapter/section with its ``section_path`` in metadata and title.
- :func:`sync_namespace_from_feeds` — keep the namespace fed after
  promotion: feed harvests still land in the shared sink, and this copies
  rows carrying the domain's tags across (idempotent by id).

Depth linkage — a paper claim contradicting today's news claim, queryable
from the news side — is the cross-backing pass (#967's
``run_cross_backing_link_pass``); this module just makes sure the reference
side holds full-text, claims, and vectors for it to bite on.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.kb.registry import load_registry


def _tables_for(conn, definition) -> Dict[str, str]:
    from src.provisioning.namespaces import (
        BACKEND_ATTACHED,
        BACKEND_TABLE_PREFIX,
        create_namespace,
    )
    from src.kb.promotion import _extend_documents_table

    backend = (
        BACKEND_ATTACHED
        if definition.namespace_backend == "attached"
        else BACKEND_TABLE_PREFIX
    )
    tables = create_namespace(conn, definition.namespace, backend)
    _extend_documents_table(conn, tables["documents"])
    return tables


def stand_up_reference(
    conn,
    config_path: Path,
    domain: str = "papers",
    backend: str = "table-prefix",
) -> Dict[str, Any]:
    """Promote the papers domain into its namespace (the pointer flip)."""
    from src.kb.promotion import promote_to_namespace

    return promote_to_namespace(conn, domain, Path(config_path), backend=backend)


def _extract_claims(
    text: str,
    max_sentences: int = 200,
    claim_detector: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Extract claims with the configured trained-model backend."""
    from src.argument_mining.models import get_claim_detector

    detector = claim_detector or get_claim_detector()
    claims = []
    sentences = [
        sentence.strip()
        for sentence in text.split(". ")
        if len(sentence.strip()) > 30
    ]
    for sentence in sentences[:max_sentences]:
        prediction = detector.predict_text(sentence)
        if getattr(prediction, "is_claim", False):
            claims.append(
                {
                    "text": sentence[:500],
                    "confidence": float(getattr(prediction, "confidence", 0.5)),
                }
            )
    return claims


def ingest_documents_into_namespace(
    conn,
    config_path: Path,
    documents: Iterable[Any],
    domain: str = "papers",
    provider: Optional[Any] = None,
    embedding_model: str = "all-MiniLM-L6-v2",
    extract_claims: bool = True,
    claim_detector: Optional[Any] = None,
) -> Dict[str, Any]:
    """Namespace-native ingest with enrichment parity.

    ``documents`` are connector ``Document`` objects (or dicts with the same
    fields). Idempotent by ``document_id``. When ``provider`` is given, each
    new document's text is embedded into the shared ``document_embeddings``
    table under ``embedding_model``, keeping the one-embedding-space rule.
    """
    from src.ingestion.embedding_store import EmbeddingStore

    registry = load_registry(config_path)
    definition = registry.get(domain)
    if definition.backing != "namespace":
        raise ValueError(
            f"domain {domain!r} is not namespace-backed; run stand_up_reference first"
        )
    tables = _tables_for(conn, definition)

    now_ms = int(time.time() * 1000)
    summary = {"documents": 0, "claims": 0, "embedded": 0, "skipped": 0}
    new_texts: List[Any] = []

    for document in documents:
        get = (
            document.get
            if isinstance(document, dict)
            else lambda key, default=None, d=document: getattr(d, key, default)
        )
        doc_id = get("document_id")
        if not doc_id:
            summary["skipped"] += 1
            continue
        exists = conn.execute(
            f"SELECT 1 FROM {tables['documents']} WHERE id = ?", [doc_id]
        ).fetchone()
        if exists:
            summary["skipped"] += 1
            continue

        content = get("content") or ""
        created_at = get("created_at")
        conn.execute(
            f"""
            INSERT INTO {tables['documents']}
                (id, title, source, source_type, url, published_at, routed_at,
                 content, ingested_at)
            VALUES (?, ?, ?, ?, ?,
                    CASE WHEN ? IS NULL THEN NULL ELSE to_timestamp(? / 1000.0) END,
                    now(), ?, ?)
            """,
            [
                doc_id, get("title"), get("source_id"), get("source_type"),
                get("url"), created_at, created_at, content,
                get("ingested_at") or now_ms,
            ],
        )
        summary["documents"] += 1
        if content:
            new_texts.append((doc_id, f"{get('title') or ''}\n{content}"[:4000]))

        if extract_claims and content:
            for index, claim in enumerate(_extract_claims(
                content, claim_detector=claim_detector
            )):
                conn.execute(
                    f"""
                    INSERT INTO {tables['claims']}
                        (claim_id, claim_text, verdict, document_id, routed_at)
                    VALUES (?, ?, NULL, ?, now())
                    """,
                    [f"{doc_id}#claim-{index}", claim["text"], doc_id],
                )
                summary["claims"] += 1

    if provider is not None and new_texts:
        store = EmbeddingStore(conn)
        vectors = provider.embed_texts([text for _, text in new_texts])
        for (doc_id, _), vector in zip(new_texts, vectors):
            store.upsert(
                doc_id,
                model=embedding_model,
                vector=[float(component) for component in vector],
            )
            summary["embedded"] += 1

    return summary


def sync_namespace_from_feeds(
    conn,
    config_path: Path,
    domain: str = "papers",
) -> Dict[str, Any]:
    """Copy tagged shared-corpus arrivals into the namespace (post-promotion).

    Feed harvests keep landing in the shared ``documents`` sink; a
    namespace-backed domain no longer reads it, so this copies rows whose
    feed tags overlap the domain's tags across, ids preserved, idempotent.
    """
    registry = load_registry(config_path)
    definition = registry.get(domain)
    if definition.backing != "namespace":
        raise ValueError(f"domain {domain!r} is not namespace-backed")
    tables = _tables_for(conn, definition)

    domain_tags = {tag.lower() for tag in definition.tags}
    rows = conn.execute(
        f"""
        SELECT d.document_id, d.title, d.source_id, d.source_type, d.url,
               d.created_at, d.content, COALESCE(d.ingested_at, 0), d.metadata
        FROM documents d
        WHERE d.document_id NOT IN (SELECT id FROM {tables['documents']})
        """
    ).fetchall()

    copied = 0
    for (doc_id, title, source_id, source_type, url, created_at,
         content, ingested_at, metadata_json) in rows:
        tags: set = set()
        if metadata_json:
            try:
                metadata = json.loads(metadata_json)
                raw_tags = metadata.get("tags") if isinstance(metadata, dict) else None
                if isinstance(raw_tags, list):
                    tags = {str(tag).lower() for tag in raw_tags}
            except (TypeError, ValueError):
                pass
        if not (domain_tags & tags):
            continue
        conn.execute(
            f"""
            INSERT INTO {tables['documents']}
                (id, title, source, source_type, url, published_at, routed_at,
                 content, ingested_at)
            VALUES (?, ?, ?, ?, ?,
                    CASE WHEN ? IS NULL THEN NULL ELSE to_timestamp(? / 1000.0) END,
                    now(), ?, ?)
            """,
            [doc_id, title, source_id, source_type, url,
             created_at, created_at, content, ingested_at],
        )
        copied += 1
    return {"copied": copied, "scanned": len(rows)}
