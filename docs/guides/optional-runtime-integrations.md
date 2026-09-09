# Optional native runtime integrations

This is the operator entry point for the September 2026 Noesis implementation continuation. The earlier `workflow_review.py` readiness records and generic normalizers are not substitutes for these native execution paths. Source originals, the review inbox, artifact graph, document revisions, and report versions remain the authorities.

**Implementation is not acceptance evidence.** The provided native adapters have focused integration tests. A fixture result does not establish independent-human accuracy, a model being importable is not a successful inference run, and a reserved cost is not a provider invoice. The issue file mapping is in `docs/development/optional-runtime-implementation-status.json`; mapping alone does not establish implementation completeness. The current execution-path audit is `docs/development/execution-path-audit.md`.

## Install only the selected profiles

The project still has a minimal base installation. `pyproject.toml` now declares `optional-runtime`, `optional-sources`, `optional-linkage`, `optional-language`, `optional-ragas`, `optional-tracing`, `optional-conversion`, `optional-redaction`, `optional-bge`, `optional-gliner`, `optional-sat`, `optional-outlines`, `optional-whisperx`, `optional-paddle`, `optional-docling`, and `optional-zyte` extras. They do not select a production backend or download model weights.

Use a real Python executable and an isolated environment, rather than the desktop application's embedded `sys.executable`. The latter can point at an AppImage and silently fail subprocess-based package installation. For example, with Python 3.13 already installed:

```sh
python3.13 -m venv .venv-runtime
.venv-runtime/bin/python -m pip install -e '.[server,ingestion,optional-runtime,optional-sources,optional-linkage,optional-ragas,optional-tracing]'
.venv-runtime/bin/python -m src.noesis_cli.optional --doctor
```

Do not combine every heavy SDK in one environment. The queried Outlines 1.3.3 and WhisperX 3.8.6 distributions require Python below 3.14; GLiNER2 2.0.0's local extra constrains Transformers below 5. Noesis's general models extra instead selects Transformers 5. Use a separate environment for such profiles and point the parent job runner at its interpreter:

```sh
export NOESIS_OPTIONAL_PYTHON_GLINER2="$PWD/.venv-gliner/bin/python"
export NOESIS_OPTIONAL_PYTHON_OUTLINES="$PWD/.venv-outlines/bin/python"
export NOESIS_OPTIONAL_PYTHON_WHISPERX="$PWD/.venv-whisperx/bin/python"
```

Any registered job accepts `NOESIS_OPTIONAL_PYTHON_<UPPERCASE_BACKEND_WITH_UNDERSCORES>`. A separate interpreter receives the Noesis source/package root, not the parent's `.eval-packages` directory containing ABI-specific extensions. Install `optional-runtime` and the selected backend profile in that environment. Keep CPU/GPU Torch/torchvision/torchaudio variants compatible; WhisperX's current profile pins Torch 2.8. Large model and runtime installation was not performed for every backend during this continuation.

Ragas 0.4.3's native metrics were exercised with `langchain-community==0.3.31`; the newer installed community package removed an import Ragas still used. The optional Ragas profile retains the working top-level compatibility pin. Record the resolved transitive environment using `python -m pip freeze` when preparing a real benchmark; top-level extras are not a universal cross-platform lockfile.

## Execute a source-bound job

The installed `noesis-optional` entry point and `python -m src.noesis_cli.optional` are equivalent. Use the normal Noesis configuration file and principal. Trusted local operator scopes come from `NOESIS_OPTIONAL_SCOPES`, **not** request JSON. A request cannot grant itself `operator` or override the configured principal.

```sh
export NOESIS_OPTIONAL_SCOPES='knowledge:optional:execute,knowledge:optional:read,namespace:research:write,namespace:research:read,document:DOCUMENT_ID:read'
python -m src.noesis_cli.optional --config /path/to/noesis.toml \
  --request /path/to/request.json --output /path/to/new-result.json
```

`--output` creates a new mode-0600 file and refuses to overwrite existing files. Without it, authorized evidence is written to standard output; do not direct that output to public logs. Sanitized errors contain a code and exception type, not credentials or remote response text. Exit 0 denotes a completed command (not independently verified quality), 2 a configuration/request error, and 3 a recorded failed/unavailable/partial/cancelled backend outcome, including replay.

A GLiNER request has this shape; replace the IDs with an actual committed document revision:

```json
{
  "kind": "analysis",
  "namespace": "research",
  "run_id": "gliner-review-001",
  "backend": "gliner2",
  "sources": [{"document_id": "DOCUMENT_ID", "revision_id": "REVISION_ID"}],
  "parameters": {"labels": ["person", "organization"], "language": "de"},
  "timeout_s": 60,
  "max_rss_bytes": 4294967296
}
```

`OptionalAnalysisStore` replaces supplied text with the captured source text. Source access is rechecked on replay; current revisions are checked again before artifact publication. Only completed backend tasks create source-linked enrichment artifacts; source edits during inference abort publication. Worker completion alone is insufficient. Registered task schemas preserve unavailable, partial, failed and cancelled states through the worker, store, telemetry, benchmark scoring and CLI. Incomplete results remain replayable diagnostics without a successful artifact. Semantic NLI abstention and a report pending review are not execution failures. Legacy false-success envelopes are downgraded on optional-runtime inspection/replay without rewriting saved history; this does not automatically invalidate previously created graph rows or downstream artifacts. Reusing a run ID with another source or configuration is a conflict.

Available source-bound backends include GLiNER2, SaT, Presidio, stance/frame classifiers, E5, BGE-M3, Qwen3 reranking, mDeBERTa NLI, Splink, RapidFuzz, Ragas, PaddleOCR, LightOnOCR, and WhisperX. `analysis_read` retrieves completed or diagnostic runs under current source access. `entity_review` explicitly transfers an eligible Splink/RapidFuzz candidate for existing canonical IDs into `EntityHistoryStore` and `ReviewInboxStore`; it does not merge entities.

Paddle/LightOnOCR/WhisperX input hashes must belong to the attached source revision's stored metadata and to an existing captured binary blob. WhisperX additionally uses the original transcript stored as `transcript_json`, not a caller-invented transcript. The actual WhisperX dispatcher verifies at most 50,000,000 captured bytes and passes those exact bytes to FFmpeg's pipe-only protocol allowlist, never the legacy path loader. `max_seconds` defaults to 600 and may be reduced; duration/protocol failures occur before the aligner loads. External media playlists and overlong input fail explicitly. Some seek-dependent formats are unsupported by this bounded decoder.

## Captured entity-linkage inputs

Source-bound `rapidfuzz` and `splink` analysis does not accept arbitrary record attributes with unrelated source references attached. RapidFuzz `source`/`candidates`, and Splink `records`, must select attributes from committed captured revisions. Each record supplies an existing active canonical `id`, an operator-declared `type`, and `field_sources`. The canonical assignment and type are explicitly review hypotheses, not model-verified identity facts.

```json
{
  "id": "entity:EXISTING_ID",
  "type": "Organization",
  "field_sources": {
    "name": {"document_id": "DOCUMENT_ID", "revision_id": "REVISION_ID", "pointer": "/content", "start": 0, "end": 12},
    "address": {"document_id": "DOCUMENT_ID", "revision_id": "REVISION_ID", "pointer": "/metadata/address"},
    "identifiers": {
      "registry": {"document_id": "DOCUMENT_ID", "revision_id": "REVISION_ID", "pointer": "/metadata/registry_json", "json_pointer": "/id"}
    }
  }
}
```

Offsets are Unicode codepoints, not UTF-16 offsets. `name` is required; `aliases` is a list of selectors, `identifiers` maps identifier types to selectors, and `address`/`affiliation` are optional selectors. Only captured `/content` and `/metadata/...` values are selectable. All selected revisions must appear in the authorized `sources` list. `json_pointer` parses an explicitly selected JSON-encoded string, not an expression or external resource. Supplied raw names/revisions/provenance are rejected rather than trusted.

Noesis derives record revisions and the exact native-input hash, validates output IDs/revisions/identifier guards, and retains field locators as `noesis-entity-source-binding-v1`. Canonical state is checked before artifact publication and review routing. Old linkage runs without a binding remain diagnostic and cannot enter review. Standalone authored benchmark inputs remain separate from this production source-bound contract.

## Combined GLiNER2 extraction

Set `parameters.schema` instead of `parameters.labels` to execute a combined native schema. Example schemas are `config/extraction_schemas/german-authorities.json`, `eu-funding.json`, and `berlin-legal.json`; supply the parsed schema object in the request. Schemas contain described `entities`, described `relations`, and named `structures` with extractive `str`/`list` fields. `threshold` defaults to 0.5; selecting it is not calibration evidence.

Native inference returns exact source-bound entity spans, relation head/tail spans and structured field spans. Missing fields/structures are explicit. Unsupported output keys, out-of-source text, non-finite confidence and oversized results fail validation. Description-qualified relation names observed in GLiNER2 2.0.0 are mapped only through exact aliases from the requested schema, with the native name retained. Source revisions, model pin, schema hash and source-text hash accompany the observations. Schema adherence and model confidence do not verify factual support; this path does not automatically insert approved knowledge-graph facts.

Real German/English inference was attempted in the compatible Python 3.12 environment. The first worker runs exposed the description-qualified relation mismatch; the repair passes both synthetic and captured-native-output regressions. A post-repair native worker rerun was blocked by the execution tool, so the saved pre-repair failures are not relabelled successful. See `continuation-gliner-schema-pre-fix.json`, `continuation-gliner-schema-smoke.json`, and the recorded native fixture for the exact distinction.

## Model caching and resources

The model registry contains immutable 40-character revisions and licenses. `--doctor` checks cache availability without running inference or using the network. Use a separately authorized `model_download` request with `download_approved: true`, `model`, and `max_download_bytes`; it requires `knowledge:optional:download`. A missing pinned model is `model_unavailable`, not a reason to substitute random or fixture outputs.

The worker uses a fresh process group and a private working directory, strips credential environment variables, disables Hugging Face online access and Ragas telemetry, limits CPU threads, kills the process tree on cancellation/deadline/RSS breach, and bounds the returned JSON. Cold-start time and child-process memory belong in the measured result. The job runner is resource isolation, not a hostile-code sandbox.

Presidio uses locally installed spaCy models only; missing assets produce `model_unavailable` without invoking spaCy's downloader. Docling requires `NOESIS_DOCLING_ARTIFACTS_PATH` and preinstalled EasyOCR German/English assets. EasyOCR downloads and Docling remote services are disabled. PDF worker dispatch validates the digest on the bytes actually parsed; Docling receives a verified `DocumentStream` rather than reopening a path. Native success/partial_success/failure/skipped states become completed/partial/failed/unavailable. Truncated OCR pages likewise produce partial diagnostic tasks, not completed enrichments. GROBID accepts an explicitly configured loopback service, records its actual version, sends repeated coordinate fields, and bounds TEI streaming. The parser benchmark reports client process-tree memory separately from GROBID server memory.

```sh
python scripts/evaluate_pdf_backends.py --backends pymupdf --out pdf-results.json
python scripts/benchmark_scraping_backends.py --backends scrapy playwright --out scrape-results.json
```

PDF scoring includes actual table row/column cell positions and structured references in addition to text/order/page/bbox metrics. Rendered HTML and backend Markdown are retained in the scraping benchmark. Its current fixtures include a real HTTP 503 and scroll-triggered lazy content. Failures and unavailable metrics are not converted into successes. The tested scroll fixture remains an unsupported case for the selected browser configurations; the benchmark captures that limitation.

## Frozen native benchmarks

The `benchmark` request kind runs an explicit `noesis-native-benchmark-v1` manifest. Each case specifies a registered local operation, bounded input payload, test split/group, source/domain/language, label origin, expected output and a JSON pointer into the native result. Supported metrics cover exact classification labels, entity spans, sentence boundaries, character/word edit rates, graded retrieval rankings and word-alignment errors. Failed/unavailable cases remain in the denominator and prevent success-only aggregate means.

`retrieval-minilm`, `retrieval-e5` and `retrieval-bge-m3` execute actual encoding and an isolated model-versioned DuckDB index over the same supplied document corpus. MiniLM is pinned to the upstream revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`; E5 retains query/passage prefixes. BGE modes use explicit dense/sparse/multi-vector weights. Results retain corpus identity, source revisions, encoding/indexing/query times, serialized index size, and isolated-worker RSS/deadline observations. The timing contract `noesis-retrieval-timing-v2` reports model loading, corpus encoding, query encoding and search separately: `query_seconds` includes warm query encoding plus search, not model loading, index construction/sizing or teardown. Historical timings are not retrospectively relabelled. BGE encodes the entire supported corpus (up to 200 documents) in calls of at most 128 records. No production index is modified. Split or chunk oversized sources before benchmarking; the runners do not silently truncate evidence to make a result pass.

`tests/fixtures/workflow_review/native_retrieval_smoke.json` demonstrates German/German, English/English and German/English cases with a requested cutoff of 30. Its relevance labels are explicitly authored fixtures. `config/optional_examples/benchmark.json` selects it. Supply real held-out labels and separately frozen development groups for an acceptance comparison. Without cached weights/dependencies the same command records unavailability, not fixture vectors or invented quality scores.

## Native regional and hosted acquisition

`regional` supports official court discovery/XML, CELLAR query/dissemination relationships, EMA medicine/document data, BfArM RSS/documents, explicit Berlin publication acquisition, OpenCorporates company lookup/search, and review-only OpenSanctions candidates. These use `DurableHTTP`, `RegionalEvidenceStore`, and normal document revision storage. `registry_import` handles bounded hash-selected CTIS/DRKS exports and selected Berlin publications. CTIS/DRKS field maps and export schema versions must describe the actual export; no undocumented public REST API is assumed.

Regional observation receipts now include `source_refs`, aligned with `document_ids`, for the exact committed revisions. Replaying an older captured import returns its original receipt without restoring the older document. Document batches and their receipt commit together. Trial recruitment status, medicine authorisation status and legal passages mentioning withdrawal do not retract the source document; an explicit source lifecycle operation is still respected.

Use `regional_review` to send a selected captured candidate to the existing review inbox:

```json
{
  "kind": "regional_review",
  "namespace": "science",
  "parameters": {
    "observation_id": "regional-observation:REPLACE_FROM_IMPORT_RECEIPT",
    "record_index": 0,
    "relationship_index": 0,
    "domain": "clinical-trials"
  }
}
```

CTIS/DRKS relationships must contain explicit supported DRKS, CTIS or DOI identifiers. Other identifiers remain captured metadata. For OpenSanctions, omit `relationship_index` and supply `entity_id` for an existing local entity plus `entity_sources`, an array of exact document/revision references. This assignment is recorded as a coordinator's review hypothesis: neither a query ID nor a provider score establishes canonical identity. Both operations require current source revisions and the ingestion, entity-history read/write/review, inbox read/write, namespace-write and document-read scopes (or the trusted operator scope). Replays reuse the same inbox task. Independent review decisions go to the existing ledger and never automatically publish merges. Legacy receipts without exact revision references cannot enter this queue.

The court index operation accepts an explicit `max_index_bytes` ceiling (default 20,000,000; maximum 100,000,000), charged against the immutable project byte budget. Large official indexes can exceed the default even when selecting a single decision; `limit` bounds selected records, not download size. Use a new budget/observation for a deliberately enlarged attempt. Failed downloads remain failed receipts; the larger ceiling does not establish live availability.

CELLAR requests select ELI and ECLI using the documented `resource_legal_eli` and `case-law_ecli` predicates. Grouped results retain all native bindings and returned identifier variants; relationships are explicitly scoped to the bounded query page. References: [CELLAR knowledge graph examples](https://op.europa.eu/en/web/cellar/cellar-data/metadata/knowledge-graph) and [EUR-Lex ELI query documentation](https://eur-lex.europa.eu/content/eli-register/ELI-pillarIV-helper-documentation_en.pdf).

OpenAlex content outcomes bind the selected work metadata, representation, budget and observation. Offline replay returns the original source revision without overwriting a newer acquisition. Conflicting metadata requires a new observation. Both the single-backend and corpus forms of `scripts/evaluate_pdf_backends.py` use bounded native workers and retain partial/unavailable/failed states.

Network requests require `network_approved: true` plus an explicit immutable budget with `budget_id`, `max_requests`, `max_usd_micros`, `max_bytes`, `account_ref`, and `reuse_notice`. Scoped access requires ingestion execution and namespace-write permissions. HTTP receipts record captured bytes, attempt state, retries/deferred failures and conservative reservations; offline replay does not need DNS or new credentials.

`hosted_search` uses native Exa/Tavily parameter names and result envelopes. `hosted_selected` acquires explicitly selected original URLs through the existing discovery-acquisition pipeline. `hosted_capture` supports bounded Firecrawl, the Scrapy/Zyte boundary, and Jina Reader. Provider summaries and transformed Markdown/HTML do not become original response bytes or precise source locators. Jina requires a matching original snapshot. `openalex_content` reads native per-format `has_content` and `content_urls`, validates work/representation identity, stores PDFs/TEI, and uses restart-safe metered reservations. An empty/scanned PDF does not become successful extracted full text.

Credentials use the fixed `NOESIS_<PROVIDER>_API_KEY` variables (documented conventional aliases are supported). Do not put tokens in JSON, source URLs, fixtures or logs. No paid request was performed in this continuation. A configured reservation ceiling is not a measured invoice or independently verified license; live pricing and reuse terms remain operator prerequisites.

## MCP, review and reporting

`github_capture` uses the official read-only GitHub MCP endpoint or an explicit local instance, an allowlisted repository set, native issue/PR/comment tools, bounded pagination, and standard snapshots/documents. Comments and proposed changes are not represented as merged code. `capture_replay` reads previously captured evidence without live MCP access and with current Noesis scopes.

`browser_capture` uses the existing remote federation adapter and a persistent SDK session. Configure the trusted `config/mcp/playwright_guard.mjs` init-page hook and `playwright-evidence.json`, with `NOESIS_BROWSER_ALLOWED_ORIGINS` set to an explicit JSON HTTPS origin list. The guard blocks undeclared origins, non-read HTTP methods, redirects, private IP destinations, WebSockets, and downloads; the client verifies the actual URL after initial navigation and every click/wait, before allowing another action. Click arguments are validated against the connected server's advertised `ref`/`target` schema, and native tool errors are not captured as source evidence. This is not a security boundary for executing hostile downloaded programs. The operator must explicitly confirm guard configuration. Cancellation/error paths always attempt browser cleanup.

`annotation_export` and `annotation_import` exchange native Label Studio JSON under explicit transfer approval and mapped assigned reviewer IDs. Source revisions, label schema hashes, UTF-16/codepoint offset conventions, annotator effort, machine preannotations and independent votes are preserved. Import does not adjudicate or release datasets. Disputed span/support labels are excluded from consensus training rows. Source originals and the Noesis review/release scopes remain authoritative.

`report_proposal` uses the native Outlines generator through the existing report-update store. It receives authorized captured evidence text, preserves assertion IDs and base revisions, constrains citations/dependencies, and rechecks both original and newly proposed dependencies before acceptance. Proposals remain pending individual review. Schema validity does not verify support; the unchanged report/bibliography export does not publish externally.

Optional `analysis.trace` configuration enables a local Phoenix OTLP sink and requires `knowledge:telemetry:export`. The independent tracer provider exports pseudonymous project/run/evidence IDs and allowlisted timings/statuses, never evidence body text or credentials. Disabled mode creates no exporter. Explicit retention configuration is required; OTLP submission is not proof of collector retention or debugging utility, and the existing run store is not replaced.

## Validation and acceptance

Focused tests execute native Splink/RapidFuzz scoring, Ragas ID metrics, OpenTelemetry spans, a real local MCP SDK HTTP handshake, native regional/hosted response mappings, source revision conflicts, Label Studio review routing, and bounded worker failures. Neural-model tests also use injected deterministic engines to test contracts without downloading weights; those are not neural accuracy measurements.

The independent 500–1000-sentence dual-human dataset (#1420), human-audited relevance/support and stance/frame accuracy, reviewer/pilot measurements, representative scan/scrape corpora and authorized paid-service measurements remain separate acceptance tasks. Installation compatibility and availability are recorded honestly. Do not close tracking parents from test count or SDK availability alone.

Primary interface references used while implementing the adapters: official package metadata on PyPI; [GLiNER2](https://github.com/fastino-ai/GLiNER2), [Outlines](https://dottxt-ai.github.io/outlines/latest/api_reference/), [Docling OCR configuration](https://github.com/docling-project/docling/blob/main/docling/datamodel/pipeline_options.py), [GROBID coordinates](https://grobid.readthedocs.io/en/latest/Coordinates-in-PDF/), [GitHub MCP](https://github.com/github/github-mcp-server), [Playwright MCP](https://github.com/microsoft/playwright-mcp), [Ragas metrics](https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/context_precision/), [Label Studio exports](https://labelstud.io/guide/export.html).


### Offline regional replay

Replay a committed regional acquisition or registry import without a provider client, credentials, network, or the original import file:

```json
{"kind":"regional","operation":"replay","namespace":"science","provider":"drks","observation":"first","reuse_notice":"Original publisher terms"}
```

The trusted principal and namespace scopes must still authorize ingestion access. Missing observations and mismatched providers are explicit errors. Replay returns the original receipt and never restores an older document as the current revision.

Regional PDF downloads preserve query selectors in their source URL. A selected `.pdf` URL returning HTML fails explicitly. Berlin publication PDFs use a 100-page ceiling inside the shared 120-second/1-GiB parser worker; failed or incomplete jobs publish no evidence.
