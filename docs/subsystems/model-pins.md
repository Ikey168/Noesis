# Argument-model pins and updates

`src/argument_mining/model_registry.py` is the source of truth and
`models/pins.lock.json` is its checked-in lock receipt. Model names and
revisions are immutable Hugging Face commit SHAs, not branches or tags.

```bash
make models                 # fetch/resume both snapshots and rewrite the lock
make models-verify          # offline registry ↔ lock consistency check
python -m src.argument_mining.fetch_models --check --require-cache
```

Runtime inference is network-free. With a valid cached snapshot, claim
detection and NLI-backed stance/frames activate by default. If dependencies or
weights are missing, inference fails closed and directs the operator to run
`make models`.

To update a model, change its name/commit in the registry, run `make models`,
then run the internal and external evaluation:

```bash
python scripts/benchmark_models.py --candidate-gate
```

Commit the registry, lock, JSON, and Markdown benchmark receipts together.
The candidate gate requires at least +2 percentage points on binary F1 for
claims and macro-F1 for stance/frames. Ordinary CI also rejects regressions of
2 points or more. If upstream deletes an object or a pin diverges, CI fails;
the project does not silently advance to a mutable revision.
