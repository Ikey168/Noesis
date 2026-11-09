"""
Consolidation v1: the claim linking pass.

The corpus is enriched per-document, so the same claim arriving from six
outlets over three days is six ``argument_claims`` rows. This pass turns
that stream into knowledge by adding **links** between claims — never by
rewriting them (link-don't-merge; the merged reading experience is computed
from clusters at presentation time, #966).

Relations: ``duplicate`` (symmetric, canonical id order), ``supports`` /
``contradicts`` (directed, premise → hypothesis), and the directed temporal
transitions ``supersedes`` / ``corrects`` / ``retracts`` (newer → older for
the same identified source).

Mechanics:

- **Candidates** are generated per new claim against claims whose documents
  fall inside a time window (default ±14 days) — never O(n²) over the
  corpus. With an embedding provider, candidate ranking is cosine over
  claim-text vectors persisted in ``claim_embeddings``; without one, a
  lexical-overlap fallback ranks the same window.
- **Classification** uses the shared NLI backend (:mod:`src.kb.nli`):
  entailment → supports, contradiction → contradicts, high similarity plus
  bidirectional entailment → duplicate. A pair whose directions disagree
  decisively (one entails, the other contradicts) abstains and is counted in
  ``summary["abstained"]`` instead of writing the more confident side. Model weights must be fetched before
  the pass runs.
- **Provenance**: every link row carries method, model version,
  ``prediction_mode``/confidence (#958), and a ``run_id`` — a bad model
  release is reverted by :func:`delete_run`, which restores the prior state
  exactly.
- **Incrementality** is set-based via a scan ledger (``kb_claim_link_scans``),
  the same discipline as the membership pass.

Link endpoints carry ``(domain, claim_id)`` pairs so cross-backing links
(#967) need no schema change; ``domain`` is the claim's document's
highest-score domain, or ``''`` when unassigned.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import Counter
from typing import Any, Sequence

from src.kb.nli import CONTRADICTION, ENTAILMENT, get_nli_backend
from src.kb.temporal import classify_temporal_relation

_LINKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_links (
    domain_a        TEXT NOT NULL,
    claim_a         TEXT NOT NULL,
    domain_b        TEXT NOT NULL,
    claim_b         TEXT NOT NULL,
    relation        TEXT NOT NULL,
    score           DOUBLE NOT NULL,
    method          TEXT NOT NULL,
    prediction_mode TEXT NOT NULL,
    confidence      DOUBLE,
    model_version   TEXT,
    run_id          TEXT NOT NULL,
    created_at      BIGINT NOT NULL,
    PRIMARY KEY (claim_a, claim_b, relation)
)
"""

_CLAIM_EMBEDDINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claim_embeddings (
    claim_id   TEXT PRIMARY KEY,
    model      TEXT,
    dim        INTEGER,
    vector     TEXT,
    updated_at BIGINT
)
"""

_SCANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_claim_link_scans (
    claim_id   TEXT PRIMARY KEY,
    run_id     TEXT,
    scanned_at BIGINT NOT NULL
)
"""

_STOP = {
    "the", "a", "an", "of", "in", "on", "to", "for", "and", "or", "is",
    "are", "was", "were", "be", "this", "that", "it", "with", "as", "by",
    "at", "from", "said", "says",
}

RELATIONS = (
    "duplicate",
    "supports",
    "contradicts",
    "supersedes",
    "corrects",
    "retracts",
)


def ensure_claim_link_schema(conn) -> None:
    from src.database.local_warehouse_seed import ensure_schema
    from src.kb.membership import ensure_membership_schema

    ensure_schema(conn)  # argument_claims + documents (+ compat views)
    ensure_membership_schema(conn)  # document_domains, read for endpoints
    conn.execute(_LINKS_SCHEMA)
    conn.execute(_CLAIM_EMBEDDINGS_SCHEMA)
    conn.execute(_SCANS_SCHEMA)
    from src.kb.claim_exclusions import ensure_exclusion_schema

    ensure_exclusion_schema(conn)


def _tokens(text: str) -> set:
    return {
        token
        for token in re.findall(r"[a-z0-9']+", text.lower())
        if token not in _STOP
    }


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _claim_domain(conn, document_id: str) -> str:
    row = conn.execute(
        "SELECT domain FROM document_domains WHERE document_id = ?"
        " ORDER BY score DESC, domain LIMIT 1",
        [document_id],
    ).fetchone()
    return row[0] if row else ""


def _embed_new_claims(conn, claims, provider, model_name: str) -> None:
    missing = [
        (claim_id, text)
        for claim_id, text in claims
        if conn.execute(
            "SELECT 1 FROM claim_embeddings WHERE claim_id = ? AND model = ?",
            [claim_id, model_name],
        ).fetchone()
        is None
    ]
    if not missing:
        return
    vectors = provider.embed_texts([text for _, text in missing])
    now = int(time.time() * 1000)
    for (claim_id, _), vector in zip(missing, vectors):
        values = [float(component) for component in vector]
        conn.execute(
            """
            INSERT INTO claim_embeddings (claim_id, model, dim, vector, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (claim_id) DO UPDATE SET
                model = excluded.model, dim = excluded.dim,
                vector = excluded.vector, updated_at = excluded.updated_at
            """,
            [claim_id, model_name, len(values), json.dumps(values), now],
        )


def _upsert_link(
    conn,
    endpoint_a: tuple[str, str],
    endpoint_b: tuple[str, str],
    relation: str,
    score: float,
    method: str,
    prediction_mode: str,
    confidence: float,
    model_version: str,
    run_id: str,
) -> bool:
    """Insert or strengthen a link; returns True when a row was written."""
    domain_a, claim_a = endpoint_a
    domain_b, claim_b = endpoint_b
    # duplicate and contradicts are symmetric — store one canonical row.
    if relation in ("duplicate", "contradicts") and claim_b < claim_a:
        domain_a, claim_a, domain_b, claim_b = domain_b, claim_b, domain_a, claim_a
    before = conn.execute(
        "SELECT confidence FROM claim_links"
        " WHERE claim_a = ? AND claim_b = ? AND relation = ?",
        [claim_a, claim_b, relation],
    ).fetchone()
    conn.execute(
        """
        INSERT INTO claim_links
            (domain_a, claim_a, domain_b, claim_b, relation, score, method,
             prediction_mode, confidence, model_version, run_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (claim_a, claim_b, relation) DO UPDATE SET
            score = excluded.score,
            method = excluded.method,
            prediction_mode = excluded.prediction_mode,
            confidence = excluded.confidence,
            model_version = excluded.model_version,
            run_id = excluded.run_id,
            created_at = excluded.created_at
        WHERE excluded.confidence > claim_links.confidence
        """,
        [
            domain_a, claim_a, domain_b, claim_b, relation, round(score, 6),
            method, prediction_mode, round(confidence, 4), model_version,
            run_id, int(time.time() * 1000),
        ],
    )
    return before is None


def run_claim_linking_pass(
    conn,
    provider: Any | None = None,
    nli: Any | None = None,
    run_id: str | None = None,
    embedding_model: str = "all-MiniLM-L6-v2",
    time_window_days: int = 14,
    k_neighbors: int = 8,
    candidate_threshold: float = 0.5,
    duplicate_threshold: float = 0.88,
    min_link_confidence: float = 0.55,
    supersede_gap_days: int = 3,
    jev_reviewer: Any | None = None,
    subject_gate: bool = True,
    allow_unidentified_subjects: bool = True,
    review_cache: bool = True,
    review_chunk_size: int = 200,
    write_chunk_claims: int = 500,
) -> dict[str, Any]:
    """Link new claims to their neighbours; returns a summary.

    ``provider`` embeds claim texts for candidate ranking (``None`` → lexical
    fallback). ``nli`` classifies pair relations (``None`` → the shared
    pretrained backend). Both are injectable for tests.

    ``jev_reviewer`` (a :class:`src.kb.claim_link_review.JevClaimLinkReviewer`)
    reviews every proposed ``supports``/``duplicate``/``contradicts`` link
    with Jev between planning and the write transaction. In ``shadow`` mode
    links are written as planned and the assessment is recorded; in ``gate``
    mode a link Jev answers against is withheld. Jev never adds a link, and
    an unavailable or abstaining review keeps the local link. Counts are in
    ``summary["jev"]``.

    ``subject_gate`` (default on) allows a ``contradicts`` link only between
    claims about the same thing (same malware family, package, host,
    wallet, file hash or named entity; see :mod:`src.kb.claim_subjects`),
    and skips any pair whose claims name different indicators. Pairs where
    neither claim names anything are linked as before unless
    ``allow_unidentified_subjects`` is False. Blocked pairs are counted in
    ``summary["subject_gate"]``.

    Reviews are asked in chunks of ``review_chunk_size`` and persisted as
    they return (``review_cache``), so a restart after a crash reuses every
    answer already paid for. Links and the scan ledger are committed every
    ``write_chunk_claims`` claims instead of in one transaction (a crash
    mid-write leaves later claims unscanned, and the next pass picks them up).
    """
    if type(write_chunk_claims) is not int or write_chunk_claims < 1:
        raise ValueError("write_chunk_claims must be a positive integer")
    ensure_claim_link_schema(conn)
    nli = nli or get_nli_backend()
    run_id = run_id or f"kb-claim-links-{uuid.uuid4().hex[:12]}"
    gate = {"enabled": subject_gate, "allow_unidentified": allow_unidentified_subjects}
    window_ms = time_window_days * 86_400_000

    new_claims = conn.execute(
        """
        SELECT c.claim_id, c.claim_text, c.document_id,
               COALESCE(d.ingested_at, 0)
        FROM argument_claims c
        LEFT JOIN documents d ON d.document_id = c.document_id
        LEFT JOIN kb_claim_link_scans s ON s.claim_id = c.claim_id
        WHERE s.claim_id IS NULL
          AND NOT EXISTS (SELECT 1 FROM argument_claim_exclusions x
                          WHERE x.claim_id = c.claim_id)
        ORDER BY c.claim_id
        """
    ).fetchall()

    summary = {
        "run_id": run_id,
        "scanned": len(new_claims),
        "links": {relation: 0 for relation in RELATIONS},
        "abstained": {"conflicting_directions": 0},
        "mode": getattr(nli, "prediction_mode", "unknown"),
    }
    if jev_reviewer is not None:
        summary["jev"] = _new_jev_summary(jev_reviewer)
    if not new_claims:
        return summary

    if provider is not None:
        _embed_new_claims(
            conn,
            [(claim_id, text) for claim_id, text, _, _ in new_claims],
            provider,
            embedding_model,
        )

    # 1. Plan every pair from local NLI (reads only).
    plans: list[tuple[str, list[dict[str, Any]]]] = []
    for claim_id, claim_text, document_id, ingested_at in new_claims:
        candidates = _candidates_for(
            conn,
            claim_id,
            claim_text,
            ingested_at,
            provider is not None,
            embedding_model,
            window_ms,
            k_neighbors,
            candidate_threshold,
        )
        links: list[dict[str, Any]] = []
        if candidates:
            domain = _claim_domain(conn, document_id)
            for other_id, other_text, other_doc, other_ingested, similarity in candidates:
                links += _plan_pair(
                    conn,
                    nli,
                    summary,
                    (domain, claim_id, claim_text, ingested_at),
                    (
                        _claim_domain(conn, other_doc),
                        other_id,
                        other_text,
                        other_ingested,
                    ),
                    similarity,
                    duplicate_threshold,
                    min_link_confidence,
                    supersede_gap_days * 86_400_000,
                    gate,
                )
        plans.append((claim_id, links))

    # 2. Optional Jev review, outside the write transaction: the decision
    #    runtime commits its own budget and receipt records.
    reviewed_plans = _review_plans(
        conn, jev_reviewer, run_id, plans, duplicate_threshold, summary,
        review_cache=review_cache, chunk_size=review_chunk_size,
    )

    # 3. Write links, assessments and the scan ledger, one transaction per
    #    chunk of claims (one transaction for a whole corpus ran out of memory).
    if type(write_chunk_claims) is not int or write_chunk_claims < 1:
        raise ValueError("write_chunk_claims must be a positive integer")
    for start in range(0, len(reviewed_plans), write_chunk_claims):
        conn.execute("BEGIN TRANSACTION")
        try:
            for claim_id, reviewed in reviewed_plans[start:start + write_chunk_claims]:
                _write_reviewed(conn, run_id, summary, reviewed)
                conn.execute(
                    """
                    INSERT INTO kb_claim_link_scans (claim_id, run_id, scanned_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (claim_id) DO UPDATE SET
                        run_id = excluded.run_id, scanned_at = excluded.scanned_at
                    """,
                    [claim_id, run_id, int(time.time() * 1000)],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return summary


def _candidates_for(
    conn,
    claim_id: str,
    claim_text: str,
    ingested_at: int,
    use_embeddings: bool,
    embedding_model: str,
    window_ms: int,
    k_neighbors: int,
    candidate_threshold: float,
) -> list[tuple[str, str, str, int, float]]:
    """Top-K similar claims inside the time window (embedding or lexical)."""
    rows = conn.execute(
        """
        SELECT c.claim_id, c.claim_text, c.document_id,
               COALESCE(d.ingested_at, 0), e.vector
        FROM argument_claims c
        LEFT JOIN documents d ON d.document_id = c.document_id
        LEFT JOIN claim_embeddings e
          ON e.claim_id = c.claim_id AND e.model = ?
        WHERE c.claim_id <> ?
          AND NOT EXISTS (SELECT 1 FROM argument_claim_exclusions x
                          WHERE x.claim_id = c.claim_id)
          AND (COALESCE(d.ingested_at, 0) = 0 OR ? = 0
               OR abs(COALESCE(d.ingested_at, 0) - ?) <= ?)
        """,
        [embedding_model, claim_id, ingested_at, ingested_at, window_ms],
    ).fetchall()

    own_vector = None
    if use_embeddings:
        vector_row = conn.execute(
            "SELECT vector FROM claim_embeddings WHERE claim_id = ? AND model = ?",
            [claim_id, embedding_model],
        ).fetchone()
        if vector_row and vector_row[0]:
            own_vector = json.loads(vector_row[0])
    own_tokens = _tokens(claim_text)

    scored = []
    for other_id, other_text, other_doc, other_ingested, other_vector_json in rows:
        if own_vector is not None and other_vector_json:
            similarity = _cosine(own_vector, json.loads(other_vector_json))
        else:
            similarity = _jaccard(own_tokens, _tokens(other_text or ""))
        if similarity >= candidate_threshold:
            scored.append((other_id, other_text, other_doc, other_ingested, similarity))
    scored.sort(key=lambda item: item[4], reverse=True)
    return scored[:k_neighbors]


def _link_pair(
    conn,
    nli,
    run_id: str,
    summary: dict[str, Any],
    side_a: tuple[str, str, str, int],
    side_b: tuple[str, str, str, int],
    similarity: float,
    duplicate_threshold: float,
    min_link_confidence: float,
    supersede_gap_ms: int,
) -> None:
    """Plan and immediately write one pair's links (no hosted review)."""
    for link in _plan_pair(
        conn, nli, summary, side_a, side_b, similarity, duplicate_threshold,
        min_link_confidence, supersede_gap_ms,
    ):
        _write_planned(conn, run_id, summary, link)


def _planned(relation, premise, hypothesis, texts, similarity, method, mode,
             confidence, model_version, primary=None) -> dict[str, Any]:
    return {
        "relation": relation,
        "premise": premise,
        "hypothesis": hypothesis,
        "texts": texts,
        "score": similarity,
        "method": method,
        "prediction_mode": mode,
        "confidence": confidence,
        "model_version": model_version,
        # A temporal transition follows its primary link's review outcome.
        "primary": primary,
    }


def _plan_pair(
    conn,
    nli,
    summary: dict[str, Any],
    side_a: tuple[str, str, str, int],
    side_b: tuple[str, str, str, int],
    similarity: float,
    duplicate_threshold: float,
    min_link_confidence: float,
    supersede_gap_ms: int,
    gate: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Decide one pair's links from local NLI without writing anything.

    Returns the primary link first, followed by any temporal transition that
    depends on it. Reads only (temporal context), so hosted review can run
    between planning and the write transaction.
    """
    domain_a, claim_a, text_a, ingested_a = side_a
    domain_b, claim_b, text_b, ingested_b = side_b
    method = getattr(nli, "name", "unknown")
    mode = getattr(nli, "prediction_mode", "unknown")
    model_version = getattr(nli, "model_version", "unknown")
    texts = {claim_a: text_a, claim_b: text_b}

    subjects = None
    if gate is not None and gate.get("enabled", True):
        from src.kb.claim_subjects import claim_subjects, subject_gate

        subjects = (claim_subjects(text_a), claim_subjects(text_b))
        # Claims naming different indicators (hash, host, package, wallet,
        # CVE, file) are about different things for every relation: skip
        # the NLI calls entirely.
        early = subject_gate(text_a, text_b, "supports",
                             subjects_a=subjects[0], subjects_b=subjects[1])
        if not early.allowed:
            _count_gate(summary, early.reason)
            return []

    forward = nli.classify(text_a, text_b)
    backward = nli.classify(text_b, text_a)
    source_a, change_a = _claim_temporal_context(conn, claim_a)
    source_b, change_b = _claim_temporal_context(conn, claim_b)
    same_source = bool(source_a and source_a == source_b)
    newer_change = change_a if ingested_a > ingested_b else change_b

    # Opposing decisive directions (A entails B but B contradicts A) are an
    # inconsistent assessment, not evidence for either relation. Writing the
    # more confident side would turn model noise into a graph edge, so the
    # pair abstains and is counted for review.
    if {forward.label, backward.label} == {ENTAILMENT, CONTRADICTION}:
        summary.setdefault("abstained", {}).setdefault("conflicting_directions", 0)
        summary["abstained"]["conflicting_directions"] += 1
        return []

    # Duplicate: high similarity + mutual entailment.
    if (
        similarity >= duplicate_threshold
        and forward.label == ENTAILMENT
        and backward.label == ENTAILMENT
    ):
        confidence = min(forward.confidence, backward.confidence)
        if confidence >= min_link_confidence:
            links = [_planned(
                "duplicate", (domain_a, claim_a), (domain_b, claim_b), texts,
                similarity, method, mode, confidence, model_version,
            )]
            transition = classify_temporal_relation(
                "supersedes",
                same_source=same_source,
                observed_a_ms=ingested_a,
                observed_b_ms=ingested_b,
                newer_change_class=newer_change,
            )
            if same_source and ingested_a and ingested_b and (
                abs(ingested_a - ingested_b) >= supersede_gap_ms
                or transition in {"corrects", "retracts"}
            ):
                newer, older = (
                    ((domain_a, claim_a), (domain_b, claim_b))
                    if ingested_a > ingested_b
                    else ((domain_b, claim_b), (domain_a, claim_a))
                )
                links.append(_planned(
                    transition, newer, older, texts, similarity,
                    f"{method}+temporal", mode, confidence, model_version,
                    primary=links[0],
                ))
            return links

    # Directed supports/contradicts: keep the stronger direction.
    best = None
    for premise, hypothesis, result in (
        ((domain_a, claim_a), (domain_b, claim_b), forward),
        ((domain_b, claim_b), (domain_a, claim_a), backward),
    ):
        if result.label in (ENTAILMENT, CONTRADICTION):
            if best is None or result.confidence > best[2].confidence:
                best = (premise, hypothesis, result)
    if best is None:
        return []
    premise, hypothesis, result = best
    if result.confidence < min_link_confidence:
        return []
    base_relation = "supports" if result.label == ENTAILMENT else "contradicts"
    if base_relation == "contradicts" and subjects is not None:
        from src.kb.claim_subjects import subject_gate

        decision = subject_gate(
            text_a, text_b, "contradicts",
            allow_unidentified=gate.get("allow_unidentified", True),
            subjects_a=subjects[0], subjects_b=subjects[1],
        )
        if not decision.allowed:
            _count_gate(summary, decision.reason)
            return []
    relation = classify_temporal_relation(
        base_relation,
        same_source=same_source,
        observed_a_ms=ingested_a,
        observed_b_ms=ingested_b,
        newer_change_class=newer_change,
    )
    link = _planned(
        relation, premise, hypothesis, texts, similarity, method, mode,
        result.confidence, model_version,
    )
    if relation in {"corrects", "retracts", "supersedes"}:
        link["premise"], link["hypothesis"] = (
            ((domain_a, claim_a), (domain_b, claim_b))
            if ingested_a > ingested_b
            else ((domain_b, claim_b), (domain_a, claim_a))
        )
        # A same-source transition is reviewed with its own rubric: does the
        # newer claim update the older one? (Not "do they conflict?", which
        # rejected most genuine updates.) Direction stays newer -> older.
        link["review_as"] = "supersedes"
    return [link]


def _count_gate(summary: dict[str, Any], reason: str | None) -> None:
    gated = summary.setdefault("subject_gate", {})
    gated[reason or "blocked"] = gated.get(reason or "blocked", 0) + 1


def _write_planned(conn, run_id: str, summary: dict[str, Any], link: dict[str, Any]) -> bool:
    wrote = _upsert_link(
        conn, link["premise"], link["hypothesis"], link["relation"],
        link["score"], link["method"], link["prediction_mode"],
        link["confidence"], link["model_version"], run_id,
    )
    if wrote:
        summary["links"][link["relation"]] += 1
    return wrote


def _new_jev_summary(reviewer) -> dict[str, Any]:
    return {
        "mode": reviewer.mode,
        "method": reviewer.method,
        "reviewed": 0,
        "agrees": 0,
        "disagrees": 0,
        "withheld": 0,
        "abstained": 0,
        "unavailable": 0,
        "unresolved": 0,
        "requests": 0,
        "reversed": 0,
    }


def _review_key(link: dict[str, Any]) -> tuple:
    relation = link.get("review_as", link["relation"])
    premise, hypothesis = link.get("review_direction", (link["premise"], link["hypothesis"]))
    ends = (premise[1], hypothesis[1])
    return (relation, tuple(sorted(ends)) if relation in _SYMMETRIC else ends)


_SYMMETRIC = ("duplicate", "contradicts")


def _text_key(item: dict[str, Any]) -> tuple:
    """Identity of the question itself, independent of which claims ask it.

    Similarity and threshold only change the answer for duplicates, so they
    are part of the key only there.
    """
    relation = item["relation"]
    texts = (item["premise"][1], item["hypothesis"][1])
    if relation in _SYMMETRIC:
        texts = tuple(sorted(texts))
    if relation == "duplicate":
        return (relation, *texts, round(float(item["similarity"]), 6), item["duplicate_threshold"])
    return (relation, *texts)


def _cache_key(identity: Any, claim_key: tuple, text_key: tuple) -> str:
    import hashlib

    payload = json.dumps([identity, list(claim_key), list(text_key)], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


class _Groups:
    """Union-find over Jev-confirmed duplicates of this pass."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, item: str) -> str:
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


_REUSABLE = ("answered", "abstained")


def _review_plans(conn, reviewer, run_id, plans, duplicate_threshold, summary, *,
                  review_cache: bool = True, chunk_size: int = 200):
    """Ask Jev about every distinct primary link (outside any write transaction).

    Each distinct question is asked once:

    * a pair planned from both sides is one link;
    * a link whose claim texts repeat word for word elsewhere reuses that
      answer (``reused_from: identical_text``);
    * duplicates are reviewed first; supports/contradicts between two groups
      of Jev-confirmed duplicates reuse one answer (``duplicate_group``);
    * with ``review_cache``, answers persisted by an earlier (e.g. crashed)
      run with the same reviewer identity are reused (``cache``).

    Reviews run in chunks of ``chunk_size``; each chunk's answers are
    persisted in ``claim_link_review_cache`` as soon as it returns, so a
    crash loses at most one chunk. The planned total per relation and
    progress are logged and reported in ``summary["jev"]``. A dependent
    temporal transition inherits its primary's review.
    """
    if reviewer is None:
        return [(claim_id, [(link, None) for link in links]) for claim_id, links in plans]
    import logging

    from src.kb.claim_link_review import ensure_assessment_schema

    log = logging.getLogger(__name__)
    ensure_assessment_schema(conn)
    jev_summary = summary.setdefault("jev", _new_jev_summary(reviewer))
    items: dict[tuple, dict[str, Any]] = {}
    for _, links in plans:
        for link in links:
            if link["primary"] is not None:
                continue
            key = _review_key(link)
            if key in items:
                continue
            premise, hypothesis = link.get(
                "review_direction", (link["premise"], link["hypothesis"])
            )
            items[key] = {
                "relation": key[0],
                "premise": (premise[1], link["texts"][premise[1]]),
                "hypothesis": (hypothesis[1], link["texts"][hypothesis[1]]),
                "similarity": link["score"],
                "duplicate_threshold": duplicate_threshold,
            }
    planned = Counter(item["relation"] for item in items.values())
    jev_summary["planned"] = dict(sorted(planned.items()))
    log.info("claim-link review %s: %d planned %s", run_id, len(items), dict(planned))
    identity = reviewer.cache_identity() if hasattr(reviewer, "cache_identity") else None
    cache_keys = {key: _cache_key(identity, key, _text_key(item)) for key, item in items.items()}
    cached: dict[tuple, dict[str, Any]] = {}
    if review_cache and identity is not None and items:
        stored = {}
        keys = list(cache_keys.values())
        for start in range(0, len(keys), 500):
            chunk = keys[start:start + 500]
            marks = ", ".join("?" for _ in chunk)
            stored.update(dict(conn.execute(
                f"SELECT cache_key, review_json FROM claim_link_review_cache WHERE cache_key IN ({marks})",
                chunk,
            ).fetchall()))
        cached = {key: json.loads(stored[ck]) for key, ck in cache_keys.items() if ck in stored}
    reused = {"cache": 0, "identical_text": 0, "duplicate_group": 0}
    reviews: dict[tuple, dict[str, Any]] = {}

    def reuse(source: dict[str, Any], relation: str, kind: str) -> dict[str, Any]:
        reused[kind] += 1
        detail = {**source["detail"], "reused_from": {
            "kind": kind, "decision_run_id": source.get("decision_run_id")}}
        return reviewer.finish(reviewer.base(relation, source.get("decision_run_id")),
                               source["status"], source["verdict"], source["jev_relation"], detail)

    def persist(key: tuple, review: dict[str, Any]) -> None:
        if not review_cache or identity is None or review["status"] not in _REUSABLE:
            return
        stored = {k: review[k] for k in ("status", "verdict", "jev_relation", "detail", "decision_run_id")}
        conn.execute(
            "INSERT INTO claim_link_review_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (cache_key) DO UPDATE SET review_json = excluded.review_json,"
            " decision_run_id = excluded.decision_run_id, linking_run_id = excluded.linking_run_id,"
            " created_at = excluded.created_at",
            [cache_keys[key], key[0], key[1][0], key[1][1], json.dumps(stored, sort_keys=True),
             review.get("decision_run_id"), run_id, int(time.time() * 1000)],
        )

    done = 0

    def ask(keys: list[tuple]) -> None:
        nonlocal done
        for start in range(0, len(keys), max(1, chunk_size)):
            chunk = keys[start:start + chunk_size]
            before = len(getattr(reviewer, "stats", []))
            results = reviewer.review_all(run_id, [items[key] for key in chunk])
            jev_summary["requests"] += len(getattr(reviewer, "stats", [])) - before
            for key, review in zip(chunk, results, strict=True):
                reviews[key] = review
                persist(key, review)
            done += len(chunk)
            log.info("claim-link review %s: %d/%d asked", run_id, done, len(items))

    groups = _Groups()

    def group_key(key: tuple) -> tuple | None:
        relation = key[0]
        if relation not in ("supports", "contradicts"):
            return None
        a, b = groups.find(key[1][0]), groups.find(key[1][1])
        ends = tuple(sorted((a, b))) if relation == "contradicts" else (a, b)
        return (relation, ends)

    def review_phase(keys: list[tuple]) -> None:
        by_text: dict[tuple, tuple] = {}
        by_group: dict[tuple, tuple] = {}
        for key in keys:
            if key in reviews:
                by_text.setdefault(_text_key(items[key]), key)
        representatives, followers = [], []
        for key in keys:
            tk, gk = _text_key(items[key]), group_key(key)
            if key in cached:
                reviews[key] = reuse(cached[key], key[0], "cache")
                if reviews[key]["status"] in _REUSABLE:
                    by_text.setdefault(tk, key)
                    if gk is not None:
                        by_group.setdefault(gk, key)
                continue
            if tk in by_text or (gk is not None and gk in by_group):
                followers.append(key)
                continue
            by_text[tk] = key
            if gk is not None:
                by_group[gk] = key
            representatives.append(key)
        ask(representatives)
        retry = []
        for key in followers:
            tk, gk = _text_key(items[key]), group_key(key)
            source = by_text.get(tk) if by_text.get(tk) in reviews else None
            kind = "identical_text"
            if source is None or reviews[source]["status"] not in _REUSABLE:
                source, kind = (by_group.get(gk), "duplicate_group") if gk is not None else (None, None)
            if source is None or source not in reviews or reviews[source]["status"] not in _REUSABLE:
                retry.append(key)  # the answer to reuse was not usable: ask this one
                continue
            reviews[key] = reuse(reviews[source], key[0], kind)
        ask(retry)

    duplicate_keys = [key for key, item in items.items() if item["relation"] == "duplicate"]
    review_phase(duplicate_keys)
    for key in duplicate_keys:
        if reviews[key]["verdict"] == "agrees":
            groups.union(key[1][0], key[1][1])
    review_phase([key for key, item in items.items() if item["relation"] != "duplicate"])
    jev_summary["reused"] = reused
    for review in reviews.values():
        jev_summary["reviewed"] += 1
        jev_summary[review["verdict"] or review["status"]] += 1
        if not review["write"]:
            jev_summary["withheld"] += 1
        if review.get("reverse"):
            jev_summary["reversed"] += 1
    out = []
    for claim_id, links in plans:
        by_link: dict[int, dict[str, Any] | None] = {}
        reviewed: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
        for link in links:
            if link["primary"] is not None:
                review = by_link.get(id(link["primary"]))
            else:
                review = reviews[_review_key(link)]
                by_link[id(link)] = review
            reviewed.append((link, review))
        out.append((claim_id, reviewed))
    return out


def _write_reviewed(conn, run_id, summary, reviewed) -> int:
    """Write reviewed links and their assessments; returns links written.

    A gate-mode ``reverse`` writes the support in the direction Jev read it.
    A link with an operator override (``claim_link_jev_overrides``) is
    written as planned and its assessment records ``override_written``.
    """
    from src.kb.claim_link_review import record_assessment
    from src.kb.jev_verdicts import overridden_keys

    def stored_key(link):
        ends = (link["premise"][1], link["hypothesis"][1])
        if link["relation"] in ("duplicate", "contradicts"):
            ends = tuple(sorted(ends))  # same canonical order as claim_links
        return (*ends, link["relation"])

    keys = [stored_key(l) for l, r in reviewed if r is not None]
    keys += [stored_key(l["primary"]) for l, r in reviewed if r is not None and l["primary"] is not None]
    overrides = overridden_keys(conn, keys) if keys else set()
    written = 0
    for link, review in reviewed:
        key = stored_key(link)
        primary_key = stored_key(link if link["primary"] is None else link["primary"])
        if review is not None and (key in overrides or primary_key in overrides) and (
            not review["write"] or review.get("reverse")
        ):
            review = {**review, "action": "override_written", "write": True, "reverse": False}
        if review is not None and link["primary"] is None:
            ends = (link["premise"][1], link["hypothesis"][1])
            if link["relation"] in ("duplicate", "contradicts"):
                ends = tuple(sorted(ends))  # same canonical order as claim_links
            record_assessment(
                conn, run_id, *ends, {**review, "relation": link["relation"]},
            )
        if review is None or review["write"]:
            if review is not None and review.get("reverse") and link["relation"] == "supports":
                link = {**link, "premise": link["hypothesis"], "hypothesis": link["premise"],
                        "method": f"{link['method']}+jev_reversed"}
            written += int(_write_planned(conn, run_id, summary, link))
    return written


def _claim_temporal_context(conn, claim_id: str) -> tuple[str, str | None]:
    """Return source identity and latest source-declared revision class."""

    row = conn.execute(
        "SELECT COALESCE(d.source_id, ''), c.document_id "
        "FROM argument_claims c LEFT JOIN documents d ON d.document_id = c.document_id "
        "WHERE c.claim_id = ?",
        [claim_id],
    ).fetchone()
    if row is None:
        return "", None
    change = None
    exists = conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'document_revisions'"
    ).fetchone()
    if exists is not None:
        revision = conn.execute(
            "SELECT change_class FROM document_revisions WHERE document_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            [row[1]],
        ).fetchone()
        change = revision[0] if revision else None
    return str(row[0]), change


def run_cross_backing_link_pass(
    conn,
    registry: Any,
    provider: Any | None = None,
    nli: Any | None = None,
    run_id: str | None = None,
    embedding_model: str = "all-MiniLM-L6-v2",
    time_window_days: int = 3650,
    k_neighbors: int = 8,
    candidate_threshold: float = 0.5,
    duplicate_threshold: float = 0.88,
    min_link_confidence: float = 0.55,
    jev_reviewer: Any | None = None,
    subject_gate: bool = True,
    allow_unidentified_subjects: bool = True,
    review_cache: bool = True,
    review_chunk_size: int = 200,
    write_chunk_claims: int = 500,
) -> dict[str, Any]:
    """Link namespace-native claims to the shared corpus (depth linkage).

    A lighter pass than intra-corpus linking: only claims that live *solely*
    in a namespace (never seen by ``argument_claims``) are candidates, and
    they bridge into the corpus along embedding similarity. The default time
    window is effectively unbounded — a 2019 paper contradicting today's
    claim is exactly the point.

    Enforces the shared embedding space: a namespace domain declaring a
    different ``embedding_model`` than the corpus domains fails loudly.

    ``jev_reviewer`` reviews proposed links exactly as in
    :func:`run_claim_linking_pass`.
    """
    from src.kb.registry import DomainConfigError
    from src.provisioning.namespaces import (
        BACKEND_ATTACHED,
        BACKEND_TABLE_PREFIX,
        ensure_attached,
        namespace_tables,
    )

    ensure_claim_link_schema(conn)
    models = registry.embedding_models()
    corpus_models = {
        model
        for name, model in models.items()
        if registry.get(name).backing == "corpus-view"
    }
    for definition in registry.domains():
        if (
            definition.backing == "namespace"
            and corpus_models
            and definition.embedding_model not in corpus_models
        ):
            raise DomainConfigError(
                f"domain {definition.name!r} embeds with "
                f"{definition.embedding_model!r} but the corpus uses "
                f"{sorted(corpus_models)}; cross-backing similarity needs one "
                "shared embedding space"
            )

    nli = nli or get_nli_backend()
    run_id = run_id or f"kb-cross-links-{uuid.uuid4().hex[:12]}"
    gate = {"enabled": subject_gate, "allow_unidentified": allow_unidentified_subjects}
    window_ms = time_window_days * 86_400_000

    summary: dict[str, Any] = {
        "run_id": run_id,
        "domains": {},
        "mode": getattr(nli, "prediction_mode", "unknown"),
    }

    for definition in registry.domains():
        if definition.backing != "namespace":
            continue
        backend = (
            BACKEND_ATTACHED
            if definition.namespace_backend == "attached"
            else BACKEND_TABLE_PREFIX
        )
        if backend == BACKEND_ATTACHED:
            ensure_attached(conn, definition.namespace)
        tables = namespace_tables(definition.namespace, backend)

        native = conn.execute(
            f"""
            SELECT n.claim_id, n.claim_text
            FROM {tables['claims']} n
            LEFT JOIN argument_claims shared ON shared.claim_id = n.claim_id
            LEFT JOIN kb_claim_link_scans s ON s.claim_id = n.claim_id
            WHERE shared.claim_id IS NULL AND s.claim_id IS NULL
            ORDER BY n.claim_id
            """
        ).fetchall()

        counts = {"scanned": len(native), "links": 0}
        if native and provider is not None:
            _embed_new_claims(conn, native, provider, embedding_model)

        plans: list[tuple[str, list[dict[str, Any]]]] = []
        scratch: dict[str, Any] = {"links": {r: 0 for r in RELATIONS}}
        for claim_id, claim_text in native:
            candidates = _candidates_for(
                conn, claim_id, claim_text, 0,
                provider is not None, embedding_model,
                window_ms, k_neighbors, candidate_threshold,
            )
            links: list[dict[str, Any]] = []
            for other_id, other_text, other_doc, other_ingested, similarity in candidates:
                links += _plan_pair(
                    conn, nli, scratch,
                    (definition.name, claim_id, claim_text, 0),
                    (
                        _claim_domain(conn, other_doc),
                        other_id, other_text, other_ingested,
                    ),
                    similarity, duplicate_threshold,
                    min_link_confidence, 10**15, gate,
                )
            plans.append((claim_id, links))
        if jev_reviewer is not None:
            summary.setdefault("jev", _new_jev_summary(jev_reviewer))
        reviewed_plans = _review_plans(
            conn, jev_reviewer, run_id, plans, duplicate_threshold, summary,
            review_cache=review_cache, chunk_size=review_chunk_size,
        )

        conn.execute("BEGIN TRANSACTION")
        try:
            for claim_id, reviewed in reviewed_plans:
                before = conn.execute(
                    "SELECT COUNT(*) FROM claim_links WHERE run_id = ?",
                    [run_id],
                ).fetchone()[0]
                _write_reviewed(conn, run_id, scratch, reviewed)
                after = conn.execute(
                    "SELECT COUNT(*) FROM claim_links WHERE run_id = ?",
                    [run_id],
                ).fetchone()[0]
                counts["links"] += max(0, after - before)
                conn.execute(
                    """
                    INSERT INTO kb_claim_link_scans (claim_id, run_id, scanned_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT (claim_id) DO UPDATE SET
                        run_id = excluded.run_id, scanned_at = excluded.scanned_at
                    """,
                    [claim_id, run_id, int(time.time() * 1000)],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        summary["domains"][definition.name] = counts

    return summary


def delete_run(conn, run_id: str) -> dict[str, int]:
    """Revert one run: its links, scan-ledger rows and Jev assessments are removed.

    Claims scanned by the run become unscanned, so the next pass reassesses
    them — deleting a bad model release's run restores the prior state.
    """
    links = conn.execute(
        "SELECT COUNT(*) FROM claim_links WHERE run_id = ?", [run_id]
    ).fetchone()[0]
    scans = conn.execute(
        "SELECT COUNT(*) FROM kb_claim_link_scans WHERE run_id = ?", [run_id]
    ).fetchone()[0]
    conn.execute("DELETE FROM claim_links WHERE run_id = ?", [run_id])
    conn.execute("DELETE FROM kb_claim_link_scans WHERE run_id = ?", [run_id])
    assessments = 0
    if conn.execute(
        "SELECT 1 FROM information_schema.tables"
        " WHERE table_name = 'claim_link_jev_assessments'"
    ).fetchone():
        assessments = conn.execute(
            "SELECT COUNT(*) FROM claim_link_jev_assessments WHERE run_id = ?",
            [run_id],
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM claim_link_jev_assessments WHERE run_id = ?", [run_id]
        )
    return {
        "links_deleted": int(links),
        "scans_deleted": int(scans),
        "jev_assessments_deleted": int(assessments),
    }
