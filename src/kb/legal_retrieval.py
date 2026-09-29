"""Legal retrieval evaluation protocol and opt-in mode gating (#1727).

Legal-specific retrieval modes stay opt-in until an independent, human-judged,
held-out evaluation meets the thresholds in ``config/legal/retrieval_modes.json``.
This module is the gate, not the evidence:

* :func:`validate_judgments` rejects labels that are not independent human
  relevance judgments (model output, exact-term membership, publisher
  identifiers) and requires an assessor protocol, at least two assessors per
  query and adjudication of every disagreement.
* :func:`evaluate` computes recall@k, nDCG@k, MRR and supported-passage
  precision per mode, stratified by jurisdiction, language and version, plus
  latency percentiles and baseline comparisons.
* :func:`decide` records a measured adopt/defer decision per mode.

No human judgments are shipped with the repository, so every relevance-ranked
mode is ``deferred``.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

CONFIG = Path(__file__).resolve().parents[2] / "config/legal/retrieval_modes.json"
EVALUATION_CONTRACT = "noesis-legal-retrieval-evaluation-v1"
ACCEPTED_LABEL_ORIGINS = frozenset({"human-assessor"})
REJECTED_LABEL_ORIGINS = frozenset({"model-generated", "exact-term-membership", "publisher-identifier-lookup",
                                    "synthetic"})
BASELINES = ("exact-identifier", "keyword")
STRATA = ("jurisdiction", "language", "version")


class RetrievalEvaluationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def retrieval_modes() -> dict[str, Any]:
    return json.loads(CONFIG.read_text())


def require_enabled(mode: str) -> None:
    state = retrieval_modes()["modes"].get(mode, {}).get("state")
    if state != "enabled":
        raise RetrievalEvaluationError(
            "mode_not_enabled",
            f"legal retrieval mode {mode!r} is {state or 'unknown'}; it needs an accepted human-judged evaluation",
        )


def validate_judgments(queries: Sequence[Mapping[str, Any]], judgments: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Check provenance, coverage and agreement; return adjudicated labels per query."""

    by_query = {str(q["query_id"]): q for q in queries}
    for query in queries:
        missing = [s for s in STRATA if not query.get(s)]
        if missing:
            raise RetrievalEvaluationError("unstratified_query", f"query {query['query_id']} lacks {missing}")
        if query.get("split") not in {"held-out", "development"}:
            raise RetrievalEvaluationError("unsplit_query", f"query {query['query_id']} needs a held-out/development split")
    grouped: dict[str, dict[str, dict[str, int]]] = defaultdict(lambda: defaultdict(dict))
    adjudicated: dict[str, dict[str, int]] = defaultdict(dict)
    for item in judgments:
        origin = str(item.get("label_origin") or "")
        if origin in REJECTED_LABEL_ORIGINS or origin not in ACCEPTED_LABEL_ORIGINS:
            raise RetrievalEvaluationError(
                "invalid_label_origin", f"label origin {origin!r} is not an independent human relevance judgment")
        if not item.get("protocol_version") or not item.get("assessor_id"):
            raise RetrievalEvaluationError("missing_protocol", "every judgment names its assessor and protocol version")
        query_id = str(item["query_id"])
        if query_id not in by_query:
            raise RetrievalEvaluationError("unknown_query", f"judgment for unknown query {query_id}")
        label = int(item["label"])
        if label not in {0, 1, 2}:
            raise RetrievalEvaluationError("invalid_label", "labels are 0 (not relevant), 1 (related), 2 (supporting)")
        if item.get("adjudication"):
            adjudicated[query_id][str(item["passage_id"])] = label
        else:
            grouped[query_id][str(item["assessor_id"])][str(item["passage_id"])] = label
    labels: dict[str, dict[str, int]] = {}
    agreements, disagreements = [], []
    for query_id in by_query:
        assessors = grouped.get(query_id, {})
        if len(assessors) < 2:
            raise RetrievalEvaluationError("too_few_assessors", f"query {query_id} needs at least two assessors")
        passages = sorted({p for a in assessors.values() for p in a})
        merged = {}
        for passage in passages:
            values = [a.get(passage) for a in assessors.values() if passage in a]
            if len(set(values)) > 1:
                if passage not in adjudicated.get(query_id, {}):
                    raise RetrievalEvaluationError(
                        "unadjudicated_disagreement", f"query {query_id} passage {passage} needs adjudication")
                disagreements.append({"query_id": query_id, "passage_id": passage, "labels": values,
                                      "adjudicated": adjudicated[query_id][passage]})
                merged[passage] = adjudicated[query_id][passage]
            else:
                merged[passage] = values[0]
        first, second = list(assessors.values())[:2]
        shared = sorted(set(first) & set(second))
        agreements.append(_kappa([first[p] > 0 for p in shared], [second[p] > 0 for p in shared]))
        labels[query_id] = merged
    return {"labels": labels, "cohen_kappa_mean": round(sum(agreements) / len(agreements), 4) if agreements else None,
            "disagreements": disagreements}


def _kappa(left: Sequence[bool], right: Sequence[bool]) -> float:
    n = len(left)
    if n == 0:
        return 0.0
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / n
    p_left, p_right = sum(left) / n, sum(right) / n
    expected = p_left * p_right + (1 - p_left) * (1 - p_right)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def _metrics(ranked: Sequence[str], labels: Mapping[str, int], k: int) -> dict[str, float]:
    relevant = {p for p, label in labels.items() if label > 0}
    top = list(ranked)[:k]
    recall = len(relevant & set(top)) / len(relevant) if relevant else 0.0
    dcg = sum((2 ** labels.get(p, 0) - 1) / math.log2(i + 2) for i, p in enumerate(top))
    ideal = sorted(labels.values(), reverse=True)[:k]
    idcg = sum((2 ** label - 1) / math.log2(i + 2) for i, label in enumerate(ideal))
    rr = next((1 / (i + 1) for i, p in enumerate(ranked) if p in relevant), 0.0)
    top5 = list(ranked)[:5]
    supported = sum(labels.get(p, 0) == 2 for p in top5) / len(top5) if top5 else 0.0
    return {"recall_at_k": recall, "ndcg_at_k": dcg / idcg if idcg else 0.0, "mrr": rr,
            "supported_passage_precision_at_5": supported}


def evaluate(queries: Sequence[Mapping[str, Any]], judgments: Sequence[Mapping[str, Any]],
             runs: Mapping[str, Mapping[str, Any]], *, k: int = 10, split: str = "held-out") -> dict[str, Any]:
    """Score each mode's ranked passages on held-out human judgments, overall and per stratum."""

    missing = [b for b in BASELINES if b not in runs]
    if missing:
        raise RetrievalEvaluationError("missing_baseline", f"runs must include baselines {missing}")
    checked = validate_judgments(queries, judgments)
    selected = [q for q in queries if q["split"] == split]
    if not selected:
        raise RetrievalEvaluationError("no_held_out_queries", f"no {split} queries to evaluate")
    results = {}
    for mode, run in runs.items():
        per_query, strata = {}, defaultdict(list)
        for query in selected:
            qid = str(query["query_id"])
            metrics = _metrics(list(run["rankings"].get(qid) or []), checked["labels"][qid], k)
            per_query[qid] = metrics
            for dimension in STRATA:
                strata[f"{dimension}={query[dimension]}"].append(metrics)
        latencies = sorted(float(v) for v in (run.get("latency_ms") or {}).values())
        results[mode] = {
            "overall": _mean(list(per_query.values())),
            "strata": {name: {**_mean(values), "queries": len(values)} for name, values in sorted(strata.items())},
            "p95_latency_ms": latencies[max(0, math.ceil(0.95 * len(latencies)) - 1)] if latencies else None,
            "resources": dict(run.get("resources") or {}),
        }
    return {"contract": EVALUATION_CONTRACT, "k": k, "split": split, "queries": len(selected),
            "cohen_kappa_mean": checked["cohen_kappa_mean"], "disagreements": len(checked["disagreements"]),
            "modes": results}


def _mean(values: Sequence[Mapping[str, float]]) -> dict[str, float]:
    keys = ("recall_at_k", "ndcg_at_k", "mrr", "supported_passage_precision_at_5")
    return {key: round(sum(v[key] for v in values) / len(values), 4) if values else 0.0 for key in keys}


def decide(evaluation: Mapping[str, Any], thresholds: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Adopt a mode only when every threshold and the baseline margin hold overall and per stratum."""

    limits = dict(thresholds or retrieval_modes()["thresholds"])
    keyword = evaluation["modes"]["keyword"]["overall"]
    decisions = {}
    for mode, result in evaluation["modes"].items():
        if mode in BASELINES:
            continue
        reasons = []
        if (evaluation.get("cohen_kappa_mean") or 0) < limits["min_cohen_kappa"]:
            reasons.append("assessor agreement below threshold")
        overall = result["overall"]
        for metric, limit in (("recall_at_k", "recall_at_10"), ("ndcg_at_k", "ndcg_at_10"),
                              ("supported_passage_precision_at_5", "supported_passage_precision_at_5")):
            if overall[metric] < limits[limit]:
                reasons.append(f"{metric} {overall[metric]} < {limits[limit]}")
            for name, stratum in result["strata"].items():
                if stratum["queries"] >= limits["min_queries_per_stratum"] and stratum[metric] < limits[limit]:
                    reasons.append(f"{name}: {metric} {stratum[metric]} < {limits[limit]}")
        if any(s["queries"] < limits["min_queries_per_stratum"] for s in result["strata"].values()):
            reasons.append("a stratum has too few held-out queries")
        if overall["ndcg_at_k"] - keyword["ndcg_at_k"] < limits["min_gain_over_keyword_baseline"]:
            reasons.append("gain over the keyword baseline is below the margin")
        if result["p95_latency_ms"] is not None and result["p95_latency_ms"] > limits["max_p95_latency_ms"]:
            reasons.append("p95 latency above the limit")
        decisions[mode] = {"decision": "adopt" if not reasons else "defer", "reasons": reasons}
    return {"thresholds": limits, "decisions": decisions}
