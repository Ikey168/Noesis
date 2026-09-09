# Noesis execution-path continuation — 2026-09-07

**Current status: scoped implementation defects repaired; the full 50-leaf backlog is not certified complete.** Use [execution-path audit](execution-path-audit.md) and `workflow-review-evidence/execution-path-validation.json` for this pass. The previous native-runtime summary below predates the reproduced dispatch/publication bugs and must not be used as a blanket completion certificate.

Actual WhisperX dispatch now consumes digest-verified bytes through the bounded pipe decoder. Incomplete backend states survive the worker, store, benchmark and CLI without successful artifacts. The audit also repairs captured PDF parsing, OCR/Docling status, BGE batching/timing, local-only Presidio loading, browser action/schema checks, source-backed entity matching, and native GLiNER relation/structured-field extraction. Exact native executions, test-double results, captured-output replay and blocked reruns remain separately labelled.

No commit/push/closure or paid calls occurred. Outstanding issue acceptance, unverified SDK/deployment paths and historical-artifact caveats are listed in the current audit; they are not silently reclassified as only external quality work.

## Historical native-runtime handoff (superseded status claims)

Branch: `feat/workflow-review-implementation`. Changes remain local and uncommitted; no push or issue closure occurred. GitHub's refreshed inventory remains 58 open issues (50 leaves, eight trackers).

## Delivered

The prior profiles/normalizers now have native provider/model implementations and supported operator entry points. `noesis-optional` connects bounded native execution to existing snapshots, document revisions, artifacts, entity review, annotations and reports. A successful execution does not imply issue acceptance or production model quality.

Native integrations cover regional EU/German sources and selected export-import paths; OpenAlex content, Exa/Tavily/Jina/Firecrawl/Zyte; Splink/RapidFuzz; GLiNER2/E5/BGE/Qwen/mDeBERTa/SaT; PaddleOCR/LightOnOCR/WhisperX; Presidio; selected Ragas metrics; Phoenix OTLP; GitHub/Playwright MCP; Label Studio; and Outlines report proposals. Existing Guardian/review-dataset/report features are retained and checked. Lingua/MinHash and their earlier actual library benchmarks are preserved.

The native benchmark runner supports frozen held-out labels, groups and explicit output mappings. MiniLM/E5/BGE retrieval uses identical supplied corpora and isolated versioned indexes. Failed cases remain visible and cannot silently improve average metrics. The fixture example is explicitly not independent human data.

Source-bound jobs recheck revisions and access, persist replayable outcomes and reject conflicting run IDs. New report citations are checked again before acceptance. Redaction decisions contain source locators rather than guessable sensitive-span hashes. Audio decoding is pipe-only; parser and scraper jobs run in private bounded process groups. SDK interpreter selection avoids forcing incompatible heavy profiles into one Python environment.

## Validation

- Broad regression: **1,421 passed, 13 skipped**, no failures/errors.
- Focused provider/model-registry/chunking/retrieval suite: **43 passed, four skipped** (overlaps some broad tests).
- Scoped Ruff and `git diff --check`: passed. Repository-wide lint is not certified.
- Wheel built, ten critical native modules checked, optional entry point present, and doctor successfully run outside checkout with zero network calls.
- PyMuPDF authored corpus: five native parser executions completed; structural quality metrics remain separate from execution success.
- Ten-case scraping corpus executed for Scrapy, Playwright, Crawl4AI, Crawlee and adaptive Crawlee. Expected failures, including scroll-triggered content, are retained. No paid hosted backend was called.

## Where to start

```sh
python -m src.noesis_cli.optional --doctor
```

Use `docs/guides/optional-runtime-integrations.md` for install profiles, trusted scopes, native job/benchmark manifests, model caching, provider approvals, annotation and report operations. Reviewed request examples are in `config/optional_examples/`.

The per-issue implementation/test/remaining-acceptance matrix is `docs/development/optional-runtime-implementation-status.json`. Machine-readable validation and environment details are in `docs/development/workflow-review-evidence/native-runtime-validation.json`.

## Not represented as complete

The 500–1000-sentence dual-independent human dataset has not been collected. Independent accuracy, reviewer/pilot work, representative scan/crawl quality, and credentialed provider billing/coverage remain acceptance tasks. Several neural SDK/weight combinations still require their compatible local environments and actual execution. No test-double inference, authored label, cache presence, or cost reservation is labelled as real independent evaluation. No production model was automatically adopted, no source was overwritten, no identity automatically merged and no model report proposal automatically approved.
