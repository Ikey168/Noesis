"""Native GLiNER relation extraction on a bounded, separately labelled test set."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.evaluation.published_relations import prepare, score_relations
from src.evaluation.runtime_jobs import execute_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source, rows = prepare(args.data)
    job = execute_job(
        "ner-benchmark",
        {"backend": "gliner2", "rows": rows},
        timeout_s=600,
        max_rss_bytes=4 * 1024**3,
    )
    report = {
        "contract": "noesis-published-relation-comparison-v1",
        "source": source,
        "job": job,
        "selection": "First ten positive politics test sentences at most forty tokens with both native flags false; all fifteen relation labels supplied for every sentence",
        "limitations": [
            "Small English-only positive selection; not a German relation evaluation",
            "Native hyphenated relation names map reversibly to underscore schema identifiers",
            "Source explanation/flags are preserved; no label-presence oracle or gold entity spans provided to inference",
            "Source tokens joined with spaces; original article whitespace unavailable",
            "No relation score is interpreted as evidence truth; public training contamination unknown",
        ],
        "decision": "Defer relation adoption; judge independently from entity extraction.",
    }
    if job["status"] == "completed":
        actual = job["result"]["rows"]
        report["summary"] = score_relations(
            [{**r, "source_id": row["id"]} for row in actual for r in row["predicted"]],
            [{**r, "source_id": row["id"]} for row in actual for r in row["expected"]],
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(job["status"], flush=True)


if __name__ == "__main__":
    main()
