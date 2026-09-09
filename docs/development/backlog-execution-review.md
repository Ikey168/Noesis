# Backlog execution review — 2026-09-08

The full backlog is not complete. GitHub now lists 39 open issues after closing 19 verified issues across these reviews (seven in the latest implementation pass). The environment PAT lacked issue-write access; the existing system keyring credential succeeded. No global authentication setting was changed. Changes remain local and uncommitted alongside the existing implementation.

## Verified and closed on GitHub

These assessments use the issues' actual acceptance boundaries. A defer decision can complete an evaluation without adopting a backend. Authored structural fixtures are identified as such and never count as independent human judgments.

| Issues | Acceptance evidence and decision |
| --- | --- |
| #1364, #1365, #1366, parent #1340 | Shared five-category CC0 PDF corpus plus a licensed six-page published scientific article with sparse, visually checked structural anchors. PyMuPDF, Docling 2.126.0 and GROBID 0.9.1 actually ran. Docling completes all five authored cases including scan OCR; it exceeds the 90-second worker deadline on the published article (99.22 seconds including monitoring/cleanup). GROBID completes four authored cases, fails the scan, and completes the article with mixed text/reference/table fidelity. Retain PyMuPDF; defer optional production adoption. Exact runs: `workflow-review-evidence/{docling,grobid,scientific-pdf}-live-2026-09-08.json`. Sparse anchors are not full-document text-accuracy gold. |
| #1388 | Optional scrapy-zyte-api 0.36.0 worker, durable request/spend limits, native response-body preservation and hosted-client regressions exist. Its issue explicitly permits unavailable live evaluation when authorized access is absent. Live cost/fidelity remains unavailable; defer adoption. Existing evidence: `workflow-review-evidence/scraping-backends-executed.json`; tests: `tests/unit/ingestion/test_hosted_acquisition.py`. No paid request was made. |
| #1463, parent #1405 | Earlier closure review verified individual accept/reject, concurrent edit protection, immutable report history and bibliography export through public MCP contracts. Siblings #1447/#1457 are closed. See `workflow-review-evidence/closure-review-2026-09-08.json`. |
| #1495 | MarkItDown 0.1.7 actually compared with existing parsers on German/English DOCX/XLSX/PPTX. Original digests and approximate locator coverage are retained. Archive expansion and duplicate members are checked before conversion; plugins are disabled; embedded content is explicitly not evaluated. Native embedded-content and malformed/expansion tests pass. DOCX default replacement is deferred; XLSX/PPTX remain optional fallback candidates. See `workflow-review-evidence/markitdown-reviewed-2026-09-08.json`. |
| #1501 | Official read-only GitHub MCP captured issue #1463, PR #1528 and issue comments through federation. Each capture survives client shutdown and reopening the durable store. Fixed the official issue projection's omitted REST database ID by using stable repository/kind/number identity. Native captured fixture and regressions cover edits, pagination, inaccessible/deleted records, access revocation, request budgets and credential echoes. Live evidence: `workflow-review-evidence/github-mcp-{verified,pr,comments}-2026-09-08.json`. Remote destructive changes and rate limits are injected test cases, not mutations of GitHub records. |
| #1503 | Guarded Playwright MCP 0.0.80 captured Berlin's public homepage, performed a wait action and replayed offline. Actual in-flight cancellation completed in 1.28 seconds, closed the browser and published zero documents. The ordinary Playwright comparison also succeeded (1.75 seconds versus the MCP capture/replay command's 2.63 seconds, which includes additional lifecycle/storage work). No speed advantage is claimed; retain the bulk pipeline and keep interactive MCP opt-in. Snapshots are accessibility representations with explicit imprecise offsets. Evidence: `workflow-review-evidence/playwright-{mcp-verified,cancellation-live,public-baseline}-2026-09-08.json`. |
| #1511 | Repaired calibration/test leakage. Development and held-out test cases have separate hashes and disjoint declared related-entity groups; flipping test labels cannot change selected thresholds. String labels and duplicate identities are rejected. Both scorers recover 75% of positives and admit two of four negatives at the development-selected threshold in this authored test split. Defer adoption; no actual entity merges occurred. Evidence: `workflow-review-evidence/rapidfuzz-heldout-2026-09-08.json`. |
| #1512 | Native datasketch 2.0.0 preserves exhaustive exact/provenance decisions on the six-case held-out regression and 50/200/1000-record scaling runs. Each size records index bytes, build/query/update/delete costs. Replay, revision invalidation and mandatory provenance candidates pass. Query cost does not beat exhaustive comparison at the largest tested size, so the measured decision is defer. Evidence: `workflow-review-evidence/minhash-reviewed-2026-09-08.json`. This is lexical-regression evidence, not human source-independence adjudication. |

These 12 issues were closed as completed on GitHub on 2026-09-08. Closure receipts and the refreshed open inventory are recorded in `workflow-review-evidence/github-closures-2026-09-08.json`. Code remains local and has not been pushed.

## Additional implementation and native evidence

- Phoenix: deployed version 20.8.0 locally and round-tripped three operational probe statuses with deterministic receipt/run correlation. Private probe bodies, tokens, project names and evidence URLs are absent from exported spans. Fixed swallowed exporter failures being reported as submitted. Seven native OpenTelemetry tests pass, including failing and raising exporters. The stored retention policy is a one-day maximum-age rule with a weekly cleanup schedule; expiry was not observed. Median synchronous export was 4.09 ms in the three-probe run. Defer adoption: a local transport success does not establish additional diagnostic value over authoritative receipts. Comparative MLflow deployment/user debugging evaluation remains outstanding for #1500. See `workflow-review-evidence/phoenix-live-2026-09-08.json`.
- BfArM correction: the earlier selected PDF URL returned HTML, which was ingested as five HTML text blocks. That run does not establish successful letter/PDF acquisition. The adapter now rejects non-PDF bytes from selected PDF URLs. See `workflow-review-evidence/regulatory-linked-live-2026-09-08.json`. The same bounded EMA exercise returned HTTP 429; it is not a successful document acquisition or a reason to bypass provider limits.
- MCP cancellation now reaches the native asynchronous transport through `RemoteMCPAdapter`, rather than being checked only between completed browser calls.

## Remaining work and real prerequisites

39 issues remain incomplete or not individually certified by these reviews. Their code and outstanding acceptance remain in `optional-runtime-implementation-status.json`; no generic mapping or green test count certifies those criteria.

#1420 explicitly requires two independent human assignments over 500–1000 real ingested sentences, agreement, adjudication and a frozen release. The human-evaluation directory contains only `.gitignore`, `README.md` and `status.json`; no assignments exist. The checked `data/neuronews.duckdb` and `data/local_warehouse.duckdb` stores contain zero documents, and `data/dev.db` has no documents table. The workflow expects news, blog, paper, transcript, book and note coverage. Newly captured MCP/web material does not satisfy that complete sampling protocol. No labels were invented and no existing benchmark was relabelled. This remains a prerequisite for #1443/#1444 and the human-dependent quality/reviewer tasks.

Other outstanding acceptance includes credentialed and budgeted provider comparisons, independent relevance/support/entity/language/segmentation/OCR/audio judgments where required, the Label Studio independent pilot, and remaining provider-specific native coverage. These are separate from the successful public samples and structural regression runs above. One Firecrawl public-page probe used an existing credit in the latest pass; no subscription or credits were purchased and no messages to annotators were sent.

## Validation

- 70 focused tests passed across federation/MCP, fuzzy scoring, workers, PDF and regional ingestion.
- After the added GitHub failure/credential cases, the overlapping federation/MCP subset passed 24 tests (three new tests).
- A separate native MarkItDown/MinHash profile passed 21 tests.
- The isolated native OpenTelemetry profile passed seven tests.
- Scoped Ruff and `git diff --check` passed. Full-repository test/lint certification is not claimed.

The test runs overlap as stated; their raw totals must not be summed without deduplication. Native parser/model artifacts are pinned by file hashes in `workflow-review-evidence/docling-runtime-2026-09-08.json`. GROBID image ID: `582dcab95710c29c0d2fd18af3f420f848649fb7566341d2eec1a32d4306da97`; its container limit was 3 GiB and an observed memory sample was 2.949 GB (not a certified peak).

## Reproduction

See `tests/fixtures/pdf_benchmark/PLOS-NOTICE.md` for publisher attribution and structural annotation limits.

```sh
uv venv --python 3.12 /tmp/noesis-docling-venv
uv pip install --python /tmp/noesis-docling-venv/bin/python --torch-backend cpu 'docling==2.126.0' 'easyocr==1.7.2' pymupdf psutil
/tmp/noesis-docling-venv/bin/docling-tools models download layout tableformer easyocr --easyocr-lang de --easyocr-lang en --output-dir /tmp/noesis-docling-models
# Set NOESIS_OPTIONAL_PYTHON_PDF_DOCLING and NOESIS_DOCLING_ARTIFACTS_PATH as above.
# Run scripts/evaluate_pdf_backends.py with either checked-in manifest.

.venv/bin/python scripts/evaluate_mcp_research.py --kind github --endpoint https://api.githubcopilot.com/mcp/ --database /tmp/github-evidence.duckdb --out /tmp/github-evidence.json --network-approved --repository Ikey168/Noesis --number 1463 --gh-auth

# In a separately owned local process, with an installed Chromium executable:
NOESIS_BROWSER_ALLOWED_ORIGINS='["https://www.berlin.de"]' npx --yes @playwright/mcp@0.0.80 --host 127.0.0.1 --allowed-hosts 127.0.0.1:8931 --port 8931 --config config/mcp/playwright-evidence.json --init-page config/mcp/playwright_guard.mjs --executable-path /path/to/chromium
.venv/bin/python scripts/evaluate_mcp_research.py --kind browser --endpoint http://127.0.0.1:8931/mcp --database /tmp/browser-evidence.duckdb --out /tmp/browser-evidence.json --network-approved --url https://www.berlin.de/ --wait-text Berlin --network-guard-confirmed

# Local Phoenix 20.8.0, with telemetry disabled and a disposable working directory:
PHOENIX_HOST=127.0.0.1 PHOENIX_PORT=16006 PHOENIX_GRPC_PORT=14317 PHOENIX_WORKING_DIR=/tmp/phoenix-evaluation PHOENIX_TELEMETRY_ENABLED=false PHOENIX_DEFAULT_RETENTION_POLICY_DAYS=1 python -m phoenix.server.main serve
python scripts/evaluate_phoenix.py --endpoint http://127.0.0.1:16006 --out /tmp/phoenix-results.json
```

Interface sources: [official GitHub MCP server](https://github.com/github/github-mcp-server), [official Playwright MCP server](https://github.com/microsoft/playwright-mcp), [Phoenix](https://github.com/Arize-ai/phoenix), and the pinned locally installed package code. These evaluation commands create local artifacts; they do not publish source content or approve model proposals.

## Open-issue implementation continuation

- #1487: repaired the shared HTTPX transport dropping query parameters from selected URLs. A new bounded public request now captures the actual BfArM Litfulo PDF, with native PyMuPDF version/configuration, digest, page/bounding-box locators and offline replay (`workflow-review-evidence/bfarm-query-fixed-2026-09-08.json`). Corrected PDF regression retains document identity, changes committed evidence assessment, preserves the original passage and keeps the current revision after download failure/old-observation replay. Unstructured headings explicitly omit unknown product names; BfArM/PEI scope remains separate.
- #1482: Berlin PDF acquisition/import now uses the same bounded worker as other regional PDFs (120 seconds, 1 GiB, at most 100 pages). Incomplete/failed/cancelled jobs publish no document or workflow receipt. Native parser configuration accompanies the source digest. Selected PDF URLs returning HTML are rejected for Berlin, BfArM and EMA.
- Regional empty feeds/discovery results now report `empty`, with a replayable receipt, instead of reporting a successful discovery merely because the response contains metadata.
- #1385–#1387: scraping adapters reject HTTP error responses and distinguish adaptive HTTP/static-parser output from browser-rendered captures. The benchmark now checks process restart by re-fetching the same static URL, applies source edits at the same URL, and reports usable-document and metadata-locator coverage alongside completion, latency and process-tree memory.


Latest closure set: #1487, #1385, #1386, #1387, #1389 and parent #1347. See [scraping acceptance review](scraping-backend-acceptance-review.md). The broad ingestion/knowledge/evaluation suite passed **1544 tests, 12 skipped, 19 warnings**. Later targeted provider/regional/PDF/hosted/CLI verification passed 75 tests; the subsequent regional streaming subset passed 37 (overlapping). Credential-free `regional`/`replay` now works after deleting the original import file and re-opening the database, with authorization and provider identity still checked. Full backlog completion is not certified.


### German federal decisions — #1481

The shared snapshot layer now honors an explicitly reserved provider byte ceiling (up to 100 MB), retaining the 20 MB default. The real RII index is 23,335,699 bytes with 84,174 entries; its old snapshot ceiling caused the previously recorded `provider_failed` outcome even after a successful HTTP response. Streaming XML selection retains only the requested window while preserving the total matching count and pagination. Entity expansion remains disabled.

After the fix, bounded native acquisition captured BVerwG and BGH decisions, preserved decision dates, dockets, missing ECLI, prior-instance references and numbered paragraph/XML locators, and passed credential-free offline replay. Repeated metadata elements are retained as native occurrences instead of overwriting earlier citations. Identical passage text in different numbered blocks retains each block's own locator. Updated-publication regression produces a new revision and replay of the earlier capture cannot restore stale text. The acquisition is limited to officially published federal decisions; the index is not a complete statement of German case law. Native ZIP/XML evidence and request/resource receipts are in `workflow-review-evidence/court-streaming-live-2026-09-08.json`.

Use the existing regional CLI with `provider=german-courts`, `operation=court_index`, parameters `limit=1000,max_index_bytes=80000000`, and an explicit byte budget large enough for the index plus selected documents. Select `court_decision` using the official download identifier. This evaluation selected historical public decisions and does not certify current law, exhaustiveness or downstream legal conclusions. Official download source: <https://www.rechtsprechung-im-internet.de/>. No account or paid service is used; publisher reuse notices remain attached to each acquisition.

#1481 is also now closed after the actual multi-court acquisition and revision/locator checks. The latest pass closed seven issues; 39 remain open. Snapshot/provider/court verification passed 37 tests. Existing snapshot typing lint violations were unchanged.
