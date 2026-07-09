# Papers connector

Ingests academic papers (arXiv) as `document-ingest-v1` records and into the
knowledge graph, turning a paper's references into first-class `CITES` edges.
First end-to-end source type of the knowledge-engine pivot; see
`docs/architecture/knowledge-engine-pivot.md`.

## Modules

- `models.py`: `PaperMetadata` and `Reference`, plus `paper_id` (stable document
  id preferring arXiv id, then DOI, then a title hash, so a paper and the papers
  that cite it line up).
- `arxiv.py`: `ArxivClient` (HTTP injectable) and `parse_atom` for the arXiv
  Atom API.
- `references.py`: reference providers. arXiv does not expose a reference list,
  so citations come from a Semantic-Scholar-style API (`SemanticScholarReferences`,
  HTTP injectable) or a `StaticReferencesProvider` for tests/offline use.
- `pdf_parser.py`: best-effort PDF text + section outline. Uses PyMuPDF when
  installed and degrades gracefully when not; `split_sections` is pure and
  dependency-free. GROBID can be slotted in here later for higher-fidelity
  structure.
- `citation_graph.py`: `build_citation_graph` adds the paper, `AUTHORED_BY`
  edges to resolved Person nodes, and `CITES` edges to each referenced work.
- `connector.py`: `PaperConnector` implementing the connector interface plus
  `ingest_to_kg`.

## Usage

```python
from src.ingestion.connectors import get_connector
from src.knowledge_graph.foundation import KnowledgeGraphStore

connector = get_connector("paper")          # network-backed by default

# As documents (document-ingest-v1):
for document in connector.harvest("1706.03762"):
    ...

# Into the knowledge graph (references -> CITES edges):
store = KnowledgeGraphStore()
paper_node = connector.ingest_to_kg(store, "1706.03762")
cites = store.triples(subject=paper_node.node_id)
```

For offline use or tests, inject an `ArxivClient(http_get=...)` and a
`StaticReferencesProvider`.

## Scope / follow-ups

- Reference extraction uses the metadata API rather than parsing the PDF; a
  GROBID-backed `pdf_parser` path can supplement it.
- PubMed/Crossref discovery and PDF-to-object-storage upload are planned
  extensions of this connector.
