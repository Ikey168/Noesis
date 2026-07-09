"""
Polling aggregate source for polls-as-evidence (#822).

The polls core (``polls.py``) checks opinion claims against stored
``PollReading``s. This module feeds it from a real source: a **CSV polling
aggregate** in the layout the major public aggregates publish (one row per
poll: pollster, dates, sample size, mode, margin of error, question, and one
column per answer option).

The column mapping is configurable so any aggregate export fits; the default
mapping matches the common `pollster/end_date/sample_size/moe/...` layout.
Methodology fields are preserved per reading — never averaged away silently.
The fetch is injectable, so parsing is fully offline-testable; operators must
check the aggregate's licensing terms before wiring a live URL (documented in
the integration guide).
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from src.ingestion.connectors.dataset.polls import PollMethodology, PollReading, poll_to_series
from src.ingestion.connectors.dataset.store import ObservationStore

logger = logging.getLogger(__name__)


@dataclass
class PollColumnMap:
    """Which CSV columns carry what. ``options`` maps answer-option columns
    (e.g. ``{"support": "yes_pct", "oppose": "no_pct"}``)."""

    topic: str = "topic"
    pollster: str = "pollster"
    end_date: str = "end_date"          # fieldwork end, YYYY-MM-DD -> period YYYY-MM
    sample_size: str = "sample_size"
    mode: str = "methodology"
    margin_of_error: str = "moe"
    population: str = "population"
    question: str = "question"
    options: Dict[str, str] = field(default_factory=lambda: {"support": "support_pct", "oppose": "oppose_pct"})


def _to_float(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace("%", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _to_int(value: Optional[str]) -> Optional[int]:
    number = _to_float(value)
    return int(number) if number is not None else None


def _period(end_date: Optional[str]) -> Optional[str]:
    if not end_date:
        return None
    text = str(end_date).strip()
    if len(text) >= 7 and text[4] in "-/":
        return f"{text[:4]}-{text[5:7]}"
    return None


def parse_poll_csv(
    csv_text: str,
    column_map: Optional[PollColumnMap] = None,
    topic: Optional[str] = None,
) -> List[PollReading]:
    """Parse an aggregate CSV into PollReadings, one per (row, answer option).

    Rows missing a period or a numeric option value are skipped (logged), never
    guessed. ``topic`` overrides/provides the topic when the CSV lacks a topic
    column (a single-question aggregate export).
    """
    cmap = column_map or PollColumnMap()
    readings: List[PollReading] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        row_topic = (row.get(cmap.topic) or topic or "").strip()
        period = _period(row.get(cmap.end_date))
        if not row_topic or period is None:
            logger.debug("poll_source: skipping row without topic/period: %r", row)
            continue
        methodology = PollMethodology(
            sample_n=_to_int(row.get(cmap.sample_size)),
            mode=(row.get(cmap.mode) or "").strip() or None,
            margin_of_error=_to_float(row.get(cmap.margin_of_error)),
            house=(row.get(cmap.pollster) or "").strip() or None,
            population=(row.get(cmap.population) or "").strip() or None,
            question=(row.get(cmap.question) or "").strip() or None,
        )
        for option, column in cmap.options.items():
            pct = _to_float(row.get(column))
            if pct is None:
                continue
            readings.append(
                PollReading(
                    topic=row_topic,
                    option=option,
                    support_pct=pct,
                    period=period,
                    methodology=methodology,
                )
            )
    return readings


def harvest_polls(
    url: str,
    store: ObservationStore,
    fetch: Optional[Callable[[str], str]] = None,
    column_map: Optional[PollColumnMap] = None,
    topic: Optional[str] = None,
    as_of: int = 0,
) -> int:
    """Fetch an aggregate CSV and store every reading as a poll series.

    One series per (poll house, topic, option); readings from the same house
    across waves accumulate as observations. Returns readings stored. With no
    ``fetch`` injected, uses urllib (operators: check the aggregate's terms).
    """
    if fetch is None:
        def fetch(u: str) -> str:  # pragma: no cover - trivial network shim
            import urllib.request

            with urllib.request.urlopen(u, timeout=30) as resp:  # noqa: S310
                return resp.read().decode("utf-8")

    readings = parse_poll_csv(fetch(url), column_map=column_map, topic=topic)
    stored = 0
    for reading in readings:
        house_slug = (reading.methodology.house or "aggregate").lower().replace(" ", "-")
        store.upsert(poll_to_series(reading, poll_id=f"{house_slug}-{reading.period}", as_of=as_of, source_url=url))
        stored += 1
    return stored
