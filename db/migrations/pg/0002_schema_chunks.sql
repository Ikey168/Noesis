-- Migration: 0002_schema_chunks.sql
-- Description: Create tables for document chunks, embeddings, and search functionality
-- Author: NeuroNews Team
-- Date: 2025-08-26

-- Create documents table
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    article_id VARCHAR(255) NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    source VARCHAR(100) NOT NULL,
    category VARCHAR(50),
    published_at TIMESTAMP WITH TIME ZONE NOT NULL,
    processed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    language VARCHAR(10) DEFAULT 'en',
    word_count INTEGER,
    metadata JSONB DEFAULT '{}',
    
    -- Indexes for common queries
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create chunks table for document segments
CREATE TABLE IF NOT EXISTS chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    word_count INTEGER NOT NULL,
    char_count INTEGER NOT NULL,
    
    -- Chunk metadata
    chunk_type VARCHAR(20) DEFAULT 'paragraph', -- paragraph, sentence, sliding_window
    overlap_tokens INTEGER DEFAULT 0,
    
    -- Position within document
    start_position INTEGER,
    end_position INTEGER,
    
    -- Additional metadata
    metadata JSONB DEFAULT '{}',
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    
    -- Ensure unique chunks per document
    UNIQUE(document_id, chunk_index)
);

-- Create embeddings table with vector storage
CREATE TABLE IF NOT EXISTS embeddings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    
    -- Vector embedding (default to 384 dimensions for sentence-transformers/all-MiniLM-L6-v2)
    embedding vector(384) NOT NULL,
    
    -- Model information
    model_name VARCHAR(100) NOT NULL DEFAULT 'sentence-transformers/all-MiniLM-L6-v2',
    model_version VARCHAR(50) DEFAULT 'latest',
    embedding_dimension INTEGER NOT NULL DEFAULT 384,
    
    -- Quality metrics
    confidence_score REAL DEFAULT 1.0,
    processing_time_ms INTEGER,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    
    -- Ensure one embedding per chunk per model
    UNIQUE(chunk_id, model_name, model_version)
);

-- Create inverted_terms table for hybrid search (optional but recommended)
CREATE TABLE IF NOT EXISTS inverted_terms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    term VARCHAR(100) NOT NULL,
    term_frequency REAL NOT NULL,
    inverse_document_frequency REAL,
    tf_idf_score REAL,
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    
    -- Ensure unique terms per chunk
    UNIQUE(chunk_id, term)
);

-- Create search_logs table for analytics and optimization
CREATE TABLE IF NOT EXISTS search_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Query information
    query_text TEXT NOT NULL,
    query_embedding vector(384),
    query_type VARCHAR(20) DEFAULT 'semantic', -- semantic, keyword, hybrid
    
    -- Search parameters
    similarity_threshold REAL DEFAULT 0.5,
    max_results INTEGER DEFAULT 10,
    search_method VARCHAR(20) DEFAULT 'cosine', -- cosine, l2, inner_product
    
    -- Results
    results_count INTEGER NOT NULL,
    top_similarity_score REAL,
    processing_time_ms INTEGER,
    
    -- User context
    user_id VARCHAR(100),
    session_id VARCHAR(100),
    ip_address INET,
    user_agent TEXT,
    
    -- Metadata
    metadata JSONB DEFAULT '{}',
    
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for optimal performance

-- Primary indexes on documents
CREATE INDEX IF NOT EXISTS idx_documents_article_id ON documents(article_id);
CREATE INDEX IF NOT EXISTS idx_documents_published_at ON documents(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category);
CREATE INDEX IF NOT EXISTS idx_documents_language ON documents(language);
CREATE INDEX IF NOT EXISTS idx_documents_processed_at ON documents(processed_at DESC);

-- Indexes on chunks
CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chunk_index ON chunks(chunk_index);
CREATE INDEX IF NOT EXISTS idx_chunks_word_count ON chunks(word_count);
CREATE INDEX IF NOT EXISTS idx_chunks_chunk_type ON chunks(chunk_type);

-- Vector indexes on embeddings (using IVFFlat for now, can be changed to HNSW)
-- Note: IVFFlat is generally faster for smaller datasets, HNSW for larger ones
CREATE INDEX IF NOT EXISTS idx_embeddings_vector_cosine 
ON embeddings USING ivfflat (embedding vector_cosine_ops) 
WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_embeddings_vector_l2 
ON embeddings USING ivfflat (embedding vector_l2_ops) 
WITH (lists = 100);

-- Alternative HNSW indexes (comment out IVFFlat above if using these)
-- CREATE INDEX IF NOT EXISTS idx_embeddings_vector_cosine_hnsw 
-- ON embeddings USING hnsw (embedding vector_cosine_ops) 
-- WITH (m = 16, ef_construction = 64);

-- CREATE INDEX IF NOT EXISTS idx_embeddings_vector_l2_hnsw 
-- ON embeddings USING hnsw (embedding vector_l2_ops) 
-- WITH (m = 16, ef_construction = 64);

-- Indexes on embeddings
CREATE INDEX IF NOT EXISTS idx_embeddings_chunk_id ON embeddings(chunk_id);
CREATE INDEX IF NOT EXISTS idx_embeddings_model_name ON embeddings(model_name);
CREATE INDEX IF NOT EXISTS idx_embeddings_created_at ON embeddings(created_at DESC);

-- Indexes on inverted_terms for keyword search
CREATE INDEX IF NOT EXISTS idx_inverted_terms_chunk_id ON inverted_terms(chunk_id);
CREATE INDEX IF NOT EXISTS idx_inverted_terms_term ON inverted_terms(term);
CREATE INDEX IF NOT EXISTS idx_inverted_terms_tf_idf ON inverted_terms(tf_idf_score DESC);

-- Indexes on search_logs for analytics
CREATE INDEX IF NOT EXISTS idx_search_logs_created_at ON search_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_logs_query_type ON search_logs(query_type);
CREATE INDEX IF NOT EXISTS idx_search_logs_user_id ON search_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_search_logs_session_id ON search_logs(session_id);

-- Create materialized view for document statistics
CREATE MATERIALIZED VIEW IF NOT EXISTS document_stats AS
SELECT 
    d.source,
    d.category,
    d.language,
    COUNT(*) as document_count,
    AVG(d.word_count) as avg_word_count,
    COUNT(c.id) as total_chunks,
    AVG(c.word_count) as avg_chunk_size,
    COUNT(e.id) as total_embeddings,
    DATE(d.published_at) as published_date
FROM documents d
LEFT JOIN chunks c ON d.id = c.document_id
LEFT JOIN embeddings e ON c.id = e.chunk_id
GROUP BY d.source, d.category, d.language, DATE(d.published_at)
ORDER BY published_date DESC;

-- Create index on materialized view
CREATE INDEX IF NOT EXISTS idx_document_stats_source ON document_stats(source);
CREATE INDEX IF NOT EXISTS idx_document_stats_published_date ON document_stats(published_date DESC);

-- Create view for search performance analytics
CREATE OR REPLACE VIEW search_analytics AS
SELECT 
    DATE(created_at) as search_date,
    query_type,
    COUNT(*) as total_searches,
    AVG(processing_time_ms) as avg_processing_time,
    AVG(results_count) as avg_results_count,
    AVG(top_similarity_score) as avg_top_score,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY processing_time_ms) as median_processing_time,
    PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY processing_time_ms) as p95_processing_time
FROM search_logs
GROUP BY DATE(created_at), query_type
ORDER BY search_date DESC, query_type;

-- Create functions for common operations

-- Function to search similar documents using cosine similarity
CREATE OR REPLACE FUNCTION search_similar_documents(
    query_embedding vector(384),
    similarity_threshold REAL DEFAULT 0.5,
    max_results INTEGER DEFAULT 10
)
RETURNS TABLE(
    document_id UUID,
    chunk_id UUID,
    similarity_score REAL,
    title TEXT,
    content TEXT,
    source VARCHAR(100),
    published_at TIMESTAMP WITH TIME ZONE
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        d.id,
        c.id,
        vector_ops.cosine_similarity(e.embedding, query_embedding) as similarity,
        d.title,
        c.content,
        d.source,
        d.published_at
    FROM embeddings e
    JOIN chunks c ON e.chunk_id = c.id
    JOIN documents d ON c.document_id = d.id
    WHERE vector_ops.cosine_similarity(e.embedding, query_embedding) >= similarity_threshold
    ORDER BY similarity DESC
    LIMIT max_results;
END;
$$ LANGUAGE plpgsql;

-- Function to get document chunks with embeddings
CREATE OR REPLACE FUNCTION get_document_chunks(doc_id UUID)
RETURNS TABLE(
    chunk_id UUID,
    chunk_index INTEGER,
    content TEXT,
    word_count INTEGER,
    has_embedding BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        c.id,
        c.chunk_index,
        c.content,
        c.word_count,
        (e.id IS NOT NULL) as has_embedding
    FROM chunks c
    LEFT JOIN embeddings e ON c.id = e.chunk_id
    WHERE c.document_id = doc_id
    ORDER BY c.chunk_index;
END;
$$ LANGUAGE plpgsql;

-- Function to refresh document statistics
CREATE OR REPLACE FUNCTION refresh_document_stats()
RETURNS VOID AS $$
BEGIN
    REFRESH MATERIALIZED VIEW document_stats;
    RAISE NOTICE 'Document statistics refreshed';
END;
$$ LANGUAGE plpgsql;

-- Add triggers for updated_at timestamps
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_documents_updated_at ON documents;
CREATE TRIGGER update_documents_updated_at 
    BEFORE UPDATE ON documents 
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Grant permissions
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO neuronews;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO neuronews;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO neuronews;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA vector_ops TO neuronews;

-- Log completion
DO $$
BEGIN
    RAISE NOTICE 'Migration 0002_schema_chunks.sql completed successfully';
    RAISE NOTICE 'Created tables: documents, chunks, embeddings, inverted_terms, search_logs';
    RAISE NOTICE 'Created indexes optimized for vector similarity search';
    RAISE NOTICE 'Created helper functions and views for search operations';
END $$;
