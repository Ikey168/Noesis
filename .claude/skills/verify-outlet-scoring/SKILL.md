---
name: verify-outlet-scoring
description: Smoke-test the outlet transparency scoring pipeline. Checks _shannon_entropy edge cases (balanced, concentrated, empty, and partial distributions) and equal-stance neutrality, then optionally runs compute_outlet_scores() against real document_frames, argument_claims, and source_stances with the --live flag. Use when changing the transparency score formulas, the entropy helper, or any of the three score dimensions.
---

# verify-outlet-scoring skill

Smoke-tests the outlet transparency scoring pipeline (#116).

**All paths are relative to the repo root.**

## Usage

```bash
# Unit tests only (no warehouse required)
python3 .claude/skills/verify-outlet-scoring/verify.py

# Include live warehouse scoring
python3 .claude/skills/verify-outlet-scoring/verify.py --live
```

## What it tests

- `_shannon_entropy()` edge cases: balanced → 1.0, concentrated → 0.0, empty → 0.0
- Partial distribution stays in (0, 1)
- Equal 4-stance neutrality → 1.0
- Live: `compute_outlet_scores()` against real `document_frames` + `argument_claims` + `source_stances`

## Three score dimensions

| Dimension | Formula | Range |
|---|---|---|
| `frame_diversity` | Shannon entropy of 7-frame dist / log(7) | 0–1, higher = more balanced |
| `attribution_rate` | attributed_claims / total_claims | 0–1, higher = more attributed |
| `stance_neutrality` | entropy of [sup,crit,neu,amb] dist / log(4) | 0–1, higher = more balanced |
| `composite_score` | mean of the three | 0–1 |

## When to use

- After editing `outlet_scorer.py`
- When verifying weekly scores after a new data ingestion run
- When debugging unexpected score values for a specific outlet
