"""
Hybrid Retrieval Service - Issue #232
Combines vector and lexical search with optional cross-encoder reranking

This module implements hybrid retrieval that merges results from:
- Vector search (semantic similarity via embeddings)
- Lexical search (BM25-like via PostgreSQL FTS)
- Optional cross-encoder reranking for improved relevance
"""

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Union, Tuple
from dataclasses import dataclass

# Import search services
from .vector import VectorSearchService, VectorSearchResult, VectorSearchFilters
from .lexical import LexicalSearchService, LexicalSearchResult, SearchFilters
from .rerank import CrossEncoderReranker, RerankResult

# Configure logging
logger = logging.getLogger(__name__)


@dataclass
class HybridSearchResult:
    """Result from hybrid search combining vector and lexical signals."""
    id: str
    doc_id: str
    chunk_id: str
    title: str
    content: str
    source: str
    url: str
    published_at: Optional[datetime]
    
    # Scoring information
    vector_score: Optional[float]
    lexical_score: Optional[float]  
    fusion_score: float
    final_score: float
    
    # Metadata
    word_count: int
    char_count: int
    search_method: str  # 'vector', 'lexical', 'both'
    

@dataclass
class HybridSearchFilters:
    """Filters for hybrid search combining vector and lexical constraints."""
    source: Optional[str] = None
    language: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    min_vector_similarity: float = 0.0
    min_lexical_rank: float = 0.0


class PartialRetrievalError(RuntimeError):
    """A list-only call cannot represent incomplete source coverage safely."""
    def __init__(self, response):
        super().__init__('Retrieval is incomplete; inspect response diagnostics')
        self.response = response


class HybridRetriever:
    """
    Hybrid retrieval service combining vector and lexical search.
    
    Features:
    - Candidate fetch from both vector and lexical search
    - Score fusion using weighted sum or Reciprocal Rank Fusion (RRF)
    - Optional cross-encoder reranking
    - Configurable search weights and thresholds
    """
    
    def __init__(
        self,
        vector_service: Optional[VectorSearchService] = None,
        lexical_service: Optional[LexicalSearchService] = None,
        reranker: Optional[CrossEncoderReranker] = None,
        connection_params: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize the hybrid retriever.
        
        Args:
            vector_service: Vector search service instance
            lexical_service: Lexical search service instance  
            reranker: Optional reranker for final scoring
            connection_params: Database connection parameters
        """
        self.connection_params = connection_params
        self.vector_service = vector_service
        self.lexical_service = lexical_service
        self.reranker = reranker
        
        # Default fusion parameters
        self.vector_weight = 0.6
        self.lexical_weight = 0.4
        self.rrf_k = 60  # RRF parameter
        
    def __enter__(self):
        """Context manager entry."""
        if self.vector_service:
            self.vector_service.connect()
        if self.lexical_service:
            self.lexical_service.connect()
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        if self.vector_service:
            self.vector_service.disconnect()
        if self.lexical_service:
            self.lexical_service.disconnect()
    
    def search(self, *args, **kwargs) -> List[HybridSearchResult]:
        response = self.search_detailed(*args, **kwargs)
        if response['status'] != 'complete':
            raise PartialRetrievalError(response)
        return response['results']

    def search_detailed(
        self,
        query: str,
        query_embedding: Optional[Union[List[float], 'np.ndarray']] = None,
        k: int = 10,
        vector_k: Optional[int] = None,
        lexical_k: Optional[int] = None,
        filters: Optional[HybridSearchFilters] = None,
        fusion_method: str = "weighted",
        enable_reranking: bool = True
    ) -> List[HybridSearchResult]:
        """
        Perform hybrid search combining vector and lexical retrieval.
        
        Args:
            query: Search query text
            query_embedding: Optional pre-computed query embedding
            k: Final number of results to return
            vector_k: Number of candidates from vector search (default: k*2)
            lexical_k: Number of candidates from lexical search (default: k*2)
            filters: Optional search filters
            fusion_method: Score fusion method ('weighted', 'rrf', 'max')
            enable_reranking: Whether to apply reranking if available
            
        Returns:
            List of hybrid search results ranked by final score
        """
        start_time = time.time()
        
        # Set default candidate counts
        if type(k) is not int or not 1 <= k <= 1000:
            raise ValueError('k must be an integer from 1 to 1000')
        vector_k = min(k * 2, 1000) if vector_k is None else vector_k
        lexical_k = min(k * 2, 1000) if lexical_k is None else lexical_k
        if any(type(v) is not int or not 1 <= v <= 1000 for v in (vector_k, lexical_k)):
            raise ValueError('candidate counts must be integers from 1 to 1000')
        diagnostics = []
        def fetch(name, service, operation):
            if service is None:
                diagnostics.append({'source': name, 'status': 'not_configured'})
                return []
            try:
                values = operation()
                diagnostics.append({'source': name, 'status': 'complete', 'count': len(values)})
                return values
            except Exception as exc:
                # Provider exception strings can contain SQL or credentials.
                diagnostics.append({'source': name, 'status': 'failed', 'error_type': type(exc).__name__})
                return []
        filters = filters or HybridSearchFilters()
        
        # Fetch candidates from both search methods
        vector_results = fetch('vector', self.vector_service,
            lambda: self._fetch_vector_candidates(query_embedding, vector_k, filters))
        lexical_results = fetch('lexical', self.lexical_service,
            lambda: self._fetch_lexical_candidates(query, lexical_k, filters))
        
        logger.info(f"Fetched {len(vector_results)} vector + {len(lexical_results)} lexical candidates")
        
        # Merge and deduplicate candidates
        merged_candidates = self._merge_candidates(vector_results, lexical_results)
        
        # Apply score fusion
        fusion_results = self._fuse_scores(merged_candidates, fusion_method)
        
        # Apply reranking if enabled
        if enable_reranking and self.reranker and self.reranker.is_enabled:
            try:
                reranked_results = self._apply_reranking(query, fusion_results)
                final_results = self._convert_reranked_results(reranked_results, fusion_results)
                diagnostics.append({'source': 'reranker', 'status': 'complete'})
            except Exception as exc:
                diagnostics.append({'source': 'reranker', 'status': 'failed', 'error_type': type(exc).__name__})
                final_results = fusion_results
        else:
            final_results = fusion_results
        
        # Sort by final score and limit to k
        final_results.sort(key=lambda x: x.final_score, reverse=True)
        final_results = final_results[:k]
        
        elapsed_time = time.time() - start_time
        logger.info(f"Hybrid search completed in {elapsed_time:.3f}s, returning {len(final_results)} results")
        
        failed = any(v['status'] == 'failed' for v in diagnostics)
        configured = any(v['status'] != 'not_configured' for v in diagnostics)
        return {'results': final_results, 'status': 'partial' if failed else 'complete' if configured else 'unavailable',
                'sources': diagnostics, 'elapsed_ms': elapsed_time * 1000,
                'candidate_limits': {'vector': vector_k, 'lexical': lexical_k},
                'cost': {'status': 'not_metered'}}

    def _fetch_vector_candidates(
        self,
        query_embedding: Optional[Union[List[float], 'np.ndarray']],
        k: int,
        filters: HybridSearchFilters
    ) -> List[VectorSearchResult]:
        """Fetch candidates from vector search."""
        if query_embedding is None or len(query_embedding) == 0:
            raise ValueError('A configured vector search requires a query embedding')
        
        try:
            vector_filters = VectorSearchFilters(
                source=filters.source,
                date_from=filters.date_from,
                date_to=filters.date_to,
                min_similarity=filters.min_vector_similarity
            )
            
            return self.vector_service.search(query_embedding, k, vector_filters)
            
        except Exception as e:
            logger.error("Vector search failed (%s)", type(e).__name__)
            raise
    
    def _fetch_lexical_candidates(
        self,
        query: str,
        k: int,
        filters: HybridSearchFilters
    ) -> List[LexicalSearchResult]:
        """Fetch candidates from lexical search."""
        try:
            lexical_filters = SearchFilters(
                source=filters.source,
                language=filters.language,
                date_from=filters.date_from,
                date_to=filters.date_to,
                min_rank=filters.min_lexical_rank
            )
            
            return self.lexical_service.search(query, k, lexical_filters)
            
        except Exception as e:
            logger.error("Lexical search failed (%s)", type(e).__name__)
            raise
    
    def _merge_candidates(
        self,
        vector_results: List[VectorSearchResult],
        lexical_results: List[LexicalSearchResult]
    ) -> List[HybridSearchResult]:
        """Merge and deduplicate candidates from vector and lexical search."""
        candidates = {}
        
        # Add vector results
        for i, result in enumerate(vector_results):
            key = (result.doc_id, result.chunk_id) if result.chunk_id else result.doc_id
            
            candidates[key] = HybridSearchResult(
                id=result.id,
                doc_id=result.doc_id,
                chunk_id=result.chunk_id,
                title=result.title,
                content=result.content,
                source=result.source,
                url=result.url,
                published_at=result.published_at,
                vector_score=result.similarity_score,
                lexical_score=None,
                fusion_score=0.0,  # Will be computed later
                final_score=0.0,   # Will be computed later
                word_count=result.word_count,
                char_count=result.char_count,
                search_method='vector'
            )
        
        # Add lexical results (merge if already exists)
        for i, result in enumerate(lexical_results):
            key = (result.doc_id, result.chunk_id) if result.chunk_id else result.doc_id
            
            if key in candidates:
                # Update existing candidate with lexical score
                candidates[key].lexical_score = result.rank
                candidates[key].search_method = 'both'
            else:
                # Add new lexical-only candidate
                candidates[key] = HybridSearchResult(
                    id=result.id,
                    doc_id=result.doc_id,
                    chunk_id=result.chunk_id,
                    title=result.title,
                    content=result.content,
                    source=result.source,
                    url=result.url,
                    published_at=result.published_at,
                    vector_score=None,
                    lexical_score=result.rank,
                    fusion_score=0.0,
                    final_score=0.0,
                    word_count=result.word_count,
                    char_count=result.char_count,
                    search_method='lexical'
                )
        
        return list(candidates.values())
    
    def _fuse_scores(
        self,
        candidates: List[HybridSearchResult],
        fusion_method: str
    ) -> List[HybridSearchResult]:
        """Apply score fusion to combine vector and lexical scores."""
        if fusion_method == "weighted":
            return self._weighted_fusion(candidates)
        elif fusion_method == "rrf":
            return self._reciprocal_rank_fusion(candidates)
        elif fusion_method == "max":
            return self._max_fusion(candidates)
        else:
            logger.warning(f"Unknown fusion method: {fusion_method}. Using weighted.")
            return self._weighted_fusion(candidates)
    
    def _weighted_fusion(self, candidates: List[HybridSearchResult]) -> List[HybridSearchResult]:
        """Apply weighted score fusion."""
        for candidate in candidates:
            vector_score = candidate.vector_score or 0.0
            lexical_score = candidate.lexical_score or 0.0
            
            # Normalize lexical score (convert rank to similarity-like score)
            lexical_normalized = min(lexical_score / 10.0, 1.0) if lexical_score > 0 else 0.0
            
            candidate.fusion_score = (
                self.vector_weight * vector_score +
                self.lexical_weight * lexical_normalized
            )
            candidate.final_score = candidate.fusion_score
        
        return candidates
    
    def _reciprocal_rank_fusion(self, candidates: List[HybridSearchResult]) -> List[HybridSearchResult]:
        """Apply Reciprocal Rank Fusion (RRF)."""
        # Create ranking lists
        vector_ranking = {
            (c.doc_id, c.chunk_id): i + 1
            for i, c in enumerate(
                sorted([c for c in candidates if c.vector_score is not None],
                       key=lambda x: x.vector_score, reverse=True)
            )
        }
        
        lexical_ranking = {
            (c.doc_id, c.chunk_id): i + 1
            for i, c in enumerate(
                sorted([c for c in candidates if c.lexical_score is not None],
                       key=lambda x: x.lexical_score, reverse=True)
            )
        }
        
        # Apply RRF formula
        for candidate in candidates:
            key = (candidate.doc_id, candidate.chunk_id)
            
            vector_rrf = 1.0 / (self.rrf_k + vector_ranking.get(key, float('inf')))
            lexical_rrf = 1.0 / (self.rrf_k + lexical_ranking.get(key, float('inf')))
            
            candidate.fusion_score = vector_rrf + lexical_rrf
            candidate.final_score = candidate.fusion_score
        
        return candidates
    
    def _max_fusion(self, candidates: List[HybridSearchResult]) -> List[HybridSearchResult]:
        """Apply max score fusion."""
        for candidate in candidates:
            vector_score = candidate.vector_score or 0.0
            lexical_score = min(candidate.lexical_score / 10.0, 1.0) if candidate.lexical_score else 0.0
            
            candidate.fusion_score = max(vector_score, lexical_score)
            candidate.final_score = candidate.fusion_score
        
        return candidates
    
    def _apply_reranking(
        self,
        query: str,
        candidates: List[HybridSearchResult]
    ) -> List[RerankResult]:
        """Apply cross-encoder reranking to candidates."""
        # Convert to format expected by reranker
        rerank_candidates = []
        for candidate in candidates:
            rerank_candidates.append({
                'title': candidate.title,
                'content': candidate.content,
                'source': candidate.source,
                'url': candidate.url,
                'score': candidate.fusion_score
            })
        
        return self.reranker.rerank(query, rerank_candidates, require_model=True)
    
    def _convert_reranked_results(
        self,
        reranked: List[RerankResult],
        original: List[HybridSearchResult]
    ) -> List[HybridSearchResult]:
        """Convert reranked results back to HybridSearchResult format."""
        reranked_results = []
        
        for rerank_result in reranked:
            original_candidate = original[rerank_result.original_index]
            
            # Update final score with reranking
            original_candidate.final_score = rerank_result.final_score
            
            reranked_results.append(original_candidate)
        
        return reranked_results
    
    def get_search_stats(self) -> Dict[str, Any]:
        """Get statistics about the hybrid search components."""
        stats = {
            'vector_service_available': self.vector_service is not None,
            'lexical_service_available': self.lexical_service is not None,
            'reranker_available': self.reranker is not None,
            'reranker_enabled': self.reranker.is_enabled if self.reranker else False,
            'fusion_weights': {
                'vector_weight': self.vector_weight,
                'lexical_weight': self.lexical_weight,
                'rrf_k': self.rrf_k
            },
            'timestamp': datetime.utcnow().isoformat()
        }
        
        # Add component-specific stats
        try:
            if self.vector_service:
                stats['vector_stats'] = self.vector_service.get_search_stats()
        except Exception as e:
            logger.error(f"Failed to get vector stats: {e}")
        
        try:
            if self.lexical_service:
                stats['lexical_stats'] = self.lexical_service.get_search_stats()
        except Exception as e:
            logger.error(f"Failed to get lexical stats: {e}")
        
        if self.reranker:
            stats['reranker_info'] = self.reranker.get_model_info()
        
        return stats


def get_hybrid_retriever(
    connection_params: Optional[Dict[str, Any]] = None,
    enable_reranking: bool = True
) -> HybridRetriever:
    """
    Factory function to create a fully configured hybrid retriever.
    
    Args:
        connection_params: Database connection parameters
        enable_reranking: Whether to enable cross-encoder reranking
        
    Returns:
        Configured hybrid retriever instance
    """
    vector_service = VectorSearchService(connection_params)
    lexical_service = LexicalSearchService(connection_params)
    
    reranker = None
    if enable_reranking:
        reranker = CrossEncoderReranker()
    
    return HybridRetriever(
        vector_service=vector_service,
        lexical_service=lexical_service,
        reranker=reranker,
        connection_params=connection_params
    )


def hybrid_search(
    query: str,
    query_embedding: Optional[Union[List[float], 'np.ndarray']] = None,
    k: int = 10,
    fusion_method: str = "weighted",
    enable_reranking: bool = True,
    connection_params: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    Convenience function for hybrid search.
    
    Args:
        query: Search query text
        query_embedding: Optional pre-computed query embedding
        k: Number of results to return
        fusion_method: Score fusion method
        enable_reranking: Whether to enable reranking
        connection_params: Database connection parameters
        
    Returns:
        List of search results as dictionaries
    """
    with get_hybrid_retriever(connection_params, enable_reranking) as retriever:
        results = retriever.search(
            query=query,
            query_embedding=query_embedding,
            k=k,
            fusion_method=fusion_method,
            enable_reranking=enable_reranking
        )
        
        # Convert to dict format for API responses
        return [
            {
                'chunk': result.content,
                'score': result.final_score,
                'source': result.source,
                'url': result.url,
                'title': result.title,
                'published_at': result.published_at.isoformat() if result.published_at else None,
                'search_method': result.search_method,
                'vector_score': result.vector_score,
                'lexical_score': result.lexical_score,
                'fusion_score': result.fusion_score
            }
            for result in results
        ]
