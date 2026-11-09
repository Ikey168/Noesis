"""Locate a claim's exact character span in its source text.

Sentence splitters and claim detectors normalise text (collapse whitespace,
straighten quotes), so the stored claim text is not always a byte-identical
substring of its document. :func:`locate_span` finds the source span anyway
and says how:

``exact``
    the claim text occurs exactly once;
``first_of_n``
    it occurs ``n`` > 1 times; the first occurrence is used;
``whitespace_insensitive``
    it matches once whitespace runs are treated as equal;
``normalized``
    it matches once whitespace, quotes, dashes and case are folded.

The returned span always indexes the *source* text, so whatever is reviewed
or cited is the source's own characters.
"""

from __future__ import annotations

import re
from typing import Any

_QUOTES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "–": "-", "—": "-", "−": "-", " ": " ", " ": " ",
}


def _fold(ch: str) -> str:
    return _QUOTES.get(ch, ch).casefold()


def _pattern(text: str, fold: bool) -> re.Pattern | None:
    tokens = text.split()
    if not tokens:
        return None
    if not fold:
        return re.compile(r"\s+".join(re.escape(token) for token in tokens))
    parts = []
    for token in tokens:
        chars = []
        for ch in token:
            variants = {ch, _QUOTES.get(ch, ch)} | {k for k, v in _QUOTES.items() if v == _QUOTES.get(ch, ch)}
            chars.append("(?:" + "|".join(re.escape(v) for v in sorted(variants)) + ")")
        parts.append("".join(chars))
    return re.compile(r"[\s  ]+".join(parts), re.IGNORECASE)


def locate_span(source: str, text: str) -> dict[str, Any] | None:
    """``{"start", "end", "match", "occurrences"}`` in ``source``, or None."""
    if not isinstance(source, str) or not isinstance(text, str) or not text.strip():
        return None
    count = source.count(text)
    if count >= 1:
        start = source.index(text)
        return {"start": start, "end": start + len(text),
                "match": "exact" if count == 1 else "first_of_n", "occurrences": count}
    stripped = text.strip()
    for fold, label in ((False, "whitespace_insensitive"), (True, "normalized")):
        pattern = _pattern(stripped, fold)
        if pattern is None:
            return None
        matches = list(pattern.finditer(source))
        if matches:
            first = matches[0]
            return {"start": first.start(), "end": first.end(),
                    "match": label if len(matches) == 1 else f"{label}_first_of_n",
                    "occurrences": len(matches)}
    return None
