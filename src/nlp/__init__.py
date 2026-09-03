"""Natural-language processing package with lazy public exports.

Importing a lightweight submodule such as ``source_comparator`` must not pull
in the Redshift-backed article processor and its optional ``psycopg2``
dependency. Public compatibility names are therefore resolved on demand.
"""
from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

__all__ = [
    "create_analyzer", "SentimentAnalyzer", "ArticleProcessor",
    "NERProcessor", "create_ner_processor", "NERArticleProcessor",
    "create_ner_article_processor",
]

_EXPORTS = {
    "ArticleProcessor": (".article_processor", "ArticleProcessor"),
    "NERArticleProcessor": (".ner_article_processor", "NERArticleProcessor"),
    "create_ner_article_processor": (
        ".ner_article_processor", "create_ner_article_processor",
    ),
    "NERProcessor": (".ner_processor", "NERProcessor"),
    "create_ner_processor": (".ner_processor", "create_ner_processor"),
    "SentimentAnalyzer": (".sentiment_analysis", "SentimentAnalyzer"),
    "create_analyzer": (".sentiment_analysis", "create_analyzer"),
}


def __getattr__(name: str):
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


if TYPE_CHECKING:  # pragma: no cover - imports are for static analyzers only
    from .article_processor import ArticleProcessor
    from .ner_article_processor import (
        NERArticleProcessor, create_ner_article_processor,
    )
    from .ner_processor import NERProcessor, create_ner_processor
    from .sentiment_analysis import SentimentAnalyzer, create_analyzer
