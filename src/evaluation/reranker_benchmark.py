"""Compare native rerankers on fixed, revision-bound retrieval candidates."""

import hashlib
import json
import math
import time


def validate(payload):
    from src.evaluation.model_backends import bounded_texts

    kind, queries = payload["backend"], payload["queries"]
    if kind not in {"minilm-reranker", "qwen3-reranker"} or not 1 <= len(queries) <= 24:
        raise ValueError("bounded backend and query set required")
    batch = payload.get("batch_size", 2)
    if type(batch) is not int or not 1 <= batch <= 8:
        raise ValueError("batch size must be 1..8")
    if len({r["id"] for r in queries}) != len(queries):
        raise ValueError("unique query IDs required")
    for query in queries:
        candidates = query["candidates"]
        if not 1 <= len(candidates) <= 30 or len({r["id"] for r in candidates}) != len(
            candidates
        ):
            raise ValueError("unique bounded candidates required")
        bounded_texts([query["text"], *(r["text"] for r in candidates)], max_records=31)
        for row in candidates:
            if row.get("revision") != hashlib.sha256(row["text"].encode()).hexdigest():
                raise ValueError("candidate revision does not match text")
    return kind, queries, batch


def run(payload):
    kind, queries, batch = validate(payload)
    from src.argument_mining.model_registry import optional_model_spec
    from src.evaluation.benchmark_runtime import score_output
    from src.evaluation.model_backends import QwenReranker, model_path

    started = time.monotonic()
    if kind == "qwen3-reranker":
        model = QwenReranker(max_tokens=2048)
    else:
        from sentence_transformers import CrossEncoder

        model = CrossEncoder(
            model_path(kind)[0],
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
        )
        for query in queries:
            for candidate in query["candidates"]:
                if (
                    len(
                        model.tokenizer.encode(
                            query["text"], candidate["text"], truncation=False
                        )
                    )
                    > model.max_length
                ):
                    raise ValueError(
                        "baseline token budget exceeded; no silent truncation"
                    )
    loaded = time.monotonic()
    runs = []
    for query in queries:
        pairs = [(query["text"], row["text"]) for row in query["candidates"]]
        before = time.monotonic()
        scores = model.predict(pairs, batch_size=batch)
        elapsed = time.monotonic() - before
        scores = [float(v) for v in scores]
        if len(scores) != len(pairs) or any(not math.isfinite(v) for v in scores):
            raise ValueError("invalid native score alignment or values")
        ranking = sorted(
            [
                {
                    "id": row["id"],
                    "revision": row["revision"],
                    "score": score,
                    "input_rank": i,
                    "support_verified": False,
                }
                for i, (row, score) in enumerate(
                    zip(query["candidates"], scores, strict=True)
                )
            ],
            key=lambda r: (-r["score"], r["input_rank"]),
        )
        fused = (
            sorted(
                [
                    {
                        **row,
                        "score": 0.7
                        * float(
                            query["candidates"][row["input_rank"]].get(
                                "retrieval_score", 0
                            )
                        )
                        + 0.3 * row["score"],
                    }
                    for row in ranking
                ],
                key=lambda r: (-r["score"], r["input_rank"]),
            )
            if all("retrieval_score" in c for c in query["candidates"])
            else None
        )
        runs.append(
            {
                "backend": kind,
                "query_id": query["id"],
                "language": query["language_pair"],
                "judgments": query["judgments"],
                "candidate_ids": [r["id"] for r in query["candidates"]],
                "candidate_count": len(pairs),
                "score_range": [min(scores), max(scores)],
                "legacy_fusion": {
                    "weights": {"retrieval": 0.7, "native_reranker": 0.3},
                    "results": fused,
                    "metrics": {
                        str(k): score_output(
                            "ranking", fused, query["judgments"], {"k": k}
                        )
                        for k in (5, 10, 30)
                    },
                }
                if fused is not None
                else None,
                "query_s": elapsed,
                "job": {"result": {"results": ranking}},
                "metrics": {
                    str(k): score_output(
                        "ranking", ranking, query["judgments"], {"k": k}
                    )
                    for k in (5, 10, 30)
                },
            }
        )
    return {
        "backend": kind,
        "model": optional_model_spec(kind),
        "batch_size": batch,
        "model_load_s": loaded - started,
        "runs": runs,
        "input_sha256": hashlib.sha256(
            json.dumps(queries, sort_keys=True).encode()
        ).hexdigest(),
        "score_semantics": "native relevance ranking only; no factual support or score calibration claim",
        "production_index_modified": False,
    }
