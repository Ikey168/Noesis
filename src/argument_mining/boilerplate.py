"""Recognise page furniture that a sentence-level claim detector mistakes for claims.

Navigation, marketing and share-button text, bylines and fragments too short
to state anything are not claims. :func:`boilerplate_reason` names why a
sentence is furniture, or returns None. The patterns are deliberately
narrow: most require a short sentence, so a real claim that merely mentions
a newsletter or a share button is kept.

Wording repeated across documents of the same site is corpus-level and is
handled by :mod:`src.kb.claim_exclusions`.
"""

from __future__ import annotations

import re
import unicodedata

#: A claim needs at least this many words and characters to state anything.
MIN_WORDS = 4
MIN_CHARS = 20
#: Furniture patterns only apply to sentences up to this many words.
SHORT_WORDS = 25

_WORD = re.compile(r"[^\W\d_][\w'’-]*|\d[\w.,%]*", re.UNICODE)

#: Navigation words that are only furniture when they stand alone or label a
#: link ("Next post: ...", "Home"), never as part of a sentence ("Next, the
#: loader downloads ...").
_NAVIGATION_LABEL = re.compile(
    r"^\W*(skip to (main )?content|main menu|menu|home|back to top|go to top|"
    r"(previous|next|older|newer)( post| article| story| posts| page)?|"
    r"categories|tags?|filed under|posted in|jump to|table of contents)"
    r"\s*(?:$|[:|»›>\-–—]|\W*$)",
    re.IGNORECASE,
)
#: Link prompts that are furniture wherever the sentence goes.
_NAVIGATION = re.compile(
    r"^\W*(read more|continue reading|read the full (story|article|report|post)|"
    r"view all|see all|show more|load more|related (posts|articles|stories|reading|content)|"
    r"related:|see also:|also read:|read next:|up next:|don't miss:|trending:|"
    r"you (may|might) also (like|enjoy)|recommended for you|more from|popular (posts|stories))",
    re.IGNORECASE,
)
_BREADCRUMB = re.compile(r"\s(?:\||»|›|>|/|·)\s")
_MARKETING = re.compile(
    r"\b(subscribe (to|now|today|for)|sign up (for|now|today|to)|"
    r"(join|subscribe to) (our|the) (newsletter|mailing list|community)|our newsletter|"
    r"free trial|start (your|a) (free )?trial|book a demo|request a demo|schedule a demo|"
    r"get started (now|today|for free)|contact (us|sales|our team)|talk to (an expert|sales)|"
    r"download (our|the) (free )?(report|whitepaper|white paper|e-?book|guide|datasheet)|"
    r"follow us( on)?|all rights reserved|cookie (policy|settings|preferences)|"
    r"we use cookies|privacy policy|terms of (use|service)|advertisement|sponsored content|"
    r"learn more about (our|how we)|try it (free|now)|buy now|limited time offer|"
    r"(trusted|used|loved) by (over |more than )?\d[\d,.]*\s*[km]?\+?\s*(orgs?|organi[sz]ations|"
    r"companies|customers|teams|developers|enterprises|users|businesses)|"
    r"(protect|secure) your (business|organi[sz]ation|team|code|supply chain) (with|today)|"
    r"request (a|your) (free )?(quote|trial|assessment|consultation)|"
    r"(see|watch) (it|us) in action|start for free|get (a|your) free)\b",
    re.IGNORECASE,
)
_SHARE = re.compile(
    r"\b(share (this|on|via|the) (post|article|story|page|link)?|click to share|"
    r"tweet this|copy (the )?link|share on (twitter|x|facebook|linkedin|reddit|whatsapp|"
    r"telegram|email|mastodon|bluesky))\b|"
    r"\b(facebook|twitter|linkedin|reddit|whatsapp|telegram|email|pinterest)\b"
    r"(\W+\b(facebook|twitter|linkedin|reddit|whatsapp|telegram|email|pinterest|x)\b){2,}",
    re.IGNORECASE,
)
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_NAME = rf"(?!(?:{_MONTHS})\b)[A-Z][\w'’.-]+(?:\s+(?:[A-Z][\w'’.-]+|de|van|von|da|di|bin|al))*"
#: "By Jane Doe", "By Jane Doe, Senior Researcher", "Posted by A. Kim on March 3"
#: -- but never "By March, the botnet ..." or "By default, the package ...".
_BYLINE = re.compile(
    rf"^\W*(?i:by|posted by|written by|authored by|author:|reported by|words by)\s+{_NAME}"
    rf"(\s*(,|and|&)\s*{_NAME})*"
    r"(\s*[,|·—–-]\s*[A-Z][\w\s,.'’&/-]*|\s+(?i:on|at|in)\s+.+|\s+\d.*)?\W*$",
)
BYLINE_MAX_WORDS = 14
#: Author bio lines: "Jane Doe is a senior threat researcher at Acme."
_AUTHOR_BIO = re.compile(
    rf"^\W*(?!(?:The|A|An|This|That|These|Those|It|He|She|They|We|Our|Their|His|Her|Its)\b){_NAME}"
    r"\s+(?:is|was|has been)\s+(?:a|an|the)\s+[\w\s,&/-]{0,60}?"
    r"\b(?:researcher|analyst|engineer|writer|editor|journalist|reporter|director|manager|"
    r"lead|head|founder|co-founder|ceo|cto|ciso|specialist|consultant|contributor|"
    r"evangelist|advocate|architect|scientist|strategist|correspondent)s?\b"
)
BIO_MAX_WORDS = 30
_DATELINE = re.compile(
    r"^\W*(published|updated|posted|last updated|last modified|date)\b( on)?\s*[:\-–—]?\s*"
    r"(\w+\.? \d{1,2},? \d{4}|\d{4}-\d{2}-\d{2}|\d{1,2} \w+ \d{4}|\d{1,2}/\d{1,2}/\d{2,4})",
    re.IGNORECASE,
)
_READ_TIME = re.compile(r"\b\d+\s*(min|minute)s?( read| to read)\b", re.IGNORECASE)
_CREDIT = re.compile(r"^\W*(photo|image|illustration|video|source|credit)s?( credit)?\s*:", re.IGNORECASE)


def words(text: str) -> list[str]:
    return _WORD.findall(text or "")


def normalize(text: str) -> str:
    """Wording key for repetition checks: case, accents, punctuation folded."""
    folded = unicodedata.normalize("NFKD", text or "").casefold()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[^\W_]+", folded))


def boilerplate_reason(text: str) -> str | None:
    """``too_short``, ``navigation``, ``share``, ``byline`` or ``marketing``; else None."""
    stripped = (text or "").strip()
    tokens = words(stripped)
    if len(tokens) < MIN_WORDS or len(stripped) < MIN_CHARS:
        return "too_short"
    short = len(tokens) <= SHORT_WORDS
    if short and (
        _NAVIGATION_LABEL.match(stripped) or _NAVIGATION.match(stripped)
        or len(_BREADCRUMB.findall(stripped)) >= 2
    ):
        return "navigation"
    if short and _SHARE.search(stripped):
        return "share"
    if short and (
        (_BYLINE.match(stripped) and len(tokens) <= BYLINE_MAX_WORDS)
        or (_AUTHOR_BIO.match(stripped) and len(tokens) <= BIO_MAX_WORDS)
        or _DATELINE.match(stripped)
        or _READ_TIME.search(stripped) or _CREDIT.match(stripped)
    ):
        return "byline"
    if short and _MARKETING.search(stripped):
        return "marketing"
    return None
