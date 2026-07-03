"""
KG namespacing by table prefix, plus per-namespace document routing (R8 #606).

Decision (from the plan's Track P): a per-KG namespace is a *table prefix*,
``kg_<name>_``. Each deployed KG owns three namespaced tables:

    kg_<name>_documents   routed documents (a copy of the matching corpus rows)
    kg_<name>_entities    entities derived from the routed documents
    kg_<name>_claims      claims scoped to the routed documents

Routing copies only the rows whose ``source`` is bound to the KG out of the
shared corpus (``news_articles``) and the shared ``argument_claims`` layer.
The shared tables are read, never mutated, so a namespace holds *only* routed
documents and the shared corpus is untouched (the #606 exit criterion).

The KG name is validated to a strict ``[a-z][a-z0-9_]*`` shape before it ever
reaches a table identifier, so the prefix can be interpolated into DDL without
opening an injection hole (DuckDB has no bind parameters for identifiers).

Stdlib-only. The connection is injected read-write by the caller.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional, Sequence

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

# Words never counted as entities when deriving them from document titles.
_STOPWORDS = frozenset(
    """a an and are as at be but by for from has have how in into is it its of
    on or that the their them then there these this to us was were what when
    where which will with would new say says said report reports amid over
    after before about""".split()
)

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9'\-]+")


def valid_name(name: str) -> bool:
    """True if ``name`` is a legal KG namespace identifier."""
    return bool(isinstance(name, str) and _NAME_RE.match(name))


def require_valid_name(name: str) -> str:
    """Return ``name`` if legal, else raise ``ValueError`` (no table prefix is
    ever built from an unvalidated string)."""
    if not valid_name(name):
        raise ValueError(
            f"invalid KG name {name!r}: must match [a-z][a-z0-9_]{{1,30}} "
            f"(lowercase letter first, then letters/digits/underscore)"
        )
    return name


def namespace_prefix(name: str) -> str:
    """The table prefix for a KG, e.g. ``kg_climate_`` for ``climate``."""
    return f"kg_{require_valid_name(name)}_"


def namespace_tables(name: str) -> Dict[str, str]:
    """The three namespaced table names for a KG, keyed by role."""
    prefix = namespace_prefix(name)
    return {
        "documents": f"{prefix}documents",
        "entities": f"{prefix}entities",
        "claims": f"{prefix}claims",
    }


def _table_exists(conn, table: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
            [table],
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def create_namespace(conn, name: str) -> Dict[str, str]:
    """Create the three namespaced tables for a KG if absent (idempotent)."""
    tables = namespace_tables(name)
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {tables['documents']} ("
        "id VARCHAR, title VARCHAR, source VARCHAR, source_type VARCHAR, "
        "url VARCHAR, published_at TIMESTAMP, routed_at TIMESTAMP)"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {tables['entities']} ("
        "entity VARCHAR, mentions INTEGER, routed_at TIMESTAMP)"
    )
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {tables['claims']} ("
        "claim_id VARCHAR, claim_text VARCHAR, verdict VARCHAR, "
        "document_id VARCHAR, routed_at TIMESTAMP)"
    )
    return tables


def drop_namespace(conn, name: str) -> None:
    """Drop the three namespaced tables for a KG (used only after archival)."""
    for table in namespace_tables(name).values():
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def archive_namespace(conn, name: str) -> Dict[str, str]:
    """Rename the namespace tables to a ``zz_archived__`` prefix so they stop
    reading as live but are never silently deleted (the teardown guarantee).
    Returns the archived table names."""
    require_valid_name(name)
    archived: Dict[str, str] = {}
    for role, table in namespace_tables(name).items():
        target = f"zz_archived__{table}"
        conn.execute(f"DROP TABLE IF EXISTS {target}")
        if _table_exists(conn, table):
            conn.execute(f"ALTER TABLE {table} RENAME TO {target}")
        archived[role] = target
    return archived


def _derive_entities(titles: Sequence[str], top: int = 25) -> List[Dict[str, Any]]:
    """Cheap entity signal from routed titles: stopword-filtered token
    frequency. Deterministic, dependency-free; the real KG builder replaces
    this once the routing path is wired to it."""
    counter: Counter = Counter()
    for title in titles:
        for word in _WORD_RE.findall(title or ""):
            token = word.lower()
            if len(token) < 3 or token in _STOPWORDS:
                continue
            counter[token] += 1
    return [
        {"entity": token, "mentions": count}
        for token, count in counter.most_common(top)
        if count > 1
    ]


def route_documents(
    conn,
    name: str,
    sources: Sequence[str],
    now: Any,
    backfill_days: Optional[int] = None,
) -> Dict[str, int]:
    """Route documents (and their claims and derived entities) from the shared
    corpus into the KG's namespace tables.

    Only rows whose ``source`` is one of ``sources`` are copied, and a document
    already present in the namespace is skipped, so re-running ingest converges
    rather than duplicating. Returns the counts newly added.
    """
    require_valid_name(name)
    tables = create_namespace(conn, name)
    if not sources:
        return {"documents": 0, "claims": 0, "entities": 0}

    placeholders = ", ".join("?" for _ in sources)
    where = [f"source IN ({placeholders})"]
    params: List[Any] = list(sources)
    if backfill_days and backfill_days > 0 and _has_column(conn, "news_articles", "publish_date"):
        where.append("publish_date >= (CURRENT_TIMESTAMP - INTERVAL (?) DAY)")
        params.append(int(backfill_days))

    existing = {
        r[0]
        for r in conn.execute(f"SELECT id FROM {tables['documents']}").fetchall()
    }
    rows = []
    if _table_exists(conn, "news_articles"):
        rows = conn.execute(
            "SELECT id, title, source, url, publish_date FROM news_articles "
            f"WHERE {' AND '.join(where)}",
            params,
        ).fetchall()

    new_docs = [r for r in rows if r[0] not in existing]
    for r in new_docs:
        conn.execute(
            f"INSERT INTO {tables['documents']} "
            "(id, title, source, source_type, url, published_at, routed_at) "
            "VALUES (?, ?, ?, 'news', ?, ?, ?)",
            [r[0], r[1], r[2], r[3], r[4], now],
        )

    # Claims scoped to the routed documents, from the shared claim layer.
    routed_ids = [r[0] for r in new_docs]
    claim_count = 0
    if routed_ids and _table_exists(conn, "argument_claims"):
        existing_claims = {
            r[0]
            for r in conn.execute(
                f"SELECT claim_id FROM {tables['claims']}"
            ).fetchall()
        }
        cph = ", ".join("?" for _ in routed_ids)
        crows = conn.execute(
            "SELECT claim_id, claim_text, "
            "COALESCE(factcheck_verdict, 'unverified'), document_id "
            f"FROM argument_claims WHERE document_id IN ({cph})",
            routed_ids,
        ).fetchall()
        for cr in crows:
            if cr[0] in existing_claims:
                continue
            conn.execute(
                f"INSERT INTO {tables['claims']} "
                "(claim_id, claim_text, verdict, document_id, routed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [cr[0], cr[1], cr[2], cr[3], now],
            )
            claim_count += 1

    # Rebuild the derived-entity table from the full routed corpus (cheap,
    # keeps the namespace self-consistent after each ingest).
    entity_count = 0
    if new_docs:
        conn.execute(f"DELETE FROM {tables['entities']}")
        all_titles = [
            r[0]
            for r in conn.execute(
                f"SELECT title FROM {tables['documents']}"
            ).fetchall()
        ]
        for ent in _derive_entities(all_titles):
            conn.execute(
                f"INSERT INTO {tables['entities']} (entity, mentions, routed_at) "
                "VALUES (?, ?, ?)",
                [ent["entity"], ent["mentions"], now],
            )
            entity_count += 1

    return {
        "documents": len(new_docs),
        "claims": claim_count,
        "entities": entity_count,
    }


def namespace_counts(conn, name: str) -> Dict[str, int]:
    """Live row counts for a KG's three namespace tables (0 when absent)."""
    tables = namespace_tables(name)
    out: Dict[str, int] = {}
    for role, table in tables.items():
        if _table_exists(conn, table):
            out[role] = int(
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        else:
            out[role] = 0
    return out


def _has_column(conn, table: str, column: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = ? AND column_name = ?",
            [table, column],
        ).fetchall()
        return bool(rows)
    except Exception:
        return False
