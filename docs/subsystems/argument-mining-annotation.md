# Human annotation protocol

Noesis separates generated training fixtures from human evaluation. The
checked-in argument-mining Parquet files are useful for deterministic smoke
tests; they are not evidence that the classifiers generalize to real writing.
Randomly perturbing those labels is reported as a noise diagnostic and never
as inter-annotator agreement.

## Evaluation sample

Use 500–1,000 sentences from genuinely ingested documents spanning `news`,
`blog`, `paper`, `transcript`, `book`, and `note`. Repository demo documents,
example.com URLs, and generated templates are excluded by the exporter. Keep
the source URL and document id as provenance, and do not use the held-out set
for prompt, threshold, or model tuning.

```bash
python scripts/human_annotation.py export --db data/noesis.duckdb --size 750
```

The command creates two independently shuffled CSV assignments. Two different
people fill `annotator_id`, `is_claim` (`0`/`1`), `stance`, and one or more
pipe-separated `frames`. Annotators work independently and follow the label
definitions in `data/argument_mining/schema.md`. They may use `notes` to flag
insufficient context but must not consult model predictions.

## Adjudication and release

After both files are complete, run `finalize` without an adjudication file to
measure Cohen's kappa and enumerate disagreements. A third reviewer then copies
one assignment, resolves every label, and records a distinct `annotator_id`.

```bash
python scripts/human_annotation.py finalize \
  --a data/argument_mining/human_eval/annotator_a.csv \
  --b data/argument_mining/human_eval/annotator_b.csv \
  --adjudication data/argument_mining/human_eval/adjudication.csv
```

Only this final command writes `human_gold.parquet`. Its report includes real
claim, stance, and frame agreement, source-type coverage, annotator identities,
and a content hash. If the people or completed assignments are absent, the
honest status is `not_collected` or `needs_adjudication`; CI must not turn that
external dependency into a fabricated green check.
