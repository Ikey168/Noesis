# Workflow-review evaluation candidates

This guide documents the offline-first evaluation surface for issues #1420,
#1426, #1427, #1492–#1494, #1499–#1501, #1503–#1512, #1514, #1520, and #1521.
It does **not** claim that optional models, paid/provider-backed services, or
independent human evaluations ran when those prerequisites are unavailable.

Run the deterministic status check with:

```bash
uv run python scripts/evaluate_workflow_review_candidates.py \
  --output docs/development/workflow-review-evidence/worker-2-candidate-status.json
```

The output records dependency availability and the current independent-human
evaluation manifest state. `ready_for_opt_in_run` means only that the Python
dependency is importable; it is not a quality result, model-download claim, or
adoption decision. Model checkpoint revisions must be pinned to an immutable
revision when a real benchmark is executed.

## Human evaluation and benchmark boundaries

`human_evaluation_status()` verifies that a collected dataset has source,
domain, language, independent annotator origins, a frozen test split, and no
related-document split leakage. If the repository manifest still declares
`not_collected`, the result is `unavailable`. Synthetic fixtures never satisfy
#1420.

`retrieval_benchmark()` measures recall@k, MRR@k, and nDCG@k over frozen qrels
and preserves `complete`, `partial`, and `unavailable` retrieval states. The
fixture deliberately requests 30 results to exercise the >20 path, but its
labels are marked `fixture-only`; it is a harness regression, not the human-
judged benchmark required by #1426.

`support_benchmark()` keeps evidence relevance/support separate from Answer-v1
schema compliance. Without a configured scorer it fails closed as unavailable.
Long spans are flagged in the case record; live NLI evaluation should use
`TransformersNLI.classify_evidence()` so inputs are windowed rather than
silently truncated.

## Optional candidates

All model/library candidates remain opt-in and leave production defaults
unchanged. The status harness covers Splink, GLiNER2, Presidio, Ragas, Phoenix,
multilingual-e5-small, BGE-M3, Qwen3-Reranker-0.6B, multilingual mDeBERTa,
LightOnOCR-2-1B, wtpsplit/SaT, Lingua, RapidFuzz, datasketch, WhisperX, and
Outlines. A missing package is an explicit `unavailable` state.

The E5 contract records required `query:`/`passage:` prefixes and isolated
model-versioned indexes. BGE-M3 explicitly does not assume multi-vector support
from the current vector store. Qwen3 reranking requires its dedicated yes/no
relevance template. mDeBERTa requires premise/hypothesis and label-map
verification. OCR retains page identity and does not treat image bboxes as text
coordinates.

## MCP and annotation exchange

`github_mcp_profile()` defines a minimal read-only research-record profile and
never stores credentials in evidence. A real completion of #1501 still needs a
configured official GitHub MCP server, pagination/rate-limit/access-revocation
tests, and a captured technical-research example that remains citable offline.

`playwright_mcp_profile()` allows only bounded interactive navigation on an
explicit domain allowlist; bulk crawling and credential entry are excluded.
Real interactive acquisition remains unavailable until the MCP server is
configured and exercised on the requested EU/German/Berlin pages.

`label_studio_export()` / `label_studio_import()` preserve source revisions,
Unicode span offsets, reviewer identity mapping, and human/model attribution.
Stale revisions and offset mismatches are rejected. Noesis remains the authority
for adjudication and dataset release. The independent pilot, annotator-time
measurement, agreement measurement, and edition-capability verification in
#1521 still require actual humans and a selected Label Studio deployment.

## Adoption decisions

No adopt/defer quality decision is emitted from dependency availability or
synthetic fixtures. Candidate adoption requires the issue-specific held-out
measurements on the same hardware/corpus, with independent labels where the
issue requires them. This keeps regression evidence distinct from real-world
validation.
