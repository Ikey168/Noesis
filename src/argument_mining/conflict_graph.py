"""
Contested Claim Conflict Graph (issue #112).

Detects when the same factual claim is made differently or directly contradicted
across sources — including cross-format conflicts (transcript vs. academic paper,
news vs. blog, etc.).

Algorithm (no heavy ML dependencies):
  1. Load recent claims grouped by topic window (news article category).
  2. For each within-topic pair from different sources, compute bag-of-words
     cosine similarity.
  3. If similarity ≥ 0.65 AND (opposite evidence relations OR cross-format
     contradiction detected), store as a conflict.
  4. conflict_type:
       "direct"  — similarity ≥ 0.80 AND explicit 'contradicts' relation in
                   claim_evidence, OR clearly opposing sentiment polarity.
       "implied" — 0.65 ≤ sim < 0.80, or high sim without explicit contradiction.

Results are stored in ``claim_conflicts`` and served by
``GET /api/v1/arguments/controversy`` and ``GET /api/v1/arguments/controversy/graph``.

Run standalone:
    python -m src.argument_mining.conflict_graph

Or via nightly scheduler:
    from src.argument_mining.conflict_scheduler import schedule_conflict_job
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from src.database.news_articles_compat import corpus_table

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stop-words (English, minimal)
# ---------------------------------------------------------------------------

_STOP: Set[str] = {
    "the", "and", "for", "that", "this", "with", "has", "have", "had",
    "are", "was", "were", "not", "but", "from", "they", "their", "been",
    "will", "can", "would", "could", "should", "may", "might", "shall",
    "into", "out", "its", "our", "all", "more", "also", "than", "which",
    "said", "says", "say", "according", "per", "cent", "year", "years",
    "new", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "first", "second", "third", "last", "next", "only",
    "other", "some", "both", "each", "such", "about", "over", "after",
    "before", "while", "when", "where", "how", "what", "who", "whom",
    "any", "most", "many", "much", "very", "just", "still", "already",
}

_SIM_DIRECT  = 0.80   # similarity threshold for "direct" conflict
_SIM_IMPLIED = 0.65   # similarity threshold for "implied" conflict

# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> List[str]:
    return [w for w in re.findall(r"\b[a-z]{3,}\b", text.lower()) if w not in _STOP]


def cosine_similarity(text_a: str, text_b: str) -> float:
    """Bag-of-words cosine similarity; returns [0.0, 1.0]."""
    ta = _tokenize(text_a)
    tb = _tokenize(text_b)
    if not ta or not tb:
        return 0.0

    freq_a: Dict[str, int] = {}
    freq_b: Dict[str, int] = {}
    for w in ta:
        freq_a[w] = freq_a.get(w, 0) + 1
    for w in tb:
        freq_b[w] = freq_b.get(w, 0) + 1

    shared = set(freq_a) & set(freq_b)
    if not shared:
        return 0.0

    dot   = sum(freq_a[w] * freq_b[w] for w in shared)
    norm_a = math.sqrt(sum(v * v for v in freq_a.values()))
    norm_b = math.sqrt(sum(v * v for v in freq_b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return round(dot / (norm_a * norm_b), 4)


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------


@dataclass
class ClaimConflict:
    claim_id_a:      str
    claim_id_b:      str
    conflict_type:   str   # direct | implied
    similarity_score: float
    source_type_a:   str
    source_type_b:   str
    topic:           str
    computed_at:     str   # ISO UTC


def _conflict_id(cid_a: str, cid_b: str) -> Tuple[str, str]:
    """Return a canonical (min, max) pair so (A,B) == (B,A)."""
    return (min(cid_a, cid_b), max(cid_a, cid_b))


def _sentiment_polarity(text: str) -> int:
    """Rough polarity: +1 positive / -1 negative / 0 neutral, via keyword."""
    pos = len(re.findall(
        r"\b(increase|rise|improve|grow|gain|expand|support|positive|higher|up)\b",
        text, re.IGNORECASE,
    ))
    neg = len(re.findall(
        r"\b(decrease|fall|decline|shrink|loss|contract|oppose|negative|lower|down)\b",
        text, re.IGNORECASE,
    ))
    if pos > neg:
        return 1
    if neg > pos:
        return -1
    return 0


def detect_conflict(
    cid_a: str, text_a: str, stype_a: str,
    cid_b: str, text_b: str, stype_b: str,
    topic: str,
    explicit_contradicts: bool = False,
) -> Optional[ClaimConflict]:
    """Return a ClaimConflict if the two claims conflict, else None.

    Thresholds:
      - direct:  sim ≥ 0.80 + opposite polarity, OR explicit contradiction + sim ≥ 0.45
      - implied: sim ≥ 0.65 + (explicit_contradicts OR cross-format), OR
                 explicit contradiction + sim ≥ 0.30
    When claim_evidence already marks claims as contradicting, topical overlap
    (sim ≥ 0.30) is sufficient — we trust the pipeline's judgement and flag the
    conflict; higher sim upgrades it to "direct".
    """
    sim = cosine_similarity(text_a, text_b)

    opposite_polarity = _sentiment_polarity(text_a) * _sentiment_polarity(text_b) == -1
    cross_format = stype_a != stype_b

    # Direct: strong semantic match + opposing signals
    if sim >= _SIM_DIRECT and (opposite_polarity or explicit_contradicts):
        ctype = "direct"
    # Direct via explicit contradiction + clear topical overlap
    elif explicit_contradicts and sim >= 0.45:
        ctype = "direct"
    # Implied: moderate similarity + some opposing signal
    elif sim >= _SIM_IMPLIED and (explicit_contradicts or cross_format or opposite_polarity):
        ctype = "implied"
    # Implied via explicit contradiction + minimal overlap (trust the pipeline)
    elif explicit_contradicts and sim >= 0.30:
        ctype = "implied"
    else:
        return None   # no signal of opposing positions

    cid_a_n, cid_b_n = _conflict_id(cid_a, cid_b)
    st_a_n, st_b_n   = (stype_a, stype_b) if cid_a_n == cid_a else (stype_b, stype_a)

    return ClaimConflict(
        claim_id_a=cid_a_n,
        claim_id_b=cid_b_n,
        conflict_type=ctype,
        similarity_score=sim,
        source_type_a=st_a_n,
        source_type_b=st_b_n,
        topic=topic,
        computed_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Batch computation
# ---------------------------------------------------------------------------


def compute_claim_conflicts(
    conn,
    lock,
    limit: int = 300,
    date_range: Optional[str] = None,
) -> dict:
    """Scan recent claims for pairwise conflicts and store in claim_conflicts.

    Args:
        conn:       DuckDB connection (read-write).
        lock:       threading.Lock guarding conn.
        limit:      Max claims to load per run.
        date_range: ISO date string (YYYY-MM-DD); skip claims older than this.

    Returns:
        {"claims_loaded": int, "pairs_tested": int, "conflicts_stored": int}
    """
    # 1. Load claims with their document topic
    where_parts = ["c.claim_text IS NOT NULL", "c.source_type IS NOT NULL"]
    params: List[Any] = []
    if date_range:
        where_parts.append("c.extracted_at >= ?")
        params.append(date_range)
    params.append(limit)

    with lock:
        claim_rows = conn.execute(
            f"""
            SELECT c.claim_id, c.claim_text, c.source_type,
                   COALESCE(n.category, 'general') AS topic,
                   COALESCE(n.source, c.source_type) AS source_name,
                   n.id AS article_id
            FROM argument_claims c
            LEFT JOIN {corpus_table(conn)} n ON c.document_id = n.id
            WHERE {" AND ".join(where_parts)}
            ORDER BY c.extracted_at DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()

    if not claim_rows:
        return {"claims_loaded": 0, "pairs_tested": 0, "conflicts_stored": 0}

    # 2. Load explicit contradiction links for quick lookup
    claim_ids = [r[0] for r in claim_rows]
    placeholders = ", ".join("?" * len(claim_ids))
    with lock:
        ev_rows = conn.execute(
            f"""
            SELECT e.claim_id, c2.claim_id
            FROM claim_evidence e
            JOIN argument_claims c2 ON e.evidence_document_id = c2.document_id
            WHERE e.claim_id IN ({placeholders})
              AND e.relation = 'contradicts'
            """,
            claim_ids,
        ).fetchall()
    explicit_pairs: Set[Tuple[str, str]] = {
        _conflict_id(r[0], r[1]) for r in ev_rows
    }

    # 3. Group by topic
    by_topic: Dict[str, List[Tuple]] = {}
    for row in claim_rows:
        t = row[3]
        by_topic.setdefault(t, []).append(row)

    # 4. Pair-wise conflict detection within each topic
    conflicts: List[ClaimConflict] = []
    seen_pairs: Set[Tuple[str, str]] = set()
    pairs_tested = 0
    MAX_PAIRS_PER_TOPIC = 200

    for topic_claims in by_topic.values():
        n = len(topic_claims)
        checked = 0
        for i in range(n):
            if checked >= MAX_PAIRS_PER_TOPIC:
                break
            for j in range(i + 1, n):
                if checked >= MAX_PAIRS_PER_TOPIC:
                    break
                ra, rb = topic_claims[i], topic_claims[j]
                cid_a, txt_a, stype_a, topic_a = ra[0], ra[1], ra[2], ra[3]
                cid_b, txt_b, stype_b, topic_b = rb[0], rb[1], rb[2], rb[3]

                # Skip same-source pairs
                if ra[4] == rb[4]:
                    continue

                pair_key = _conflict_id(cid_a, cid_b)
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                pairs_tested += 1
                checked += 1

                explicit = pair_key in explicit_pairs
                c = detect_conflict(
                    cid_a, txt_a, stype_a,
                    cid_b, txt_b, stype_b,
                    topic_a,
                    explicit_contradicts=explicit,
                )
                if c:
                    conflicts.append(c)

    # 5. Store results
    if conflicts:
        with lock:
            conn.executemany(
                """
                INSERT OR REPLACE INTO claim_conflicts
                    (claim_id_a, claim_id_b, conflict_type, similarity_score,
                     source_type_a, source_type_b, topic, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (c.claim_id_a, c.claim_id_b, c.conflict_type,
                     c.similarity_score, c.source_type_a, c.source_type_b,
                     c.topic, c.computed_at)
                    for c in conflicts
                ],
            )

    counts = {
        "claims_loaded": len(claim_rows),
        "pairs_tested": pairs_tested,
        "conflicts_stored": len(conflicts),
    }
    logger.info("compute_claim_conflicts: %s", counts)
    return counts


# ---------------------------------------------------------------------------
# Graph builder (for controversy endpoints)
# ---------------------------------------------------------------------------


def build_conflict_graph(
    conn,
    lock,
    topic: Optional[str] = None,
    source_type: Optional[str] = None,
    date_cutoff: Optional[str] = None,
    limit: int = 60,
) -> Dict[str, Any]:
    """Build nodes+edges JSON from claim_conflicts (falls back to claim_evidence).

    Returns the same shape as the existing controversy/graph endpoint so the
    frontend force-directed graph works without changes.
    """
    where_parts: List[str] = []
    params: List[Any] = []

    if source_type:
        where_parts.append("(cf.source_type_a = ? OR cf.source_type_b = ?)")
        params.extend([source_type, source_type])
    if topic:
        where_parts.append("cf.topic ILIKE ?")
        params.append(f"%{topic}%")
    if date_cutoff:
        where_parts.append("cf.computed_at >= ?")
        params.append(date_cutoff)

    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    with lock:
        # Prefer claim_conflicts table
        try:
            rows = conn.execute(
                f"""
                SELECT
                    cf.claim_id_a, ca.claim_text, ca.source_type,
                    ca.confidence, ca.extracted_at,
                    COALESCE(na.source, ca.source_type) AS source_a,
                    cf.topic,
                    ca.document_id,
                    cf.claim_id_b, cb.claim_text, cb.source_type,
                    cb.confidence, cb.extracted_at,
                    COALESCE(nb.source, cb.source_type) AS source_b,
                    cf.topic,
                    cb.document_id,
                    cf.similarity_score,
                    cf.conflict_type
                FROM claim_conflicts cf
                JOIN argument_claims ca ON cf.claim_id_a = ca.claim_id
                JOIN argument_claims cb ON cf.claim_id_b = cb.claim_id
                LEFT JOIN {corpus_table(conn)} na ON ca.document_id = na.id
                LEFT JOIN {corpus_table(conn)} nb ON cb.document_id = nb.id
                {where_clause}
                ORDER BY cf.similarity_score DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        except Exception:
            rows = []

    nodes: Dict[str, Any] = {}
    edges: List[Dict[str, Any]] = []

    for r in rows:
        (
            cid_a, txt_a, stype_a, conf_a, date_a, src_a, topic_a, doc_a,
            cid_b, txt_b, stype_b, conf_b, date_b, src_b, topic_b, doc_b,
            sim, ctype,
        ) = r
        for cid, txt, stype, conf, date_, src, topic_v, doc in [
            (cid_a, txt_a, stype_a, conf_a, date_a, src_a, topic_a, doc_a),
            (cid_b, txt_b, stype_b, conf_b, date_b, src_b, topic_b, doc_b),
        ]:
            if cid not in nodes:
                nodes[cid] = {
                    "id": cid,
                    "label": src,
                    "source": src,
                    "source_type": stype,
                    "topic": topic_v,
                    "date": str(date_) if date_ else None,
                    "claim_text": txt,
                    "confidence": float(conf) if conf is not None else 0.5,
                    "document_id": doc,
                    "conflict_type": ctype,
                }
        edges.append({
            "source":    cid_a,
            "target":    cid_b,
            "severity":  round(float(sim), 3),
            "relation":  "contradicts",
            "conflict_type": ctype,
        })

    node_list = list(nodes.values())
    return {
        "nodes": node_list,
        "edges": edges,
        "node_count": len(node_list),
        "edge_count": len(edges),
        "_source": "claim_conflicts",
    }


if __name__ == "__main__":
    import threading
    logging.basicConfig(level=logging.INFO)
    from src.database.local_analytics_connector import get_shared_connection

    _conn = get_shared_connection()
    _lock = getattr(_conn, "_lock", None) or threading.Lock()
    result = compute_claim_conflicts(_conn, _lock)
    print(f"Done: {result}")
