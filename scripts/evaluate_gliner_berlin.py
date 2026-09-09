"""Source-span/missing-field diagnostic on captured Berlin legislation, not gold labels."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.evaluation.runtime_jobs import execute_job
from src.ingestion.regional_providers import parse_berlin_juris_xml

DIGEST = "b701bc5b07cb78c22a9db24b39c74a15f7a896ea2b333a3002e6c41f8610fb8a"
URL = "https://gesetze.berlin.de/jportal/bsbeAizDownload/Verf_BE.zip?doc.id=jlr-NNLBE000047B0&doc.part=X&_=%2FVerf_BE.zip"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.xml.stat().st_size > 20_000_000:
        raise ValueError("bounded captured XML required")
    raw = args.xml.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DIGEST:
        raise ValueError("captured XML digest mismatch")
    record = parse_berlin_juris_xml(raw, source_url=URL)[0]
    # Select before inference: first complete paragraph naming the legislature.
    section = next(
        r
        for r in record["sections"]
        if "Abgeordnetenhaus" in r["text"] and len(r["text"]) < 2000
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "config/extraction_schemas/berlin-legal.json"
        ).read_text()
    )
    report = {
        "source_url": URL,
        "source_sha256": DIGEST,
        "locator": section["locator"],
        "selection": "First complete paragraph containing Abgeordnetenhaus, shorter than 2000 characters",
        "label_origin": "No independent labels; native source-span and missing-field diagnostic only",
        "job": execute_job(
            "gliner2",
            {
                "text": section["text"],
                "schema": schema,
                "source_id": section["locator"]["official_norm_id"],
                "source_revision": DIGEST,
                "language": "de",
                "threshold": 0.5,
            },
            timeout_s=120,
            max_rss_bytes=4 * 1024**3,
        ),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(report["job"]["status"])


if __name__ == "__main__":
    main()
