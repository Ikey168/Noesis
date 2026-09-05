"""
Unified Embedding Provider with Pluggable Backends
Issue #228: Embedding provider (local + cloud pluggable)

This module provides a unified interface for generating embeddings with
support for multiple backends including local sentence-transformers
and cloud providers like OpenAI.
"""

import os
import logging
from abc import ABC, abstractmethod
from typing import List, Optional, Union
import numpy as np

# Configure logging
logger = logging.getLogger(__name__)



class EmbeddingBackend(ABC):
    """Abstract base class for embedding backends."""
    
    @abstractmethod
    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """
        Generate embeddings for a list of texts.
        
        Args:
            texts: List of text strings to embed
            
        Returns:
            numpy array of shape [N, dim] where N is len(texts)
        """
        pass
    
    @abstractmethod
    def dim(self) -> int:
        """Return the embedding dimension."""
        pass
    
    @abstractmethod
    def name(self) -> str:
        """Return the backend name."""
        pass


class EmbeddingProvider:
    """
    Unified embedding provider with pluggable backends.
    
    Supports local sentence-transformers and cloud providers
    with consistent API and configurable parameters.
    """
    
    def __init__(
        self,
        provider: str = "local",
        model_name: Optional[str] = None,
        batch_size: int = 32,
        deterministic_seed: Optional[int] = None,
        max_retries: int = 3,
        **kwargs
    ):
        """
        Initialize embedding provider.
        
        Args:
            provider: Backend provider ("local" or "openai")
            model_name: Model name (defaults based on provider)
            batch_size: Batch size for processing
            deterministic_seed: Seed for reproducible results
            max_retries: Maximum retry attempts for failed requests
            **kwargs: Additional provider-specific arguments
        """
        self.provider_name = provider
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.deterministic_seed = deterministic_seed
        
        # Set deterministic seed if specified
        if deterministic_seed is not None:
            np.random.seed(deterministic_seed)
            
        # Initialize backend
        self.backend = self._create_backend(provider, model_name, **kwargs)
        
    def _create_backend(self, provider: str, model_name: Optional[str], **kwargs) -> EmbeddingBackend:
        """Create the appropriate backend instance."""
        if provider == "local":
            from .backends.local_sentence_transformers import LocalSentenceTransformersBackend
            return LocalSentenceTransformersBackend(
                model_name=model_name or "all-MiniLM-L6-v2",
                deterministic_seed=self.deterministic_seed,
                **kwargs
            )
        elif provider == "openai":
            from .backends.openai import OpenAIBackend
            return OpenAIBackend(
                model_name=model_name or "text-embedding-ada-002",
                **kwargs
            )
        elif provider == "hashing":
            # Deterministic, dependency-light backend: offline default for tests
            # and the fallback where sentence-transformers is unavailable.
            from .backends.hashing import HashingBackend
            return HashingBackend(**kwargs)
        else:
            raise ValueError(f"Unknown provider: {provider}")
    
    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """
        Generate embeddings for a list of texts.
        
        Args:
            texts: List of text strings to embed
            
        Returns:
            numpy array of shape [N, dim] where N is len(texts)
        """
        if not texts:
            return np.empty((0, self.dim()))
            
        # Process in batches with retry logic
        all_embeddings = []
        
        for i in range(0, len(texts), self.batch_size):
            batch_texts = texts[i:i + self.batch_size]
            
            for attempt in range(self.max_retries):
                try:
                    batch_embeddings = self.backend.embed_texts(batch_texts)
                    all_embeddings.append(batch_embeddings)
                    break
                except Exception as e:
                    if attempt == self.max_retries - 1:
                        logger.error(f"Failed to embed batch after {self.max_retries} attempts: {e}")
                        raise
                    else:
                        logger.warning(f"Batch embedding attempt {attempt + 1} failed: {e}, retrying...")
        
        return np.vstack(all_embeddings) if all_embeddings else np.empty((0, self.dim()))
    
    def embed_queries(self, texts: List[str]) -> np.ndarray:
        """Use the backend query policy; documents retain embed_texts semantics."""
        encode = getattr(self.backend, "embed_queries", self.backend.embed_texts)
        if not texts:
            return np.empty((0, self.dim()))
        return np.vstack([encode(texts[i:i+self.batch_size]) for i in range(0, len(texts), self.batch_size)])

    def dim(self) -> int:
        """Return the embedding dimension."""
        return self.backend.dim()
    
    def name(self) -> str:
        """Return the provider name."""
        return f"{self.provider_name}:{self.backend.name()}"

    def count_tokens(self, text: str) -> int:
        """Count the actual backend tokenizer input, including special tokens."""
        counter = getattr(self.backend, "count_tokens", None)
        if counter is None:
            raise NotImplementedError("embedding backend must declare token counting for full-document indexing")
        return counter(text)

    def token_limit(self) -> int:
        method = getattr(self.backend, "token_limit", None)
        if method is None:
            raise NotImplementedError("embedding backend must declare its input token limit")
        return method()

    def tokenizer_identity(self) -> dict:
        method = getattr(self.backend, "tokenizer_identity", None)
        if method is None:
            raise NotImplementedError("embedding backend must declare tokenizer identity")
        return method()


def get_embedding_provider(
    provider: Optional[str] = None,
    model_name: Optional[str] = None,
    **kwargs
) -> EmbeddingProvider:
    """
    Factory function to create an embedding provider.
    
    Args:
        provider: Provider name (from environment or "local")
        model_name: Model name (from environment or provider default)
        **kwargs: Additional arguments for the provider
        
    Returns:
        Configured EmbeddingProvider instance
    """
    # Get provider from environment or use default
    if provider is None:
        provider = os.getenv("EMBEDDING_PROVIDER", "local")
    
    # Get model name from environment if not specified
    if model_name is None:
        model_name = os.getenv("EMBEDDING_MODEL_NAME")
    
    return EmbeddingProvider(
        provider=provider,
        model_name=model_name,
        **kwargs
    )
