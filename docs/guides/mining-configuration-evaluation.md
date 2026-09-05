# Stance and frame configuration evaluation

The real pinned NLI experiment covers the existing 1,076-example held-out split for each task. It compares the existing stance/dominant-frame behavior, full multi-label frame decisions, and predeclared abstention/threshold settings. It does not change the stronger claim detector or deploy a selected replacement.

| Configuration | Macro F1 | Coverage | Additional finding |
|---|---:|---:|---|
| Existing stance | 0.3289 | 100% | Critical recall 0.0970 |
| Stance with 0.35 score / 0.40 share floor | 0.1813 | 41.45% | Critical recall 0.1269; substantial abstention |
| Existing dominant frame | 0.4193 | 100% | Reproduces the earlier frame result |
| Existing frame thresholds, all selected labels | 0.5445 | 100% | Different multi-label decision rule; not directly interchangeable with dominant-only output |
| Frames with uniform 0.35 threshold | 0.5490 | 97.12% | Uncertain outcome when no label clears the threshold |

The stance threshold changes do not fix its class confusion and sharply reduce coverage. The frame experiment shows that dominant-only evaluation hides multi-label behavior, but the best tested result still falls below the provisional readiness target. Nine `environment` labels lie outside the classifier's seven-label vocabulary; they remain in the rows and are reported as out-of-ontology annotations rather than silently remapped or discarded. Source breakdowns are reported; missing domain annotations remain `unknown`.

The evidence file `docs/development/workflow-implementation-evidence/mining-configurations.json` contains per-class precision/recall, confusion counts, exact label-set accuracy, selective accuracy, coverage, Brier scores and calibration bins. Frame calibration is also reported per label; the separate maximum-frame-score/exact-set diagnostic is not a calibrated multi-label probability. The existing stance result differs from the earlier rounded value by 0.0001 because these diagnostics average unrounded class metrics.

Provisional absolute readiness criteria are macro F1 ≥0.70 for stance / ≥0.65 for frames, per-class recall ≥0.70 / ≥0.50, and coverage ≥80%, followed by independent domain validation. These are explicit engineering targets, separate from a relative regression gate. Passing an old regression gate does not establish readiness. No candidate is marked task-ready or selected for production.

The cold run used two PyTorch CPU threads: stance scoring took 213.62 seconds and frame scoring 332.80 seconds. It made 1,904 uncached per-example scoring calls, reusing identical hypothesis pairs within the fixed dataset. External inference charges were zero; local compute was not priced. Per-frame calibration was later calculated from those persisted scores without new model calls; the original timing measurements are retained. The report pins source files and model/runtime identities.

Reproduce with:

```sh
python scripts/evaluate_mining_configurations.py --out report.json --cache /tmp/noesis-mining-scores.json
```

The cache records scores keyed by exact pairs and pinned runtime identity. The output states how much inference was reused. Threshold candidates are declared before evaluation; none were fitted to this test split. This compares configurations of the configured pinned NLI model, not every available model family.

EX-05 remains uncollected: the existing benchmark is not the requested 500–1,000 independently annotated real sentences. Use the existing `scripts/human_annotation.py` prediction-blind assignments, agreement/adjudication and frozen-split workflow with two actual annotators. EX-06/07 human validation, review-effort effectiveness, and substantive answer/report-support validation remain dependent on those external judgments. No human labels or reviewers were fabricated.
