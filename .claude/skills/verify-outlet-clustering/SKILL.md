---
name: verify-outlet-clustering
description: Smoke-test the outlet editorial-framing cluster pipeline without a running API server or a populated warehouse. Runs on synthetic vectors by default and reports outlet count, method, and silhouette score per k; the --live flag clusters real document_frames from the warehouse instead. Use when changing the clustering implementation, tuning k or the clustering method, or confirming the pipeline works before relying on outlet cluster labels.
---

# verify-outlet-clustering skill

Smoke-tests the outlet editorial-framing cluster pipeline (#115) without
needing a running API server or a populated warehouse.

**All paths are relative to the repo root.**

## Usage

```bash
# Synthetic vectors (no warehouse required)
python3 .claude/skills/verify-outlet-clustering/verify.py

# Live warehouse (needs document_frames populated)
python3 .claude/skills/verify-outlet-clustering/verify.py --live
```

## Example output

```
────────────────────────────────────────────────────────────────────────
  Outlet Clustering — smoke-test
────────────────────────────────────────────────────────────────────────
  Mode: synthetic vectors

  k=2  method=kmeans  silhouette=0.4302  outlets=10

  Cluster 0 — economic-dominant  (dominant: economic)
    [news        ] Bloomberg  (69 docs)
    [news        ] Reuters  (64 docs)
    ...

  RESULT: PASS — clustering produced valid k-cluster assignment with PCA coords
────────────────────────────────────────────────────────────────────────
```

## When to use

| Scenario | Action |
|---|---|
| After editing `outlet_clustering.py` | Run synthetic mode to catch logic regressions |
| After running frame extraction on new content | Run `--live` to see real cluster assignments |
| Debugging silhouette score < 0 | Check for degenerate vectors (all-zero frames) |

## Requirements

- `numpy`, `scikit-learn` (already in project deps)
- No trained models, no network, no warehouse (synthetic mode)
