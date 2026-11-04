# Scholarly paper connectors

Recent scientific papers as `document-ingest-v1` `Document`s
(`source_type="paper"`), **recency by publication date** (`Document.created_at`).
Every connector answers the same query shape — a topic plus an optional
`{since, until}` (`YYYY-MM-DD`) publication-date window — and is registered so it
resolves via `get_connector("<name>")`.

```python
from src.ingestion.connectors.registry import get_connector
conn = get_connector("openalex")
for doc in conn.harvest({"topic": "coastal flooding", "since": "2026-01-01", "until": "2026-09-30", "limit": 50}):
    ...  # doc.created_at is the publication date (ms epoch)
```

Add a source by appending a `ScholarlySource` spec + a one-line subclass in
`sources.py` (the `_spec_connector(SPEC)` helper for pure JSON-search APIs, or a
`ScholarlyConnector` subclass overriding `fetch`/`parse` for anything two-step).

## Sources

| name | host | key | notes |
|------|------|-----|-------|
| `openalex` | api.openalex.org | — | broadest coverage |
| `crossref` | api.crossref.org | — | journal-article DOI metadata |
| `semantic_scholar` | api.semanticscholar.org | `SEMANTIC_SCHOLAR_API_KEY` (optional) | abstracts; keyless works at a lower rate limit |
| `europepmc` | www.ebi.ac.uk | — | biomedical / life sciences |
| `pubmed` | eutils.ncbi.nlm.nih.gov | `NCBI_API_KEY` (optional) | E-utilities esearch→esummary; metadata-only |
| `biorxiv` | api.biorxiv.org | — | preprints; date-window API, topic-filtered locally |
| `medrxiv` | api.biorxiv.org | — | preprints; date-window API, topic-filtered locally |
| `doaj` | doaj.org | — | open-access journals; year-granular dates |
| `core` | api.core.ac.uk | `CORE_API_KEY` (required) | OA aggregator |
| `dblp` | dblp.org | — | computer science; year-granular dates |
| `hal` | api.archives-ouvertes.fr | — | multi-disciplinary (France) |
| `plos` | api.plos.org | — | PLOS journals |
| `zenodo` | zenodo.org | — | research outputs / datasets |
| `scopus` | api.elsevier.com | `ELSEVIER_API_KEY` (required) | STANDARD view: no abstracts, first author only, 25 per request (paged); year-granular date filter; cover dates after `until` are kept and flagged `cover_date_after_window` |
| `eric` | api.ies.ed.gov | — | education literature (IES/ED); keyword search over title/abstract/subject; year-granular dates; record URLs synthesized |
| `edarxiv` | api.osf.io | — | education preprints (OSF Preprints); title contains-match search; contributor names embedded; prefers the minted DOI |

Network safety: credential-free HTTPS only, per-source host allowlist, and the
resolved address must be public (no SSRF). Set `NOESIS_SCHOLARLY_CONTACT` (a
contact email) to be polite to the APIs' rate limits. `http_get` and the DNS
resolver are injectable for offline tests.

## Abstract backfill by DOI

Sources that return no abstract (`scopus` without an entitlement, `pubmed`
esummary) can be completed by DOI with `abstracts.AbstractBackfill`:

```python
from src.ingestion.connectors.scholarly.abstracts import AbstractBackfill
documents, report = AbstractBackfill().fill(documents)      # copies; originals untouched
report = AbstractBackfill().fetch(["10.1186/s12909-026-09671-0"])  # DOIs only
```

Lookup order: OpenAlex (batched `filter=doi:…`, 50 per request, abstract rebuilt
from `abstract_inverted_index`), then Crossref (`/works/{doi}`, JATS stripped),
then Semantic Scholar (`POST /graph/v1/paper/batch`, 500 per request). Each
filled document records `abstract_source`, `abstract_provider_id` and
`abstract_retrieved_at`, and its `content_coverage` becomes `abstract-only`.
DOIs no provider can supply are reported as `missing`; a provider error (for
example HTTP 429) is recorded under `errors` and the next provider is tried.
Keys are optional: `NOESIS_OPENALEX_API_KEY` (or `OPENALEX_API_KEY`) and
`SEMANTIC_SCHOLAR_API_KEY`; without the latter, Semantic Scholar's shared
anonymous pool often answers 429. Publishers that withhold abstracts from these
indexes (common for Elsevier) stay missing.

The same lookup is exposed read-only on the knowledge-engine MCP server as
`lookup_paper_abstracts(dois, providers=None)` (at most 200 DOIs per call).
