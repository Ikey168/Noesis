"""Keep stance/frame transfer diagnostics separate from human-labeled NLI scores."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.runtime_jobs import execute_job


def prepare():
    import duckdb

    from src.argument_mining.frames import FrameClassifier
    from src.argument_mining.models import StanceClassifier

    probes, captures = {}, {}
    db = duckdb.connect(config={"threads": 1, "memory_limit": "256MB"})
    try:
        for task, templates in (
            ("stance", StanceClassifier.NLI_TEMPLATES),
            ("frames", FrameClassifier.NLI_TEMPLATES),
        ):
            path = (
                Path(__file__).resolve().parents[1]
                / "data/argument_mining"
                / (task + ".parquet")
            )
            captures[task] = {
                "path": str(path.relative_to(Path(__file__).resolve().parents[1])),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "split": "test",
            }
            cursor = db.execute(
                "SELECT * FROM read_parquet(?) WHERE split='test'", [str(path)]
            )
            columns = [c[0] for c in cursor.description]
            probes[task] = []
            for native in cursor.fetchall():
                row = dict(zip(columns, native, strict=True))
                labels = (
                    [row["stance"]]
                    if task == "stance"
                    else json.loads(row["frames"])
                    if isinstance(row["frames"], str)
                    else row["frames"]
                )
                if not set(labels) <= set(templates):
                    continue
                probes[task].append(
                    {
                        "id": row["id"],
                        "text": row["text"],
                        "topic": row.get("topic", "the issue"),
                        "labels": labels,
                        "source_type": row["source_type"],
                    }
                )
                if len(probes[task]) == 6:
                    break
            if len(probes[task]) != 6:
                raise ValueError("six unchanged held-out task probes required")
    finally:
        db.close()
    return captures, probes


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--xnli-data-dir", type=Path)
    args = parser.parse_args()
    captures, probes = prepare()
    quote_rows = []
    if args.xnli_data_dir:
        from src.evaluation.published_nli import prepare_quote_probe

        quote_rows = prepare_quote_probe(args.xnli_data_dir)
    report = {
        "contract": "noesis-nli-task-transfer-v1",
        "source_captures": captures,
        "selection": "First six test records per task whose labels are in the existing ontology; unchanged source text/targets. Not a representative quality estimate.",
        "label_origin": "existing benchmark, not independently collected EX-05 human labels",
        "runs": {},
        "decision": "Defer stance/frame replacement; do not transfer NLI calibration thresholds or treat NLI accuracy as task accuracy.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for kind in ("baseline", "mdeberta"):
        report["runs"][kind] = execute_job(
            "nli-benchmark",
            {"backend": kind, "rows": quote_rows, "task_probes": probes},
            timeout_s=300,
            max_rss_bytes=3 * 1024**3,
        )
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(kind, report["runs"][kind]["status"], flush=True)
