"""
Generic document routes — work regardless of which domain packs are enabled.

Provides CRUD and enrichment endpoints over the ``documents`` abstraction
(document-ingest-v1 contract). News-specific endpoints live in the news domain
pack routes and are only registered when that pack is enabled.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field

from services.ingest.common.document_model import SOURCE_TYPES
from src.ingestion.document_store import DocumentStore

router = APIRouter(prefix="/documents", tags=["documents"])


# --------------------------------------------------------------------------- #
# Pydantic schemas
# --------------------------------------------------------------------------- #

class DocumentIn(BaseModel):
    document_id: str
    source_type: str = Field(..., description=f"One of: {', '.join(SOURCE_TYPES)}")
    language: str = "en"
    title: Optional[str] = None
    content: Optional[str] = None
    content_ref: Optional[str] = None
    url: Optional[str] = None
    source_id: Optional[str] = None
    authors: List[str] = Field(default_factory=list)
    created_at: Optional[int] = Field(
        None, description="Original publish time, epoch milliseconds (document-ingest-v1)"
    )
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DocumentOut(DocumentIn):
    ingested_at: int


class EnrichmentOut(BaseModel):
    document_id: str
    enrichments: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Persistence — DocumentStore over the shared warehouse connection
# --------------------------------------------------------------------------- #

# When set (by tests), this store is used instead of the shared warehouse, so
# route behaviour is exercised offline against an in-memory DuckDB.
_test_store: Optional[DocumentStore] = None
_shared_store: Optional[DocumentStore] = None


def use_store_for_testing(store: Optional[DocumentStore]) -> None:
    """Point the document routes at ``store`` (or reset to the warehouse if None)."""
    global _test_store
    _test_store = store


@contextmanager
def _store_ctx() -> Iterator[DocumentStore]:
    """Yield the active DocumentStore, serialized against the shared connection.

    Tests inject an in-memory store (no warehouse, no lock); in production the
    store wraps the process-wide DuckDB connection and every access is held
    under the warehouse lock, since a DuckDB connection is not concurrency-safe.
    """
    if _test_store is not None:
        yield _test_store
        return
    from src.database.local_analytics_connector import locked_connection

    global _shared_store
    with locked_connection() as conn:
        if _shared_store is None:
            _shared_store = DocumentStore(conn)
        yield _shared_store


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@router.post("/ingest", response_model=DocumentOut, status_code=201)
async def ingest_document(
    doc: DocumentIn,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Ingest a document into the knowledge engine.

    This endpoint is source-type-agnostic — it accepts any ``source_type``
    from the document-ingest-v1 contract. News-specific enrichment is applied
    downstream by the news domain pack when enabled.

    A background task updates the live knowledge graph after the response is
    sent, extracting entity mentions and recording new connections so they
    are visible via GET /kg/connections/emerging and GET /kg/topics/evolving.
    """
    if doc.source_type not in SOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid source_type {doc.source_type!r}. Must be one of: {list(SOURCE_TYPES)}",
        )
    record = doc.model_dump()
    record["ingested_at"] = int(time.time() * 1000)

    with _store_ctx() as store:
        summary = store.upsert([record])
    if summary.invalid:
        # Contract violation the source_type pre-check did not catch.
        detail = summary.dead_letter[0]["error"] if summary.dead_letter else "invalid document"
        raise HTTPException(status_code=422, detail=detail)

    try:
        from src.knowledge_graph.kg_updater import update_from_document
        background_tasks.add_task(update_from_document, record)
    except Exception:
        pass  # KG update is best-effort; never block the ingestion response

    return record


@router.get("", response_model=List[DocumentOut])
async def list_documents(
    source_type: Optional[str] = Query(None, description="Filter by source_type"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> List[Dict[str, Any]]:
    """List ingested documents, optionally filtered by source_type."""
    if source_type and source_type not in SOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown source_type {source_type!r}. Must be one of: {list(SOURCE_TYPES)}",
        )
    with _store_ctx() as store:
        return store.list_documents(source_type=source_type, limit=limit, offset=offset)


@router.get("/source-types", response_model=List[str])
async def list_source_types() -> List[str]:
    """List all valid source types from the document-ingest-v1 contract."""
    return list(SOURCE_TYPES)


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: str) -> Dict[str, Any]:
    """Retrieve a single document by ID."""
    with _store_ctx() as store:
        doc = store.get(document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id!r} not found")
    return doc


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: str) -> None:
    """Remove a document from the store."""
    with _store_ctx() as store:
        existed = store.delete(document_id)
    if not existed:
        raise HTTPException(status_code=404, detail=f"Document {document_id!r} not found")


@router.get("/{document_id}/enrichments", response_model=EnrichmentOut)
async def get_enrichments(document_id: str) -> Dict[str, Any]:
    """Return available enrichments for a document.

    Enrichments are produced by the domain packs that are enabled. If no pack
    has enriched this document yet the ``enrichments`` dict is empty.
    """
    with _store_ctx() as store:
        doc = store.get(document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"Document {document_id!r} not found")

    from src.domains.registry import get_enabled_packs

    source_type = doc.get("source_type", "")
    enrichments: Dict[str, Any] = {}

    for pack in get_enabled_packs():
        for enricher in pack.enrichers:
            if enricher.applies_to(source_type):
                result = enricher.run(doc)
                if result is not None:
                    enrichments[enricher.name] = result

    return {"document_id": document_id, "enrichments": enrichments}
