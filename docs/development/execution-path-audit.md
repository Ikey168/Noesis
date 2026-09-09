# Noesis execution-path audit — 7 September 2026

**Status: the reproduced dispatch/publication defects and the additional scoped gaps below are repaired. The entire remaining backlog is not certified implemented or accepted.** The last refreshed inventory was 50 leaf tasks and eight tracking parents; this pass did not re-query GitHub or close issues. Changes remain local on `feat/workflow-review-implementation`, with prior uncommitted work preserved.

The previous handoff's file mapping and 1,421 passing tests did not establish correct invocation paths. This pass added failing regressions at the actual dispatcher, subprocess, artifact store, review and CLI boundaries, applied fixes, then ran the current scoped regressions. The implementation matrix now labels individually rechecked paths separately from issues not reaudited.

## Repaired execution paths

| Area | Actual change | Verification boundary |
|---|---|---|
| WhisperX (#1514) | The real media dispatcher reads digest-verified captured bytes (at most 50,000,000), calls the pipe-only decoder with the selected duration ceiling, and only then constructs the aligner. No legacy `whisperx.audio.load_audio(path)` call remains. | `test_execution_outcomes.py` exercises `media_job`, checks exact bytes, rejects a changed file and an overlong decode before model loading. Existing decoder tests remain. Actual alignment quality is not claimed. |
| Shared outcomes / Ragas (#1499) | A registered operation-specific completion contract preserves unavailable/partial/failed/cancelled states through worker, supervisor, store, telemetry, benchmark and CLI. Worker return alone no longer implies a publishable enrichment. | Missing Ragas is forced in an actual disposable subprocess, without uninstalling it; the store persists diagnostics, publishes zero artifacts and replays them. The rebuilt wheel repeats this outside the checkout, with exit 3 and zero artifact rows. |
| Legacy replay | Optional-runtime inspection/replay downgrades old nested false successes, suppresses their returned artifact reference and preserves raw saved history. Old linkage runs without captured field bindings cannot enter review. | Raw saved JSON remains unchanged in the regression. This is NOT a global migration/invalidation of historical graph rows or downstream artifacts. |
| PDF and OCR | PDF dispatch validates the digest on the bytes actually parsed. Docling receives those bytes as a `DocumentStream`, not a reopened path. Native success/partial_success/failure/skipped map explicitly to task states. Truncated OCR is partial, not a successful artifact. | Real captured PDF validation plus SDK-shaped Docling dispatch tests in `test_pdf_runtime_contracts.py`; parser status and byte-replacement races covered. Current native Docling inference was not run. |
| BGE retrieval bounds and timing (#1505) | The supported 129–200-document benchmark range now batches into calls of at most 128. Timing v2 separates loading, corpus encoding, query encoding and search; warm query time includes query encoding plus search, not index sizing/teardown. | Tests exercise both 129- and 200-document inputs with the actual adapter and an injected native-shaped engine. A real cached MiniLM worker records the same timing contract. Native BGE quality was not measured. |
| Presidio (#1494) | Replaced the native auto-download-capable spaCy load path with an explicitly local-only engine. Missing assets report `model_unavailable`; dependency absence remains unavailable. | Installed Presidio plus German/English spaCy models ran; email redaction succeeded in authored examples, with the actual spaCy downloader forbidden and zero calls. Missing-model regression verifies no implicit download. |
| Browser MCP (#1503) | Click arguments are negotiated against the connected server's advertised `ref`/`target` schema. Native tool errors cannot be turned into captures. Every navigate/click/wait boundary is checked before another action, not only the final URL. | Fixture server schemas, unapproved intermediate navigation, native error and unconditional cleanup tests pass. This is not proof that the production browser guard hook is deployed. |
| Entity linkage (#1492/#1511) | Source-bound Splink/RapidFuzz fields now resolve exact captured content/metadata selectors. Caller-supplied names/revisions are rejected. Input hashes, locator-derived revisions, canonical state and returned IDs/identifier guards are verified before artifact/review publication. | Actual native RapidFuzz and Splink scoring runs through `OptionalAnalysisStore`; tests reject forged attributes/IDs/revisions/guards, preserve source dependencies and verify review-only routing without redirects. Canonical assignment/type remain explicit operator review hypotheses. |
| GLiNER (#1493) | Added the missing native relation and structured-field paths, combined schemas for German authorities/EU funding/Berlin legal notices, exact span validation, missing-field reporting and schema/source/model identity. | Combined dispatch through the actual source store is tested. Real SDK inference exposed an additional description-qualified relation-key mismatch; exact frozen-schema alias handling now passes captured-native-output replay. A post-fix full worker rerun was blocked, as detailed below. |

`src/evaluation/runtime_outcomes.py` deliberately does not scan arbitrary nested `status` fields. An NLI abstention or a report pending human review is not an execution failure; incomplete evidence coverage is still distinguished from a complete task. Incomplete outputs remain replayable diagnostics, not empty successful enrichments.

## Native execution evidence and its limits

MiniLM retrieval and Qwen3 reranking completed with cached pinned weights in isolated workers on two authored German/English documents. Their outputs, timings and RSS are in `workflow-review-evidence/continuation-cached-model-smoke.json`. These are real inference smoke tests, not independent relevance judgments, representative accuracy or adoption decisions. Native Presidio sanity execution is recorded in `/tmp/noesis-presidio-local-smoke.log`. Splink/RapidFuzz source binding is exercised by `tests/unit/kb/test_entity_analysis_binding.py`.

GLiNER2 2.0.0 was installed only into the existing compatible Python 3.12 environment (Transformers 4.57.6, Torch 2.14.0+cpu). Actual German and English schema worker runs reached inference but failed output validation. Both failures are preserved in `continuation-gliner-schema-pre-fix.json`; the original `continuation-gliner-schema-smoke.json` also still contains these pre-fix failures and must not be interpreted as a successful post-fix run.

A bounded offline native SDK invocation then produced the raw German result. The SDK emitted a relation key combining the name and description, alongside an empty canonical relation key. The adapter now resolves only exact aliases from the supplied schema, retaining the native label and source offsets. The recorded SDK output is `tests/fixtures/workflow_review/gliner2_native_schema.json`; its provenance explicitly says it is native output on authored input, not independent human labels. Post-fix replay of that output produces three entities, one relation and a structured funding award with exact source spans. The full post-fix worker rerun was blocked by the execution tool's safety-status check and did not run. Captured-output replay is not silently relabelled successful end-to-end inference.

No paid hosted inference, fabricated model result, new independent human annotation, automatic entity merge or model report approval occurred. Optional model results do not become verified factual support merely because their JSON is valid.

## Current verification

The broad scoped regression completed with **1,502 passed, 13 skipped, zero failures/errors**. Scope:

```text
tests/unit/ingestion
tests/unit/kb
tests/unit/evaluation
tests/unit/noesis_cli/test_optional.py
tests/rag/test_normalization.py
tests/unit/services/rag/test_lingua_normalization.py
tests/knowledge_graph/test_entity_resolution.py
```

Log: `/tmp/noesis-continuation-final.log`; JUnit: `/tmp/noesis-continuation-final.xml`. Exact times, hashes and skip reasons are retained in `workflow-review-evidence/execution-path-validation.json`. The preceding run had 1,500 passes before two additional GLiNER regressions; it is not a separate set of unique tests.

The historical focused command named two nonexistent paths in the current checkout: `tests/argument_mining/test_model_registry.py` and `tests/unit/services/rag/test_retriever_config.py`. Its current-path replacement initially could not collect because `psycopg2` was absent. After installing only `psycopg2-binary==2.9.12` in `.eval-packages`, the five actual files below passed **59 tests**:

```text
tests/unit/kb/test_model_registry.py
tests/rag/test_chunking.py
tests/unit/services/rag/test_chunking_comprehensive.py
tests/unit/services/rag/test_retriever_comprehensive.py
tests/unit/kb/test_derived_revisions.py
```

Log: `/tmp/noesis-continuation-provider-ready.log`; JUnit: `/tmp/noesis-continuation-provider-ready.xml`. This is a separately named current scope, not a retroactive validation of the old 43-pass command. Some tests overlap the broad suite; do not add counts. The broad result predates the optional driver installation; the focused result follows it.

Scoped Ruff and `git diff --check` pass. Neither the full repository suite nor repository-wide lint has been certified.

The rebuilt wheel was extracted outside the checkout and its actual CLI was exercised against a temporary configured warehouse. Doctor passed; missing Ragas returned exit 3 and unavailable; no artifact was created; replay preserved those outcomes. Python socket traps observed zero network attempts. The optional entry point and ten required current modules were verified in the archive, with `.eval-*`, storage and virtual environments excluded. See `workflow-review-evidence/continuation-wheel-validation.json`. No release or production package installation occurred. These tests do not constitute a universal network sandbox.

## Current contracts and changed files

Primary new modules are `src/evaluation/runtime_outcomes.py`, `src/kb/entity_analysis_inputs.py` and `src/evaluation/gliner_schema.py`. They are wired into the existing worker/store/model entry points rather than a parallel source or review store. New boundary tests are `test_execution_outcomes.py`, `test_pdf_runtime_contracts.py`, `test_entity_analysis_binding.py` and `test_gliner_schema.py`; existing media/benchmark/Presidio/MCP/store tests were extended.

The operator guide documents the source-bound linkage selector contract, combined GLiNER schemas, timing v2, exact media byte/duration limits, state propagation and legacy replay caveat: `docs/guides/optional-runtime-integrations.md`. `optional-runtime-implementation-status.json` records the specific rechecked paths; it is not a blanket acceptance certificate.

## Remaining work is not only external quality validation

The entire 50-leaf backlog has not been individually reaudited against every actual issue body and real SDK/planner/publication path. Several neural, alignment, OCR, parser and report SDK/weight combinations still need actual compatible invocation. Official MCP deployments and their guard configuration remain unverified. Native regional/registry workflows exposed through the optional CLI do not by themselves prove every issue's planner/source-pack and cross-registry integration requirements. These can still expose implementation defects; no claim is made that all code work is finished.

Historical erroneous artifact rows are not automatically repaired by the new safe optional read/replay view. Existing persistent warehouses need an explicit evidence-preserving historical-artifact/downstream assessment before treating those older rows as usable. No destructive migration was performed here.

Independent human collection required by #1420 (500–1000 real sentences with two annotators, agreement/adjudication and frozen release), other specified relevance/support judgments, model comparisons and reviewer pilots remain unfulfilled. Representative PDF/OCR/scientific/media/browser/anti-bot and active-handler crash evaluations are not replaced by authored fixtures. Label Studio deployment/pilot and Phoenix retention/debugging studies remain incomplete. The MinHash threshold selection changed after the prior held-out fixture was inspected, so that fixture is not fresh independent validation.

Actual issue scope remains authoritative. #1388 permits explicit live-evaluation unavailability when authorized Zyte access is absent; a paid live run is not imposed as an unconditional closure condition. #1463's actual report criteria—individual accept/reject, versioned and reproducible prior reports, updated report/bibliography export without external publication—are exercised in `tests/unit/kb/test_report_updates.py`; unrelated model-quality requirements have been removed from its current matrix row. Closure is separate: earlier PAT write attempts failed with HTTP 403, and no identical write was retried.

No commits, pushes, merges, GitHub closures or paid requests were performed in this continuation. The prior dirty tree was preserved. The next completion judgment must use the remaining issues' actual acceptance bodies and demonstrated integration paths, not this pass's file count or green test totals.
