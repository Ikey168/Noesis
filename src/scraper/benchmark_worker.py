"""One bounded local scraping probe, reusable from the installed job worker."""

import hashlib
import json
import resource
import time
from pathlib import Path

from src.ingestion.extract import extract_article
from src.scraper.backend_evaluation import fetch_backend


def benchmark_one(url, backend, output_path):
    """Run one backend in an isolated forked process and persist diagnostics."""
    start = time.monotonic()
    try:
        html = fetch_backend(url, backend)
        if len(html) > 2_000_000:
            raise ValueError("page budget exceeded")
        result = extract_article(html, url=url)
        output = {
            "status": "completed",
            "text": result.text if result else "",
            "snapshot_sha256": hashlib.sha256(html.encode()).hexdigest(),
            "captured_html": str(html),
            "backend_markdown": getattr(html, "markdown", None),
            "acquisition_metadata": getattr(html, "acquisition_metadata", None),
            "extraction_metadata": result.metadata if result else None,
        }
    except Exception as exc:  # noqa: BLE001 - benchmark must preserve backend failure as data
        output = {
            "status": "failed",
            "failure_type": type(exc).__name__,
            "failure": str(exc)[:200],
        }
    output.update(
        elapsed_seconds=time.monotonic() - start,
        peak_parent_rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    )
    Path(output_path).write_text(json.dumps(output))
