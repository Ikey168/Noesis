-- Migration: 0003_add_indexer_columns.sql
-- Description: Add columns to support CLI indexer with direct doc_id/chunk_id upsert
-- Issue #230: Indexer job (embed + upsert)

-- Add columns to chunks table for CLI indexer compatibility
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS doc_id VARCHAR(255);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS chunk_id VARCHAR(255);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS title TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS source VARCHAR(100);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS published_at TIMESTAMP WITH TIME ZONE;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS url TEXT;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS language VARCHAR(10) DEFAULT 'en';
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector(384);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- Create indexes for the new columns
CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_chunk_id ON chunks(chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunks_doc_chunk_composite ON chunks(doc_id, chunk_id);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source);
CREATE INDEX IF NOT EXISTS idx_chunks_language ON chunks(language);
CREATE INDEX IF NOT EXISTS idx_chunks_published_at ON chunks(published_at);

-- Create vector similarity index for embeddings (if not exists)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding_cosine ON chunks 
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Add unique constraint for (doc_id, chunk_id) combination
-- This will be used for UPSERT operations as specified in issue #230
DO $$
BEGIN
    BEGIN
        ALTER TABLE chunks ADD CONSTRAINT chunks_doc_chunk_unique UNIQUE (doc_id, chunk_id);
    EXCEPTION
        WHEN duplicate_table THEN
            -- Constraint already exists, ignore
            NULL;
    END;
END $$;

-- Update existing records to have proper chunk_id format if needed
UPDATE chunks 
SET chunk_id = CONCAT(COALESCE(doc_id, 'doc_unknown'), '_chunk_', chunk_index)
WHERE chunk_id IS NULL AND doc_id IS NOT NULL;

-- Update trigger for updated_at timestamp
CREATE OR REPLACE FUNCTION update_chunks_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_chunks_updated_at ON chunks;
CREATE TRIGGER trigger_chunks_updated_at
    BEFORE UPDATE ON chunks
    FOR EACH ROW
    EXECUTE FUNCTION update_chunks_timestamp();

DO $$
BEGIN
    RAISE NOTICE 'Migration 0003_add_indexer_columns.sql completed successfully';
    RAISE NOTICE 'Added CLI indexer support columns: doc_id, chunk_id, title, source, etc.';
    RAISE NOTICE 'Added unique constraint on (doc_id, chunk_id) for UPSERT operations';
    RAISE NOTICE 'Added vector similarity index for embeddings';
END $$;
