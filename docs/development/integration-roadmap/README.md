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
| #1473 | Research source pack `datacite-dois` | Native queries, cursor pagination and typed relationships; committed ingestion/replay and historical version checks passed |
| #1509 | `src.integrations.text.SaTSegmenter` and chunker's `sentence_segmenter` | Exact source offsets; real ONNX smoke probe passed; independent benchmark outstanding |
| #1510 | Normalizer `language_backend="lingua"` | Language confidence, abstention and mixed-language spans; independent corpus outstanding |
| #1511 | Entity resolver `fuzzy_backend="rapidfuzz"` plus explicit threshold | Existing identity rules retained; false-merge and throughput benchmark outstanding |
| #1512 | Origin inference `candidate_backend="minhash"` | Approximate candidates plus exhaustive provenance pairs; candidate-run receipts; measured recall outstanding |
| #1513 | Planner `optimizer="cp-sat"` | Bounded constraints; actual greedy comparison, exhaustive oracle and execution fallback/replay passed |
| #1514 | Media connector `aligner=WhisperXAligner(...)` | Optional word alignment adapter; dependency/model and actual audio evaluation outstanding |
| #1515 | `SDMXConnector` | Native ECB/Eurostat/Bundesbank data, structure/code-list mapping, archived ingestion and overlapping baseline comparison verified |
| #1516 | Dataset store `validate_batch` | Explicit Pandera preflight, preserves declared schema; quarantine integration and comparative evaluation outstanding |
| #1517 | Quantitative store `convert_physical` | Pint physical conversions plus isolated versioned Noesis unit definitions/aliases; formula evaluation and comparative cost evidence outstanding |
| #1518 | Geospatial `relation(backend="shapely")` | Topology; wider geometry fixtures/evaluation outstanding |
| #1519 | Geospatial `import_projected_geometry` | Published Berlin coordinate references, offline transform receipts and import replay validated |
| #1520 | Report updates `generate_proposal` with `OutlinesEditor` | Schema-constrained pending text proposals; real generation and semantic revision evaluation outstanding |
| #1521 | Review inbox `export_label_studio` / `import_label_studio` | Pinned source/reviewer checks, exact Unicode spans, pending proposals; independent human annotation outstanding |
| #1522 | Anomaly store `simulate_drift` | Ordered ADWIN replay with duplicate/late-event handling; watch-delivery integration and tuning outstanding |
| #1523 | Authored report `render` / MCP export `output_format="docx"` | Pandoc citeproc DOCX/HTML; PDF engine and larger citation/rendering evaluation outstanding |
| #1524 | Research package `export_rocrate` | Native package in RO-Crate envelope; detailed entity mapping and independent validator outstanding |
| #1495 | Upload parser `backend="markitdown"` | Explicit converted-text representation; actual HTML smoke test; document corpus outstanding |
| #1497 | `src.integrations.warc` | Bounded capture read/write and document ingestion; archive corpus and full ingestion regression outstanding |
| #1501, #1503 | `src.integrations.mcp.federation_adapter` | Explicit presets and tool allowlists; real Playwright session probe passed; GitHub and browser-domain evaluation outstanding |
| #1502 | `Context7Research` | Live documentation discovery/query and selected original capture; version uncertainty retained |
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

## Versioned Pint registry mapping

`QuantitativeStore.convert(..., backend="pint")` resolves names/aliases through
the existing Noesis ledger and builds an empty, isolated Pint registry from those
exact definitions. Receipts pin unit IDs, semantic versions, definition hashes,
registry hash and Pint version. Custom compound dimensions and Noesis offset
semantics are preserved. Currency or exchange-rate inputs are explicitly refused;
existing economic conversion and comparability contracts remain authoritative.

39 focused quantitative/document-store tests passed, including candidate/native
agreement for length, Celsius, ratios, percentages, compound speed and half-even
rounding, plus replay after a custom unit version changes. This completes registry
mapping; formula-backend comparison and measured cost evidence remain for #1517.

## Planner evaluation and adoption (#1513)

`python -m scripts.benchmark_integration_planning --out planning-benchmark.json`
compares the actual planner preview implementations on 24 reproducible authored
Berlin objectives, with three repetitions each. These use synthetic capability,
authority and cost values; they are not measurements of named providers.
Greedy found 8 feasible cases and CP-SAT 10. Median preview latency was 3.97 ms
versus 4.86 ms; p95 was 4.56 ms versus 6.13 ms. Per-case coverage, independent
groups, projected costs, selected IDs and plan hashes are retained in the JSON.
The process peak includes fixture/database setup and both backends.

An independent exhaustive subset oracle verifies cost/count optimality or
infeasibility over 60 additional seeded small cases. Runtime tests exercise
persisted plan replay, fallback acquisition after a selected source fails, and
explicit UNKNOWN/INFEASIBLE errors followed by caller-requested greedy fallback.
The adapter reports the solver's OPTIMAL/FEASIBLE status without promoting a
feasible result to optimal. Costs round up to millionths and budgets round down;
the lexicographic objective minimizes projected cost, then source count. Coverage,
required sources, source-count and independence are hard constraints.

Decision: adopt as an explicit optional planner for this bounded constraint model.
Keep greedy as default: editorial authority is not optimized by CP-SAT and the
solver adds latency. OR-Tools 9.15.6755 is pinned in the optional installation;
limits are 1000 candidates/parts, one worker and a 0.01–30 second solve deadline
(default two seconds). No paid acquisition was invoked or actual provider cost
inferred. Definition/receipt provenance continues through the existing planner.

## Published Berlin coordinate reference (#1519)

`python -m scripts.benchmark_integration_coordinates --out coordinate-benchmark.json`
compares two FU Berlin-Dahlem points published by Berlin Umweltatlas (page 25,
table 7) in geographic WGS84 and ETRS89/UTM33 coordinates. The source PDF was
retrieved and the table visually checked; its hash and factual coordinate
transcription are in `tests/fixtures/integrations/berlin-coordinate-reference.json`.
The 3 m tolerance accounts for 0.1 arcsecond rounding and the selected operation's
1 m stated accuracy; it does not claim survey precision.

Observed errors were 1.68 m and 0.98 m. Thirty repetitions per point measured
median transforms of 4.95 ms and 4.35 ms. pyproj 3.7.2 / PROJ 9.5.1 / EPSG
v11.022 receipts retain pipeline, area, accuracy, database date, grid list and
original coordinates/CRS. This transformation uses no grids. Network-enabled
operation is rejected; an unavailable best grid cannot silently choose a fallback.
Nine coordinate/geospatial tests cover published pairs, axis order, round trips,
invalid latitude, missing-grid failure and original-receipt import replay.

Decision: adopt as an explicit offline regional import option at declared source
precision. Other CRS pairs require their own reference validation. Existing
geographic/planar metric restrictions are preserved. Source:
https://www.berlin.de/umweltatlas/_assets/klima/klimaparameter/langjaehrig/de-texte/k413_2022.pdf?ts=1769763403

## Persistent MCP sessions and real Playwright probe

The official Python MCP client now keeps one session across discovery, navigation,
wait and snapshot calls, serializes concurrent calls, resets failed/timed-out
transports and exposes `close()` / a context manager for resource cleanup. Browser
presets require `navigation_origins=["https://www.berlin.de"]` (or explicit local
fixture origins). Only navigate, snapshot and bounded waits are exposed. This
checks requested navigation destinations; it is not a browser-wide network or
redirect sandbox. GitHub provider scope/evaluation and browser-domain enforcement
remain outstanding for #1501 and #1503. Context7 completion is described below.

The real `@playwright/mcp@0.0.80` server passed navigation and a later dynamic German
text snapshot through existing federation receipts. Its reported server version
is preserved in each result's backend provenance. `playwright-mcp-probe.json`
retains the observations and generated snapshot files from the authored local
page. Run `python -m scripts.probe_playwright_mcp --browser-path /path/to/chrome
--out probe.json` with Node/npx and an installed Chromium executable. The probe
starts an isolated profile, blocks service workers, and closes its client/browser
server afterward. For this server version, `--allowed-hosts` must include the
port, e.g. `127.0.0.1:8766`; a bare host receives HTTP 403.

Eleven MCP/federation tests passed, including a real official-SDK local server,
same-session identity, timeout/reset/reconnect, closed-client rejection and
pre-connection browser action/origin/wait limits. This is an interoperability
result on authored HTML, not independent evaluation of public websites or a
comparison with the production bulk browser acquisition path.

## Context7 documentation research (#1502)

`Context7Research` wraps the existing federated adapter. Discovery preserves
multiple library candidates and requires explicit library selection; query
results retain library ID, requested version, source links and retrieval time.
The provider did not attest an exact version in the tested response, so
`resolved_version` remains null even when the caller requests one. Conflicting
version suffixes fail before a remote call. Missing citations are explicit.

An anonymous live query against Context7 MCP 4.0.5 completed in 3.53 seconds.
It returned Pint documentation at `stable`, not a verified 0.25.3 snapshot.
The selected original page was independently fetched in 0.78 seconds, and its
208,885 bytes were captured through existing SnapshotStore and DocumentStore
contracts. `context7-probe.json` records the SHA256 and source URL. The original
capture does not retroactively verify the snippet's requested library version.
The source requires an identifying User-Agent; the client sends the Noesis
project identity. Captures are limited to explicitly selected cited URLs on an
allowed public host, same-host redirects, 2 MB and 15 seconds, with declared
language. Source snapshots are local evidence archives; no external publication
is performed.

Reproduce with `python -m scripts.probe_context7 --anonymous --database evidence.duckdb
--out context7.json --library-id /websites/pint_readthedocs_io_en_stable
--requested-version 0.25.3 --query "Create an empty UnitRegistry with Decimal numeric values"
--source-url https://pint.readthedocs.io/en/stable/_modules/pint/facets/plain/registry.html
--allowed-host pint.readthedocs.io --language en`. Provision `NOESIS_CONTEXT7_API_KEY`
for account-backed use and omit `--anonymous`; this run did not use an account.

Decision: adopt opt-in discovery/query and original-source capture while retaining
version uncertainty. Two focused tests additionally cover ambiguous names,
version conflicts, missing citations, unavailable originals, immutable byte
storage, separate retrieval observations and idempotent document replay.

Latest combined verification after the Context7 changes: **130 tests passed** across
integrations, quantitative/report/planner/geospatial/federation stores, document
and source-pack runtime, and quantitative/planner MCP surfaces.

## SDMX provider completion (#1515)

The optional sdmx1 2.27.0 connector now maps Eurostat lowercase dimensions and
ECB/Bundesbank attribute-based unit/frequency fields, preserving their native
names. Original numeric text, missing values/status flags, dataset action/validity,
provider prepared/extracted timestamps and retrieval time remain explicit.
Prepared/retrieved times do not establish a publication date or historical vintage.

`fetch_structure` / `parse_structure` map dataflows, dimensions and multilingual
code-list identities/versions. Data parsing accepts an explicit matching structure
and includes labels for selected dimension codes. Structural annotations outside
this mapping remain in the native RawSeries; the mapping states its coverage.
`ingest(query, observation_store, structure=...)` archives exact source bytes in
the existing SnapshotStore and publishes observations atomically. Both older
values and their full native metadata remain available from archived bytes.

Actual public requests succeeded for ECB EXR, Eurostat NAMA_10_GDP and Bundesbank
BBEX3; no account credentials were supplied. The overlapping German 2023 GDP
observation matched the existing Eurostat JSON-stat connector (4,254,930.0 in
provider unit CP_MEUR, preliminary flag preserved). A native Eurostat structure
capture contains six code lists and 5,751 codes. Bundesbank requires period syntax
matching frequency; older BBK01 example keys returned no data. HTTP diagnostics
now retain provider and status instead of a generic failure. Native unit codes,
multipliers and denominator dimensions are retained without currency conversion.

`python -m scripts.benchmark_integration_sdmx --out sdmx.json` replays the frozen
captures; add `--live` for bounded public downloads and a fresh baseline comparison.
The live command actually ran and records source URLs, capture timestamps and
hashes in `sdmx-live-evaluation.json`. It writes raw captures beside the requested
report. Twenty parser repetitions measured sub-1 ms median on these tiny data
responses; structure download/parsing and full-corpus throughput are separate.
Nine tests cover native mapping, source contract validation, code labels, missing
values, provider errors, byte/observation controls, atomic archived ingestion and
observation revision/replay. Limits are 8 MB / 10,000 observations by default,
20-second requests, and 100,000 structural codes; failed/oversized requests do not
publish truncated series.

Decision: adopt as an explicit connector for these verified paths and retain the
existing Eurostat default. This does not establish every provider endpoint,
historical vintage access or economic comparability. Primary documentation:
https://sdmx1.readthedocs.io/en/latest/sources.html and
https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-detailed-guidelines/sdmx2-1/structure-queries .
