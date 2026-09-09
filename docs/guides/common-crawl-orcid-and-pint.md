# Common Crawl, ORCID and Pint integration results

These additions are explicit Python entry points, not newly enabled production
defaults. Results below were measured on 2026-09-06. Fixture tests are synthetic
contract tests; neither provider has a successful live observation in this pass.

## Common Crawl (#1498)

Install `.[archives]` (warcio 1.8.1). The public CDX index API and HTTPS WARC byte
ranges require no AWS account. `CommonCrawlCollection` in
`src/ingestion/common_crawl.py` requires an immutable crawl ID, exact host and
14-digit capture date window. For example:

```python
import duckdb
from src.ingestion.common_crawl import CommonCrawlCollection

conn = duckdb.connect("research.duckdb")
collection = CommonCrawlCollection(
    conn, "berlin-2025-05", crawl="CC-MAIN-2025-05", host="www.berlin.de",
    from_timestamp="20250101000000", to_timestamp="20250131235959",
    max_pages=2, max_records=10, max_requests=15, max_network_bytes=10_000_000,
)
while collection.inspect()["status"] not in {"complete", "bounded"}:
    collection.step()
```

Each step reserves its request and maximum response bytes durably before the GET.
A timeout consumes that reservation; resume never resets the ceiling. Defaults
are five index pages, 100 records, 110 requests, 50 MB total reserved network,
2 MB per index page/record and 15 seconds per request. Index page size means
ZipNum blocks, not a record count: a page exceeding the byte cap is rejected.
Narrow the query or explicitly raise the cap under a new collection ID.

WARC retrieval requires an exact 206 byte range, gzip bounds, WARC checksums and
agreement with the CDX URL, capture date and SHA-1 digest. Native archive import
keeps HTTP headers and original URL. Collection receipts retain crawl IDs and
CDX records; binary payload hashes deduplicate content across crawls while each
capture remains an observation. Reopening the same database resumes the stored
page/pending record. A terminal collection makes no further requests. Changed
configuration under the same ID is rejected.

The seven tests cover pagination/restart, cross-crawl deduplication, bad ranges,
corruption, wrong digest, oversized records, no captures and durable request
limits. No crawl is a complete website history. The index filters successful
responses and the adapter imports only response records; missing/revisit entries
are not silently treated as captured page content. Storage includes payloads,
headers and receipts and grows with distinct content and capture count; the
network cap is not a database disk quota. Data downloads are free, while local
storage, networking and processing incur operator costs. Crawled works retain
their own access/reuse conditions. See [index API](https://index.commoncrawl.org/),
[access instructions](https://commoncrawl.org/get-started) and
[terms](https://commoncrawl.org/terms-of-use).

## ORCID (#1474)

`src.ingestion.orcid.acquire_author` reads the v3.0 public `/record` endpoint.
Supply a `/read-public` token from the caller's credential store as
`access_token`; the adapter neither registers credentials nor persists the token.
Public API credentials are subject to ORCID's non-commercial use terms; choose
the appropriate membership/API arrangement for other use. See the
[read tutorial](https://info.orcid.org/documentation/api-tutorials/api-tutorial-read-data-on-a-record/)
and [Public API terms](https://info.orcid.org/public-client-terms-of-service/).

Call with a DuckDB connection, checksum-valid ORCID, unique `request_id`,
`namespace`, authenticated `principal_id`, current `scopes`, and optional
`candidates=[{"entity_id": "existing-local-id", "orcid": "0000-..."}]`.
Candidate identities must already exist. Operators are supported; other callers
need namespace write, entity-history read/review and inbox read/write scopes,
plus document visibility when creating an inbox task. The language is explicit
(`de` or `en`, for example). One GET per new request, 15-second timeout, default
2 MB response and 200 public activities; hard maxima 20 MB/1000 activities and
20 local candidates. Rate-limit errors are surfaced without automatic retry.

Only PUBLIC names, work summaries and employment/education/qualification
summaries are mapped. Missing/private fields remain missing. Stored snapshots
are labelled normalized public summaries rather than full raw responses; source
identifiers, put-codes, attribution and modification dates accompany the data.
Changed affiliations produce a new document revision. Exactly one supplied
matching ORCID is `identifier_confirmed`; name-only or conflicting candidates
produce proposed entity-history decisions and existing review-inbox tasks.
No match merges identities or overwrites canonical properties. Successful
request replay checks current authorization and returns the durable receipt.
401/403/404 return `unavailable`; these failures are not cached as successes.

Three tests cover public/private mapping, checksum/limits, German umlaut names,
same-name ambiguity, changed affiliations, explicit identifiers, denied access
and database-reopen replay. These establish adapter behavior, not measured
author-disambiguation accuracy. Live access remains unverified because no token
was configured.

## Pint (#1517): defer default adoption

Install `.[unit-evaluation]` (Pint **0.25.2**, BSD-3-Clause). An isolated Decimal
registry maps immutable Noesis unit IDs, factors, offsets and aliases. No Pint
global registry or arbitrary caller-supplied definition text is used. A cache
holds at most 32 registries. Conversions use decimal precision 28 and half-even
output rounding capped at 12 places. Currency/rate dates, exchange evidence,
statistical series/population comparability and vintages remain with the existing
Noesis APIs. Currency input is explicitly rejected by this adapter.

`PintUnitEvaluation(QuantitativeStore(conn)).convert(...)` has the native
conversion arguments and returns an existing quantitative calculation receipt,
including input unit revisions, backend/version and registry hash. Replay
verifies the durable result rather than recomputing under newer definitions.
`product_dimensions(namespace, terms, scopes=...)` is a bounded dimension helper
(20 terms, integer powers -8..8). Absolute Celsius in products is rejected;
temperature differences require an explicit unit definition. Existing formula
evaluation validates declared input dimensions; this candidate additionally
derives product dimensions. It does not replace the production formula engine.

The six independently specified arithmetic examples cover distance, both
Celsius/Kelvin directions, percentages, duration and mass. Native and candidate
matched all six exactly. On Python 3.14.6, 120 receipt-producing calls per backend
gave native/Pint first-call latency **5.64/56.65 ms**, warm median **2.70/2.85 ms**;
whole-process peak RSS was **217220 KiB**, not incremental Pint memory. Seven
focused tests also cover compound km/h aliases, energy dimensions, incompatible
and unknown units, currencies, offsets and receipt replay.

Decision: retain this optional evaluation; defer a default switch. Conversion
parity on this small suite offers no demonstrated improvement to justify a
new production dependency, and cold startup is slower. Compound-dimension
checking is useful but does not establish scientific formula coverage.
See [Pint definitions](https://pint.readthedocs.io/en/stable/advanced/defining.html)
and [offset units](https://pint.readthedocs.io/en/stable/user/nonmult.html).

## Reproduce

```sh
.venv/bin/python -m pytest tests/unit/ingestion/test_common_crawl.py tests/unit/ingestion/test_orcid.py tests/unit/kb/test_pint_evaluation.py -q --override-ini addopts=''
.venv/bin/python scripts/evaluate_pint.py --out docs/development/workflow-implementation-evidence/pint-evaluation.json
```

Result: **17 passed**. Benchmark JSON is recorded in the linked evidence path;
latency will vary with the host. No paid-provider or human-label acceptance is
claimed by these tests.
