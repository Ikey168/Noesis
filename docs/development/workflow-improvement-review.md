# Workflow improvement review

Reviewed 2026-09-05 at commit `0bf70327`. This extends the [source collection backlog](source-collection-backlog.md) to the rest of the workflow. It is a code review with targeted local reproductions, not a live deployment, load test, or independent model evaluation. No production implementation was changed.

The companion [new-feature review](workflow-feature-opportunities.md) adds 24 granular feature tasks across eight capabilities, including persistent research projects, living reports, a review inbox, systematic literature reviews, bibliography interoperability, forecasts, analysis notebooks, and decision records. Together these reviews contain 29 improvement candidates and 24 feature tasks.

<!-- workflow-review-publication -->
All 29 numbered improvement candidates and the four further-validation recommendations are published as granular GitHub issues. See the [complete issue index](workflow-issue-backlog.md) for task, parent, and prerequisite links. The companion feature review is also fully published.

## Current workflow and capabilities

Noesis collects documents, extracts claims/entities/events, resolves identities, builds knowledge projections, answers queries, watches changes, and exports research packages. Domain definitions already support multiple knowledge bases: corpus views over shared documents or separately provisioned namespaces. Documents can belong to multiple domains. See `config/domains.yml`, `src/kb/registry.py`, and `src/kb/membership.py`.

The canonical orchestration is `ingest -> extract -> resolve -> index -> query -> subscribe -> export`. Revision histories, artifact invalidation, hybrid retrieval, reranking, namespace policies, subscription polling, and package signatures already exist. Improvements should extend these components.

The highest-impact integration gap is in source-pack maintenance: `src/kb/maintenance.py:779` calls `reference_handlers`. Its extraction handler reads pre-populated `metadata.knowledge` through `_FixtureExtractor`, rather than extracting ordinary document text. `src/kb/derived_revisions.py:188` makes vectors from SHA-256 bytes and labels them `noesis-content-hash-vector-v1`; these do not encode semantic similarity. The deterministic reference workflow is documented honestly as a fixture, but successful maintenance receipts therefore do not demonstrate production extraction or semantic retrieval. The older argument-mining and embedding paths contain model integrations that can be connected here.

## Reproduced findings

The diagnostic scripts and captured output are in [workflow-review-evidence](workflow-review-evidence/). They use temporary/in-memory databases and injected deterministic providers or failures; they do not call paid model APIs. The chunking probe loads the implementation file directly because importing the RAG package in this environment eagerly requires `psycopg2`.

| Probe | Observed result | Implication |
|---|---|---|
| `document_identity` | Two distinct `content_ref`-only documents insert one row and report one duplicate; two distinct sources with identical text also collapse. | Ordinary document ingestion loses distinct source observations. Source-pack run metadata bypasses this branch; the finding is specific to other callers. |
| `stale_derivatives` | Revising a positive document to a negative correction refreshes zero enrichments and zero embeddings; changing the requested model also leaves `model-a` stored. | Direct enrichment and embedding passes skip existing IDs without checking revisions/configuration. This is separate from the newer derived-revision store. |
| `ambiguous_entities` | Resolving “Smith” chooses John Smith or Jane Smith depending on insertion order. | Ambiguous names silently inherit the first compatible identity. |
| `query_deadline` | A 20 ms query budget returns after approximately 260 ms with a timeout failure. | Executor shutdown waits for the running adapter despite the reported deadline. |
| `watermark_crash` | Failure immediately before watermark commit, followed by resume, yields a completed run with a null watermark using pass-through handlers. | Completed index receipts are skipped without repairing missing publication. The normal query handler instead rejects the missing watermark. |
| `chunk_offsets` | Two paragraphs sharing their first 50 characters both receive offset zero; the second cited slice differs from its text. | Heuristic position lookup corrupts evidence locators. |
| `mining_failure` | An injected model failure leaves zero prior claims and evidence rows; the batch reports `status=complete`, `failed=1`. | Replacement deletes precede successful extraction and are not rolled back per document. |
| `subscription_coverage` | Replaying the same items/watermark with changed coverage is accepted as replay; stored coverage remains complete. | Coverage is omitted from snapshot identity. |
| `package_closure` | Removing a required dependency from an unsigned package and recomputing its outer digest still verifies as valid. | Verification checks supplied hashes and omission declarations, but does not independently establish dependency completeness. This is not a hash or signature forgery. |
| `archive_io` | A nonexistent local archive path reports `archived`, then `restored` and `restored_atomically=true`; no file exists. | Archive/restore currently record metadata and verify the local checkpoint rather than moving/restoring archive bytes. |
| `membership_revision` | Changing an economics document to gardening scans zero documents on the next membership pass and retains its economics assignment. | Membership ledgers do not notice document revisions. |
| `checkpoint_truncation` | Three input records with limit two produce a two-record checkpoint marked complete for the full requested generation range. | A bounded checkpoint can silently omit records. |

These are 12 diagnostic scenarios, not a count of all independent defects. Timings are local observations, not performance benchmarks.

## Granular implementation candidates

Priority P1 means data correctness, recovery, or a missing capability needed for the claimed workflow. P2 means quality, scale, or an optional feature. “Reproduced” refers to the table above; “review” means directly observed implementation behavior; “evaluation” means proposed measurement rather than a confirmed defect.

### Workflow execution

**WF-01 — Connect production extraction to source-pack maintenance (P1, review).**

Evidence: `src/kb/maintenance.py:779`; `src/kb/workflows.py:649` registers `reference-fixture` with `metadata.knowledge` inputs.

Acceptance: Select a registered, versioned text extractor through maintenance configuration. A plain-text document without precomputed knowledge must produce provenance-linked outputs, or an explicit unavailable/unsupported outcome. Preserve the fixture handler for offline tests. Reuse existing argument-mining APIs where appropriate.

**WF-02 — Repair index watermark publication during workflow resume (P1, reproduced).**

Evidence: `src/kb/workflows.py:471` publishes after committing the index receipt; completed stages are skipped on replay.

Acceptance: Atomically persist the publication intent with the index receipt, or reconcile it on resume. Inject failure before knowledge watermark commit and before subscription watermark publication; resuming must publish exactly once and allow the normal query stage to proceed.

**WF-03 — Add a real-text acceptance run across domain knowledge bases (P1, evaluation).**

Evidence: `docs/guides/knowledge-engine-reference.md` proves deterministic subsystem composition using prepared fixture knowledge.

Acceptance: Exercise configured production handlers with a small, pinned real-text corpus spanning at least two domains. Cover update, correction, retraction, resumed execution, query evidence, subscription events, and export verification. Distinguish model unavailable, partial coverage, and successful execution. Depends on WF-01, IX-01, and relevant recovery fixes below.

### Document processing and domain assignment

**DP-01 — Preserve observation identity when deduplicating document content (P1, reproduced).**

Evidence: `src/ingestion/document_store.py:170`; `document_identity` probe.

Acceptance: Distinct binary references with absent text survive ingestion. Identical content from different source observations shares content storage without losing source IDs, dates, URLs, or origin relationships. Retries of the same observation remain idempotent. Coordinate with source collection issue #1392; publication-specific identity and generic store deduplication are different layers.

**DP-02 — Carry exact source offsets through chunk splitting (P1, reproduced).**

Evidence: `services/rag/chunking.py:398` searches for the first 50 characters and falls back to zero.

Acceptance: Preserve offsets as text is split, with an explicit coordinate system for normalized/section text. Repeated prefixes, short chunks, Unicode, whitespace normalization, and overlap produce locators that reconstruct the intended evidence span.

**DP-03 — Reassess domain membership after document revisions (P1, reproduced).**

Evidence: `src/kb/membership.py:268` selects only unscanned or embedding-pending pairs; the ledger has no content revision.

Acceptance: Track the revision and relevant metadata/vector configuration assessed per document/domain. Content or tag changes can add, lower, and remove assignments atomically. Test movement between domains and preservation of valid multi-domain membership; unchanged documents remain skipped.

### Extraction, enrichment, and graph updates

**EX-01 — Replace mined claims and evidence atomically (P1, reproduced).**

Evidence: `src/ingestion/argument_mining.py:130`; `mining_failure` probe.

Acceptance: Stage model outputs before replacing the current claim/evidence set, or use a safe per-document transaction. Model, classifier, and persistence failures preserve the prior committed generation and record a retryable failure. Successful revisions replace the set once.

**EX-02 — Include extractor/model configuration in mining freshness (P1, review).**

Evidence: `src/ingestion/argument_mining.py:109` compares content hash/status; `prediction_mode` is stored after processing but does not invalidate scans.

Acceptance: Ledger identity includes effective model, rule, schema, and relevant configuration versions. A changed extractor with unchanged text triggers controlled reprocessing; unchanged configurations do not. Depends on EX-01 for safe replacement.

**EX-03 — Retry failed graph projection independently of mining (P1, review).**

Evidence: `src/ingestion/argument_mining.py:148` suppresses graph update exceptions and completes the argument scan.

Acceptance: Persist graph projection work/status independently, expose lag, and retry idempotently without repeating successful inference. Inject a graph failure and prove recovery after the graph becomes available.

**EX-04 — Refresh enrichment when content or analyzer changes (P1, reproduced).**

Evidence: `src/ingestion/enrich.py:101` selects only IDs with no enrichment.

Acceptance: Record source revision and analyzer version/configuration, regenerate stale sentiment/topics, and preserve prior committed outputs on failure. Test a correction that reverses sentiment and a configuration-only change.

**EX-05 — Collect the missing independent human evaluation set (P2, evaluation).**

Evidence: `data/argument_mining/human_eval/status.json` explicitly says `not_collected` and requests two independent annotations over 500–1000 real ingested sentences.

Acceptance: Complete the existing annotation workflow with source/domain/language coverage, agreement and adjudication records, and a frozen test split. Keep human labels distinguishable from model-generated labels; do not relabel the existing benchmark as this missing dataset.

**EX-06 — Improve and calibrate stance classification (P2, evaluation).**

Evidence: `docs/subsystems/argument-mining-benchmarks.md` reports stance macro F1 0.3288 and critical-class recall 0.0970 on the existing 1,076-example held-out split.

Acceptance: Diagnose class confusions, compare candidate models/configurations on fixed splits, and measure per-class precision/recall plus calibration and abstention. Define absolute task-readiness criteria separately from regression gates. Validate the selected approach against EX-05; the existing result alone does not establish live-domain accuracy.

**EX-07 — Improve and calibrate frame classification (P2, evaluation).**

Evidence: The same benchmark reports frame macro F1 0.4193; claim detection is materially stronger at F1 0.9197, so model work should be targeted.

Acceptance: Evaluate per-frame and multi-label errors, thresholds, and an uncertain/unsupported outcome on frozen labels. Report source/domain breakdowns and model costs. Validate against EX-05; do not bundle replacement of the stronger claim detector into this task.

### Entity resolution

**ER-01 — Abstain on ambiguous entity aliases (P1, reproduced).**

Evidence: `src/knowledge_graph/foundation/resolution.py:53` and `:170`; surname-only compatibility plus first-match selection.

Acceptance: Generate candidates, use available identifiers/context, and retain ambiguity when multiple people fit. Reverse insertion order in the John/Jane Smith example without changing the outcome. Preserve source mentions and reversible resolution history. Evaluate probabilistic record linkage only when sufficiently rich structured fields are available.

### Indexing and embeddings

**IX-01 — Use semantic model embeddings in maintenance projections (P1, review).**

Evidence: `src/kb/derived_revisions.py:188` hashes text into 12-dimensional vectors; maintenance uses these projections.

Acceptance: Inject the existing supported embedding provider, record model/dimension/tokenization identity, and guard against mixed vector spaces. Ordinary maintenance outputs must support measured semantic retrieval. Keep content-hash vectors explicitly limited to deterministic test mode, with no silent semantic fallback.

**IX-02 — Rebuild direct embeddings after revision or model changes (P1, reproduced).**

Evidence: `src/ingestion/embed.py:54`; `src/ingestion/embedding_store.py` keys current vectors by document ID.

Acceptance: Track source revision and effective embedding configuration. Regenerate stale vectors and publish them atomically; trigger dependent membership/retrieval refresh. Cover content-only and model-only changes while avoiding mixed dimensions.

**IX-03 — Embed complete documents with token-aware chunks (P2, review).**

Evidence: `src/ingestion/embed.py:27` defaults to the first 4,000 characters and one vector per document; richer chunking exists separately under `services/rag`.

Acceptance: Reuse a corrected chunker, respect the selected model's token budget, preserve document/revision/offset references, and retrieve evidence beyond the first 4,000 characters. Measure recall and duplication effects. Depends on DP-02 and IX-02.

**IX-04 — Bound embedding batches and validate provider output (P1, review).**

Evidence: `src/ingestion/embed.py:64` fetches all pending rows, embeds the full list, and zips IDs with returned vectors while returning the input count.

Acceptance: Bound document/token batches and memory, validate vector count/dimension/finite values before publication, and report persisted counts. Test a provider returning too few vectors, malformed vectors, and a failed middle batch with resumable progress.

### Retrieval and answer evaluation

**QA-01 — Enforce one end-to-end query deadline (P1, reproduced).**

Evidence: `src/kb/unified_query.py:1428`; memory adapters execute before the pool, retries do not recalculate the remaining total budget, and executor context exit waits for running tasks.

Acceptance: Carry one absolute deadline through memory lookup, queueing, retries, and source I/O. Ensure supported adapters cooperate with cancellation and isolate worker/connection lifetimes safely. Test a slow adapter and bounded return latency with a documented allowance; never advertise a hard bound for uncancellable work without isolation.

**QA-02 — Benchmark the existing hybrid retrieval and reranking paths (P2, evaluation).**

Evidence: `services/rag/retriever.py` already combines vector/lexical results; defaults cap each candidate pool at 20, and some adapter failures collapse into empty results.

Acceptance: Use fixed, human-judged domain queries to compare lexical, semantic, fusion, and reranked runs with recall, nDCG, MRR, latency, and cost. Include requested result counts above 20. Confirm which API/MCP consumer uses each path, and expose partial-source failures distinctly from zero matches. Use `ir-measures` or Sentence Transformers' evaluators where appropriate.

**QA-03 — Evaluate whether answers are supported by their cited evidence (P2, evaluation).**

Evidence: `src/kb/answer_eval.py:32` checks structural/citation/refusal rules; these do not establish factual support. The answer engine's deterministic extraction is a deliberate design choice.

Acceptance: Add labeled cases for unsupported citations, contradictions, corrections, incomplete coverage, and appropriate refusal. Score evidence relevance and entailment separately from schema compliance, with a human audit subset. Cover long evidence spans: `src/kb/nli.py:100` truncates paired inputs at 512 tokens. Preserve the existing fail-closed model-unavailable behavior.

### Watches and delivery

**SU-01 — Include coverage in subscription snapshot identity (P1, reproduced).**

Evidence: `src/kb/subscriptions.py:216` hashes normalized items alone.

Acceptance: Define immutable replay identity over items and meaningful coverage fields. Changed coverage at the same watermark must produce an explicit conflict or a documented new evaluation revision; it must not silently retain a complete snapshot.

**SU-02 — Distinguish query-result disappearance from source withdrawal (P2, review).**

Evidence: `src/kb/subscriptions.py:224` emits `removed` for every previous key absent now, including incomplete coverage. Existing tests intentionally allow `removed` alongside `coverage-degraded`.

Acceptance: Specify event semantics for top-k displacement, filter changes, unavailable sources, and confirmed deletion/retraction. Include reason and coverage in consumer-visible events so a missing search result is not presented as withdrawn evidence. Preserve documented backward compatibility.

**SU-03 — Implement the subscription outbox delivery lifecycle (P2, review).**

Evidence: `src/kb/subscriptions.py:236` writes pending outbox rows; `pending_deliveries` and the MCP tool list them. Searches of `src`, `tools`, and `scripts` found no claim/ack/retry worker for this table. Poll subscriptions already function.

Acceptance: Add lease/claim, acknowledgement, retry/backoff, terminal failure, and redrive operations with owner-scoped access and idempotency. Test competing workers, receiver errors, and crash-after-send. Use a fake destination in tests; support an explicitly configured real transport separately.

### Research packages and retention

**PK-01 — Recompute research-package dependency closure during verification (P1, reproduced).**

Evidence: `src/kb/research_packages.py:518` trusts declared omissions when detecting missing members.

Acceptance: Independently validate roots, member identifiers, dependency edges, permitted external/redacted references, and closure digest/completeness. Reject a self-consistently rehashed package missing an undeclared required member. Preserve explicit partial-package policy and distinguish structural validity from byte integrity.

**PK-02 — Expose trusted-signature policy at package import (P2, review).**

Evidence: `src/kb/research_packages.py:634` calls `verify(package)` without the existing `public_keys`/`require_signature` controls.

Acceptance: Let namespace/import policy require a trusted signer, including key rotation/revocation behavior. Report byte integrity and publisher trust separately. Test unsigned permitted imports, required signatures, unknown keys, and invalid signatures; unsigned local packages need not become universally forbidden.

**RT-01 — Write and verify archive bytes through a storage adapter (P1, reproduced).**

Evidence: `src/kb/knowledge_retention.py:408`; `archive_io` probe.

Acceptance: Write checkpoint content/manifest to a configured backend, verify persisted bytes, and mark archived only after durable success. Derive availability/partial outcomes from actual I/O. Test interrupted writes and checksum mismatch. Evaluate `fsspec` for the backend interface; Noesis still owns durability and publication semantics.

**RT-02 — Restore from archive into a fresh database (P1, reproduced).**

Evidence: `src/kb/knowledge_retention.py:464` verifies the checkpoint already in the local database and changes status.

Acceptance: Retrieve archived bytes, validate identity/schema, restore records and tombstones atomically, and support a fresh database without the original local checkpoint. Test corruption, interrupted restore, and a successful restore after the original database is removed. Depends on RT-01.

**RT-03 — Reject or explicitly paginate truncated checkpoints (P1, reproduced).**

Evidence: `src/kb/knowledge_retention.py:328` slices records to the limit and marks the checkpoint complete unless cancelled.

Acceptance: Overflow must fail explicitly or produce a partial checkpoint with a resumable continuation and honest generation coverage. Bound consumption of iterators as well as stored rows. Test exact-limit, limit-plus-one, and multi-page reconstruction without omissions.

## Libraries worth evaluating

These recommendations are scoped integrations, not proposed framework replacements. Official documentation checked 2026-09-05:

| Tool | Fit and limitations | Related work |
|---|---|---|
| [ir-measures](https://ir-measur.es/en/latest/) | A common API for established retrieval metrics over qrels and result runs. Requires relevant judgments; installing it does not create ground truth. | QA-02 |
| [Sentence Transformers evaluation](https://sbert.net/docs/package_reference/sentence_transformer/evaluation.html) | Existing ecosystem evaluators for embedding retrieval and related tasks; useful alongside Noesis's current embedding integrations. | IX-01, QA-02 |
| [Splink](https://moj-analytical-services.github.io/splink/) | Candidate for probabilistic linkage of multi-field structured entity records. Its documentation explicitly excludes single-column bags of words as a suitable use case; it is not a drop-in fix for surname-only text mentions. | ER-01 |
| [fsspec](https://filesystem-spec.readthedocs.io/en/latest/) | Common local/remote bytes-storage interface with optional backend dependencies. Does not by itself implement a correct backup/restore protocol. | RT-01, RT-02 |

## Recommended sequence and limits

1. Prevent data loss and misleading success: EX-01, WF-02, RT-03, PK-01, DP-01/02.
2. Make revisions propagate: EX-02/03/04, IX-02/04, DP-03, SU-01.
3. Connect actual extraction and semantic embeddings: WF-01, IX-01, then WF-03.
4. Establish quality measurements before choosing model replacements: EX-05/06/07, QA-02/03; fix ER-01 and QA-01 independently.
5. Complete optional delivery, trusted import, and real archive/restore: SU-02/03, PK-02, RT-01/02. Improve long-document coverage through IX-03.

There are **29 granular candidates** above. They separate reproducible defects, missing integration, and evaluation work. None requires adopting a new orchestration framework or replacing the existing knowledge-domain abstraction.

Further validation should include cross-surface namespace/access-policy conformance, retraction/deletion propagation under retention holds, migration compatibility, and sustained workload/resource limits. This review did not establish an access-control vulnerability or measure production throughput. The existing source-collection issues remain the place for scraping, scholarly APIs, and parser evaluations.

## Further-validation issues

The final review recommendations are tracked separately from reproduced defects:

- **CV-01 — Verify namespace and access-policy conformance across query, snapshot, and export surfaces**: [#1439](https://github.com/Ikey168/Noesis/issues/1439).
- **CV-02 — Verify retraction and deletion propagation under retention holds and snapshot pins**: [#1455](https://github.com/Ikey168/Noesis/issues/1455).
- **CV-03 — Test workflow migrations and compatibility using prior persisted database fixtures**: [#1440](https://github.com/Ikey168/Noesis/issues/1440).
- **CV-04 — Measure sustained workflow throughput and resource limits under recovery and cancellation**: [#1456](https://github.com/Ikey168/Noesis/issues/1456).
