# Integration roadmap implementation evidence

This branch starts from `8ac181b99b90decae069be7d35165395f89b8d1b`.
`backlog.json` tracks all 81 open issues captured at implementation start.
**This is a partial implementation checkpoint, not completion of the backlog.**
Optional adapters preserve the existing defaults. Installing a backend does not
activate it. No benchmark issue is satisfied by a unit test or synthetic probe.

## Available entry points

Install the pinned local backends with `pip install '.[workflow-integrations]'`.
Model and MCP adapters require their separate runtime dependencies and locally
provisioned model snapshots. Model revisions are in `config/integration-models.json`.

| Issues | Entry point | Current scope / remaining work |
| --- | --- | --- |
| #1473 | Research source pack `datacite-dois` | Native metadata, DOI lookup, cursor pagination; native capture regression; end-to-end publication and evaluation outstanding |
| #1509 | `src.integrations.text.SaTSegmenter` and chunker's `sentence_segmenter` | Exact source offsets; real model benchmark outstanding |
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
| #1506–1508 | Qwen scorer, optional multilingual NLI, LightOn OCR | Explicit adapters/model pins; actual model inference and benchmark outstanding |

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

`src.ingestion.zenodo.ZenodoClient().acquire(record_id, selected_file_keys, document_store)`
fetches a bounded public manifest, checks file sizes and MD5/SHA256 digests, and
uses the existing upload parser and document store for selected textual artifacts.
The default aggregate byte budget is 20 MB; at most 100 files may be listed.
All selected downloads are checked before storage. Restricted and embargoed
records fail explicitly. Metadata preserves DOI, concept/version links, licensing,
and related identifiers as JSON in document metadata. Record-specific IDs keep
versions separate. A native metadata fetch succeeded; document storage, replay,
restricted access and corrupt-download tests use synthetic files. General binary
software/data artifact storage remains outstanding for #1476.
Primary API documentation: https://developers.zenodo.org/ .
