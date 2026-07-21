"""
Document → domain membership: the corpus-view backing's data spine.

Membership is **data, not a per-query filter**: a consolidation pass assigns
documents to domains and stores the assignment in ``document_domains``, so
per-domain views stay cheap and every assignment is auditable (method, score,
run id). A document can belong to several domains — that is the point of
views over namespaces.

Three assignment methods, strongest kept per (document, domain):

- ``source``    — the document arrived through a feed/connector tagged with
  the domain's tags (score 1.0; provenance is the subscription itself).
- ``keyword``   — the domain's seed vocabulary matches the title/content
  (score scales with distinct keyword hits; 2+ hits clear the default
  threshold, a single hit does not — precision over recall, the embedding
  path supplies recall).
- ``embedding`` — cosine similarity between the document's stored vector and
  the mean of the domain's anchor embeddings, computed in the domain's
  declared embedding space. Skipped (never guessed) when the stored vector's
  model does not match the domain's ``embedding_model``.

Incrementality is set-based, not timestamp-based: a scan ledger
(``kb_membership_scans``) records which (document, domain) pairs have been
assessed, so out-of-order ingestion, payload-supplied timestamps, and
``ingested_at = 0`` legacy rows can never be silently skipped. A document
assessed before its embedding vector existed stays ``embedding_pending`` and
is re-assessed once the vector (and an anchor provider) is available.

A config fingerprint triggers a full rebuild of a domain's rows when its
definition changes; the rebuild computes anchors *before* any destructive
work and runs delete + reassign inside one transaction, so a failed rebuild
leaves the previous assignments intact.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from src.kb.registry import DomainDefinition, KnowledgeDomainRegistry

_MEMBERSHIP_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_domains (
    document_id TEXT NOT NULL,
    domain      TEXT NOT NULL,
    score       DOUBLE NOT NULL,
    method      TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    assigned_at BIGINT NOT NULL,
    PRIMARY KEY (document_id, domain)
)
"""

_SCANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_membership_scans (
    document_id       TEXT NOT NULL,
    domain            TEXT NOT NULL,
    embedding_pending BOOLEAN NOT NULL,
    run_id            TEXT,
    scanned_at        BIGINT NOT NULL,
    PRIMARY KEY (document_id, domain)
)
"""

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_membership_state (
    domain             TEXT PRIMARY KEY,
    config_fingerprint TEXT NOT NULL,
    last_run_id        TEXT,
    updated_at         BIGINT NOT NULL
)
"""

#: single keyword hit ≈ 0.34: below the default 0.35 threshold by design.
_KEYWORD_HIT_SCORE = 0.34


def ensure_membership_schema(conn) -> None:
    """Create the membership tables plus every table the pass reads.

    The pass joins ``documents`` and ``document_embeddings``, so their
    schemas are ensured here too — a warehouse that has never run the
    embedding pass must not crash the membership pass.
    """
    from src.database.news_articles_compat import ensure_documents_schema
    from src.ingestion.embedding_store import EmbeddingStore

    ensure_documents_schema(conn)
    EmbeddingStore(conn)
    conn.execute(_MEMBERSHIP_SCHEMA)
    conn.execute(_SCANS_SCHEMA)
    conn.execute(_STATE_SCHEMA)


def _fingerprint(definition: DomainDefinition) -> str:
    payload = json.dumps(
        {
            "tags": sorted(definition.tags),
            "keywords": sorted(definition.keywords),
            "anchors": list(definition.embedding_anchors),
            "threshold": definition.membership_threshold,
            "embedding_model": definition.embedding_model,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _domain_state(conn, domain: str):
    return conn.execute(
        "SELECT config_fingerprint FROM kb_membership_state WHERE domain = ?",
        [domain],
    ).fetchone()


def _upsert_assignment(
    conn, document_id: str, domain: str, score: float, method: str, run_id: str
) -> None:
    conn.execute(
        """
        INSERT INTO document_domains (document_id, domain, score, method, run_id, assigned_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (document_id, domain) DO UPDATE SET
            score = excluded.score,
            method = excluded.method,
            run_id = excluded.run_id,
            assigned_at = excluded.assigned_at
        WHERE excluded.score > document_domains.score
        """,
        [document_id, domain, score, method, run_id, int(time.time() * 1000)],
    )


def _upsert_scan(
    conn, document_id: str, domain: str, embedding_pending: bool, run_id: str
) -> None:
    conn.execute(
        """
        INSERT INTO kb_membership_scans
            (document_id, domain, embedding_pending, run_id, scanned_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (document_id, domain) DO UPDATE SET
            embedding_pending = excluded.embedding_pending,
            run_id = excluded.run_id,
            scanned_at = excluded.scanned_at
        """,
        [document_id, domain, embedding_pending, run_id, int(time.time() * 1000)],
    )


def _document_tags(metadata_json: Optional[str]) -> List[str]:
    if not metadata_json:
        return []
    try:
        metadata = json.loads(metadata_json)
    except (TypeError, ValueError):
        return []
    tags = metadata.get("tags") if isinstance(metadata, dict) else None
    if isinstance(tags, list):
        return [str(tag).lower() for tag in tags]
    return []


def _keyword_hits(text: str, keywords: List[str]) -> int:
    hits = 0
    for keyword in keywords:
        if re.search(rf"(?<!\w){re.escape(keyword.lower())}(?!\w)", text):
            hits += 1
    return hits


def _cosine(a: List[float], b: List[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


def _anchor_vector(
    definition: DomainDefinition, provider: Any
) -> Optional[List[float]]:
    if not definition.embedding_anchors or provider is None:
        return None
    vectors = provider.embed_texts(list(definition.embedding_anchors))
    rows = [list(map(float, row)) for row in vectors]
    if not rows:
        return None
    dim = len(rows[0])
    return [sum(row[i] for row in rows) / len(rows) for i in range(dim)]


def run_membership_pass(
    conn,
    registry: Optional[KnowledgeDomainRegistry] = None,
    provider: Optional[Any] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Assess unscanned documents for every corpus-view domain.

    ``provider`` is the embedding provider used for domain *anchor* texts
    only (document vectors come from the embeddings sink). Passing ``None``
    disables the embedding method for this run; affected documents stay
    ``embedding_pending`` and are re-assessed on a later run.

    Intended to run inside the warehouse-owning process (the same rule as
    every shared-connection consumer); callers running it as a separate job
    must inject their own connection.
    """
    from src.kb.registry import load_registry

    registry = registry or load_registry()
    run_id = run_id or f"kb-membership-{uuid.uuid4().hex[:12]}"
    ensure_membership_schema(conn)

    summary: Dict[str, Any] = {"run_id": run_id, "domains": {}}
    for definition in registry.domains():
        if definition.backing != "corpus-view":
            continue
        summary["domains"][definition.name] = _run_domain(
            conn, definition, provider, run_id
        )
    return summary


def _run_domain(
    conn, definition: DomainDefinition, provider: Any, run_id: str
) -> Dict[str, Any]:
    fingerprint = _fingerprint(definition)
    state = _domain_state(conn, definition.name)
    rebuild = state is not None and state[0] != fingerprint

    # Anchor embedding is the only fallible external call — do it before any
    # destructive work so a provider failure cannot leave the domain empty.
    anchor = _anchor_vector(definition, provider)
    domain_tags = {tag.lower() for tag in definition.tags}
    keywords = [keyword for keyword in definition.keywords if keyword.strip()]
    has_anchors = bool(definition.embedding_anchors)

    counts = {"source": 0, "keyword": 0, "embedding": 0, "scanned": 0}

    conn.execute("BEGIN TRANSACTION")
    try:
        if rebuild:
            conn.execute(
                "DELETE FROM document_domains WHERE domain = ?", [definition.name]
            )
            conn.execute(
                "DELETE FROM kb_membership_scans WHERE domain = ?", [definition.name]
            )

        # Set-based candidates: never-scanned documents, plus previously
        # scanned ones whose embedding assessment is still pending.
        rows = conn.execute(
            """
            SELECT d.document_id, COALESCE(d.title, ''), COALESCE(d.content, ''),
                   d.metadata, e.vector
            FROM documents d
            LEFT JOIN kb_membership_scans s
              ON s.document_id = d.document_id AND s.domain = ?
            LEFT JOIN document_embeddings e
              ON e.document_id = d.document_id AND e.model = ?
            WHERE s.document_id IS NULL OR s.embedding_pending
            """,
            [definition.name, definition.embedding_model],
        ).fetchall()

        for document_id, title, content, metadata_json, vector_json in rows:
            assigned = False

            if domain_tags and domain_tags & set(_document_tags(metadata_json)):
                _upsert_assignment(
                    conn, document_id, definition.name, 1.0, "source", run_id
                )
                counts["source"] += 1
                assigned = True

            if not assigned and keywords:
                hits = _keyword_hits(f"{title}\n{content}".lower(), keywords)
                score = min(1.0, hits * _KEYWORD_HIT_SCORE)
                if hits and score >= definition.membership_threshold:
                    _upsert_assignment(
                        conn, document_id, definition.name, score, "keyword", run_id
                    )
                    counts["keyword"] += 1
                    assigned = True

            embedding_assessed = False
            if not assigned and anchor is not None and vector_json:
                embedding_assessed = True
                similarity = _cosine(anchor, json.loads(vector_json))
                if similarity >= definition.membership_threshold:
                    _upsert_assignment(
                        conn,
                        document_id,
                        definition.name,
                        round(similarity, 6),
                        "embedding",
                        run_id,
                    )
                    counts["embedding"] += 1
                    assigned = True

            # Pending: the embedding method could still change the outcome —
            # the domain has anchors but this run lacked a vector or provider.
            embedding_pending = (
                has_anchors and not assigned and not embedding_assessed
            )
            _upsert_scan(
                conn, document_id, definition.name, embedding_pending, run_id
            )

        conn.execute(
            """
            INSERT INTO kb_membership_state
                (domain, config_fingerprint, last_run_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (domain) DO UPDATE SET
                config_fingerprint = excluded.config_fingerprint,
                last_run_id = excluded.last_run_id,
                updated_at = excluded.updated_at
            """,
            [definition.name, fingerprint, run_id, int(time.time() * 1000)],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    counts["scanned"] = len(rows)
    return counts


def view_name(domain: str) -> str:
    """Stable per-domain view name (slug dashes become underscores)."""
    return f"kb_domain_{domain.replace('-', '_')}"


def ensure_domain_views(conn, registry: Optional[KnowledgeDomainRegistry] = None) -> List[str]:
    """Create/refresh one view per corpus-view domain; return view names.

    Domain names are registry-validated slugs (``[a-z0-9-]``), which is what
    makes the f-string interpolation below safe.
    """
    from src.kb.registry import load_registry

    registry = registry or load_registry()
    ensure_membership_schema(conn)

    created: List[str] = []
    for definition in registry.domains():
        if definition.backing != "corpus-view":
            continue
        name = view_name(definition.name)
        conn.execute(
            f"""
            CREATE OR REPLACE VIEW {name} AS
            SELECT
                d.document_id,
                d.source_type,
                d.source_id,
                d.url,
                d.title,
                d.content,
                d.language,
                d.authors,
                d.metadata,
                d.ingested_at,
                d.created_at,
                m.score  AS domain_score,
                m.method AS domain_method,
                e.sentiment_score,
                e.sentiment_label
            FROM documents d
            JOIN document_domains m
              ON m.document_id = d.document_id AND m.domain = '{definition.name}'
            LEFT JOIN document_enrichments e
              ON e.document_id = d.document_id
            """
        )
        created.append(name)
    return created
