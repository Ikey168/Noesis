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
import json
from typing import Any, Dict, List, Optional

from src.analytics.honesty import analytic_envelope, interval

VENUE_METHOD = "composite transparency score generalized to venues"
VENUE_ASSUMPTIONS = [
    "credibility blends concept diversity, attribution rate and citation impact",
    "concept diversity is Shannon entropy of the venue's topic mix (like outlet frame diversity)",
    "needs several papers per venue; sparse venues carry wide intervals",
]

def _table_exists(conn, table: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def _paper_rows(conn) -> List[Dict[str, Any]]:
    """Read papers from either the legacy flat table or document-ingest-v1.

    Missing citation counts stay ``None`` (absence is not zero impact), and a
    missing concept stays ``None`` so callers can report it as missing data.
    """
    if not _table_exists(conn, "documents"):
        return []
    available = {row[1] for row in conn.execute("PRAGMA table_info('documents')").fetchall()}
    selected = [name for name in (
        "id", "document_id", "title", "venue", "concept", "citations", "refs", "metadata",
    ) if name in available]
    if not selected:
        return []
    rows = conn.execute(
        f"SELECT {', '.join(selected)} FROM documents WHERE source_type = 'paper'"
    ).fetchall()
    papers = []
    for values in rows:
        row = dict(zip(selected, values))
        raw_metadata = row.get("metadata")
        try:
            metadata = json.loads(raw_metadata) if isinstance(raw_metadata, str) else raw_metadata
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        raw_refs = row.get("refs") or metadata.get("refs") or metadata.get("reference_ids") or metadata.get("references") or []
        refs = raw_refs.split(",") if isinstance(raw_refs, str) else raw_refs
        if not isinstance(refs, list):
            refs = []
        citations = row.get("citations")
        if citations is None:
            citations = metadata.get("citations", metadata.get("cited_by"))
        if isinstance(citations, bool) or not isinstance(citations, int) or citations < 0:
            citations = None
        venue = row.get("venue") or metadata.get("venue") or metadata.get("journal") or metadata.get("booktitle")
        if isinstance(venue, list):
            venue = next((item for item in venue if isinstance(item, str) and item.strip()), None)
        if not isinstance(venue, str) or not venue.strip():
            venue = None
        papers.append({
            "id": row.get("id") or row.get("document_id"),
            "title": row.get("title") or "",
            "venue": venue.strip() if venue else None,
            "concept": row.get("concept") or metadata.get("concept") or metadata.get("primary_category") or None,
            "citations": citations,
            "refs": [str(ref).strip() for ref in refs if ref],
            "doi": metadata.get("doi"),
            "external_id": metadata.get("external_id"),
        })
    return papers


def _entropy(counts: List[int]) -> float:
    total = sum(counts)
    if total <= 0:
        return 0.0
    active = [c for c in counts if c > 0]
    if len(active) < 2:
        return 0.0
    ent = -sum((c / total) * math.log(c / total) for c in active)
    # Normalize to 0..1 by the maximum entropy for active categories.
    return min(1.0, max(0.0, ent / math.log(len(active))))


def venue_credibility(conn) -> Dict[str, Any]:
    """Per-venue credibility over the paper corpus (generalized transparency)."""
    if not _table_exists(conn, "documents"):
        return analytic_envelope(
            n=0, method=VENUE_METHOD, assumptions=VENUE_ASSUMPTIONS,
            venues=[], note="no document corpus ingested",
        )
    papers = _paper_rows(conn)
    by_venue: Dict[str, List[Dict[str, Any]]] = {}
    for paper in papers:
        if paper["venue"]:
            by_venue.setdefault(paper["venue"], []).append(paper)
    corpus_max = max((paper["citations"] for paper in papers if paper["citations"] is not None), default=0) or 1

    # Attribution rate per venue from the shared claim layer, when present.
    attribution: Dict[str, float] = {}
    if papers and _table_exists(conn, "argument_claims"):
        try:
            paper_venues = {paper["id"]: paper["venue"] for paper in papers if paper["venue"]}
            claim_counts: Dict[str, List[int]] = {}
            for document_id, attributed in conn.execute(
                "SELECT document_id, attributed FROM argument_claims WHERE source_type = 'paper'"
            ).fetchall():
                venue = paper_venues.get(document_id)
                if venue:
                    bucket = claim_counts.setdefault(venue, [0, 0])
                    bucket[0] += int(bool(attributed))
                    bucket[1] += 1
            attribution = {venue: good / total for venue, (good, total) in claim_counts.items()}
        except Exception:
            attribution = {}

    # Concept-diversity input: paper counts per (venue) topic bucket.
    concept_counts: Dict[str, List[int]] = {}
    for venue, items in by_venue.items():
        counts: Dict[str, int] = {}
        for paper in items:
            if paper["concept"]:
                key = str(paper["concept"])
                counts[key] = counts.get(key, 0) + 1
        concept_counts[venue] = list(counts.values())

    venues = []
    for venue, items in by_venue.items():
        count = len(items)
        citations = [item["citations"] for item in items if item["citations"] is not None]
        if not concept_counts[venue] or not citations or venue not in attribution:
            venues.append({
                "venue": venue, "papers": count, "status": "insufficient_data",
                "missing": [name for name, ready in (
                    ("concepts", bool(concept_counts[venue])),
                    ("citation_counts", bool(citations)),
                    ("claim_attribution", venue in attribution),
                ) if not ready],
            })
            continue
        diversity = _entropy(concept_counts[venue])
        attr = attribution.get(venue, 0.0)
        impact = min(1.0, (sum(citations) / len(citations)) / corpus_max)
        composite = (diversity + attr + impact) / 3.0
        # Interval width shrinks with the venue's paper count (more evidence).
        half = 0.25 / math.sqrt(max(1, count))
        venues.append(
            {
                "venue": venue,
                "papers": count,
                "status": "scored",
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
    venues.sort(key=lambda v: (v["status"] != "scored", -v.get("credibility", {}).get("value", 0), v["venue"]))
    return analytic_envelope(
        n=sum(len(items) for items in by_venue.values()),
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
    all_papers = _paper_rows(conn)
    aliases = {}
    for paper in all_papers:
        for alias in (paper["id"], paper.get("doi"), paper.get("external_id")):
            if alias:
                aliases[str(alias).lower().removeprefix("https://doi.org/")] = paper["id"]
    rows = [
        paper for paper in all_papers
        if not topic or topic.casefold() in paper["title"].casefold()
        or topic.casefold() == str(paper["concept"] or "").casefold()
    ]
    rows.sort(key=lambda paper: (-(paper["citations"] or 0), paper["id"]))
    rows = rows[:limit]
    ids = {row["id"] for row in rows}
    nodes = [
        {"id": row["id"], "title": row["title"], "venue": row["venue"], "citations": row["citations"]}
        for row in rows
    ]
    edges = []
    for row in rows:
        for ref in row["refs"]:
            target = aliases.get(str(ref).lower().removeprefix("https://doi.org/"))
            if target in ids and target != row["id"]:
                edges.append({"from": row["id"], "to": target})
    result = {
        "nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges),
        "reference_data_count": sum(bool(row["refs"]) for row in rows),
        "citation_count_data_count": sum(row["citations"] is not None for row in rows),
    }
    if rows and not result["reference_data_count"]:
        result["note"] = "no reference lists were supplied for these papers; zero edges is not evidence of no influence"
    return result


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
    result = {"claims": claims, "count": len(claims), "topic": topic}
    if not claims:
        result["note"] = "no extracted paper claims in the selected corpus; no disagreement assessment is available"
    return result


def _doi(value: Any) -> str:
    text = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        text = text.removeprefix(prefix)
    return text


def papers_by_identifier(conn, *, doi: Optional[str] = None) -> List[Dict[str, Any]]:
    """Science literature records (``source_type = 'paper'``) whose metadata states this DOI.

    Exact, case-insensitive DOI match only (``https://doi.org/`` and ``doi:``
    prefixes are ignored); nothing is guessed from titles. Other packs use it to
    resolve a cited reference to a literature record.
    """
    if not doi or not _table_exists(conn, "documents"):
        return []
    available = {row[1] for row in conn.execute("PRAGMA table_info('documents')").fetchall()}
    if not {"document_id", "title", "metadata", "source_type"} <= available:
        return []
    wanted, found = _doi(doi), []
    for document_id, title, raw in conn.execute(
        "SELECT document_id, title, metadata FROM documents WHERE source_type = 'paper' ORDER BY document_id"
    ).fetchall():
        try:
            metadata = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except (TypeError, ValueError):
            continue
        stated = _doi((metadata or {}).get("doi")) if isinstance(metadata, dict) else ""
        if stated and stated == wanted:
            found.append({"document_id": document_id, "title": title or "", "doi": stated})
    return found
