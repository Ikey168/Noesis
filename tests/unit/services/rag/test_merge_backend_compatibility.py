"""Compatibility between the backlog and existing main-branch backend hooks."""

from types import SimpleNamespace

import numpy as np
import pytest

from services.embeddings.provider import EmbeddingProvider
from services.rag.chunking import ChunkConfig, TextChunker
from services.rag.normalization import ArticleNormalizer


def test_query_embedding_preserves_batch_limits_and_query_policy():
    calls = []

    def encode(texts):
        calls.append(list(texts))
        return np.ones((len(texts), 2))

    provider = object.__new__(EmbeddingProvider)
    provider.batch_size = 2
    provider.backend = SimpleNamespace(
        embed_queries=encode,
        embed_texts=lambda _: pytest.fail("document encoding used for queries"),
        dim=lambda: 2,
    )
    assert provider.embed_queries(["a", "b", "c"]).shape == (3, 2)
    assert calls == [["a", "b"], ["c"]]
    assert provider.embed_queries([]).shape == (0, 2)
    assert len(calls) == 2


@pytest.mark.parametrize("object_api", [True, False])
def test_both_segmentation_hooks_reject_omitted_source_text(object_api):
    hook = (
        {"segmenter": SimpleNamespace(segment=lambda _: [{"start": 0, "end": 5}])}
        if object_api
        else {"sentence_segmenter": lambda _: [(0, 5)]}
    )
    chunker = TextChunker(ChunkConfig(min_chunk_chars=0), **hook)
    with pytest.raises(ValueError, match="omitted source text"):
        chunker._sentence_spans("First. Second.")


def test_language_candidates_remains_a_supported_configuration_alias():
    normalizer = ArticleNormalizer(language_candidates=("fr", "de"))
    assert normalizer.lingua_languages == ("fr", "de")
    assert normalizer.language_candidates == normalizer.lingua_languages
