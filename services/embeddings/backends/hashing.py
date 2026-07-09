"""
Deterministic hashing embedding backend (offline, dependency-light).

Embeds text via the hashing trick: tokens are hashed into a fixed-dimensional,
signed bag-of-words vector and L2-normalised. No model download, no network, and
fully reproducible — this is the default backend for tests and for environments
without sentence-transformers, and the fallback when the heavy stack is absent.

It captures *lexical* similarity (documents that share vocabulary get similar
vectors), which is enough for offline semantic-search smoke tests and a usable,
model-free baseline. Swap in the ``local`` (sentence-transformers) or ``openai``
backend for semantic (meaning-level) similarity in production.
"""

import re
import zlib
from typing import List

import numpy as np

from ..provider import EmbeddingBackend

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class HashingBackend(EmbeddingBackend):
    """Deterministic feature-hashing embeddings; no heavy dependencies."""

    def __init__(self, dim: int = 256, model_name: str = "hashing"):
        self._dim = int(dim)
        self._model_name = model_name

    def _embed_one(self, text: str) -> np.ndarray:
        vec = np.zeros(self._dim, dtype=np.float64)
        for tok in _TOKEN_RE.findall((text or "").lower()):
            # crc32 is a stable, non-cryptographic hash (deterministic across
            # runs, unlike the builtin hash()); the low bit gives a sign so
            # colliding tokens can cancel rather than always reinforce.
            h = zlib.crc32(tok.encode("utf-8"))
            vec[h % self._dim] += 1.0 if (h >> 8) & 1 == 0 else -1.0
        norm = np.linalg.norm(vec)
        if norm > 0.0:
            vec /= norm
        return vec

    def embed_texts(self, texts: List[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self._dim))
        return np.vstack([self._embed_one(t) for t in texts])

    def dim(self) -> int:
        return self._dim

    def name(self) -> str:
        return self._model_name
