"""
``source_reliability(source)`` - OSINT source vetting (R10 #612).

The outlet transparency machinery generalized to any ``source_type`` (blogs,
papers, transcripts, filings), so a source can be vetted the same way an outlet
is. Composes three signals:

* **transparency**: the latest ``outlet_scores`` composite for the source (the
  same score path outlets use; it keys on ``source``, not on being news), plus
  its attribution / frame-diversity / neutrality components,
* **corroboration hit-rate**: of this source's claims, the fraction that at
  least one independent source supports (from ``claim_evidence``), and
* **correction history**: how often the source's claims were later disputed
  (``factcheck_verdict``), a cheap stand-in for a formal correction log.

The reliability figure is honesty-wrapped and always carries an interval whose
width shrinks with the source's track-record size, so a thinly-evidenced
source reads as uncertain rather than authoritative.

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

import math
from typing import Any, Dict

from src.analytics.conformal import coverage_of_band
from src.analytics.honesty import analytic_envelope, interval
from src.osint import common

METHOD = "outlet transparency scoring generalized to any source_type"
ASSUMPTIONS = [
    "reliability blends transparency, corroboration hit-rate and a clean-record rate",
    "transparency reuses the outlet composite score, which keys on source not on being news",
    "corroboration hit-rate needs claims with evidence; sparse sources carry wide intervals",
    "correction history approximated by disputed fact-check verdicts on the source's claims",
]


def _track_record(conn, source: str) -> Dict[str, Any]:
    if not common.table_exists(conn, "news_articles"):
        return {"documents": 0, "last_seen": None}
    row = conn.execute(
        "SELECT COUNT(*), MAX(publish_date) FROM news_articles WHERE source = ?",
        [source],
    ).fetchone()
    return {
        "documents": int(row[0] or 0),
        "last_seen": str(row[1]) if row and row[1] is not None else None,
    }


def _claim_record(conn, source: str) -> Dict[str, Any]:
    """Corroboration hit-rate and disputed-rate over the source's claims."""
    if not (
        common.table_exists(conn, "argument_claims")
        and common.table_exists(conn, "news_articles")
    ):
        return {"claims": 0, "corroborated": 0, "disputed": 0}
    claim_rows = conn.execute(
        """
        SELECT c.claim_id, COALESCE(c.factcheck_verdict, '')
        FROM argument_claims c
        JOIN news_articles a ON c.document_id = a.id
        WHERE a.source = ?
        """,
        [source],
    ).fetchall()
    claim_ids = [r[0] for r in claim_rows]
    disputed = sum(1 for r in claim_rows if r[1].lower() in ("disputed", "false", "refuted"))
    corroborated = 0
    if claim_ids and common.table_exists(conn, "claim_evidence"):
        ph = ", ".join("?" for _ in claim_ids)
        supported = {
            r[0]
            for r in conn.execute(
                f"SELECT DISTINCT claim_id FROM claim_evidence "
                f"WHERE claim_id IN ({ph}) AND lower(relation) = 'supports'",
                claim_ids,
            ).fetchall()
        }
        corroborated = len(supported)
    return {"claims": len(claim_ids), "corroborated": corroborated, "disputed": disputed}


def source_reliability(conn, source: str) -> Dict[str, Any]:
    """Reliability card for any source: transparency, corroboration hit-rate,
    correction history, and a track-record-weighted reliability interval."""
    track = _track_record(conn, source)
    claims = _claim_record(conn, source)

    # Transparency component (the outlet composite), if the source has been scored.
    components: Dict[str, Any] = {}
    transparency = None
    if common.table_exists(conn, "outlet_scores"):
        row = conn.execute(
            """
            SELECT composite_score, attribution_rate, frame_diversity, stance_neutrality
            FROM outlet_scores o
            WHERE source = ?
              AND score_date = (SELECT MAX(score_date) FROM outlet_scores i WHERE i.source = o.source)
            """,
            [source],
        ).fetchone()
        if row is not None:
            transparency = float(row[0]) if row[0] is not None else None
            components = {
                "transparency": round(transparency, 3) if transparency is not None else None,
                "attribution_rate": round(float(row[1]), 3) if row[1] is not None else None,
                "frame_diversity": round(float(row[2]), 3) if row[2] is not None else None,
                "stance_neutrality": round(float(row[3]), 3) if row[3] is not None else None,
            }

    total_claims = claims["claims"]
    hit_rate = (claims["corroborated"] / total_claims) if total_claims else 0.0
    clean_rate = (1.0 - claims["disputed"] / total_claims) if total_claims else 1.0
    components["corroboration_hit_rate"] = round(hit_rate, 3)
    components["clean_record_rate"] = round(clean_rate, 3)

    parts = [p for p in (transparency, hit_rate, clean_rate) if p is not None]
    composite = sum(parts) / len(parts) if parts else common.DEFAULT_CREDIBILITY

    # M7.2: the band width stays evidence-driven (a sparse source carries a wider
    # interval), and the reported coverage is the *measured* fraction of the
    # components that fall inside it — a documented coverage rate, not a claimed
    # one. A band too tight for disagreeing components reads back as low coverage.
    evidence = max(track["documents"], total_claims, 1)
    half = 0.3 / math.sqrt(evidence)
    level = 0.9
    component_devs = [p - composite for p in parts]
    coverage = round(coverage_of_band(component_devs, half), 4)

    return analytic_envelope(
        n=evidence,
        method=METHOD,
        assumptions=ASSUMPTIONS,
        source=source,
        found=track["documents"] > 0 or total_claims > 0,
        reliability=interval(
            composite, max(0.0, composite - half), min(1.0, composite + half), level
        ),
        coverage=coverage,
        calibration_n=len(parts),
        components=components,
        track_record={
            "documents": track["documents"],
            "claims": total_claims,
            "last_seen": track["last_seen"],
        },
        corroboration={"corroborated_claims": claims["corroborated"], "total_claims": total_claims},
        corrections={"disputed_claims": claims["disputed"]},
        scored_as_outlet=transparency is not None,
    )
