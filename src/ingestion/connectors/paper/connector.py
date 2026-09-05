"""
Papers connector: ingest academic papers (arXiv) as documents and into the KG.

Implements the connector interface (`discover` -> `fetch` -> `parse` ->
`Document`) for `source_type="paper"`, and additionally exposes
`ingest_to_kg`, which builds the citation graph (references as `CITES` edges,
authors as `AUTHORED_BY` edges) for a paper.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, List, Optional, Union

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.paper.arxiv import ArxivClient, parse_atom
from src.ingestion.connectors.paper.citation_graph import build_citation_graph
from src.ingestion.connectors.paper.models import PaperMetadata
from src.ingestion.connectors.paper.references import ReferencesProvider
from src.ingestion.connectors.registry import register_connector
from src.knowledge_graph.foundation import EntityResolver, KnowledgeGraphStore, Node


def _to_millis(dt) -> Optional[int]:
    return int(dt.timestamp() * 1000) if dt is not None else None


def paper_metadata_to_document(meta: PaperMetadata, ingested_at: int) -> Document:
    """Map paper metadata to a document-ingest-v1 record.

    References are not embedded here (they live in the knowledge graph); only
    lightweight, contract-valid scalars and string arrays go in ``metadata``.
    """
    metadata = {
        "arxiv_id": meta.arxiv_id,
        "version_id": meta.version_id,
        "work_identifier": "doi:"+meta.doi.lower() if meta.doi else "arxiv:"+str(meta.arxiv_id),
        "content_coverage": "abstract-only" if meta.abstract else "metadata-only",
        "full_text_acquisition": "unavailable",
        "doi": meta.doi,
        "primary_category": meta.primary_category,
        "categories": list(meta.categories),
        "reference_count": len(meta.references),
        "reference_ids": [r.document_id for r in meta.references],
    }
    metadata = {k: v for k, v in metadata.items() if v not in (None, [])}

    url = f"https://arxiv.org/abs/{meta.arxiv_id}" if meta.arxiv_id else None
    return Document(
        document_id=meta.document_id,
        source_type="paper",
        language="en",
        ingested_at=ingested_at,
        source_id="arxiv" if meta.arxiv_id else None,
        url=url,
        title=meta.title,
        content=meta.abstract or None,
        content_ref=meta.pdf_url,
        authors=list(meta.authors),
        created_at=_to_millis(meta.published),
        metadata=metadata,
    )


@register_connector
class PaperConnector(Connector):
    """Ingest academic papers from arXiv as documents and citation-graph facts."""

    source_type = "paper"

    def __init__(
        self,
        arxiv_client: Optional[ArxivClient] = None,
        references_provider: Optional[ReferencesProvider] = None,
        http_get=None,
        full_text_acquirer=None,
        oa_resolver=None,
    ):
        self._arxiv = arxiv_client or ArxivClient(http_get=http_get)
        self._references = references_provider
        self._full_text_acquirer = full_text_acquirer
        self._oa_resolver = oa_resolver

    def discover(self, query: Optional[Union[str, Iterable[str]]] = None) -> Iterable[SourceRef]:
        """Yield a SourceRef per arXiv id. ``query`` is an id or list of ids."""
        if query is None:
            return
        if isinstance(query, dict):
            for paper in self._arxiv.search(query):
                yield SourceRef(locator=paper.version_id or paper.arxiv_id, metadata={'arxiv_id':paper.arxiv_id,'version_id':paper.version_id,'discovery_objective':dict(query)})
            return
        ids = [query] if isinstance(query, str) else list(query)
        for arxiv_id in ids:
            yield SourceRef(locator=arxiv_id, metadata={"arxiv_id": arxiv_id})

    def fetch(self, ref: SourceRef) -> RawDocument:
        return RawDocument(
            ref=ref,
            content=self._arxiv.fetch_by_id(ref.locator),
            content_type="application/atom+xml",
        )

    def parse(self, raw: RawDocument) -> List[Document]:
        content = raw.content
        if isinstance(content, str):
            content = content.encode("utf-8")
        documents = []
        for meta in parse_atom(content):
            if self._references is not None:
                meta.references = self._references.references_for(meta)
            document=paper_metadata_to_document(meta, raw.fetched_at)
            full_text_url=meta.pdf_url
            if self._full_text_acquirer is not None and not full_text_url and self._oa_resolver is not None and meta.doi:
                try:
                    locations=self._oa_resolver(meta.doi)
                    if locations:
                        chosen=locations[0]
                        full_text_url=chosen['url']
                        document.metadata['full_text_version']=chosen.get('version')
                        document.metadata['full_text_license']=chosen.get('license')
                except Exception:
                    document.metadata['full_text_acquisition']='failed'
            if self._full_text_acquirer is not None and full_text_url:
                acquired=self._full_text_acquirer.acquire(full_text_url.replace('http://','https://',1))
                document.metadata['full_text_acquisition']=acquired['outcome']
                if acquired['outcome']=='full-text':
                    import json
                    document.content=acquired['text']
                    document.metadata['content_coverage']='full-text'
                    document.metadata['full_text_provenance_json']=json.dumps({k:v for k,v in acquired.items() if k!='text'},sort_keys=True)
            documents.append(document)
        return documents

    # ---- metadata + knowledge graph ------------------------------------ #

    def metadata_for(self, arxiv_id: str) -> PaperMetadata:
        """Fetch full metadata for a paper, including references if a provider is set."""
        meta = self._arxiv.get_metadata(arxiv_id)
        if self._references is not None:
            meta.references = self._references.references_for(meta)
        return meta

    def ingest_to_kg(
        self,
        store: KnowledgeGraphStore,
        arxiv_id: str,
        resolver: Optional[EntityResolver] = None,
    ) -> Node:
        """Ingest a paper by id into the knowledge graph (references -> CITES)."""
        return build_citation_graph(store, self.metadata_for(arxiv_id), resolver=resolver)
