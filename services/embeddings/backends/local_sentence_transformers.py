"""
Local Sentence Transformers Backend for Embeddings
Issue #228: Embedding provider (local + cloud pluggable)

This backend uses sentence-transformers library for local embedding generation.
"""

import logging
from typing import List, Optional
import numpy as np

try:
    from sentence_transformers import SentenceTransformer
except ImportError:
    raise ImportError(
        "sentence-transformers is required for local backend. "
        "Install it with: pip install sentence-transformers"
    )

from ..provider import EmbeddingBackend

logger = logging.getLogger(__name__)


class LocalSentenceTransformersBackend(EmbeddingBackend):
    """
    Local sentence transformers embedding backend.
    
    Uses sentence-transformers library to generate embeddings locally
    without requiring API calls or internet connectivity.
    """
    
    def __init__(
        self,
        model_name: str = "all-MiniLM-L6-v2",
        cache_dir: Optional[str] = None,
        device: Optional[str] = None,
        deterministic_seed: Optional[int] = None,
        **kwargs
    ):
        """
        Initialize local sentence transformers backend.
        
        Args:
            model_name: Name of the sentence transformer model
            cache_dir: Directory to cache downloaded models
            device: Device to run model on ('cuda', 'cpu', or None for auto)
            deterministic_seed: Seed for reproducible results
            **kwargs: Additional arguments passed to SentenceTransformer
        """
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.device = device
        self.deterministic_seed = deterministic_seed
        
        # Initialize model
        logger.info(f"Loading sentence transformer model: {model_name}")
        
        model_kwargs = {}
        if cache_dir:
            model_kwargs['cache_folder'] = cache_dir
        if device:
            model_kwargs['device'] = device
        model_kwargs.update(kwargs)
        
        self.model = SentenceTransformer(model_name, **model_kwargs)
        
        # Set deterministic behavior if seed provided
        if deterministic_seed is not None:
            import torch
            import random
            torch.manual_seed(deterministic_seed)
            torch.cuda.manual_seed_all(deterministic_seed)
            np.random.seed(deterministic_seed)
            random.seed(deterministic_seed)
            
            # Set deterministic algorithms
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
        
        dimension = getattr(self.model, "get_embedding_dimension", None) or self.model.get_sentence_embedding_dimension
        self._embedding_dimension = dimension()
        
        logger.info(
            f"Loaded model '{model_name}' with {self._embedding_dimension} dimensions "
            f"on device: {self.model.device}"
        )
    
    def embed_texts(self, texts: List[str]) -> np.ndarray:
        """
        Generate embeddings for a list of texts.
        
        Args:
            texts: List of text strings to embed
            
        Returns:
            numpy array of shape [N, dim] where N is len(texts)
        """
        if not texts:
            return np.empty((0, self._embedding_dimension))
        
        # Generate embeddings
        embeddings = self.model.encode(
            texts,
            convert_to_numpy=True,
            show_progress_bar=False,
            normalize_embeddings=True,  # Normalize for consistent similarity calculations
        )
        
        # Ensure correct shape
        if embeddings.ndim == 1:
            embeddings = embeddings.reshape(1, -1)
            
        return embeddings
    
    def dim(self) -> int:
        """Return the embedding dimension."""
        return self._embedding_dimension

    def count_tokens(self, text: str) -> int:
        return len(self.model.tokenizer.encode(text, add_special_tokens=True, truncation=False))

    def token_limit(self) -> int:
        return int(self.model.max_seq_length)

    def tokenizer_identity(self) -> dict:
        config = self.model[0].auto_model.config
        return {"name": self.model.tokenizer.name_or_path,
                "revision": getattr(config, "_commit_hash", None),
                "class": type(self.model.tokenizer).__name__, "max_tokens": self.token_limit(),
                "special_tokens_included": True}
    
    def name(self) -> str:
        """Return the backend name."""
        return f"sentence-transformers:{self.model_name}"
    
    def get_model_info(self) -> dict:
        """Get detailed model information."""
        return {
            "model_name": self.model_name,
            "embedding_dimension": self._embedding_dimension,
            "device": str(self.model.device),
            "max_seq_length": getattr(self.model, "max_seq_length", "unknown"),
            "cache_dir": self.cache_dir,
            "deterministic_seed": self.deterministic_seed,
        }
