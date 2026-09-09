"""Run each parser in a bounded subprocess; preserve failures and raw structure."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingestion.pdf_evaluation import score


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/fixtures/pdf_benchmark/manifest.json"),
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--backends",
        nargs="+",
        choices=["pymupdf", "docling", "grobid"],
        default=["pymupdf", "docling", "grobid"],
    )
    parser.add_argument("--timeout-s", type=float, default=120)
    parser.add_argument("--max-rss-bytes", type=int, default=4 * 1024**3)
    parser.add_argument("--backend", choices=["pymupdf", "docling", "grobid"])
    parser.add_argument("--input", type=Path)
    parser.add_argument("--grobid-url", default=os.environ.get("NOESIS_GROBID_URL"))
    args = parser.parse_args()
    if args.backend:
        from src.evaluation.runtime_jobs import execute_job

        if args.input is None:
            parser.error("--backend requires --input")
        with args.input.open("rb") as source:
            raw = source.read(20_000_001)
        if not 0 < len(raw) <= 20_000_000:
            raise ValueError("PDF byte budget exceeded")
        job = execute_job(
            "pdf-" + args.backend,
            {
                "path": str(args.input.resolve()),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "grobid_url": args.grobid_url,
            },
            timeout_s=args.timeout_s,
            max_rss_bytes=args.max_rss_bytes,
        )
        result = {
            **job.get("result", {}),
            "status": job["status"],
            "elapsed_seconds": job.get("elapsed_seconds"),
            "peak_rss_kib": job.get("peak_process_tree_rss_bytes", 0) / 1024,
            "runtime_receipt": {
                key: value for key, value in job.items() if key != "result"
            },
        }
        args.out.write_text(json.dumps(result, default=str))
        return
    if args.manifest.stat().st_size > 4 * 1024**2:
        raise ValueError("manifest byte budget exceeded")
    manifest = json.loads(args.manifest.read_text())
    if not 1 <= len(manifest["documents"]) <= 250:
        raise ValueError("corpus document budget exceeded")
    runs = []
    for document in manifest["documents"]:
        path = Path(document["path"]).resolve()
        if path.stat().st_size > 20_000_000:
            raise ValueError("PDF byte budget exceeded")
        if hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"]:
            raise ValueError("corpus hash changed")
        for backend in args.backends:
            from src.evaluation.runtime_jobs import execute_job

            job = execute_job(
                "pdf-" + backend,
                {
                    "path": str(path),
                    "sha256": document["sha256"],
                    "grobid_url": args.grobid_url,
                },
                timeout_s=args.timeout_s,
                max_rss_bytes=args.max_rss_bytes,
            )
            result = {
                **job.get("result", {}),
                "status": job["status"],
                "elapsed_seconds": job.get("elapsed_seconds"),
                "peak_rss_kib": job.get("peak_process_tree_rss_bytes", 0) / 1024,
                "runtime_receipt": {
                    key: value for key, value in job.items() if key != "result"
                },
            }
            runs.append(
                {
                    "document": document["path"],
                    "sha256": document["sha256"],
                    "backend": backend,
                    **result,
                    "metrics": score(document, result)
                    if result["status"] == "completed"
                    else None,
                }
            )
    summaries = {}
    for backend in args.backends:
        values = [run for run in runs if run["backend"] == backend]
        completed = [run for run in values if run["status"] == "completed"]
        summaries[backend] = {
            "documents": len(values),
            "completed": len(completed),
            "completion_rate": len(completed) / len(values) if values else 0.0,
            "median_elapsed_seconds": median(
                run["elapsed_seconds"] for run in completed
            )
            if completed
            else None,
            "max_peak_rss_kib": max(
                (run["peak_rss_kib"] for run in completed), default=None
            ),
        }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "corpus_provenance": manifest["provenance"],
                "resources": "separate bounded process tree per document/backend; GROBID service memory is excluded",
                "metric_definitions": {
                    "table_positional_cell_recall": "exact normalized text at the correct table/page/row/column divided by annotated cells; never just bag-of-text",
                    "page_text_locator_recall": "fraction of authored expected spans found on the expected page",
                    "mean_bbox_iou": "mean rectangle intersection-over-union for exact authored text/page matches; unavailable for backends without comparable boxes",
                },
                "runs": runs,
                "summaries": summaries,
                "decision": "retain PyMuPDF baseline; defer optional production adoption pending representative independently checked documents and successful deployment-specific evaluation",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
