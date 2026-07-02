"""
Heuristic intent planner.

Scores intent facets from keyword evidence, extracts a topic phrase, a
source-type filter and a time window, then assembles a prioritized panel
layout from the catalog. Always available — no model, network or API key —
mirroring the heuristic-fallback convention used by the argument-mining
inference wrappers.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from src.genui.adaptivity import (
    apply_signals,
    filter_panels,
    normalize_signals,
)
from src.genui.catalog import PANEL_CATALOG, get_panel_def
from src.genui.spec import MAX_PANELS, PanelSpec, UISpec

# Keyword evidence per facet. Multi-word phrases are matched as substrings of
# the normalized intent; single words are matched as whole tokens.
FACET_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "trend": (
        "trend", "trends", "trending", "over time", "shift", "shifted",
        "evolution", "evolve", "evolved", "changing", "changed", "history",
        "momentum", "trajectory", "drift",
    ),
    "sentiment": (
        "sentiment", "tone", "mood", "feeling", "positive", "negative",
        "emotion", "optimism", "pessimism",
    ),
    "claims": (
        "claim", "claims", "fact", "facts", "factcheck", "fact-check",
        "verify", "verified", "unverified", "true", "false",
        "misinformation", "disinformation", "evidence", "assertion",
        "unsourced",
    ),
    "stance": (
        "stance", "stances", "support", "supports", "supportive", "oppose",
        "opposes", "opposition", "critical", "position", "positions",
        "agree", "agrees", "disagree", "disagrees", "pro", "against",
    ),
    "actors": (
        "who", "actor", "actors", "stakeholder", "stakeholders",
        "politician", "politicians", "speaker", "speakers", "people",
        "person", "says", "said", "voices",
    ),
    "conflict": (
        "conflict", "conflicts", "controversy", "controversial",
        "contradict", "contradicts", "contradiction", "contradictions",
        "dispute", "disputes", "disagreement", "clash", "debate",
    ),
    "sources": (
        "outlet", "outlets", "source", "sources", "bias", "biased",
        "framing", "frame", "frames", "coverage", "compare", "comparison",
        "transparency", "media", "publisher", "publishers",
    ),
    "entities": (
        "entity", "entities", "network", "graph", "connection",
        "connections", "related", "relationship", "relationships",
        "influence",
    ),
    "events": (
        "event", "events", "cluster", "clusters", "story", "stories",
        "breaking", "timeline", "happening", "developments", "watchlist",
        "watchlists", "alert", "alerts",
    ),
    "library": (
        "library", "document", "documents", "docs", "archive", "reading",
        "publications", "ingested", "corpus",
    ),
}

_SOURCE_TYPE_WORDS: Dict[str, str] = {
    "news": "news",
    "blog": "blog",
    "blogs": "blog",
    "paper": "paper",
    "papers": "paper",
    "book": "book",
    "books": "book",
    "transcript": "transcript",
    "transcripts": "transcript",
    "note": "note",
    "notes": "note",
}

_TIME_WORDS: Dict[str, int] = {
    "today": 1,
    "yesterday": 2,
    "week": 7,
    "weekly": 7,
    "fortnight": 14,
    "month": 30,
    "monthly": 30,
    "quarter": 90,
    "year": 365,
}

_STOPWORDS = frozenset(
    """a about an and are as at be been being between but by can concerning
    could did do does for from give had has have how i in into is it its last
    latest me my of on or our regarding show shows tell that the their them
    then there these this those to us versus vs was we were what when where
    which will with would you your recent past across""".split()
)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]*")
_DAYS_RE = re.compile(r"(\d+)\s*day")

DEFAULT_FACETS = ("overview",)


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall(text.lower())


def score_facets(intent: str) -> Dict[str, int]:
    """Count keyword evidence for each facet in the intent."""
    normalized = " ".join(_tokenize(intent))
    tokens = set(normalized.split())
    scores: Dict[str, int] = {}
    for facet, keywords in FACET_KEYWORDS.items():
        hits = 0
        for kw in keywords:
            if " " in kw or "-" in kw:
                if kw in normalized:
                    hits += 1
            elif kw in tokens:
                hits += 1
        if hits:
            scores[facet] = hits
    return scores


def detect_source_type(intent: str) -> Optional[str]:
    """Return an explicit source-type filter mentioned in the intent."""
    for token in _tokenize(intent):
        if token in _SOURCE_TYPE_WORDS:
            return _SOURCE_TYPE_WORDS[token]
    return None


def detect_days(intent: str) -> Optional[int]:
    """Return a time window in days if the intent mentions one."""
    lowered = intent.lower()
    match = _DAYS_RE.search(lowered)
    if match:
        return max(1, min(365, int(match.group(1))))
    for token in _tokenize(intent):
        if token in _TIME_WORDS:
            return _TIME_WORDS[token]
    return None


def extract_topic(intent: str) -> Optional[str]:
    """Extract the topic phrase: what's left after facet/time/type words."""
    facet_words = set()
    for keywords in FACET_KEYWORDS.values():
        for kw in keywords:
            facet_words.update(kw.replace("-", " ").split())
    keep: List[str] = []
    for token in _tokenize(intent):
        if token in _STOPWORDS or token in facet_words:
            continue
        if token in _SOURCE_TYPE_WORDS or token in _TIME_WORDS:
            continue
        if token.isdigit() or token == "days":
            continue
        keep.append(token)
    if not keep:
        return None
    return " ".join(keep[:5])


def _facet_panels(facets: Iterable[str]) -> List[PanelSpec]:
    """Assemble candidate panels for the selected facets, deduplicated."""
    weights = {}
    for rank, facet in enumerate(facets):
        weights[facet] = max(0.4, 1.0 - 0.2 * rank)

    chosen: Dict[str, PanelSpec] = {}
    for facet, weight in weights.items():
        position = 0
        for pdef in PANEL_CATALOG:
            if pdef.type == "note" or facet not in pdef.facets:
                continue
            priority = max(0.05, min(1.0, weight - 0.05 * position))
            position += 1
            existing = chosen.get(pdef.type)
            if existing is None or priority > existing.priority:
                chosen[pdef.type] = PanelSpec(
                    id=pdef.type,
                    type=pdef.type,
                    title=pdef.title,
                    span=pdef.default_span,
                    priority=priority,
                    rationale=f"selected for the '{facet}' facet",
                )
    return sorted(chosen.values(), key=lambda p: -p.priority)


def _apply_params(
    panels: List[PanelSpec],
    topic: Optional[str],
    source_type: Optional[str],
    days: Optional[int],
) -> None:
    for panel in panels:
        pdef = get_panel_def(panel.type)
        if pdef is None:
            continue
        panel.endpoint = pdef.endpoint
        if topic and pdef.topic_param:
            panel.params[pdef.topic_param] = topic
        if source_type and pdef.source_type_param:
            panel.params[pdef.source_type_param] = source_type
        if days and pdef.days_param:
            # Each endpoint bounds its days Query param; exceeding it would
            # draw a 422 and blank the panel.
            panel.params[pdef.days_param] = min(days, pdef.max_days) if pdef.max_days else days


def _narrative(
    intent: str,
    facets: List[str],
    topic: Optional[str],
    source_type: Optional[str],
    days: Optional[int],
    dropped: List[str],
) -> str:
    parts = []
    if intent.strip():
        parts.append(f"Canvas generated for “{intent.strip()}”.")
    else:
        parts.append("Default adaptive briefing canvas.")
    parts.append("Focus: " + ", ".join(facets) + ".")
    if topic:
        parts.append(f"Topic filter: {topic}.")
    if source_type:
        parts.append(f"Source type: {source_type}.")
    if days:
        parts.append(f"Window: last {days} days.")
    if dropped:
        parts.append(
            "Hidden for now (no data or disabled pack): " + ", ".join(sorted(set(dropped))) + "."
        )
    return " ".join(parts)


def plan(
    intent: str = "",
    source_type: Optional[str] = None,
    signals: Optional[Dict] = None,
    availability: Optional[Dict[str, bool]] = None,
    ui_flags: Optional[Dict[str, bool]] = None,
) -> UISpec:
    """Generate a ui-spec-v1 layout for an intent.

    ``availability`` maps warehouse table names to whether they hold data
    (None = unknown, panels are kept); ``ui_flags`` is the merged domain-pack
    flag map; ``signals`` is the client usage-signal dict.
    """
    intent = (intent or "")[:500]
    scores = score_facets(intent)
    facets = [f for f, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))][:3]
    if not facets:
        facets = list(DEFAULT_FACETS)

    topic = extract_topic(intent)
    detected_type = detect_source_type(intent)
    effective_type = source_type or detected_type
    days = detect_days(intent)

    candidates = _facet_panels(facets)
    kept, dropped = filter_panels(candidates, availability, ui_flags)

    # Never render an empty canvas: if adaptivity removed every data panel,
    # fall back to the overview set gated only by ui_flags (the frontend
    # renders demo data for panels whose endpoints come back empty).
    if not kept:
        fallback = _facet_panels(DEFAULT_FACETS)
        kept, _ = filter_panels(fallback, None, ui_flags)
        for panel in kept:
            panel.rationale = "fallback overview (no live data detected)"

    normalized = normalize_signals(signals)
    kept, dismissed_types = apply_signals(kept, normalized)
    dropped = dropped + dismissed_types

    # Usage signals can also empty the canvas (every surviving panel muted).
    # Show the overview set minus muted types — or all of it when the
    # operator muted literally everything — so there is always a way back.
    if not kept:
        fallback, _ = filter_panels(_facet_panels(DEFAULT_FACETS), None, ui_flags)
        dismissed_set = set(normalized["dismissed"])
        kept = [p for p in fallback if p.type not in dismissed_set] or fallback
        for panel in kept:
            panel.rationale = "fallback overview (everything else muted)"

    kept = kept[: MAX_PANELS - 1]

    _apply_params(kept, topic, effective_type, days)

    note = PanelSpec(
        id="note",
        type="note",
        title="Plan",
        span=12,
        priority=1.0,
        rationale="how this canvas was assembled",
        body=_narrative(intent, facets, topic, effective_type, days, dropped),
    )
    panels = [note] + kept
    for i, panel in enumerate(panels):
        panel.id = f"p{i + 1}"

    title = topic.title() if topic else "Adaptive Briefing"
    subtitle_bits = [", ".join(facets)]
    if effective_type:
        subtitle_bits.append(effective_type)
    if days:
        subtitle_bits.append(f"{days}d window")

    return UISpec(
        intent=intent,
        title=title,
        subtitle=" · ".join(subtitle_bits),
        generated_by="heuristic",
        facets=facets,
        topic=topic,
        source_type=effective_type,
        panels=panels,
    )
