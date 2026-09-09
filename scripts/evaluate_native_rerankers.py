"""Rerank a frozen MiniLM candidate pool with both pinned native models."""

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.evaluate_cross_language_retrieval import add_cross_language
from scripts.evaluate_published_retrieval import prepare, verify_metrics
from src.evaluation.published_nli import prepare as prepare_nli
from src.evaluation.runtime_jobs import execute_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval-data-dir", type=Path, required=True)
    parser.add_argument("--xnli-data-dir", type=Path, required=True)
    parser.add_argument("--retrieval-report", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--candidate-count", type=int, choices=[10, 30], default=30)
    parser.add_argument("--batch-size", type=int, choices=[1, 2, 4], default=2)
    parser.add_argument("--queries-per-direction", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    from transformers import AutoTokenizer

    from src.evaluation.model_backends import model_path

    tokenizers = {
        k: AutoTokenizer.from_pretrained(
            model_path(k)[0], local_files_only=True, trust_remote_code=False
        )
        for k in ("minilm", "e5")
    }

    def fits(text):
        return len(tokenizers["minilm"].encode(text, truncation=False)) <= 256 and all(
            len(tokenizers["e5"].encode(p + text, truncation=False)) <= 512
            for p in ("query: ", "passage: ")
        )

    _, rows = prepare_nli(args.xnli_data_dir)
    corpus = add_cross_language(prepare(args.retrieval_data_dir, fits), rows, fits)
    retrieval = json.loads(args.retrieval_report.read_text())
    baseline = retrieval["runs"]["minilm"]["job"]["result"]
    import hashlib

    digest = hashlib.sha256(
        json.dumps(corpus["documents"], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if digest != baseline["corpus_sha256"]:
        raise ValueError("frozen corpus differs from candidate retrieval run")
    query_digest = hashlib.sha256(
        json.dumps(corpus["queries"], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    if query_digest != baseline["query_set_sha256"]:
        raise ValueError("frozen query set differs from candidate retrieval run")
    by_id = {r["id"]: r for r in corpus["documents"]}
    rankings = {r["query_id"]: r for r in baseline["runs"]}
    queries = []
    for direction in ("de-de", "en-en", "de-en"):
        for query in [r for r in corpus["queries"] if r["language_pair"] == direction][
            : args.queries_per_direction
        ]:
            ranking = rankings[query["id"]]["job"]["result"]["results"]
            queries.append(
                {
                    **query,
                    "candidates": [
                        {**by_id[r["id"]], "retrieval_score": r["score"]}
                        for r in ranking[: args.candidate_count]
                    ],
                }
            )
    report = {
        "contract": "noesis-native-reranker-comparison-v1",
        "sources": retrieval["sources"],
        "corpus_sha256": digest,
        "selection": f"First {args.queries_per_direction} queries per direction; fixed top {args.candidate_count} MiniLM candidates without gold injection",
        "configuration": {
            "threads": 2,
            "timeout_s": 600,
            "max_worker_rss_bytes": 4 * 1024**3,
            "batch_size": args.batch_size,
        },
        "baseline_runs": [rankings[q["id"]] for q in queries],
        "runs": {},
        "limitations": retrieval["limitations"]
        + [
            "Small query subset; native scores are not calibrated and relevance is not factual support"
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for kind in ("minilm-reranker", "qwen3-reranker"):
        job = execute_job(
            "reranker-benchmark",
            {"backend": kind, "queries": queries, "batch_size": args.batch_size},
            timeout_s=600,
            max_rss_bytes=4 * 1024**3,
        )
        entry = {"job": job}
        if job["status"] == "completed":
            result = job["result"]
            entry["metric_verification"] = verify_metrics(result)
            entry["fusion_metric_verification"] = verify_metrics(
                {
                    "runs": [
                        {
                            "query_id": row["query_id"],
                            "judgments": row["judgments"],
                            "job": {
                                "result": {"results": row["legacy_fusion"]["results"]}
                            },
                            "metrics": row["legacy_fusion"]["metrics"],
                        }
                        for row in result["runs"]
                        if row["legacy_fusion"] is not None
                    ]
                }
            )
            times = sorted(r["query_s"] for r in result["runs"])
            entry["summary"] = {
                "p50_s": statistics.median(times),
                "p95_s": times[math.ceil(len(times) * 0.95) - 1],
                "pairs_per_second": sum(r["candidate_count"] for r in result["runs"])
                / sum(times),
            }
        report["runs"][kind] = entry
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(kind, job["status"], flush=True)
    report["decision"] = (
        "Keep reranking opt-in; this bounded fixed-candidate experiment does not establish deployment readiness."
    )
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
