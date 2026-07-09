"""
Polls and survey aggregates as evidence (candidate track #787).

Claims about public opinion are as common as claims about statistics. A poll is
a sibling of a Track A series: a reading of support for an option over a
fieldwork period, carrying methodology (sample size, mode, margin of error,
house, question wording) as first-class fields — exactly the discipline the
statistical-honesty contract asks for.

A poll reading becomes a ``dataset-series-v1`` ``SeriesRecord`` with
``provider = "poll"`` and its methodology in ``metadata``, so it lives in the
same observation store. Opinion claims ("a majority support X", "45% back the
plan") are parsed and checked against the poll with the margin of error as the
tolerance, and the question wording carried as a declared assumption.

Stdlib only; connection-injected for the check. Reuses the honesty envelope.

See ``docs/architecture/BEYOND_TEXT_ROADMAP.md`` §4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.ingest.common.series_model import Observation, SeriesRecord
from src.analytics.honesty import analytic_envelope, interval


@dataclass
class PollMethodology:
    sample_n: Optional[int] = None
    mode: Optional[str] = None          # phone / online / mixed
    margin_of_error: Optional[float] = None  # percentage points
    house: Optional[str] = None
    population: Optional[str] = None     # adults / likely voters / ...
    question: Optional[str] = None

    def to_metadata(self) -> Dict[str, Any]:
        return {k: v for k, v in {
            "sample_n": self.sample_n,
            "mode": self.mode,
            "margin_of_error": self.margin_of_error,
            "house": self.house,
            "population": self.population,
            "question": self.question,
        }.items() if v is not None}


@dataclass
class PollReading:
    topic: str
    option: str            # the option/answer being measured (e.g. "support")
    support_pct: float     # 0..100
    period: str            # fieldwork period, e.g. "2024-03"
    methodology: PollMethodology = field(default_factory=PollMethodology)
    geography: Optional[str] = None


def poll_to_series(reading: PollReading, poll_id: str, as_of: int = 0, source_url: Optional[str] = None) -> SeriesRecord:
    """A poll reading as a document-store series (provider='poll')."""
    slug = re.sub(r"[^a-z0-9]+", "-", f"{reading.topic}-{reading.option}".lower()).strip("-")
    series_id = f"poll:{poll_id}:{slug}"
    meta = {"topic": reading.topic, "option": reading.option, **reading.methodology.to_metadata()}
    return SeriesRecord(
        series_id=series_id,
        provider="poll",
        title=f"{reading.option} for {reading.topic}",
        frequency="irregular",
        as_of=as_of,
        observations=[Observation(period=reading.period, value=reading.support_pct)],
        unit="percent",
        geography=reading.geography,
        license=reading.methodology.house or "polling aggregate",
        source_url=source_url,
        metadata=meta,
    )


# Opinion-claim parsing -----------------------------------------------------

_MAJORITY_RE = re.compile(r"\b(majorit(y|ies)|most (people|voters|respondents|americans))\b", re.IGNORECASE)
_PLURALITY_RE = re.compile(r"\bplural(ity|ities)\b", re.IGNORECASE)
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|percent|per cent)", re.IGNORECASE)
_SUPPORT_RE = re.compile(r"\b(support|back|favou?r|approve|oppose|reject|against)\b", re.IGNORECASE)


@dataclass
class OpinionClaim:
    threshold: float          # the % the claim asserts opinion clears
    direction: str            # exceeds | equals
    polarity: str             # support | oppose
    text: str


def parse_opinion_claim(text: str) -> Optional[OpinionClaim]:
    """Parse an opinion claim into a threshold check, or None if not one."""
    if not text:
        return None
    lowered = text.lower()
    has_majority = bool(_MAJORITY_RE.search(text) or _PLURALITY_RE.search(text))
    has_support_cue = bool(_SUPPORT_RE.search(text))
    # An opinion claim needs an opinion cue — otherwise a statistical percentage
    # claim ("GDP rose 3%") would be misread as one.
    if not (has_majority or has_support_cue):
        return None
    polarity = "oppose" if re.search(r"\b(oppose|against|reject)\b", lowered) else "support"
    pct = _PCT_RE.search(text)
    if pct and has_support_cue:
        return OpinionClaim(threshold=float(pct.group(1)), direction="exceeds" if re.search(r"\b(more than|over|at least|exceed)\b", lowered) else "equals", polarity=polarity, text=text.strip())
    if _MAJORITY_RE.search(text):
        return OpinionClaim(threshold=50.0, direction="exceeds", polarity=polarity, text=text.strip())
    if _PLURALITY_RE.search(text):
        # A plurality: the largest bloc; approximated as > 33% (weak).
        return OpinionClaim(threshold=33.0, direction="exceeds", polarity=polarity, text=text.strip())
    return None


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


def check_opinion(conn, claim_text: str, topic: str, option: str = "support") -> Dict[str, Any]:
    """Check an opinion claim against the matching poll series.

    Resolves by topic+option among ``provider='poll'`` series, compares the
    latest support reading to the claimed threshold with the margin of error as
    tolerance, and carries the question wording + methodology as assumptions.
    """
    parsed = parse_opinion_claim(claim_text)
    if parsed is None:
        return analytic_envelope(n=0, method="opinion-claim check", assumptions=["not an opinion claim"], verdict="unverifiable")
    if not _table_exists(conn, "dataset_series"):
        return analytic_envelope(n=0, method="opinion-claim check", assumptions=["no poll series available"], verdict="unverifiable")

    slug = re.sub(r"[^a-z0-9]+", "-", f"{topic}-{option}".lower()).strip("-")
    row = conn.execute(
        """
        SELECT series_id, metadata FROM dataset_series
        WHERE provider = 'poll' AND series_id LIKE ?
        ORDER BY as_of DESC LIMIT 1
        """,
        [f"poll:%:{slug}"],
    ).fetchone()
    if row is None:
        return analytic_envelope(n=0, method="opinion-claim check", assumptions=["no matching poll"], verdict="unverifiable")
    series_id = row[0]
    import json

    meta = json.loads(row[1]) if isinstance(row[1], str) else (row[1] or {})
    obs = conn.execute(
        "SELECT value FROM dataset_observations WHERE series_id = ? ORDER BY period DESC LIMIT 1", [series_id]
    ).fetchone()
    if obs is None or obs[0] is None:
        return analytic_envelope(n=0, method="opinion-claim check", assumptions=["poll has no reading"], verdict="unverifiable")
    observed = float(obs[0])
    moe = meta.get("margin_of_error") or 3.0

    assumptions = [
        f"poll question: {meta.get('question', 'unstated')}",
        f"sample n={meta.get('sample_n', 'unknown')}, mode={meta.get('mode', 'unknown')}, house={meta.get('house', 'unknown')}",
        f"margin of error ±{moe}pp used as tolerance",
    ]
    if parsed.direction == "exceeds":
        verdict = "supported" if observed > parsed.threshold + moe else ("contradicted" if observed < parsed.threshold - moe else "unverifiable")
    else:  # equals
        verdict = "supported" if abs(observed - parsed.threshold) <= moe else "contradicted"
    return analytic_envelope(
        n=meta.get("sample_n") or 1,
        method="opinion-claim check (poll)",
        assumptions=assumptions,
        verdict=verdict,
        claimed_threshold=parsed.threshold,
        observed=interval(observed, observed - moe, observed + moe),
        series_id=series_id,
        polarity=parsed.polarity,
    )
