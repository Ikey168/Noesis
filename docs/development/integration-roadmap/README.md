# Integration roadmap implementation evidence

This branch starts from `8ac181b99b90decae069be7d35165395f89b8d1b`.
`backlog.json` tracks all 81 open issues captured at implementation start.
**This is a partial implementation checkpoint, not completion of the backlog.**
Optional adapters preserve the existing defaults. Installing a backend does not
activate it. No benchmark issue is satisfied by a unit test or synthetic probe.

## Available entry points

Use Python 3.12 or later for the pinned local backends (Lingua 2.2.0 does not
provide Python 3.11 wheels). Install with `pip install '.[workflow-integrations]'`.
Install `.[workflow-models]` for the pinned SaT/Outlines/Transformers runtime.
Model and MCP adapters require their runtime dependencies and locally
provisioned model snapshots. Model revisions are in `src/integrations/model-pins.json`.

| Issues | Entry point | Current scope / remaining work |
| --- | --- | --- |
| #1473 | Research source pack `datacite-dois` | Native metadata, DOI lookup, cursor pagination; native capture regression; end-to-end publication and evaluation outstanding |
| #1509 | `src.integrations.text.SaTSegmenter` and chunker's `sentence_segmenter` | Exact source offsets; real ONNX smoke probe passed; independent benchmark outstanding |
| #1510 | Normalizer `language_backend="lingua"` | Language confidence, abstention and mixed-language spans; independent corpus outstanding |
| #1511 | Entity resolver `fuzzy_backend="rapidfuzz"` plus explicit threshold | Existing identity rules retained; false-merge and throughput benchmark outstanding |
| #1512 | Origin inference `candidate_backend="minhash"` | Approximate candidates plus exhaustive provenance pairs; candidate-run receipts; measured recall outstanding |
| #1513 | Planner `optimizer="cp-sat"` | Bounded coverage/cost/independence constraints; end-to-end execution comparison outstanding |
| #1514 | Media connector `aligner=WhisperXAligner(...)` | Optional word alignment adapter; dependency/model and actual audio evaluation outstanding |
| #1515 | `SDMXConnector` | Native series parsing and bounded transport; ECB live capture tested; Eurostat/Bundesbank and code-list mapping outstanding |
| #1516 | Dataset store `validate_batch` | Explicit Pandera preflight, preserves declared schema; quarantine integration and comparative evaluation outstanding |
| #1517 | Quantitative store `convert_physical` | Pint physical conversions and receipts; custom-unit registry mapping outstanding |
| #1518–1519 | Geospatial `relation(backend="shapely")`, `import_projected_geometry` | Topology and offline pyproj transforms; wider geometry fixtures/evaluation outstanding |
| #1520 | Report updates `generate_proposal` with `OutlinesEditor` | Schema-constrained pending text proposals; real generation and semantic revision evaluation outstanding |
| #1521 | Review inbox `export_label_studio` / `import_label_studio` | Pinned source/reviewer checks, exact Unicode spans, pending proposals; independent human annotation outstanding |
| #1522 | Anomaly store `simulate_drift` | Ordered ADWIN replay with duplicate/late-event handling; watch-delivery integration and tuning outstanding |
| #1523 | Authored report `render` / MCP export `output_format="docx"` | Pandoc citeproc DOCX/HTML; PDF engine and larger citation/rendering evaluation outstanding |
| #1524 | Research package `export_rocrate` | Native package in RO-Crate envelope; detailed entity mapping and independent validator outstanding |
| #1495 | Upload parser `backend="markitdown"` | Explicit converted-text representation; actual HTML smoke test; document corpus outstanding |
| #1497 | `src.integrations.warc` | Bounded capture read/write and document ingestion; archive corpus and full ingestion regression outstanding |
| #1501–1503 | `src.integrations.mcp.federation_adapter` | Explicit presets and tool allowlists; real service interoperability, stateful Playwright lifecycle and scoped evaluation outstanding |
| #1504 | E5 embedding input policy and query embedding interface | Real pinned CPU smoke probe; independent retrieval benchmark outstanding |
| #1506–1508 | Qwen scorer, optional multilingual NLI, LightOn OCR | Explicit adapters/model pins; Qwen inference passed; NLI/OCR inference and independent benchmarks outstanding |

The remaining issues in the ledger have not been implemented by this checkpoint.
In particular BGE-M3 multi-mode retrieval and GLiNER are not implemented here.

## Evidence

The expanded regression command passed **152 tests** using CPython 3.12 and the
installed optional backends. Native DataCite JSON and ECB SDMX captures live in
`tests/fixtures/integrations/`; their README records provenance. External fixture
records are retained as data and are never interpreted as executable instructions.

`retrieval-probe.json` records real CPU inference on four synthetic DE/EN queries
and four passages. Both MiniLM and E5 achieved recall@1 of 1.0 on that trivial
probe, with E5 slower. This does **not** establish production quality or justify
switching defaults. Run `scripts/benchmark_integration_retrieval.py` to repeat
with locally cached models; use independently annotated data for adoption.

Independent human annotation cannot be replaced with generated labels. Live paid
service comparisons require operator-provided credentials and budgets. Neither
missing evidence nor adapter presence is grounds for closing those issues.

## Registry and artifact clients

`src.ingestion.ror.RORClient().enrich(ror_id, graph_store)` reads the public ROR
v2 endpoint and persists registry assertions in the existing knowledge graph.
Supply either a short ROR ID or its `https://ror.org/` identifier. No API key is
required for the tested endpoint. Calls use a 15-second timeout and 2 MB response
limit; each search reads one page of at most 20 records. Callers schedule requests
within the provider's published rate limits; there is no automatic bulk crawl.

Search returns candidates and never automatically resolves an ambiguous name.
Explicit IDs create distinct nodes, with typed parent/child/related/predecessor/
successor relationships retained in `ror_record.relationships`. Historical names
and distinct registry revisions remain in aliases and `ror_history`. This does
not infer equivalence between related institutions. The initial graph display
name remains stable; the latest authoritative name is `ror_record.name`.
Native Berlin fixture, inactive-record, same-name/different-ID, replay and durable
restart tests cover this path. A direct public fetch of `01hcx6992` succeeded.
Primary API documentation: https://ror.readme.io/docs/rest-api .

`src.ingestion.zenodo.ZenodoClient().acquire(record_id, selected_file_keys, document_store, languages=language_by_file_key)`
fetches a bounded public manifest, checks file sizes and MD5/SHA256 digests, and
uses the existing upload parser and document store for selected textual artifacts.
The default aggregate byte budget is 20 MB; at most 100 files may be listed.
All selected downloads are checked before storage. Restricted and embargoed
records fail explicitly. Metadata preserves DOI, concept/version links, licensing,
and related identifiers as JSON in document metadata. Record-specific IDs keep
versions separate. A native metadata fetch succeeded; document storage, replay,
restricted access and corrupt-download tests use synthetic files. Pass `artifact_only=True` to retain software/data bytes without parsing or execution.
Both modes store verified original bytes in the document store and carry immutable
`noesis-artifact:sha256:` content references, readable with `read_artifact`. Blob and
document writes commit together; validation failure rolls back both. Explicit
provider paper links are available through `related_resources`. Four focused tests
cover document and binary replay, distinct record versions, durable reopening,
restricted downloads, checksums and validation rollback.
Primary API documentation: https://developers.zenodo.org/ .

## Latest combined regression and model probes

The combined regression command passed **294 tests with 3 optional-backend skips**.
The new CI integration job installs the optional local libraries and executes their
actual backend tests; packaging checks also resolve both new extras and verify
that the model registry is present in an installed wheel.

`reranker-probe.json` records real Qwen inference through `CrossEncoderReranker`
with `require_model=True` (fallback results cannot satisfy this probe). All four
synthetic pairs ranked their relevant passage first. Median latency was 4472 ms
for a two-passage batch on two CPU threads. This latency and the tiny fixture do
not justify a default switch. Run `python -m scripts.benchmark_integration_reranker
--out PATH` after explicitly provisioning the pinned model.

`segmentation-probe.json` records real CPU ONNX SaT inference on two synthetic
German/English samples. Model and tokenizer revisions are both pinned; instantiate
`SaTSegmenter(model_dir, tokenizer_path=tokenizer_dir, use_onnx=True)`. It never
falls back to fetching a default tokenizer. Exact source slices are recorded,
but sentence-boundary quality still needs independent annotations.

A public OpenReview API v2 notes request returned HTTP 403 in this environment;
no OpenReview integration or successful live validation is claimed.

CP-SAT correctness was additionally checked against exhaustive enumeration on
60 generated six-source instances, including infeasible combinations. The planner
integration test verifies stable plan hashes and execution fallback after a
selected source fails. Eight focused planner tests pass. This is an independent
algorithmic oracle; the cases are synthetic and do not measure real acquisition
utility or actual provider spending. The original greedy mode remains explicit.

`src.ingestion.opencitations.OpenCitationsClient` captures incoming or outgoing
citation records using API v2.2.0. Optional provider tokens stay in request headers.
The documented service has no cursor pagination: the adapter bounds the full
response (5 MB / 10,000 edges by default), then `ingest_snapshot` resumes over a
captured snapshot with a hash-bound local cursor. OCI identifiers, observation
times and native records are retained on knowledge-graph edges. Replaying an
identical snapshot does not duplicate edges or provenance. A real 72-edge capture,
incoming direction, malformed identifiers, changed snapshots and provider errors
are tested. Provider copies do not count as independent corroboration. Binding
acquisition to the research API/MCP surface and reconciling non-DOI cross-provider
identities remain outstanding for #1472.
Documentation: https://api.opencitations.net/index/v2 .

`src.ingestion.orcid.ORCIDClient(token=read_public_token).enrich(orcid, graph_store)`
uses ORCID v3 public professional-record data. Provision a `/read-public` OAuth
token according to ORCID's documentation; secrets remain in request headers.
Requests have a 15-second timeout and 2 MB cap. Names, public employment/education
and works keep their original assertion sources and dates. Unavailable/private
fields remain explicit missingness, never negative evidence. Explicit identifiers
bind graph identities; same-name researchers with different ORCIDs remain distinct.
Registry revisions are retained in `orcid_history`. The native public example was
fetched anonymously for parser verification; authenticated production access has
not been tested. Ambiguous-name integration with the review inbox also remains
outstanding for #1474. Tests use an explicitly synthetic German name/affiliation
change in addition to the public example.
Documentation: https://info.orcid.org/documentation/api-tutorials/api-tutorial-read-data-on-a-record/ .

## DataCite runtime completion (#1473)

Both declared DataCite sources use the native public REST API. Queries support
DOI lookup, author names, versions and native affiliation/client/resource filters;
bounded backfills query metadata `updated` timestamps in UTC (not publication
dates inferred from publication years). Cursor origins and page sizes remain
adapter controlled. No API key or new dependency is required.

Provider `relatedIdentifiers` become directed bibliographic links with source and
target identifier types, the exact predicate, provider record URL and native
assertion. Links are persisted in immutable document metadata and available via
`DocumentStore.related_resources(document_id, revision=...)`. Uncollected targets
remain explicit identifiers; the API neither downloads them nor interprets links
as factual support. Existing revision reads/export retain the same links.

`python -m pytest -q tests/unit/integrations tests/unit/ingestion/test_source_pack_runtime.py`
passed 47 tests. The added native-shaped test exercises two HTTP pages through
actual runtime commits, unchanged replay, changed resource version and historical
links. A separate check preserves HasPart/IsPartOf direction and URL identifiers.
Native captured Berlin metadata is reused; controlled version changes are
synthetic test transitions. Documentation: https://support.datacite.org/docs/queries
and https://support.datacite.org/docs/connecting-to-works .
