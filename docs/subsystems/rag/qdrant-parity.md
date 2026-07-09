# Qdrant Backend Parity

**Issue #239**: Swappable vector database with the same retriever API

This document describes the implementation of Qdrant backend parity with the existing pgvector implementation, allowing seamless switching between vector databases.

## Overview

The NeuroNews system now supports two vector database backends:
- **pgvector**: PostgreSQL with vector extensions (default)
- **Qdrant**: Dedicated vector database

Both backends provide identical APIs and functionality, allowing you to switch between them via environment variables without code changes.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   UnifiedVectorService                     │
│                                                             │
│  ┌─────────────────────┐    ┌─────────────────────────────┐ │
│  │   PgVectorBackend   │    │    QdrantBackend           │ │
│  │                     │    │                             │ │
│  │ - PostgreSQL        │    │ - Qdrant Client            │ │
│  │ - pgvector ext      │    │ - Collection Management    │ │
│  │ - SQL queries       │    │ - gRPC/HTTP API            │ │
│  └─────────────────────┘    └─────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

## Environment Configuration

### Backend Selection

Set the `VECTOR_BACKEND` environment variable to choose your backend:

```bash
# Use Qdrant (requires Qdrant server)
export VECTOR_BACKEND=qdrant

# Use pgvector (default)
export VECTOR_BACKEND=pgvector
```

### Qdrant Configuration

```bash
# Qdrant connection settings
export QDRANT_HOST=localhost          # Default: localhost
export QDRANT_PORT=6333               # Default: 6333  
export QDRANT_COLLECTION=neuronews_vectors  # Default: neuronews_vectors

# Vector configuration
export VECTOR_SIZE=384                # Default: 384 (sentence-transformers)
```

### pgvector Configuration

```bash
# PostgreSQL connection settings
export POSTGRES_HOST=localhost        # Default: localhost
export POSTGRES_PORT=5433             # Default: 5433
export POSTGRES_DB=neuronews_vector   # Default: neuronews_vector
export POSTGRES_USER=neuronews        # Default: neuronews
export POSTGRES_PASSWORD=neuronews    # Default: neuronews
```

## Usage

### Basic Usage

```python
from services.vector_service import get_vector_service

# Get service with environment-configured backend
service = get_vector_service()

# Or explicitly specify backend
service = get_vector_service(backend_type='qdrant')
service = get_vector_service(backend_type='pgvector')

# All operations identical regardless of backend
results = service.search(query_embedding, k=10)
service.upsert(documents)
stats = service.get_stats()
```

### Advanced Usage

```python
from services.vector_service import UnifiedVectorService

# Qdrant with custom configuration
qdrant_service = UnifiedVectorService(
    backend_type='qdrant',
    host='qdrant.example.com',
    port=6333,
    collection_name='custom_collection'
)

# pgvector with custom connection
pgvector_service = UnifiedVectorService(
    backend_type='pgvector',
    connection_params={
        'host': 'postgres.example.com',
        'port': 5432,
        'database': 'vectors',
        'user': 'vector_user',
        'password': 'secret'
    }
)
```

## API Reference

### Core Operations

All backends implement these operations with identical signatures:

#### `create_collection(**kwargs) -> bool`
- **pgvector**: Ensures database tables exist
- **Qdrant**: Creates vector collection

#### `upsert(points: List[Dict], **kwargs) -> int`
- **pgvector**: Inserts/updates vectors in PostgreSQL
- **Qdrant**: Upserts points to collection
- Returns number of points processed

#### `search(query_embedding, k=10, filters=None, **kwargs) -> List[Dict]`
- **Both**: Vector similarity search with optional filters
- Returns standardized result format

#### `delete_by_filter(filters: Dict) -> int`
- **pgvector**: Delete by filter criteria (limited implementation)
- **Qdrant**: Full filter-based deletion support

#### `health_check() -> bool`
- **Both**: Check backend connectivity and health

#### `get_stats() -> Dict`
- **Both**: Return backend-specific statistics

### Search Filters

Both backends support the same filter format:

```python
filters = {
    'source': 'TechNews',           # Filter by source
    'date_from': datetime(2024, 1, 1),  # From date
    'date_to': datetime(2024, 12, 31),   # To date  
    'min_similarity': 0.7           # Minimum similarity score
}

results = service.search(embedding, k=10, filters=filters)
```

### Result Format

Both backends return results in the same format:

```python
{
    'id': 'unique_id',
    'doc_id': 'document_id', 
    'chunk_id': 'chunk_id',
    'title': 'Document Title',
    'content': 'Document content...',
    'source': 'Source Name',
    'url': 'https://example.com',
    'published_at': datetime_object,
    'similarity_score': 0.85,
    'word_count': 150,
    'char_count': 800
}
```

## Deployment

### Qdrant Deployment

Use the provided Docker Compose file:

```bash
# Start Qdrant service
cd docker/vector
docker-compose -f docker-compose.qdrant.yml up -d

# Verify health
curl http://localhost:6333/health
```

### pgvector Deployment

Use existing PostgreSQL setup with pgvector extension:

```bash
# Example Docker command
docker run -d \
  --name postgres-vector \
  -e POSTGRES_DB=neuronews_vector \
  -e POSTGRES_USER=neuronews \
  -e POSTGRES_PASSWORD=neuronews \
  -p 5433:5432 \
  pgvector/pgvector:pg15
```

## Performance Comparison

| Feature | pgvector | Qdrant |
|---------|----------|--------|
| **Setup** | PostgreSQL + extension | Standalone service |
| **Indexing** | IVFFlat, HNSW | HNSW |
| **Filtering** | SQL WHERE clauses | Native filter support |
| **Scalability** | Limited by PostgreSQL | Designed for scale |
| **Memory Usage** | Shared with PostgreSQL | Dedicated memory |
| **Consistency** | ACID transactions | Eventual consistency |
| **Query Speed** | Good for small-medium | Optimized for vectors |

## Migration Between Backends

### From pgvector to Qdrant

```python
# 1. Export data from pgvector
from services.vector_service import get_vector_service

pg_service = get_vector_service(backend_type='pgvector')
# Export logic here...

# 2. Import to Qdrant
qdrant_service = get_vector_service(backend_type='qdrant')
qdrant_service.create_collection()
qdrant_service.upsert(exported_data)

# 3. Update environment
export VECTOR_BACKEND=qdrant
```

### From Qdrant to pgvector

```python
# 1. Export from Qdrant
qdrant_service = get_vector_service(backend_type='qdrant')
# Export logic...

# 2. Import to pgvector  
pg_service = get_vector_service(backend_type='pgvector')
pg_service.upsert(exported_data)

# 3. Update environment
export VECTOR_BACKEND=pgvector
```

## Testing

### Parity Tests

Run the comprehensive parity tests to verify both backends work identically:

```bash
# Set up both backends
docker-compose -f docker/vector/docker-compose.qdrant.yml up -d
# Start PostgreSQL with pgvector...

# Run parity tests
python -m pytest tests/vector_backends/test_parity.py -v

# Or run directly
cd tests/vector_backends
python test_parity.py
```

### Individual Backend Tests

```bash
# Test Qdrant only
export VECTOR_BACKEND=qdrant
python -m pytest tests/vector_backends/ -k qdrant

# Test pgvector only  
export VECTOR_BACKEND=pgvector
python -m pytest tests/vector_backends/ -k pgvector
```

## Troubleshooting

### Common Issues

1. **Qdrant Connection Failed**
   ```bash
   # Check if Qdrant is running
   curl http://localhost:6333/health
   
   # Check Docker logs
   docker logs neuronews-qdrant
   ```

2. **pgvector Extension Missing**
   ```sql
   -- In PostgreSQL, check extensions
   SELECT * FROM pg_available_extensions WHERE name = 'vector';
   
   -- Install if missing
   CREATE EXTENSION IF NOT EXISTS vector;
   ```

3. **Collection Not Found (Qdrant)**
   ```python
   # Manually create collection
   service = get_vector_service(backend_type='qdrant')
   service.create_collection(force_recreate=True)
   ```

4. **Environment Variables Not Applied**
   ```bash
   # Verify environment
   echo $VECTOR_BACKEND
   echo $QDRANT_HOST
   
   # Restart application after changes
   ```

### Performance Tuning

#### Qdrant Optimization

```yaml
# docker-compose.qdrant.yml
environment:
  - QDRANT__SERVICE__MAX_REQUEST_SIZE_MB=32
  - QDRANT__SERVICE__MAX_TIMEOUT_SEC=60
  - QDRANT__STORAGE__HNSW_INDEX__M=16
  - QDRANT__STORAGE__HNSW_INDEX__EF_CONSTRUCT=200
```

#### pgvector Optimization

```sql
-- Optimize PostgreSQL for vectors
SET maintenance_work_mem = '2GB';
SET max_parallel_maintenance_workers = 4;

-- Create optimized index
CREATE INDEX CONCURRENTLY ON embeddings 
USING ivfflat (embedding vector_cosine_ops) 
WITH (lists = 1000);
```

## Roadmap

### Planned Improvements

1. **Automatic Migration Tools**
   - Scripts to migrate data between backends
   - Zero-downtime migration support

2. **Advanced Filtering**
   - More complex filter combinations
   - Full-text search integration

3. **Monitoring & Metrics**
   - Performance monitoring
   - Usage analytics

4. **Additional Backends**
   - Weaviate support
   - Pinecone integration
   - Milvus backend

### Contributing

To add a new vector backend:

1. Implement `VectorBackend` abstract class
2. Add backend to `UnifiedVectorService`
3. Create parity tests
4. Update documentation

See `services/embeddings/backends/qdrant_store.py` for reference implementation.

---

*This implementation ensures that NeuroNews can seamlessly switch between vector databases while maintaining identical functionality and performance characteristics.*
