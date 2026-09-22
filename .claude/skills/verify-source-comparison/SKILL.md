---
name: verify-source-comparison
description: Smoke-test the multi-source news comparison engine without a running API server. Covers per-source coverage grouping and counts, average and dominant sentiment, trust scores (frame_diversity and composite_score), cluster labels, per-topic stance, the summary fields (most positive, negative, trusted, and highest-coverage source), edge cases (empty topic, no-match topic, limit), get_source_profile(), and list_source_trustworthiness() filtering. Use when changing the comparison queries or the joins into outlet_scores, outlet_clusters, or source_stances.
---

# verify-source-comparison

Smoke-tests the multi-source news comparison engine (Issue #46) without
needing a running API server.

## What it tests

| Stage | What |
|-------|------|
| Coverage query | Articles correctly grouped and counted by source |
| Sentiment | avg_sentiment and dominant_sentiment match expected values |
| Trust scores | `frame_diversity`, `composite_score` populated from outlet_scores |
| Cluster | `cluster_label` joined from outlet_clusters |
| Stance | Per-topic stance joined from source_stances |
| Summary | most_positive/negative/trusted/coverage_source correct |
| Edge cases | Empty topic, no-match topic, limit parameter |
| Profile | `get_source_profile()` returns article count, trust scores, stances |
| Trustworthiness list | `list_source_trustworthiness()` filters by source_type |

## Usage

```bash
# From repo root:
PYTHONPATH=. python3 .claude/skills/verify-source-comparison/smoke.py
```

Expected: all checks pass, exit 0.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| `ModuleNotFoundError: duckdb` | `pip install duckdb` |
| `BinderException: Cannot compare VARCHAR and TIMESTAMP` | `computed_at` column is not a valid timestamp string in the test fixture |
| Wrong sentiment sign | Check `avg_sentiment` computation — negative scores for negative articles |
| 0 sources found | ILIKE pattern not matching — check topic keyword vs. article title/content |
