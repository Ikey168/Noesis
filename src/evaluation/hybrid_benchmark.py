"""Native PostgreSQL/pgvector and cross-encoder benchmark on an isolated database."""

from __future__ import annotations

import hashlib
import json
import time
import uuid


def run(payload):
    import psycopg2
    from sentence_transformers import CrossEncoder, SentenceTransformer

    from services.rag.lexical import LexicalSearchService
    from services.rag.rerank import CrossEncoderReranker
    from services.rag.retriever import HybridRetriever
    from services.rag.vector import VectorSearchService
    from src.argument_mining.model_registry import optional_model_spec
    from src.evaluation.benchmark_runtime import score_output
    from src.evaluation.model_backends import bounded_texts, model_path

    connection = payload["connection"]
    if (
        connection.get("host") != "127.0.0.1"
        or connection.get("database") != "noesis_hybrid_benchmark"
    ):
        raise ValueError("only the explicit loopback benchmark database is supported")
    corpus = payload["corpus"]
    documents, queries = corpus["documents"], corpus["queries"]
    if not 1 <= len(documents) <= 200 or not 1 <= len(queries) <= 32:
        raise ValueError("bounded corpus and query set required")
    if len({r["id"] for r in documents}) != len(documents):
        raise ValueError("unique document IDs required")
    bounded_texts(
        [r["text"] for r in documents + queries], max_records=232, max_chars=32000
    )
    db = psycopg2.connect(**connection)
    vector, lexical = VectorSearchService(connection), LexicalSearchService(connection)
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM documents) + (SELECT count(*) FROM chunks)"
            )
            if cur.fetchone()[0]:
                raise ValueError("benchmark refuses to modify a populated database")
            cur.execute("SET statement_timeout = '10s'")
        start = time.monotonic()
        encoder = SentenceTransformer(
            model_path("minilm")[0],
            device="cpu",
            local_files_only=True,
            trust_remote_code=False,
        )
        if any(
            len(encoder.tokenizer.encode(r["text"], truncation=False))
            > encoder.max_seq_length
            for r in documents + queries
        ):
            raise ValueError(
                "chunk inputs before benchmark; silent truncation is forbidden"
            )
        embeddings = encoder.encode(
            [r["text"] for r in documents],
            normalize_embeddings=True,
            batch_size=16,
            show_progress_bar=False,
        )
        encoded = time.monotonic()
        with db.cursor() as cur:
            for record, embedding in zip(documents, embeddings, strict=True):
                did = str(uuid.uuid5(uuid.NAMESPACE_URL, record["id"]))
                cid = str(uuid.uuid5(uuid.NAMESPACE_URL, record["id"] + ":whole"))
                text = record["text"]
                language = (
                    "de" if record["source"] in {"germandpr", "berlin-law"} else "en"
                )
                cur.execute(
                    "INSERT INTO documents(id,article_id,url,title,content,source,published_at,language,metadata) VALUES(%s,%s,'','',%s,%s,'1970-01-01',%s,%s)",
                    (
                        did,
                        record["id"],
                        text,
                        record["source"],
                        language,
                        json.dumps(
                            {
                                "source_revision": record["revision"],
                                "publication_date_unknown": True,
                            }
                        ),
                    ),
                )
                cur.execute(
                    "INSERT INTO chunks(id,document_id,chunk_index,content,word_count,char_count,doc_id,chunk_id,title,source,language,url) VALUES(%s,%s,0,%s,%s,%s,%s,%s,'',%s,%s,'')",
                    (
                        cid,
                        did,
                        text,
                        len(text.split()),
                        len(text),
                        record["id"],
                        record["id"] + ":whole",
                        record["source"],
                        language,
                    ),
                )
                cur.execute(
                    "INSERT INTO embeddings(chunk_id,embedding,model_name,model_version) VALUES(%s,%s::vector,%s,%s)",
                    (
                        cid,
                        embedding.tolist(),
                        optional_model_spec("minilm")["model"],
                        optional_model_spec("minilm")["revision"],
                    ),
                )
        db.commit()
        indexed = time.monotonic()
        model = CrossEncoder(
            model_path("minilm-reranker")[0],
            device="cpu",
            max_length=512,
            local_files_only=True,
            trust_remote_code=False,
        )
        for query in queries:
            if any(
                len(
                    model.tokenizer(query["text"], d["text"], truncation=False)[
                        "input_ids"
                    ]
                )
                > 512
                for d in documents
            ):
                raise ValueError("reranker paired token budget exceeded")
        reranker = object.__new__(CrossEncoderReranker)
        reranker.model, reranker.is_enabled = model, True
        vector.connect()
        lexical.connect()
        for service in (vector, lexical):
            with service.connection.cursor() as cur:
                cur.execute("SET statement_timeout = '10s'")
        modes = {
            "lexical": HybridRetriever(lexical_service=lexical),
            "semantic": HybridRetriever(vector_service=vector),
            "fusion": HybridRetriever(vector_service=vector, lexical_service=lexical),
            "reranked": HybridRetriever(
                vector_service=vector, lexical_service=lexical, reranker=reranker
            ),
        }
        report = {
            "corpus_sha256": hashlib.sha256(
                json.dumps(corpus, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "documents_sha256": hashlib.sha256(
                json.dumps(documents, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "query_set_sha256": hashlib.sha256(
                json.dumps(queries, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "document_count": len(documents),
            "models": {
                k: optional_model_spec(k) for k in ("minilm", "minilm-reranker")
            },
            "corpus_encoding_and_load_s": encoded - start,
            "indexing_s": indexed - encoded,
            "runs": [],
            "configuration": {
                "k": 30,
                "candidate_limits": 60,
                "fusion": "rrf, k=60",
                "reranker_fusion": "existing weighted default: 0.7 original + 0.3 native model score",
                "lexical": "existing English PostgreSQL text search configuration in both languages",
                "publication_date": "required documents.published_at uses 1970-01-01 sentinel; source date remains unknown",
                "database": "isolated disposable PostgreSQL; no production index modified",
            },
        }
        for query in queries:
            start = time.monotonic()
            qvector = encoder.encode(
                [query["text"]], normalize_embeddings=True, show_progress_bar=False
            )[0]
            query_encoding_s = time.monotonic() - start
            for mode, retriever in modes.items():
                observed = retriever.search_detailed(
                    query["text"],
                    qvector if mode != "lexical" else None,
                    k=30,
                    fusion_method="rrf",
                    enable_reranking=mode == "reranked",
                )
                rankings = [
                    {
                        "id": r.doc_id,
                        "storage_id": r.id,
                        "chunk_id": r.chunk_id,
                        "score": r.final_score,
                    }
                    for r in observed.pop("results")
                ]
                report["runs"].append(
                    {
                        "backend": mode,
                        "query_id": query["id"],
                        "language": query.get("language_pair", query["language"]),
                        "judgments": query["judgments"],
                        "diagnostics": observed,
                        "query_encoding_s": query_encoding_s
                        if mode != "lexical"
                        else 0.0,
                        "job": {"status": "completed", "result": {"results": rankings}},
                        "metrics": {
                            str(k): score_output(
                                "ranking", rankings, query["judgments"], {"k": k}
                            )
                            for k in (5, 10, 30)
                        },
                    }
                )
        report["zero_matches"] = modes["lexical"].search_detailed(
            "zzzxxyyneveraword", k=30, enable_reranking=False
        )
        lexical.connection.close()
        partial = modes["fusion"].search_detailed(
            queries[0]["text"], qvector, k=30, enable_reranking=False
        )
        report["partial_source_probe"] = {
            k: v for k, v in partial.items() if k != "results"
        }
        if (
            partial["status"] != "partial"
            or report["zero_matches"]["status"] != "complete"
            or report["zero_matches"]["results"]
        ):
            raise AssertionError("partial-source failure collapsed into zero matches")
        with db.cursor() as cur:
            cur.execute(
                "SELECT version(), pg_database_size(current_database()), (SELECT extversion FROM pg_extension WHERE extname='vector')"
            )
            version, size, extension = cur.fetchone()
        report["postgres"] = {
            "version": version,
            "database_bytes": size,
            "pgvector_version": extension,
        }
        return report
    finally:
        vector.disconnect()
        lexical.disconnect()
        db.close()
