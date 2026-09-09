import hashlib
import json
import sys

import pytest

from scripts.evaluate_pdf_backends import main
from src.evaluation import runtime_jobs


@pytest.mark.parametrize("status", ["partial", "unavailable", "failed", "completed"])
def test_single_backend_cli_preserves_bounded_worker_outcome(
    tmp_path, monkeypatch, status
):
    pdf, output = tmp_path / "input.pdf", tmp_path / "result.json"
    pdf.write_bytes(b"%PDF authored dispatcher fixture")
    calls = []

    def execute(operation, payload, **limits):
        calls.append((operation, payload, limits))
        return {
            "status": status,
            "result": {"text": "Partial text"},
            "elapsed_seconds": 0.1,
            "peak_process_tree_rss_bytes": 2048,
        }

    monkeypatch.setattr(runtime_jobs, "execute_job", execute)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "evaluate_pdf_backends",
            "--backend",
            "docling",
            "--input",
            str(pdf),
            "--out",
            str(output),
            "--timeout-s",
            "3",
        ],
    )
    main()
    result = json.loads(output.read_text())
    assert result["status"] == status
    assert result["runtime_receipt"]["status"] == status
    assert calls[0][0] == "pdf-docling"
    assert calls[0][1]["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert calls[0][2]["timeout_s"] == 3
    assert result["peak_rss_kib"] == 2
