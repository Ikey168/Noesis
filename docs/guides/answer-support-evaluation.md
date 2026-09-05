# Answer support evaluation

`src.kb.answer_support_eval.evaluate_support` scores evidence relevance, entailment, and appropriate refusal separately. Optional `noesis-answer-v1` responses go through the existing structural evaluator in a separate field. A cited, schema-valid response can still receive a wrong support judgment.

Supply 1–1,000 uniquely identified cases covering `unsupported_citation`, `contradiction`, `correction`, `incomplete_coverage`, and `appropriate_refusal`. Each case includes a pinned `source_revision_id`, `locator`, `annotator_id`, and `judgment` with boolean `relevant`, boolean `should_refuse`, and `entailment` (`entailment`, `contradiction`, `neutral`, or `unavailable`). Predictions are keyed by case ID with `entailment`, nullable boolean `relevant`, boolean `refused`, and optionally `answer`. Missing relevance stays unavailable; every case requires an explicit outcome.

An audit entry has an independent `reviewer_id` and a `judgment_hash` computed using the canonical project `_hash` helper. The result identifies the audit subset without claiming that supplied identities were independently authenticated. Behavior fixtures require `label_origin="fixture", allow_fixture=True` and cannot become a completed human audit. Real labeled cases and an independently reviewed audit subset remain required for QA-03 completion.

## Long evidence

`TransformersNLI.classify` and `entailment_scores` now reject pairs exceeding 512 tokens before inference. They no longer silently truncate paired evidence. Call `classify_evidence(premise, hypothesis)` to assess complete longer spans using at most 64 windows with exact original character coordinates and 32-token overlap by default. The hypothesis and special tokens count against each 512-token window. The full plan must fit the window budget before any inference starts.

Results retain each window's prediction. Opposing entailment and contradiction windows return `conflicting_evidence` with zero aggregate confidence. A decisive window can determine the aggregate label when no opposite window exists; this is an explicit aggregation rule, not validated cross-window reasoning. Reported model scores are not calibrated probabilities. Evidence beyond the text/window limits fails explicitly. Missing cached models still fail closed; no heuristic answer-support fallback was added.

The unit cases validate tail coverage, opposing windows, bounds, missing models and separate metric accounting. Their labels are test fixtures, not human judgments of real scientific evidence.
