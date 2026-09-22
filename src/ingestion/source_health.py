"""
Per-source extraction-health tracking, drift detection, and adaptive
scheduling (#878, #879).

The scrapers' blind spot: a site redesign breaks a selector, the page still
returns 200, extraction yields nothing, and the source silently goes dark —
indistinguishable from "no news today". All existing failure telemetry fires
on fetch/HTTP errors, never on "200 OK but zero articles extracted".

``SourceHealthTracker`` closes that gap with deterministic, threshold-based
rules over a rolling per-source window:

- ``record_run()`` after every harvest/scrape pass (articles extracted,
  per-field fill rates, fetch errors).
- A source is ``unknown`` until ``MIN_RUNS`` runs, then ``healthy`` /
  ``degraded`` / ``quarantined``:

  * **degraded** — recent yield or field-fill collapsed against the source's
    own baseline (median of prior runs): every recent run yielded zero while
    the baseline is >= 1; or recent mean yield fell below
    ``DEGRADED_YIELD_RATIO`` of a solid baseline; or a field that used to
    fill >= ``FILL_BASELINE_OK`` now fills < ``FILL_DEGRADED``.
  * **quarantined** — degradation persisted for ``QUARANTINE_AFTER``
    consecutive bad runs. Any healthy run fully recovers the source.

- Adaptive scheduling (#879): ``due()`` / ``next_due_ms()`` back a harvest
  loop — consecutive empty runs back the recrawl interval off exponentially
  (bounded), productive runs snap it back to the base interval, and
  quarantined sources are probed only at the max interval so they can
  self-recover without hammering a broken or blocking site.

State is in-memory with optional JSON persistence; the clock is injectable
(``now_ms``) so everything is offline-testable.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from statistics import mean, median
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# --- Drift thresholds (#878) — deterministic and documented ----------------
MIN_RUNS = 5                 # runs before a source can be judged at all
EVAL_WINDOW = 3              # most-recent runs evaluated against the baseline
HISTORY = 30                 # rolling window of retained runs per source
DEGRADED_YIELD_RATIO = 0.25  # recent mean yield below this fraction of baseline
SOLID_BASELINE = 4           # ratio rule only applies to baselines >= this
FILL_BASELINE_OK = 0.9       # a field is "reliably filled" at/above this rate
FILL_DEGRADED = 0.5          # ... and "collapsed" below this rate
QUARANTINE_AFTER = 5         # consecutive bad runs before quarantine

# --- Scheduling defaults (#879) --------------------------------------------
DEFAULT_BASE_INTERVAL_MS = 30 * 60 * 1000        # 30 min between fetches
DEFAULT_MAX_INTERVAL_MS = 24 * 60 * 60 * 1000    # never back off past 1 day

STATUSES = ("unknown", "healthy", "degraded", "quarantined")


def field_fill_rates(
    articles: Iterable[Dict[str, Any]],
    fields: Iterable[str] = ("title", "content"),
) -> Dict[str, float]:
    """Fraction of articles with a non-empty value per field (1.0 when empty run)."""
    items = list(articles)
    if not items:
        return {f: 1.0 for f in fields}
    return {
        f: sum(1 for a in items if str(a.get(f) or "").strip()) / len(items)
        for f in fields
    }


class SourceHealthTracker:
    """Rolling extraction-health state for scraped/harvested sources."""

    def __init__(self, path: Optional[str] = None):
        self._path = path
        # source_id -> {"runs": deque[dict], "bad_streak": int, "empty_streak": int,
        #               "last_run_ms": int|None}
        self._sources: Dict[str, Dict[str, Any]] = {}
        if path and os.path.exists(path):
            self._load()

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #

    def record_unchanged(self, source_id, now_ms=None):
        state = self._sources.setdefault(source_id, {'runs':deque(maxlen=HISTORY), 'bad_streak':0, 'empty_streak':0, 'last_run_ms':None})
        state['last_run_ms'] = self._now(now_ms)
        state['last_unchanged_ms'] = state['last_run_ms']
        if self._path:
            self._save()
        return self.status(source_id)

    def record_run(
        self,
        source_id: str,
        articles: int,
        field_fill: Optional[Dict[str, float]] = None,
        fetch_errors: int = 0,
        now_ms: Optional[int] = None,
    ) -> str:
        """Record one harvest/scrape pass; returns the post-run status."""
        now_ms = self._now(now_ms)
        state = self._sources.setdefault(
            source_id,
            {"runs": deque(maxlen=HISTORY), "bad_streak": 0, "empty_streak": 0,
             "last_run_ms": None},
        )
        state["runs"].append({
            "at_ms": now_ms,
            "articles": int(articles),
            "fill": dict(field_fill or {}),
            "fetch_errors": int(fetch_errors),
        })
        state["last_run_ms"] = now_ms
        state["empty_streak"] = 0 if articles > 0 else state["empty_streak"] + 1

        if self._run_is_bad(source_id):
            state["bad_streak"] += 1
        else:
            state["bad_streak"] = 0  # any healthy run fully recovers

        if self._path:
            self._save()

        status = self.status(source_id)
        if status in ("degraded", "quarantined"):
            logger.warning(
                "source-health: %s is %s (yield baseline=%s, bad_streak=%d)",
                source_id, status,
                (self.baseline(source_id) or {}).get("yield"),
                state["bad_streak"],
            )
        return status

    # ------------------------------------------------------------------ #
    # Drift assessment (#878)
    # ------------------------------------------------------------------ #

    def baseline(self, source_id: str) -> Optional[Dict[str, Any]]:
        """Median yield and per-field fill over runs preceding the eval window."""
        runs = list(self._sources.get(source_id, {}).get("runs", ()))
        history = runs[:-EVAL_WINDOW] if len(runs) > EVAL_WINDOW else []
        if len(history) < MIN_RUNS - EVAL_WINDOW or not history:
            return None
        fill_fields = {f for r in history for f in r["fill"]}
        return {
            "yield": median(r["articles"] for r in history),
            "fill": {
                f: median(r["fill"].get(f, 1.0) for r in history)
                for f in fill_fields
            },
            "runs": len(history),
        }

    def status(self, source_id: str) -> str:
        state = self._sources.get(source_id)
        if state is None or len(state["runs"]) < MIN_RUNS:
            return "unknown"
        if not self._run_is_bad(source_id):
            return "healthy"
        if state["bad_streak"] >= QUARANTINE_AFTER:
            return "quarantined"
        return "degraded"

    def is_quarantined(self, source_id: str) -> bool:
        return self.status(source_id) == "quarantined"

    def _run_is_bad(self, source_id: str) -> bool:
        """True when the recent window has collapsed against the baseline."""
        base = self.baseline(source_id)
        if base is None:
            return False
        recent = list(self._sources[source_id]["runs"])[-EVAL_WINDOW:]

        yields = [r["articles"] for r in recent]
        if base["yield"] >= 1 and all(y == 0 for y in yields):
            return True
        if base["yield"] >= SOLID_BASELINE and mean(yields) < DEGRADED_YIELD_RATIO * base["yield"]:
            return True
        for field, base_fill in base["fill"].items():
            if base_fill >= FILL_BASELINE_OK:
                recent_fill = mean(r["fill"].get(field, 1.0) for r in recent)
                if recent_fill < FILL_DEGRADED:
                    return True
        return False

    # ------------------------------------------------------------------ #
    # Adaptive scheduling (#879)
    # ------------------------------------------------------------------ #

    def next_due_ms(
        self,
        source_id: str,
        base_interval_ms: int = DEFAULT_BASE_INTERVAL_MS,
        max_interval_ms: int = DEFAULT_MAX_INTERVAL_MS,
    ) -> Optional[int]:
        """Timestamp after which the source should be fetched again.

        None (always due) for sources with no recorded runs. Empty runs back
        the interval off exponentially (bounded); a productive run resets it
        to the base. Quarantined sources are probed at the max interval only.
        """
        state = self._sources.get(source_id)
        if state is None or state["last_run_ms"] is None:
            return None
        if self.is_quarantined(source_id):
            interval = max_interval_ms
        else:
            interval = min(
                base_interval_ms * (2 ** min(state["empty_streak"], 16)),
                max_interval_ms,
            )
        return state["last_run_ms"] + interval

    def due(
        self,
        source_id: str,
        now_ms: Optional[int] = None,
        base_interval_ms: int = DEFAULT_BASE_INTERVAL_MS,
        max_interval_ms: int = DEFAULT_MAX_INTERVAL_MS,
    ) -> bool:
        """Whether a harvest loop should fetch this source now."""
        next_at = self.next_due_ms(source_id, base_interval_ms, max_interval_ms)
        return True if next_at is None else self._now(now_ms) >= next_at

    # ------------------------------------------------------------------ #
    # Reporting / persistence
    # ------------------------------------------------------------------ #

    def report(self) -> List[Dict[str, Any]]:
        """Per-source health summary for monitoring/MCP surfaces."""
        out = []
        for source_id, state in sorted(self._sources.items()):
            runs = list(state["runs"])
            out.append({
                "source_id": source_id,
                "status": self.status(source_id),
                "runs": len(runs),
                "last_articles": runs[-1]["articles"] if runs else None,
                "baseline": self.baseline(source_id),
                "bad_streak": state["bad_streak"],
                "empty_streak": state["empty_streak"],
                "last_run_ms": state["last_run_ms"],
                "last_unchanged_ms": state.get("last_unchanged_ms"),
            })
        return out

    def _save(self) -> None:
        try:
            payload = {
                sid: {**state, "runs": list(state["runs"])}
                for sid, state in self._sources.items()
            }
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except OSError as exc:  # persistence is best-effort, never fatal
            logger.warning("source-health: could not persist state: %s", exc)

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as fh:
                payload = json.load(fh)
            for sid, state in payload.items():
                self._sources[sid] = {
                    "runs": deque(state.get("runs", []), maxlen=HISTORY),
                    "bad_streak": int(state.get("bad_streak", 0)),
                    "empty_streak": int(state.get("empty_streak", 0)),
                    "last_run_ms": state.get("last_run_ms"),
                    "last_unchanged_ms": state.get("last_unchanged_ms"),
                }
        except (OSError, ValueError) as exc:
            logger.warning("source-health: could not load state (%s); starting fresh", exc)
            self._sources = {}

    @staticmethod
    def _now(now_ms: Optional[int]) -> int:
        return int(time.time() * 1000) if now_ms is None else int(now_ms)
