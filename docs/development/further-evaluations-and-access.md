# Follow-up evaluations, human review and provider access

Status: **deferred follow-up work**, transferred from the implementation issue queue at the user's request on 2026-09-09. This document owns the outstanding work from the 23 issues listed below. GitHub closure uses **not planned** because the remaining acceptance work is deferred, not passed. Closing tracking parents retires their queue entries; it does not certify every original acceptance criterion.

The implementations, adapters and contract tests are recorded in [the implementation inventory](optional-runtime-implementation-status.json). The latest regression run passed 1,598 tests with 13 skips. Code remains local and uncommitted; closure does not mean it has been merged or deployed. Existing production defaults and release gates remain authoritative.

[Execution results and limitations](backlog-execution-2026-09-09.md) preserve completed native comparisons. [The original issue snapshot](workflow-review-evidence/deferred-issue-snapshot-2026-09-09.json) preserves every transferred issue body and URL. [The closure receipt](workflow-review-evidence/deferred-issue-closures-2026-09-09.json) records actual GitHub state after the transfer. Do not treat authored regression fixtures, model-generated labels, successful imports or schema-valid outputs as independent human quality evidence.

## Resume sequence and responsibilities

Owners are roles to assign when this work is scheduled; no person or budget is currently assigned.

1. A research lead defines domain/language coverage, annotation instructions and task-readiness thresholds before inspecting test results. An operator identifies licensed source material and, where necessary, provider accounts and spending ceilings.
2. Two independent annotators label the same selected material. A separate adjudication role resolves disagreements and records decisions. A release owner freezes an approved dataset with related-document groups kept in a single split.
3. An evaluation engineer runs baseline and candidate on the same held-out inputs, hardware and limits. Validation data selects thresholds; frozen test data measures quality. Failed and unavailable cases remain explicit.
4. Reviewers measure actual effort and error reduction. The research lead records a narrow adopt/defer decision per task and language. Any discovered code defect becomes a concrete implementation issue; deferred studies can be reopened with their dataset/account prerequisites attached.

## Independent human annotations and review

### Collection and release — #1420, #1521

`data/argument_mining/human_eval/status.json` remains `not_collected`. Collect two independent human annotations over **500–1000 real ingested sentences**, with source, domain and language coverage. Retain immutable document/revision IDs, exact original character spans, task schema/version, distinct reviewer identities, independent votes, disagreement and adjudication records. Keep model preannotations distinguishable and prevent them from substituting for independent votes. Keep related documents in one split and freeze validation/test membership before model selection.

Use `scripts/human_annotation.py` and the existing review/dataset stores. Run its `finalize` workflow only after the required human work is complete. For Label Studio, select the actual deployment and edition, verify the required capabilities, and perform a bounded export/import pilot through `src/kb/annotation_exchange.py`. Measure import fidelity, stale-revision/schema rejection, Unicode span preservation, annotator time and agreement. Noesis access scopes, adjudication and explicit release approval remain authoritative.

Deliverables: collection manifest and source rights; annotation handbook; assignments and independent votes; agreement by task/language/domain; adjudication ledger; frozen split manifest and release approval; pilot timing and exchange-fidelity report. Store sensitive source bodies and reviewer identities in the authorized dataset store, not public benchmark receipts.

### Review utility — #1458, #1492

For reviewed correction exports, measure actual review time and held-out error reduction before versus after using the released corrections. Preserve provenance, disputed labels and split boundaries. Exporting a dataset alone is not measured training benefit and does not authorize automatic retraining.

For entity resolution, collect adjudicated multilingual names, affiliations, addresses, absent identifiers and similar-name cases, including Berlin examples. Compare the existing resolver with Splink on frozen groups; measure false merges, match recall, calibration, latency and reviewer effort. Select thresholds on validation data. Preserve explicit-ID precedence and the review queue; predictions must not directly merge entities.

## Further model and workflow evaluations

| Original issues | Held-out material still needed | Comparison and deliverable |
| --- | --- | --- |
| #1443, #1444 | Released #1420 stance/frame labels, language/domain metadata, explicit stance targets and frame definitions | Separate task-specific per-class precision/recall/F1, frame multi-label errors, confusion matrices, calibration, abstention and model cost. Define absolute readiness separately from regression gates; preserve the existing claim detector. |
| #1427 | Independently judged answers, citations and evidence, including unsupported citations, contradiction, correction, incomplete coverage, appropriate refusal and long evidence | Human-audited relevance and textual support separately from schema compliance; complete-context coverage across the 512-token boundary; explicit fail-closed unavailable/partial behavior. |
| #1507 | Literal-quotation challenge pairs and language-identified German stance/frame labels | Extend the recorded published DE/EN NLI comparison; reported speech is not literal quotation. Evaluate stance/frame separately with frozen targets/templates; retain validation-only calibration and per-language results. |
| #1499 | Frozen Noesis questions, retrieved records, answers and citations with independent human relevance/support judgments | Compare selected Ragas diagnostics against current metrics and human judgments. Record disagreement, model/prompt versions, runtime and cost; label synthetic and judge-generated assessments. ID/text overlap does not establish entailment. |
| #1508 | Independently transcribed German/English administrative scans, including umlauts, tables, forms, multiple columns and degraded pages | Run pinned LightOnOCR against the same existing PDF/OCR and PaddleOCR inputs. Report CER/WER, reading order, table fidelity, omissions/inventions, p50/p95 page latency, throughput and peak RAM/VRAM. Retain page/source identity; unavailable text coordinates remain unavailable. |
| #1509 | Independent German/English sentence boundaries and downstream retrieval/claim-span judgments, including abbreviations, legal references, quotations, tables and OCR line breaks | Compare SaT, the current sentencizer and correctly configured German spaCy. Report boundary precision/recall/F1, chunk token limits, downstream quality, CPU time and memory with exact original offsets. |
| #1510 | Human document and mixed-language span labels for short titles, DE/EN prose, English abstracts in German documents, quotations and noisy OCR | Compare Lingua with langdetect at document and segment level; measure precision/recall, confidence thresholds, abstention, offset fidelity, latency and RAM. Freeze the language set and select thresholds on validation only. |
| #1514 | Manually aligned German/English audio with source transcripts, names, numbers, overlap and noise | Run pinned WhisperX over existing transcripts. Measure word-boundary error, alignment coverage and citation seek accuracy, elapsed time and memory. Preserve original segments, speakers and unaligned words; improved alignment is not improved transcription. |

Every run should retain the dataset release/hash, input selection rule, source revisions, human/model label origin, baseline/candidate model and dependency pins, schema/prompts, thresholds, hardware, resource limits and failure records. Report metrics per language/domain and sample counts, not just pooled averages. Keep native output observations separate from independently verified labels. Use the bounded optional runtime and existing replay contracts described in [the operator guide](../guides/optional-runtime-integrations.md).

## Provider credentials and live checks

An account operator must supply appropriately licensed working credentials through the existing secret configuration. GitHub issue-write credentials do not authenticate to these providers. Never place secret values in this document, request fixtures, source URLs or committed logs. The following are existing configuration names, not credentials:

| Provider / original issue | Existing secret variable | Remaining live evidence |
| --- | --- | --- |
| Guardian — #1391; parent #1348 | `NOESIS_GUARDIAN_API_KEY` | Native domain source-pack collection with bounded pagination/query/date windows; retained response/body provenance, incremental replay and identity deduplication. Record actual access/reuse conditions and representative EU/German/Berlin coverage. The earlier public `test` key returned authentication failure. |
| OpenAlex content — #1471 | `NOESIS_OPENALEX_API_KEY` | Authorized native per-format PDF/TEI acquisition, absent/inaccessible representations, content identity and restart-safe usage reservations. Reconcile measured usage with account billing/coverage; a configured cost ceiling is not an invoice. |
| OpenCorporates — #1483 | `NOESIS_OPENCORPORATES_API_KEY` | Current licensed company lookup/search for German/EU and available Berlin-registered examples; retain registry jurisdiction, registry number, provenance, historical records, staleness and missing fields. Distinguish headquarters from registration and similarly named companies from matches. |
| Exa — #1488 | `NOESIS_EXA_API_KEY` | Common DE/EN EU/German/Berlin query set versus existing discovery and Tavily, with independently judged relevance, unique useful sources, latency and actual cost. Acquire selected original URLs separately from provider summaries. |
| Tavily — #1489 | `NOESIS_TAVILY_API_KEY` | Same frozen queries and judgments as Exa/current discovery; measured coverage, usefulness, latency and cost, followed by normal original-source acquisition. |

Before a live run, record the account reference, permitted use/retention, selected operation, query/document set and request/byte/spend ceilings using the existing runtime budget. Confirm current account entitlements and pricing at execution time; this document makes no claim about current prices or newly granted permissions. Supply the existing `network_approved` configuration and trusted scopes required by the operator interface. Preserve sanitized receipts, actual usage, failed requests and replay evidence. Credential availability alone is not proof that a live comparison passed.

If a future Ragas configuration uses a hosted judge, separately record its model/provider account, data-transfer authorization and spend ceiling; the existing local metrics do not imply that such an account is configured.

## Issue-by-issue transfer register

Each item below preserves its remaining acceptance and points to its existing implementation and tests. Tracking parents have no separate runtime feature to implement; their outstanding child evaluations move here with them.

### [1348: [Tracking] Connect the existing Guardian API collector to domain source packs and incremental ingestion](https://github.com/Ikey168/Noesis/issues/1348)

Tracking parent, not a standalone runtime task.

Deferred work: All child issue acceptance and dependent human/live evidence must be reviewed before closure.

### [1391: Expose Guardian API collection through domain source packs](https://github.com/Ikey168/Noesis/issues/1391)

Existing maintained Guardian native runtime/source-pack integration retained and regression-tested.

Deferred work: Authorized credentialed live/access-license check.

Implementation: [src/ingestion/guardian_api.py](../../src/ingestion/guardian_api.py), [src/ingestion/source_pack_runtime.py](../../src/ingestion/source_pack_runtime.py), [config/source_packs/guardian.json](../../config/source_packs/guardian.json).

Existing checks: [tests/unit/ingestion/test_guardian_api.py](../../tests/unit/ingestion/test_guardian_api.py).

### [1399: [Tracking] Revision-safe extraction, enrichment, and model evaluation](https://github.com/Ikey168/Noesis/issues/1399)

Tracking parent, not a standalone runtime task.

Deferred work: All child issue acceptance and dependent human/live evidence must be reviewed before closure.

### [1401: [Tracking] Query deadlines, retrieval relevance, and evidence-supported answers](https://github.com/Ikey168/Noesis/issues/1401)

Tracking parent, not a standalone runtime task.

Deferred work: All child issue acceptance and dependent human/live evidence must be reviewed before closure.

### [1406: [Tracking] Unified evidence review inbox](https://github.com/Ikey168/Noesis/issues/1406)

Tracking parent, not a standalone runtime task.

Deferred work: All child issue acceptance and dependent human/live evidence must be reviewed before closure.

### [1420: Collect the missing independent human evaluation set](https://github.com/Ikey168/Noesis/issues/1420)

Independent annotation workflow and native Label Studio exchange; not a claim of human collection.

Deferred work: 500–1000 real sentences independently annotated by two humans, agreement/adjudication and frozen test release.

Implementation: [src/kb/annotation_exchange.py](../../src/kb/annotation_exchange.py), [src/kb/review_inbox.py](../../src/kb/review_inbox.py), [scripts/human_annotation.py](../../scripts/human_annotation.py).

Existing checks: [tests/unit/kb/test_annotation_exchange.py](../../tests/unit/kb/test_annotation_exchange.py), [tests/unit/kb/test_review_inbox.py](../../tests/unit/kb/test_review_inbox.py).

### [1427: Evaluate whether answers are supported by their cited evidence](https://github.com/Ikey168/Noesis/issues/1427)

Evidence-support evaluation and complete-context NLI with explicit unavailable/partial states.

Deferred work: Human audit subset and independent relevance/support evaluation.

Implementation: [src/kb/nli.py](../../src/kb/nli.py), [src/kb/nli_evidence.py](../../src/kb/nli_evidence.py), [src/evaluation/mining_runtime.py](../../src/evaluation/mining_runtime.py), [src/evaluation/benchmark_runtime.py](../../src/evaluation/benchmark_runtime.py).

Existing checks: [tests/unit/kb/test_nli_evidence.py](../../tests/unit/kb/test_nli_evidence.py), [tests/unit/evaluation/test_mining_runtime.py](../../tests/unit/evaluation/test_mining_runtime.py).

### [1443: Improve and calibrate stance classification](https://github.com/Ikey168/Noesis/issues/1443)

Native multilingual stance/frame inference, validation-only calibration and frozen policy application.

Deferred work: Validate task readiness against the missing independent #1420 set; do not infer live accuracy from the existing benchmark.

Implementation: [src/evaluation/mining_runtime.py](../../src/evaluation/mining_runtime.py), [src/argument_mining/models.py](../../src/argument_mining/models.py), [src/argument_mining/frames.py](../../src/argument_mining/frames.py), [src/kb/optional_runtime.py](../../src/kb/optional_runtime.py).

Existing checks: [tests/unit/evaluation/test_mining_runtime.py](../../tests/unit/evaluation/test_mining_runtime.py).

### [1444: Improve and calibrate frame classification](https://github.com/Ikey168/Noesis/issues/1444)

Native multilingual stance/frame inference, validation-only calibration and frozen policy application.

Deferred work: Validate task readiness against the missing independent #1420 set; do not infer live accuracy from the existing benchmark.

Implementation: [src/evaluation/mining_runtime.py](../../src/evaluation/mining_runtime.py), [src/argument_mining/models.py](../../src/argument_mining/models.py), [src/argument_mining/frames.py](../../src/argument_mining/frames.py), [src/kb/optional_runtime.py](../../src/kb/optional_runtime.py).

Existing checks: [tests/unit/evaluation/test_mining_runtime.py](../../tests/unit/evaluation/test_mining_runtime.py).

### [1458: Export reviewed corrections as evaluation/training candidates](https://github.com/Ikey168/Noesis/issues/1458)

Existing reviewed dataset releases extended with native annotation details, disagreement and source-revision safeguards.

Deferred work: Actual reviewer effort and validated held-out error reduction, separate from self-reported fixture effort.

Implementation: [src/kb/review_datasets.py](../../src/kb/review_datasets.py), [src/kb/annotation_exchange.py](../../src/kb/annotation_exchange.py).

Existing checks: [tests/unit/kb/test_review_datasets.py](../../tests/unit/kb/test_review_datasets.py), [tests/unit/kb/test_annotation_exchange.py](../../tests/unit/kb/test_annotation_exchange.py).

### [1469: [Tracking] API, library and MCP integration roadmap for EU/Germany/Berlin](https://github.com/Ikey168/Noesis/issues/1469)

Tracking parent, not a standalone runtime task.

Deferred work: All child issue acceptance and dependent human/live evidence must be reviewed before closure.

### [1471: Acquire OpenAlex PDFs and TEI XML through the existing paper pipeline](https://github.com/Ikey168/Noesis/issues/1471)

Native per-format OpenAlex content acquisition, TEI/PDF storage and restart-safe reservations.

Deferred work: Authorized metered provider execution and invoice/coverage validation; a source license field is not independent permission certification.

Implementation: [src/ingestion/hosted_acquisition.py](../../src/ingestion/hosted_acquisition.py).

Existing checks: [tests/unit/ingestion/test_hosted_acquisition.py](../../tests/unit/ingestion/test_hosted_acquisition.py).

### [1483: Add EU/German company enrichment through OpenCorporates](https://github.com/Ikey168/Noesis/issues/1483)

Native provider-specific bounded acquisition or explicit validated export import through document/snapshot/revision storage.

Deferred work: Current German/EU/Berlin OpenCorporates native company lookup/search under an appropriately licensed account; validate historical records, similar names and coverage.

Implementation: [src/ingestion/regional_providers.py](../../src/ingestion/regional_providers.py), [src/ingestion/regional_workflow.py](../../src/ingestion/regional_workflow.py), [src/ingestion/provider_execution.py](../../src/ingestion/provider_execution.py), [src/noesis_cli/optional.py](../../src/noesis_cli/optional.py).

Existing checks: [tests/unit/ingestion/test_regional_providers.py](../../tests/unit/ingestion/test_regional_providers.py), [tests/unit/ingestion/test_regional_workflow.py](../../tests/unit/ingestion/test_regional_workflow.py), [tests/unit/ingestion/test_provider_execution.py](../../tests/unit/ingestion/test_provider_execution.py), [tests/unit/noesis_cli/test_optional.py](../../tests/unit/noesis_cli/test_optional.py).

### [1488: Benchmark Exa discovery against existing search acquisition](https://github.com/Ikey168/Noesis/issues/1488)

Native Exa/Tavily/Jina request formats, durable budgets and original-source acquisition/lineage.

Deferred work: Authorized common-query/page live comparison, judged relevance/fidelity and actual provider cost.

Implementation: [src/ingestion/hosted_acquisition.py](../../src/ingestion/hosted_acquisition.py), [src/ingestion/discovery_acquisition.py](../../src/ingestion/discovery_acquisition.py).

Existing checks: [tests/unit/ingestion/test_hosted_acquisition.py](../../tests/unit/ingestion/test_hosted_acquisition.py), [tests/unit/ingestion/test_discovery_acquisition.py](../../tests/unit/ingestion/test_discovery_acquisition.py).

### [1489: Benchmark Tavily discovery against existing search acquisition](https://github.com/Ikey168/Noesis/issues/1489)

Native Exa/Tavily/Jina request formats, durable budgets and original-source acquisition/lineage.

Deferred work: Authorized common-query/page live comparison, judged relevance/fidelity and actual provider cost.

Implementation: [src/ingestion/hosted_acquisition.py](../../src/ingestion/hosted_acquisition.py), [src/ingestion/discovery_acquisition.py](../../src/ingestion/discovery_acquisition.py).

Existing checks: [tests/unit/ingestion/test_hosted_acquisition.py](../../tests/unit/ingestion/test_hosted_acquisition.py), [tests/unit/ingestion/test_discovery_acquisition.py](../../tests/unit/ingestion/test_discovery_acquisition.py).

### [1492: Benchmark Splink for reviewable multi-attribute entity resolution](https://github.com/Ikey168/Noesis/issues/1492)

Actual native Splink/RapidFuzz candidate scoring with identifier/type precedence and explicit existing review-inbox routing.

Deferred work: Larger adjudicated held-out entity corpus, calibrated operational thresholds and reviewer utility.

Implementation: [src/evaluation/entity_backends.py](../../src/evaluation/entity_backends.py), [src/kb/optional_runtime.py](../../src/kb/optional_runtime.py), [src/kb/entity_analysis_inputs.py](../../src/kb/entity_analysis_inputs.py).

Existing checks: [tests/unit/evaluation/test_entity_backends.py](../../tests/unit/evaluation/test_entity_backends.py), [tests/unit/kb/test_optional_runtime.py](../../tests/unit/kb/test_optional_runtime.py), [tests/unit/kb/test_entity_analysis_binding.py](../../tests/unit/kb/test_entity_analysis_binding.py).

### [1499: Evaluate Ragas alongside existing retrieval and answer-support metrics](https://github.com/Ikey168/Noesis/issues/1499)

Native selected Ragas ID/text metrics over frozen question/retrieval/answer records, no implicit hosted judge.

Deferred work: Independent human metric comparison and disagreement review. ID/text overlap is not entailment.

Implementation: [src/evaluation/ragas_metrics.py](../../src/evaluation/ragas_metrics.py), [src/kb/optional_runtime.py](../../src/kb/optional_runtime.py).

Existing checks: [tests/unit/evaluation/test_ragas_metrics.py](../../tests/unit/evaluation/test_ragas_metrics.py), [tests/unit/evaluation/test_execution_outcomes.py](../../tests/unit/evaluation/test_execution_outcomes.py).

### [1507: Evaluate multilingual mDeBERTa NLI for German evidence support and contradiction](https://github.com/Ikey168/Noesis/issues/1507)

Pinned native mDeBERTa inference with verified label/pair order, complete evidence windows and separate stance/frame tasks.

Deferred work: Literal quotation challenge coverage and language-identified German stance/frame comparison remain outstanding.

Implementation: [src/evaluation/mining_runtime.py](../../src/evaluation/mining_runtime.py), [src/kb/nli.py](../../src/kb/nli.py), [src/kb/optional_runtime.py](../../src/kb/optional_runtime.py).

Existing checks: [tests/unit/evaluation/test_mining_runtime.py](../../tests/unit/evaluation/test_mining_runtime.py), [tests/unit/kb/test_nli_evidence.py](../../tests/unit/kb/test_nli_evidence.py).

### [1508: Benchmark LightOnOCR-2-1B on scanned German administrative documents](https://github.com/Ikey168/Noesis/issues/1508)

Native bounded PaddleOCR/LightOnOCR/WhisperX jobs bound to captured binaries/transcripts and exact source identity.

Deferred work: Execute pinned LightOnOCR on independently labeled German administrative scans against the same PDF/OCR and PaddleOCR baselines; report quality and resource comparison.

Implementation: [src/evaluation/media_backends.py](../../src/evaluation/media_backends.py), [src/kb/optional_runtime.py](../../src/kb/optional_runtime.py), [src/evaluation/benchmark_runtime.py](../../src/evaluation/benchmark_runtime.py).

Existing checks: [tests/unit/evaluation/test_media_backends.py](../../tests/unit/evaluation/test_media_backends.py), [tests/unit/kb/test_optional_runtime.py](../../tests/unit/kb/test_optional_runtime.py), [tests/unit/evaluation/test_execution_outcomes.py](../../tests/unit/evaluation/test_execution_outcomes.py).

### [1509: Benchmark wtpsplit/SaT for German and multilingual sentence segmentation](https://github.com/Ikey168/Noesis/issues/1509)

Native SaT segmenter preserving exact source offsets, existing chunker selection and executable boundary evaluation.

Deferred work: Representative independent sentence boundaries and downstream retrieval/claim-span comparison with properly configured German baselines.

Implementation: [src/evaluation/model_backends.py](../../src/evaluation/model_backends.py), [services/rag/chunking.py](../../services/rag/chunking.py), [src/evaluation/benchmark_runtime.py](../../src/evaluation/benchmark_runtime.py).

Existing checks: [tests/unit/evaluation/test_model_backends.py](../../tests/unit/evaluation/test_model_backends.py).

### [1510: Benchmark Lingua for short and mixed-language research evidence](https://github.com/Ikey168/Noesis/issues/1510)

Native optional Lingua normalized-source language segments with explicit uncertainty and exact offsets.

Deferred work: Independent language/span labels; authored-fixture execution does not establish production multilingual accuracy.

Implementation: [services/rag/language_detection.py](../../services/rag/language_detection.py), [services/rag/normalization.py](../../services/rag/normalization.py), [scripts/evaluate_lingua.py](../../scripts/evaluate_lingua.py).

Existing checks: [tests/unit/services/rag/test_lingua_normalization.py](../../tests/unit/services/rag/test_lingua_normalization.py).

### [1514: Evaluate WhisperX forced alignment for word-level evidence timestamps](https://github.com/Ikey168/Noesis/issues/1514)

Native bounded PaddleOCR/LightOnOCR/WhisperX jobs bound to captured binaries/transcripts and exact source identity.

Deferred work: Execute pinned WhisperX on manually aligned German/English audio including names, numbers, overlap and noise; measure word-boundary error, coverage and citation seek accuracy.

Implementation: [src/evaluation/media_backends.py](../../src/evaluation/media_backends.py), [src/kb/optional_runtime.py](../../src/kb/optional_runtime.py), [src/evaluation/benchmark_runtime.py](../../src/evaluation/benchmark_runtime.py).

Existing checks: [tests/unit/evaluation/test_media_backends.py](../../tests/unit/evaluation/test_media_backends.py), [tests/unit/kb/test_optional_runtime.py](../../tests/unit/kb/test_optional_runtime.py), [tests/unit/evaluation/test_execution_outcomes.py](../../tests/unit/evaluation/test_execution_outcomes.py).

### [1521: Evaluate Label Studio exchange for independent evidence annotation](https://github.com/Ikey168/Noesis/issues/1521)

Native Label Studio JSON export/import integrated with existing assignments, votes, dispute handling and dataset releases.

Deferred work: Selected Label Studio deployment/edition, genuinely independent pilot, measured annotator time/agreement and actual human collection.

Implementation: [src/kb/annotation_exchange.py](../../src/kb/annotation_exchange.py), [src/kb/review_inbox.py](../../src/kb/review_inbox.py), [src/kb/review_datasets.py](../../src/kb/review_datasets.py).

Existing checks: [tests/unit/kb/test_annotation_exchange.py](../../tests/unit/kb/test_annotation_exchange.py).
