"""
Unsourced-assertion detector — content-type-aware attribution classifier.

For each claim text, determines whether a source or basis is attributed within
the sentence and, if so, extracts a short attribution snippet.

Rules are heuristic and content-type-specific:

  news        — "according to X", "X said/stated/confirmed/reported/told …"
                "per X", "citing X", "X announced", "officials/researchers say"
  paper       — APA/MLA parenthetical: "(Smith, 2023)", "(Smith et al.)"
                numeric inline: "[1]", "(1)", "[12,13]"
  transcript  — speaker labels ("Jane Doe: …"), quote attribution
                ("Jane Doe said …")
  blog/note   — any of the news patterns OR first-person hedges
                ("I found", "in my experience", "we observed") — these count
                as attributed because they carry an explicit epistemic anchor

Public API
----------
  classify_attribution(claim_text, source_type)
      -> (attributed: bool, attribution_text: str | None)

  run_attribution_batch(conn, lock, limit)
      -> {"updated": int, "skipped": int}
      Sweeps argument_claims with attributed IS NULL and updates them.
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# News: "according to <noun phrase>", "X said/reported/…", "per <noun phrase>"
_NEWS_ACCORDING = re.compile(
    r"\baccording\s+to\s+([\w\s,\.'-]{2,50}?)(?:\s*[,;]|\s+(?:the|a|an|its|their)\b)",
    re.I,
)
_NEWS_SAID = re.compile(
    r"([\w\s\-]{2,40}?)\s+(?:said|stated|confirmed|reported|told|announced|noted|"
    r"warned|argued|claimed|wrote|added|explained|revealed|disclosed|indicated|"
    r"stressed|emphasized|insisted|acknowledged|admitted|conceded)\b",
    re.I,
)
_NEWS_PER = re.compile(r"\bper\s+([\w\s,'-]{2,40}?)(?:\s*[,;]|$)", re.I)
_NEWS_CITING = re.compile(r"\bciting\s+([\w\s,'-]{2,40}?)(?:\s*[,;]|$)", re.I)
_NEWS_OFFICIALS = re.compile(
    r"\b(officials?|researchers?|scientists?|analysts?|experts?|"
    r"authorities?|investigators?|sources?)\s+(?:said|say|confirmed|noted|reported)\b",
    re.I,
)

# Paper: APA parenthetical, numeric inline
_PAPER_APA = re.compile(
    r"\((?:[A-Z][a-z]+(?:\s+et\s+al\.?)?(?:,\s*\d{4})?(?:;\s*)?){1,4}\)",
)
_PAPER_NUMERIC = re.compile(r"\[[\d,\s]+\]|\(\d+(?:,\s*\d+)*\)")

# Transcript: "Speaker Name: " at start, or "Speaker said/stated that"
_TRANSCRIPT_LABEL = re.compile(
    r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s*:",
)
_TRANSCRIPT_ATTRIBUTION = re.compile(
    r"([\w\s\-]{2,35}?)\s+(?:said|stated|explained|noted|argued|confirmed)\s+that\b",
    re.I,
)

# Blog/note: first-person or "we" anchors (count as attributed — epistemic source explicit)
_FIRST_PERSON = re.compile(
    r"\b(?:I\s+(?:found|noticed|observed|believe|think|argue|wrote|showed|"
    r"measured|tested|confirmed)|we\s+(?:found|observed|measured|tested|showed|"
    r"confirmed|reported)|in\s+my\s+(?:experience|view|opinion|analysis|testing))\b",
    re.I,
)

# Opinion-as-fact markers that DON'T carry attribution (unsourced assertions)
_OPINION_AS_FACT = re.compile(
    r"\b(?:clearly|obviously|everyone knows|it is (?:clear|obvious|evident|"
    r"well.known)|undeniably|undoubtedly|of course|needless to say|"
    r"it goes without saying|it is(?:'s)? (?:simply|just) (?:true|a fact))\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Per-type classifiers
# ---------------------------------------------------------------------------

def _news(text: str) -> Tuple[bool, Optional[str]]:
    for pat in (_NEWS_ACCORDING, _NEWS_PER, _NEWS_CITING):
        m = pat.search(text)
        if m:
            return True, m.group(1).strip()
    m = _NEWS_OFFICIALS.search(text)
    if m:
        return True, m.group(1).strip()
    m = _NEWS_SAID.search(text)
    if m:
        snippet = m.group(1).strip()
        # Reject matches that are common sentence openers without real attribution
        if len(snippet.split()) >= 1 and not snippet.lower().startswith(
            ("the ", "a ", "an ", "this ", "that ", "it ", "they ")
        ):
            return True, snippet
    return False, None


def _paper(text: str) -> Tuple[bool, Optional[str]]:
    m = _PAPER_APA.search(text)
    if m:
        return True, m.group(0)
    m = _PAPER_NUMERIC.search(text)
    if m:
        return True, m.group(0)
    return False, None


def _transcript(text: str) -> Tuple[bool, Optional[str]]:
    m = _TRANSCRIPT_LABEL.search(text)
    if m:
        return True, m.group(1).strip()
    m = _TRANSCRIPT_ATTRIBUTION.search(text)
    if m:
        return True, m.group(1).strip()
    # Fall through to news patterns — transcripts contain quotes
    return _news(text)


def _blog_note(text: str) -> Tuple[bool, Optional[str]]:
    # First-person / we — explicit epistemic anchor
    m = _FIRST_PERSON.search(text)
    if m:
        return True, m.group(0).strip()
    # Unsourced opinion-as-fact
    if _OPINION_AS_FACT.search(text):
        return False, None
    # Fall through to news patterns
    return _news(text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_CLASSIFIERS = {
    "news":       _news,
    "paper":      _paper,
    "transcript": _transcript,
    "blog":       _blog_note,
    "note":       _blog_note,
    "book":       _paper,    # books carry citations like papers
    "web":        _news,
}


def classify_attribution(
    claim_text: str,
    source_type: str,
) -> Tuple[bool, Optional[str]]:
    """
    Determine whether `claim_text` carries an explicit attribution.

    Returns
    -------
    (attributed, attribution_text)
        attributed       True if a source/basis is identified.
        attribution_text Short extracted snippet, or None.
    """
    fn = _CLASSIFIERS.get(source_type, _news)
    attributed, snippet = fn(claim_text)
    # Clip attribution text to 120 chars
    if snippet and len(snippet) > 120:
        snippet = snippet[:117] + "…"
    return attributed, snippet


def run_attribution_batch(conn, lock, limit: int = 500) -> dict:
    """
    Sweep ``argument_claims`` rows where ``attributed IS NULL`` and classify.

    Designed to run after every claim-extraction batch.  Safe to call
    repeatedly — already-classified rows are skipped.
    """
    try:
        with lock:
            rows = conn.execute(
                """
                SELECT claim_id, claim_text, source_type
                FROM argument_claims
                WHERE attributed IS NULL
                LIMIT ?
                """,
                [limit],
            ).fetchall()
    except Exception as e:
        return {"error": str(e), "updated": 0, "skipped": 0}

    updated = 0
    for claim_id, claim_text, source_type in rows:
        try:
            attributed, attribution_text = classify_attribution(
                claim_text or "", source_type or "news"
            )
            with lock:
                conn.execute(
                    """
                    UPDATE argument_claims
                    SET attributed = ?, attribution_text = ?
                    WHERE claim_id = ?
                    """,
                    [attributed, attribution_text, claim_id],
                )
            updated += 1
        except Exception:
            pass

    return {"updated": updated, "skipped": len(rows) - updated}


def suggest_speaker_with_jev(*args, **kwargs) -> dict:
    """Explicit selection among extracted actors; stored attribution is unchanged."""
    from src.argument_mining.jev_nlp_adapters import suggest_attribution

    return suggest_attribution(*args, **kwargs)
