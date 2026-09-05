# Parser and scraper backend evaluation

The optional adapters in `src/ingestion/pdf_evaluation.py` and `src/scraper/backend_evaluation.py` are evaluation-only. Production selection stays unchanged. Run:

```sh
python scripts/make_pdf_benchmark_corpus.py
python scripts/evaluate_pdf_backends.py --grobid-url http://127.0.0.1:8070 --out pdf-backends.json
python scripts/benchmark_scraping_backends.py --out scraping-backends.json
```

The PDF corpus is five original CC0 synthetic fixtures: digital text, columns, a table, bibliography and a raster scan. Expected text, order, page and rectangle annotations are in its hashed manifest. These are authored fixtures, not independent human annotations of published papers. Token recall and expected-line order are explicit proxy metrics. Table-cell text recall does not establish correct table structure; retained boxes/TEI coordinates are not automatically adjudicated.

The measured run used PyMuPDF, Docling 2.126.0 (two CPU threads), and GROBID 0.9.1 CRF in a temporary local rootless Podman service. Its image ID was `582dcab95710c29c0d2fd18af3f420f848649fb7566341d2eec1a32d4306da97`, limited to two CPUs and 4 GiB RAM. The service used approximately 3.69 GB at the final observation and was removed after evaluation. GROBID client RSS is separate from service memory.

| Backend | Result on this corpus | Cost observed | Decision |
|---|---|---|---|
| PyMuPDF | Full expected token recall on digital fixtures; no scan text | 0.12–0.15 s/file, 67–77 MiB process RSS | Retain baseline |
| Docling | Full expected token recall on all five fixtures, including scan | First run 85.50 s including model setup; subsequent 7.91–9.55 s; up to 1,397 MiB RSS | Defer production adoption; promising OCR benefit requires representative documents and deployment budgeting |
| GROBID CRF | Digital/column text retained; table text lost; bibliography token recall 0.364; scan HTTP 500 | 1.16–9.04 s/file plus service memory | Defer; this short synthetic corpus does not establish scientific reference fidelity |

Docling initially failed because the evaluation environment combined CPU Torch with a non-CPU torchvision wheel. Installing matching `torchvision==0.29.0+cpu` resolved the image-processor import. Evaluation dependencies were installed separately from the application's environment: Docling 2.126.0, Crawl4AI 0.9.3, Crawlee 1.10.0 with its Playwright extra. CPU inference/model downloads and service startup are operational dependencies, not free remote inference.

The scraping corpus serves authored static, delayed-JavaScript and absent-article pages from a local HTTP server. Every backend uses the same content extraction cascade after acquiring HTML. Scrapy retrieves static content but cannot see the delayed body; Playwright, Crawl4AI and Crawlee retrieved both. The missing-article browser cases fail explicitly. Measured static/delayed process times were approximately 0.7 s for Scrapy, 1.0 s for Playwright, 1.3–1.5 s for Crawl4AI and 1.5–1.7 s for Crawlee. RSS reports the parent process, excluding browser children. Crawl4AI's absent-article timeout took 10.8 s versus 1.4 s for Playwright and 2.1 s for Crawlee.

Retain the repaired Scrapy/Playwright stack. This small comparison shows no incremental fidelity benefit from the optional crawl backends, and does not establish their restart behavior, anti-bot effectiveness, source-policy compliance or large-corpus capacity. The separate persistent-cache and browser cleanup regressions exercise the production stack's failure paths.

Zyte and Firecrawl evaluation adapters are present but were not called: no credentials or priced public evaluation corpus were configured, and remote services cannot fetch the local corpus. No quality/cost/adoption claim is made for either service. Their dedicated credentialed evaluation criteria remain open. Secrets are supplied at execution, never embedded in manifests or result reports.

Sources: [Docling documentation](https://docling-project.github.io/docling/), [GROBID container requirements](https://grobid.readthedocs.io/en/latest/Grobid-docker/), [Crawl4AI configuration](https://docs.crawl4ai.com/core/quickstart/), [Crawlee quick start](https://crawlee.dev/python/docs/quick-start), [Zyte API](https://docs.zyte.com/zyte-api/get-started.html), [Firecrawl scraping](https://docs.firecrawl.dev/features/scrape).

The separate `check_crawlee_resume.py` probe now runs three independent processes against one named queue: one static page, one delayed page, then no remaining work. Each URL was requested exactly once. Crawlee persists its request counter as well as its queue, so the cumulative request ceiling must increase on resume. This is bounded-stop recovery evidence; adaptive concurrency tuning and termination during an active handler remain unmeasured. See `crawlee-resume.json`.

The Zyte evaluation adapter uses the existing Scrapy crawler boundary with the optional `scrapy-zyte-api` add-on, explicit request metadata, and transparent mode disabled. Credentials are supplied only at runtime. Ordinary robots requests remain direct. This path has not made a credentialed request; neither its service cost nor its quality is established. Configuration follows the [official integration instructions](https://scrapy-zyte-api.readthedocs.io/en/latest/setup.html).

The `crawlee-adaptive` adapter now also exercises `AdaptivePlaywrightCrawler` with a deterministic static-first policy. The recorded probe used HTTP alone for the static article, then HTTP followed by a browser for the delayed article. Both recovered all expected text; the missing article failed explicitly. Observed times were 1.84 s, 2.28 s and 2.68 s. These measurements exercise backend selection and fallback, not the learned rendering predictor. See `crawlee-adaptive.json`; reproduce with `--backends crawlee-adaptive` in an environment containing `crawlee[adaptive-crawler]==1.10.0`.

Crawlee remains deferred for production: migration would replace the current queue/checkpoint boundary, adapt source budgets and receipts to Crawlee's cumulative limits, port existing extraction/provenance handling, and rerun lifecycle and failure-recovery acceptance. The small corpus shows working orchestration but no incremental extraction quality over repaired Playwright. Active-handler crash recovery and learned predictor quality need a larger evaluation before adoption.
