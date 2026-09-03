"""
Consolidation v1: the claim linking pass.

The corpus is enriched per-document, so the same claim arriving from six
outlets over three days is six ``argument_claims`` rows. This pass turns
that stream into knowledge by adding **links** between claims — never by
rewriting them (link-don't-merge; the merged reading experience is computed
from clusters at presentation time, #966).

Relations: ``duplicate`` (symmetric, canonical id order), ``supports`` /
``contradicts`` (directed, premise → hypothesis), and ``supersedes``
(directed, newer → older duplicate across a time gap; cluster-level
supersedence lands with #966).

Mechanics:

- **Candidates** are generated per new claim against claims whose documents
  fall inside a time window (default ±14 days) — never O(n²) over the
  corpus. With an embedding provider, candidate ranking is cosine over
  claim-text vectors persisted in ``claim_embeddings``; without one, a
  lexical-overlap fallback ranks the same window.
- **Classification** uses the shared NLI backend (:mod:`src.kb.nli`):
  entailment → supports, contradiction → contradicts, high similarity plus
  bidirectional entailment → duplicate. Model weights must be fetched before
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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.kb.nli import CONTRADICTION, ENTAILMENT, get_nli_backend

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

RELATIONS = ("duplicate", "supports", "contradicts", "supersedes")


def ensure_claim_link_schema(conn) -> None:
    from src.database.local_warehouse_seed import ensure_schema
    from src.kb.membership import ensure_membership_schema

    ensure_schema(conn)  # argument_claims + documents (+ compat views)
    ensure_membership_schema(conn)  # document_domains, read for endpoints
    conn.execute(_LINKS_SCHEMA)
    conn.execute(_CLAIM_EMBEDDINGS_SCHEMA)
    conn.execute(_SCANS_SCHEMA)


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
    endpoint_a: Tuple[str, str],
    endpoint_b: Tuple[str, str],
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
    provider: Optional[Any] = None,
    nli: Optional[Any] = None,
    run_id: Optional[str] = None,
    embedding_model: str = "all-MiniLM-L6-v2",
    time_window_days: int = 14,
    k_neighbors: int = 8,
    candidate_threshold: float = 0.5,
    duplicate_threshold: float = 0.88,
    min_link_confidence: float = 0.55,
    supersede_gap_days: int = 3,
) -> Dict[str, Any]:
    """Link new claims to their neighbours; returns a summary.

    ``provider`` embeds claim texts for candidate ranking (``None`` → lexical
    fallback). ``nli`` classifies pair relations (``None`` → the shared
    pretrained backend). Both are injectable for tests.
    """
    ensure_claim_link_schema(conn)
    nli = nli or get_nli_backend()
    run_id = run_id or f"kb-claim-links-{uuid.uuid4().hex[:12]}"
    window_ms = time_window_days * 86_400_000

    new_claims = conn.execute(
        """
        SELECT c.claim_id, c.claim_text, c.document_id,
               COALESCE(d.ingested_at, 0)
        FROM argument_claims c
        LEFT JOIN documents d ON d.document_id = c.document_id
        LEFT JOIN kb_claim_link_scans s ON s.claim_id = c.claim_id
        WHERE s.claim_id IS NULL
        ORDER BY c.claim_id
        """
    ).fetchall()

    summary = {
        "run_id": run_id,
        "scanned": len(new_claims),
        "links": {relation: 0 for relation in RELATIONS},
        "mode": getattr(nli, "prediction_mode", "unknown"),
    }
    if not new_claims:
        return summary

    if provider is not None:
        _embed_new_claims(
            conn,
            [(claim_id, text) for claim_id, text, _, _ in new_claims],
            provider,
            embedding_model,
        )

    conn.execute("BEGIN TRANSACTION")
    try:
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
            if candidates:
                domain = _claim_domain(conn, document_id)
                for other_id, other_text, other_doc, other_ingested, similarity in candidates:
                    _link_pair(
                        conn,
                        nli,
                        run_id,
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
                    )
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
) -> List[Tuple[str, str, str, int, float]]:
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
    summary: Dict[str, Any],
    side_a: Tuple[str, str, str, int],
    side_b: Tuple[str, str, str, int],
    similarity: float,
    duplicate_threshold: float,
    min_link_confidence: float,
    supersede_gap_ms: int,
) -> None:
    domain_a, claim_a, text_a, ingested_a = side_a
    domain_b, claim_b, text_b, ingested_b = side_b
    method = getattr(nli, "name", "unknown")
    mode = getattr(nli, "prediction_mode", "unknown")
    model_version = getattr(nli, "model_version", "unknown")

    forward = nli.classify(text_a, text_b)
    backward = nli.classify(text_b, text_a)

    # Duplicate: high similarity + mutual entailment.
    if (
        similarity >= duplicate_threshold
        and forward.label == ENTAILMENT
        and backward.label == ENTAILMENT
    ):
        confidence = min(forward.confidence, backward.confidence)
        if confidence >= min_link_confidence:
            wrote = _upsert_link(
                conn, (domain_a, claim_a), (domain_b, claim_b), "duplicate",
                similarity, method, mode, confidence, model_version, run_id,
            )
            if wrote:
                summary["links"]["duplicate"] += 1
            if (
                ingested_a and ingested_b
                and abs(ingested_a - ingested_b) >= supersede_gap_ms
            ):
                newer, older = (
                    ((domain_a, claim_a), (domain_b, claim_b))
                    if ingested_a > ingested_b
                    else ((domain_b, claim_b), (domain_a, claim_a))
                )
                if _upsert_link(
                    conn, newer, older, "supersedes", similarity,
                    f"{method}+temporal", mode, confidence, model_version, run_id,
                ):
                    summary["links"]["supersedes"] += 1
            return

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
        return
    premise, hypothesis, result = best
    if result.confidence < min_link_confidence:
        return
    relation = "supports" if result.label == ENTAILMENT else "contradicts"
    if _upsert_link(
        conn, premise, hypothesis, relation, similarity, method, mode,
        result.confidence, model_version, run_id,
    ):
        summary["links"][relation] += 1


def run_cross_backing_link_pass(
    conn,
    registry: Any,
    provider: Optional[Any] = None,
    nli: Optional[Any] = None,
    run_id: Optional[str] = None,
    embedding_model: str = "all-MiniLM-L6-v2",
    time_window_days: int = 3650,
    k_neighbors: int = 8,
    candidate_threshold: float = 0.5,
    duplicate_threshold: float = 0.88,
    min_link_confidence: float = 0.55,
) -> Dict[str, Any]:
    """Link namespace-native claims to the shared corpus (depth linkage).

    A lighter pass than intra-corpus linking: only claims that live *solely*
    in a namespace (never seen by ``argument_claims``) are candidates, and
    they bridge into the corpus along embedding similarity. The default time
    window is effectively unbounded — a 2019 paper contradicting today's
    claim is exactly the point.

    Enforces the shared embedding space: a namespace domain declaring a
    different ``embedding_model`` than the corpus domains fails loudly.
    """
    from src.kb.registry import DomainConfigError
    from src.provisioning.namespaces import (
        BACKEND_ATTACHED,
        BACKEND_TABLE_PREFIX,
        ensure_attached,
        namespace_tables,
    )

    ensure_claim_link_schema(conn)
    nli = nli or get_nli_backend()
    run_id = run_id or f"kb-cross-links-{uuid.uuid4().hex[:12]}"
    window_ms = time_window_days * 86_400_000

    models = registry.embedding_models()
    summary: Dict[str, Any] = {
        "run_id": run_id,
        "domains": {},
        "mode": getattr(nli, "prediction_mode", "unknown"),
    }

    for definition in registry.domains():
        if definition.backing != "namespace":
            continue
        corpus_models = {
            model
            for name, model in models.items()
            if registry.get(name).backing == "corpus-view"
        }
        if corpus_models and definition.embedding_model not in corpus_models:
            raise DomainConfigError(
                f"domain {definition.name!r} embeds with "
                f"{definition.embedding_model!r} but the corpus uses "
                f"{sorted(corpus_models)}; cross-backing similarity needs one "
                "shared embedding space"
            )

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

        conn.execute("BEGIN TRANSACTION")
        try:
            for claim_id, claim_text in native:
                candidates = _candidates_for(
                    conn, claim_id, claim_text, 0,
                    provider is not None, embedding_model,
                    window_ms, k_neighbors, candidate_threshold,
                )
                for other_id, other_text, other_doc, other_ingested, similarity in candidates:
                    before = conn.execute(
                        "SELECT COUNT(*) FROM claim_links WHERE run_id = ?",
                        [run_id],
                    ).fetchone()[0]
                    _link_pair(
                        conn, nli, run_id,
                        {"links": {r: 0 for r in RELATIONS}},
                        (definition.name, claim_id, claim_text, 0),
                        (
                            _claim_domain(conn, other_doc),
                            other_id, other_text, other_ingested,
                        ),
                        similarity, duplicate_threshold,
                        min_link_confidence, 10**15,
                    )
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


def delete_run(conn, run_id: str) -> Dict[str, int]:
    """Revert one run: its links and scan-ledger rows are removed.

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
    return {"links_deleted": int(links), "scans_deleted": int(scans)}
