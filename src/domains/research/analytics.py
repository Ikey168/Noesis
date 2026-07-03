"""
Research-domain analytics (MCP rearchitecture plan, R7 / Track N1).

Panel-facing reads over the ingested paper corpus:

* :func:`venue_credibility` generalizes the outlet transparency scoring to
  publication venues: a composite of concept diversity (Shannon entropy of a
  venue's topic mix, mirroring outlet frame diversity), claim-attribution
  rate, and normalized citation impact. Honesty-wrapped, so the venues panel
  shows a defensible score, not a bare number.
* :func:`citation_graph` builds the paper -> paper / paper -> venue citation
  network from document metadata.
* :func:`literature_claims` surfaces SUPPORTS / CONTRADICTS claims scoped to
  papers from the shared claim layer.

Reads a ``documents``-style corpus (``source_type = 'paper'``) and the shared
``argument_claims`` table; both are queried defensively so a corpus without
papers degrades to an empty (still valid) payload rather than an error.

Stdlib-only maths (reuses :mod:`src.analytics`).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from src.analytics.honesty import analytic_envelope, interval

VENUE_METHOD = "composite transparency score generalized to venues"
VENUE_ASSUMPTIONS = [
    "credibility blends concept diversity, attribution rate and citation impact",
    "concept diversity is Shannon entropy of the venue's topic mix (like outlet frame diversity)",
    "needs several papers per venue; sparse venues carry wide intervals",
]

_EPS = 1e-9


def _table_exists(conn, table: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def _entropy(counts: List[int]) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    ent = -sum((c / total) * math.log(c / total + _EPS) for c in counts if c > 0)
    # Normalize to 0..1 by the max entropy for this many categories.
    max_ent = math.log(len([c for c in counts if c > 0]) + _EPS) if len(counts) > 1 else 1.0
    return ent / max_ent if max_ent > _EPS else 0.0


def venue_credibility(conn) -> Dict[str, Any]:
    """Per-venue credibility over the paper corpus (generalized transparency)."""
    if not _table_exists(conn, "documents"):
        return analytic_envelope(
            n=0, method=VENUE_METHOD, assumptions=VENUE_ASSUMPTIONS,
            venues=[], note="no document corpus ingested",
        )
    rows = conn.execute(
        """
        SELECT venue,
               COUNT(*) AS papers,
               AVG(COALESCE(citations, 0)) AS avg_citations,
               MAX(COALESCE(citations, 0)) AS max_citations
        FROM documents
        WHERE source_type = 'paper' AND venue IS NOT NULL
        GROUP BY venue
        ORDER BY papers DESC
        """
    ).fetchall()
    corpus_max = max((r[3] for r in rows), default=0) or 1

    # Attribution rate per venue from the shared claim layer, when present.
    attribution: Dict[str, float] = {}
    if _table_exists(conn, "argument_claims"):
        try:
            arows = conn.execute(
                """
                SELECT d.venue,
                       AVG(CASE WHEN c.attributed THEN 1.0 ELSE 0.0 END)
                FROM argument_claims c
                JOIN documents d ON c.document_id = d.id
                WHERE d.source_type = 'paper' AND d.venue IS NOT NULL
                GROUP BY d.venue
                """
            ).fetchall()
            attribution = {r[0]: float(r[1] or 0.0) for r in arows}
        except Exception:
            attribution = {}

    # Concept-diversity input: paper counts per (venue) topic bucket.
    concept_counts: Dict[str, List[int]] = {}
    try:
        crows = conn.execute(
            "SELECT venue, COALESCE(concept, 'other'), COUNT(*) FROM documents "
            "WHERE source_type = 'paper' AND venue IS NOT NULL GROUP BY 1, 2"
        ).fetchall()
        for venue, _concept, count in crows:
            concept_counts.setdefault(venue, []).append(int(count))
    except Exception:
        concept_counts = {}

    venues = []
    for venue, papers, avg_cit, _max_cit in rows:
        diversity = _entropy(concept_counts.get(venue, [papers]))
        attr = attribution.get(venue, 0.0)
        impact = min(1.0, (avg_cit or 0.0) / (corpus_max or 1))
        composite = (diversity + attr + impact) / 3.0
        # Interval width shrinks with the venue's paper count (more evidence).
        half = 0.25 / math.sqrt(max(1, papers))
        venues.append(
            {
                "venue": venue,
                "papers": int(papers),
                "credibility": interval(
                    composite, max(0.0, composite - half), min(1.0, composite + half)
                ),
                "components": {
                    "concept_diversity": round(diversity, 3),
                    "attribution_rate": round(attr, 3),
                    "citation_impact": round(impact, 3),
                },
            }
        )
    venues.sort(key=lambda v: -v["credibility"]["value"])
    return analytic_envelope(
        n=sum(r[1] for r in rows),
        method=VENUE_METHOD,
        assumptions=VENUE_ASSUMPTIONS,
        venue_count=len(venues),
        venues=venues,
    )


def citation_graph(conn, topic: Optional[str] = None, limit: int = 40) -> Dict[str, Any]:
    """Paper citation network (nodes = papers, edges = citations) from the
    ``documents`` corpus. Papers cite others via metadata ``references``,
    persisted as a ``references`` column (comma-separated ids)."""
    if not _table_exists(conn, "documents"):
        return {"nodes": [], "edges": [], "note": "no document corpus ingested"}
    where = ["source_type = 'paper'"]
    params: List[Any] = []
    if topic:
        where.append("(concept = ? OR title ILIKE ?)")
        params.extend([topic, f"%{topic}%"])
    params.append(limit)
    rows = conn.execute(
        f"SELECT id, title, venue, COALESCE(citations, 0), COALESCE(refs, '') "
        f"FROM documents WHERE {' AND '.join(where)} "
        f"ORDER BY COALESCE(citations, 0) DESC LIMIT ?",
        params,
    ).fetchall()
    ids = {r[0] for r in rows}
    nodes = [
        {"id": r[0], "title": r[1], "venue": r[2], "citations": int(r[3])}
        for r in rows
    ]
    edges = []
    for r in rows:
        for ref in (r[4] or "").split(","):
            ref = ref.strip()
            if ref and ref in ids:
                edges.append({"from": r[0], "to": ref})
    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


def literature_claims(conn, topic: Optional[str] = None, limit: int = 30) -> Dict[str, Any]:
    """SUPPORTS / CONTRADICTS claims scoped to papers, from the claim layer."""
    if not _table_exists(conn, "argument_claims"):
        return {"claims": [], "note": "no claim layer available"}
    where = ["source_type = 'paper'"]
    params: List[Any] = []
    if topic:
        where.append("claim_text ILIKE ?")
        params.append(f"%{topic}%")
    params.append(limit)
    rows = conn.execute(
        f"SELECT claim_id, claim_text, COALESCE(factcheck_verdict, 'unverified'), "
        f"COALESCE(attributed, FALSE) FROM argument_claims "
        f"WHERE {' AND '.join(where)} ORDER BY confidence DESC NULLS LAST LIMIT ?",
        params,
    ).fetchall()
    claims = [
        {
            "claim_id": r[0],
            "text": (r[1] or "")[:180],
            "verdict": r[2],
            "attributed": bool(r[3]),
        }
        for r in rows
    ]
    return {"claims": claims, "count": len(claims), "topic": topic}
