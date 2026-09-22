"""Encode one frozen corpus once, then time actual model-versioned queries."""

import hashlib
import json
import time


def run(payload):
    import duckdb
    import numpy as np
    from sentence_transformers import SentenceTransformer

    from src.argument_mining.model_registry import optional_model_spec
    from src.evaluation.benchmark_runtime import score_output
    from src.evaluation.model_backends import (
        BGEBackend,
        E5Backend,
        ModelSpaceIndex,
        bounded_texts,
        model_path,
    )

    kind, documents, queries = (
        payload["backend"],
        payload["documents"],
        payload["queries"],
    )
    if (
        kind not in {"minilm", "e5", "bge-m3"}
        or not 1 <= len(documents) <= 200
        or not 1 <= len(queries) <= 64
    ):
        raise ValueError("bounded model, corpus and query set required")
    if len({r["id"] for r in documents}) != len(documents) or len(
        {r["id"] for r in queries}
    ) != len(queries):
        raise ValueError("unique source/query IDs required")
    bounded_texts(
        [r["text"] for r in documents + queries], max_records=264, max_chars=32000
    )
    if any(not r.get("revision") for r in documents):
        raise ValueError("source revisions required")
    start = time.monotonic()
    if kind in {"e5", "bge-m3"}:
        model = E5Backend() if kind == "e5" else BGEBackend()
        space = model.space_id
    else:
        model = SentenceTransformer(
            model_path(kind)[0],
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
        )
        if any(
            len(model.tokenizer.encode(r["text"], truncation=False))
            > model.max_seq_length
            for r in documents + queries
        ):
            raise ValueError("MiniLM token limit exceeded; chunk before inference")
        space = hashlib.sha256(
            json.dumps(
                [optional_model_spec(kind), "normalized", model.max_seq_length],
                sort_keys=True,
            ).encode()
        ).hexdigest()
    loaded = time.monotonic()
    encoding_s = 0.0

    def bge_vectors():
        nonlocal encoding_s
        for offset in range(0, len(documents), 4):
            before = time.monotonic()
            batch = model.encode([r["text"] for r in documents[offset : offset + 4]])
            encoding_s += time.monotonic() - before
            yield from batch

    if kind == "bge-m3":
        vectors = bge_vectors()
    else:
        before = time.monotonic()
        vectors = (
            model.embed_texts([r["text"] for r in documents])
            if kind == "e5"
            else model.encode(
                [r["text"] for r in documents],
                normalize_embeddings=True,
                show_progress_bar=False,
                batch_size=16,
            )
        )
        encoding_s = time.monotonic() - before
    database_memory = "1024MB" if kind == "bge-m3" else "512MB"
    db = duckdb.connect(config={"threads": 2, "memory_limit": database_memory})
    try:
        index = ModelSpaceIndex(db, space, max_records=200)
        for row, vector in zip(documents, vectors, strict=True):
            index.upsert(
                row["id"],
                row["revision"],
                vector
                if kind == "bge-m3"
                else {"dense": np.asarray(vector).tolist(), "space_id": space},
                provenance={"source": row["source"], "revision": row["revision"]},
            )
        indexed = time.monotonic()
        runs = []
        for query in queries:
            start_query = time.monotonic()
            qvector = (
                model.encode([query["text"]])[0]
                if kind == "bge-m3"
                else (
                    model.embed_queries([query["text"]])[0]
                    if kind == "e5"
                    else model.encode(
                        [query["text"]],
                        normalize_embeddings=True,
                        show_progress_bar=False,
                    )[0]
                )
            )
            encoded_query = time.monotonic()
            modes = {"dense": {"dense": 1.0}}
            if kind == "bge-m3":
                modes.update(
                    {
                        "dense-sparse": {"dense": 1.0, "sparse": 1.0},
                        "multivector": {"multivector": 1.0},
                    }
                )
            component_start = time.monotonic()
            components = index.search(
                qvector
                if kind == "bge-m3"
                else {"dense": np.asarray(qvector).tolist(), "space_id": space},
                limit=len(documents),
            )
            component_s = time.monotonic() - component_start
            for mode, weights in modes.items():
                sort_start = time.monotonic()
                ranking = sorted(
                    [
                        {
                            **row,
                            "score": sum(
                                row["scores"][key] * weight
                                for key, weight in weights.items()
                            ),
                        }
                        for row in components
                    ],
                    key=lambda row: (-row["score"], row["id"]),
                )[:30]
                search_s = component_s + time.monotonic() - sort_start
                runs.append(
                    {
                        "backend": kind,
                        "mode": mode,
                        "weights": weights,
                        "query_id": query["id"],
                        "language": query["language_pair"],
                        "judgments": query["judgments"],
                        "label_origin": query.get(
                            "label_origin", "published source judgments"
                        ),
                        "query_encoding_s": encoded_query - start_query,
                        "shared_component_scoring_s": component_s,
                        "search_s": search_s,
                        "query_s": encoded_query - start_query + search_s,
                        "job": {"result": {"results": ranking}},
                        "metrics": {
                            str(k): score_output(
                                "ranking", ranking, query["judgments"], {"k": k}
                            )
                            for k in (5, 10, 30)
                        },
                    }
                )
        size = sum(
            db.execute(
                "SELECT octet_length(encode(payload)) FROM optional_model_index WHERE space_id=? AND id=?",
                [space, row["id"]],
            ).fetchone()[0]
            for row in documents
        )
    finally:
        db.close()
    return {
        "backend": kind,
        "model": optional_model_spec(kind),
        "space_id": space,
        "corpus_sha256": hashlib.sha256(
            json.dumps(documents, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "query_set_sha256": hashlib.sha256(
            json.dumps(queries, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "document_count": len(documents),
        "model_load_s": loaded - start,
        "corpus_encoding_s": encoding_s,
        "index_construction_s": indexed - loaded - encoding_s,
        "database_memory_limit": database_memory,
        "corpus_encoding_batch_size": 4 if kind == "bge-m3" else "backend default",
        "scoring_implementation": "exact evaluator computes all indexed components once per query and shares them across mode sorts; per-mode timing includes the shared computation and is not an optimized single-modality search",
        "indexing_documents_per_second": len(documents) / (indexed - loaded),
        "serialized_index_bytes": size,
        "runs": runs,
        "production_index_modified": False,
    }
