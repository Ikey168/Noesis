# Source collection and scraping backlog

Updated 2026-09-05. Repository: `Ikey168/Noesis`.

Published 44 focused implementation/evaluation issues under the 12 original tracking parents. Every child has a native GitHub parent relationship, acceptance criteria, code pointers and prerequisite issue links where needed. Parent checklists retain the original scope and evidence.

The repository remains unchanged apart from this backlog document; no scraper implementation or vendor subscription was added.

## Tracking parents

| Parent | Scope | Tasks |
|---|---|---|
| [#1337](https://github.com/Ikey168/Noesis/issues/1337) | Fix provider-specific requests, mapping, and pagination in source-pack execution | 6 |
| [#1338](https://github.com/Ikey168/Noesis/issues/1338) | Extend domain source discovery with scholarly search, sitemaps, and optional web search | 5 |
| [#1339](https://github.com/Ikey168/Noesis/issues/1339) | Acquire scholarly full text through Unpaywall and Europe PMC with versioned provenance | 4 |
| [#1340](https://github.com/Ikey168/Noesis/issues/1340) | Benchmark Docling and GROBID as optional structured document parsers | 3 |
| [#1341](https://github.com/Ikey168/Noesis/issues/1341) | Add conditional feed fetching and explicit unchanged collection outcomes | 3 |
| [#1342](https://github.com/Ikey168/Noesis/issues/1342) | Package supported ingestion and browser dependencies as installable extras | 3 |
| [#1343](https://github.com/Ikey168/Noesis/issues/1343) | Repair article discovery and HTTP-to-browser fallback in existing scrapers | 4 |
| [#1344](https://github.com/Ikey168/Noesis/issues/1344) | Prevent indefinite Scrapy cache reuse from hiding source updates | 2 |
| [#1345](https://github.com/Ikey168/Noesis/issues/1345) | Close Playwright pages on download failures and bound browser resource use | 3 |
| [#1346](https://github.com/Ikey168/Noesis/issues/1346) | Preserve structured page metadata with extruct and honest missing-date handling | 3 |
| [#1347](https://github.com/Ikey168/Noesis/issues/1347) | Benchmark optional crawl backends against the repaired Scrapy and Playwright pipeline | 5 |
| [#1348](https://github.com/Ikey168/Noesis/issues/1348) | Connect the existing Guardian API collector to domain source packs and incremental ingestion | 3 |

## Implementation order

Start with independently actionable correctness fixes: native provider mappings, the missing HTTP link extractor, Playwright href extraction, Guardian year filtering, cache policy and browser failure cleanup. Dependent tasks identify their prerequisites below. Evaluate alternative backends after the baseline fixes.

## #1337: Fix provider-specific requests, mapping, and pagination in source-pack execution

### [#1349: Implement native Crossref works requests and response mapping](https://github.com/Ikey168/Noesis/issues/1349)

The generic adapter does not read message.items/message.next-cursor and sends limit instead of rows.

Acceptance criteria:

- Map DOI/search/date operations, rows and cursors to Crossref requests; configure contact information separately from authentication.
- Normalize DOI, title arrays, authors and provider dates; malformed envelopes fail explicitly.
- Recorded native fixtures cover two pages, cursor restart and repeated records without duplicate logical documents.

Code pointers: src/ingestion/source_pack_runtime.py; config/source_packs/research.json.

### [#1350: Implement native OpenAlex works pagination and field mapping](https://github.com/Ikey168/Noesis/issues/1350)

OpenAlex results are recognized, but meta.next_cursor is discarded and the generic request does not use per_page.

Acceptance criteria:

- Translate supported operations to OpenAlex parameters and follow meta.next_cursor within page/result budgets.
- Map provider ID, DOI, authorships, dates and abstract availability without presenting metadata JSON as full text.
- Cover two native pages, empty completion and resumed collection; keep API credentials out of receipts.

Code pointers: src/ingestion/source_pack_runtime.py; config/source_packs/research.json.

### [#1351: Implement native Europe PMC search requests and result mapping](https://github.com/Ikey168/Noesis/issues/1351)

The generic adapter treats resultList.result and nextCursorMark as an envelope document.

Acceptance criteria:

- Use native query/pageSize/cursorMark parameters and explicitly request the supported response format.
- Normalize publication IDs, DOI, authors, dates and availability indicators.
- Native fixtures prove two-page collection and restart; malformed responses report schema drift.

Code pointers: src/ingestion/source_pack_runtime.py; config/source_packs/scientific.json.

### [#1352: Normalize real HTTP failures in the source-pack transport](https://github.com/Ikey168/Noesis/issues/1352)

The urllib transport raises HTTPError before fetch_page can inspect 429/401/403, while injected transports return status dictionaries.

Acceptance criteria:

- Normalize live HTTPError status, bounded body and headers into the same handling path as injected transport responses.
- Retain Retry-After for 429; classify authentication, timeout and unavailability without leaking credentials.
- A local transport test exercises actual urllib 429/401/503 responses and proves the intended runtime retry behavior.

Code pointers: src/ingestion/source_pack_runtime.py:413; tests/unit/ingestion/test_source_pack_runtime.py.

### [#1353: Distinguish fixture conformance from live provider readiness](https://github.com/Ikey168/Noesis/issues/1353)

Passing simplified fixtures does not prove that a source-pack declaration supports its named live provider.

Depends on: [#1349](https://github.com/Ikey168/Noesis/issues/1349), [#1350](https://github.com/Ikey168/Noesis/issues/1350), [#1351](https://github.com/Ikey168/Noesis/issues/1351).

Acceptance criteria:

- Report fixture-tested and live-verified status separately, including adapter version, verification time and failures.
- Add opt-in bounded live checks for Crossref, OpenAlex and Europe PMC; keep deterministic CI offline.
- A provider whose native mapping is unavailable must not be reported as fully ready.

Code pointers: src/ingestion/source_packs.py; src/ingestion/source_pack_runtime.py; source-pack conformance tests.

### [#1354: Audit remaining source packs for native protocol compatibility](https://github.com/Ikey168/Noesis/issues/1354)

Every connector kind currently defaults to HTTPSPageAdapter, including datasets, filings and XML-oriented sources.

Acceptance criteria:

- For political/economic/technical/OSINT/scientific sources beyond the three scholarly adapters, record operation URL, HTTP method, auth, record path and pagination compatibility.
- Check representative native fixtures and distinguish existing dedicated connectors from generic placeholders.
- Publish a per-source ready/unsupported table and actionable mappings; do not implement every provider in this audit.

Code pointers: config/source_packs/; src/ingestion/source_pack_runtime.py; src/ingestion/connectors/dataset/.

## #1338: Extend domain source discovery with scholarly search, sitemaps, and optional web search

### [#1355: Add topic, author and date search to PaperConnector discovery](https://github.com/Ikey168/Noesis/issues/1355)

PaperConnector.discover currently accepts arXiv IDs rather than a scholarly search objective.

Acceptance criteria:

- Accept an explicit structured search query while preserving existing ID-based calls.
- Build encoded arXiv search requests with bounded pagination and provider pacing; preserve version metadata.
- Atom fixtures cover topic/author/date filters, no results and result/page limits.

Code pointers: src/ingestion/connectors/paper/connector.py; src/ingestion/connectors/paper/arxiv.py.

### [#1356: Route domain research objectives to scholarly discovery operations](https://github.com/Ikey168/Noesis/issues/1356)

The acquisition planner needs a concrete mapping from a domain objective to provider-supported scholarly queries.

Depends on: [#1349](https://github.com/Ikey168/Noesis/issues/1349), [#1350](https://github.com/Ikey168/Noesis/issues/1350).

Acceptance criteria:

- Translate question/topic, author and freshness constraints into declared OpenAlex/Crossref operations.
- Keep provider selection, budgets, exclusions and discovered-work provenance in existing plan receipts.
- An offline domain workflow discovers work IDs without a manually supplied list and resumes deterministically.

Code pointers: src/kb/source_planner.py; config/source_packs/research.json.

### [#1357: Discover feed and sitemap URLs from configured domain websites](https://github.com/Ikey168/Noesis/issues/1357)

Trafilatura is used for article extraction but website discovery is not exposed through domain collection.

Acceptance criteria:

- Add bounded feed/sitemap discovery from explicit seed websites using Trafilatura's existing APIs.
- Resolve relative URLs, normalize candidates and retain source/discovery provenance under configured host scope.
- Fixtures cover sitemap indexes, duplicate URLs, invalid XML and a site with no sitemap/feed.

Code pointers: src/ingestion/connectors/; src/ingestion/extract.py; src/kb/source_planner.py.

### [#1358: Persist a bounded website crawl frontier for domain discovery](https://github.com/Ikey168/Noesis/issues/1358)

Website exploration needs restartable URL state so each run does not revisit the same pages or expand without bound.

Depends on: [#1357](https://github.com/Ikey168/Noesis/issues/1357).

Acceptance criteria:

- Persist pending/visited/failed URLs scoped to the domain and source; reuse existing store conventions.
- Enforce host, depth, page and time limits and retain discovery lineage.
- A local multi-page site can be interrupted and resumed with no duplicate completed fetches; links outside scope stay excluded.

Code pointers: src/ingestion/connectors/; src/ingestion/source_pack_runtime.py.

### [#1359: Add an optional Brave Search source-discovery adapter](https://github.com/Ikey168/Noesis/issues/1359)

Domains need a budgeted way to discover candidate sources beyond configured websites and feeds.

Acceptance criteria:

- Implement query, freshness, language and country parameters under existing source-planner credential/cost controls.
- Return candidate URLs with query/provider provenance; search snippets remain discovery metadata.
- Native fixtures cover pagination, exhausted budgets and no results; demonstrate candidate URLs entering normal acquisition.

Code pointers: src/kb/source_planner.py; src/ingestion/source_pack_runtime.py; config/source_packs/.

## #1339: Acquire scholarly full text through Unpaywall and Europe PMC with versioned provenance

### [#1360: Resolve DOI open-access locations through Unpaywall](https://github.com/Ikey168/Noesis/issues/1360)

Paper acquisition needs a DOI-to-accessible-copy resolution step before attempting full-text retrieval.

Acceptance criteria:

- Add a bounded Unpaywall resolver with explicit contact configuration and native response parsing.
- Return candidate URLs with version, host and license fields, preserving unknown licenses and absent OA locations.
- Fixtures cover no OA copy, multiple versions and malformed responses; this task does not download the document.

Code pointers: src/ingestion/connectors/paper/; config/source_packs/research.json.

### [#1361: Download scholarly full text into content-addressed snapshots](https://github.com/Ikey168/Noesis/issues/1361)

PaperConnector stores a remote PDF URL but does not acquire its bytes through the normal paper path.

Depends on: [#1360](https://github.com/Ikey168/Noesis/issues/1360).

Acceptance criteria:

- Fetch a selected full-text URL under timeout, byte, redirect and source limits; reuse existing snapshot storage.
- Record original URL, final URL, content type, fetch time and digest; failed downloads preserve abstract-only results.
- Tests cover repeated downloads, changed content, oversized response and timeout without false full-text success.

Code pointers: src/ingestion/connectors/paper/connector.py; src/ingestion/snapshots.py.

### [#1362: Parse Europe PMC full-text XML into cited document sections](https://github.com/Ikey168/Noesis/issues/1362)

Europe PMC can supply structured article content; Noesis needs a section/reference-preserving ingestion path.

Depends on: [#1351](https://github.com/Ikey168/Noesis/issues/1351).

Acceptance criteria:

- Retrieve available full-text XML for a resolved record using the existing acquisition bounds.
- Map sections, paragraphs, references and stable XML locators into DocumentStore-compatible records.
- Fixtures cover unavailable full text, malformed XML and citations across sections; retain the source XML snapshot.

Code pointers: src/ingestion/connectors/paper/; src/ingestion/document_store.py.

### [#1363: Expose scholarly content coverage and manuscript version relationships](https://github.com/Ikey168/Noesis/issues/1363)

Metadata, abstracts and acquired full text are different evidence coverage levels; multiple manuscript versions must remain identifiable.

Depends on: [#1361](https://github.com/Ikey168/Noesis/issues/1361), [#1362](https://github.com/Ikey168/Noesis/issues/1362).

Acceptance criteria:

- Persist explicit metadata-only/abstract-only/full-text/unavailable/failed acquisition outcomes.
- Link preprint, accepted and published versions using provider identifiers without overwriting distinct content revisions.
- An acquisition-to-query regression demonstrates honest coverage and stable version identity on repeated ingestion.

Code pointers: src/ingestion/connectors/paper/models.py; src/ingestion/connectors/paper/connector.py; src/ingestion/document_store.py.

## #1340: Benchmark Docling and GROBID as optional structured document parsers

### [#1364: Build a structured-PDF evaluation corpus and PyMuPDF baseline](https://github.com/Ikey168/Noesis/issues/1364)

Parser comparisons need one reproducible corpus and shared fidelity/resource measurements.

Acceptance criteria:

- Assemble a redistributable corpus with scans, digital PDFs, multi-column text, tables and bibliographies, plus annotated expected structure/locators.
- Measure text/reading-order/table/citation fidelity, latency and memory using the existing parser.
- Publish corpus provenance, scoring definitions, baseline outputs and a reproducible command.

Code pointers: src/ingestion/connectors/paper/pdf_parser.py; tests/fixtures/; scripts/.

### [#1365: Evaluate Docling against the structured-PDF baseline](https://github.com/Ikey168/Noesis/issues/1365)

Docling may improve layout/table/OCR extraction, but its benefit and operational cost have not been measured on Noesis documents.

Depends on: [#1364](https://github.com/Ikey168/Noesis/issues/1364).

Acceptance criteria:

- Add an optional evaluation adapter preserving structured JSON and page/section/box provenance.
- Run the shared corpus; report fidelity, latency, memory, model dependencies and failures versus PyMuPDF.
- Record an adopt/defer decision and fallback requirements; keep production behavior unchanged until that decision.

Code pointers: src/ingestion/connectors/paper/pdf_parser.py; evaluation scripts.

### [#1366: Evaluate GROBID for scientific sections and citation references](https://github.com/Ikey168/Noesis/issues/1366)

GROBID needs a separate assessment focused on scientific structure, reference extraction and locator preservation.

Depends on: [#1364](https://github.com/Ikey168/Noesis/issues/1364).

Acceptance criteria:

- Add an optional evaluation adapter for TEI output, bibliographic references and PDF coordinates.
- Run the shared scientific-paper corpus and report fidelity plus service/runtime requirements versus the baseline.
- Publish an adopt/defer decision with versioned configuration and failure cases.

Code pointers: src/ingestion/connectors/paper/pdf_parser.py; src/ingestion/connectors/paper/references.py; evaluation scripts.

## #1341: Add conditional feed fetching and explicit unchanged collection outcomes

### [#1367: Persist feed HTTP validators and send conditional requests](https://github.com/Ikey168/Noesis/issues/1367)

BlogConnector discards ETag/Last-Modified and repeatedly downloads unchanged feeds.

Acceptance criteria:

- Persist validators per feed URL/request representation and send If-None-Match/If-Modified-Since.
- Expose 304 as an explicit unchanged fetch result while retaining validator updates correctly.
- A local server proves 200 -> 304 -> changed 200 across a process restart; unrelated feeds do not share validators.

Code pointers: src/ingestion/connectors/blog/connector.py; src/ingestion/connectors/base.py.

### [#1368: Record unchanged feed checks without degrading source health](https://github.com/Ikey168/Noesis/issues/1368)

A 304 or unchanged article must count as a successful check rather than an empty extraction failure.

Depends on: [#1367](https://github.com/Ikey168/Noesis/issues/1367).

Acceptance criteria:

- Consume explicit unchanged results, advance last-successful-check time and retain last-content-change time separately.
- Skip unnecessary full-body acquisition for unchanged entries while retaining scheduled article revalidation.
- Tests prove unchanged checks neither degrade the source nor create revisions, and later source edits are still detected.

Code pointers: src/ingestion/source_health.py; src/ingestion/connectors/base.py; src/ingestion/connectors/blog/connector.py; src/ingestion/refetch.py.

### [#1369: Honor Retry-After in feed and article fetch retries](https://github.com/Ikey168/Noesis/issues/1369)

The base connector uses generic exponential backoff and does not incorporate provider Retry-After guidance.

Acceptance criteria:

- Carry HTTP status/Retry-After from feed and article fetchers into bounded retry scheduling.
- Handle both delta-seconds and HTTP-date values, with per-host pacing and a documented bound when guidance exceeds the run budget.
- Fake-clock/local-server tests cover 429, malformed headers, exhausted budgets and successful retry.

Code pointers: src/ingestion/connectors/base.py; src/ingestion/connectors/blog/connector.py; src/ingestion/connectors/blog/readability.py.

## #1342: Package supported ingestion and browser dependencies as installable extras

### [#1370: Declare an ingestion extra for RSS and HTML extraction](https://github.com/Ikey168/Noesis/issues/1370)

feedparser is imported by BlogConnector and Trafilatura/readability are in requirements.txt, but root project extras do not supply this supported path.

Acceptance criteria:

- Declare an ingestion dependency group and align the supported dependencies with requirements.txt.
- A wheel installed outside the checkout parses a feed and extracts HTML through the documented cascade.
- Document the install command and preserve minimal CLI installation.

Code pointers: pyproject.toml; requirements.txt; .github/workflows/cli-smoke.yml.

### [#1371: Declare a browser-scraping extra and browser installation path](https://github.com/Ikey168/Noesis/issues/1371)

Scrapy settings and the browser spider require scrapy_playwright, but browser dependencies and executable setup lack a coherent package extra.

Acceptance criteria:

- Package compatible Scrapy/Playwright integration dependencies separately from minimal ingestion.
- Document browser executable installation and diagnose a missing executable explicitly.
- Fresh-install CI launches and closes a browser against a local page outside the checkout.

Code pointers: pyproject.toml; requirements.txt; src/scraper/settings.py; .github/workflows/cli-smoke.yml.

### [#1372: Report connector dependency readiness without breaking unrelated imports](https://github.com/Ikey168/Noesis/issues/1372)

Optional connector imports should not prevent other source types from loading or advertise unavailable backends as ready.

Depends on: [#1370](https://github.com/Ikey168/Noesis/issues/1370), [#1371](https://github.com/Ikey168/Noesis/issues/1371).

Acceptance criteria:

- Guard optional imports at the appropriate connector boundary and expose the missing package/backend in readiness.
- Doctor/capability output gives the exact supported extra installation command.
- Tests load the minimal installation and prove one missing feed/browser dependency does not disable unrelated connectors.

Code pointers: src/ingestion/connectors/__init__.py; src/ingestion/connectors/registry.py; src/noesis_cli/.

## #1343: Repair article discovery and HTTP-to-browser fallback in existing scrapers

### [#1373: Implement the missing async HTTP article-link extractor](https://github.com/Ikey168/Noesis/issues/1373)

get_article_links_http calls self.extract_links_from_html, but AsyncNewsScraperEngine defines no such method.

Acceptance criteria:

- Implement or reuse link extraction with relative-URL resolution, source scope and stable deduplication.
- An actual get_article_links_http call against local HTML returns article URLs without swallowing AttributeError.
- An invalid page reports a distinct discovery failure; cover empty, duplicate and out-of-scope links.

Code pointers: src/scraper/async_scraper_engine.py:get_article_links_http.

### [#1374: Fix Playwright article-link extraction to read href](https://github.com/Ikey168/Noesis/issues/1374)

Both PlaywrightNewsSpider.parse link loops call get_attribute('hre'), so ordinary anchor links are missed.

Acceptance criteria:

- Read href in both specific-selector and fallback loops and resolve relative links correctly.
- A focused regression exercises both loops with real href attributes and confirms the expected follow-up Requests.

Code pointers: src/scraper/spiders/playwright_spider.py:64; src/scraper/spiders/playwright_spider.py:77.

### [#1375: Remove hard-coded 2024/2025 URL filtering from Guardian discovery](https://github.com/Ikey168/Noesis/issues/1375)

GuardianSpider.parse excludes article URLs from later years because its selectors only match /2024/ and /2025/.

Acceptance criteria:

- Use a maintained article-link rule that does not require annual code edits.
- Fixtures discover historical, current and future-year article URLs while rejecting unrelated navigation links.

Code pointers: src/scraper/spiders/guardian_spider.py:25.

### [#1376: Allow browser retries for URLs whose HTTP extraction failed](https://github.com/Ikey168/Noesis/issues/1376)

The HTTP path adds URLs to shared seen_urls before success; the browser path can then skip the same failed URLs.

Depends on: [#1373](https://github.com/Ikey168/Noesis/issues/1373).

Acceptance criteria:

- Represent in-flight, succeeded and retryable-failed URL states so HTTP failure permits a bounded browser attempt.
- Retain FetchEscalationPolicy and avoid duplicate successful ingestion or infinite fallback loops.
- A regression demonstrates HTTP extraction failure followed by browser success for the same URL.

Code pointers: src/scraper/async_scraper_engine.py:scrape_http_source; src/scraper/async_scraper_engine.py:scrape_js_source.

## #1344: Prevent indefinite Scrapy cache reuse from hiding source updates

### [#1377: Set an explicit live Scrapy cache revalidation policy](https://github.com/Ikey168/Noesis/issues/1377)

Enabled DummyPolicy caching with zero expiry can serve old pages indefinitely and retain transient error responses.

Acceptance criteria:

- Configure bounded expiry or HTTP-aware revalidation for live recurring scrapes, with explicit 429/5xx behavior.
- A local server test with a persistent cache observes a changed page and recovers from a transient error without deleting cache files.
- Document the live policy and its freshness bound.

Code pointers: src/scraper/settings.py:96; scraper cache integration tests.

### [#1378: Separate offline cache replay from live fetch provenance](https://github.com/Ikey168/Noesis/issues/1378)

Reading a cached page must not make old source content appear newly fetched or generate a false correction.

Depends on: [#1377](https://github.com/Ikey168/Noesis/issues/1377).

Acceptance criteria:

- Expose original fetch time, last successful revalidation time and cache/replay status separately.
- Provide an explicit deterministic offline replay mode that does not claim live freshness.
- A restart/revalidation integration test verifies unchanged/changed revision and health outcomes under both modes.

Code pointers: src/scraper/settings.py; src/ingestion/refetch.py; src/ingestion/revisions.py.

## #1345: Close Playwright pages on download failures and bound browser resource use

### [#1379: Close included Playwright pages from request errbacks](https://github.com/Ikey168/Noesis/issues/1379)

Both browser Request types include Page objects but have no errback; failures before callbacks bypass their finally cleanup.

Acceptance criteria:

- Add failure cleanup for owned pages on navigation/wait errors and cancellation, without closing shared contexts.
- A local regression produces repeated failures beyond the page limit and then successfully fetches a valid page.
- Retain normal callback cleanup and verify no leaked page remains after shutdown.

Code pointers: src/scraper/spiders/playwright_spider.py.

### [#1380: Configure browser page/context concurrency and shutdown ownership](https://github.com/Ikey168/Noesis/issues/1380)

Browser resource limits and ownership need explicit configuration so source fan-out cannot exhaust contexts/pages.

Depends on: [#1379](https://github.com/Ikey168/Noesis/issues/1379).

Acceptance criteria:

- Set configurable finite page/context limits and identify who owns each resource.
- Ensure cancellation/shutdown releases owned resources and reports timeouts/cleanup outcomes in health.
- A bounded multi-source local workload respects limits and exits cleanly.

Code pointers: src/scraper/settings.py; src/scraper/spiders/playwright_spider.py; src/scraper/async_scraper_engine.py.

### [#1381: Replace fixed browser sleeps with bounded readiness rules](https://github.com/Ikey168/Noesis/issues/1381)

PlaywrightNewsSpider waits an extra two seconds on every page after a selector is ready.

Depends on: [#1379](https://github.com/Ikey168/Noesis/issues/1379).

Acceptance criteria:

- Replace unconditional wait_for_timeout calls with configurable bounded content-readiness conditions.
- Cover fast static pages, delayed article content and a page that never becomes ready.
- Record readiness timeouts distinctly; do not suppress images/other assets needed by enabled evidence collection.

Code pointers: src/scraper/spiders/playwright_spider.py.

## #1346: Preserve structured page metadata with extruct and honest missing-date handling

### [#1382: Keep unknown publication dates distinct from scrape time](https://github.com/Ikey168/Noesis/issues/1382)

GuardianSpider and PlaywrightNewsSpider substitute datetime.now() when no publication date is present.

Acceptance criteria:

- Emit an unknown publication date when evidence is absent and preserve collection time in its own field.
- Retain provider publication/modification dates with timezone semantics when available.
- Fixtures with missing dates and timezone offsets verify no fabricated publication timestamp reaches ingestion.

Code pointers: src/scraper/spiders/guardian_spider.py; src/scraper/spiders/playwright_spider.py; src/ingestion/extract.py.

### [#1383: Add a shared structured HTML metadata extractor using extruct](https://github.com/Ikey168/Noesis/issues/1383)

Repeated CSS field scraping misses embedded JSON-LD/Open Graph and other structured metadata.

Depends on: [#1370](https://github.com/Ikey168/Noesis/issues/1370).

Acceptance criteria:

- Parse already-fetched HTML with an optional extruct backend and retain field-level origin metadata.
- Resolve multiple JSON-LD entities/@graph with deterministic selection; retain conflicts and malformed-field diagnostics.
- Fixtures cover multiple articles, metadata/visible-content disagreement, canonical URL candidates and authors; canonical tags alone cannot merge documents.

Code pointers: src/ingestion/extract.py; src/scraper/spiders/; pyproject.toml.

### [#1384: Retain extraction versions, field locators and heuristic score semantics](https://github.com/Ikey168/Noesis/issues/1384)

ExtractResult currently exposes a hard-coded method score; metadata transformations need traceable origins and honest score meaning.

Depends on: [#1383](https://github.com/Ikey168/Noesis/issues/1383).

Acceptance criteria:

- Carry extractor name/version and source snapshot/field locators through the normalized document metadata.
- Preserve structured metadata candidates with the selected value and precedence reason.
- Label method scores as heuristic tiers rather than calibrated probabilities; verify serialization and downstream preservation.

Code pointers: src/ingestion/extract.py; src/ingestion/document_store.py; services/ingest/common/document_model.py.

## #1347: Benchmark optional crawl backends against the repaired Scrapy and Playwright pipeline

### [#1385: Build a reproducible scraping backend benchmark](https://github.com/Ikey168/Noesis/issues/1385)

Alternative scraping backends need a common benchmark against the repaired existing implementation.

Depends on: [#1373](https://github.com/Ikey168/Noesis/issues/1373), [#1374](https://github.com/Ikey168/Noesis/issues/1374), [#1376](https://github.com/Ikey168/Noesis/issues/1376), [#1377](https://github.com/Ikey168/Noesis/issues/1377), [#1379](https://github.com/Ikey168/Noesis/issues/1379).

Acceptance criteria:

- Create a bounded corpus covering static/JS pages, pagination, lazy loading, errors and source edits.
- Measure usable-document fidelity, metadata/locator preservation, completion, p50/p95 latency, memory and restart behavior.
- Publish baseline versions/configuration/results and cost accounting conventions without invoking paid backends.

Code pointers: scripts/; tests/fixtures/; src/scraper/.

### [#1386: Evaluate Crawl4AI on the scraping benchmark](https://github.com/Ikey168/Noesis/issues/1386)

Assess whether Crawl4AI improves structured extraction and dynamic-page collection over the repaired Noesis pipeline.

Depends on: [#1385](https://github.com/Ikey168/Noesis/issues/1385).

Acceptance criteria:

- Implement a benchmark-only adapter preserving HTML/rendered artifacts and locators alongside Markdown.
- Run the common corpus and report fidelity, resources, restart behavior and an adopt/defer decision.

Code pointers: scraping benchmark adapters; scripts/.

### [#1387: Evaluate Crawlee Python for resumable adaptive crawling](https://github.com/Ikey168/Noesis/issues/1387)

Assess whether Crawlee's queues and HTTP/browser orchestration justify replacing any of the custom async engine.

Depends on: [#1385](https://github.com/Ikey168/Noesis/issues/1385).

Acceptance criteria:

- Implement a benchmark-only adapter and exercise persistent URL queues, restart and HTTP/browser selection.
- Report common benchmark results plus migration cost and an adopt/defer decision; do not replace the production runtime.

Code pointers: scraping benchmark adapters; scripts/.

### [#1388: Evaluate Zyte API through the existing Scrapy integration boundary](https://github.com/Ikey168/Noesis/issues/1388)

Assess an opt-in managed fetch backend with scrapy-zyte-api rather than a separate production orchestrator.

Depends on: [#1385](https://github.com/Ikey168/Noesis/issues/1385).

Acceptance criteria:

- Build an opt-in benchmark adapter with credential references and hard request/spend limits; use fixtures without credentials.
- With authorized access, record usable-document fidelity, latency and actual cost; otherwise explicitly report live evaluation unavailable.
- Preserve final source URL, fetch time, provider identity and available response artifacts; publish an adopt/defer report.

Code pointers: scraping benchmark adapters; scripts/.

### [#1389: Evaluate Firecrawl as an optional scrape API backend](https://github.com/Ikey168/Noesis/issues/1389)

Assess Firecrawl's HTML/structured outputs against Noesis evidence preservation and operational requirements.

Depends on: [#1385](https://github.com/Ikey168/Noesis/issues/1385).

Acceptance criteria:

- Build an opt-in benchmark adapter with credential references and hard request/spend limits; use fixtures without credentials.
- With authorized access, measure fidelity, latency, cost and restart behavior; distinguish transformed HTML from original response bytes.
- Publish an adopt/defer report; keep private documents out of hosted evaluation by default.

Code pointers: scraping benchmark adapters; scripts/.

## #1348: Connect the existing Guardian API collector to domain source packs and incremental ingestion

### [#1390: Add pagination and date windows to the existing Guardian API client](https://github.com/Ikey168/Noesis/issues/1390)

The existing _fetch_guardian_data implementation fetches one page and does not apply supplied date-window arguments.

Acceptance criteria:

- Apply supported from/to dates, section/query filters and bounded native pagination.
- Expose checkpoint state for incremental resume and preserve provider article IDs.
- Native fixtures cover two pages, date boundaries, rate limits and restarting without duplicate logical records.

Code pointers: src/scraper/extensions/connectors/news_aggregator_connector.py:199.

### [#1391: Expose Guardian API collection through domain source packs](https://github.com/Ikey168/Noesis/issues/1391)

Guardian API support exists in a legacy aggregator but is not exposed as a maintained domain acquisition adapter.

Depends on: [#1390](https://github.com/Ikey168/Noesis/issues/1390).

Acceptance criteria:

- Wrap the existing client with the current planner/runtime interface and declare operations, readiness, credentials and quotas.
- Normalize body HTML with its source snapshot, canonical URL, provider ID, dates and contributor metadata.
- Demonstrate fixture-based domain ingestion and an opt-in live check with explicit access/license requirements.

Code pointers: src/scraper/extensions/connectors/news_aggregator_connector.py; src/ingestion/source_pack_runtime.py; config/source_packs/.

### [#1392: Deduplicate Guardian API, RSS and HTML representations by article identity](https://github.com/Ikey168/Noesis/issues/1392)

The same Guardian article may enter through three collection paths and must not count as independent corroboration.

Depends on: [#1391](https://github.com/Ikey168/Noesis/issues/1391).

Acceptance criteria:

- Resolve API/RSS/HTML representations to stable article identity while preserving their separate acquisition receipts and revisions.
- Make configured acquisition preference and fallback explicit without losing changes or falsely combining distinct articles.
- A three-representation fixture produces one reporting origin and retained provenance; label aggregator-truncated content as partial.

Code pointers: src/ingestion/scrapy_integration.py; src/ingestion/document_store.py; source identity/independence integration.

## Review evidence and limits

The source-pack response issue was reproduced with the actual Noesis adapter and representative native-shaped responses. AST inspection confirmed the missing async link method, both incorrect hre attribute lookups and absent browser Request errbacks. Other findings are code/configuration review supported by primary documentation linked in GitHub issues. No full live scraping benchmark was run.
