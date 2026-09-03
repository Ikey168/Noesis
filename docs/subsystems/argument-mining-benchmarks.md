# Argument Mining Model Benchmarks

> Generated: 2026-09-03T13:58:55.385299+00:00
> Dataset: held-out test split (claim n=1076, stance n=1076, frame n=1076)

## Gate Status: NOT RUN

Run with `--gate` to enforce the saved baseline.

## Backend comparison

| Backend | Claims F1 | Stance macro F1 | Frames macro F1 | Active modes |
| --- | ---: | ---: | ---: | --- |
| heuristic | 0.8447 | 0.4277 | 0.4590 | heuristic |
| cached-pretrained-default | 0.9197 | 0.3288 | 0.4193 | pretrained:Nithiwat/mdeberta-v3-base_claimbuster, zero-shot:cross-encoder/nli-deberta-v3-base |

The pinned claim backend improves the internal claim F1 by 7.5 points. The
zero-shot stance backend does **not** clear the quality bar: it trails the
heuristic by 9.9 points and therefore does not resolve the model-quality gap.
Fine-tuning and promotion remain blocked on the real two-annotator gold set;
the synthetic test split is retained only for reproducible regression checks.

## Claim Detector

| Mode | Precision | Recall | F1 | Accuracy | N |
|------|-----------|--------|----|----------|---|
| pretrained:Nithiwat/mdeberta-v3-base_claimbuster | 0.9181 | 0.9213 | 0.9197 | 0.8708 | 1076 |

### Per Source Type

| Source Type | Precision | Recall | F1 | N |
|-------------|-----------|--------|-----|---|
| blog | 1.0000 | 1.0000 | 1.0000 | 150 |
| book | 0.9565 | 0.7395 | 0.8341 | 150 |
| news | 0.9858 | 0.9905 | 0.9881 | 226 |
| note | 0.7880 | 0.9243 | 0.8507 | 250 |
| paper | 0.9612 | 0.9394 | 0.9502 | 150 |
| transcript | 0.8860 | 0.8860 | 0.8860 | 150 |

### Failure Modes by Article Length

| Length Bucket | F1 | Precision | Recall | N |
|---------------|----|-----------|--------|---|
| medium | 0.9771 | 1.0000 | 0.9552 | 80 |
| short | 0.9150 | 0.9116 | 0.9184 | 996 |

## Stance Classifier

| Mode | Macro F1 | Accuracy | N |
|------|----------|----------|---|
| zero-shot:cross-encoder/nli-deberta-v3-base | 0.3288 | 0.4851 | 1076 |

### Per Class

| Class | Precision | Recall | F1 | Support |
|-------|-----------|--------|----|---------|
| supportive | 0.1877 | 0.5120 | 0.2747 | 125 |
| critical | 0.3611 | 0.0970 | 0.1529 | 134 |
| neutral | 0.6283 | 0.6121 | 0.6201 | 696 |
| ambiguous | 0.9048 | 0.1570 | 0.2676 | 121 |

### Per Source Type

| Source Type | Macro F1 | Accuracy | N |
|-------------|----------|----------|---|
| blog | 0.3652 | 0.5533 | 150 |
| book | 0.4027 | 0.4867 | 150 |
| news | 0.3207 | 0.4690 | 226 |
| note | 0.2672 | 0.5360 | 250 |
| paper | 0.2712 | 0.3800 | 150 |
| transcript | 0.2876 | 0.4600 | 150 |

### Failure Modes by Article Length

| Length | Macro F1 | Accuracy | N |
|--------|----------|----------|---|
| medium | 0.2535 | 0.3125 | 80 |
| short | 0.3363 | 0.4990 | 996 |

## Frame Classifier

| Mode | Macro F1 | Subset Accuracy | Dominant Accuracy | N |
|------|----------|-----------------|-------------------|---|
| zero-shot:cross-encoder/nli-deberta-v3-base | 0.4193 | 0.4712 | 0.4201 | 1076 |

### Per Frame Label

| Frame | Precision | Recall | F1 | Support |
|-------|-----------|--------|----|---------|
| economic | 0.8148 | 0.3173 | 0.4567 | 416 |
| security | 0.7407 | 0.3509 | 0.4762 | 57 |
| humanitarian | 0.9655 | 0.2044 | 0.3373 | 137 |
| legal | 0.8108 | 0.5357 | 0.6452 | 112 |
| political | 1.0000 | 0.0839 | 0.1548 | 286 |
| scientific | 0.9474 | 0.4235 | 0.5854 | 340 |
| other | 0.1628 | 0.9900 | 0.2797 | 100 |

### Per Source Type

| Source Type | Macro F1 | Subset Accuracy | N |
|-------------|----------|-----------------|---|
| blog | 0.1905 | 0.2067 | 150 |
| book | 0.4696 | 0.4733 | 150 |
| news | 0.6551 | 0.6991 | 226 |
| note | 0.3477 | 0.5200 | 250 |
| paper | 0.4615 | 0.5867 | 150 |
| transcript | 0.2262 | 0.1933 | 150 |

### Failure Modes by Article Length

| Length | Macro F1 | Subset Accuracy | N |
|--------|----------|-----------------|---|
| medium | 0.2004 | 0.4375 | 80 |
| short | 0.4287 | 0.4739 | 996 |

## Annotation provenance

Human evaluation status: **not_collected**.


> Synthetic labels are used only for pipeline smoke tests. The
> random-perturbation similarity numbers are not IAA or a quality gate.

## Cross-Dataset Generalisation

Claim detector (binary) evaluated against external benchmarks.

| Dataset | Precision | Recall | F1 | N | Notes |
|---------|-----------|--------|----|---|-------|
| FEVER | 0.7427 | 0.8759 | 0.8038 | 200 | SUPPORTS/REFUTES=claim; NEI=non-claim |
| LIAR | 1.0000 | 0.8900 | 0.9418 | 200 | all political claims (sanity check) |
| AVeriTeC | 1.0000 | 0.8700 | 0.9305 | 200 | verifiable real-world claims |

### External backend comparison

| Backend | Dataset | Precision | Recall | F1 | N |
| --- | --- | ---: | ---: | ---: | ---: |
| heuristic | FEVER | 0.7231 | 0.9724 | 0.8294 | 200 |
| heuristic | LIAR | 1.0000 | 0.8350 | 0.9101 | 200 |
| heuristic | AVERITEC | 1.0000 | 0.9150 | 0.9556 | 200 |
| cached-pretrained-default | FEVER | 0.7427 | 0.8759 | 0.8038 | 200 |
| cached-pretrained-default | LIAR | 1.0000 | 0.8900 | 0.9418 | 200 |
| cached-pretrained-default | AVERITEC | 1.0000 | 0.8700 | 0.9305 | 200 |

## Model Update Gate

Any model update must show ≥2% absolute F1 improvement over the
previous checkpoint stored in `docs/benchmark_results.json`.

| Model | Condition | Threshold |
|-------|-----------|-----------|
| ClaimDetector | binary F1 | ≥+2 pp |
| StanceClassifier | macro F1 | ≥+2 pp |
| FrameClassifier | macro F1 | ≥+2 pp |

Re-run `python scripts/benchmark_models.py --gate` after training to validate.

---

*Benchmarks auto-generated by `scripts/benchmark_models.py`.*
