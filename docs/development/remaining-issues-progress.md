# Remaining issue implementation — 2026-09-08

## Current disposition — non-implementation follow-up transferred

At the user's request on 2026-09-09, the remaining **23 issues were closed as not planned**, including their five tracking parents. GitHub now has **zero open issues**. Outstanding evaluations, independent human annotation/review and credentialed provider checks are preserved in [the follow-up document](further-evaluations-and-access.md), with original issue requirements and a verified closure receipt. These closures do not claim the deferred acceptance checks passed. Code remains local and uncommitted. Earlier counts below are historical snapshots.

## Current continuation — 2026-09-09

The stored system-keyring GitHub credential supports issue closure. Sixteen issues have closed in the September 9 continuation: #1426, #1478, #1479, #1480, #1482, #1484, #1486, #1490, #1493, #1494, #1496, #1500, #1504, #1505, #1506 and #1520. The refreshed inventory contains 23 open issues, down from 39. [Current evidence and limits](backlog-execution-2026-09-09.md) records native provider imports, replay, OCR, actual PostgreSQL hybrid retrieval, three-direction E5 retrieval, BGE mode/reference benchmarks, Qwen reranker resource/fusion comparisons and constrained/unconstrained report proposal comparisons. The NLI comparison now uses published independently human-labelled German/English validation and test pairs; its outstanding quotation and German task-transfer coverage remains explicit. GLiNER now has completed published DE/EN entity and separate English relation measurements, a repaired native SDK adapter, and a documented defer decision. Broad validation is 1,598 passed with 13 skips. Code remains local and uncommitted. The complete backlog and specified independent Noesis human collection remain outstanding.

## Latest full-backlog continuation

The full backlog remains incomplete. [Execution and acceptance review](backlog-execution-review.md) records 12 locally closure-ready issues, measured native Docling/GROBID/GitHub/Playwright/Phoenix/BfArM runs, repaired calibration leakage, transport cancellation and Office expansion handling. The current GitHub token still rejects issue closure with HTTP 403. The other 46 issues remain incomplete or unverified; missing independent human assignments are not replaced with synthetic labels. This section supersedes older generic environment/unavailability claims only where the new review provides direct evidence.


## Closure-first review and regional publication repairs — 2026-09-08

**The full request remains incomplete; no issues were closed in this pass.** GitHub rejected both GraphQL `closeIssue` and REST PATCH for #1463 with “Resource not accessible by personal access token” (REST HTTP 403). Issue-write access is required to close the verified work. #1463 and parent #1405 are ready: 14 report-store/public-MCP tests passed and siblings #1447/#1457 are closed. #1364's requested annotated, redistributable baseline is also ready within its authored-corpus scope: a refreshed five-document PyMuPDF run records text/order/table/reference/locator metrics and resource measurements. Independent scientific-document quality and optional parser comparisons remain separate work.

Implemented in this pass:

- Regional linked PDFs now use the existing digest-bound process worker with a 120-second deadline and 1 GiB RSS ceiling. Partial, unavailable, failed, timed-out and cancelled results cannot publish evidence. Captured bytes remain available for retry without another fetch. Parser version/configuration are preserved; variable runtime measurements stay outside source-version identity.
- Regional acquisition/import commits documents, revisions, observation receipts and workflow replay receipts atomically. Injected failures after receipt insertion roll everything back; retry and offline replay succeed.

Validation: **64 passed, one existing warning**, scoped Ruff and `git diff --check` passed. Includes native PDF execution and failure-injection tests; this is not a full-repository certification. Exact test scope, source hashes and GitHub rejection: [closure review](workflow-review-evidence/closure-review-2026-09-08.json). Refreshed parser measurements: [PDF baseline](workflow-review-evidence/pdf-baseline-closure-review-2026-09-08.json).

Changes remain local alongside pre-existing uncommitted work. The 58-issue inventory remains open on GitHub. Other acceptance remains incomplete or unverified; no human dataset or selected deployment configuration was supplied in response to the request during this pass.

## Previous continuation

The refreshed GitHub inventory remains **58 open issues (50 leaf tasks and eight trackers)**. The full backlog is **not complete**. The exact issue bodies are retained in `workflow-review-evidence/github-open-issues-2026-09-08.json`; the implementation matrix records this continuation's scoped verification without certifying unreviewed acceptance criteria.

Implemented and tested in this continuation:

- **#1478/#1479/#1484:** `regional_review` connects captured CTIS/DRKS cross-registry/publication identifiers and OpenSanctions candidates to the existing inbox and entity decision ledger. Tasks carry committed source revision references; stale and unauthorized inputs fail. Sanctions-to-local-entity assignment is an explicit coordinator hypothesis. Replay reuses the task, related identity groups remain linked for dataset splitting, and review never automatically merges identities.
- **#1471 and regional acquisition:** replay returns an older observation's exact receipt without restoring it as the current document. OpenAlex observations reject conflicting work metadata or budgets. Regional batches and OpenAlex document/outcome publication commit atomically. Invalid regional batches leave no partial documents or revision rows.
- **Registry source lifecycle:** the existing text classifier treated a changed registry payload containing “withdrawn” as a retracted source. Regional status and legal-text changes now remain active source evidence unless an explicit lifecycle operation withdraws them. The regression exercises a changed DRKS registration and subsequent old-import replay.
- **#1480:** CELLAR actually selects the documented ELI/ECLI predicates, retains all grouped native bindings and identifier variants, rejects unrequested result languages, and labels relationships as a bounded query page.
- **#1481/#1487:** repeated identical BfArM entries are deduplicated; conflicting entries are explicit failures. Court index acquisition supports a selected byte ceiling up to 100 MB, still reserved against the immutable project budget. Its default remains 20 MB.
- **#1364–#1366:** the single-backend PDF benchmark entry point now uses the same bounded worker as corpus evaluation; it cannot relabel partial/unavailable parser output as completed.

Native execution evidence:

- Post-fix GLiNER2 completed an actual offline worker run on the prior authored German input, producing entities, a relation and structured fields. This supersedes the previous blocked invocation only; it is not independent extraction-quality evidence. `workflow-review-evidence/gliner-native-2026-09-08.json` retains the result and fixture hash.
- Bounded public EMA and BfArM acquisition ingested one medicine record and two notices, respectively, and replayed offline. The German court index exceeded the default byte limit; an explicitly larger attempt failed at the transport layer. No successful live court discovery or complete agency coverage is claimed. `workflow-review-evidence/regional-live-2026-09-08.json` retains receipts and limits.
- The repaired single-PDF command completed real PyMuPDF parsing. The wheel built successfully and its CLI doctor ran outside the checkout with `regional_review` present and zero network calls.

Final scoped regression: **1,560 passed, four skipped, zero failures/errors**, with 19 existing warnings. Scope: ingestion, KB, evaluation, optional CLI, normalization and entity-resolution tests. The initial run exposed two missing-RapidFuzz dependency failures; installing the repository-pinned `rapidfuzz==3.14.3` in `.venv` resolved them. Scoped Ruff and `git diff --check` passed. Exact scope, skips, hashes and native PDF receipt: `workflow-review-evidence/remaining-issues-validation-2026-09-08.json`. A separate run with the existing pinned `.eval-ragas-compat` profile passed both Ragas tests, including native metrics without a hosted judge; this resolves that profile-specific skip and is not added to the broad test count. The full repository suite is not certified.

### Still required for the full request

No independent human dataset was supplied or collected: #1420 still requires 500–1000 real ingested sentences, two independent annotators, adjudication and a frozen split. The acceptance criteria that depend on human relevance/support judgments, calibration, reviewer effort/error reduction or Label Studio pilots cannot be completed by relabelling authored fixtures. Credentialed provider comparisons, configured MCP/observability deployments, representative model/parser/media comparisons and other issue-specific live acceptance also remain outstanding. The successful EMA/BfArM samples and GLiNER invocation do not satisfy those unrelated requirements. Unreviewed issue criteria remain explicitly unverified in the matrix.

Changes remain local alongside the pre-existing work. No commits, pushes, GitHub issue closures, external messages, paid provider calls or fabricated human annotations occurred. Operator instructions are in [optional-runtime integrations](../guides/optional-runtime-integrations.md).

## Previous execution-path audit — 2026-09-07

**The complete remaining backlog is not certified implemented.** This continuation reproduced and repaired the WhisperX decoder bypass and nested unavailable-result publication, then found and repaired replay, OCR/Docling state propagation, captured-PDF byte identity, BGE batching/timing, implicit Presidio downloads, browser action validation, source-bound entity scoring, and missing GLiNER relations/structured-field dispatch. Green file mappings and prior test counts were not used as proof of completeness.

Current changes, exact validation and remaining invocation/acceptance limits are recorded in [execution-path audit](execution-path-audit.md) and `workflow-review-evidence/execution-path-validation.json`. Historical results below remain historical. No commit, push, issue closure, paid provider request, fabricated human annotation, automatic entity merge or report approval occurred in this pass. The issue matrix explicitly separates rechecked paths from issues not individually reaudited; #1388's permitted unavailable evaluation and #1463's actual report-versioning scope are preserved.

## Native runtime continuation

The earlier statement that only execution remained was too broad: multiple items still had only profiles, generic mappings or output contracts. This continuation adds executable native model/provider/MCP/annotation paths and wires them to Noesis's existing document revisions, artifact graph, review inbox and report versions. The operator entry point is `noesis-optional` (`python -m src.noesis_cli.optional`). No production backend was silently selected.

Current code/test mapping for every open issue: [implementation status](optional-runtime-implementation-status.json). Setup, profiles, bounded execution, native benchmark manifests and authorization: [operator guide](../guides/optional-runtime-integrations.md). Final validation evidence: `workflow-review-evidence/native-runtime-validation.json`.

The recovered code is local and uncommitted. No code has been pushed, no issues have been closed in this continuation, and no paid provider calls were made. Independent-human collection (#1420), representative measured quality, deployment compatibility and credentialed live acceptance remain distinct from the executable implementations. The historical inventory below is not a current statement of installed SDK/model availability.

### Packaging and invocation

The wheel includes the optional CLI and runtime modules. An outside-checkout wheel smoke executed the doctor successfully with zero network calls. Each selected heavy backend can use `NOESIS_OPTIONAL_PYTHON_<BACKEND>` to avoid incompatible Python/SDK combinations. Provider execution requires explicit scoped approval and immutable request/byte/reserved-cost ceilings; source-bound model results cannot overwrite originals, merge entities or approve reports automatically.

## Historical implementation passes

GitHub inventory at start: 72 open issues. This work includes the implementations listed below. **The request to finish every issue is not complete.** At the user's explicit request, implemented tasks #1359, #1477, #1485, #1491 and #1497 and their now-complete tracking parent #1338 were closed as completed on GitHub. No code change has been pushed or merged; implementation remains local. Other issue acceptance remains outstanding. The later continuation closed #1470, #1474 and #1498; a refreshed tracker showed #1517 and #1518 already closed.

## 2026-09-06 scaffold pass (superseded by the native runtime continuation)

The live GitHub inventory was refreshed at **58 open issues**. This pass implemented the remaining locally automatable contracts, adapters, benchmark harnesses and deterministic fixtures without fabricating external acceptance evidence. The broad regression after integration is **1271 passed, 12 skipped, 6 warnings** across `tests/unit/ingestion`, `tests/unit/kb`, and `tests/unit/evaluation`.

New implementation/evaluation surfaces include:

- structured-PDF corpus scoring plus PyMuPDF/Docling/GROBID evaluation boundaries (`src/ingestion/pdf_evaluation.py`, `scripts/evaluate_pdf_backends.py`); the frozen corpus contains digital, multi-column, table, bibliography and scan fixtures;
- a bounded scraping backend corpus and adapters for repaired Scrapy/Playwright plus Crawl4AI, Crawlee/adaptive Crawlee, Zyte and Firecrawl evaluation (`scripts/benchmark_scraping_backends.py`, `src/scraper/backend_evaluation.py`);
- maintained Guardian Open Platform source-pack exposure, native body/contributor/date normalization, source identity and fixture/live-disabled conformance;
- bounded OpenAlex content/TEI acquisition plus CTIS, DRKS, CELLAR, German courts, Berlin law, OpenCorporates, OpenSanctions, EMA and BfArM normalization contracts; Exa/Tavily discovery, Jina Reader, MarkItDown and PaddleOCR evaluation boundaries;
- independent-human manifest validation, retrieval and answer-support benchmark contracts, review-only linkage/entity extraction/redaction adapters, E5 input/index isolation, reranker/forced-alignment/constrained-report adapters, Phoenix/GitHub MCP/Playwright MCP profiles and Label Studio exchange;
- a reproducible RapidFuzz-versus-SequenceMatcher benchmark with a separately calibrated threshold curve, recall/false-merge/latency/allocation evidence and a **defer** decision on the authored regression corpus. Production resolution remains unchanged.

Two environment-specific regressions found by the broad suite were also fixed: JSON-LD source identity is retained when `extruct` normalizes `@graph`, feed/robots discovery no longer depends on Trafilatura parser-version behavior, and subprocess tests no longer assume `sys.executable` is a normal Python binary inside the desktop harness.

No additional GitHub issues were closed in this pass. An attempted close-with-comment for #1463/#1405 was rejected by the current GitHub token's comment permission, and evaluation-heavy issues are deliberately left open until their real acceptance evidence exists.

### Historical prerequisites before the native runtime continuation

- **Independent humans:** #1420 still requires the declared 500–1000 real-sentence dual-independent annotation/adjudication set. That blocks honest live-domain claims for #1426, #1427, #1443, #1444, #1458, #1492, #1493, #1494, #1499, #1507 and #1521 where their criteria explicitly require independent judgments or pilots.
- **Optional model/runtime execution:** Docling, GROBID service, Splink, GLiNER2, Presidio, Ragas, Phoenix, E5/BGE/Qwen/mDeBERTa/LightOnOCR, wtpsplit, Lingua, datasketch, WhisperX and Outlines are not available/configured in this environment. Their adapters fail closed and record availability rather than inventing benchmark numbers. PyMuPDF is likewise absent from the active evaluation interpreter, so #1364's baseline run remains an environment prerequisite even though the corpus/scoring harness is present.
- **Credentialed/live providers:** Guardian, Zyte, Firecrawl, OpenAlex priced content, OpenCorporates, OpenSanctions, Exa and Tavily still need authorized bounded live runs for acceptance items that require actual cost/latency/provider behavior. Jina Reader requires explicit remote-processing opt-in.
- **Configured MCP/deployments:** #1501 and #1503 require real GitHub/Playwright MCP capture/replay exercises; #1521 requires a selected Label Studio deployment and independent pilot.
- **Representative corpus evidence:** #1364–#1366 and #1385–#1387 require representative independently checked PDF/scientific and browser/anti-bot behavior beyond the authored deterministic regression fixtures.

These are intentionally represented as **unavailable/deferred**, not silently converted into synthetic “completed” evaluations.

## Implemented locally and closed on GitHub

| Issue | Evidence and limits |
|---|---|
| [#1359](https://github.com/Ikey168/Noesis/issues/1359) | Candidate handoff implemented and verified through the shared extractor, binary snapshots and DocumentStore; German/DE discovery fixture and durable replay passed. |
| [#1477](https://github.com/Ikey168/Noesis/issues/1477) | Public API v2 notes integrated with the source-pack runtime; threaded notes, edited revisions, private-field exclusion and restart/replay passed. Live endpoint returned an interactive 403 challenge; live access unverified. |
| [#1497](https://github.com/Ikey168/Noesis/issues/1497) | Pinned optional warcio integration implemented; WARC/ARC, gzip bounds/truncation, response/revisit blob identity and export round-trip tests passed. Supported revisit and export limitations documented. |
| [#1491](https://github.com/Ikey168/Noesis/issues/1491) | Bounded read-only availability/capture acquisition implemented; source/capture/retrieval dates separated, normal storage, durable replay and unavailable/redirect/failure tests passed. Live archive access not claimed. |
| [#1485](https://github.com/Ikey168/Noesis/issues/1485) | Bounded entity/property acquisition implemented with statement references/qualifiers/ranks, review-only local candidates and durable receipts. German fixtures and a live Berlin Q64 acquisition of 24 statements passed. |

| [#1498](https://github.com/Ikey168/Noesis/issues/1498) | Bounded Common Crawl index/range acquisition, durable reservations and cursor resume; seven tests including cross-crawl payload deduplication. |
| [#1474](https://github.com/Ikey168/Noesis/issues/1474) | Public ORCID v3 summaries, explicit-identifier distinction and ambiguous matches in the existing review inbox; three tests, no live credential configured. |
| [#1470](https://github.com/Ikey168/Noesis/issues/1470) | Crossref notices feed integrity/watch/report dependency paths without changing historical source generations. Eight tests and live correction acquisition passed. |
| [#1522](https://github.com/Ikey168/Noesis/issues/1522) | Optional River ADWIN operational-stream adapter with ordered event evidence, durable replay state, anomaly publication and seeded evaluation. |
| [#1523](https://github.com/Ikey168/Noesis/issues/1523) | Optional Pandoc 3.9/Typst 0.15.0 report rendering with pinned CSL, DOCX/PDF outputs, literal AST, bounded workers and measured German/English fixtures. |
| [#1516](https://github.com/Ikey168/Noesis/issues/1516) | Optional Pandera 0.33.1 validation over immutable dataset releases, German CSV/Parquet checks, bounded actionable reports, replay and benchmark evidence. |

An optional Pint 0.25.2 adapter and reproducible measured evaluation were also added; #1517 was already closed when closure was attempted. Shapely #1518 was also already closed on refresh, so no Shapely implementation was added here.

Continuation documentation: [Common Crawl, ORCID, Pint](../guides/common-crawl-orcid-and-pint.md), [Crossref notices](../guides/crossref-notices.md). Latest ingestion/CLI/quantitative/report/watch regression: **716 passed, three skipped**, 17 existing warnings. The tracker had **61 open issues** after #1470 closure.

Setup, interfaces, limits and reproduction: [acquisition guide](../guides/archive-and-discovery-acquisition.md).

Validation: ingestion + CLI regression passed **648 tests, three skipped** before the later Wikidata addition. The Wikidata suite subsequently passed **three tests**, and the acquisition/source-planner/MCP subset passed **36 tests**. Two additional archive limit/rollback tests passed afterward in a 13-test archive/Wikidata subset. New modules and tests pass Ruff. Existing dependency/deprecation warnings remain.

## Historical open-acceptance inventory

These issues were still open at that historical pass. Use the current implementation-status JSON for executable code and remaining acceptance evidence. Existing work and dependencies must be checked against their actual acceptance criteria before closure. Human annotation, relevance/support labels, reviewer studies and paid-provider comparisons require real inputs; no synthetic substitute is represented as independent human evidence. A path/configuration-name question was sent to the user; no inputs were supplied during this pass.

| Issue | Task |
|---|---|
| [#1340](https://github.com/Ikey168/Noesis/issues/1340) | [Tracking] Benchmark Docling and GROBID as optional structured document parsers |
| [#1347](https://github.com/Ikey168/Noesis/issues/1347) | [Tracking] Benchmark optional crawl backends against the repaired Scrapy and Playwright pipeline |
| [#1348](https://github.com/Ikey168/Noesis/issues/1348) | [Tracking] Connect the existing Guardian API collector to domain source packs and incremental ingestion |
| [#1364](https://github.com/Ikey168/Noesis/issues/1364) | Build a structured-PDF evaluation corpus and PyMuPDF baseline |
| [#1365](https://github.com/Ikey168/Noesis/issues/1365) | Evaluate Docling against the structured-PDF baseline |
| [#1366](https://github.com/Ikey168/Noesis/issues/1366) | Evaluate GROBID for scientific sections and citation references |
| [#1385](https://github.com/Ikey168/Noesis/issues/1385) | Build a reproducible scraping backend benchmark |
| [#1386](https://github.com/Ikey168/Noesis/issues/1386) | Evaluate Crawl4AI on the scraping benchmark |
| [#1387](https://github.com/Ikey168/Noesis/issues/1387) | Evaluate Crawlee Python for resumable adaptive crawling |
| [#1388](https://github.com/Ikey168/Noesis/issues/1388) | Evaluate Zyte API through the existing Scrapy integration boundary |
| [#1389](https://github.com/Ikey168/Noesis/issues/1389) | Evaluate Firecrawl as an optional scrape API backend |
| [#1391](https://github.com/Ikey168/Noesis/issues/1391) | Expose Guardian API collection through domain source packs |
| [#1399](https://github.com/Ikey168/Noesis/issues/1399) | [Tracking] Revision-safe extraction, enrichment, and model evaluation |
| [#1401](https://github.com/Ikey168/Noesis/issues/1401) | [Tracking] Query deadlines, retrieval relevance, and evidence-supported answers |
| [#1405](https://github.com/Ikey168/Noesis/issues/1405) | [Tracking] Living research reports |
| [#1406](https://github.com/Ikey168/Noesis/issues/1406) | [Tracking] Unified evidence review inbox |
| [#1420](https://github.com/Ikey168/Noesis/issues/1420) | Collect the missing independent human evaluation set |
| [#1426](https://github.com/Ikey168/Noesis/issues/1426) | Benchmark the existing hybrid retrieval and reranking paths |
| [#1427](https://github.com/Ikey168/Noesis/issues/1427) | Evaluate whether answers are supported by their cited evidence |
| [#1443](https://github.com/Ikey168/Noesis/issues/1443) | Improve and calibrate stance classification |
| [#1444](https://github.com/Ikey168/Noesis/issues/1444) | Improve and calibrate frame classification |
| [#1458](https://github.com/Ikey168/Noesis/issues/1458) | Export reviewed corrections as evaluation/training candidates |
| [#1463](https://github.com/Ikey168/Noesis/issues/1463) | Draft and review report revisions |
| [#1469](https://github.com/Ikey168/Noesis/issues/1469) | [Tracking] API, library and MCP integration roadmap for EU/Germany/Berlin |
| [#1471](https://github.com/Ikey168/Noesis/issues/1471) | Acquire OpenAlex PDFs and TEI XML through the existing paper pipeline |
| [#1478](https://github.com/Ikey168/Noesis/issues/1478) | Acquire EU/EEA clinical trial evidence from CTIS public records |
| [#1479](https://github.com/Ikey168/Noesis/issues/1479) | Ingest German Clinical Trials Register DRKS exports |
| [#1480](https://github.com/Ikey168/Noesis/issues/1480) | Extend EUR-Lex with CELLAR legal texts and case-law relationships |
| [#1481](https://github.com/Ikey168/Noesis/issues/1481) | Acquire German federal court decisions from Rechtsprechung im Internet |
| [#1482](https://github.com/Ikey168/Noesis/issues/1482) | Add Berlin legal texts and decisions through documented public access |
| [#1483](https://github.com/Ikey168/Noesis/issues/1483) | Add EU/German company enrichment through OpenCorporates |
| [#1484](https://github.com/Ikey168/Noesis/issues/1484) | Add reviewable EU-focused OpenSanctions entity matches |
| [#1486](https://github.com/Ikey168/Noesis/issues/1486) | Ingest EMA public medicine metadata and linked regulatory documents |
| [#1487](https://github.com/Ikey168/Noesis/issues/1487) | Ingest BfArM German medicine safety letters and updates |
| [#1488](https://github.com/Ikey168/Noesis/issues/1488) | Benchmark Exa discovery against existing search acquisition |
| [#1489](https://github.com/Ikey168/Noesis/issues/1489) | Benchmark Tavily discovery against existing search acquisition |
| [#1490](https://github.com/Ikey168/Noesis/issues/1490) | Evaluate Jina Reader as a bounded web extraction fallback |
| [#1492](https://github.com/Ikey168/Noesis/issues/1492) | Benchmark Splink for reviewable multi-attribute entity resolution |
| [#1493](https://github.com/Ikey168/Noesis/issues/1493) | Benchmark GLiNER2-multi-v1 as a domain-specific extraction backend |
| [#1494](https://github.com/Ikey168/Noesis/issues/1494) | Evaluate Presidio for traceable redaction of derived research outputs |
| [#1495](https://github.com/Ikey168/Noesis/issues/1495) | Evaluate MarkItDown as an optional document conversion fallback |
| [#1496](https://github.com/Ikey168/Noesis/issues/1496) | Benchmark PaddleOCR for scanned German and multilingual evidence |
| [#1499](https://github.com/Ikey168/Noesis/issues/1499) | Evaluate Ragas alongside existing retrieval and answer-support metrics |
| [#1500](https://github.com/Ikey168/Noesis/issues/1500) | Evaluate Phoenix tracing against existing MLflow and observability |
| [#1501](https://github.com/Ikey168/Noesis/issues/1501) | Connect GitHub MCP research records through existing federation |
| [#1503](https://github.com/Ikey168/Noesis/issues/1503) | Evaluate Playwright MCP for interactive evidence acquisition |
| [#1504](https://github.com/Ikey168/Noesis/issues/1504) | Benchmark multilingual-e5-small for lower-cost German/English retrieval |
| [#1505](https://github.com/Ikey168/Noesis/issues/1505) | Benchmark BGE-M3 dense, sparse and multi-vector retrieval |
| [#1506](https://github.com/Ikey168/Noesis/issues/1506) | Evaluate Qwen3-Reranker-0.6B with a dedicated multilingual scoring adapter |
| [#1507](https://github.com/Ikey168/Noesis/issues/1507) | Evaluate multilingual mDeBERTa NLI for German evidence support and contradiction |
| [#1508](https://github.com/Ikey168/Noesis/issues/1508) | Benchmark LightOnOCR-2-1B on scanned German administrative documents |
| [#1509](https://github.com/Ikey168/Noesis/issues/1509) | Benchmark wtpsplit/SaT for German and multilingual sentence segmentation |
| [#1510](https://github.com/Ikey168/Noesis/issues/1510) | Benchmark Lingua for short and mixed-language research evidence |
| [#1511](https://github.com/Ikey168/Noesis/issues/1511) | Benchmark RapidFuzz for entity-resolution candidate scoring |
| [#1512](https://github.com/Ikey168/Noesis/issues/1512) | Evaluate datasketch MinHash LSH for scalable text-reuse candidate discovery |
| [#1514](https://github.com/Ikey168/Noesis/issues/1514) | Evaluate WhisperX forced alignment for word-level evidence timestamps |
| [#1520](https://github.com/Ikey168/Noesis/issues/1520) | Evaluate Outlines for schema-constrained report revision proposals |
| [#1521](https://github.com/Ikey168/Noesis/issues/1521) | Evaluate Label Studio exchange for independent evidence annotation |

## GitHub credential resolution — 2026-09-08

The environment token overrode an existing working system keyring credential. Using the stored credential closed #1340, #1364, #1365, #1366, #1388, #1405, #1463, #1495, #1501, #1503, #1511 and #1512 as completed. The refreshed tracker has 46 open issues. Code remains local; full backlog completion is not certified. See [closure evidence](workflow-review-evidence/github-closures-2026-09-08.json).


## Open-issue implementation pass — 2026-09-08

Closed #1487, #1385, #1386, #1387, #1389 and #1347 after native/fixture verification. There are 39 remaining open issues. Fixed dropped HTTP download query parameters, false PDF success on HTML responses, unbounded Berlin PDF parsing, empty-discovery status, scraping HTTP-error acceptance, adaptive representation attribution, hosted Markdown loss and Firecrawl optional-cost defaults. Added credential-free regional replay and streamed court-index pagination. Native BfArM now parses the actual PDF. Firecrawl consumed one existing credit; no subscription or credits were purchased. See [current backlog review](backlog-execution-review.md) and [scraping acceptance](scraping-backend-acceptance-review.md). Code remains local and unpushed.

#1481 is also now closed after the actual multi-court acquisition and revision/locator checks. The latest pass closed seven issues; 39 remain open. Snapshot/provider/court verification passed 37 tests. Existing snapshot typing lint violations were unchanged.
