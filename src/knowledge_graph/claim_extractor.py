"""
Claim extraction.

Extracts atomic subject-predicate-object claims from document text, each tagged
with provenance (source document + chunk path) so the knowledge graph becomes a
database of *cited* claims rather than anonymous text. The extractor is
heuristic and dependency-free (sentence splitting + a verb-anchored SPO parse
with negation detection); it is the integration point where an LLM-based
extractor can be swapped in later.

See ``docs/architecture/knowledge-engine-pivot.md`` (claim extraction and
knowledge layer).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Verbs that typically anchor an assertional claim (subject VERB object).
_CLAIM_VERBS = {
    "is", "are", "was", "were", "improves", "improve", "improved",
    "increases", "increase", "increased", "reduces", "reduce", "reduced",
    "outperforms", "outperform", "outperformed", "causes", "cause", "caused",
    "shows", "show", "showed", "demonstrates", "demonstrate", "enables",
    "enable", "achieves", "achieve", "achieved", "beats", "beat", "exceeds",
    "exceed", "requires", "require", "leads", "lead", "predicts", "predict",
    "produces", "produce", "yields", "yield", "affects", "affect", "supports",
    "support", "correlates", "correlate", "depends", "depend",
}

# Auxiliary verbs and negation tokens consumed around the main verb.
_AUX = {
    "do", "does", "did", "be", "been", "being", "can", "could", "will",
    "would", "shall", "should", "may", "might", "must", "has", "have", "had",
}
_NEG = {"not", "n't", "no", "never", "cannot", "can't", "without", "fails", "fail"}

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD = re.compile(r"[A-Za-z0-9']+|[^\sA-Za-z0-9]")


@dataclass
class ExtractedClaim:
    """An atomic claim with its provenance."""

    text: str
    subject: str
    predicate: str
    object: str
    negated: bool = False
    source_doc: str = ""
    chunk_path: List[str] = field(default_factory=list)
    confidence: float = 0.6

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "subject": self.subject,
            "predicate": self.predicate,
            "object": self.object,
            "negated": self.negated,
            "source_doc": self.source_doc,
            "chunk_path": list(self.chunk_path),
            "confidence": self.confidence,
        }


class ClaimExtractor:
    """Heuristic subject-predicate-object claim extractor."""

    def __init__(self, min_subject_words: int = 1, min_object_words: int = 1):
        self.min_subject_words = min_subject_words
        self.min_object_words = min_object_words

    def extract(
        self,
        text: str,
        source_doc: str,
        chunk_path: Optional[Sequence[str]] = None,
        confidence: float = 0.6,
    ) -> List[ExtractedClaim]:
        """Extract claims from a block of text."""
        if not source_doc:
            raise ValueError("source_doc is required so each claim is cited")
        claims: List[ExtractedClaim] = []
        for sentence in self._split_sentences(text):
            spo = self._extract_spo(sentence)
            if spo is None:
                continue
            subject, predicate, obj, negated = spo
            claims.append(
                ExtractedClaim(
                    text=sentence.strip(),
                    subject=subject,
                    predicate=predicate,
                    object=obj,
                    negated=negated,
                    source_doc=source_doc,
                    chunk_path=list(chunk_path or []),
                    confidence=confidence,
                )
            )
        return claims

    def extract_from_chunks(
        self,
        chunks: Sequence[Any],
        source_doc: str,
        confidence: float = 0.6,
    ) -> List[ExtractedClaim]:
        """Extract claims from chunk dicts/objects carrying ``text`` and ``path``."""
        claims: List[ExtractedClaim] = []
        for chunk in chunks:
            if isinstance(chunk, dict):
                chunk_text = chunk.get("text", "")
                path = chunk.get("path", [])
            else:
                chunk_text = getattr(chunk, "text", "")
                path = getattr(chunk, "path", [])
            claims.extend(self.extract(chunk_text, source_doc, path, confidence))
        return claims

    # ---- internals ------------------------------------------------------ #

    @staticmethod
    def _split_sentences(text: str) -> List[str]:
        return [s for s in _SENTENCE_SPLIT.split((text or "").strip()) if s.strip()]

    def _extract_spo(self, sentence: str) -> Optional[Tuple[str, str, str, bool]]:
        clean = sentence.strip()
        if clean.endswith("?"):
            return None  # questions are not claims
        tokens = _WORD.findall(clean)
        words = [t for t in tokens if re.match(r"[A-Za-z0-9']", t)]
        if len(words) < 3:
            return None

        lower = [w.lower() for w in words]
        verb_index = next((i for i, w in enumerate(lower) if w in _CLAIM_VERBS), None)
        if verb_index is None or verb_index == 0 or verb_index >= len(words) - 1:
            return None

        # Walk left over auxiliaries/negation to find the true subject end.
        negated = False
        j = verb_index - 1
        while j >= 0 and (lower[j] in _AUX or lower[j] in _NEG):
            if lower[j] in _NEG:
                negated = True
            j -= 1
        if j < 0:
            return None  # no real subject before the verb cluster

        subject = " ".join(words[: j + 1]).strip()
        predicate = words[verb_index].lower()
        obj = " ".join(words[verb_index + 1:]).strip()

        if lower[verb_index] in ("fails", "fail"):
            negated = True

        if len(subject.split()) < self.min_subject_words:
            return None
        if len(obj.split()) < self.min_object_words:
            return None
        return subject, predicate, obj, negated
