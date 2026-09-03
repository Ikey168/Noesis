# noesis-human-eval-v1 — human argument-mining evaluation

This additive contract governs real-human evaluation for claim detection,
stance classification, and frame classification. Generated examples, model
predictions, and perturbed labels cannot satisfy it.

## Sampling and assignment

`scripts/human_annotation.py export` deterministically samples sentence-level
items from the unified `documents` table. Full sets contain 500–1,000 examples;
smaller sets must be marked as pilots. The manifest records the seed, sampler
version, source/language/length/difficulty slices, exclusions, privacy policy,
and an immutable dataset digest. News, blog, paper, transcript, book, and note
each have an enforced minimum quota. Per-document caps limit domination by a
single work.

Example and generated documents, repeated sentences, and private material
without explicit `human_eval_consent` metadata are excluded. Assignment CSVs
contain source text and provenance, so they are operational artifacts and are
not committed. They contain no model predictions.

Two different people independently complete shuffled assignments. Every label
must carry one stable annotator identifier and a timezone-qualified timestamp.
Changing an immutable item field invalidates the assignment.

## Labels and adjudication

Claim is binary. Stance is one of `supportive`, `critical`, `neutral`, or
`ambiguous`, interpreted against the fixed `topic` included with the item.
Frames are one or more of `economic`, `security`, `humanitarian`, `legal`,
`political`, `scientific`, and `other`; `other` cannot be combined with another
frame.

The analysis step reports Cohen's kappa for claim and stance, one-vs-rest kappa
for every frame, exact agreement, deterministic 95% bootstrap intervals, and
source/language/length slices. Values with fewer than ten examples remain in
the report as `undersized`. Only disagreements are sent to a third person, who
must be distinct from both annotators. Final gold preserves all raw judgements,
timestamps, and adjudicated values.

## Evaluation and promotion

Models are evaluated only on the untouched document-grouped test partition.
Reports include task and per-label metrics, confusion matrices, calibration,
abstention curves, failure slices, sample sizes, and 95% intervals. Human and
synthetic results are explicitly separated.

A candidate is eligible for pinning only when stance and frame macro-F1 each
improve by at least 0.02 over the same gold test set and no reported slice
regresses by more than 0.05. Calibration regressions are surfaced for review.
An accepted report authorizes a separate, reviewable model-pin change; the
evaluation command never silently changes a production pin. Rejected candidates
remain recorded in their report.

The checked-in `status.json` stays `not_collected` until people have actually
completed the work. CI tests protocol mechanics with fixtures, never fabricated
human agreement.
