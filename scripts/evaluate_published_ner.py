"""Run cached native entity backends against immutable published test annotations."""

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.evaluation.published_ner import prepare, prepare_politics
from src.evaluation.runtime_jobs import execute_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--gliner-python", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--politics-schema", action="store_true")
    args = parser.parse_args()
    sources, selection, rows = (prepare_politics if args.politics_schema else prepare)(
        args.data_dir
    )
    report = {
        "contract": "noesis-published-ner-comparison-v1",
        "sources": sources,
        "selection": selection,
        "configuration": {
            "timeout_s": 600,
            "max_rss_bytes": 4 * 1024**3,
            "threads": 2,
            "threshold": "GLiNER2 default 0.5",
        },
        "limitations": [
            "Selected positive test sentences within a shared person/organisation/location ontology; not full benchmark scores",
            "Character coordinates use space-joined published tokens; original article whitespace is not available",
            "GermEval nested spans are retained; compound/other ontology sentences excluded before inference",
            "CrossNER specialised politician/party/country tags are explicitly mapped to shared entity types",
            "No training or threshold tuning; public benchmark training contamination is unknown",
            "Authorities, funding programmes, legal references, relations and structured-field readiness are not established by these three-class labels",
            "Does not replace the specified independent Noesis human collection",
        ],
        "runs": {},
    }
    if args.politics_schema:
        report["limitations"] = [
            "Twenty English politics test sentences; all nine original domain labels retained without collapsing types",
            "No baseline claims for specialised labels the actor extractor does not support; shared-ontology comparison is separate",
            "Space-joined published token coordinates; no claim of original article whitespace",
            "No relation or structured-field human labels in this dataset; those quality tasks remain unsupported",
            "No parameter fitting; public benchmark training contamination is not excluded",
        ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for kind in (
        ("gliner2",)
        if args.politics_schema
        else ("metadata-spacy", "language-spacy", "gliner2")
    ):
        key = "NOESIS_OPTIONAL_PYTHON_NER_BENCHMARK"
        original = os.environ.get(key)
        old_path = None
        try:
            if kind == "gliner2":
                os.environ[key] = str(args.gliner_python.absolute())
                # The dedicated Python 3.12 environment must not inherit 3.14 binaries.
                old_path = os.environ.pop("PYTHONPATH", None)
            else:
                old_path = None
            job = execute_job(
                "ner-benchmark",
                {"backend": kind, "rows": rows},
                timeout_s=600,
                max_rss_bytes=4 * 1024**3,
            )
        finally:
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original
            if old_path is not None:
                os.environ["PYTHONPATH"] = old_path
        entry = {"job": job}
        if job["status"] == "completed":
            entry["summary"] = {}
            for language in sorted({r["language"] for r in rows}):
                records = [
                    r for r in job["result"]["rows"] if r["language"] == language
                ]
                gold = {
                    (r["id"], e["start"], e["end"], e["label"])
                    for r in records
                    for e in r["expected"]
                }
                predicted = {
                    (r["id"], e["start"], e["end"], e["label"])
                    for r in records
                    for e in r["predicted"]
                }
                tp = len(gold & predicted)
                precision = tp / len(predicted) if predicted else 0
                recall = tp / len(gold) if gold else 0
                times = sorted(r["elapsed_s"] for r in records)
                entry["summary"][language] = {
                    "n": len(records),
                    "gold_spans": len(gold),
                    "predicted_spans": len(predicted),
                    "correct_spans": tp,
                    "precision": precision,
                    "recall": recall,
                    "f1": 2 * precision * recall / (precision + recall)
                    if precision + recall
                    else 0,
                    "p50_s": statistics.median(times),
                    "p95_s": times[math.ceil(len(times) * 0.95) - 1],
                }
        report["runs"][kind] = entry
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(kind, job["status"], flush=True)
    report["decision"] = (
        "Keep optional extraction backends opt-in; a small shared-ontology evaluation does not certify domain schema readiness."
    )
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
