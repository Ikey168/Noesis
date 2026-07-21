"""
Consolidation v1: entity canonicalization.

"Fed", "Federal Reserve", and "the central bank" must converge on one
canonical node or entity dossiers, surge detection, and cross-domain joins
all fragment. Same link-don't-merge discipline as claim linking: alias rows
link surface forms to canonical entities; extracted mentions
(``document_actors``) are never rewritten.

Three alias methods, in strength order:

- ``manual``          — user-supplied mappings (including approved merge
  corrections from the KG corrections workflow). Score 1.0; never
  overwritten by automatic passes.
- ``exact-normalize`` — surfaces identical after conservative normalization
  (case, punctuation, whitespace, leading article, corporate suffixes).
- ``similarity``      — near-identical variants above a fuzzy threshold,
  linked only when **unambiguous**: two candidates inside the ambiguity
  margin leave the surface as its own canonical rather than guessing.

Semantic aliasing ("the central bank" → Federal Reserve) is deliberately
out of automatic scope — that is what manual aliases and, later, the
embedding bridge are for. Precision over recall: a wrong merge poisons
every downstream aggregate; an unmerged alias just splits a count.
"""

from __future__ import annotations

import difflib
import re
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

_CANONICAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical_entities (
    canonical_id   TEXT PRIMARY KEY,
    preferred_name TEXT NOT NULL,
    entity_type    TEXT,
    created_at     BIGINT NOT NULL
)
"""

_ALIASES_SCHEMA = """
CREATE TABLE IF NOT EXISTS entity_aliases (
    surface_form TEXT PRIMARY KEY,
    canonical_id TEXT NOT NULL,
    score        DOUBLE NOT NULL,
    method       TEXT NOT NULL,
    run_id       TEXT,
    created_at   BIGINT NOT NULL
)
"""

_SCANS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_entity_scans (
    surface_form TEXT PRIMARY KEY,
    run_id       TEXT,
    scanned_at   BIGINT NOT NULL
)
"""

_CORP_SUFFIXES = {"inc", "corp", "corporation", "ltd", "llc", "plc", "co", "company"}


def ensure_entity_schema(conn) -> None:
    from src.database.local_warehouse_seed import ensure_schema

    ensure_schema(conn)  # document_actors et al.
    conn.execute(_CANONICAL_SCHEMA)
    conn.execute(_ALIASES_SCHEMA)
    conn.execute(_SCANS_SCHEMA)


def normalize_surface(name: str) -> str:
    """Conservative normalization for matching (never for display)."""
    text = name.lower().strip()
    text = re.sub(r"[.’']", "", text)          # U.S. -> us, O'Neil -> oneil
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if text.startswith("the "):
        text = text[4:]
    tokens = text.split()
    while tokens and tokens[-1] in _CORP_SUFFIXES:
        tokens = tokens[:-1]
    return " ".join(tokens)


def _canonical_id(normalized: str) -> str:
    return "ent-" + re.sub(r"\s+", "-", normalized)


def _now() -> int:
    return int(time.time() * 1000)


def _upsert_alias(
    conn,
    surface_norm: str,
    canonical_id: str,
    score: float,
    method: str,
    run_id: Optional[str],
) -> None:
    """Write an alias; manual rows are never displaced by automatic ones."""
    conn.execute(
        """
        INSERT INTO entity_aliases (surface_form, canonical_id, score, method, run_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (surface_form) DO UPDATE SET
            canonical_id = excluded.canonical_id,
            score = excluded.score,
            method = excluded.method,
            run_id = excluded.run_id,
            created_at = excluded.created_at
        WHERE entity_aliases.method <> 'manual' OR excluded.method = 'manual'
        """,
        [surface_norm, canonical_id, round(score, 4), method, run_id, _now()],
    )


def _ensure_canonical(
    conn, preferred_name: str, entity_type: Optional[str] = None
) -> str:
    normalized = normalize_surface(preferred_name)
    canonical_id = _canonical_id(normalized)
    conn.execute(
        """
        INSERT INTO canonical_entities (canonical_id, preferred_name, entity_type, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (canonical_id) DO NOTHING
        """,
        [canonical_id, preferred_name.strip(), entity_type, _now()],
    )
    return canonical_id


def add_manual_alias(
    conn, surface_form: str, canonical_name: str, entity_type: Optional[str] = None
) -> str:
    """Register a user-supplied alias; outranks every automatic link."""
    ensure_entity_schema(conn)
    canonical_id = _ensure_canonical(conn, canonical_name, entity_type)
    _upsert_alias(
        conn, normalize_surface(surface_form), canonical_id, 1.0, "manual", None
    )
    # The canonical's own name always resolves to itself.
    _upsert_alias(
        conn, normalize_surface(canonical_name), canonical_id, 1.0, "manual", None
    )
    return canonical_id


def seed_from_correction_store(conn, store: Any) -> int:
    """Fold approved merge corrections into manual aliases.

    Duck-typed: ``store.list_corrections(...)`` rows with a
    ``merge_duplicates``-style payload (``target``/``source`` names, or ids
    used as names) become manual alias rows — first-class, auto-outranking.
    """
    seeded = 0
    try:
        corrections = store.list_corrections()
    except TypeError:
        corrections = store.list_corrections
    for correction in corrections:
        status = getattr(correction, "status", None)
        ctype = getattr(correction, "correction_type", None)
        payload = getattr(correction, "payload", None) or {}
        if getattr(status, "value", status) != "approved":
            continue
        if "merge" not in str(getattr(ctype, "value", ctype) or ""):
            continue
        target = payload.get("target_name") or payload.get("target_id")
        source = payload.get("source_name") or payload.get("source_id")
        if target and source:
            add_manual_alias(conn, str(source), str(target))
            seeded += 1
    return seeded


def run_entity_canonicalization_pass(
    conn,
    manual_aliases: Optional[Iterable[Tuple[str, str]]] = None,
    run_id: Optional[str] = None,
    similarity_threshold: float = 0.88,
    ambiguity_margin: float = 0.04,
) -> Dict[str, Any]:
    """Canonicalize new ``document_actors`` surfaces; returns a summary.

    ``manual_aliases`` is an iterable of ``(surface, canonical_name)`` pairs
    applied (as method ``manual``) before the automatic pass.
    """
    ensure_entity_schema(conn)
    run_id = run_id or f"kb-entities-{uuid.uuid4().hex[:12]}"

    for surface, canonical_name in manual_aliases or []:
        add_manual_alias(conn, surface, canonical_name)

    new_surfaces = conn.execute(
        """
        SELECT DISTINCT a.actor_name
        FROM document_actors a
        LEFT JOIN kb_entity_scans s
          ON s.surface_form = a.actor_name
        WHERE s.surface_form IS NULL
        ORDER BY a.actor_name
        """
    ).fetchall()

    summary = {
        "run_id": run_id,
        "scanned": len(new_surfaces),
        "linked": {"manual": 0, "exact-normalize": 0, "similarity": 0},
        "new_canonicals": 0,
        "ambiguous": 0,
    }
    if not new_surfaces:
        return summary

    conn.execute("BEGIN TRANSACTION")
    try:
        for (actor_name,) in new_surfaces:
            normalized = normalize_surface(actor_name)
            outcome = _canonicalize_surface(
                conn, actor_name, normalized, run_id,
                similarity_threshold, ambiguity_margin,
            )
            summary_key = outcome[0]
            if summary_key in summary["linked"]:
                summary["linked"][summary_key] += 1
            elif summary_key == "new":
                summary["new_canonicals"] += 1
            elif summary_key == "ambiguous":
                summary["ambiguous"] += 1
            conn.execute(
                """
                INSERT INTO kb_entity_scans (surface_form, run_id, scanned_at)
                VALUES (?, ?, ?)
                ON CONFLICT (surface_form) DO UPDATE SET
                    run_id = excluded.run_id, scanned_at = excluded.scanned_at
                """,
                [actor_name, run_id, _now()],
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return summary


def _canonicalize_surface(
    conn,
    actor_name: str,
    normalized: str,
    run_id: str,
    similarity_threshold: float,
    ambiguity_margin: float,
) -> Tuple[str, str]:
    """Returns (outcome, canonical_id) for one new surface form."""
    if not normalized:
        return ("skipped", "")

    # Already covered (manual alias or previous run under another raw form).
    existing = conn.execute(
        "SELECT canonical_id, method FROM entity_aliases WHERE surface_form = ?",
        [normalized],
    ).fetchone()
    if existing is not None:
        return (existing[1], existing[0])

    # Exact match against existing canonicals' normalized preferred names.
    exact = conn.execute(
        "SELECT canonical_id FROM canonical_entities WHERE canonical_id = ?",
        [_canonical_id(normalized)],
    ).fetchone()
    if exact is not None:
        _upsert_alias(conn, normalized, exact[0], 1.0, "exact-normalize", run_id)
        return ("exact-normalize", exact[0])

    # Fuzzy match against every known normalized surface.
    known = conn.execute(
        "SELECT surface_form, canonical_id FROM entity_aliases"
    ).fetchall()
    scored: List[Tuple[float, str]] = []
    for known_surface, canonical_id in known:
        ratio = difflib.SequenceMatcher(None, normalized, known_surface).ratio()
        if ratio >= similarity_threshold:
            scored.append((ratio, canonical_id))
    scored.sort(reverse=True)

    if scored:
        distinct = {canonical_id for _, canonical_id in scored}
        if len(distinct) > 1 and (scored[0][0] - scored[1][0]) < ambiguity_margin:
            # Two different canonicals are both plausible: do not guess.
            canonical_id = _ensure_canonical(conn, actor_name)
            _upsert_alias(conn, normalized, canonical_id, 0.5, "exact-normalize", run_id)
            return ("ambiguous", canonical_id)
        best_score, best_canonical = scored[0]
        _upsert_alias(conn, normalized, best_canonical, best_score, "similarity", run_id)
        return ("similarity", best_canonical)

    canonical_id = _ensure_canonical(conn, actor_name)
    _upsert_alias(conn, normalized, canonical_id, 1.0, "exact-normalize", run_id)
    return ("new", canonical_id)


# --------------------------------------------------------------------------
# Read helpers: resolution and canonical aggregates
# --------------------------------------------------------------------------

def resolve(conn, surface_form: str) -> Optional[Dict[str, Any]]:
    """Resolve any surface form to its canonical entity (or None)."""
    row = conn.execute(
        """
        SELECT c.canonical_id, c.preferred_name, c.entity_type, a.method, a.score
        FROM entity_aliases a
        JOIN canonical_entities c ON c.canonical_id = a.canonical_id
        WHERE a.surface_form = ?
        """,
        [normalize_surface(surface_form)],
    ).fetchone()
    if row is None:
        return None
    return {
        "canonical_id": row[0],
        "preferred_name": row[1],
        "entity_type": row[2],
        "method": row[3],
        "score": row[4],
    }


def expand(conn, canonical_id: str) -> List[str]:
    """All normalized surface forms linking to a canonical entity."""
    return [
        row[0]
        for row in conn.execute(
            "SELECT surface_form FROM entity_aliases WHERE canonical_id = ?"
            " ORDER BY surface_form",
            [canonical_id],
        ).fetchall()
    ]


def mention_counts(conn, canonical_id: str) -> Dict[str, Any]:
    """Mention totals for a canonical entity across all its aliases.

    Counts come from ``document_actors`` joined through the alias table by
    normalized surface, so every alias's mentions fold into one number with
    the per-alias breakdown attached.
    """
    rows = conn.execute(
        """
        SELECT a.actor_name, COUNT(*)
        FROM document_actors a
        GROUP BY a.actor_name
        """
    ).fetchall()
    aliases = set(expand(conn, canonical_id))
    breakdown = {
        actor_name: int(count)
        for actor_name, count in rows
        if normalize_surface(actor_name) in aliases
    }
    return {
        "canonical_id": canonical_id,
        "total_mentions": sum(breakdown.values()),
        "by_alias": breakdown,
    }
