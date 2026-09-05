"""Run each parser in a bounded subprocess; preserve failures and raw structure."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.ingestion.pdf_evaluation import parse_backend, score


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("tests/fixtures/pdf_benchmark/manifest.json"),
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--backend", choices=["pymupdf", "docling", "grobid"])
    parser.add_argument("--input", type=Path)
    parser.add_argument("--grobid-url", default=os.environ.get("NOESIS_GROBID_URL"))
    args = parser.parse_args()
    if args.backend:
        import resource

        start = time.monotonic()
        try:
            result = parse_backend(args.input, args.backend, grobid_url=args.grobid_url)
            result.update(status="completed")
        except Exception as exc:
            result = {
                "status": "failed",
                "failure_type": type(exc).__name__,
                "failure": str(exc)[:250],
            }
        result.update(
            elapsed_seconds=time.monotonic() - start,
            peak_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        )
        args.out.write_text(json.dumps(result, default=str))
        return
    manifest = json.loads(args.manifest.read_text())
    runs = []
    for document in manifest["documents"]:
        path = Path(document["path"]).resolve()
        if hashlib.sha256(path.read_bytes()).hexdigest() != document["sha256"]:
            raise ValueError("corpus hash changed")
        for backend in ["pymupdf", "docling", "grobid"]:
            with tempfile.TemporaryDirectory(prefix="noesis-pdf-eval-") as temp:
                output = Path(temp) / "result.json"
                command = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--backend",
                    backend,
                    "--input",
                    str(path),
                    "--out",
                    str(output),
                ]
                if args.grobid_url:
                    command += ["--grobid-url", args.grobid_url]
                try:
                    subprocess.run(
                        command,
                        timeout=120,
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    result = (
                        json.loads(output.read_text())
                        if output.stat().st_size <= 20_000_000
                        else {"status": "failed", "failure_type": "output_budget"}
                    )
                except (
                    subprocess.TimeoutExpired,
                    subprocess.CalledProcessError,
                    OSError,
                ):
                    result = {
                        "status": "failed",
                        "failure_type": "process_failed_or_timeout",
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
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "corpus_provenance": manifest["provenance"],
                "runs": runs,
                "decision": "retain PyMuPDF baseline; defer optional production adoption pending representative independently checked documents and successful deployment-specific evaluation",
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
