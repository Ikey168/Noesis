# Pivot Plan: From News Analytics to a General Knowledge Extraction Engine

**Status:** Largely delivered — see §15 (status update 2026-07-02) for what
shipped, what changed, and the beyond-news continuation
**Owner:** TBD
**Date:** 2026-06-21 (status update 2026-07-02)
**Related:** `docs/architecture/MCP_REARCHITECTURE_PLAN.md`, `docs/genui.md`
**Decisions locked in:**

- First new source type built end-to-end: **Academic papers** (arXiv / PubMed / PDF).
- Existing news features are **kept as an optional "domain pack"**, not deleted.
- Migration is **incremental and additive** — the existing news pipeline stays green throughout.

---

## 1. Vision

NeuroNews becomes a **knowledge extraction engine for arbitrary text media** — news sites,
blogs, books, papers, transcripts, and uploaded documents. The product output shifts from
"news dashboards" to a **queryable, cited, cross-source knowledge base**: entities, relations,
and claims extracted from any document, retrievable via RAG and explorable as a knowledge graph.

News analytics (sentiment, fake-news, events, influence networks) is preserved as one
**pluggable domain pack** that runs only for `source_type = news`.

---

## 2. What already transfers (no change required)

These components are already domain-agnostic and form the core of the new engine:

| Capability | Location | Role in new engine |
| --- | --- | --- |
| RAG (chunk/retrieve/rerank/diversify/answer) | `services/rag/` | Core Q&A over any corpus |
| Vector search | Qdrant + pgvector, `services/vector_service.py` | Core semantic retrieval |
| Embeddings | `src/nlp/article_embedder.py`, `services/embeddings/` | Core indexing |
| Entity **and relation** extraction | `src/knowledge_graph/enhanced_entity_extractor.py` (`extract_entities_from_article`, `extract_relationships`) | Core KG construction |
| Knowledge graph build/query/search | `src/knowledge_graph/{graph_builder,graph_query_engine,graph_search_service,semantic_analyzer}.py` | Core knowledge base |
| NER / keywords / topics / summarization / multilingual | `src/nlp/` | Generic enrichers |
| Orchestration / MLOps / contracts / FinOps | `airflow/`, `src/ml/mlops/`, `contracts/`, `deploy/kubernetes/finops/` | Reusable as-is |

**Implication:** the pivot is mostly a *data-model generalization* plus *new ingestion connectors*,
not a rewrite of the analytical core.

---

## 3. Keystone: `Article` → `Document`

All news-specificity traces back to the ingest contract
(`contracts/schemas/jsonschema/article-ingest-v1.json`), which assumes a published news article:
`url`, `source_id` (publisher), `published_at`, and `country` are effectively required and
`sentiment_score`/`topics` are inlined.

### 3.1 New core contract: `document-ingest-v1`

Introduce `contracts/schemas/jsonschema/document-ingest-v1.json` (and matching Avro
`contracts/schemas/avro/document-ingest-v1.avsc`) alongside the existing article contract.

Core fields:

```jsonc
{
  "document_id": "string (required)",
  "source_type": "enum: news | blog | book | paper | transcript | web | note (required)",
  "title": "string|null",
  "content": "string|null",            // inline body, OR
  "content_ref": "string|null",        // pointer to object storage for large docs (books/PDFs)
  "language": "string (ISO 639-1)",
  "ingested_at": "date-time (required)",
  "created_at": "date-time|null",      // generalizes published_at; optional
  "authors": ["string"],               // optional
  "source_id": "string|null",          // publisher/repository; optional
  "url": "string|null",                // optional (books/uploads have none)
  "metadata": { }                      // type-specific bag (see below)
}
```

`metadata` examples by `source_type`:

- **paper**: `doi`, `arxiv_id`, `pubmed_id`, `journal`, `venue`, `references[]`, `sections[]`, `cited_by_count`
- **book**: `isbn`, `publisher`, `edition`, `toc[]`, `chapter`, `page_range`
- **transcript**: `media_url`, `duration_s`, `speakers[]`, `timestamps[]`
- **news**: `country`, `section`, `byline` (the old fields move here)

### 3.2 Enrichments move out of the core record

`sentiment_score` and `topics` leave the ingest record and become rows in an **enrichment
layer** (one analyzer among many), keyed by `document_id`. This is what makes news analytics
"just a domain pack."

### 3.3 Backward compatibility

- Keep `article-ingest-v1` valid. Add an **adapter** that maps an `ArticleIngest` to a
  `Document` (`source_type = news`, news fields → `metadata`).
- Existing DAGs/tables continue to work via the adapter; no big-bang migration.
- DB: add a `documents` table (or generalize the existing articles table with a
  `source_type` column + JSON `metadata`). Provide a view `articles` for legacy queries.

---

## 4. Ingestion connector framework

Today ingestion is HTML/Scrapy-centric (`src/ingestion/`, `src/scraper/`). Introduce a
**connector framework** so each media type plugs in behind a common interface.

```
src/ingestion/connectors/
  base.py            # Connector protocol: discover() -> fetch() -> parse() -> Document
  registry.py        # source_type -> connector
  paper/             # FIRST (this milestone)
    arxiv.py         # arXiv API: metadata, PDF, references
    pubmed.py        # PubMed/Crossref metadata
    pdf_parser.py    # PDF -> sections, figures/captions, references (GROBID or pdfplumber)
  book/              # later: EPUB/PDF, hierarchical TOC, OCR fallback
  blog/              # later: RSS/Atom + readability (reuse scraper)
  transcript/        # later: audio/video -> Whisper -> Document
  upload/            # later: generic PDF/md/docx/text upload
```

Common interface:

```python
class Connector(Protocol):
    source_type: str
    async def discover(self, query) -> list[Ref]: ...
    async def fetch(self, ref) -> RawDoc: ...
    async def parse(self, raw) -> Document: ...   # normalized Document contract
```

---

## 5. First milestone end-to-end: Academic papers

Chosen as the proof of the pivot — cleanest metadata, highest knowledge-engine value, and it
exercises the citation graph (a genuinely new capability).

Pipeline:

1. **Discover** via arXiv/PubMed/Crossref API by query, author, or ID.
2. **Fetch** PDF + structured metadata; store PDF in object storage, reference via `content_ref`.
3. **Parse** with GROBID (preferred) or `pdfplumber`/`pymupdf` fallback into:
   - section tree (abstract, intro, methods, …)
   - figure/table captions
   - **reference list** (parsed citations with DOIs where resolvable)
4. **Chunk** section-aware + hierarchical (see §6).
5. **Extract** entities + relations (reuse `enhanced_entity_extractor`) and **claims** (§7).
6. **Build KG**: paper node, author nodes, and **`CITES` edges** between papers → a citation
   graph. Concepts/entities link papers across the corpus.
7. **Index** chunks into the vector store for RAG.
8. **Expose** via API (§8) and a new web view (§9).

New API surface for this milestone: `POST /documents/ingest` (paper by id/url),
`GET /documents/{id}`, `GET /documents/{id}/citations`, RAG `ask` already works over the new chunks.

---

## 6. Structure-aware chunking

`services/rag/chunking.py` is currently flat (sentence/paragraph/word/char). Add a
**hierarchical, structure-aware** strategy:

- New `SplitStrategy.STRUCTURED` that consumes the parsed section/chapter tree.
- Each `TextChunk` carries `path` metadata (e.g., `["§4", "4.2 Methods"]` or
  `["Part II", "Chapter 5"]`) so retrieval can cite location.
- Keep existing strategies for flat sources (news/blogs) — fully backward compatible.

This materially improves long-document RAG for papers and books.

---

## 7. Relation + claim extraction (the differentiator)

Entities and relations already exist. Add a **claim extraction** pass:

- New module `src/knowledge_graph/claim_extractor.py`: extract atomic
  subject–predicate–object claims with **provenance** (`document_id` + chunk `path`).
- Store claims as KG nodes/edges so the graph answers cross-document questions
  ("what does the literature say about X") with citations.
- Reuse RAG to surface corroborating/contradicting evidence per claim (this also powers the
  existing news fact-checking idea as a special case).

---

## 8. API generalization

`src/api/routes/` is article/news-centric. Plan:

- Add `document_routes.py` (ingest, get, list by `source_type`, citations, enrichments).
- Keep `article_routes.py`, `news_routes.py`, `sentiment_*`, `veracity_*`, `event_*`,
  `influence_*` registered **only when the news domain pack is enabled** (feature flag).
- RAG/search/graph routes stay; they already operate on generic chunks/KG.

---

## 9. Domain-pack architecture (news kept, not deleted)

Introduce a lightweight pack registry:

```
src/domains/
  base.py        # DomainPack: enrichers, routes, UI feature flags
  news/          # wraps fake_news_detector, news sentiment, event_clusterer,
                 #   influence_network_analyzer, news timeline UI
```

- A pack registers **enrichers** (run per matching `source_type`), **routes**, and **UI flags**.
- Config (`config/`) selects active packs. Default ships `news` enabled so nothing regresses.
- Future packs: `research` (papers), `legal`, `finance`, etc.

This satisfies "keep news as an optional pack" with minimal disruption.

---

## 10. Web app reframing (`apps/web/`)

- `NewsFeed.tsx` → generic **Library / Sources** view with a `source_type` filter.
- `EntityGraphView.tsx` extended to render the **citation graph** for papers.
- Add a **Document reader** view (section tree + inline entity/claim highlights + "ask this doc").
- News-only views (`Sentiment`, `Timeline`, `Clusters`, `Trending`, `Watchlists`) gated behind
  the news domain-pack flag.
- Rebrand/rename is the **last** step, after the engine is generalized.

---

## 11. Milestones

The current `src/knowledge_graph/` is an **entity-relationship (ER) graph**, not a knowledge
graph: nodes are `Article` vertices, relations are inferred by co-occurrence/patterns, and there
is no ontology, no provenance, no entity resolution, and no claim representation. The pivot
therefore promotes the **knowledge-graph redesign to a foundational milestone (M2)**, not a
late-stage add-on.

Milestones are ordered by dependency. M0–M6 deliver a working general knowledge engine with
papers end-to-end; M7–M12 broaden coverage and polish.

### M0 — Document data model (keystone)
- New core contract `document-ingest-v1` (JSON Schema + Avro) alongside `article-ingest-v1`.
- `Document` model with `source_type`, optional `url`/`created_at`/`country`, flexible `metadata`.
- `Article → Document` adapter (`source_type = news`, news fields → `metadata`).
- DB: `documents` table (or `source_type` + JSON `metadata` column) + legacy `articles` view.
- Move `sentiment_score`/`topics` out of the core record into an enrichment layer.
- **Exit:** existing news pipeline green via adapter; contract tests pass.

### M1 — Connector framework
- `src/ingestion/connectors/{base.py,registry.py}` with `discover→fetch→parse→Document`.
- Refactor existing Scrapy/Playwright HTML ingest behind the framework as the `news`/`web` connector.
- **Exit:** current news ingest runs unchanged through the new framework.

### M2 — Knowledge graph foundation (was "ER graph")
- **Ontology**: typed entity classes (`Entity`, `Person`, `Organization`, `Concept`, `Document`,
  `Claim`, `Method`, `Dataset`) and relation types (`AUTHORED_BY`, `CITES`, `INSTANCE_OF`,
  `PART_OF`, `DEFINES`, `SUPPORTS`, `CONTRADICTS`, `MENTIONS`) with constraints.
- **Node re-centering**: graph center of gravity moves from `Article` vertices to
  `Concept`/`Claim` nodes; documents become anchors, not the primary node.
- **Provenance on every triple**: `(subject, predicate, object, source_doc, chunk_id, confidence)`.
- **Reified relations**: edges carry properties (e.g., `CITES.year`, `CITES.context`).
- **Exit:** facts are stored as cited triples; ontology validates entity/relation types.

### M3 — Entity resolution
- Canonicalization stage before graph insertion: dedup the same person/org/concept across
  documents into one canonical node (alias table, embedding + string similarity).
- Backfill existing nodes to canonical IDs.
- **Exit:** "Geoffrey Hinton" / "Hinton" / "G. Hinton" resolve to one node across the corpus.

### M4 — Papers connector + citation graph
- arXiv/PubMed/Crossref/Semantic Scholar discovery; PDF fetch to object storage (`content_ref`).
- PDF parsing (GROBID preferred, `pymupdf`/`pdfplumber` fallback): sections, figures/captions,
  reference list.
- References become first-class `CITES` edges → queryable citation graph.
- **Exit:** ingest a paper by id → its references appear as `CITES` edges in the KG.

### M5 — Structure-aware chunking
- `SplitStrategy.STRUCTURED` in `services/rag/chunking.py` consuming the section/chapter tree.
- Each chunk carries a `path` (e.g., `["§4","4.2 Methods"]`) for location-cited retrieval.
- **Exit:** RAG over a paper returns answers cited to specific sections.

### M6 — Claim extraction + knowledge layer
- `src/knowledge_graph/claim_extractor.py`: atomic subject–predicate–object claims with provenance.
- Claims stored as KG nodes/edges; `SUPPORTS`/`CONTRADICTS` links across documents.
- RAG surfaces corroborating/contradicting evidence per claim.
- **Exit:** "what does the literature say about X" returns synthesized, cited claims; papers E2E done.

### M7 — Domain packs (news kept, optional)
- `src/domains/{base.py,news/}`; move `fake_news_detector`, news sentiment, `event_clusterer`,
  `influence_network_analyzer`, news timeline UI into the `news` pack.
- Feature-flag pack registration; add generic `document_routes.py`; gate news-only routes.
- **Exit:** news features run only when pack enabled; generic API/KG works with pack off.

### M8 — Books connector
- EPUB/PDF ingestion with hierarchical structure (parts → chapters → sections); OCR fallback.
- Reuses M5 structured chunking and M2 KG.
- **Exit:** "chat with this book" answers cited to chapter/section.

### M9 — Blogs / RSS / Atom connector
- Feed subscription + readability extraction (reuses existing scraper).
- Generic watchlists/digests over feeds.
- **Exit:** subscribe to a feed → new posts auto-ingested, entities/claims extracted.

### M10 — Media (audio/video) connector
- Whisper transcription → `Document` with `source_type = transcript`, timestamped chunks.
- **Exit:** semantic search over a podcast returns timestamp-linked answers.

### M11 — Generic upload connector
- PDF/Markdown/docx/text/email upload + paste path → personal/team knowledge base.
- **Exit:** drop in mixed docs → cross-corpus, cross-source-type cited Q&A.

### M12 — Web reframing & rebrand
- `NewsFeed` → generic **Library / Sources**; `EntityGraphView` → knowledge/citation graph UI;
  new **Document reader** view; gate news views behind the pack.
- Rename/rebrand from "NeuroNews" last, after the engine is generalized.
- **Exit:** all source types usable in the UI under a non-news identity.

**Critical path to a working general engine with papers:** M0 → M1 → M2 → M3 → M4 → M5 → M6.
M7 unlocks the optional-news architecture; M8–M11 add coverage; M12 finishes the product identity.

---

## 12. Testing & data quality

- Contract tests for `document-ingest-v1` mirroring existing `contracts/tests/` + valid/invalid examples.
- Connector unit tests with recorded fixtures (one real arXiv paper, one PubMed record).
- Golden-set RAG eval (`evals/`) extended with paper Q&A pairs.
- Adapter round-trip test: `ArticleIngest` ⇄ `Document` lossless for news fields.

---

## 13. Open questions / risks

- **PDF parsing dependency:** GROBID (Docker service, best quality) vs. pure-Python
  (`pymupdf`/`pdfplumber`, simpler deploy). Recommend GROBID with a Python fallback.
- **Storage of large docs:** books/PDFs use `content_ref` to S3-compatible storage rather than
  inlining `content` — keep the metadata store lean.
- **Entity resolution at scale:** cross-source dedup of the same person/concept is foundational,
  not optional — promoted to its own milestone (M3).
- **Naming:** "NeuroNews" implies news; rebrand deferred to M12 to avoid churn mid-pivot.

---

## 14. Recommended immediate next step

Begin **M0** (the `Document` contract + Article adapter). It is purely additive, unblocks every
later milestone, and carries zero risk to the existing news pipeline.

---

## 15. Status update (2026-07-02) — and the beyond-news continuation

### 15.1 Milestone status

Every milestone has shipped code; the engine layer of this pivot is done.

| Milestone | Status | Evidence |
| --- | --- | --- |
| M0 Document model | Delivered | `contracts/schemas/jsonschema/document-ingest-v1.json` + fixtures, `Document` model, article adapter, contract tests |
| M1 Connector framework | Delivered | `src/ingestion/connectors/{base,registry}.py`; news runs through it |
| M2 KG foundation | Delivered | `src/knowledge_graph/foundation/{ontology,model,store}.py` (typed ontology, provenance store) |
| M3 Entity resolution | Delivered | `src/knowledge_graph/foundation/resolution.py` + `entity_corrections` |
| M4 Papers + citation graph | Delivered | `connectors/paper/{arxiv,pdf_parser,references,citation_graph}.py` |
| M5 Structured chunking | Delivered | `SplitStrategy.STRUCTURED` in `services/rag/chunking.py` |
| M6 Claim extraction | Delivered | `src/knowledge_graph/{claim_extractor,claim_graph}.py` |
| M7 Domain packs | Delivered | `src/domains/` registry, news pack feature-flagged, `document_routes` |
| M8–M11 Books/blogs/media/upload | Delivered | `connectors/{book,blog,media,upload}/` |
| M12 Web reframing + rebrand | Delivered, differently | Superseded by the **fully generative canvas** (`docs/genui.md`): fixed views were not reframed but *removed* — every screen is planned from intent. Rebrand to Noesis done |

### 15.2 What changed since this plan was written

Two architecture tracks landed after §1–§14 and reshape the endgame:

- **The generative UI** (`docs/genui.md`) dissolves §10's view-by-view
  reframing: there are no views to gate. Domains surface as *panel
  families* selected by the planner, gated by pack `ui_flags` and data
  availability. Adding a domain to the UI = adding catalog entries, not
  screens.
- **The MCP rearchitecture** (`MCP_REARCHITECTURE_PLAN.md`) turns §9's
  pack registry into connected MCP servers (Stage 1), and — decisively —
  **Track P** makes new knowledge domains *provisionable*: `kg_deploy` +
  `kg_attach_sources` stand up a namespaced KG fed by selected sources,
  with the canvas growing panels via discovery. Hand-built packs stop
  being the only way to add a domain.

### 15.3 Beyond news: the remaining coupling

The engine is general; the *product posture* is still news-first. The
concrete residue, in priority order:

1. **No second first-class pack.** Papers/books ingest and render in
   generic panels, but nothing exploits them: no citation-graph panel, no
   venue/source credibility for non-news, no literature-claims analytics.
   The news pack is load-bearing by default (`config/domain_packs.json`).
2. **genui gravity.** Overview panels anchor availability on the
   `news_articles` table; the empty-canvas movers, KPI tiles, and the
   BREAKING ticker are news telemetry shown regardless of domain mix.
3. **News-shaped vocabulary.** "Outlet" scoring/clustering generalizes
   conceptually to *source* credibility (paper venues, blogs, publishers)
   but is named, stored (`outlet_*` tables), and worded for newsrooms.
4. **Brand residue in infra.** `NEURONEWS_*` env vars, `@neuronews/web`,
   `neuronews-*` MCP server names, "NeuroNews API" title, page title.
   Cosmetic but pervasive; rename with aliases, never a breaking cut.

### 15.4 Continuation plan

- **N1 — Research pack as the second first-class domain** (proves
  pack-plurality; the papers milestone locked this choice). A
  `research` DomainPack/MCP server: enrichers over paper metadata,
  `ui_flags` for new panel types — `citation_graph`, `venues`
  (credibility per venue, reusing the transparency machinery),
  `literature_claims` (SUPPORTS/CONTRADICTS from M6) — plus
  research-flavored empty-canvas telemetry (recently ingested papers,
  emerging concepts) when the pack dominates the corpus mix.
- **N2 — De-news the shared layer.** Generalize outlet→source naming
  behind views (keep `outlet_*` tables, add `source_*` views), make
  empty-canvas telemetry pack-aware (movers/ticker supplied by whichever
  packs are enabled), and anchor overview availability on `documents`
  rather than `news_articles`.
- **N3 — Domains via Track P.** Finance (earnings-call transcripts —
  the media connector already transcribes) and legal/policy arrive as
  *provisioned* domains, not hand-built packs: the acceptance test for
  Track P is standing up one of these without writing a pack.
- **N4 — Naming/alias sweep.** `NOESIS_*` env vars with `NEURONEWS_*`
  fallbacks, package/server renames, API title — mechanical, last, and
  alias-first so nothing breaks.

Sequencing: N1 needs nothing from the MCP plan and can start now; N2
pairs naturally with MCP Stage 1 (availability from stats tools); N3 is
Track P's exit criterion; N4 is cleanup once N1–N3 make Noesis
demonstrably not-a-news-app.
