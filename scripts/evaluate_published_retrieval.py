"""Bounded, offline retrieval comparison using published test-set judgments.

This is a selected short-passage diagnostic, not the Noesis human collection or
the full GermanDPR/BEIR benchmark. Downloads and model acquisition are explicit
prerequisites; no dataset code or network access is needed here.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import platform
import statistics
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.benchmark_runtime import score_output
from src.evaluation.runtime_jobs import execute_job

SOURCES = {
    "germandpr-test.parquet": {
        "sha256": "f6106a22d6d6a6cab1621d70a06e0edd8edea9d1428e52424b1610f83b6228f0",
        "url": "https://huggingface.co/datasets/deepset/germandpr/resolve/d0b867a49c45a44d172041351b5df73a87714372/plain_text/test/0000.parquet",
        "license": "CC-BY-4.0",
        "attribution": "Möller, Risch and Pietsch (2021), GermanQuAD and GermanDPR; deepset and Wikipedia contributors",
    },
    "scifact.zip": {
        "sha256": "536e14446a0ba56ed1398ab1055f39fe852686ecad24a6306c80c490fa8e0165",
        "url": "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/scifact.zip",
        "license": "CC-BY-SA-4.0 declared by the BeIR/scifact dataset card; underlying abstracts retain their source rights",
        "attribution": "Wadden et al. (2020), Fact or Fiction: Verifying Scientific Claims; BEIR distribution",
    },
}


def verified_bytes(directory, name):
    path = directory / name
    if path.stat().st_size > 20 * 1024**2:
        raise ValueError("dataset input exceeds 20 MiB")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != SOURCES[name]["sha256"]:
        raise ValueError("published dataset digest mismatch")
    return raw


def prepare(directory, fits):
    """Select before inference; never shorten passages or alter supplied labels."""
    import duckdb

    verified_bytes(directory, "germandpr-test.parquet")
    documents, queries = {}, []

    def add(text, source, native_id):
        revision = hashlib.sha256(text.encode()).hexdigest()
        identity = source + ":" + str(native_id)
        documents[identity] = {
            "id": identity,
            "revision": revision,
            "text": text,
            "source": source,
        }
        return identity

    conn = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
    try:
        rows = conn.execute(
            "SELECT question, positive_ctxs, hard_negative_ctxs FROM read_parquet(?)",
            [str(directory / "germandpr-test.parquet")],
        ).fetchall()
    finally:
        conn.close()
    for row_number, (query, positive, negative) in enumerate(rows):
        if not positive["text"] or not all(fits(t) for t in [query, *positive["text"]]):
            continue
        kept_negatives = [t for t in negative["text"] if fits(t)]
        texts = positive["text"] + kept_negatives
        judgments = {}
        for i, text in enumerate(texts):
            identity = add(text, "germandpr", hashlib.sha256(text.encode()).hexdigest())
            judgments[identity] = max(
                judgments.get(identity, 0), int(i < len(positive["text"]))
            )
        queries.append(
            {
                "id": f"germandpr:test:{row_number}",
                "omitted_overlong_hard_negatives": len(negative["text"])
                - len(kept_negatives),
                "language": "de",
                "text": query,
                "judgments": judgments,
            }
        )
        if len(queries) == 8:
            break
    raw = verified_bytes(directory, "scifact.zip")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:

        def read(name):
            info = archive.getinfo("scifact/" + name)
            if info.file_size > 20 * 1024**2:
                raise ValueError("dataset member exceeds expansion budget")
            return archive.read(info).decode("utf-8")

        corpus = {
            r["_id"]: r for r in map(json.loads, read("corpus.jsonl").splitlines())
        }
        native_queries = {
            r["_id"]: r["text"]
            for r in map(json.loads, read("queries.jsonl").splitlines())
        }
        qrels = {}
        for row in csv.DictReader(io.StringIO(read("qrels/test.tsv")), delimiter="\t"):
            qrels.setdefault(row["query-id"], {})[row["corpus-id"]] = int(row["score"])
    english = 0
    for qid in sorted(qrels, key=int):
        judgments = qrels[qid]
        texts = [corpus[i]["title"] + "\n" + corpus[i]["text"] for i in judgments]
        if not all(fits(t) for t in [native_queries[qid], *texts]):
            continue
        selected = {
            add(text, "scifact", i): judgments[i]
            for i, text in zip(judgments, texts, strict=True)
        }
        queries.append(
            {
                "id": "scifact:test:" + qid,
                "language": "en",
                "text": native_queries[qid],
                "judgments": selected,
            }
        )
        english += 1
        if english == 8:
            break
    # Fixed additional distractors; no retrieval model chooses the test pool.
    for identity in sorted(corpus, key=int):
        text = corpus[identity]["title"] + "\n" + corpus[identity]["text"]
        if fits(text):
            add(text, "scifact", identity)
        if len(documents) >= 96:
            break
    if len(queries) != 16 or len(documents) != 96:
        raise ValueError(
            f"frozen selection yielded {len(queries)} queries/{len(documents)} documents; required 16/96"
        )
    return {"documents": list(documents.values()), "queries": queries}


def verify_metrics(report):
    """Independently check the locally reported binary relevance metrics."""
    import importlib.metadata

    import ir_measures

    checked = 0
    for row in report["runs"]:
        if "metrics" not in row:
            continue
        qid = row["query_id"]
        qrels = [
            ir_measures.Qrel(qid, identity, grade)
            for identity, grade in row["judgments"].items()
        ]
        results = row["job"]["result"]["results"]
        run = [
            ir_measures.ScoredDoc(qid, result["id"], float(len(results) - rank))
            for rank, result in enumerate(results)
        ]
        for k in (5, 10, 30):
            measures = {
                ir_measures.R @ k: "recall_at_k",
                ir_measures.nDCG @ k: "ndcg_at_k",
                ir_measures.RR @ k: "mrr_at_k",
            }
            scores = ir_measures.calc_aggregate(list(measures), qrels, run)
            for measure, field in measures.items():
                if abs(scores[measure] - row["metrics"][str(k)][field]) > 1e-9:
                    raise ValueError("native metric disagrees with ir-measures")
                checked += 1
    return {
        "package": "ir-measures",
        "version": importlib.metadata.version("ir-measures"),
        "checked_scores": checked,
        "agreement_tolerance": 1e-9,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    from transformers import AutoTokenizer

    from src.evaluation.model_backends import model_path

    tokenizers = {
        name: AutoTokenizer.from_pretrained(
            model_path(name)[0], local_files_only=True, trust_remote_code=False
        )
        for name in ("minilm", "e5")
    }

    def fits(text):
        return len(tokenizers["minilm"].encode(text, truncation=False)) <= 256 and all(
            len(tokenizers["e5"].encode(prefix + text, truncation=False)) <= 512
            for prefix in ("query: ", "passage: ")
        )

    corpus = prepare(args.data_dir, fits)
    frozen = json.dumps(corpus, sort_keys=True, ensure_ascii=False).encode()
    report = {
        "contract": "noesis-published-retrieval-diagnostic-v1",
        "sources": SOURCES,
        "corpus_sha256": hashlib.sha256(frozen).hexdigest(),
        "document_revisions": [
            {k: v for k, v in row.items() if k != "text"} for row in corpus["documents"]
        ],
        "selection": "First eight test queries per language whose complete positive passages fit both pinned tokenizers; omit and count overlong GermanDPR hard negatives. Add ascending-ID short SciFact distractors to 96 documents. No truncation or output-dependent selection.",
        "limitations": [
            "Small length-filtered subset, not full-dataset benchmark scores",
            "GermanDPR negatives are mined; only positive answer contexts derive from human annotation",
            "Unjudged pooled passages count as nonrelevant for scoring; relevance is incomplete",
            "Published test sets may occur in model training; independence from model training is not established",
            "No independently translated German-to-English queries",
            "SciFact relevance can include supporting or refuting evidence; retrieval scores do not establish entailment",
            "No Noesis-specific legal/Berlin coverage certificate or replacement for issue 1420",
            "Isolated exact semantic index; not a deployed hybrid-path benchmark",
        ],
        "hardware": {
            "platform": platform.platform(),
            "processor": platform.processor(),
        },
        "runs": [],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for backend in ("minilm", "e5"):
        for query in corpus["queries"]:
            job = execute_job(
                "retrieval-" + backend,
                {
                    "backend": backend,
                    "documents": corpus["documents"],
                    "query": query["text"],
                    "limit": 30,
                },
                timeout_s=120,
                max_rss_bytes=3 * 1024**3,
            )
            row = {
                "backend": backend,
                "query_id": query["id"],
                "language": query["language"],
                "judgments": query["judgments"],
                "omitted_overlong_hard_negatives": query.get(
                    "omitted_overlong_hard_negatives", 0
                ),
                "job": job,
            }
            if job["status"] == "completed":
                row["metrics"] = {
                    str(k): score_output(
                        "ranking",
                        job["result"]["results"],
                        query["judgments"],
                        {"k": k},
                    )
                    for k in (5, 10, 30)
                }
            report["runs"].append(row)
            args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            print(backend, query["id"], job["status"], flush=True)
    report["summary"] = {}
    for backend in ("minilm", "e5"):
        for language in ("de", "en"):
            rows = [
                r
                for r in report["runs"]
                if r["backend"] == backend
                and r["language"] == language
                and "metrics" in r
            ]
            key = backend + ":" + language
            report["summary"][key] = {"completed": len(rows)}
            if rows:
                latencies = sorted(r["job"]["result"]["query_seconds"] for r in rows)
                report["summary"][key].update(
                    query_p50_s=statistics.median(latencies),
                    query_p95_s=latencies[-1],
                    metrics={
                        str(k): {
                            metric: statistics.mean(
                                r["metrics"][str(k)][metric] for r in rows
                            )
                            for metric in ("recall_at_k", "ndcg_at_k", "mrr_at_k")
                        }
                        for k in (5, 10, 30)
                    },
                )
    finalize(report)
    report["decision"] = (
        "Defer production adoption: restricted published-data diagnostic cannot establish remaining cross-language/domain acceptance."
    )
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


def finalize(report):
    """Add verification and resource summaries without rerunning inference."""
    import importlib.metadata
    import os

    import psutil

    report["metric_verification"] = verify_metrics(report)
    report["hardware"].update(
        logical_cpus=os.cpu_count(),
        total_ram_bytes=psutil.virtual_memory().total,
        device="cpu",
        worker_threads=2,
    )
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        with cpuinfo.open() as stream:
            report["hardware"]["cpu_model"] = next(
                (
                    line.split(":", 1)[1].strip()
                    for line in stream.read(32768).splitlines()
                    if line.startswith("model name")
                ),
                "unavailable",
            )
    report["corpus_document_counts"] = {
        source: sum(r["source"] == source for r in report["document_revisions"])
        for source in ("germandpr", "scifact")
    }
    report["omitted_overlong_germandpr_hard_negatives"] = sum(
        r.get("omitted_overlong_hard_negatives", 0)
        for r in report["runs"]
        if r["backend"] == "minilm"
    )
    report["dependency_versions"] = {
        name: importlib.metadata.version(name)
        for name in (
            "torch",
            "transformers",
            "sentence-transformers",
            "duckdb",
            "ir-measures",
        )
    }
    report["resource_summary"] = {}
    for backend in ("minilm", "e5"):
        rows = [
            r["job"]
            for r in report["runs"]
            if r["backend"] == backend and r["job"]["status"] == "completed"
        ]
        if not rows:
            continue
        report["resource_summary"][backend] = {
            "peak_worker_tree_rss_bytes": max(
                r["peak_process_tree_rss_bytes"] for r in rows
            ),
            "median_model_load_s": statistics.median(
                r["result"]["model_load_seconds"] for r in rows
            ),
            "median_corpus_encoding_s": statistics.median(
                r["result"]["corpus_encoding_seconds"] for r in rows
            ),
            "median_encoding_documents_per_second": statistics.median(
                96 / r["result"]["corpus_encoding_seconds"] for r in rows
            ),
            "median_index_construction_s": statistics.median(
                r["result"]["indexing_seconds"] for r in rows
            ),
            "serialized_index_bytes": sorted(
                {r["result"]["serialized_index_bytes"] for r in rows}
            ),
            "query_timing": "warm encoding plus exact search; excludes cold worker/model load and corpus/index construction",
            "p95_method": "nearest-rank; maximum for each eight-query language group",
            "migration": "isolated in-memory revisioned index; production indexes untouched; discard to roll back",
        }
    return report


if __name__ == "__main__":
    main()
