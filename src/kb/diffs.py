"""
``diff(domain, since)``: the change-feed primitive every application reduces to.

The daily brief, alerting, and any sync-style consumer all ask the same
question — "what changed in this domain since T" — and this module answers
it from **consolidation outputs**, not raw documents: a re-reported story is
a cluster gaining a source, not five "new" items.

Sections (every analytic entry cited, with ``prediction_mode``/confidence
where a model produced it):

- ``documents``      — new arrivals + which sources delivered + the total,
  so "nothing new" is distinguishable from "nothing ingested".
- ``new_clusters``   — claim clusters whose *first* member arrived after T.
- ``gained_corroboration`` — pre-existing clusters that picked up new
  sources after T (the new sources are named).
- ``new_contradictions``   — contradicts-links created after T touching the
  domain, both sides cited.
- ``superseded``     — claims superseded after T (marked historical, never
  dropped).
- ``entity_surges``  — canonical-entity mention rates in the window versus a
  trailing baseline window of equal length.
- ``meta``           — as-of / since timestamps and the consolidation run
  watermarks the answer was computed against. Passes commit
  transactionally, so a diff never observes a half-finished run.

Ranking is the consumer's job; every entry carries the signals
(corroboration, recency, sources) a ranker needs. Length budgets live in
the application (#960), not here.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.kb.entities import normalize_surface


def _watermarks(conn) -> Dict[str, Any]:
    marks: Dict[str, Any] = {}
    for key, sql in (
        (
            "membership",
            "SELECT last_run_id FROM kb_membership_state"
            " ORDER BY updated_at DESC LIMIT 1",
        ),
        (
            "claim_links",
            "SELECT run_id FROM claim_links ORDER BY created_at DESC LIMIT 1",
        ),
        (
            "clusters",
            "SELECT run_id FROM claim_clusters ORDER BY assigned_at DESC LIMIT 1",
        ),
    ):
        try:
            row = conn.execute(sql).fetchone()
            marks[key] = row[0] if row else None
        except Exception:
            marks[key] = None
    return marks


def _domain_claim_ids(conn, domain: str) -> set:
    return {
        row[0]
        for row in conn.execute(
            """
            SELECT c.claim_id FROM argument_claims c
            JOIN document_domains m
              ON m.document_id = c.document_id AND m.domain = ?
            """,
            [domain],
        ).fetchall()
    }


def _link_entries(
    conn, domain: str, relation: str, since_ms: int, claim_ids: set
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT l.claim_a, l.domain_a, l.claim_b, l.domain_b,
               l.confidence, l.prediction_mode, l.created_at,
               a.claim_text, b.claim_text,
               a.document_id, da.source_id, da.url,
               b.document_id, db.source_id, db.url
        FROM claim_links l
        LEFT JOIN argument_claims a ON a.claim_id = l.claim_a
        LEFT JOIN argument_claims b ON b.claim_id = l.claim_b
        LEFT JOIN documents da ON da.document_id = a.document_id
        LEFT JOIN documents db ON db.document_id = b.document_id
        WHERE l.relation = ? AND l.created_at >= ?
        """,
        [relation, since_ms],
    ).fetchall()
    entries = []
    for (claim_a, domain_a, claim_b, domain_b, confidence, mode,
         created_at, text_a, text_b, doc_a, source_a, url_a,
         doc_b, source_b, url_b) in rows:
        if (
            claim_a not in claim_ids
            and claim_b not in claim_ids
            and domain_a != domain
            and domain_b != domain
        ):
            continue
        entries.append(
            {
                "claim_a": {"claim_id": claim_a, "domain": domain_a, "text": text_a,
                            "document_id": doc_a, "source": source_a, "url": url_a,
                            "cited": bool(doc_a)},
                "claim_b": {"claim_id": claim_b, "domain": domain_b, "text": text_b,
                            "document_id": doc_b, "source": source_b, "url": url_b,
                            "cited": bool(doc_b)},
                "confidence": confidence,
                "prediction_mode": mode,
                "created_at_ms": created_at,
            }
        )
    return entries


def _entity_surges(
    conn, domain: str, since_ms: int, as_of_ms: int
) -> List[Dict[str, Any]]:
    window = max(as_of_ms - since_ms, 1)
    baseline_start = since_ms - window
    rows = conn.execute(
        """
        SELECT a.actor_name, COALESCE(d.ingested_at, 0)
        FROM document_actors a
        JOIN document_domains m
          ON m.document_id = a.document_id AND m.domain = ?
        JOIN documents d ON d.document_id = a.document_id
        WHERE COALESCE(d.ingested_at, 0) >= ?
        """,
        [domain, baseline_start],
    ).fetchall()
    if not rows:
        return []

    aliases = dict(
        conn.execute(
            "SELECT surface_form, canonical_id FROM entity_aliases"
        ).fetchall()
    )
    names = dict(
        conn.execute(
            "SELECT canonical_id, preferred_name FROM canonical_entities"
        ).fetchall()
    )

    recent: Dict[str, int] = {}
    baseline: Dict[str, int] = {}
    for actor_name, ingested_at in rows:
        normalized = normalize_surface(actor_name)
        canonical = aliases.get(normalized, f"raw:{normalized}")
        bucket = recent if ingested_at >= since_ms else baseline
        bucket[canonical] = bucket.get(canonical, 0) + 1

    surges = []
    for canonical, count in recent.items():
        base = baseline.get(canonical, 0)
        if count >= 3 and count > 2 * base:
            evidence_rows = conn.execute(
                """SELECT DISTINCT d.document_id, d.source_id, d.url
                   FROM document_actors a JOIN documents d ON d.document_id=a.document_id
                   JOIN document_domains m ON m.document_id=d.document_id AND m.domain=?
                   WHERE lower(a.actor_name)=lower(?) AND COALESCE(d.ingested_at,0)>=?
                   LIMIT 10""",
                [domain, names.get(canonical, canonical.removeprefix("raw:")), since_ms],
            ).fetchall()
            surges.append(
                {
                    "canonical_id": canonical,
                    "name": names.get(canonical, canonical.removeprefix("raw:")),
                    "mentions": count,
                    "baseline_mentions": base,
                    "prediction_mode": "counting",
                    "confidence": None,
                    "evidence": [
                        {"document_id": row[0], "source": row[1] or "unknown",
                         "url": row[2], "path": row[0], "cited": True}
                        for row in evidence_rows
                    ],
                }
            )
    surges.sort(key=lambda surge: surge["mentions"], reverse=True)
    return surges


def _stance_shifts(conn, domain: str, since_ms: int) -> List[Dict[str, Any]]:
    """Stance changes for sources that delivered documents to this domain."""
    try:
        rows = conn.execute(
            """SELECT s.source, s.source_type, s.topic, s.from_stance, s.to_stance,
                      s.confidence_delta, s.prediction_mode, s.detected_at
               FROM stance_drift_events s
               WHERE s.detected_at IS NOT NULL
                 AND s.source IN (
                   SELECT DISTINCT d.source_id FROM documents d
                   JOIN document_domains m ON m.document_id=d.document_id AND m.domain=?
                 )
               ORDER BY s.detected_at DESC""",
            [domain],
        ).fetchall()
    except Exception:
        return []
    shifts = []
    for row in rows:
        detected = row[7]
        try:
            detected_ms = int(datetime.fromisoformat(str(detected).replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            detected_ms = 0
        if detected_ms < since_ms:
            continue
        evidence_rows = conn.execute(
            """SELECT d.document_id, d.source_id, d.url FROM documents d
               JOIN document_domains m ON m.document_id=d.document_id AND m.domain=?
               WHERE d.source_id=? ORDER BY d.ingested_at DESC LIMIT 5""",
            [domain, row[0]],
        ).fetchall()
        shifts.append({
            "source": row[0], "source_type": row[1], "topic": row[2],
            "from_stance": row[3], "to_stance": row[4],
            "confidence": row[5], "prediction_mode": row[6] or "unknown",
            "detected_at": str(row[7]), "detected_at_ms": detected_ms,
            "evidence": [
                {"document_id": ev[0], "source": ev[1] or "unknown", "url": ev[2],
                 "path": ev[0], "cited": True} for ev in evidence_rows
            ],
        })
    return shifts


def compute_corpus_diff(
    conn, domain: str, since_ms: int
) -> Dict[str, Any]:
    """Corpus-view diff: all six sections. See the module docstring."""
    from src.kb.clusters import cluster_claims, ensure_cluster_schema
    from src.kb.entities import ensure_entity_schema

    ensure_cluster_schema(conn)
    ensure_entity_schema(conn)
    as_of_ms = int(time.time() * 1000)

    total, new_documents = conn.execute(
        """
        SELECT COUNT(*),
               COALESCE(SUM(CASE WHEN COALESCE(d.ingested_at, 0) >= ?
                                 THEN 1 ELSE 0 END), 0)
        FROM documents d
        JOIN document_domains m
          ON m.document_id = d.document_id AND m.domain = ?
        """,
        [since_ms, domain],
    ).fetchone()
    sources = dict(
        conn.execute(
            """
            SELECT COALESCE(d.source_id, ''), COUNT(*)
            FROM documents d
            JOIN document_domains m
              ON m.document_id = d.document_id AND m.domain = ?
            WHERE COALESCE(d.ingested_at, 0) >= ?
            GROUP BY 1 ORDER BY 2 DESC
            """,
            [domain, since_ms],
        ).fetchall()
    )

    clusters = cluster_claims(conn, domain=domain, limit=100_000)
    new_clusters, gained = [], []
    for cluster in clusters:
        arrivals = [c["ingested_at"] for c in cluster["citations"]]
        first_seen = min(arrivals) if arrivals else 0
        if first_seen >= since_ms:
            new_clusters.append(cluster)
        else:
            new_citations = [
                c for c in cluster["citations"] if c["ingested_at"] >= since_ms
            ]
            new_sources = {
                str(c["source"]).strip().lower() for c in new_citations if c["source"]
            } - {
                str(c["source"]).strip().lower()
                for c in cluster["citations"]
                if c["source"] and c["ingested_at"] < since_ms
            }
            if new_sources:
                gained.append(
                    {**cluster, "new_sources": sorted(new_sources)}
                )

    claim_ids = _domain_claim_ids(conn, domain)
    contradictions = _link_entries(conn, domain, "contradicts", since_ms, claim_ids)
    superseded = _link_entries(conn, domain, "supersedes", since_ms, claim_ids)
    integrity_ids = [row[0] for row in conn.execute(
        """SELECT d.document_id FROM documents d JOIN document_domains m
           ON m.document_id=d.document_id AND m.domain=?
           WHERE COALESCE(d.ingested_at, 0) >= ?
           ORDER BY d.ingested_at DESC LIMIT 100""",
        [domain, since_ms],
    ).fetchall()]
    from src.integrity.ledger import integrity_ledger
    integrity = integrity_ledger(conn, integrity_ids, limit=100)

    return {
        "domain": domain,
        "documents": {
            "new": int(new_documents),
            "total": int(total),
            "sources_delivered": sources,
        },
        "new_clusters": new_clusters,
        "gained_corroboration": gained,
        "new_contradictions": contradictions,
        "superseded": [
            {**entry, "superseded_claim": entry["claim_b"]} for entry in superseded
        ],
        "entity_surges": _entity_surges(conn, domain, since_ms, as_of_ms),
        "stance_shifts": _stance_shifts(conn, domain, since_ms),
        "integrity": integrity,
        "meta": {
            "as_of_ms": as_of_ms,
            "since_ms": since_ms,
            "consolidation": _watermarks(conn),
        },
    }


def compute_namespace_diff(
    conn, definition: Any, since_ms: int
) -> Dict[str, Any]:
    """Namespace diff: same shape, honest gaps.

    Entity surges are ``None`` (namespace entity tables carry no mention
    timeline) rather than silently empty; cluster sections reduce to new
    native claims plus link activity, which is what consolidation tracks
    for a namespace today.
    """
    from src.provisioning.namespaces import (
        BACKEND_ATTACHED,
        BACKEND_TABLE_PREFIX,
        ensure_attached,
        namespace_tables,
        _has_column,
    )
    from src.kb.claim_links import ensure_claim_link_schema

    ensure_claim_link_schema(conn)
    as_of_ms = int(time.time() * 1000)
    backend = (
        BACKEND_ATTACHED
        if definition.namespace_backend == "attached"
        else BACKEND_TABLE_PREFIX
    )
    if backend == BACKEND_ATTACHED:
        ensure_attached(conn, definition.namespace)
    tables = namespace_tables(definition.namespace, backend)

    docs = tables["documents"]
    arrival = (
        "COALESCE(ingested_at, epoch_ms(routed_at), 0)"
        if _has_column(conn, docs, "ingested_at")
        else "COALESCE(epoch_ms(routed_at), 0)"
    )
    total, new_documents = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {arrival} >= ? THEN 1 ELSE 0 END), 0)"
        f" FROM {docs}",
        [since_ms],
    ).fetchone()
    sources = dict(
        conn.execute(
            f"SELECT COALESCE(source, ''), COUNT(*) FROM {docs}"
            f" WHERE {arrival} >= ? GROUP BY 1 ORDER BY 2 DESC",
            [since_ms],
        ).fetchall()
    )

    new_claims = [
        {"claim_id": row[0], "claim_text": row[1], "document_id": row[2]}
        for row in conn.execute(
            f"SELECT claim_id, claim_text, document_id FROM {tables['claims']}"
            f" WHERE COALESCE(epoch_ms(routed_at), 0) >= ? ORDER BY claim_id",
            [since_ms],
        ).fetchall()
    ]
    native_ids = {
        row[0]
        for row in conn.execute(
            f"SELECT claim_id FROM {tables['claims']}"
        ).fetchall()
    }
    contradictions = _link_entries(
        conn, definition.name, "contradicts", since_ms, native_ids
    )
    superseded = _link_entries(
        conn, definition.name, "supersedes", since_ms, native_ids
    )

    return {
        "domain": definition.name,
        "documents": {
            "new": int(new_documents),
            "total": int(total),
            "sources_delivered": sources,
        },
        "new_clusters": [
            {
                "cluster_id": f"cl-{claim['claim_id']}",
                "representative": claim,
                "citations": [claim],
                "corroboration": 1,
                "size": 1,
            }
            for claim in new_claims
        ],
        "gained_corroboration": [],
        "new_contradictions": contradictions,
        "superseded": [
            {**entry, "superseded_claim": entry["claim_b"]} for entry in superseded
        ],
        "entity_surges": None,  # no mention timeline in namespace entity tables
        "stance_shifts": None,
        "integrity": {
            "n": 0,
            "method": "integrity ledger aggregation v1",
            "assumptions": ["namespace backing does not yet retain integrity source tables"],
            "documents": [], "document_count": 0, "findings": [], "finding_count": 0,
        },
        "meta": {
            "as_of_ms": as_of_ms,
            "since_ms": since_ms,
            "consolidation": _watermarks(conn),
        },
    }
