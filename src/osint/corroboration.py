"""
``corroborate(claim_id)`` - the core OSINT primitive (R10 #611).

How many probable reporting origins support or contradict a claim, and how
good their publications are. Pure composition of three layers Noesis already
builds:

* the claim itself (``argument_claims``) and its carrying source,
* the RAG evidence links (``claim_evidence``: SUPPORTS / CONTRADICTS to other
  documents), and
* the semantic conflict edges (``claim_conflicts``: another claim contradicts
  this one), and
* the KB claim-to-claim NLI links (``claim_links``): another outlet's claim
  that entails this one (``supports`` toward it, or a mutual ``duplicate``)
  corroborates it, a ``contradicts`` link contradicts it, and same-source
  ``supersedes``/``corrects``/``retracts`` transitions are reported as the
  claim's own revision history rather than as independent sources,

each source weighted by its credibility (``outlet_scores.composite_score``).

The output is deliberately **not** a single confidence number: it separates
publication counts from probable reporting origins, exposes unresolved
lineage, and retains per-publication credibility plus weighted tallies. A
claim with only its own source is flagged ``single_sourced`` so the panel can
mark it clearly rather than implying corroboration that does not exist.

OX01 (#2041): the output also carries ``credibility_grade``, an Admiralty-style
information-credibility digit 1–6 for the claim, beside the carrying source's
``source_reliability_grade`` (A–F). They are two independent axes, reported
side by side with their derivations and never fused into one number.

Honesty-wrapped (``n`` = number of probable corroborating origins, or distinct
sources under the explicitly named fallback).
Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.analytics.conformal import calibrated_envelope_fields, conformal_interval
from src.analytics.honesty import analytic_envelope, interval
from src.osint import common

METHOD = "origin-aware corroboration over RAG evidence and conflict edges"
ASSUMPTIONS = [
    "probable reporting origins replace outlet counts when lineage is available",
    "warehouses without lineage fall back explicitly to distinct normalized sources",
    "source credibility is the latest outlet transparency composite (0.5 when unscored)",
    "absence of evidence is not evidence: a single-sourced claim is flagged, not scored",
    "reads only already-ingested public documents; no crawling or targeting",
    "a claim link counts as support only in its entailment direction (other -> this claim) or as a mutual duplicate",
    "one document reached through several layers counts once per relation",
    "same-source corrections and retractions are revision history, never independent corroboration",
]


CREDIBILITY_LABELS = {
    1: "confirmed by other sources",
    2: "probably true",
    3: "possibly true",
    4: "doubtful",
    5: "improbable",
    6: "truth cannot be judged",
}
CREDIBILITY_METHOD = (
    "admiralty-information-credibility-v1: rule table over probable-origin counts "
    "and credibility-weighted support vs. contradiction"
)
CREDIBILITY_ASSUMPTIONS = [
    "6 (cannot be judged) when lineage is unresolved or nothing independent bears on the claim",
    "5 when the only independent origins contradict it, or it is single-sourced and fact-checked as disputed",
    "1 needs at least two independent supporting origins and no contradicting origin",
    "2 needs at least two supporting origins and weighted support at least twice weighted contradiction",
    "3 is one uncontradicted supporting origin, or support that outweighs contradiction by less than 2x",
    "4 when weighted contradiction outweighs weighted support",
    "grades the claim only; source reliability (A-F) is a separate, independent axis",
]
AXES_NOTE = (
    "source reliability (A-F) and information credibility (1-6) are independent axes: "
    "a reliable source can carry an unconfirmed claim and vice versa; they are never "
    "combined into one confidence"
)
_DISPUTED_VERDICTS = ("disputed", "false", "refuted")


def credibility_grade(
    *,
    support_origins: int,
    contradict_origins: int,
    weighted_support: float,
    weighted_contradict: float,
    single_sourced: bool,
    unresolved: int,
    verdict: Optional[str] = None,
) -> Dict[str, Any]:
    """Admiralty information-credibility digit (1–6) with its derivation."""
    s, c = int(support_origins), int(contradict_origins)
    ws, wc = float(weighted_support), float(weighted_contradict)
    lineage_unresolved = unresolved > 0 and (s + c) == 0
    disputed = (verdict or "").lower() in _DISPUTED_VERDICTS
    if lineage_unresolved:
        grade, rule = 6, "lineage unresolved: no resolved independent origin"
    elif single_sourced:
        if disputed:
            grade, rule = 5, "single-sourced and fact-checked as disputed"
        else:
            grade, rule = 6, "single-sourced: nothing independent bears on the claim"
    elif s == 0:
        grade, rule = 5, f"no supporting origin; {c} contradicting origin(s)"
    elif s >= 2 and c == 0:
        grade, rule = 1, f"{s} independent supporting origins, none contradicting"
    elif s >= 2 and ws >= 2 * wc:
        grade, rule = 2, f"{s} supporting origins; weighted support {ws} >= 2 x {wc}"
    elif c == 0 or ws >= wc:
        grade, rule = 3, f"{s} supporting / {c} contradicting origin(s); support {ws} >= contradiction {wc}"
    else:
        grade, rule = 4, f"weighted contradiction {wc} > weighted support {ws}"
    return {
        "axis": "information_credibility",
        "scale": "1-6",
        "grade": grade,
        "label": CREDIBILITY_LABELS[grade],
        "derivation": {
            "inputs": {
                "probable_origin_support_count": s,
                "probable_origin_contradict_count": c,
                "weighted_support": round(ws, 3),
                "weighted_contradict": round(wc, 3),
                "single_sourced": bool(single_sourced),
                "unresolved_count": int(unresolved),
                "lineage_unresolved": lineage_unresolved,
                "factcheck_verdict": verdict,
            },
            "rule_applied": rule,
        },
        "n": s + c,
        "method": CREDIBILITY_METHOD,
        "assumptions": list(CREDIBILITY_ASSUMPTIONS),
        "independent_of": "source_reliability",
    }


def _support_contradict_from_evidence(
    conn, claim_id: str
) -> List[Dict[str, Any]]:
    """Evidence links for this claim: each references an evidence document,
    whose source and credibility we resolve."""
    if not common.table_exists(conn, "claim_evidence"):
        return []
    citation_tbl = common.citation_table(conn)
    if citation_tbl:
        rows = conn.execute(
            f"""
            SELECT e.relation, e.evidence_source_type, e.similarity_score,
                   a.source, e.evidence_document_id
            FROM claim_evidence e
            LEFT JOIN {citation_tbl} a ON e.evidence_document_id = a.id
            WHERE e.claim_id = ?
              AND lower(e.relation) IN ('supports', 'contradicts')
            """,
            [claim_id],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT relation, evidence_source_type, similarity_score, NULL,"
            " evidence_document_id "
            "FROM claim_evidence WHERE claim_id = ? "
            "AND lower(relation) IN ('supports', 'contradicts')",
            [claim_id],
        ).fetchall()
    out = []
    for relation, source_type, sim, source, document_id in rows:
        out.append(
            {
                "relation": relation.lower(),
                "source": source or source_type or "unknown",
                "source_type": source_type,
                "similarity": float(sim) if sim is not None else None,
                "via": "evidence",
                "document_id": document_id,
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
        SELECT CASE WHEN claim_id_a = ? THEN claim_id_b ELSE claim_id_a END,
               similarity_score, conflict_type
        FROM claim_conflicts
        WHERE claim_id_a = ? OR claim_id_b = ?
        """,
        [claim_id, claim_id, claim_id],
    ).fetchall()
    edges = {r[0]: (r[1], r[2]) for r in rows if r[0] and r[0] != claim_id}
    other_ids = list(edges)
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
                "similarity": float(edges[oid][0]) if edges[oid][0] is not None else None,
                "conflict_type": edges[oid][1],
                "via": "conflict",
                "claim_id": oid,
                "document_id": info.get("document_id"),
            }
        )
    return out


_REVISION_RELATIONS = ("supersedes", "corrects", "retracts")


def _from_claim_links(conn, claim_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Claim-to-claim NLI links naming this claim.

    Returns ``(entries, revisions)``. ``supports`` is directed premise ->
    hypothesis, so only a link *into* this claim corroborates it; a link out of
    it (this claim entails a weaker one) says nothing about this claim's truth.
    ``duplicate`` is mutual entailment and ``contradicts`` is symmetric.
    Temporal transitions are newer -> older from the same source.
    """
    if not common.table_exists(conn, "claim_links"):
        return [], []
    rows = conn.execute(
        """
        SELECT claim_a, claim_b, relation, confidence, score
        FROM claim_links
        WHERE (claim_a = ? OR claim_b = ?) AND claim_a <> claim_b
        """,
        [claim_id, claim_id],
    ).fetchall()
    other_ids = list({b if a == claim_id else a for a, b, *_ in rows})
    src_map = common.claim_sources(conn, other_ids)
    entries: list[dict[str, Any]] = []
    revisions: list[dict[str, Any]] = []
    for claim_a, claim_b, relation, confidence, score in rows:
        relation = (relation or "").lower()
        other = claim_b if claim_a == claim_id else claim_a
        if relation in _REVISION_RELATIONS:
            revisions.append(
                {
                    "relation": relation,
                    # newer -> older: when this claim is claim_b, it was revised.
                    "this_claim_is": "older" if claim_b == claim_id else "newer",
                    "claim_id": other,
                    "confidence": float(confidence) if confidence is not None else None,
                }
            )
            continue
        if relation == "supports" and claim_b != claim_id:
            continue  # this claim entails the other; not corroboration of it
        if relation not in ("supports", "duplicate", "contradicts"):
            continue
        info = src_map.get(other, {})
        entries.append(
            {
                "relation": "contradicts" if relation == "contradicts" else "supports",
                "source": info.get("source", "unknown"),
                "source_type": info.get("source_type"),
                "similarity": float(score) if score is not None else None,
                "link_relation": relation,
                "link_confidence": float(confidence) if confidence is not None else None,
                "via": "claim_link",
                "claim_id": other,
                "document_id": info.get("document_id"),
            }
        )
    revisions.sort(key=lambda r: (r["this_claim_is"], r["relation"], r["claim_id"]))
    return entries, revisions


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per (relation, document) — the same publication found via
    evidence, a conflict edge and a claim link is still one publication. The
    strongest similarity is kept and every layer that found it is listed."""
    out: dict[tuple, dict[str, Any]] = {}
    for entry in entries:
        document = entry.get("document_id")
        key = (
            entry["relation"],
            document if document else ("claim", entry.get("claim_id"), entry["source"], entry["via"]),
        )
        current = out.get(key)
        if current is None:
            out[key] = {**entry, "found_via": [entry["via"]]}
            continue
        if entry["via"] not in current["found_via"]:
            current["found_via"].append(entry["via"])
        if (entry.get("similarity") or 0) > (current.get("similarity") or 0):
            current["similarity"] = entry.get("similarity")
    return list(out.values())


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
    link_entries, revisions = _from_claim_links(conn, claim_id)
    entries = _dedupe_entries(entries + link_entries)

    # A source only counts as independent corroboration if it is not the
    # claim's own carrying source.
    support = [
        e for e in entries if e["relation"] == "supports" and e["source"] != own_source
    ]
    contradict = [
        e for e in entries if e["relation"] == "contradicts" and e["source"] != own_source
    ]
    # A source on both sides is contested within itself; keep it on both
    # sides but name it, rather than silently netting it out.
    mixed_sources = sorted(
        {e["source"] for e in support} & {e["source"] for e in contradict}
    )

    cred = common.source_credibility(
        conn, [e["source"] for e in support + contradict] + [own_source]
    )
    for e in support + contradict:
        e["credibility"] = cred.get(e["source"])

    support_sources = common.dedupe_sources(support)
    contradict_sources = common.dedupe_sources(contradict)

    from src.osint.independence import origin_summary

    support_independence = origin_summary(
        conn,
        [entry.get("document_id") for entry in support],
        sources=[entry.get("source") for entry in support],
    )
    contradict_independence = origin_summary(
        conn,
        [entry.get("document_id") for entry in contradict],
        sources=[entry.get("source") for entry in contradict],
    )

    def _weighted(sources: List[str]) -> float:
        return round(
            sum(common.credibility_or_default(cred.get(s)) for s in sources), 3
        )

    independent_total = (
        support_independence["independent_source_count"]
        + contradict_independence["independent_source_count"]
    )
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

    weighted_support = _weighted(support_sources)
    weighted_contradict = _weighted(contradict_sources)
    info_grade = credibility_grade(
        support_origins=support_independence["probable_origin_count"],
        contradict_origins=contradict_independence["probable_origin_count"],
        weighted_support=weighted_support,
        weighted_contradict=weighted_contradict,
        single_sourced=single_sourced,
        unresolved=support_independence["unresolved_count"]
        + contradict_independence["unresolved_count"],
        verdict=row[4],
    )
    from src.osint.reliability import source_reliability

    own_card = source_reliability(conn, own_source)
    source_grade = own_card.get("reliability_grade")

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
        independent_support_count=support_independence["independent_source_count"],
        independent_contradict_count=contradict_independence["independent_source_count"],
        publication_support_count=support_independence["publication_count"],
        publication_contradict_count=contradict_independence["publication_count"],
        probable_origin_support_count=support_independence["probable_origin_count"],
        probable_origin_contradict_count=contradict_independence["probable_origin_count"],
        unresolved_support_count=support_independence["unresolved_count"],
        unresolved_contradict_count=contradict_independence["unresolved_count"],
        independence={
            "n": independent_total,
            "support": support_independence,
            "contradict": contradict_independence,
            "method": support_independence["method"],
            "assumptions": support_independence["assumptions"],
        },
        weighted_support=weighted_support,
        weighted_contradict=weighted_contradict,
        single_sourced=single_sourced,
        mixed_sources=mixed_sources,
        revisions=revisions,
        retracted_by_source=any(
            r["relation"] == "retracts" and r["this_claim_is"] == "older" for r in revisions
        ),
        corrected_by_source=any(
            r["relation"] == "corrects" and r["this_claim_is"] == "older" for r in revisions
        ),
        source_reliability_grade=source_grade,
        credibility_grade=info_grade,
        grading={
            "source_reliability": source_grade["grade"] if source_grade else None,
            "information_credibility": info_grade["grade"],
            "independent_axes": True,
            "note": AXES_NOTE,
        },
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
                "found_via": list(e.get("found_via") or [e["via"]]),
                "publication_count": 1,
                "similarity": e.get("similarity"),
            }
        else:
            cur["publication_count"] += 1
            for via in e.get("found_via") or [e["via"]]:
                if via not in cur["found_via"]:
                    cur["found_via"].append(via)
            if (e.get("similarity") or 0) > (cur.get("similarity") or 0):
                cur["similarity"] = e.get("similarity")
    # Highest-credibility sources first (None sorts last), stable by name.
    return sorted(
        by_source.values(),
        key=lambda r: (r["credibility"] is None, -(r["credibility"] or 0), r["source"]),
    )
