"""
Cross-Encoder Reranking Service for Hybrid Retrieval
Part of Issue #232: Optional reranking step for improved relevance

This module provides cross-encoder reranking using models like ms-marco-MiniLM-L-6-v2
to improve the final ranking of retrieval candidates.
"""

import logging
import os
import time
from typing import Dict, List, Optional, Any, Union, Tuple
from dataclasses import dataclass

# Configure logging
logger = logging.getLogger(__name__)

# Optional dependencies for reranking
try:
    from sentence_transformers import CrossEncoder
    HAS_SENTENCE_TRANSFORMERS = True
except ImportError:
    HAS_SENTENCE_TRANSFORMERS = False
    logger.warning("sentence-transformers not available. Reranking will use fallback scoring.")

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


@dataclass
class RerankResult:
    """Result from reranking with updated scores."""
    original_index: int
    original_score: float
    rerank_score: float
    final_score: float
    content: str
    title: str
    source: str
    url: str


class CrossEncoderReranker:
    """
    Cross-encoder reranking service for improving retrieval relevance.
    
    Features:
    - Cross-encoder models (ms-marco-MiniLM-L-6-v2, etc.)
    - Score fusion strategies (weighted, max, product)
    - Configurable reranking thresholds
    - Fallback scoring when models unavailable
    """
    
    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        device: Optional[str] = None,
        max_length: int = 512
    ):
        """
        Initialize the cross-encoder reranker.
        
        Args:
            model_name: Name of the cross-encoder model
            device: Device to run the model on (cpu/cuda)
            max_length: Maximum sequence length for the model
        """
        self.model_name = model_name
        self.device = device
        self.max_length = max_length
        self.model = None
        self.is_enabled = self._check_reranking_enabled()
        
        if self.is_enabled and HAS_SENTENCE_TRANSFORMERS:
            self._load_model()
        elif self.is_enabled:
            logger.warning("Reranking enabled but sentence-transformers not available. Using fallback.")
    
    def _check_reranking_enabled(self) -> bool:
        """Check if reranking is enabled via environment variable."""
        return os.getenv('ENABLE_RERANKING', 'false').lower() in ('true', '1', 'yes', 'on')
    
    def _load_model(self):
        """Load the cross-encoder model."""
        try:
            self.model = CrossEncoder(self.model_name, device=self.device, max_length=self.max_length)
            logger.info(f"Loaded cross-encoder model: {self.model_name}")
        except Exception as e:
            logger.error(f"Failed to load cross-encoder model {self.model_name}: {e}")
            self.model = None
    
    def rerank(
        self,
        query: str,
        candidates: List[Dict[str, Any]],
        top_k: Optional[int] = None,
        score_fusion: str = "weighted",
        fusion_weight: float = 0.7,
        require_model: bool = False,
    ) -> List[RerankResult]:
        """
        Rerank retrieval candidates using cross-encoder scoring.
        
        Args:
            query: The search query
            candidates: List of candidate documents with scores
            top_k: Number of top results to return (None for all)
            score_fusion: Strategy for combining scores ('weighted', 'max', 'product', 'rerank_only')
            fusion_weight: Weight for original scores in weighted fusion
            
        Returns:
            List of reranked results with updated scores
        """
        if require_model and (not self.is_enabled or self.model is None):
            raise RuntimeError('Configured cross-encoder model is unavailable')
        if not self.is_enabled:
            logger.info("Reranking disabled. Returning original candidates.")
            return self._fallback_rerank(candidates, top_k)
        
        if not candidates:
            return []
        
        start_time = time.time()
        
        try:
            # Prepare query-document pairs for reranking
            query_doc_pairs = []
            for candidate in candidates:
                # Combine title and content for better context
                doc_text = f"{candidate.get('title', '')} {candidate.get('content', '')}"
                query_doc_pairs.append([query, doc_text])
            
            # Get reranking scores
            if self.model is not None:
                rerank_scores = self._cross_encoder_score(query_doc_pairs)
            else:
                rerank_scores = self._fallback_score(query, candidates)
            
            # Create rerank results with score fusion
            rerank_results = []
            for i, (candidate, rerank_score) in enumerate(zip(candidates, rerank_scores)):
                original_score = candidate.get('score', candidate.get('similarity_score', candidate.get('rank', 0.0)))
                
                # Apply score fusion strategy
                final_score = self._fuse_scores(
                    original_score, rerank_score, score_fusion, fusion_weight
                )
                
                result = RerankResult(
                    original_index=i,
                    original_score=float(original_score),
                    rerank_score=float(rerank_score),
                    final_score=float(final_score),
                    content=candidate.get('content', ''),
                    title=candidate.get('title', ''),
                    source=candidate.get('source', ''),
                    url=candidate.get('url', '')
                )
                rerank_results.append(result)
            
            # Sort by final score and apply top_k
            rerank_results.sort(key=lambda x: x.final_score, reverse=True)
            if top_k is not None:
                rerank_results = rerank_results[:top_k]
            
            elapsed_time = time.time() - start_time
            logger.info(f"Reranked {len(candidates)} candidates in {elapsed_time:.3f}s")
            
            return rerank_results
            
        except Exception as e:
            if require_model:
                raise
            logger.error(f"Reranking failed: {e}. Using fallback.")
            return self._fallback_rerank(candidates, top_k)
    
    def _cross_encoder_score(self, query_doc_pairs: List[List[str]]) -> List[float]:
        """Score query-document pairs using cross-encoder model."""
        if not self.model:
            raise ValueError("Cross-encoder model not loaded")
        
        # Predict relevance scores
        scores = self.model.predict(query_doc_pairs)
        
        # Convert to list if numpy array
        if HAS_NUMPY and isinstance(scores, np.ndarray):
            scores = scores.tolist()
        
        return scores
    
    def _fallback_score(self, query: str, candidates: List[Dict[str, Any]]) -> List[float]:
        """Fallback scoring when cross-encoder is not available."""
        scores = []
        query_words = set(query.lower().split())
        
        for candidate in candidates:
            # Simple text overlap scoring
            text = f"{candidate.get('title', '')} {candidate.get('content', '')}"
            text_words = set(text.lower().split())
            
            # Calculate Jaccard similarity
            intersection = len(query_words & text_words)
            union = len(query_words | text_words)
            
            if union == 0:
                score = 0.0
            else:
                score = intersection / union
            
            scores.append(score)
        
        return scores
    
    def _fuse_scores(
        self,
        original_score: float,
        rerank_score: float,
        fusion_strategy: str,
        fusion_weight: float
    ) -> float:
        """Fuse original retrieval score with reranking score."""
        if fusion_strategy == "weighted":
            return fusion_weight * original_score + (1 - fusion_weight) * rerank_score
        elif fusion_strategy == "max":
            return max(original_score, rerank_score)
        elif fusion_strategy == "product":
            return original_score * rerank_score
        elif fusion_strategy == "rerank_only":
            return rerank_score
        else:
            logger.warning(f"Unknown fusion strategy: {fusion_strategy}. Using weighted.")
            return fusion_weight * original_score + (1 - fusion_weight) * rerank_score
    
    def _fallback_rerank(
        self,
        candidates: List[Dict[str, Any]],
        top_k: Optional[int] = None
    ) -> List[RerankResult]:
        """Fallback reranking that preserves original order."""
        results = []
        for i, candidate in enumerate(candidates):
            original_score = candidate.get('score', candidate.get('similarity_score', candidate.get('rank', 0.0)))
            
            result = RerankResult(
                original_index=i,
                original_score=float(original_score),
                rerank_score=float(original_score),  # Same as original
                final_score=float(original_score),
                content=candidate.get('content', ''),
                title=candidate.get('title', ''),
                source=candidate.get('source', ''),
                url=candidate.get('url', '')
            )
            results.append(result)
        
        if top_k is not None:
            results = results[:top_k]
        
        return results
    
    def get_model_info(self) -> Dict[str, Any]:
        """Get information about the loaded model."""
        return {
            'model_name': self.model_name,
            'device': self.device,
            'max_length': self.max_length,
            'is_enabled': self.is_enabled,
            'model_loaded': self.model is not None,
            'has_sentence_transformers': HAS_SENTENCE_TRANSFORMERS,
        }


def get_reranker(
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    device: Optional[str] = None
) -> CrossEncoderReranker:
    """Factory function to get a reranker instance."""
    return CrossEncoderReranker(model_name, device)


def rerank_candidates(
    query: str,
    candidates: List[Dict[str, Any]],
    top_k: Optional[int] = None,
    model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
    score_fusion: str = "weighted"
) -> List[RerankResult]:
    """
    Convenience function to rerank candidates.
    
    Args:
        query: Search query
        candidates: List of candidate documents
        top_k: Number of top results to return
        model_name: Cross-encoder model name
        score_fusion: Score fusion strategy
        
    Returns:
        List of reranked results
    """
    reranker = CrossEncoderReranker(model_name)
    return reranker.rerank(query, candidates, top_k, score_fusion)
