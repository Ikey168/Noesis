"""
Quantitative claim extraction (Track A / A3).

To check a claim against statistical data, the claim must first be parsed into a
comparable assertion: *what* is measured, *which direction* it moved (or what it
equals), an optional magnitude and unit, a period, and a geography. This module
is the heuristic extractor — pattern + lexicon, stdlib only — mirroring the
``ClaimDetector`` design (heuristic first, an optional fine-tuned model later) so
it runs with no key and no weights.

A :class:`QuantAssertion` is deliberately shallow and honest: the subject is a
best-effort span, and ``confidence`` reflects how much of the pattern matched.
Downstream (A4, #772) resolves an assertion to candidate series and runs the
honesty-gated check; a low-confidence or unresolved assertion is ``unverifiable``
there, never a guess.

See ``docs/architecture/EVIDENCE_DATASETS_PLAN.md`` §3.4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --- Direction lexicon -----------------------------------------------------
# Ordered by specificity; the first cue found in a sentence wins.
DIRECTIONS: Dict[str, List[str]] = {
    "rose": [
        "rose", "rise", "rises", "increased", "increase", "increases", "grew",
        "grow", "grows", "climbed", "climb", "climbs", "surged", "surge",
        "jumped", "jump", "soared", "gained", "gain", "risen", "rising",
        "up by", "went up", "higher", "doubled", "tripled", "quadrupled",
    ],
    "fell": [
        "fell", "fall", "falls", "decreased", "decrease", "decreases",
        "declined", "decline", "declines", "dropped", "drop", "drops",
        "plunged", "plummeted", "shrank", "shrunk", "sank", "fallen",
        "falling", "down by", "went down", "lower", "halved",
    ],
    "exceeds": [
        "exceeds", "exceed", "exceeded", "surpass", "surpasses", "surpassed",
        "more than", "greater than", "above", "over", "outpace", "outpaced",
        "tops", "topped",
    ],
    "below": [
        "below", "less than", "fewer than", "under", "beneath", "trails",
    ],
    "unchanged": [
        "unchanged", "flat", "steady", "stable", "held at", "remained at",
        "stayed at",
    ],
    "equals": [
        "reached", "stands at", "stood at", "totaled", "totalled", "amounted to",
        "hit", "was", "is", "equals", "equal to", "at a rate of",
    ],
}

# Magnitude multipliers for "doubled"/"tripled"/"halved" style claims.
_RELATIVE = {"doubled": 2.0, "tripled": 3.0, "quadrupled": 4.0, "halved": 0.5}

# Scale words.
_SCALES = {
    "hundred": 1e2, "thousand": 1e3, "k": 1e3,
    "million": 1e6, "m": 1e6, "billion": 1e9, "bn": 1e9,
    "trillion": 1e12, "tn": 1e12,
}

# Country / demonym -> ISO 3166 alpha-2 (or a coarse region code). Modest by
# design; extended as coverage needs grow.
_GEO_NAMES = {
    "germany": "DE", "german": "DE",
    "france": "FR", "french": "FR",
    "italy": "IT", "italian": "IT",
    "spain": "ES", "spanish": "ES",
    "united kingdom": "GB", "uk": "GB", "u.k.": "GB", "britain": "GB", "british": "GB", "england": "GB",
    "united states": "US", "u.s.": "US", "us": "US", "usa": "US", "america": "US", "american": "US",
    "canada": "CA", "canadian": "CA",
    "china": "CN", "chinese": "CN",
    "india": "IN", "indian": "IN",
    "japan": "JP", "japanese": "JP",
    "russia": "RU", "russian": "RU",
    "brazil": "BR", "brazilian": "BR",
    "australia": "AU", "australian": "AU",
    "european union": "EU", "eu": "EU", "europe": "EU", "eurozone": "EU",
}

# Unit cues -> normalized unit.
_UNIT_CUES = [
    (re.compile(r"%|percent|per cent|percentage points?|basis points?", re.I), "percent"),
    (re.compile(r"\$|usd|dollars?|us dollars?", re.I), "usd"),
    (re.compile(r"€|eur|euros?", re.I), "eur"),
    (re.compile(r"£|gbp|pounds?", re.I), "gbp"),
]

_YEAR = r"(?:19|20)\d{2}"
_PERIOD_PATTERNS = [
    re.compile(rf"between\s+({_YEAR})\s+and\s+({_YEAR})", re.I),
    re.compile(rf"from\s+({_YEAR})\s+to\s+({_YEAR})", re.I),
    re.compile(rf"since\s+({_YEAR})", re.I),
    re.compile(rf"\bin\s+({_YEAR})", re.I),
    re.compile(rf"\b(Q[1-4])\s*({_YEAR})", re.I),
    re.compile(rf"\b({_YEAR})\b"),
]

_NUMBER = re.compile(r"(-?\d[\d,]*\.?\d*)\s*(hundred|thousand|million|billion|trillion|bn|tn)?", re.I)

_STOPWORD_PREFIX = re.compile(r"^(the|a|an|its|their|our|this|that|these|those|in|on|by|for|of)\s+", re.I)


@dataclass
class QuantAssertion:
    """A quantitative assertion parsed from a claim sentence."""

    text: str
    subject: str
    direction: str  # rose | fell | exceeds | below | equals | unchanged
    value: Optional[float] = None
    unit: Optional[str] = None
    period: Optional[str] = None
    geography: Optional[str] = None
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "subject": self.subject,
            "direction": self.direction,
            "value": self.value,
            "unit": self.unit,
            "period": self.period,
            "geography": self.geography,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }


def _find_direction(lowered: str):
    """Return (canonical_direction, cue, index) for the earliest cue, or None."""
    best = None
    for canonical, cues in DIRECTIONS.items():
        for cue in cues:
            idx = _word_index(lowered, cue)
            if idx is not None and (best is None or idx < best[2]):
                best = (canonical, cue, idx)
    return best


def _word_index(haystack: str, needle: str) -> Optional[int]:
    """Index of ``needle`` as a whole word/phrase in ``haystack``, else None."""
    pattern = r"\b" + re.escape(needle) + r"\b"
    m = re.search(pattern, haystack)
    return m.start() if m else None


def _extract_value_unit(text: str):
    """Return (value, unit) from the first numeric mention, or (None, unit?)."""
    unit = None
    for rx, u in _UNIT_CUES:
        if rx.search(text):
            unit = u
            break
    m = _NUMBER.search(text)
    if not m:
        return (None, unit)
    raw = m.group(1).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return (None, unit)
    scale = m.group(2)
    if scale:
        value *= _SCALES.get(scale.lower(), 1.0)
    return (value, unit)


def _extract_period(text: str) -> Optional[str]:
    for rx in _PERIOD_PATTERNS:
        m = rx.search(text)
        if m:
            groups = [g for g in m.groups() if g]
            if len(groups) == 2 and groups[0].upper().startswith("Q"):
                return f"{groups[1]}-{groups[0].upper()}"  # 2024-Q1
            if len(groups) == 2:
                return f"{groups[0]}..{groups[1]}"  # a range
            return groups[0]
    return None


def _blank_periods(text: str) -> str:
    """Blank out period phrases so a year used as a date is not mistaken for a
    measured value (e.g. 'crime doubled since 2020' -> no value 2020)."""
    out = text
    for rx in _PERIOD_PATTERNS:
        out = rx.sub(" ", out)
    return out


_TRAILING_AUX = re.compile(r"\s+(has|have|had|is|was|were|are|will|would|now|then|also)$", re.I)


def _extract_geography(lowered: str) -> Optional[str]:
    # Longest name first so "united states" beats "us".
    for name in sorted(_GEO_NAMES, key=len, reverse=True):
        if _word_index(lowered, name) is not None:
            return _GEO_NAMES[name]
    return None


def _extract_subject(text: str, direction_idx: int) -> str:
    """Best-effort subject: the span before the direction cue, trimmed."""
    head = text[:direction_idx].strip(" ,;:")
    # Drop a leading period/geography clause if the cue span is long.
    head = re.split(r"\bsince\b|\bbetween\b|\bfrom\b", head, flags=re.I)[0].strip()
    prev = None
    while prev != head:
        prev = head
        head = _STOPWORD_PREFIX.sub("", head).strip()
    # Strip a trailing auxiliary/adverb left dangling before the cue.
    prev = None
    while prev != head:
        prev = head
        head = _TRAILING_AUX.sub("", head).strip()
    # Keep the last few words (the head noun phrase), avoid trailing clutter.
    words = head.split()
    return " ".join(words[-6:]) if words else head


class QuantityExtractor:
    """Heuristic extractor of :class:`QuantAssertion`s from claim text."""

    def extract(self, text: str) -> List[QuantAssertion]:
        """Return zero or one assertion for a sentence (the dominant one).

        Zero when no direction cue is present — a sentence with no quantitative
        movement/comparison is not a quantitative claim.
        """
        if not text or not text.strip():
            return []
        lowered = text.lower()
        found = _find_direction(lowered)
        if found is None:
            return []
        direction, cue, idx = found

        value, unit = _extract_value_unit(_blank_periods(text))
        # Relative multipliers ("doubled") imply a rose/fell with a factor.
        relative = _RELATIVE.get(cue)
        metadata: Dict[str, Any] = {"cue": cue}
        if relative is not None:
            metadata["relative_factor"] = relative

        period = _extract_period(text)
        geography = _extract_geography(lowered)
        subject = _extract_subject(text, idx)

        # Confidence: partial credit for each recovered slot, capped.
        score = 0.4
        if value is not None:
            score += 0.2
        if unit is not None:
            score += 0.1
        if period is not None:
            score += 0.15
        if geography is not None:
            score += 0.1
        if subject:
            score += 0.05
        confidence = round(min(score, 0.95), 3)

        return [
            QuantAssertion(
                text=text.strip(),
                subject=subject,
                direction=direction,
                value=value,
                unit=unit,
                period=period,
                geography=geography,
                confidence=confidence,
                metadata=metadata,
            )
        ]

    def extract_sentences(self, sentences: List[str]) -> List[QuantAssertion]:
        out: List[QuantAssertion] = []
        for s in sentences:
            out.extend(self.extract(s))
        return out

    def extract_document(self, document) -> List[QuantAssertion]:
        """Extract over every sentence of a document (any of the six source
        types). Imported lazily so this module stays dependency-light."""
        from src.argument_mining.dataset import sentences_from_document

        return self.extract_sentences(sentences_from_document(document))
