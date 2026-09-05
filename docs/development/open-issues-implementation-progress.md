# Open GitHub issue implementation

Snapshot: 129 open issues on 2026-09-05; 28 tracking parents and 101 concrete tasks. The earlier 57-task workflow implementation and remaining human-dependent acceptance are documented in [the workflow ledger](workflow-implementation-progress.md). This ledger records the additional 44 source-collection tasks. No issues are closed automatically.

| Issue | Task | Current evidence / remaining work |
|---|---|---|
| [#1349](https://github.com/Ikey168/Noesis/issues/1349) | Implement native Crossref works requests and response mapping | Native Crossref requests/normalization implemented; native fixture pages and bounded live page pass. Runtime restart/dedup acceptance still needs an integration check. |
| [#1350](https://github.com/Ikey168/Noesis/issues/1350) | Implement native OpenAlex works pagination and field mapping | Native OpenAlex cursor/metadata implemented; native fixtures and bounded live page pass. Runtime restart integration pending. |
| [#1351](https://github.com/Ikey168/Noesis/issues/1351) | Implement native Europe PMC search requests and result mapping | Previously implemented Europe PMC; live native page reconfirmed. |
| [#1352](https://github.com/Ikey168/Noesis/issues/1352) | Normalize real HTTP failures in the source-pack transport | Previously implemented typed urllib failures and Retry-After handling. |
| [#1353](https://github.com/Ikey168/Noesis/issues/1353) | Distinguish fixture conformance from live provider readiness | Opt-in timestamped three-provider live script implemented; all three passed. Capability readiness integration pending. |
| [#1354](https://github.com/Ikey168/Noesis/issues/1354) | Audit remaining source packs for native protocol compatibility | Pending implementation and validation. |
| [#1355](https://github.com/Ikey168/Noesis/issues/1355) | Add topic, author and date search to PaperConnector discovery | Pending implementation and validation. |
| [#1356](https://github.com/Ikey168/Noesis/issues/1356) | Route domain research objectives to scholarly discovery operations | Pending implementation and validation. |
| [#1357](https://github.com/Ikey168/Noesis/issues/1357) | Discover feed and sitemap URLs from configured domain websites | Pending implementation and validation. |
| [#1358](https://github.com/Ikey168/Noesis/issues/1358) | Persist a bounded website crawl frontier for domain discovery | Pending implementation and validation. |
| [#1359](https://github.com/Ikey168/Noesis/issues/1359) | Add an optional Brave Search source-discovery adapter | Pending implementation and validation. |
| [#1360](https://github.com/Ikey168/Noesis/issues/1360) | Resolve DOI open-access locations through Unpaywall | Pending implementation and validation. |
| [#1361](https://github.com/Ikey168/Noesis/issues/1361) | Download scholarly full text into content-addressed snapshots | Pending implementation and validation. |
| [#1362](https://github.com/Ikey168/Noesis/issues/1362) | Parse Europe PMC full-text XML into cited document sections | Pending implementation and validation. |
| [#1363](https://github.com/Ikey168/Noesis/issues/1363) | Expose scholarly content coverage and manuscript version relationships | Pending implementation and validation. |
| [#1364](https://github.com/Ikey168/Noesis/issues/1364) | Build a structured-PDF evaluation corpus and PyMuPDF baseline | Pending implementation and validation. |
| [#1365](https://github.com/Ikey168/Noesis/issues/1365) | Evaluate Docling against the structured-PDF baseline | Pending implementation and validation. |
| [#1366](https://github.com/Ikey168/Noesis/issues/1366) | Evaluate GROBID for scientific sections and citation references | Pending implementation and validation. |
| [#1367](https://github.com/Ikey168/Noesis/issues/1367) | Persist feed HTTP validators and send conditional requests | Persistent SQLite feed validators implemented; restart/304 regression passed. |
| [#1368](https://github.com/Ikey168/Noesis/issues/1368) | Record unchanged feed checks without degrading source health | Explicit unchanged harvest outcome and non-degrading health implemented; 38 feed/health checks passed. |
| [#1369](https://github.com/Ikey168/Noesis/issues/1369) | Honor Retry-After in feed and article fetch retries | Feed Retry-After respected or deferred; article-fetch integration pending. |
| [#1370](https://github.com/Ikey168/Noesis/issues/1370) | Declare an ingestion extra for RSS and HTML extraction | Ingestion extra declared and installed; outside-checkout wheel/CI smoke pending. |
| [#1371](https://github.com/Ikey168/Noesis/issues/1371) | Declare a browser-scraping extra and browser installation path | Browser extra declared, installed and real Chromium lifecycle passed; wheel/CI smoke pending. |
| [#1372](https://github.com/Ikey168/Noesis/issues/1372) | Report connector dependency readiness without breaking unrelated imports | Doctor dependency groups added; executable diagnosis and minimal import test pending. |
| [#1373](https://github.com/Ikey168/Noesis/issues/1373) | Implement the missing async HTTP article-link extractor | HTTP link extractor implemented with scoped stable URLs; invalid-page diagnostics; actual HTTP local-server check pending. |
| [#1374](https://github.com/Ikey168/Noesis/issues/1374) | Fix Playwright article-link extraction to read href | Both href loops repaired; relative URLs/scope tested. |
| [#1375](https://github.com/Ikey168/Noesis/issues/1375) | Remove hard-coded 2024/2025 URL filtering from Guardian discovery | Guardian year-independent path rule implemented; historical fixtures passed, current/future explicit coverage pending. |
| [#1376](https://github.com/Ikey168/Noesis/issues/1376) | Allow browser retries for URLs whose HTTP extraction failed | Per-backend bounded attempts and success-only dedup implemented; failed HTTP then browser success regression passed. |
| [#1377](https://github.com/Ikey168/Noesis/issues/1377) | Set an explicit live Scrapy cache revalidation policy | Bounded RFC2616 cache policy configured; persistent local-server acceptance pending. |
| [#1378](https://github.com/Ikey168/Noesis/issues/1378) | Separate offline cache replay from live fetch provenance | Pending implementation and validation. |
| [#1379](https://github.com/Ikey168/Noesis/issues/1379) | Close included Playwright pages from request errbacks | Page errback cleanup implemented; six real readiness failures then successful static/delayed pages passed. |
| [#1380](https://github.com/Ikey168/Noesis/issues/1380) | Configure browser page/context concurrency and shutdown ownership | Finite Scrapy context/page limits configured; multi-source shutdown health acceptance pending. |
| [#1381](https://github.com/Ikey168/Noesis/issues/1381) | Replace fixed browser sleeps with bounded readiness rules | Fixed sleeps removed; configurable bounded selector readiness and real browser regression passed. |
| [#1382](https://github.com/Ikey168/Noesis/issues/1382) | Keep unknown publication dates distinct from scrape time | Unknown dates retained in both spiders and async engine; spider date tests passed. |
| [#1383](https://github.com/Ikey168/Noesis/issues/1383) | Add a shared structured HTML metadata extractor using extruct | Optional extruct candidate selection/conflict provenance implemented; fixtures passed. |
| [#1384](https://github.com/Ikey168/Noesis/issues/1384) | Retain extraction versions, field locators and heuristic score semantics | Extractor versions/snapshot/field locators and heuristic score semantics added; downstream document preservation pending. |
| [#1385](https://github.com/Ikey168/Noesis/issues/1385) | Build a reproducible scraping backend benchmark | Pending implementation and validation. |
| [#1386](https://github.com/Ikey168/Noesis/issues/1386) | Evaluate Crawl4AI on the scraping benchmark | Pending implementation and validation. |
| [#1387](https://github.com/Ikey168/Noesis/issues/1387) | Evaluate Crawlee Python for resumable adaptive crawling | Pending implementation and validation. |
| [#1388](https://github.com/Ikey168/Noesis/issues/1388) | Evaluate Zyte API through the existing Scrapy integration boundary | Pending implementation and validation. |
| [#1389](https://github.com/Ikey168/Noesis/issues/1389) | Evaluate Firecrawl as an optional scrape API backend | Pending implementation and validation. |
| [#1390](https://github.com/Ikey168/Noesis/issues/1390) | Add pagination and date windows to the existing Guardian API client | Pending implementation and validation. |
| [#1391](https://github.com/Ikey168/Noesis/issues/1391) | Expose Guardian API collection through domain source packs | Pending implementation and validation. |
| [#1392](https://github.com/Ikey168/Noesis/issues/1392) | Deduplicate Guardian API, RSS and HTML representations by article identity | Pending implementation and validation. |

Validation so far: 32 native source-pack tests; 92 scraper/extraction tests; 38 feed/health tests; two discovery/fallback regressions; one real Chromium lifecycle test. All three native scholarly providers returned records in the live check. These checks establish the stated behavior, not complete acceptance for every issue.
