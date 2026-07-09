# run-benchmark skill

Run the argument-mining benchmark evaluation and update `docs/subsystems/argument-mining-benchmarks.md`.

**All paths relative to repo root** (`/home/Ikey/NeuroNews`).

## Quick usage

```bash
# Baseline evaluation (no gate, no external datasets)
python scripts/benchmark_models.py

# Apply ≥2% F1 gate vs. previous checkpoint
python scripts/benchmark_models.py --gate

# Include external benchmark datasets
python scripts/benchmark_models.py \
  --fever  /data/fever/ \
  --liar   /data/liar/  \
  --averitec /data/averitec/

# Gate + external (full CI-style run)
python scripts/benchmark_models.py --gate --fever /data/fever/ --liar /data/liar/
```

## What it evaluates

| Model | Primary metric | GT source |
|---|---|---|
| `ClaimDetector` | binary F1 (claim vs. non-claim) | `claims.parquet` test split |
| `StanceClassifier` | macro F1 (4 stances) | `stance.parquet` test split |
| `FrameClassifier` | macro F1 (7 frames, multi-label) | `frames.parquet` test split |

Breakdown computed per-source-type (blog/book/news/note/paper/transcript)
and per-length-bucket (short <100 chars / medium 100-300 / long >300).

## Outputs

- `docs/benchmark_results.json` — machine-readable, consumed by `get_benchmark_results` MCP tool
- `docs/subsystems/argument-mining-benchmarks.md` — human-readable report with tables

## F1 gate

Pass `--gate` to enforce the ≥2 percentage-point improvement policy from issue #92.
The script exits 1 if any model regressed vs. the previous `benchmark_results.json`.

```
Exit 0 = pass
Exit 1 = gate failure (regression ≥2 pp)
Exit 2 = evaluation error (bad data / import failure)
```

## MCP shortcut

After running the script, use the MCP tool to inspect results without re-running:

```
get_benchmark_results()                    # compact summary of all three models
get_benchmark_results(model="claim_detector")   # full claim_detector results
```

## Cross-dataset generalisation

External datasets are skipped gracefully when not available.  To enable them,
download and point to their directories:

- **FEVER** (`paper_dev.jsonl`): https://fever.ai/dataset/fever.html
- **LIAR** (`test.tsv`): https://huggingface.co/datasets/liar
- **AVeriTeC** (`dev.json`): https://huggingface.co/datasets/chenxwh/AVeriTeC

## When to use

- After editing `models.py`, `frames.py`, or any classifier heuristic
- After training a new checkpoint (set `--gate` to enforce the F1 policy)
- When publishing a release to snapshot current performance
