"""Reproducible native-job comparisons on explicit frozen held-out cases.

This executes registered backends, not model-availability probes. Labels and
inputs are supplied separately; synthetic fixtures cannot certify human quality.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import Counter

from src.evaluation.runtime_jobs import OPERATIONS, execute_job
from src.evaluation.runtime_outcomes import normalize_outcome


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _pointer(value, pointer):
    if not isinstance(pointer, str) or pointer and not pointer.startswith("/"):
        raise ValueError("explicit JSON pointer required")
    for token in pointer.split("/")[1:] if pointer else ():
        token = token.replace("~1", "/").replace("~0", "~")
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def _prf(predicted, expected):
    if len(predicted) > 10000 or len(expected) > 10000:
        raise ValueError("annotation count exceeds metric budget")
    predicted, expected = Counter(predicted), Counter(expected)
    hits = sum((predicted & expected).values())
    p = hits / sum(predicted.values()) if predicted else float(not expected)
    r = hits / sum(expected.values()) if expected else float(not predicted)
    return {
        "precision": p,
        "recall": r,
        "f1": 2 * p * r / (p + r) if p + r else 0.0,
        "true_positives": hits,
        "false_positives": sum(predicted.values()) - hits,
        "false_negatives": sum(expected.values()) - hits,
    }


def _edit_distance(left, right):
    if len(left) * len(right) > 4000000:
        raise ValueError(
            "exact edit-distance budget exceeded; score smaller explicitly labelled pages/regions"
        )
    prior = list(range(len(right) + 1))
    for i, x in enumerate(left, 1):
        current = [i]
        for j, y in enumerate(right, 1):
            current.append(min(current[-1] + 1, prior[j] + 1, prior[j - 1] + (x != y)))
        prior = current
    return prior[-1]


def score_output(metric, prediction, expected, configuration=None):
    config = configuration or {}
    if metric == "classification":
        if not isinstance(prediction, str) or not isinstance(expected, str):
            raise TypeError(
                "classification output and reference must be explicit labels"
            )
        return {
            "accuracy": float(prediction == expected),
            "predicted": prediction,
            "expected": expected,
        }
    if metric in {"spans", "boundaries"}:
        if not isinstance(prediction, list) or not isinstance(expected, list):
            raise TypeError("span/boundary arrays required")
        if metric == "spans":

            def key(row):
                if (
                    type(row.get("start")) is not int
                    or type(row.get("end")) is not int
                    or not 0 <= row["start"] < row["end"]
                ):
                    raise ValueError("invalid exact character offsets")
                return (row["start"], row["end"], row["label"])
        else:

            def key(row):
                end = row.get("end") if isinstance(row, dict) else row
                if type(end) is not int or end < 0:
                    raise ValueError("exact sentence boundary offsets required")
                return end

        return _prf([key(row) for row in prediction], [key(row) for row in expected])
    if metric == "text":
        if (
            not isinstance(prediction, str)
            or not isinstance(expected, str)
            or max(len(prediction), len(expected)) > 100000
        ):
            raise ValueError("bounded plain-text outputs required")
        char = _edit_distance(prediction, expected)
        words = _edit_distance(prediction.split(), expected.split())
        return {
            "character_error_rate": char / max(1, len(expected)),
            "word_error_rate": words / max(1, len(expected.split())),
            "character_edits": char,
            "word_edits": words,
            "normalization": "none; exact supplied source text",
        }
    if metric == "ranking":
        if (
            not isinstance(prediction, list)
            or len(prediction) > 1000
            or not isinstance(expected, dict)
            or not expected
        ):
            raise ValueError("bounded ranking and nonempty graded judgments required")
        ids = [row["id"] if isinstance(row, dict) else row for row in prediction]
        if len(set(ids)) != len(ids) or any(not isinstance(v, str) for v in ids):
            raise ValueError("unique ranked source IDs required")
        if any(type(v) is not int or not 0 <= v <= 3 for v in expected.values()):
            raise ValueError("bounded graded relevance judgments required")
        k = config.get("k", 30)
        if type(k) is not int or not 1 <= k <= 1000:
            raise ValueError("bounded ranking cutoff required")
        top = ids[:k]
        relevant = {key for key, value in expected.items() if value > 0}
        dcg = lambda grades: sum(
            (2**value - 1) / math.log2(i + 2) for i, value in enumerate(grades)
        )
        ideal = dcg(sorted(expected.values(), reverse=True)[:k])
        return {
            "recall_at_k": len(set(top) & relevant) / len(relevant)
            if relevant
            else 0.0,
            "mrr_at_k": next(
                (1 / (i + 1) for i, key in enumerate(top) if key in relevant), 0.0
            ),
            "ndcg_at_k": dcg([expected.get(key, 0) for key in top]) / ideal
            if ideal
            else 0.0,
            "judged_fraction": sum(key in expected for key in top) / len(top)
            if top
            else 0.0,
            "cutoff": k,
        }
    if metric == "alignment":
        if (
            not isinstance(prediction, list)
            or not isinstance(expected, list)
            or len(expected) > 5000
        ):
            raise ValueError("bounded word alignment arrays required")
        errors = []
        aligned = 0
        mismatched = 0
        for i, word in enumerate(expected):
            observed = prediction[i] if i < len(prediction) else {}
            if observed.get("word") != word["word"]:
                mismatched += 1
                continue
            start, end = observed.get("start_s"), observed.get("end_s")
            if start is None or end is None:
                continue
            times = [start, end, word["start_s"], word["end_s"]]
            if (
                any(
                    type(v) not in {int, float} or not math.isfinite(v) or v < 0
                    for v in times
                )
                or start > end
            ):
                raise ValueError("finite ordered word boundaries required")
            errors.append((abs(start - word["start_s"]) + abs(end - word["end_s"])) / 2)
            aligned += 1
        return {
            "alignment_coverage": aligned / len(expected) if expected else 0.0,
            "mean_word_boundary_error_s": statistics.mean(errors) if errors else None,
            "word_identity_mismatches": mismatched,
            "seek_tolerance_s": config.get("seek_tolerance_s", 0.25),
            "citation_seek_accuracy": sum(
                v <= config.get("seek_tolerance_s", 0.25) for v in errors
            )
            / len(expected)
            if expected
            else 0.0,
        }
    raise ValueError("unsupported explicit benchmark metric")


def evaluate_manifest(manifest, *, executor=None):
    if (
        not isinstance(manifest, dict)
        or manifest.get("contract") != "noesis-native-benchmark-v1"
    ):
        raise ValueError("native benchmark contract required")
    cases = manifest.get("cases")
    if (
        not isinstance(cases, list)
        or not 1 <= len(cases) <= 500
        or len(json.dumps(manifest).encode()) > 16 * 1024**2
    ):
        raise ValueError("bounded frozen benchmark manifest required")
    frozen = manifest.get("configuration", {})
    forbidden = set(frozen.get("development_groups", []))
    ids = set()
    # Validate the entire corpus before doing any computation.
    for case in cases:
        if (
            not isinstance(case, dict)
            or not case.get("id")
            or case["id"] in ids
            or case.get("split") != "test"
            or not case.get("group_id")
            or case["group_id"] in forbidden
        ):
            raise ValueError("unique held-out cases and non-leaking groups required")
        if case.get("label_origin") not in {
            "independent-human",
            "assisted-human",
            "model",
            "fixture",
        } or any(not case.get(k) for k in ("source", "domain", "language")):
            raise ValueError(
                "explicit source/domain/language and label provenance required"
            )
        if case.get("operation") not in OPERATIONS or case["operation"] in {
            "zyte-fetch",
            "scrape-fixture",
        }:
            raise ValueError(
                "only registered local inference jobs are permitted in a frozen model benchmark"
            )
        if case.get("metric") not in {
            "classification",
            "spans",
            "boundaries",
            "text",
            "ranking",
            "alignment",
        }:
            raise ValueError("explicit supported metric required")
        if not isinstance(case.get("payload"), dict) or "expected" not in case:
            raise ValueError(
                "native input and separate frozen expected output required"
            )
        ids.add(case["id"])
    runner = executor or execute_job
    results = []
    started = time.monotonic()
    for case in cases:
        job = runner(
            case["operation"],
            case["payload"],
            timeout_s=frozen.get("timeout_s", 60),
            max_rss_bytes=frozen.get("max_rss_bytes", 4 * 1024**3),
        )
        job = normalize_outcome(case["operation"], job)
        result = {
            "id": case["id"],
            "source": case["source"],
            "domain": case["domain"],
            "language": case["language"],
            "label_origin": case["label_origin"],
            "operation": case["operation"],
            "metric": case["metric"],
            "job": job,
            "metrics": None,
            "input_sha256": _hash(case["payload"]),
            "expected_sha256": _hash(case["expected"]),
        }
        if job["status"] == "completed":
            try:
                result["metrics"] = score_output(
                    case["metric"],
                    _pointer(job["result"], case.get("output_pointer", "")),
                    case["expected"],
                    case.get("metric_configuration"),
                )
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                result["metric_error"] = type(exc).__name__
        results.append(result)
    complete = all(row["metrics"] is not None for row in results)
    groups = {}
    for metric in sorted({row["metric"] for row in results}):
        rows = [row for row in results if row["metric"] == metric]
        valid = [row["metrics"] for row in rows if row["metrics"] is not None]
        keys = set.intersection(*(set(row) for row in valid)) if valid else set()
        # Missing or failed cases never silently improve the reported mean.
        mean = {
            key: statistics.mean(row[key] for row in valid)
            for key in keys
            if len(valid) == len(rows)
            and all(type(row[key]) in {int, float} for row in valid)
        }
        groups[metric] = {
            "cases": len(rows),
            "scored": len(valid),
            "means": mean if len(valid) == len(rows) else None,
        }
    elapsed = [row["job"].get("elapsed_seconds") for row in results]
    timings = sorted(v for v in elapsed if type(v) in {int, float} and math.isfinite(v))
    return {
        "contract": "noesis-native-benchmark-result-v1",
        "status": "completed" if complete else "partial",
        "cases": results,
        "summary": groups,
        "manifest_sha256": _hash(manifest),
        "configuration": frozen,
        "elapsed_seconds": time.monotonic() - started,
        "p50_job_seconds": statistics.median(timings) if timings else None,
        "p95_job_seconds": timings[math.ceil(0.95 * len(timings)) - 1]
        if timings
        else None,
        "executor_kind": "injected-fixture-executor"
        if executor
        else "native-isolated-jobs",
        "human_provenance_independently_verified": False,
        "automatic_adoption": False,
        "decision": "defer production change until issue-specific representative acceptance is reviewed",
        "limitations": [
            "Fixtures and judge-generated labels do not replace independent human judgments.",
            "Resource results include model startup in each isolated case.",
            "Reported means retain all cases; partial benchmarks have no successful-subset means.",
        ],
    }


def retrieval_job(payload, *, model=None):
    """Actual pinned E5/BGE/MiniLM encoding and isolated exact index comparison."""
    import duckdb
    import numpy as np

    from src.argument_mining.model_registry import optional_model_spec
    from src.evaluation.model_backends import (
        BGEBackend,
        E5Backend,
        ModelSpaceIndex,
        bounded_texts,
        model_path,
    )

    backend = payload["backend"]
    records = payload["documents"]
    query = payload["query"]
    if (
        backend not in {"e5", "bge-m3", "minilm"}
        or not isinstance(records, list)
        or not 1 <= len(records) <= 200
    ):
        raise ValueError("bounded corpus and supported retrieval model required")
    if any(not row.get("id") or not row.get("revision") for row in records) or len(
        {row["id"] for row in records}
    ) != len(records):
        raise ValueError("unique captured document IDs and revisions required")
    bounded_texts(
        [query, *[row["text"] for row in records]], max_records=201, max_chars=32000
    )
    texts = [row["text"] for row in records]
    start = time.monotonic()
    if model is None:
        if backend == "minilm":
            from sentence_transformers import SentenceTransformer

            path, _ = model_path("minilm")
            model = SentenceTransformer(
                path, device="cpu", local_files_only=True, trust_remote_code=False
            )
        else:
            model = {"e5": E5Backend, "bge-m3": BGEBackend}[backend]()
    loaded = time.monotonic()
    space = _hash(
        [optional_model_spec(backend), "normalized", "retrieval-benchmark-v1"]
    )
    if backend == "bge-m3":
        # The benchmark accepts 200 documents; each native adapter invocation is
        # capped at 128. Preserve every source/revision instead of truncating.
        docvectors = []
        for offset in range(0, len(texts), 128):
            docvectors.extend(model.encode(texts[offset : offset + 128]))
        corpus_encoded = time.monotonic()
        q = model.encode([query])[0]
    else:
        if backend == "e5":
            dense = model.embed_texts(texts)
            corpus_encoded = time.monotonic()
            querydense = model.embed_queries([query])[0]
            space = model.space_id
        else:

            def check_minilm(inputs):
                if any(
                    len(
                        model.tokenizer.encode(
                            text, add_special_tokens=True, truncation=False
                        )
                    )
                    > model.max_seq_length
                    for text in inputs
                ):
                    raise ValueError(
                        "MiniLM input exceeds its pinned token limit; chunk before comparison"
                    )

            check_minilm(texts)
            dense = model.encode(
                texts,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
                batch_size=16,
            )
            corpus_encoded = time.monotonic()
            check_minilm([query])
            querydense = model.encode(
                [query],
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )[0]
        docvectors = [
            {"dense": np.asarray(row).tolist(), "space_id": space} for row in dense
        ]
        q = {"dense": np.asarray(querydense).tolist(), "space_id": space}
    encoded = time.monotonic()
    conn = duckdb.connect(config={"threads": 2, "memory_limit": "512MB"})
    try:
        index = ModelSpaceIndex(conn, q["space_id"], max_records=200)
        for record, representation in zip(records, docvectors, strict=True):
            index.upsert(
                record["id"],
                record["revision"],
                representation,
                provenance={
                    "source": record.get("source"),
                    "original_text_sha256": _hash(record["text"]),
                },
            )
        indexed = time.monotonic()
        results = index.search(
            q, limit=payload.get("limit", 30), weights=payload.get("weights")
        )
        searched = time.monotonic()
        size = conn.execute(
            "SELECT sum(octet_length(encode(payload))) FROM optional_model_index"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "backend": backend,
        "model": optional_model_spec(backend),
        "results": results,
        "corpus_sha256": _hash(records),
        "timing_contract": "noesis-retrieval-timing-v2",
        "model_load_seconds": loaded - start,
        "corpus_encoding_seconds": corpus_encoded - loaded,
        "query_encoding_seconds": encoded - corpus_encoded,
        "search_seconds": searched - indexed,
        "encoding_seconds": encoded - start,
        "indexing_seconds": indexed - encoded,
        "query_seconds": (encoded - corpus_encoded) + (searched - indexed),
        "timing_semantics": "query_seconds includes warm query encoding and search, excluding model load, corpus/index construction, index sizing and teardown",
        "serialized_index_bytes": size,
        "space_id": q["space_id"],
        "production_index_modified": False,
    }
