"""Bounded optional inference jobs with JSON I/O and process-tree cleanup.

No arbitrary module names, code or shell commands are accepted
from a job payload. Only zyte-fetch permits an explicitly reserved hosted request. A fresh process gives each model a hard wall deadline and
isolates memory. Production callers can cancel without waiting for inference.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src.evaluation.runtime_errors import BackendError
from src.evaluation.runtime_outcomes import (
    backend_outcome,
    exception_outcome,
    normalize_outcome,
)

OPERATIONS = frozenset(
    {
        "health",
        "retrieval-e5",
        "retrieval-bge-m3",
        "retrieval-minilm",
        "retrieval-hybrid",
        "retrieval-batch",
        "reranker-benchmark",
        "nli-benchmark",
        "ner-benchmark",
        "scrape-fixture",
        "pdf-pymupdf",
        "pdf-docling",
        "pdf-grobid",
        "stance",
        "frames",
        "e5",
        "bge-m3",
        "qwen3-reranker",
        "mdeberta",
        "gliner2",
        "sat",
        "paddleocr",
        "ocr-tesseract",
        "lightonocr",
        "whisperx",
        "outlines",
        "report-unconstrained",
        "splink",
        "rapidfuzz",
        "presidio",
        "ragas",
        "zyte-fetch",
    }
)


def _interpreter(operation=None):
    if operation is not None:
        name = "NOESIS_OPTIONAL_PYTHON_" + operation.upper().replace("-", "_")
        configured = os.environ.get(name)
        if configured:
            path = Path(configured).expanduser().absolute()
            if not path.is_file() or not os.access(path, os.X_OK):
                raise BackendError(
                    "runtime_unavailable",
                    "configured optional Python is not executable",
                )
            return str(path)
    candidate = Path(sys.executable)
    if candidate.name.lower().startswith(("python", "pypy")):
        return str(candidate)
    candidate = shutil.which("python3")
    if not candidate:
        raise BackendError(
            "runtime_unavailable",
            "a real Python interpreter is required for bounded jobs",
        )
    return candidate


def _kill_group(process):
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=0.5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass
            process.wait()
    # A model loader may spawn children that outlive the direct worker. A unique
    # session/process group is owned by this job and can safely be swept.
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def execute_job(
    operation,
    payload,
    *,
    timeout_s=60.0,
    max_rss_bytes=6 * 1024**3,
    max_output_bytes=32 * 1024**2,
    threads=2,
    cancelled=None,
):
    """Execute only an allowlisted local backend, returning an explicit outcome."""
    if operation not in OPERATIONS or not isinstance(payload, dict):
        raise ValueError("unknown optional backend operation")
    if not math.isfinite(timeout_s) or not 0.01 <= timeout_s <= 600:
        raise ValueError("wall timeout must be .01..600 seconds")
    if (
        type(max_rss_bytes) is not int
        or not 32 * 1024**2 <= max_rss_bytes <= 16 * 1024**3
    ):
        raise ValueError("RSS budget must be 32 MiB..16 GiB")
    if (
        not 1 <= max_output_bytes <= 64 * 1024**2
        or type(threads) is not int
        or not 1 <= threads <= 4
    ):
        raise ValueError("invalid output/thread budget")
    if "path" in payload and isinstance(payload["path"], str):
        payload = {**payload, "path": str(Path(payload["path"]).resolve())}
    raw = json.dumps(
        {"operation": operation, "payload": payload},
        allow_nan=False,
        ensure_ascii=False,
    ).encode()
    if len(raw) > 16 * 1024**2:
        raise ValueError("job input exceeds 16 MiB")
    if cancelled and cancelled():
        return {
            "status": "cancelled",
            "operation": operation,
            "failure_code": "cancelled_before_start",
        }
    try:
        import psutil
    except ImportError:
        return {
            "status": "unavailable",
            "operation": operation,
            "failure_code": "resource_monitor_unavailable",
        }
    started = time.monotonic()
    root = Path(__file__).resolve().parents[2]
    # Explicitly omit API keys and tokens. Only local caches and native runtimes
    # need environment configuration, not user provider credentials.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "LD_LIBRARY_PATH",
            "SYSTEMROOT",
            "TMPDIR",
            "HF_HOME",
            "HF_HUB_CACHE",
            "TRANSFORMERS_CACHE",
            "CUDA_VISIBLE_DEVICES",
            "NOESIS_DOCLING_ARTIFACTS_PATH",
            "NLTK_DATA",
            "TESSDATA_PREFIX",
            "PLAYWRIGHT_BROWSERS_PATH",
        }
    }
    isolated_interpreter = os.environ.get(
        "NOESIS_OPTIONAL_PYTHON_" + operation.upper().replace("-", "_")
    )
    environment.update(
        PYTHONPATH=str(root)
        if isolated_interpreter
        else os.pathsep.join(
            dict.fromkeys([str(root), *[str(Path(p).resolve()) for p in sys.path if p]])
        ),
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        RAGAS_DO_NOT_TRACK="true",
        TOKENIZERS_PARALLELISM="false",
        OMP_NUM_THREADS=str(threads),
        MKL_NUM_THREADS=str(threads),
        OPENBLAS_NUM_THREADS=str(threads),
        NOESIS_MODEL_THREADS=str(threads),
    )
    with tempfile.TemporaryDirectory(prefix="noesis-model-job-") as directory:
        incoming, outgoing = (
            Path(directory) / "input.json",
            Path(directory) / "output.json",
        )
        incoming.write_bytes(raw)
        command = [
            _interpreter(operation),
            "-m",
            "src.evaluation.runtime_jobs",
            "--worker",
            str(incoming),
            "--output",
            str(outgoing),
            "--max-output-bytes",
            str(max_output_bytes),
        ]
        process = subprocess.Popen(
            command,
            cwd=directory,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        peak, failure = 0, None
        monitor = psutil.Process(process.pid)
        try:
            while process.poll() is None:
                if cancelled and cancelled():
                    failure = "cancelled"
                    break
                if time.monotonic() - started > timeout_s:
                    failure = "deadline_exceeded"
                    break
                try:
                    tree = [monitor, *monitor.children(recursive=True)]
                    rss = 0
                    for child in tree:
                        try:
                            rss += child.memory_info().rss
                        except psutil.NoSuchProcess:
                            pass
                    peak = max(peak, rss)
                    if rss > max_rss_bytes:
                        failure = "memory_limit"
                        break
                except psutil.NoSuchProcess:
                    pass
                time.sleep(0.02)
            if failure is not None:
                result = {
                    "status": "cancelled" if failure == "cancelled" else "failed",
                    "failure_code": failure,
                }
            elif not outgoing.is_file() or outgoing.stat().st_size > max_output_bytes:
                result = {
                    "status": "failed",
                    "failure_code": "worker_output_missing_or_oversized",
                    "worker_exit": process.returncode,
                }
            else:
                try:
                    result = normalize_outcome(
                        operation, json.loads(outgoing.read_bytes())
                    )
                    if process.returncode != 0:
                        result = {
                            "status": "failed",
                            "failure_code": "worker_process_failed",
                            "worker_exit": process.returncode,
                        }
                except (ValueError, OSError, BackendError):
                    result = {
                        "status": "failed",
                        "failure_code": "invalid_worker_output",
                    }
        finally:
            _kill_group(process)
    return {
        **result,
        "operation": operation,
        "elapsed_seconds": time.monotonic() - started,
        "peak_process_tree_rss_bytes": peak,
        "resource_limits": {
            "timeout_s": timeout_s,
            "rss_bytes": max_rss_bytes,
            "threads": threads,
        },
        "execution": "isolated-local-process",
        "hosted_inference_used": False,
        "remote_processing_used": operation == "zyte-fetch",
    }


def dispatch(operation, payload):
    """Allowlisted operations, also usable inside an already-isolated worker."""
    if operation == "ner-benchmark":
        from src.evaluation.published_ner import run

        return run(payload)
    if operation == "nli-benchmark":
        from src.evaluation.published_nli import run

        return run(payload)
    if operation == "reranker-benchmark":
        from src.evaluation.reranker_benchmark import run

        return run(payload)
    if operation == "retrieval-batch":
        from src.evaluation.retrieval_batch import run

        return run(payload)
    if operation == "retrieval-hybrid":
        from src.evaluation.hybrid_benchmark import run

        return run(payload)
    if operation == "health":
        return {
            "python": sys.version.split()[0],
            "backends": sorted(OPERATIONS - {"health"}),
        }
    if operation == "scrape-fixture":
        import ipaddress
        from urllib.parse import urlsplit

        parsed = urlsplit(payload["url"])
        if (
            parsed.scheme != "http"
            or parsed.username
            or parsed.password
            or not ipaddress.ip_address(parsed.hostname).is_loopback
            or payload["backend"]
            not in {"scrapy", "playwright", "crawl4ai", "crawlee", "crawlee-adaptive"}
        ):
            raise ValueError("only local authored scraping fixtures are permitted")
        from src.scraper.benchmark_worker import benchmark_one

        output = Path.cwd() / "scrape-output.json"
        benchmark_one(payload["url"], payload["backend"], output)
        if output.stat().st_size > 8_000_000:
            raise ValueError("scraper output budget")
        return json.loads(output.read_bytes())
    if operation.startswith("retrieval-"):
        from src.evaluation.benchmark_runtime import retrieval_job

        return retrieval_job(
            {**payload, "backend": operation.removeprefix("retrieval-")}
        )
    if operation.startswith("pdf-"):
        from src.ingestion.pdf_evaluation import parse_backend

        return parse_backend(
            Path(payload["path"]),
            operation.removeprefix("pdf-"),
            grobid_url=payload.get("grobid_url"),
            expected_sha256=payload["sha256"],
            max_pages=payload.get("max_pages", 250),
        )
    if operation == "zyte-fetch":
        from src.scraper.zyte_worker import fetch_once

        return fetch_once(payload)
    if operation == "rapidfuzz":
        from src.evaluation.entity_backends import RapidFuzzCandidates

        return RapidFuzzCandidates(threshold=payload["threshold"]).score(
            payload["source"], payload["candidates"]
        )
    if operation == "splink":
        from src.evaluation.entity_backends import SplinkCandidates

        return SplinkCandidates(payload["policy"]).predict(
            payload["records"],
            evaluation_group_ids=payload.get("evaluation_group_ids", ()),
        )
    if operation == "presidio":
        from src.evaluation.presidio_redaction import PresidioRedactor

        return PresidioRedactor.from_spacy_models().redact(
            payload["original"], language=payload["language"]
        )
    if operation == "ragas":
        import asyncio

        from src.evaluation.ragas_metrics import evaluate_ragas

        return asyncio.run(
            evaluate_ragas(
                payload["cases"],
                metrics=payload.get("metrics", ["id_precision", "id_recall"]),
            )
        )
    if operation in {"e5", "bge-m3", "qwen3-reranker", "gliner2", "sat"}:
        from src.evaluation.model_backends import (
            BGEBackend,
            E5Backend,
            GLiNERBackend,
            QwenReranker,
            SaTSegmenter,
        )

        if operation == "e5":
            backend = E5Backend(max_tokens=payload.get("max_tokens", 512))
            mode = payload.get("mode", "passage")
            if mode not in {"passage", "query"}:
                raise ValueError("embedding mode must be passage or query")
            vectors = (
                backend.embed_queries if mode == "query" else backend.embed_texts
            )(payload["texts"])
            return {
                "space_id": backend.space_id,
                "vectors": vectors.tolist(),
                "mode": mode,
                "model": backend.tokenizer_identity(),
            }
        if operation == "bge-m3":
            backend = BGEBackend(max_tokens=payload.get("max_tokens", 512))
            return {
                "space_id": backend.space_id,
                "representations": backend.encode(payload["texts"]),
            }
        if operation == "qwen3-reranker":
            backend = QwenReranker(max_tokens=payload.get("max_tokens", 2048))
            return {
                "results": backend.rank_records(
                    payload["query"], payload["candidates"], limit=payload.get("limit")
                ),
                "model": backend.spec,
            }
        if operation == "gliner2":
            backend = GLiNERBackend(max_chars=payload.get("max_chars", 8000))
            if "schema" in payload:
                if "labels" in payload:
                    raise ValueError(
                        "choose either entity labels or a combined extraction schema"
                    )
                return backend.extract_schema(
                    payload["text"],
                    payload["schema"],
                    source_id=payload["source_id"],
                    source_revision=payload["source_revision"],
                    language=payload["language"],
                    threshold=payload.get("threshold", 0.5),
                )
            return backend.extract(
                payload["text"],
                payload["labels"],
                source_id=payload["source_id"],
                source_revision=payload["source_revision"],
                language=payload["language"],
            )
        return {
            "segments": SaTSegmenter(threshold=payload.get("threshold", 0.5)).segment(
                payload["text"]
            )
        }
    if operation in {"stance", "frames"}:
        from src.evaluation.mining_runtime import (
            CalibratedMiningBackend,
            MultilingualNLI,
        )

        if payload.get("policy", {}).get("task") != operation:
            raise ValueError("task-specific frozen calibration policy required")
        backend = CalibratedMiningBackend(
            MultilingualNLI(), payload["policy"], payload["templates"]
        )
        return backend.predict(payload["text"], topic=payload.get("topic", ""))
    if operation == "mdeberta":
        from src.evaluation.mining_runtime import MultilingualNLI

        backend = MultilingualNLI()
        if "claims" in payload:
            claims = payload["claims"]
            if not isinstance(claims, list) or not 1 <= len(claims) <= 64:
                raise ValueError("bounded NLI hypotheses required")
            return {
                "assessments": [
                    backend.classify_evidence(
                        payload["premise"],
                        claim,
                        max_windows=payload.get("max_windows", 64),
                    )
                    for claim in claims
                ],
                "model": backend.spec,
            }
        if "pairs" in payload:
            return {
                "probabilities": backend.probabilities(payload["pairs"]),
                "model": backend.spec,
            }
        return {
            **backend.classify_evidence(
                payload["premise"],
                payload["hypothesis"],
                max_windows=payload.get("max_windows", 64),
            ),
            "model": backend.spec,
        }
    if operation in {"paddleocr", "ocr-tesseract", "lightonocr", "whisperx"}:
        from src.evaluation.media_backends import media_job

        return media_job(operation, payload)
    if operation in {"outlines", "report-unconstrained"}:
        from src.evaluation.report_generation import OutlinesProposalGenerator

        return OutlinesProposalGenerator(
            max_new_tokens=payload.get("max_new_tokens", 1024),
            constrained=operation == "outlines",
        ).generate(payload)
    raise ValueError("unknown backend operation")


def _worker(args):
    # Limit output growth even if a model's result contains more data than the
    # adapter should return. Memory is supervised externally across children.
    if os.name == "posix":
        import resource

        resource.setrlimit(
            resource.RLIMIT_FSIZE, (args.max_output_bytes, args.max_output_bytes)
        )
    try:
        if args.worker.stat().st_size > 16 * 1024**2:
            raise ValueError("input byte limit")
        request = json.loads(args.worker.read_bytes())
        if request.get("operation") not in OPERATIONS or not isinstance(
            request.get("payload"), dict
        ):
            raise ValueError("invalid job request")
        if request["operation"] in {
            "retrieval-e5",
            "retrieval-bge-m3",
            "retrieval-minilm",
            "retrieval-hybrid",
            "retrieval-batch",
            "reranker-benchmark",
            "nli-benchmark",
            "ner-benchmark",
            "e5",
            "bge-m3",
            "qwen3-reranker",
            "mdeberta",
            "gliner2",
            "sat",
            "whisperx",
            "lightonocr",
            "outlines",
            "report-unconstrained",
            "stance",
            "frames",
            "pdf-docling",
        }:
            try:
                import torch

                torch.set_num_threads(int(os.environ.get("NOESIS_MODEL_THREADS", "2")))
            except ImportError:
                pass
        output = dispatch(request["operation"], request["payload"])
        result = backend_outcome(request["operation"], output)
    except Exception as exc:  # noqa: BLE001 - an isolated worker writes explicit outcomes, not exception text
        result = exception_outcome(exc)
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError, OverflowError):
        encoded = json.dumps(
            {"status": "failed", "failure_code": "invalid_model_output"}
        ).encode()
    if len(encoded) > args.max_output_bytes:
        encoded = json.dumps(
            {"status": "failed", "failure_code": "output_budget"}
        ).encode()
    args.output.write_bytes(encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-output-bytes", type=int, default=32 * 1024**2)
    _worker(parser.parse_args())


if __name__ == "__main__":
    main()
