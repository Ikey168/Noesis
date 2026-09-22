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

Network safety: credential-free HTTPS only, per-source host allowlist, and the
resolved address must be public (no SSRF). Set `NOESIS_SCHOLARLY_CONTACT` (a
contact email) to be polite to the APIs' rate limits. `http_get` and the DNS
resolver are injectable for offline tests.
