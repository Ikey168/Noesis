"""
Claim-vs-data checking (Track A / A4).

The payoff of Track A: a quantitative assertion (parsed by
``src.argument_mining.quantities``) is resolved to candidate statistical series
in the observation store and checked against the data, producing a
**supported / contradicted / unverifiable** finding under the statistical-honesty
contract (``src.analytics.honesty``).

Verdicts are three-valued and conservative. A bare boolean is a contract
violation; an assertion that resolves to no series above the match threshold, or
whose series lacks the needed observations, is ``unverifiable`` — never a guess.

Every result is an :func:`analytic_envelope` (``n`` = observations used,
``method``, ``assumptions`` including match confidence, the series definition,
and the vintage checked) with an interval on the observed headline value.

See ``docs/architecture/EVIDENCE_DATASETS_PLAN.md`` §3.5.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from src.analytics.honesty import analytic_envelope, interval

DEFAULT_MATCH_THRESHOLD = 0.35
DEFAULT_REL_TOLERANCE = 0.05  # 5% fallback tolerance for magnitude checks

_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "and", "for", "by", "its",
    "their", "our", "this", "that", "total", "rate", "percent", "index",
    "has", "have", "is", "was", "now",
}


def _tokens(text: Optional[str]) -> List[str]:
    if not text:
        return []
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def _table_exists(conn, table: str) -> bool:
    try:
        return bool(conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall())
    except Exception:  # noqa: BLE001
        return False


@dataclass
class Candidate:
    series_id: str
    title: str
    unit: Optional[str]
    geography: Optional[str]
    match_confidence: float


def resolve_series(conn, assertion, threshold: float = DEFAULT_MATCH_THRESHOLD) -> List[Candidate]:
    """Rank stored series by compatibility with an assertion.

    Scoring: subject/title token overlap (base), plus credit for matching unit
    and geography. Candidates below ``threshold`` are dropped, so a poor match
    surfaces as "no candidate" (``unverifiable``) rather than a wrong series.
    """
    if not _table_exists(conn, "dataset_series"):
        return []
    subj = _tokens(getattr(assertion, "subject", None))
    a_unit = getattr(assertion, "unit", None)
    a_geo = getattr(assertion, "geography", None)

    rows = conn.execute(
        "SELECT series_id, title, unit, geography FROM dataset_series"
    ).fetchall()
    candidates: List[Candidate] = []
    for series_id, title, unit, geography in rows:
        title_tokens = set(_tokens(title))
        if not subj or not title_tokens:
            overlap = 0.0
        else:
            overlap = len(set(subj) & title_tokens) / len(set(subj))
        score = 0.6 * overlap
        # Geography: a positive match helps; a definite mismatch is fatal.
        if a_geo and geography:
            if a_geo == geography:
                score += 0.25
            else:
                continue  # different country: not this series
        if a_unit and unit and a_unit == unit:
            score += 0.15
        score = min(score, 1.0)
        if score >= threshold:
            candidates.append(Candidate(series_id, title, unit, geography, round(score, 3)))
    candidates.sort(key=lambda c: c.match_confidence, reverse=True)
    return candidates


def _observations(conn, series_id: str, as_of: Optional[int]) -> Dict[str, Optional[float]]:
    if as_of is None:
        latest = conn.execute(
            "SELECT MAX(as_of) FROM dataset_observations WHERE series_id = ?", [series_id]
        ).fetchone()
        if not latest or latest[0] is None:
            return {}
        as_of = latest[0]
    rows = conn.execute(
        "SELECT period, value FROM dataset_observations WHERE series_id = ? AND as_of = ? ORDER BY period",
        [series_id, as_of],
    ).fetchall()
    return {p: v for p, v in rows}


def _latest_as_of(conn, series_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT MAX(as_of) FROM dataset_observations WHERE series_id = ?", [series_id]
    ).fetchone()
    return row[0] if row else None


def revision_tolerance(conn, series_id: str, period: str) -> Optional[float]:
    """Max absolute spread of a period's value across stored vintages, used as
    the tolerance for magnitude checks. None when only one vintage exists."""
    rows = conn.execute(
        "SELECT value FROM dataset_observations WHERE series_id = ? AND period = ? AND value IS NOT NULL",
        [series_id, period],
    ).fetchall()
    vals = [r[0] for r in rows]
    if len(vals) < 2:
        return None
    return max(vals) - min(vals)


def _prior_period(periods: List[str], period: str) -> Optional[str]:
    ordered = sorted(periods)
    if period not in ordered:
        return None
    i = ordered.index(period)
    return ordered[i - 1] if i > 0 else None


def _unverifiable(reason: str, assertion, series_id: Optional[str] = None) -> Dict[str, Any]:
    return analytic_envelope(
        n=0,
        method="claim-vs-data check",
        assumptions=[reason],
        verdict="unverifiable",
        reason=reason,
        subject=getattr(assertion, "subject", None),
        direction=getattr(assertion, "direction", None),
        series_id=series_id,
    )


def check_assertion(
    conn,
    assertion,
    series_id: Optional[str] = None,
    as_of: Optional[int] = None,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> Dict[str, Any]:
    """Check one assertion against the data. Returns an honesty envelope with a
    three-valued ``verdict``."""
    if not _table_exists(conn, "dataset_observations"):
        return _unverifiable("no dataset observations harvested", assertion)

    match_confidence = None
    if series_id is None:
        candidates = resolve_series(conn, assertion, threshold=threshold)
        if not candidates:
            return _unverifiable("no matching series above threshold", assertion)
        top = candidates[0]
        series_id = top.series_id
        match_confidence = top.match_confidence
        series_title = top.title
    else:
        row = conn.execute(
            "SELECT title FROM dataset_series WHERE series_id = ?", [series_id]
        ).fetchone()
        series_title = row[0] if row else series_id

    if as_of is None:
        as_of = _latest_as_of(conn, series_id)
    obs = _observations(conn, series_id, as_of)
    if not obs:
        return _unverifiable("series has no observations at this vintage", assertion, series_id)

    direction = getattr(assertion, "direction", None)
    period = getattr(assertion, "period", None)
    a_value = getattr(assertion, "value", None)

    assumptions = [
        f"series: {series_title}",
        f"vintage as_of={as_of}",
    ]
    if match_confidence is not None:
        assumptions.append(f"series match confidence={match_confidence}")

    # --- comparison-to-a-value directions (exceeds / below / equals) --------
    if direction in ("exceeds", "below", "equals") and a_value is not None:
        if period and ".." not in period:
            # A claim about a specific period is unverifiable if that period is
            # absent — never silently substitute the latest observation.
            if period not in obs or obs.get(period) is None:
                return _unverifiable("no observation for the claimed period", assertion, series_id)
            target_period = period
        else:
            target_period = sorted(obs)[-1] if obs else None
        if target_period is None or obs.get(target_period) is None:
            return _unverifiable("no observation for the claimed period", assertion, series_id)
        observed = obs[target_period]
        rev_tol = revision_tolerance(conn, series_id, target_period)
        if rev_tol is not None:
            tol = rev_tol
            assumptions.append(f"magnitude tolerance={round(tol, 6)} (from revision spread)")
        else:
            tol = abs(observed) * DEFAULT_REL_TOLERANCE
            assumptions.append(f"magnitude tolerance={round(tol, 6)} (5% fallback)")
        if direction == "exceeds":
            verdict = "supported" if observed > a_value + tol else ("contradicted" if observed < a_value - tol else "unverifiable")
        elif direction == "below":
            verdict = "supported" if observed < a_value - tol else ("contradicted" if observed > a_value + tol else "unverifiable")
        else:  # equals
            verdict = "supported" if abs(observed - a_value) <= tol else "contradicted"
        return analytic_envelope(
            n=1,
            method="claim-vs-data check (magnitude)",
            assumptions=assumptions,
            verdict=verdict,
            subject=getattr(assertion, "subject", None),
            direction=direction,
            claimed_value=a_value,
            observed=interval(observed, observed - tol, observed + tol),
            period=target_period,
            series_id=series_id,
            series_title=series_title,
            match_confidence=match_confidence,
        )

    # --- movement directions (rose / fell / unchanged) ----------------------
    if direction in ("rose", "fell", "unchanged"):
        if period and ".." in period:
            start, end = period.split("..", 1)
            p_from, p_to = start, end
        elif period:
            # A claim about a specific period is unverifiable if that period is
            # absent — do not silently fall back to the latest observations.
            if period not in obs:
                return _unverifiable("no observation for the claimed period", assertion, series_id)
            p_to = period
            p_from = _prior_period(list(obs), period)
        else:
            ordered = sorted(obs)
            p_to = ordered[-1] if ordered else None
            p_from = ordered[-2] if len(ordered) >= 2 else None
        if p_from is None or p_to is None or obs.get(p_from) is None or obs.get(p_to) is None:
            return _unverifiable("not enough observations to measure movement", assertion, series_id)
        delta = obs[p_to] - obs[p_from]
        tol = abs(obs[p_from]) * DEFAULT_REL_TOLERANCE
        assumptions.append(f"movement {p_from}->{p_to}; delta={round(delta, 6)}")
        if direction == "rose":
            verdict = "supported" if delta > tol else ("contradicted" if delta < -tol else "unverifiable")
        elif direction == "fell":
            verdict = "supported" if delta < -tol else ("contradicted" if delta > tol else "unverifiable")
        else:  # unchanged
            verdict = "supported" if abs(delta) <= tol else "contradicted"
        return analytic_envelope(
            n=2,
            method="claim-vs-data check (movement)",
            assumptions=assumptions,
            verdict=verdict,
            subject=getattr(assertion, "subject", None),
            direction=direction,
            delta=delta,
            observed=interval(obs[p_to], min(obs[p_from], obs[p_to]), max(obs[p_from], obs[p_to])),
            period_from=p_from,
            period_to=p_to,
            series_id=series_id,
            series_title=series_title,
            match_confidence=match_confidence,
        )

    return _unverifiable(f"unhandled direction {direction!r}", assertion, series_id)


# --- persistence -----------------------------------------------------------

_CHECKS_DDL = """
CREATE TABLE IF NOT EXISTS claim_data_checks (
    check_id        TEXT PRIMARY KEY,
    claim_id        TEXT,
    subject         TEXT,
    direction       TEXT,
    series_id       TEXT,
    as_of           BIGINT,
    verdict         TEXT NOT NULL,
    match_confidence DOUBLE,
    envelope        JSON NOT NULL,
    created_at      BIGINT
)
"""


def ensure_checks_table(conn) -> None:
    conn.execute(_CHECKS_DDL)


def record_check(
    conn,
    envelope: Dict[str, Any],
    claim_id: Optional[str] = None,
    now_ms: Optional[int] = None,
) -> str:
    """Persist a check envelope into ``claim_data_checks`` (idempotent by
    check_id). Returns the check_id."""
    ensure_checks_table(conn)
    series_id = envelope.get("series_id")
    as_of = None
    for a in envelope.get("assumptions", []):
        if a.startswith("vintage as_of="):
            try:
                as_of = int(a.split("=", 1)[1])
            except ValueError:
                as_of = None
    key_src = f"{claim_id}|{series_id}|{as_of}|{envelope.get('direction')}|{envelope.get('period') or envelope.get('period_to')}"
    check_id = "chk:" + hashlib.md5(key_src.encode()).hexdigest()[:16]
    conn.execute(
        """
        INSERT INTO claim_data_checks
            (check_id, claim_id, subject, direction, series_id, as_of, verdict, match_confidence, envelope, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (check_id) DO UPDATE SET
            verdict = excluded.verdict,
            match_confidence = excluded.match_confidence,
            envelope = excluded.envelope,
            created_at = excluded.created_at
        """,
        [
            check_id,
            claim_id,
            envelope.get("subject"),
            envelope.get("direction"),
            series_id,
            as_of,
            envelope.get("verdict", "unverifiable"),
            envelope.get("match_confidence"),
            json.dumps(envelope),
            now_ms,
        ],
    )
    return check_id


def claim_vs_data(conn, topic: Optional[str] = None, limit: int = 40) -> Dict[str, Any]:
    """Panel payload: recent checks with the detail a claim-vs-data panel needs
    (verdict, observed interval, series, match confidence), parsed from the
    stored envelope."""
    if not _table_exists(conn, "claim_data_checks"):
        return {"checks": [], "count": 0, "note": "no checks recorded"}
    clauses: List[str] = []
    params: List[Any] = []
    if topic:
        clauses.append("LOWER(subject) LIKE ?")
        params.append(f"%{topic.lower()}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    capped = max(1, min(limit, 200))
    rows = conn.execute(
        f"""
        SELECT check_id, claim_id, subject, direction, series_id, verdict, match_confidence, envelope
        FROM claim_data_checks{where}
        ORDER BY created_at DESC NULLS LAST, check_id
        LIMIT {capped}
        """,
        params,
    ).fetchall()
    checks: List[Dict[str, Any]] = []
    for check_id, claim_id, subject, direction, series_id, verdict, mc, envelope in rows:
        env = json.loads(envelope) if isinstance(envelope, str) else (envelope or {})
        checks.append({
            "check_id": check_id,
            "claim_id": claim_id,
            "subject": subject,
            "direction": direction,
            "series_id": series_id,
            "series_title": env.get("series_title"),
            "verdict": verdict,
            "match_confidence": mc,
            "observed": env.get("observed"),
            "period": env.get("period") or env.get("period_to"),
            "assumptions": env.get("assumptions", []),
        })
    return {"checks": checks, "count": len(checks)}


def data_check_ledger(conn, verdict: Optional[str] = None, topic: Optional[str] = None, limit: int = 40) -> Dict[str, Any]:
    """The quantitative wing of the contradiction ledger: recorded checks,
    optionally filtered by verdict (e.g. 'contradicted') or subject topic."""
    if not _table_exists(conn, "claim_data_checks"):
        return {"checks": [], "count": 0, "note": "no checks recorded"}
    clauses: List[str] = []
    params: List[Any] = []
    if verdict:
        clauses.append("verdict = ?")
        params.append(verdict)
    if topic:
        clauses.append("LOWER(subject) LIKE ?")
        params.append(f"%{topic.lower()}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    capped = max(1, min(limit, 200))
    rows = conn.execute(
        f"""
        SELECT check_id, claim_id, subject, direction, series_id, verdict, match_confidence
        FROM claim_data_checks{where}
        ORDER BY created_at DESC NULLS LAST, check_id
        LIMIT {capped}
        """,
        params,
    ).fetchall()
    keys = ["check_id", "claim_id", "subject", "direction", "series_id", "verdict", "match_confidence"]
    checks = [dict(zip(keys, r)) for r in rows]
    return {"checks": checks, "count": len(checks), "verdict": verdict}
