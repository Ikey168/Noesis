"""
Vector Search Service using PostgreSQL pgvector
Part of hybrid retrieval system for Issue #232

This module provides semantic search capabilities using PostgreSQL's
pgvector extension for similarity search with embeddings.
"""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional, Union, Any, Tuple
from dataclasses import dataclass

import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor

# Configure logging
logger = logging.getLogger(__name__)


@dataclass
class VectorSearchResult:
    """Result from vector similarity search."""
    id: str
    doc_id: str
    chunk_id: str
    title: str
    content: str
    source: str
    url: str
    published_at: Optional[datetime]
    similarity_score: float
    word_count: int
    char_count: int


@dataclass 
class VectorSearchFilters:
    """Filters for vector search queries."""
    source: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    min_similarity: float = 0.0


class VectorSearchService:
    """
    PostgreSQL pgvector-based semantic search service.
    
    Features:
    - Cosine similarity search with embeddings
    - Configurable similarity thresholds
    - Source and date filtering
    - Performance monitoring
    """
    
    def __init__(self, connection_params: Optional[Dict[str, Any]] = None):
        """Initialize the vector search service."""
        self.connection_params = connection_params or self._default_connection_params()
        self.connection = None
        
    def _default_connection_params(self) -> Dict[str, Any]:
        """Get default database connection parameters."""
        return {
            'host': os.getenv('POSTGRES_HOST', 'localhost'),
            'port': int(os.getenv('POSTGRES_PORT', 5433)),
            'database': os.getenv('POSTGRES_DB', 'neuronews_vector'),
            'user': os.getenv('POSTGRES_USER', 'neuronews'),
            'password': os.getenv('POSTGRES_PASSWORD', 'neuronews'),
        }
    
    def connect(self):
        """Connect to PostgreSQL database."""
        try:
            self.connection = psycopg2.connect(**self.connection_params)
            logger.info("Connected to PostgreSQL for vector search")
        except Exception as e:
            logger.error(f"Failed to connect to PostgreSQL: {e}")
            raise
    
    def disconnect(self):
        """Disconnect from database."""
        if self.connection:
            self.connection.close()
            self.connection = None
            logger.info("Disconnected from PostgreSQL")
    
    def __enter__(self):
        """Context manager entry."""
        self.connect()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.disconnect()
    
    def search(
        self,
        query_embedding: Union[np.ndarray, List[float]],
        k: int = 10,
        filters: Optional[VectorSearchFilters] = None
    ) -> List[VectorSearchResult]:
        """
        Perform vector similarity search using PostgreSQL pgvector.
        
        Args:
            query_embedding: Query embedding vector
            k: Number of results to return (default: 10)
            filters: Optional search filters
            
        Returns:
            List of ranked search results by similarity
        """
        if not self.connection:
            raise RuntimeError("Not connected to database. Call connect() first or use context manager.")
        
        filters = filters or VectorSearchFilters()
        
        # Convert numpy array to list if needed
        if isinstance(query_embedding, np.ndarray):
            query_embedding = query_embedding.tolist()
        
        try:
            with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
                # Build the SQL query with filters
                where_conditions = []
                params = [query_embedding, query_embedding, filters.min_similarity]
                
                if filters.source:
                    where_conditions.append("d.source = %s")
                    params.append(filters.source)
                
                if filters.date_from:
                    where_conditions.append("d.published_at >= %s")
                    params.append(filters.date_from)
                    
                if filters.date_to:
                    where_conditions.append("d.published_at <= %s")
                    params.append(filters.date_to)
                params.append(k)
                
                where_clause = ""
                if where_conditions:
                    where_clause = "AND " + " AND ".join(where_conditions)
                
                # Execute vector similarity search
                query = f"""
                    SELECT 
                        c.id,
                        COALESCE(c.doc_id, d.article_id) AS doc_id,
                        c.chunk_id,
                        d.title,
                        c.content,
                        d.source,
                        d.url,
                        d.published_at,
                        vector_ops.cosine_similarity(e.embedding, %s::vector) as similarity_score,
                        c.word_count,
                        c.char_count
                    FROM embeddings e
                    JOIN chunks c ON e.chunk_id = c.id
                    JOIN documents d ON c.document_id = d.id
                    WHERE vector_ops.cosine_similarity(e.embedding, %s::vector) >= %s
                    {where_clause}
                    ORDER BY similarity_score DESC
                    LIMIT %s
                """
                
                cursor.execute(query, params)
                results = cursor.fetchall()
                
                # Convert to VectorSearchResult objects
                search_results = []
                for row in results:
                    result = VectorSearchResult(
                        id=str(row['id']),
                        doc_id=row['doc_id'] or '',
                        chunk_id=row['chunk_id'] or '',
                        title=row['title'] or '',
                        content=row['content'] or '',
                        source=row['source'] or '',
                        url=row['url'] or '',
                        published_at=row['published_at'],
                        similarity_score=float(row['similarity_score']),
                        word_count=row['word_count'] or 0,
                        char_count=row['char_count'] or 0,
                    )
                    search_results.append(result)
                
                logger.info(f"Vector search returned {len(search_results)} results")
                return search_results
                
        except Exception as e:
            logger.error(f"Vector search failed: {e}")
            raise
    
    def search_by_function(
        self,
        query_embedding: Union[np.ndarray, List[float]],
        similarity_threshold: float = 0.5,
        k: int = 10
    ) -> List[VectorSearchResult]:
        """
        Search using the database's search_similar_documents function.
        
        Args:
            query_embedding: Query embedding vector
            similarity_threshold: Minimum similarity threshold
            k: Number of results to return
            
        Returns:
            List of ranked search results
        """
        if not self.connection:
            raise RuntimeError("Not connected to database. Call connect() first or use context manager.")
        
        # Convert numpy array to list if needed
        if isinstance(query_embedding, np.ndarray):
            query_embedding = query_embedding.tolist()
        
        try:
            with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT * FROM search_similar_documents(%s::vector, %s, %s)
                """, (query_embedding, similarity_threshold, k))
                
                results = cursor.fetchall()
                
                # Convert to VectorSearchResult objects
                search_results = []
                for row in results:
                    result = VectorSearchResult(
                        id=str(row['document_id']),
                        doc_id='',  # Not in function output
                        chunk_id=str(row['chunk_id']),
                        title=row['title'] or '',
                        content=row['content'] or '',
                        source=row['source'] or '',
                        url='',  # Not in function output
                        published_at=row['published_at'],
                        similarity_score=float(row['similarity_score']),
                        word_count=0,  # Not in function output
                        char_count=0,  # Not in function output
                    )
                    search_results.append(result)
                
                logger.info(f"Vector search (function) returned {len(search_results)} results")
                return search_results
                
        except Exception as e:
            logger.error(f"Vector search (function) failed: {e}")
            raise
    
    def get_search_stats(self) -> Dict[str, Any]:
        """Get statistics about the vector search index."""
        if not self.connection:
            raise RuntimeError("Not connected to database. Call connect() first or use context manager.")
        
        try:
            with self.connection.cursor(cursor_factory=RealDictCursor) as cursor:
                # Get embedding statistics
                cursor.execute("""
                    SELECT 
                        COUNT(*) as total_embeddings,
                        AVG(array_length(embedding, 1)) as avg_dimension,
                        MIN(created_at) as oldest_embedding,
                        MAX(created_at) as newest_embedding
                    FROM embeddings
                """)
                
                stats = cursor.fetchone()
                
                # Get index statistics
                cursor.execute("""
                    SELECT 
                        schemaname,
                        tablename,
                        indexname,
                        idx_scan,
                        idx_tup_read,
                        idx_tup_fetch
                    FROM pg_stat_user_indexes 
                    WHERE tablename = 'embeddings'
                """)
                
                index_stats = cursor.fetchall()
                
                return {
                    'total_embeddings': stats['total_embeddings'],
                    'avg_dimension': stats['avg_dimension'],
                    'oldest_embedding': stats['oldest_embedding'],
                    'newest_embedding': stats['newest_embedding'],
                    'index_statistics': [dict(row) for row in index_stats],
                    'timestamp': datetime.utcnow().isoformat()
                }
                
        except Exception as e:
            logger.error(f"Failed to get search stats: {e}")
            raise


def get_vector_search_service(connection_params: Optional[Dict[str, Any]] = None) -> VectorSearchService:
    """Factory function to get a vector search service instance."""
    return VectorSearchService(connection_params)


# Convenience functions for direct usage
def vector_search(
    query_embedding: Union[np.ndarray, List[float]],
    k: int = 10,
    filters: Optional[VectorSearchFilters] = None,
    connection_params: Optional[Dict[str, Any]] = None
) -> List[VectorSearchResult]:
    """
    Perform vector search with default service.
    
    Args:
        query_embedding: Query embedding vector
        k: Number of results to return
        filters: Optional search filters
        connection_params: Optional database connection parameters
        
    Returns:
        List of search results
    """
    with VectorSearchService(connection_params) as service:
        return service.search(query_embedding, k, filters)


def simple_vector_search(
    query_embedding: Union[np.ndarray, List[float]],
    k: int = 10,
    similarity_threshold: float = 0.5,
    connection_params: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Perform simple vector search using database function.
    
    Args:
        query_embedding: Query embedding vector
        k: Number of results to return
        similarity_threshold: Minimum similarity threshold
        connection_params: Optional database connection parameters
        
    Returns:
        List of simple search results as dicts
    """
    with VectorSearchService(connection_params) as service:
        results = service.search_by_function(query_embedding, similarity_threshold, k)
        return [
            {
                'doc_id': result.doc_id,
                'chunk_id': result.chunk_id,
                'title': result.title,
                'similarity_score': result.similarity_score,
                'content': result.content[:200] + '...' if len(result.content) > 200 else result.content,
            }
            for result in results
        ]
