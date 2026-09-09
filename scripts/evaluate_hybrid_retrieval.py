"""Run the actual hybrid services in a fresh, bounded local pgvector container."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.evaluate_published_retrieval import SOURCES, prepare, verify_metrics
from src.evaluation.runtime_jobs import execute_job

IMAGE = "pgvector/pgvector@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"


def summarize(report):
    job = report["job"]
    if job["status"] != "completed":
        return report
    report["metric_verification"] = verify_metrics(job["result"])
    report["summary"] = {}
    for mode in ("lexical", "semantic", "fusion", "reranked"):
        for language in sorted({r["language"] for r in job["result"]["runs"]}):
            rows = [
                r
                for r in job["result"]["runs"]
                if r["backend"] == mode and r["language"] == language
            ]
            latencies = sorted(
                r["diagnostics"]["elapsed_ms"] + 1000 * r["query_encoding_s"]
                for r in rows
            )
            report["summary"][mode + ":" + language] = {
                "queries": len(rows),
                "complete_queries": sum(
                    r["diagnostics"]["status"] == "complete" for r in rows
                ),
                "max_returned_count": max(
                    len(r["job"]["result"]["results"]) for r in rows
                ),
                "p50_ms": statistics.median(latencies),
                "p95_ms": latencies[math.ceil(len(latencies) * 0.95) - 1],
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
                "cost": {
                    "status": "local_compute_not_metered",
                    "hosted_inference_calls": 0,
                },
            }
    return report


@contextlib.contextmanager
def database():
    import psycopg2

    name = "noesis-hybrid-benchmark-" + uuid.uuid4().hex[:12]
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--rm",
            "--pull=never",
            "--name",
            name,
            "--memory",
            "512m",
            "--cpus",
            "2",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            "POSTGRES_USER=neuronews",
            "--env",
            "POSTGRES_PASSWORD=isolated-benchmark-only",
            "--env",
            "POSTGRES_DB=noesis_hybrid_benchmark",
            "--tmpfs",
            "/var/lib/postgresql/data:rw,size=268435456",
            IMAGE,
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    try:
        ports = json.loads(
            subprocess.run(
                [
                    "docker",
                    "inspect",
                    name,
                    "--format",
                    "{{json .NetworkSettings.Ports}}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout
        )
        connection = {
            "host": "127.0.0.1",
            "port": int(ports["5432/tcp"][0]["HostPort"]),
            "database": "noesis_hybrid_benchmark",
            "user": "neuronews",
            "password": "isolated-benchmark-only",
            "connect_timeout": 2,
        }
        deadline = time.monotonic() + 30
        while True:
            try:
                db = psycopg2.connect(**connection)
                break
            except psycopg2.OperationalError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.2)
        try:
            db.autocommit = True
            migrations = {}
            for filename in (
                "0001_init_pgvector.sql",
                "0002_schema_chunks.sql",
                "0003_add_indexer_columns.sql",
                "0004_fts.sql",
            ):
                raw = (
                    Path(__file__).resolve().parents[1] / "migrations" / "pg" / filename
                ).read_bytes()
                with db.cursor() as cur:
                    cur.execute(raw.decode())
                migrations[filename] = hashlib.sha256(raw).hexdigest()
            yield connection, migrations
        finally:
            db.close()
    finally:
        subprocess.run(
            ["docker", "stop", "--time", "3", name],
            capture_output=True,
            timeout=15,
            check=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--xnli-data-dir", type=Path)
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

    corpus = prepare(args.data_dir, fits)
    sources = dict(SOURCES)
    if args.xnli_data_dir:
        from scripts.evaluate_cross_language_retrieval import add_cross_language
        from src.evaluation.published_nli import prepare as prepare_nli

        sources["xnli"], nli_rows = prepare_nli(args.xnli_data_dir)
        corpus = add_cross_language(corpus, nli_rows, fits)
    with database() as (connection, migrations):
        job = execute_job(
            "retrieval-hybrid",
            {"connection": connection, "corpus": corpus},
            timeout_s=600,
            max_rss_bytes=3 * 1024**3,
        )
    report = {
        "contract": "noesis-native-hybrid-comparison-v1",
        "container_image": IMAGE,
        "container_limits": {
            "memory_bytes": 512 * 1024**2,
            "cpus": 2,
            "temporary_data_bytes": 256 * 1024**2,
        },
        "migrations": migrations,
        "sources": sources,
        "job": job,
        "container_removed": True,
        "limitations": [
            "Published short-passage subset with incomplete pooled judgments; not independent Noesis annotation",
            f"{len(corpus['documents'])} frozen documents and {len(corpus['queries'])} queries; 15 overlong mined negatives omitted",
            "Any cross-language labels are derived supporting-passage judgments from human XNLI entailment and aligned professional translation",
            "No tuning on these labels; published test-set training contamination is unknown",
            "Exercises actual services and migrations in isolated PostgreSQL, not a deployed production endpoint",
        ],
        "decision": "Defer retrieval-default changes pending representative cross-language/domain evaluation.",
    }
    summarize(report)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(job["status"], flush=True)


if __name__ == "__main__":
    main()
