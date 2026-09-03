# Human annotation protocol (`noesis-human-eval-v1`)

Noesis separates generated training fixtures from human evaluation. The
checked-in argument-mining Parquet files are useful for deterministic smoke
tests; they are not evidence that the classifiers generalize to real writing.
Randomly perturbing those labels is reported as a noise diagnostic and never
as inter-annotator agreement.

## Labels

Annotate the sentence itself, using the fixed `topic` column for stance:

- `is_claim=1` when the sentence makes a checkable assertion; use `0` for a
  question, command, greeting, or purely subjective reaction. Attributed and
  false assertions are still claims—the label is not a truth judgement.
- `supportive` endorses or supplies evidence for the topic; `critical` disputes
  or argues against it; `neutral` reports without taking a side; `ambiguous`
  applies only when context genuinely prevents one of those decisions.
- Frames may be multi-label: `economic` (money, jobs, markets), `security`
  (threat, conflict, policing), `humanitarian` (welfare, rights, suffering),
  `legal` (law, courts, rules), `political` (power, elections, governance),
  `scientific` (research, methods, empirical mechanisms), or `other`.
  `other` cannot be combined with another frame.

Examples: “The trial cut infections by 30%” is a claim with a scientific frame.
“Although costly, the policy is necessary” is typically supportive and may be
economic plus political. “Did the policy work?” is not a claim; its stance may
still be ambiguous. Record uncertainty in `notes`; do not invent missing
context.

## Pilot and evaluation sample

Start with a 24–60 item pilot. Resolve recurring disagreements by clarifying
this document and record the protocol revision before exporting the full set:

```bash
python scripts/human_annotation.py export --pilot --size 48 --seed 1729
python scripts/human_annotation.py analyze \
  --a data/argument_mining/human_eval/annotator_a.csv \
  --b data/argument_mining/human_eval/annotator_b.csv \
  --guideline-change "Clarified that attributed assertions still count as claims"
```

Use 500–1,000 sentences from genuinely ingested documents spanning `news`,
`blog`, `paper`, `transcript`, `book`, and `note`. Repository demo documents,
example.com URLs, and generated templates are excluded by the exporter. Keep
the source URL and document id as provenance, and do not use the held-out set
for prompt, threshold, or model tuning.

```bash
python scripts/human_annotation.py export --db data/noesis.duckdb --size 750
```

The command creates two independently shuffled CSV assignments and a manifest
containing the seed and slice counts. Two different people fill one stable
`annotator_id`, an ISO-8601 `annotated_at` with timezone, all labels, and
optional `notes`. They must not collaborate, see the other assignment, or
consult model predictions. The validator rejects missing, duplicate, malformed,
or source-edited rows.

Private documents are eligible only when their metadata explicitly contains
`human_eval_consent: true`. Confirm redistribution terms before sharing any
assignment; CSVs and released gold are gitignored because they contain source
text. Public URLs default to `source-terms`, not an assumed open license.

## Adjudication and release

After both files are complete, run `analyze` to measure agreement and create an
adjudication file containing exactly the disagreements. A third reviewer
resolves every label and records a distinct identifier and timestamp.

```bash
python scripts/human_annotation.py analyze \
  --a data/argument_mining/human_eval/annotator_a.csv \
  --b data/argument_mining/human_eval/annotator_b.csv
```

```bash
python scripts/human_annotation.py finalize \
  --a data/argument_mining/human_eval/annotator_a.csv \
  --b data/argument_mining/human_eval/annotator_b.csv \
  --adjudication data/argument_mining/human_eval/adjudication.csv
```

Only this final command writes `human_gold.parquet`. Its report includes real
claim, stance, and per-frame agreement, deterministic 95% intervals,
source/language/length coverage, annotator provenance, and content hashes. Raw
labels are retained beside the adjudicated label. Splits are deterministic by
document so sentences from one work cannot cross train/dev/test boundaries.

## Benchmark and promotion

Generate predictions from the pinned models, then reproduce a human-gold report:

```bash
python scripts/human_annotation.py predict \
  --gold data/argument_mining/human_eval/human_gold.jsonl \
  --stance-threshold 0.65 --frame-threshold 0.60
python scripts/human_annotation.py evaluate \
  --gold data/argument_mining/human_eval/human_gold.jsonl \
  --predictions data/argument_mining/human_eval/predictions.csv \
  --experiment candidate-experiment.json
```

Tune label text, thresholds, calibration, or a candidate model on `train` and
`dev` only. Evaluate the frozen candidate once on `test`, passing the baseline
report via `--baseline`. Promotion requires at least +2 percentage points of
stance and frame macro-F1 and no source/language/length slice regression beyond
5 points. Calibration and coverage/error trade-offs remain visible even when
the quality gate passes. An accepted candidate is pinned in a separate reviewed
change; a rejected report is retained.

If the people or completed assignments are absent, the honest status is
`not_collected` or `needs_adjudication`; CI must not turn that external
dependency into a fabricated green check. Simulated perturbations are noise
diagnostics, never human agreement.
