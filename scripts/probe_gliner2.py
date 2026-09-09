"""Run real pinned GLiNER2 on authored examples, without claiming quality eval."""

import argparse
import json
import os
import resource
import time
from datetime import UTC, datetime
from pathlib import Path

from src.integrations.entities import GLiNER2Extractor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    started = time.perf_counter()
    extractor = GLiNER2Extractor()
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "model_load_ms": (time.perf_counter() - started) * 1000,
        "corpus": "authored smoke examples, not independently annotated evaluation",
        "decision": "defer adoption pending held-out comparison (#1420/#1493)",
        "device": "cpu",
        "runtime_threads": {
            name: os.environ.get(name)
            for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS")
        },
        "examples": [],
    }
    for language, text in [
        ("de", "Die Humboldt-Universität zu Berlin erhält Mittel aus Horizont Europa."),
        ("en", "The European Commission funds research through Horizon Europe."),
        ("de", "Das Bezirksamt Mitte prüft den Antrag nach § 34 BauGB."),
    ]:
        started = time.perf_counter()
        run = extractor.extract(
            text,
            language=language,
            article_id=f"authored:{len(report['examples'])}",
            revision_id="v1",
        )
        report["examples"].append(
            {
                "text": text,
                "latency_ms": (time.perf_counter() - started) * 1000,
                **run,
            }
        )
    report["process_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "examples": len(report["examples"]),
                "peak_rss_kib": report["process_peak_rss_kib"],
            }
        )
    )


if __name__ == "__main__":
    main()
