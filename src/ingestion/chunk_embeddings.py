"""Token-aware full-document embeddings with atomic publication receipts."""

import hashlib
import json
import math
from itertools import islice

from src.ingestion.processing_versions import ProcessingVersions, configuration_hash, document_input_hash
from src.ingestion.token_chunks import token_chunks

_DDL = """CREATE TABLE IF NOT EXISTS document_chunk_embeddings(
    document_id TEXT NOT NULL,chunk_id TEXT NOT NULL,input_hash TEXT NOT NULL,configuration_hash TEXT NOT NULL,
    source_revision_id TEXT,coordinate_field TEXT NOT NULL,start_offset BIGINT NOT NULL,end_offset BIGINT NOT NULL,
    text TEXT NOT NULL,token_count INTEGER NOT NULL,model TEXT NOT NULL,dim INTEGER NOT NULL,
    tokenizer_json TEXT NOT NULL,vector_json TEXT NOT NULL,PRIMARY KEY(document_id,chunk_id))"""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def publish_chunk_receipts(conn, receipts):
    """Call inside the transaction that publishes the containing generation."""
    versions = ProcessingVersions(conn)
    for receipt in receipts:
        current = conn.execute(f"SELECT {document_input_hash()} FROM documents d WHERE d.document_id=?", [receipt["document_id"]]).fetchone()
        if not current or current[0] != receipt["input_hash"]:
            raise ValueError("source changed before chunk publication")
        count = conn.execute("""SELECT count(*) FROM document_chunk_embeddings WHERE document_id=?
            AND input_hash=? AND configuration_hash=?""", [receipt["document_id"], receipt["input_hash"], receipt["configuration_hash"]]).fetchone()[0]
        if count != receipt["chunk_count"]:
            raise ValueError("chunk publication is incomplete")
        versions.record(receipt["document_id"], "chunk_embedding", receipt["input_hash"], receipt["configuration_hash"])
        conn.execute("""DELETE FROM document_chunk_embeddings WHERE document_id=? AND
            (input_hash<>? OR configuration_hash<>?)""", [receipt["document_id"], receipt["input_hash"], receipt["configuration_hash"]])


def embed_document_chunks(conn, provider, *, document_ids=None, limit=100, max_tokens=None,
                          overlap_tokens=32, batch_size=32, max_chunks=5000, publish=True):
    if not 1 <= batch_size <= 128 or not 1 <= max_chunks <= 10000 or not 0 <= limit <= 10000:
        raise ValueError("invalid chunk indexing bounds")
    from src.ingestion.document_store import DocumentStore
    DocumentStore(conn)
    conn.execute(_DDL)
    ProcessingVersions(conn)
    tokenizer = provider.tokenizer_identity()
    config_hash = configuration_hash({"model": provider.name(), "dim": provider.dim(), "tokenizer": tokenizer,
                                      "max_tokens": max_tokens or provider.token_limit(), "overlap_tokens": overlap_tokens,
                                      "chunker": "original-text-token-count-v1"})
    clause, params = "", []
    if document_ids is not None:
        ids = sorted(set(document_ids))
        if not ids:
            return {"processed": 0, "receipts": []}
        clause = " AND d.document_id IN (" + ",".join("?" for _ in ids) + ")"
        params.extend(ids)
    rows = conn.execute(f"""SELECT d.document_id FROM documents d
        LEFT JOIN document_processing_versions p ON p.document_id=d.document_id AND p.stage='chunk_embedding'
        WHERE (p.input_hash IS DISTINCT FROM {document_input_hash()} OR p.configuration_hash IS DISTINCT FROM ?)
        {clause} ORDER BY d.document_id LIMIT ?""", [config_hash, *params, limit]).fetchall()
    receipts = []
    for (document_id,) in rows:
        row = conn.execute(f"SELECT title,content,{document_input_hash()} FROM documents d WHERE document_id=?", [document_id]).fetchone()
        field = "content" if row[1] else "title"
        text = row[1] or row[0] or ""
        if len(text) > 20_000_000:
            raise ValueError("document exceeds the explicit chunk indexing character budget")
        input_hash = row[2]
        revision = conn.execute("SELECT revision_id FROM document_revision_records WHERE document_id=? ORDER BY revision DESC LIMIT 1", [document_id]).fetchone()
        chunks = list(islice(token_chunks(text, provider, max_tokens=max_tokens, overlap_tokens=overlap_tokens), max_chunks + 1))
        if len(chunks) > max_chunks:
            raise ValueError("document exceeds the chunk count budget")
        pending = []
        for chunk in chunks:
            chunk["chunk_id"] = "chunk:" + hashlib.sha256(_json([document_id, input_hash, config_hash, chunk["start_offset"], chunk["end_offset"]]).encode()).hexdigest()[:32]
            exists = conn.execute("SELECT 1 FROM document_chunk_embeddings WHERE document_id=? AND chunk_id=?", [document_id, chunk["chunk_id"]]).fetchone()
            if not exists:
                pending.append(chunk)
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset + batch_size]
            matrix = list(islice(iter(provider.embed_texts([chunk["text"] for chunk in batch])), len(batch) + 1))
            if len(matrix) != len(batch):
                raise ValueError("embedding provider returned an incorrect chunk vector count")
            vectors = [[float(value) for value in vector] for vector in matrix]
            if any(len(vector) != provider.dim() or not all(math.isfinite(value) for value in vector) or not any(vector) for vector in vectors):
                raise ValueError("chunk embedding dimension or finiteness differs")
            conn.execute("BEGIN TRANSACTION")
            try:
                for chunk, vector in zip(batch, vectors):
                    conn.execute("INSERT OR IGNORE INTO document_chunk_embeddings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [document_id, chunk["chunk_id"], input_hash, config_hash, revision[0] if revision else None,
                         field, chunk["start_offset"], chunk["end_offset"], chunk["text"], chunk["token_count"],
                         provider.name(), provider.dim(), _json(tokenizer), _json(vector)])
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        receipt = {"document_id": document_id, "input_hash": input_hash, "configuration_hash": config_hash,
                   "chunk_count": len(chunks), "source_revision_id": revision[0] if revision else None}
        if publish:
            conn.execute("BEGIN TRANSACTION")
            try:
                publish_chunk_receipts(conn, [receipt])
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        receipts.append(receipt)
    return {"processed": len(receipts), "receipts": receipts, "published": publish}


def search_document_chunks(conn, query, provider, *, top_k=10, document_ids=None):
    if provider.count_tokens(query) > provider.token_limit():
        raise ValueError("query exceeds the configured tokenizer limit")
    matrix = list(islice(iter(provider.embed_texts([query])), 2))
    if len(matrix) != 1:
        raise ValueError("query provider returned an incorrect vector count")
    vector = [float(value) for value in matrix[0]]
    if len(vector) != provider.dim() or not all(math.isfinite(value) for value in vector) or not any(vector):
        raise ValueError("invalid query embedding")
    clause, params = "", []
    if document_ids is not None:
        ids = sorted(set(document_ids))
        if not ids:
            return {"results": [], "count": 0, "query": query, "coverage": {"complete": True, "pending_documents": 0}}
        clause = " AND d.document_id IN (" + ",".join("?" for _ in ids) + ")"
        params = ids
    rows = conn.execute(f"""WITH scored AS (
        SELECT c.document_id,c.chunk_id,c.source_revision_id,c.coordinate_field,c.start_offset,c.end_offset,c.text,
               d.title,d.source_id,d.url,list_cosine_similarity(CAST(c.vector_json AS DOUBLE[]),CAST(? AS DOUBLE[])) AS score
        FROM document_chunk_embeddings c JOIN documents d ON d.document_id=c.document_id
        JOIN document_processing_versions p ON p.document_id=c.document_id AND p.stage='chunk_embedding'
        AND p.input_hash=c.input_hash AND p.configuration_hash=c.configuration_hash
        WHERE c.input_hash={document_input_hash()} AND c.model=? AND c.dim=? AND c.tokenizer_json=? {clause}
        QUALIFY row_number() OVER (PARTITION BY c.document_id ORDER BY score DESC,c.chunk_id)=1)
        SELECT * FROM scored ORDER BY score DESC,document_id LIMIT ?""",
        [vector, provider.name(), provider.dim(), _json(provider.tokenizer_identity()), *params, min(max(int(top_k), 1), 1000)]).fetchall()
    pending = conn.execute(f"""SELECT count(*) FROM documents d
        LEFT JOIN document_processing_versions p ON p.document_id=d.document_id AND p.stage='chunk_embedding'
        WHERE length(trim(coalesce(nullif(d.content,''),d.title,'')))>0 AND (p.input_hash IS DISTINCT FROM {document_input_hash()} OR NOT EXISTS (
          SELECT 1 FROM document_chunk_embeddings c WHERE c.document_id=d.document_id AND c.input_hash=p.input_hash
          AND c.configuration_hash=p.configuration_hash AND c.model=? AND c.dim=? AND c.tokenizer_json=?)) {clause}""",
        [provider.name(), provider.dim(), _json(provider.tokenizer_identity()), *params]).fetchone()[0]
    hits = [{"document_id": r[0], "chunk_id": r[1], "source_revision_id": r[2], "coordinate_field": r[3],
             "start_offset": r[4], "end_offset": r[5], "text": r[6], "title": r[7], "source": r[8],
             "url": r[9], "score": r[10]} for r in rows]
    return {"results": hits, "count": len(hits), "query": query, "model": provider.name(),
            "method": "token-bounded chunk cosine", "coverage": {"complete": pending == 0, "pending_documents": pending}}
