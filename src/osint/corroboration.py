"""
``corroborate(claim_id)`` - the core OSINT primitive (R10 #611).

How many *independent* sources support or contradict a claim, and how good are
those sources. Pure composition of three layers Noesis already builds:

* the claim itself (``argument_claims``) and its carrying source,
* the RAG evidence links (``claim_evidence``: SUPPORTS / CONTRADICTS to other
  documents), and
* the semantic conflict edges (``claim_conflicts``: another claim contradicts
  this one),

each source weighted by its credibility (``outlet_scores.composite_score``).

The output is deliberately **not** a single confidence number: it is the count
of independent supporting and contradicting sources, each with its own
credibility, plus credibility-weighted tallies. A claim with only its own
source is flagged ``single_sourced`` so the panel can mark it clearly rather
than implying corroboration that does not exist.

Honesty-wrapped (``n`` = number of independent corroborating sources).
Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.analytics.conformal import calibrated_envelope_fields, conformal_interval
from src.analytics.honesty import analytic_envelope, interval
from src.osint import common

METHOD = "independent-source corroboration over RAG evidence and conflict edges"
ASSUMPTIONS = [
    "independence is by distinct source (outlet); two claims from one source count once",
    "source credibility is the latest outlet transparency composite (0.5 when unscored)",
    "absence of evidence is not evidence: a single-sourced claim is flagged, not scored",
    "reads only already-ingested public documents; no crawling or targeting",
]


def _support_contradict_from_evidence(
    conn, claim_id: str
) -> List[Dict[str, Any]]:
    """Evidence links for this claim: each references an evidence document,
    whose source and credibility we resolve."""
    if not common.table_exists(conn, "claim_evidence"):
        return []
    has_articles = common.table_exists(conn, "news_articles")
    if has_articles:
        rows = conn.execute(
            """
            SELECT e.relation, e.evidence_source_type, e.similarity_score,
                   a.source
            FROM claim_evidence e
            LEFT JOIN news_articles a ON e.evidence_document_id = a.id
            WHERE e.claim_id = ?
              AND lower(e.relation) IN ('supports', 'contradicts')
            """,
            [claim_id],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT relation, evidence_source_type, similarity_score, NULL "
            "FROM claim_evidence WHERE claim_id = ? "
            "AND lower(relation) IN ('supports', 'contradicts')",
            [claim_id],
        ).fetchall()
    out = []
    for relation, source_type, sim, source in rows:
        out.append(
            {
                "relation": relation.lower(),
                "source": source or source_type or "unknown",
                "source_type": source_type,
                "similarity": float(sim) if sim is not None else None,
                "via": "evidence",
            }
        )
    return out


def _contradict_from_conflicts(conn, claim_id: str) -> List[Dict[str, Any]]:
    """Semantic conflict edges naming this claim: the other claim contradicts
    it, so its source is a contradicting source."""
    if not common.table_exists(conn, "claim_conflicts"):
        return []
    rows = conn.execute(
        """
        SELECT CASE WHEN claim_id_a = ? THEN claim_id_b ELSE claim_id_a END
        FROM claim_conflicts
        WHERE claim_id_a = ? OR claim_id_b = ?
        """,
        [claim_id, claim_id, claim_id],
    ).fetchall()
    other_ids = [r[0] for r in rows if r[0]]
    if not other_ids:
        return []
    src_map = common.claim_sources(conn, other_ids)
    out = []
    for oid in other_ids:
        info = src_map.get(oid, {})
        out.append(
            {
                "relation": "contradicts",
                "source": info.get("source", "unknown"),
                "source_type": info.get("source_type"),
                "similarity": None,
                "via": "conflict",
                "claim_id": oid,
            }
        )
    return out


def corroborate(conn, claim_id: str) -> Dict[str, Any]:
    """Independent-source corroboration for one claim.

    Returns the claim, its own source, the supporting and contradicting
    sources (each with credibility), the independent-source counts and
    credibility-weighted tallies, and ``single_sourced`` when nothing
    independent corroborates it.
    """
    if not common.table_exists(conn, "argument_claims"):
        return {"error": "no claim layer available"}
    row = conn.execute(
        "SELECT claim_id, claim_text, document_id, source_type, "
        "COALESCE(factcheck_verdict, 'unverified') "
        "FROM argument_claims WHERE claim_id = ?",
        [claim_id],
    ).fetchone()
    if row is None:
        return {"error": f"claim {claim_id!r} not found"}

    own = common.claim_sources(conn, [claim_id]).get(claim_id, {})
    own_source = own.get("source", row[3] or "unknown")

    entries = _support_contradict_from_evidence(conn, claim_id)
    entries += _contradict_from_conflicts(conn, claim_id)

    # A source only counts as independent corroboration if it is not the
    # claim's own carrying source.
    support = [
        e for e in entries if e["relation"] == "supports" and e["source"] != own_source
    ]
    contradict = [
        e for e in entries if e["relation"] == "contradicts" and e["source"] != own_source
    ]

    cred = common.source_credibility(
        conn, [e["source"] for e in support + contradict] + [own_source]
    )
    for e in support + contradict:
        e["credibility"] = cred.get(e["source"])

    support_sources = common.dedupe_sources(support)
    contradict_sources = common.dedupe_sources(contradict)

    def _weighted(sources: List[str]) -> float:
        return round(
            sum(common.credibility_or_default(cred.get(s)) for s in sources), 3
        )

    independent_total = len(set(support_sources) | set(contradict_sources))
    single_sourced = independent_total == 0

    # M7.2: a *calibrated* corroboration-strength range instead of a bare
    # weighted number. The supporting sources' credibilities are the calibration
    # sample; the conformal band over their spread covers them at the target
    # level, and the measured coverage ships alongside. No support -> no range.
    level = 0.9
    support_creds = [common.credibility_or_default(cred.get(s)) for s in support_sources]
    if support_creds:
        mean_cred = sum(support_creds) / len(support_creds)
        cred_residuals = (
            [c - mean_cred for c in support_creds] if len(support_creds) >= 2 else [0.25]
        )
        band = conformal_interval(mean_cred, cred_residuals, level)
        support_credibility = interval(
            mean_cred, max(0.0, band["lo"]), min(1.0, band["hi"]), level
        )
        support_calib = calibrated_envelope_fields(cred_residuals, level)
    else:
        support_credibility = None
        support_calib = {"coverage": None, "calibration_n": 0}

    return analytic_envelope(
        n=independent_total,
        method=METHOD,
        assumptions=ASSUMPTIONS,
        claim={
            "claim_id": row[0],
            "text": (row[1] or "")[:280],
            "source": own_source,
            "source_type": row[3],
            "verdict": row[4],
            "credibility": cred.get(own_source),
        },
        support=_collapse(support, cred),
        contradict=_collapse(contradict, cred),
        independent_support_count=len(support_sources),
        independent_contradict_count=len(contradict_sources),
        weighted_support=_weighted(support_sources),
        weighted_contradict=_weighted(contradict_sources),
        single_sourced=single_sourced,
        support_credibility=support_credibility,
        support_coverage=support_calib["coverage"],
        support_calibration_n=support_calib["calibration_n"],
    )


def _collapse(entries: List[Dict[str, Any]], cred: Dict[str, Optional[float]]) -> List[Dict[str, Any]]:
    """One row per distinct source, keeping the strongest similarity seen and
    how the corroboration was found."""
    by_source: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        cur = by_source.get(e["source"])
        if cur is None:
            by_source[e["source"]] = {
                "source": e["source"],
                "source_type": e.get("source_type"),
                "credibility": cred.get(e["source"]),
                "via": e["via"],
                "similarity": e.get("similarity"),
            }
        else:
            if (e.get("similarity") or 0) > (cur.get("similarity") or 0):
                cur["similarity"] = e.get("similarity")
    # Highest-credibility sources first (None sorts last).
    return sorted(
        by_source.values(),
        key=lambda r: (r["credibility"] is not None, r["credibility"] or 0),
        reverse=True,
    )
