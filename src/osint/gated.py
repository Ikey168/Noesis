"""
Review-gated OSINT tools (issue #639 item 3 / #618 review gate).

``geolocate_claims`` and ``narrative_coordination`` are the two most abusable
OSINT primitives. They are implemented here under strict, in-code purpose
limitation, and they stay behind the ``NOESIS_OSINT_GATED_TOOLS`` flag (off by
default) so they are absent from the served surface until a human deliberately
enables them after the review in ``docs/security/osint-review-gate.md`` and the abuse
analysis in ``docs/security/osint-abuse-analysis.md``. The flag is the gate's
enforcement; a test asserts the tools are absent while it is off.

Purpose limitation, enforced not just documented:

* ``geolocate_claims`` resolves only **event geography** from document content
  (where an event is reported to have happened). It refuses to geolocate a
  person, and it never emits a person's location, only an event's, always
  cited and always flagged unverified.
* ``narrative_coordination`` flags **cohorts for human review**. It never
  accuses: it reports groups of sources publishing near-identical claims with
  a calibrated caveat that similarity is often coincidental (shared wire copy,
  a common event), and every cohort is marked "warrants review", not
  "coordinated".

Stdlib-only; the connection is injected read-only.
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Any, Dict, List, Optional

from src.osint import common

# A small, static gazetteer: event geography is matched against known place
# names in document text. Deliberately not a person-attribute lookup.
_PLACES = {
    "afghanistan", "africa", "america", "asia", "australia", "beijing", "berlin",
    "brazil", "brussels", "california", "canada", "chicago", "china", "delhi",
    "egypt", "england", "europe", "france", "germany", "india", "iran", "iraq",
    "israel", "japan", "kyiv", "london", "mexico", "moscow", "new york", "nigeria",
    "paris", "russia", "seoul", "spain", "taiwan", "texas", "tokyo", "ukraine",
    "united kingdom", "united states", "washington",
}
_PLACE_RE = re.compile(
    r"\b(" + "|".join(sorted((re.escape(p) for p in _PLACES), key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)

# Words dropped before comparing claim text for narrative-coordination overlap.
_STOP = frozenset(
    """a an and are as at be by for from has have in into is it its of on or that
    the their them then there these this to was were will with would""".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'\-]+")


def _person_refusal(who: str) -> Dict[str, Any]:
    return {
        "error": (
            f"refused: {who!r} is a person; geolocate_claims resolves only "
            f"event geography, never a person's location"
        ),
        "code": "person_geolocation_refused",
    }


def geolocate_claims(
    conn, topic: Optional[str] = None, entity: Optional[str] = None, limit: int = 40
) -> Dict[str, Any]:
    """Event geography from claim text: which places a claim reports an event
    happening in, cited to the source document. Refuses to geolocate a person.

    Args:
        topic: optional topic filter (claim-text substring).
        entity: optional entity filter (an actor in the corpus). A person
            entity is refused; this tool never locates individuals.
    """
    # Fail-closed person guard on the entity filter: an entity we cannot
    # confidently classify as non-human is treated as a person and refused.
    if entity is not None and common.is_person(conn, entity):
        return _person_refusal(entity)
    # The same guard on the topic path: a topic that positively names a known
    # person is refused too (unknown_is_person=False so ordinary topics — which
    # are not names — still resolve event geography).
    if topic and common.is_person(conn, topic, unknown_is_person=False):
        return _person_refusal(topic)
    if not common.table_exists(conn, "argument_claims"):
        return {"locations": [], "count": 0, "note": "no claim layer available"}

    where: List[str] = []
    params: List[Any] = []
    if topic:
        where.append("c.claim_text ILIKE ?")
        params.append(f"%{topic}%")
    if entity and common.table_exists(conn, "document_actors"):
        rows = conn.execute(
            "SELECT DISTINCT document_id FROM document_actors "
            "WHERE actor_name = ? OR entity_id = ?",
            [entity, entity],
        ).fetchall()
        doc_ids = [r[0] for r in rows if r[0]]
        if not doc_ids:
            return {"locations": [], "count": 0, "entity": entity,
                    "note": "entity not found in the corpus"}
        where.append("c.document_id IN (" + ", ".join("?" for _ in doc_ids) + ")")
        params.extend(doc_ids)

    has_articles = common.table_exists(conn, "news_articles")
    join = "LEFT JOIN news_articles a ON c.document_id = a.id" if has_articles else ""
    src = "a.source" if has_articles else "NULL"
    url = "a.url" if has_articles else "NULL"
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(int(limit))
    rows = conn.execute(
        f"SELECT c.claim_id, c.claim_text, {src}, {url} FROM argument_claims c "
        f"{join} {clause} LIMIT ?",
        params,
    ).fetchall()

    locations = []
    for claim_id, text, source, url_ in rows:
        places = sorted({m.group(1).lower() for m in _PLACE_RE.finditer(text or "")})
        for place in places:
            locations.append(
                {
                    "location": place,
                    "kind": "event-geography",
                    "claim_id": claim_id,
                    "source": source or "unknown",
                    "url": url_,
                    "cited": source is not None,
                    "verified": False,
                }
            )
    return {
        "locations": locations,
        "count": len(locations),
        "topic": topic,
        "entity": entity,
        "method": "gazetteer match on claim text; event geography only",
        "caveat": "unverified place mentions; never a person's location",
    }


def _claim_tokens(text: str) -> set:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOP and len(t) > 2}


def narrative_coordination(
    conn, topic: Optional[str] = None, min_similarity: float = 0.6, limit: int = 200
) -> Dict[str, Any]:
    """Flag cohorts of sources publishing near-identical claims on a topic, for
    human review. Never accuses: cohorts are "warrants review", with a caveat
    that similarity is often coincidental (shared wire copy, a common event).

    Args:
        topic: optional topic filter (claim-text substring).
        min_similarity: Jaccard threshold for two claims to count as echoing.
    """
    if not (common.table_exists(conn, "argument_claims") and common.table_exists(conn, "news_articles")):
        return {"cohorts": [], "count": 0, "note": "claim and source layers required"}

    where = ["a.source IS NOT NULL"]
    params: List[Any] = []
    if topic:
        where.append("c.claim_text ILIKE ?")
        params.append(f"%{topic}%")
    params.append(int(limit))
    rows = conn.execute(
        "SELECT c.claim_id, c.claim_text, a.source FROM argument_claims c "
        "JOIN news_articles a ON c.document_id = a.id "
        f"WHERE {' AND '.join(where)} LIMIT ?",
        params,
    ).fetchall()

    claims = [
        {"claim_id": r[0], "source": r[2], "tokens": _claim_tokens(r[1]), "text": (r[1] or "")[:160]}
        for r in rows
        if r[2]
    ]
    # Build an undirected "echo" graph: sources linked when two of their claims
    # are near-identical and come from *different* sources.
    edges: Dict[frozenset, List[Dict[str, Any]]] = {}
    for a, b in combinations(claims, 2):
        if a["source"] == b["source"] or not a["tokens"] or not b["tokens"]:
            continue
        inter = len(a["tokens"] & b["tokens"])
        union = len(a["tokens"] | b["tokens"])
        sim = inter / union if union else 0.0
        if sim >= min_similarity:
            key = frozenset((a["source"], b["source"]))
            edges.setdefault(key, []).append(
                {"similarity": round(sim, 3), "claims": [a["claim_id"], b["claim_id"]],
                 "sample": a["text"]}
            )

    # Connected components over the echo edges are candidate cohorts.
    adj: Dict[str, set] = {}
    for key in edges:
        s1, s2 = tuple(key)
        adj.setdefault(s1, set()).add(s2)
        adj.setdefault(s2, set()).add(s1)
    seen: set = set()
    cohorts = []
    for start in adj:
        if start in seen:
            continue
        comp, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n in comp:
                continue
            comp.add(n)
            seen.add(n)
            stack.extend(adj.get(n, ()) - comp)
        if len(comp) < 2:
            continue
        evidence = []
        for pair in combinations(sorted(comp), 2):
            evidence.extend(edges.get(frozenset(pair), []))
        score = round(sum(e["similarity"] for e in evidence) / max(1, len(evidence)), 3)
        cohorts.append(
            {
                "sources": sorted(comp),
                "size": len(comp),
                "coordination_score": score,
                "evidence": evidence[:10],
                "status": "warrants review",
                "note": "not an accusation; similarity can be coincidental (shared wire, common event)",
            }
        )
    cohorts.sort(key=lambda c: -c["coordination_score"])
    return {
        "cohorts": cohorts,
        "count": len(cohorts),
        "topic": topic,
        "method": "claim-text Jaccard echo graph; connected components as cohorts",
        "caveat": "flags cohorts for human review only; a null model would expect "
                  "some echo from shared sourcing. Never treat as proof of coordination.",
    }
