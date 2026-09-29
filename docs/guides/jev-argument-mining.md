# Optional Jev stance and editorial frame suggestions

`src.argument_mining.jev_adapters` adds optional task adapters over the
authorized `DecisionRuntime`. They do not alter `StanceClassifier`,
`FrameClassifier`, ingestion, or stored argument-mining results. The caller must
provide an exact source revision, principal and scopes, hosted policy, and an
explicit `allow_remote=True`. The runtime captures the source version, checks
access again immediately before execution, reserves cost before sending, and
records its decision receipt. The knowledge-engine MCP exposes them as
`suggest_jev_stance` and `suggest_jev_frames`; both require
`knowledge:decision:execute` and produce `accepted: false` outputs under the
`noesis-jev-stance-suggestion-v1` and `noesis-jev-frame-suggestion-v1`
contracts.

## Stance (#1627)

`suggest_stance` uses the existing sentence splitter on the captured text. It
records the original sentence, index, character span, nearby bounded context,
topic, source binding, full Choice probabilities, and vendor confidence. The
Choice labels are `supportive`, `critical`, `neutral`, and `ambiguous`; quoted
positions and speaker changes are explicit in the rubric. A frozen stance
calibration policy can turn the raw Choice distribution into an optional
suggestion or abstention. Its model and rubric identities must match the
receipt. Without that policy, no final stance is selected.

## Editorial frames (#1628)

`suggest_frames` evaluates independent Noul questions for economic, security,
humanitarian, legal, political and scientific frames in overlapping 4,000
character windows. Scores remain separate from decisions; each label uses its
own validation-fitted threshold. `other` is derived only if no substantive
frame clears a threshold. Multiple frames may be selected together. Every
window and its source span is recorded. An unavailable window or a document
over the 16-window bound produces an incomplete-coverage abstention instead
of a partial document classification. A model or rubric change between
windows also causes abstention. The legacy local snippet classifier still
uses its existing default path.

## Evaluation and release

`fit_jev_mining_policy` wraps the shared mining calibration code and uses
completed validation receipts only. `evaluate_jev_mining` requires a disjoint
held-out test split and reports macro and per-label precision, recall and F1,
content-type results, accepted coverage, statuses and frame subset/dominant
accuracy. Rows can include both `legacy_snippet` and
`calibrated_full_window` baseline predictions. Quoted positions, opposing
speakers, implicit stance and topic changes belong in independently labeled
held-out cases; the offline fixtures cover API behavior but make no quality
claim. The current benchmark file records local baseline figures, but no Jev
result or human-label gain has been measured. The evaluator always returns
`task_ready: false`; production selection needs human-label evidence and
explicit domain review.
