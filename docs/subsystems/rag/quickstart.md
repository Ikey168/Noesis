# NeuroNews Vector Store & RAG Quickstart

This guide provides a complete setup for the NeuroNews vector store using PostgreSQL with pgvector extension for semantic search and RAG (Retrieval-Augmented Generation) capabilities.

## 🎯 Overview

The vector store enables:
- **Semantic Search**: Find similar articles using vector embeddings
- **RAG Pipeline**: Retrieve relevant context for question answering
- **Hybrid Search**: Combine semantic and keyword-based search
- **Analytics**: Track search patterns and performance

## 🏗️ Architecture

```
News Articles → Document Chunks → Embeddings → Vector Store (PostgreSQL + pgvector)
                                                      ↓
                                              Semantic Search & RAG
```

### Components

- **PostgreSQL 16** with pgvector extension
- **Document Processing**: Chunking and embedding generation
- **Vector Indexes**: IVFFlat and HNSW for fast similarity search
- **Search Analytics**: Query logging and performance metrics

## 🚀 Quick Start

### 1. Start the Vector Store

```bash
# Start PostgreSQL with pgvector
make rag-up

# Check service health
docker-compose -f docker/vector/docker-compose.rag.yml ps
```

### 2. Run Migrations

```bash
# Initialize pgvector and create schema
make rag-migrate

# Verify tables exist
psql -h localhost -p 5433 -U neuronews -d neuronews_vector -c "\dt"
```

### 3. Connect to Database

```bash
# Using psql
psql -h localhost -p 5433 -U neuronews -d neuronews_vector

# Using connection string
postgresql://neuronews:neuronews_vector_pass@localhost:5433/neuronews_vector
```

## 📊 Database Schema

### Core Tables

#### `documents`
Stores original articles and metadata
```sql
CREATE TABLE documents (
    id UUID PRIMARY KEY,
    article_id VARCHAR(255) NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source VARCHAR(100) NOT NULL,
    published_at TIMESTAMP WITH TIME ZONE NOT NULL,
    -- ... additional fields
);
```

#### `chunks`
Document segments for processing
```sql
CREATE TABLE chunks (
    id UUID PRIMARY KEY,
    document_id UUID REFERENCES documents(id),
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    word_count INTEGER NOT NULL,
    -- ... additional fields
);
```

#### `embeddings`
Vector representations of chunks
```sql
CREATE TABLE embeddings (
    id UUID PRIMARY KEY,
    chunk_id UUID REFERENCES chunks(id),
    embedding vector(384) NOT NULL,  -- Default dimension
    model_name VARCHAR(100) NOT NULL,
    -- ... additional fields
);
```

#### `search_logs`
Analytics and performance tracking
```sql
CREATE TABLE search_logs (
    id UUID PRIMARY KEY,
    query_text TEXT NOT NULL,
    query_embedding vector(384),
    results_count INTEGER NOT NULL,
    processing_time_ms INTEGER,
    -- ... additional fields
);
```

### Vector Indexes

The schema includes optimized indexes for vector similarity search:

```sql
-- IVFFlat index for cosine similarity (fast for smaller datasets)
CREATE INDEX idx_embeddings_vector_cosine 
ON embeddings USING ivfflat (embedding vector_cosine_ops) 
WITH (lists = 100);

-- Alternative HNSW index (better for larger datasets)
-- CREATE INDEX idx_embeddings_vector_cosine_hnsw 
-- ON embeddings USING hnsw (embedding vector_cosine_ops) 
-- WITH (m = 16, ef_construction = 64);
```

## 🔍 Search Operations

### Semantic Search

```sql
-- Search for similar documents using cosine similarity
SELECT 
    d.title,
    c.content,
    vector_ops.cosine_similarity(e.embedding, query_embedding) as similarity
FROM embeddings e
JOIN chunks c ON e.chunk_id = c.id
JOIN documents d ON c.document_id = d.id
WHERE vector_ops.cosine_similarity(e.embedding, query_embedding) > 0.7
ORDER BY similarity DESC
LIMIT 10;
```

### Using Helper Functions

```sql
-- Search similar documents
SELECT * FROM search_similar_documents(
    query_embedding := '[0.1, 0.2, ...]'::vector(384),
    similarity_threshold := 0.6,
    max_results := 5
);

-- Get document chunks
SELECT * FROM get_document_chunks('document-uuid-here');
```

## 🛠️ Configuration

### Environment Variables

```bash
# Database connection
POSTGRES_HOST=localhost
POSTGRES_PORT=5433
POSTGRES_DB=neuronews_vector
POSTGRES_USER=neuronews
POSTGRES_PASSWORD=neuronews_vector_pass

# Vector configuration
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
EMBEDDING_DIMENSION=384
CHUNK_SIZE=512
CHUNK_OVERLAP=50
```

### Makefile Targets

```bash
# Start services
make rag-up

# Stop services
make rag-down

# Run migrations
make rag-migrate

# Reset database (WARNING: deletes all data)
make rag-reset

# Show logs
make rag-logs

# Connect to database
make rag-connect
```

## 📈 Performance Optimization

### Vector Index Tuning

#### IVFFlat Parameters
```sql
-- Adjust list count based on data size
-- Rule of thumb: lists ≈ sqrt(total_rows)
ALTER INDEX idx_embeddings_vector_cosine SET (lists = 200);
```

#### HNSW Parameters
```sql
-- m: connections per layer (higher = better recall, more memory)
-- ef_construction: search effort during index build
CREATE INDEX idx_embeddings_vector_hnsw 
ON embeddings USING hnsw (embedding vector_cosine_ops) 
WITH (m = 32, ef_construction = 128);
```

### Query Performance

```sql
-- Set search parameters for HNSW
SET hnsw.ef_search = 100;  -- Higher = better recall, slower search

-- Adjust work_mem for vector operations
SET work_mem = '256MB';
```

### Monitoring

```sql
-- Check index usage
SELECT 
    schemaname, 
    tablename, 
    indexname, 
    idx_scan, 
    idx_tup_read 
FROM pg_stat_user_indexes 
WHERE tablename = 'embeddings';

-- Search performance analytics
SELECT * FROM search_analytics 
WHERE search_date >= CURRENT_DATE - INTERVAL '7 days';
```

## 🔧 Common Operations

### Adding Documents

```python
import psycopg2
import numpy as np
from sentence_transformers import SentenceTransformer

# Connect to database
conn = psycopg2.connect(
    host="localhost",
    port=5433,
    database="neuronews_vector",
    user="neuronews",
    password="neuronews_vector_pass"
)

# Load embedding model
model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')

# Insert document
def add_document(article_id, title, content, source):
    with conn.cursor() as cur:
        # Insert document
        cur.execute("""
            INSERT INTO documents (article_id, title, content, source, published_at)
            VALUES (%s, %s, %s, %s, NOW())
            RETURNING id
        """, (article_id, title, content, source))
        
        doc_id = cur.fetchone()[0]
        
        # Create chunks (simplified)
        chunks = [content[i:i+512] for i in range(0, len(content), 412)]  # 100 char overlap
        
        for i, chunk in enumerate(chunks):
            # Insert chunk
            cur.execute("""
                INSERT INTO chunks (document_id, chunk_index, content, word_count, char_count)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (doc_id, i, chunk, len(chunk.split()), len(chunk)))
            
            chunk_id = cur.fetchone()[0]
            
            # Generate and insert embedding
            embedding = model.encode(chunk)
            cur.execute("""
                INSERT INTO embeddings (chunk_id, embedding, model_name)
                VALUES (%s, %s, %s)
            """, (chunk_id, embedding.tolist(), 'sentence-transformers/all-MiniLM-L6-v2'))
        
        conn.commit()
        return doc_id
```

### Searching Documents

```python
def search_documents(query_text, limit=10):
    # Generate query embedding
    query_embedding = model.encode(query_text)
    
    with conn.cursor() as cur:
        cur.execute("""
            SELECT 
                d.title,
                c.content,
                (1 - (e.embedding <=> %s::vector)) as similarity
            FROM embeddings e
            JOIN chunks c ON e.chunk_id = c.id
            JOIN documents d ON c.document_id = d.id
            WHERE (1 - (e.embedding <=> %s::vector)) > 0.6
            ORDER BY similarity DESC
            LIMIT %s
        """, (query_embedding.tolist(), query_embedding.tolist(), limit))
        
        return cur.fetchall()
```

## 🚨 Troubleshooting

### Common Issues

#### pgvector Extension Not Found
```bash
# Check if extension is available
docker exec neuronews-postgres-vector psql -U neuronews -d neuronews_vector -c "SELECT * FROM pg_available_extensions WHERE name = 'vector';"

# If not available, the image might not include pgvector
# Ensure you're using pgvector/pgvector:pg16 image
```

#### Slow Vector Queries
```sql
-- Check if indexes are being used
EXPLAIN (ANALYZE, BUFFERS) 
SELECT * FROM embeddings 
WHERE embedding <=> '[0.1,0.2,...]'::vector(384) < 0.5 
ORDER BY embedding <=> '[0.1,0.2,...]'::vector(384) 
LIMIT 10;

-- If index not used, check statistics
ANALYZE embeddings;
```

#### Memory Issues
```sql
-- Increase work_mem for vector operations
ALTER SYSTEM SET work_mem = '512MB';
SELECT pg_reload_conf();

-- Monitor memory usage
SELECT 
    name, 
    setting, 
    unit 
FROM pg_settings 
WHERE name IN ('work_mem', 'shared_buffers', 'maintenance_work_mem');
```

### Performance Tips

1. **Index Strategy**: Use IVFFlat for smaller datasets (<1M vectors), HNSW for larger
2. **Chunk Size**: Optimize based on your use case (128-512 tokens typical)
3. **Embedding Model**: Balance between quality and speed
4. **Connection Pooling**: Use pgbouncer for production deployments

## 📚 Next Steps

1. **Data Ingestion**: Set up automated pipelines to populate the vector store
2. **RAG Implementation**: Build question-answering system using retrieved chunks
3. **Hybrid Search**: Combine semantic and keyword search for better results
4. **Monitoring**: Set up alerts for performance and capacity
5. **Scaling**: Consider read replicas and sharding for large datasets

## 🔗 Resources

- [pgvector Documentation](https://github.com/pgvector/pgvector)
- [PostgreSQL Vector Operations](https://www.postgresql.org/docs/current/)
- [Sentence Transformers](https://www.sbert.net/)
- [Vector Database Best Practices](https://www.pinecone.io/learn/vector-database/)

---

*This vector store setup provides the foundation for advanced semantic search and RAG capabilities in the NeuroNews platform.*
