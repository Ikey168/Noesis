"""
Dependency-light text helpers for the analytics plane (R6).

Bag-of-words term-frequency vectors with stopword removal — the lexical
fallback the plan mandates when document embeddings are not available. Pure
stdlib so the tool servers stay import-safe.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Sequence

_TOKEN = re.compile(r"[a-z][a-z0-9'-]+")

STOPWORDS = frozenset(
    "the a an and or but of to in on at for with from by as is are was were be "
    "been being this that these those it its it's he she they them his her their "
    "we you i not no yes do does did has have had will would can could should "
    "may might must about over under after before into out up down more most "
    "than then there here what which who whom whose how why when where new say "
    "says said also just like get got one two new amid via per".split()
)


def tokenize(text: str) -> List[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in STOPWORDS and len(t) > 2]


def tf_vector(text: str) -> Dict[str, float]:
    """L2-normalized term-frequency vector for a document."""
    counts = Counter(tokenize(text))
    if not counts:
        return {}
    norm = sum(v * v for v in counts.values()) ** 0.5
    return {term: c / norm for term, c in counts.items()}


def context_counts(texts: Sequence[str], term: str) -> Counter:
    """Word counts across documents that mention ``term`` (the term itself
    excluded) — the lexical 'meaning' profile of the term."""
    term = term.lower()
    ctx: Counter = Counter()
    for text in texts:
        tokens = tokenize(text)
        if term in tokens:
            ctx.update(t for t in tokens if t != term)
    return ctx


def top_terms(vector: Dict[str, float], n: int = 6) -> List[str]:
    return [t for t, _ in sorted(vector.items(), key=lambda kv: -kv[1])[:n]]
