"""
RAG (Retrieval Augmented Generation) services package.

Issue #229: Chunking & normalization pipeline
Issue #231: Lexical search (Postgres FTS) for hybrid retrieval
Issue #232: Hybrid retrieval combining vector + lexical search
Provides text normalization, chunking, lexical search, vector search, and hybrid retrieval services for articles.
"""

from importlib import import_module

# Optional database/model backends are loaded only when requested. Pure text
# chunking must remain usable in an installation without a Postgres driver.
_EXPORTS = {
    **dict.fromkeys(['ArticleNormalizer', 'normalize_article'], 'normalization'),
    **dict.fromkeys(['TextChunker', 'ChunkConfig', 'SplitStrategy', 'TextChunk', 'chunk_text'], 'chunking'),
    **dict.fromkeys(['LexicalSearchService', 'LexicalSearchResult', 'SearchFilters',
                    'get_lexical_search_service', 'lexical_search', 'simple_lexical_search'], 'lexical'),
    **dict.fromkeys(['VectorSearchService', 'VectorSearchResult', 'VectorSearchFilters'], 'vector'),
    'CrossEncoderReranker': 'rerank',
    'HybridRetriever': 'retriever',
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f'{__name__}.{_EXPORTS[name]}'), name)
    globals()[name] = value
    return value

__all__ = [
    'ArticleNormalizer', 'normalize_article',
    'TextChunker', 'ChunkConfig', 'SplitStrategy', 'TextChunk', 'chunk_text',
    'LexicalSearchService', 'LexicalSearchResult', 'SearchFilters',
    'get_lexical_search_service', 'lexical_search', 'simple_lexical_search',
    'VectorSearchService', 'VectorSearchResult', 'VectorSearchFilters',
    'CrossEncoderReranker',
    'HybridRetriever'
]
