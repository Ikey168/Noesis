# Scraping backend acceptance review — 2026-09-08

The common authored corpus covers static and delayed JavaScript pages, two pagination pages, timed and scroll-triggered lazy loading, missing articles, HTTP errors, and before/after content served at the same URL. Every job has a 45-second deadline and 1 GiB process-tree memory limit. The eleventh probe restarts a worker and fetches the same static URL again. This is a reproducible structural evaluation, not a production anti-bot benchmark.

| Backend | Usable content cases | p50 seconds | p95 seconds | Peak sampled process-tree MiB |
| --- | --- | --- | --- | --- |
| scrapy | 66.7% | 0.53 | 0.58 | 80.6 |
| playwright | 88.9% | 0.72 | 1.31 | 647.5 |
| crawl4ai | 88.9% | 1.59 | 1.85 | 655.9 |
| crawlee | 88.9% | 3.11 | 3.60 | 1003.9 |
| crawlee-adaptive | 66.7% | 2.70 | 2.72 | 1033.0 |

All five adapters reproduced the static text and snapshot after worker restart, and detected the same-URL source edit. Captured HTML, representation, hashes, selected title/locator metadata and native Crawl4AI Markdown are retained. Body locators remain approximate when the existing extractor cannot return exact source spans. Failure cases remain in the denominator; fetch completion is reported separately from usable body recovery. Process-tree sampling can observe a small overshoot before terminating a worker.

Crawlee 1.10.0 also passed the separate three-process named-queue probe: one page, then the remaining page, then zero pages; each source fetched once. The request ceiling is cumulative across restarts. Adaptive static-first selection uses HTTP on static pages, but two dynamic probes exceeded the 1 GiB ceiling. Scroll-triggered lazy loading is not handled by these selectors. Pagination pages are selected explicitly; recursive pagination discovery is not claimed.

Decision: retain existing defaults and defer Crawl4AI/Crawlee adoption. Crawl4AI provides additional Markdown but no measured usable-body improvement over Playwright here and has higher latency. Crawlee adds queue and browser orchestration state, settings migration, handler translation and cumulative-limit management without a measured corpus advantage. Crash-during-handler recovery and production site coverage remain reasons against adoption. These limitations are evaluation findings, not unimplemented production promises.

Evidence: `workflow-review-evidence/scraping-backends-reviewed-2026-09-08.json` and `workflow-review-evidence/crawlee-resume-reviewed-2026-09-08.json`. Native versions are recorded there. Reproduce with `PYTHONPATH=.eval-packages:. /usr/bin/python scripts/benchmark_scraping_backends.py --backends scrapy playwright crawl4ai crawlee crawlee-adaptive --out /tmp/scraping.json` and `scripts/check_crawlee_resume.py --out /tmp/crawlee-resume.json` in the same runtime. Local provider cost is zero; compute cost is not priced and hosted calls are excluded.

#1385, #1386 and #1387 meet their benchmark/evaluation acceptance with a defer decision. No production backend was replaced.


## Firecrawl — #1389

A single public Berlin homepage scrape ran through the real Firecrawl v2 API using an existing account credit. The request fixes `proxy=basic`, disables PDF parsers and additional cache storage, requests rawHtml plus Markdown, and reserves one request and at most $0.01. The account reported 810 credits before the first request and 809 afterward; the response reports `creditsUsed=1`. These are credit measurements, not a dollar invoice. No subscription or credits were purchased.

The provider reported a cache hit. The existing extractor recovered the same body tokens as a sequential direct Playwright capture (token recall 1.0 for this sample). Metadata, provider-observed status, source URL, native response digest, transformed HTML and Markdown are retained. Firecrawl rawHtml is explicitly provider-transformed material, not original HTTP bytes. API success with a missing/non-2xx source status now fails instead of publishing an error page. Reopening the durable database and replaying with network/DNS traps returns identical HTML; the credit balance stays 809. The first evaluation harness failed during replay configuration; recovery replayed its existing ledger and did not issue another scrape.

Decision: defer production adoption. One cached public page establishes the adapter's native response, budget, extraction and restart contracts but does not establish a broad fidelity or cost advantage. Native latency and the direct baseline are retained in `workflow-review-evidence/firecrawl-live-2026-09-08.json`. No private documents were sent. Setup uses the existing `FIRECRAWL_API_KEY`; reproduce with `scripts/evaluate_firecrawl.py --url https://www.berlin.de/ --database /tmp/firecrawl.duckdb --out /tmp/firecrawl.json --network-approved --max-usd-micros 10000`.

Request options and credit accounting were verified against the [official scrape API](https://docs.firecrawl.dev/api-reference/endpoint/scrape) and [credit-usage API](https://docs.firecrawl.dev/api-reference/endpoint/credit-usage). The existing Zyte #1388 defer/unavailable result completes the remaining child scope for tracking issue #1347; no Zyte call was made.
