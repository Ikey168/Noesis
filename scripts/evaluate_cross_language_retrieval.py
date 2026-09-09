"""DE/EN retrieval with explicitly derived human-translation support judgments."""

import argparse
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_published_retrieval import SOURCES, prepare, verify_metrics
from src.evaluation.published_nli import prepare as prepare_nli
from src.evaluation.runtime_jobs import execute_job


def add_cross_language(corpus, nli_rows, fits):
    documents = list(corpus["documents"])
    queries = [
        {**r, "language_pair": r["language"] + "-" + r["language"]}
        for r in corpus["queries"]
    ]
    translated = {r["id"]: r for r in nli_rows}
    selected = set()
    for row in nli_rows:
        if (
            row["split"] != "test"
            or row["language"] != "de"
            or row["labels"] != ["entailment"]
            or row["group_id"] in selected
        ):
            continue
        english = translated[row["id"].replace(":de:", ":en:")]
        if any(
            r.get("label_origin") != "independent-human"
            or r.get("source") != "facebook/xnli"
            for r in (row, english)
        ):
            raise ValueError(
                "derived relevance requires published human label provenance"
            )
        if english["group_id"] != row["group_id"] or english["labels"] != row["labels"]:
            raise ValueError(
                "cross-language judgments require aligned human translations"
            )
        text, query = english["premise"], row["hypothesis"]
        if not fits(text) or not fits(query):
            continue
        selected.add(row["group_id"])
        identity = "xnli-support:" + row["group_id"]
        documents.append(
            {
                "id": identity,
                "revision": hashlib.sha256(text.encode()).hexdigest(),
                "text": text,
                "source": "xnli",
                "native_row_id": english["id"],
            }
        )
        queries.append(
            {
                "id": row["id"] + ":de-en",
                "text": query,
                "language": "de",
                "language_pair": "de-en",
                "judgments": {identity: 1},
                "label_origin": "Supporting-passage relevance derived from published human entailment labels and aligned professional translations; not a separately annotated retrieval judgment",
                "native_query_row": row["id"],
                "native_passage_row": english["id"],
            }
        )
        if len(selected) == 8:
            break
    if len(selected) != 8:
        raise ValueError(
            "eight independent translated supporting-passage groups required"
        )
    return {"documents": documents, "queries": queries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retrieval-data-dir", type=Path, required=True)
    parser.add_argument("--xnli-data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--backends",
        nargs="+",
        choices=["minilm", "e5", "bge-m3"],
        default=["minilm", "e5"],
    )
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
            len(tokenizers["e5"].encode(prefix + text, truncation=False)) <= 512
            for prefix in ("query: ", "passage: ")
        )

    xnli_source, nli_rows = prepare_nli(args.xnli_data_dir)
    corpus = add_cross_language(prepare(args.retrieval_data_dir, fits), nli_rows, fits)
    report = {
        "contract": "noesis-cross-language-retrieval-v1",
        "sources": {"retrieval": SOURCES, "xnli": xnli_source},
        "query_provenance": [
            {k: v for k, v in r.items() if k != "text"} for r in corpus["queries"]
        ],
        "document_provenance": [
            {k: v for k, v in r.items() if k != "text"} for r in corpus["documents"]
        ],
        "configuration": {
            "worker_limits": {
                k: {
                    "timeout_s": 600 if k == "bge-m3" else 180,
                    "max_rss_bytes": (4 if k == "bge-m3" else 3) * 1024**3,
                }
                for k in args.backends
            },
            "threads": 2,
            "corpus_encoding": "once per model; subsequent queries use the same isolated in-memory index",
            "truncation": "none; full passages/queries must fit both pinned tokenizers",
        },
        "runs": {},
        "limitations": [
            "Eight queries per direction on a selected 104-document mixed corpus; not full public benchmark scores",
            "Cross-language labels derive from human NLI entailment and professional translations; measure supporting-evidence retrieval, not unrestricted search relevance",
            "Unjudged passages count as nonrelevant; relevance judgments are incomplete",
            "Public benchmark training contamination is not ruled out",
            "No new Noesis human collection or legal/Berlin task-readiness certificate",
            "Local non-commercial dataset evaluation; original data licenses remain authoritative",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for kind in args.backends:
        job = execute_job(
            "retrieval-batch",
            {"backend": kind, **corpus},
            timeout_s=600 if kind == "bge-m3" else 180,
            max_rss_bytes=(4 if kind == "bge-m3" else 3) * 1024**3,
        )
        entry = {"job": job}
        if job["status"] == "completed":
            result = job["result"]
            entry["metric_verification"] = verify_metrics(result)
            entry["summary"] = {}
            for direction, mode in [
                (d, m)
                for d in ("de-de", "en-en", "de-en")
                for m in sorted({r.get("mode", "dense") for r in result["runs"]})
            ]:
                rows = [
                    r
                    for r in result["runs"]
                    if r["language"] == direction and r.get("mode", "dense") == mode
                ]
                latencies = sorted(r["query_s"] for r in rows)
                entry["summary"][
                    direction + (":" + mode if kind == "bge-m3" else "")
                ] = {
                    "n": len(rows),
                    "p50_s": statistics.median(latencies),
                    "p95_s": latencies[math.ceil(len(latencies) * 0.95) - 1],
                    "metrics": {
                        str(k): {
                            metric: statistics.mean(
                                r["metrics"][str(k)][metric] for r in rows
                            )
                            for metric in (
                                "recall_at_k",
                                "ndcg_at_k",
                                "mrr_at_k",
                                "judged_fraction",
                            )
                        }
                        for k in (5, 10, 30)
                    },
                }
        report["runs"][kind] = entry
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(kind, job["status"], flush=True)
    report["decision"] = (
        "Keep evaluated backends opt-in; a bounded supporting-evidence diagnostic does not justify replacing production defaults or selecting a heavier model. Reindex into a separate space for evaluation and discard it to roll back."
    )
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
