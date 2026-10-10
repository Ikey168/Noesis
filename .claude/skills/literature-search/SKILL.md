---
name: literature-search
description: Run a review protocol's literature search in Noesis and turn the hits into traceable review candidates — set up the paper's knowledge domain, translate each search expression into the exact per-database queries, harvest the scholarly connectors (OpenAlex, Crossref, Semantic Scholar, DBLP, DOAJ, CORE and others) by publication-date window, record a search-run receipt per database with pre-deduplication counts, commit the papers through Noesis's ingest workflow, import searches done outside Noesis (e.g. ERIC), acquire and ingest full texts, and add each paper as a candidate under the protocol. Use after the protocol is registered (systematic-review) and before screening (screening-extraction), or to rerun or update a search. Drives the scholarly connectors and src.gateway ingest workflow in the repo's Python environment plus the noesis-knowledge-engine add_review_candidate tool.
---

# Literature search

Turns the registered protocol (**systematic-review**) into review candidates
whose origin can be traced: which database, which exact query, when, how many
hits before deduplication, and which committed document revision each candidate
points at. Screening starts in **screening-extraction**.

Harvesting and ingesting have no MCP tool. They are short Python runs in the
repo's environment (`.venv`) through the functions below. **Live network access
and ingestion need the user's go-ahead for each run.** Show them the query plan
first.

## 0. The paper's knowledge domain

Documents live in a knowledge domain. Add one for the paper to the workspace
`domains.yml` (created by `noesis init`), using the existing entries as the
pattern:

```yaml
  - name: paper-ai-education-2026
    backing: corpus-view
    description: Corpus for the AI-in-education review.
    embedding_model: all-MiniLM-L6-v2
    tags: [paper-ai-education-2026, private]
    keywords: []
    embedding_anchors: [generative AI tutoring and learning outcomes in schools]
    feeds: []
```

Use the same name as the **paper-project** namespace, so `source_namespace` on
candidates, report dependencies and project scope all agree. Check it with the
gateway's `domains()` tool.

## 1. Query plan: one line per database

Boolean syntax is not portable. Each connector passes `topic` to its API's own
search, and each API interprets it differently. Turn each protocol
`search_expression` into the exact string per database, and write the plan down
before running anything:

| Database (connector) | Exact query string | Window | Notes |
|---|---|---|---|
| `openalex` | `"large language model" tutor student achievement` | 2022-11-30 → 2026-09-30 | broadest |
| `dblp` | `LLM tutoring` | 2022 → 2026 | year-granular; AIED/EDM/LAK/L@S proceedings |
| `core` | … | … | needs `CORE_API_KEY` |

Each connector returns at most `limit` records, up to its own ceiling
(`get_connector(name).SOURCE.max_limit`): **10,000** for the sources that page
(`openalex`, `europepmc`, `pubmed`, `core`, `doaj`), **5,000** for `scopus`, and
**200** (a single request) for the rest (`crossref`, `semantic_scholar`, `dblp`,
…). Every harvested document carries the API's hit count as
`metadata["source_total_results"]`. A query is **truncated** when that total
exceeds the effective limit, or when `metadata["source_unretrieved_records"]` is
set (a later page kept failing after retries, so the pages read so far were
kept). Split truncated queries by window until no slice is truncated, and record
each slice. Set `NOESIS_SCHOLARLY_CONTACT` to a contact email to respect the
APIs' rate limits.

Scope and syntax:

- Pass `"scope": "title_abstract"` for `openalex`, `europepmc` and `scopus` to
  match only title and abstract. OpenAlex's `search` and unfielded Europe PMC
  queries also match full texts, which inflates hits by orders of magnitude. A
  source that can't honour a scope raises instead of ignoring it.
- `openalex`, `core` and `doaj` reject or ignore wildcards: expand `term*` into
  explicit forms.
- `core`: combine bare terms with explicit `AND`; use one flat OR group per
  concept (nested parentheses silently under-match); a lone quoted phrase is
  rejected, a phrase inside a group works.
- `doaj`: the window is applied as a `bibjson.year` range (year-granular); run
  large queries in year slices, because deep offsets of a large result set can
  return HTTP 502.
- `crossref` and `semantic_scholar` (`/paper/search`) have no Boolean syntax;
  they can't execute a Boolean search expression. For a supplementary keyword
  search in Crossref, pass `"order": "relevance"` (otherwise results are newest
  first) and register how many top-ranked records are screened.
- Records that only carry a publication **year** (DOAJ, CORE's `yearPublished`,
  some Crossref records) are kept when the year overlaps the window and marked
  `metadata["publication_date_precision"] == "year"`; screening confirms the date.

## 2. Harvest, receipt, ingest

Per database and query slice:

```python
import hashlib, json, time
from src.ingestion.connectors.registry import get_connector
from src.gateway import ingest_documents
from src.noesis_cli.config import load_config

query = {"topic": "<exact string>", "since": "2022-11-30", "until": "2026-09-30",
         "limit": 10000, "scope": "title_abstract"}
connector = get_connector("openalex")
docs = list(connector.harvest(query))

limit = min(query["limit"], connector.SOURCE.max_limit)
meta = docs[0].metadata if docs else {}
total, unretrieved = meta.get("source_total_results"), meta.get("source_unretrieved_records") or 0
receipt = {"database": "openalex", "query": query, "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "hits": len(docs), "api_total": total, "unretrieved": unretrieved,
           "truncated": bool(unretrieved) or (total > limit if total is not None else len(docs) >= limit),
           "document_ids": sorted(d.document_id for d in docs)}
receipt["search_run_id"] = "search:" + hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()[:24]

result = ingest_documents(load_config(), docs, domain="paper-ai-education-2026",
                          source_identity=receipt["search_run_id"])
receipt["ingest"] = {"workflow_run_id": result["processing"]["workflow_run_id"],
                     "watermark": result["processing"]["watermark"], "upsert": result["upsert"],
                     "warnings": result["processing"]["warnings"]}
```

- Save every receipt (JSON, one file per run) in the paper's working
  directory, outside the repository. Receipts are the PRISMA "records
  identified" per database, and they are the only place the
  pre-deduplication counts exist.
- Documents with a DOI get the same `document_id` from every database, so
  ingest deduplicates them. Records without a DOI do not collapse; find their
  duplicates by title, year and first author before adding candidates.
- `ingest_documents` runs Noesis's ingest → extract → resolve → index
  workflow. When a model is unavailable it still commits but reports degraded
  coverage in `warnings`. Keep those in the receipt.
- Harvested papers are metadata or abstract only
  (`metadata.content_coverage`). Full text is step 4.

## 3. Searches outside Noesis

No Noesis connector covers **ERIC**, Google Scholar, hand searching or
citation chasing. Run them outside Noesis and give them the same receipt shape
(`"database": "ERIC (external)"`, the exact query, the date, the hit count).
Ingest each hit with the gateway `add(source=<DOI URL, landing page or local file>, domain=...)`
tool, or `ingest_documents` for a batch. They count under "records identified
via other methods" in PRISMA.

## 4. Full texts

Screening can include a study at the full-text stage only if the full text is
ingested. **A review candidate cannot be updated later**: `full_text_available`
and the pinned `source_revision` are fixed when it is added. Two workable
orders:

- **Full text first (small searches).** For each hit, get the full text and
  ingest it before adding the candidate. Then add it with
  `full_text_available=True`.
- **Two-step (large searches).** Add abstract-only candidates
  (`full_text_available=False`) and screen titles and abstracts. For each
  title/abstract include, get the full text, ingest it, and add a **second
  candidate** for the full-text document with the same `study_id` and the
  original `search_run_id`. Each reviewer records their own title/abstract
  decision on it again, citing the first candidate's id in the reason, before
  full-text screening. **paper-figures** counts these as reports retrieved, not
  new records.

Sources of full text, in order: open-access PDFs or JATS XML from the
publisher or a repository (arXiv, PubMed Central, institutional repositories),
then PDFs the user is licensed to use, added as local files with
`add(source="/path/paper.pdf", domain=...)`. Never fetch paywalled content
around a licence. Record "not retrieved" for the rest; it stays pending in
screening and is reported, not hidden.

## 5. Add the candidates

For each deduplicated paper:

```
add_review_candidate(namespace="paper-ai-education-2026", protocol_id=..., protocol_revision=<current>,
  publication_id=<document_id>, source_revision=<committed revision id>,
  source_namespace="paper-ai-education-2026", search_run_id=<receipt id of the first run that found it>,
  study_id=<one id per study; reuse for companion reports>, title=..., abstract=...,
  full_text_available=<True only if the full text is ingested>)
```

- `source_revision` is the committed revision of the ingested document. Read
  it from the gateway's `inspect_source(document_id, domain)` integrity
  evidence. Extraction later refuses anything but a committed revision.
- Assign `study_id`s deliberately. Several papers reporting one trial share
  one study id, so study and report counts stay distinct.
- Re-running a search (an update before submission) is a new receipt. Already
  added papers return the same candidate; only new ones are added.

## Rules

- No hit is dropped silently: every harvested record is either a candidate or
  listed in the receipt as a duplicate.
- Exact query strings and dates go into the paper's methods. "We searched
  OpenAlex for AI and education" is not reproducible.
- Truncated result sets are split, never ignored.
- Ask before each live harvest or ingest run, and never commit receipts or
  corpora to the repository.
