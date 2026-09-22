---
name: science-overview
description: Produce a cited overview of a research topic from recent scientific papers in Noesis, recency by publication date. Use when an agent must survey the recent literature on a topic and synthesize a structured, evidence-backed overview — consensus vs. disagreement, influential papers and venues, coverage gaps, and open questions. Harvests recent papers across the scholarly connectors (OpenAlex, Crossref, Semantic Scholar, Europe PMC, PubMed, bioRxiv, medRxiv, DOAJ, CORE, DBLP, HAL, PLOS, Zenodo), then runs a bounded Deep Research pass with claim/contradiction/coverage analysis and an Evidence Bundle export.
---

# Scientific literature overview

Given a **research topic**, build a structured, **fully cited** overview from the
recent paper corpus in Noesis. Papers are `source_type="paper"` documents in a
research knowledge domain; the research pack enriches them with venue, citations,
and concept metadata. Every claim in the output must carry a statement-level
citation to a Noesis document — never assert anything the evidence layer can't
back.

## Inputs

- **topic** — the research question/area (required).
- **recency window** — scoped by **publication date** (`Document.created_at`).
  Pass `since`/`until` as `YYYY-MM-DD`; the scholarly connectors query each API's
  native publication-date filter and the harvest is additionally post-filtered on
  publication date, so the window is exact regardless of when a paper was ingested.
- **domain/namespace** — the research domain to work in (e.g. `research`, or a
  topic-scoped namespace). Create one if the topic needs isolation.
- **depth** — quick scan vs. systematic; sets the Deep Research time budget.
- **sources** — which scholarly connectors to harvest (default: all applicable;
  see the table in step 2).

## Which tools (deployed vs. richer)

- **Reachable over `noesis-oracle` (deployed REST):** `POST /api/v1/documents/ingest`;
  `GET /api/v1/kb/{domain}/search|answer|documents|claims|contradictions|coverage`;
  `GET /api/v1/kb/brief`; `POST /api/v1/kb/cross-domain/answer`;
  `/api/v1/arguments/claims/*` (extract, evidence, factcheck).
- **Richer literature tools (research pack / gateway MCP, warehouse-scoped):**
  `venues()` (per-venue credibility), `citation_graph(topic, …)` (paper citation
  network), `literature_claims(topic)` (SUPPORTS/CONTRADICTS across papers), and
  the gateway `research`/`brief`/`export`/`watch` verbs. Use these when the
  research MCP is available; otherwise fall back to the REST equivalents above.

## Workflow

1. **Scope the domain.** Select or provision a research knowledge domain for the
   topic (isolate with its own namespace when the corpus should not mix with
   general domains). Confirm it backs `source_type="paper"`.
2. **Harvest recent papers by publication date.** Run the scholarly connectors
   for the topic + `{since, until}` window and ingest what they return (each
   yields `source_type="paper"` `Document`s with `created_at` = publication
   date). Harvest broadly, then dedupe by DOI (`metadata.work_identifier`).

   | Connector (`get_connector` name) | Coverage | Notes |
   |---|---|---|
   | `openalex` | all fields | broadest; no key |
   | `crossref` | all fields (journal articles) | DOI metadata; no key |
   | `semantic_scholar` | all fields + abstracts | key optional (raises rate limit) |
   | `europepmc` | biomedical + life sci | no key |
   | `pubmed` | biomedical (NCBI) | `NCBI_API_KEY` optional; metadata-only |
   | `biorxiv` / `medrxiv` | preprints | date-window API, topic-filtered locally |
   | `doaj` | open-access journals | year-granular dates |
   | `core` | OA aggregator | **`CORE_API_KEY` required** |
   | `dblp` | computer science | year-granular dates |
   | `hal` | multi-disciplinary (FR) | no key |
   | `plos` | PLOS journals | no key |
   | `zenodo` | research outputs/datasets | no key |

   Do **not** paste paper text into the overview; ingest via `add(url, domain=…)`
   / `POST /api/v1/documents/ingest` so each paper becomes cited evidence. For a
   *standing* recent-paper feed, register a scholarly connector as a durable
   source; add a brand-new source type with the **scaffold-document-connector**
   skill.
3. **Bound the work.** Open a **Deep Research** session (the `research` gateway
   tool, or the **intake-mode** skill's `start_intake_mode(mode="Deep Research")`)
   so the pass is time-budgeted, replayable, and audited.
4. **Map the literature.**
   - `search`/`kb/{domain}/search` and `ask`/`kb/{domain}/answer` to pull the
     core evidence with citations.
   - `literature_claims(topic)` (or `kb/{domain}/claims`) for the SUPPORTS /
     CONTRADICTS claim structure.
   - `kb/{domain}/contradictions` to surface where papers disagree.
   - `citation_graph(topic, …)` and `venues()` to identify influential papers and
     weigh source credibility.
   - `kb/{domain}/coverage` to see what the corpus does and does **not** cover.
5. **Synthesize the overview** (structure below), each point cited. Use
   `cross-domain/answer` when the topic spans domains.
6. **Export evidence.** `export(kind="bundle", …)` / the Evidence Bundle endpoint
   so the overview ships with a verifiable, portable citation set.
7. **(Optional) Keep it live.** `watch(...)` a durable evidence watch on the
   topic so new ingested papers update the picture.

## Output structure

Produce the overview as:

- **Topic & scope** — the question, the recency window, the corpus surveyed
  (domain, #papers, ingestion window).
- **Consensus** — well-supported claims, each with citations and corroboration.
- **Disagreements / open contradictions** — where papers conflict, both sides
  cited (from `contradictions` / `literature_claims`).
- **Influential work & venues** — key papers by citation structure; venue
  credibility notes.
- **Coverage & gaps** — what the corpus covers well vs. thin/absent areas.
- **Open questions** — what the recent literature leaves unresolved.
- **Evidence bundle** — the exported bundle reference/digest.

## Rules

- **Cite everything.** No claim without a statement-level citation to an ingested
  paper; if the evidence layer refuses (insufficient support), report the gap
  rather than assert.
- **Recency is publication date** (`Document.created_at`) — the scholarly
  connectors window on it at harvest. Note the exact window in the overview's
  scope. (The KB's own default filters are ingestion-time; rely on the harvest
  window and each paper's `created_at`, not the KB default, for "recent".)
- **Surface contradictions**, don't smooth them over — disagreement in the
  literature is a finding.
- **Reference, don't copy** — the overview links to Noesis documents/claims;
  source content stays in the store.
