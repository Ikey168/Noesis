# Web archive provenance across archives (Memento)

For any cited URL in any pack, Noesis can answer **"what did this page say on date X,
according to which archive"** across several web archives. It can also pin a citation to
one archived capture, so an evidence export survives link rot. Tracking issue: #2226.
The archive contracts and access decisions are in the
[archive audit](../roadmaps/platform-web-archives-source-audit.md).

This is a platform capability, not a pack. The `platform.web-archives` provider
(`packs/platform/providers/platform.web-archives.json`) can be composed by any bundle.
The Osint bundle composes it through its optional `web-archives` feature (default off).
All records live in the existing citation preservation store (`src/kb/citation_preservation.py`).
There is no new store.

## Archives

| Archive | Role | Decision |
|---|---|---|
| Time Travel aggregator | resolver (never recorded as the archive) | in scope |
| Internet Archive | Memento archive; TimeMap plus CDX digests | in scope |
| UK Web Archive | national Memento archive; reading-room captures listed, never fetched | in scope |
| Arquivo.pt | national Memento archive | in scope |
| Library of Congress, Vefsafn.is | national Memento archives | deferred (terms review) |
| BnF | legal deposit, on site only | excluded |
| archive.today | on-demand capture service | **excluded**: listed as `excluded_by_access_decision` in every answer |
| Common Crawl | crawl corpus, not Memento; never a TimeGate answer | in scope (index lookups) |

Live access is `unverified-live` until the bounded live run (#2348). Everything below runs
offline against synthetic fixtures (`tests/fixtures/web_archives/`).

## Journey: from a cited URL to a pinned capture

```python
from src.ingestion.memento import MementoClient
from src.kb.citation_preservation import CitationPreservationStore
from src.kb.web_archive_identity import CaptureMatcher
from src.kb.web_archive_queries import page_as_of

client = MementoClient(conn, "research", principal_id="alice", scopes=scopes)
# 1. Resolve: one aggregator TimeMap, direct archive TimeMaps, one Common Crawl index lookup.
client.resolve(url, request_id="r1", crawls=["CC-MAIN-2024-10"])
client.resolve(url, request_id="r2", via_aggregator=False,
               archives=["internet-archive", "uk-web-archive", "arquivo-pt"])
# 2. Ask: nearest captures before and after a date, per archive.
answer = page_as_of(conn, "research", url, "2024-02-20", scopes=scopes)
# 3. Match the cited URL (maybe a variant) to captures; non-exact matches need review.
matches = CaptureMatcher(conn).propose("research", "cite:1", cited_url, principal_id="alice", scopes=scopes)
CaptureMatcher(conn).review("research", match_id, "accept", "same page", principal_id="bob", scopes=scopes)
# 4. Pin, then export: the citation export carries archive_pin next to the untouched cited URL.
store = CitationPreservationStore(conn)
store.pin_citation("research", "cite:1", capture_id, cited_url, principal_id="alice", scopes=scopes)
store.export("research", ["cite:1"], scopes=scopes)
```

The same steps as MCP tools on the knowledge-engine server are
`resolve_web_archive_captures`, `web_page_as_of`, `list_web_archive_captures`,
`propose_web_archive_matches`, `review_web_archive_match`, `pin_citation_capture`,
`list_citation_pins`, `export_preserved_citations`, `web_archive_contracts` and
`web_archive_readiness`.

## What each answer keeps apart

Each archive gets exactly one of these outcomes:

- `captures_on_record`
- `no_capture_on_record` (a TimeMap read found none)
- `archive_unavailable`
- `excluded_by_access_decision` or `deferred_by_access_decision`
- `excluded_by_archive` (robots or administrative exclusion, honoured as published)
- `blocked_by_archive` (CAPTCHA or HTTP 429, never worked around)
- `not_reported_by_aggregator` (not proof of absence)
- `not_queried`

Captures with the same digest (algorithm and value) are grouped as the same content, and
every archive's record is kept. Digests are labelled `published` (CDX or index) or
`computed` (recomputed on a WARC range read). There is no content diffing and no inference
about dates between captures.

## Records

| Contract | What |
|---|---|
| `noesis-web-archive-capture-v1` | URI-R, URI-M, archive and resolver, Memento-Datetime, status, mimetype, labelled digests, access condition, receipt; idempotent on (archive, URI-M) |
| `noesis-web-archive-timemap-v1` | what one archive listed for a URI-R and when; an unchanged TimeMap adds nothing |
| `noesis-web-archive-match-v1` | exact / canonicalised / redirect-derived match under `wa-canon-v1`, with rules and review |
| `noesis-citation-pin-v1` | citation id, capture, pin time and actor, revision; the cited URL is never rewritten |

Matching uses the rules of `wa-canon-v1` (scheme and host case, http/https, default ports,
`www.`, trailing slash, tracking parameters, query order, fragment). Archive-reported
redirects are recorded as published and never followed. A rejected match never pins. The
shared review inbox can route `archive_match` targets.

## Monitoring

`create_web_archive_monitor` creates an ordinary knowledge subscription for a cited URL,
optionally naming the citation whose pin it guards. `check_web_archive_monitor` makes one
bounded round: one resolution, one request to the live URL and one request per pinned
capture. `run_web_archive_monitor` evaluates a committed watermark and emits:

- `new_capture`
- `live_url_failure` (only while a pinned capture exists)
- `pinned_capture_unavailable`

Each notification cites its capture, citation, pin and health record ids. Messages report
the HTTP outcome as observed ("HTTP 404 observed"), never a conclusion about the publisher.

## Save Page Now (write, off by default)

`request_save_page_now` asks the Internet Archive to make a new capture. It requires all of
the following:

- the dedicated scope `knowledge:citation:archive-request`
- the Osint bundle's optional `save-page-now` feature
- archive.org keys in `NOESIS_IA_S3_ACCESS_KEY` / `NOESIS_IA_S3_SECRET_KEY`

Each request records the requester, URL, time, job id, status and resulting URI-M. The
new capture can then be pinned. Each namespace gets 5 requests per UTC day, and each call
makes one status poll (`check_save_page_now` polls again later). Refused or robots-excluded
URLs are recorded as refused and never retried through another archive. While Save Page
Now is unverified-live, the live transport accepts only the audit's verification URL set.
Tests never reach it.

## Offline acceptance

`tests/unit/domains/test_web_archives_acceptance.py` runs the full journey from a cited URL
to a pinned capture in an evidence export.
