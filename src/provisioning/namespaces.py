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
from pathlib import Path
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


# Isolation backends (P2 / #640). ``table-prefix`` keeps a KG's tables in the
# shared warehouse (R8 default); ``attached`` gives a KG its own DuckDB file,
# attached under the alias ``kg_<name>`` so its data lives in a separate
# database entirely.
BACKEND_TABLE_PREFIX = "table-prefix"
BACKEND_ATTACHED = "attached"
# M4.3: an external Postgres database attached under the KG's alias via DuckDB's
# postgres extension, so the namespace lives in Postgres rather than a DuckDB
# file. Reuses the attached-backend machinery (alias-qualified tables); the only
# difference is the ATTACH target (a DSN) and TYPE.
BACKEND_POSTGRES = "postgres"
_ROLES = ("documents", "entities", "claims")


def _attached_like(backend: str) -> bool:
    """Backends whose namespace lives in a database attached under the KG alias
    (its own DuckDB file, or an external Postgres), so tables are alias.role."""
    return backend in (BACKEND_ATTACHED, BACKEND_POSTGRES)


def namespace_prefix(name: str) -> str:
    """The table prefix for a KG, e.g. ``kg_climate_`` for ``climate``."""
    return f"kg_{require_valid_name(name)}_"


def attached_alias(name: str) -> str:
    """The attached-database alias for a KG (its own DuckDB), e.g. ``kg_climate``."""
    return f"kg_{require_valid_name(name)}"


def attached_db_path(name: str, base_dir: Optional[str] = None) -> str:
    """The on-disk file for a KG's attached database. Defaults to a
    ``provisioned/`` directory beside the shared warehouse."""
    require_valid_name(name)
    if base_dir is None:
        try:
            from src.config.env import warehouse_path

            base_dir = str(Path(warehouse_path()).resolve().parent / "provisioned")
        except Exception:
            base_dir = str(Path.cwd() / "data" / "provisioned")
    return str(Path(base_dir) / f"kg_{name}.duckdb")


def _is_attached(conn, alias: str) -> bool:
    try:
        rows = conn.execute(
            "SELECT 1 FROM duckdb_databases() WHERE database_name = ?", [alias]
        ).fetchall()
        return bool(rows)
    except Exception:
        return False


def ensure_attached(conn, name: str, db_path: Optional[str] = None) -> str:
    """Attach a KG's own DuckDB file under its alias if not already attached
    in this connection. Returns the alias. The file is created on first attach."""
    alias = attached_alias(name)
    if _is_attached(conn, alias):
        return alias
    path = db_path or attached_db_path(name)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # The alias is derived from a validated name, and the path is quoted; no
    # untrusted input reaches the ATTACH statement.
    conn.execute(f"ATTACH '{path}' AS {alias}")
    return alias


def detach(conn, name: str) -> None:
    """Detach a KG's database if attached (the file / Postgres schema is left in
    place)."""
    alias = attached_alias(name)
    if _is_attached(conn, alias):
        conn.execute(f"DETACH {alias}")


def postgres_dsn(name: str, dsn: Optional[str] = None, tenant: Optional[str] = None) -> str:
    """Resolve the Postgres DSN for a KG's attached database (M4.3). Explicit
    ``dsn`` wins; otherwise a per-tenant ``NOESIS_PROV_PG_DSN_<TENANT>`` env var,
    then the shared ``NOESIS_PROV_PG_DSN``. Raises when none is configured, so a
    Postgres deploy fails clearly rather than silently falling back."""
    require_valid_name(name)
    if dsn:
        return dsn
    import os

    if tenant and tenant != "default":
        suffix = "_" + "".join(c if c.isalnum() else "_" for c in tenant).upper()
        per_tenant = os.getenv("NOESIS_PROV_PG_DSN" + suffix)
        if per_tenant:
            return per_tenant
    shared = os.getenv("NOESIS_PROV_PG_DSN")
    if shared:
        return shared
    raise ValueError(
        "no Postgres DSN configured; set NOESIS_PROV_PG_DSN (or a per-tenant "
        "NOESIS_PROV_PG_DSN_<TENANT>) to use the postgres backend"
    )


def postgres_attach_sql(name: str, dsn: str) -> str:
    """The ATTACH statement for a KG's Postgres database. The alias is derived
    from a validated name; the DSN is quoted."""
    alias = attached_alias(name)
    return f"ATTACH '{dsn}' AS {alias} (TYPE POSTGRES)"


def ensure_attached_postgres(conn, name: str, dsn: str) -> str:
    """Attach a KG's external Postgres database under its alias if not already
    attached in this connection (M4.3). Requires DuckDB's postgres extension."""
    alias = attached_alias(name)
    if _is_attached(conn, alias):
        return alias
    try:
        conn.execute("INSTALL postgres")
        conn.execute("LOAD postgres")
    except Exception:
        pass  # extension may be bundled or preloaded; ATTACH will error if not
    conn.execute(postgres_attach_sql(name, dsn))
    return alias


def namespace_tables(name: str, backend: str = BACKEND_TABLE_PREFIX) -> Dict[str, str]:
    """The three namespaced table references for a KG, keyed by role. For an
    attached backend (own DuckDB file or external Postgres) these are qualified
    by the KG's database alias."""
    if _attached_like(backend):
        alias = attached_alias(name)
        return {role: f"{alias}.{role}" for role in _ROLES}
    prefix = namespace_prefix(name)
    return {role: f"{prefix}{role}" for role in _ROLES}


def _prepare(conn, name: str, backend: str, db_path: Optional[str]) -> Dict[str, str]:
    """Ensure the backend is ready (attach the DB for an attached backend) and
    return the table refs. For ``postgres`` ``db_path`` carries the DSN."""
    if backend == BACKEND_ATTACHED:
        ensure_attached(conn, name, db_path)
    elif backend == BACKEND_POSTGRES:
        ensure_attached_postgres(conn, name, postgres_dsn(name, db_path))
    return namespace_tables(name, backend)


def _table_exists(conn, table: str) -> bool:
    """Whether a table exists. Handles a bare name (shared warehouse) and a
    ``db.table`` reference (an attached backend), which information_schema does
    not see from the main catalog."""
    try:
        if "." in table:
            db, tbl = table.split(".", 1)
            rows = conn.execute(
                "SELECT 1 FROM duckdb_tables() WHERE database_name = ? AND table_name = ?",
                [db, tbl],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
                [table],
            ).fetchall()
        return bool(rows)
    except Exception:
        return False


def create_namespace(
    conn, name: str, backend: str = BACKEND_TABLE_PREFIX, db_path: Optional[str] = None
) -> Dict[str, str]:
    """Create the three namespaced tables for a KG if absent (idempotent). For
    the ``attached`` backend, attaches the KG's own database first."""
    tables = _prepare(conn, name, backend, db_path)
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


def drop_namespace(conn, name: str, backend: str = BACKEND_TABLE_PREFIX) -> None:
    """Drop the three namespaced tables for a KG (used only after archival)."""
    for table in namespace_tables(name, backend).values():
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def archive_namespace(
    conn, name: str, backend: str = BACKEND_TABLE_PREFIX, db_path: Optional[str] = None
) -> Dict[str, str]:
    """Archive a KG's namespace without deleting it (the teardown guarantee).

    For ``table-prefix`` the tables are renamed aside under ``zz_archived__``;
    for an attached backend the database is detached and its file / Postgres
    schema left in place (never dropped)."""
    require_valid_name(name)
    if _attached_like(backend):
        if backend == BACKEND_POSTGRES:
            target = db_path or "postgres"
        else:
            target = db_path or attached_db_path(name)
        detach(conn, name)
        return {"detached_db": target}
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
    backend: str = BACKEND_TABLE_PREFIX,
    db_path: Optional[str] = None,
) -> Dict[str, int]:
    """Route documents (and their claims and derived entities) from the shared
    corpus into the KG's namespace tables.

    Only rows whose ``source`` is one of ``sources`` are copied, and a document
    already present in the namespace is skipped, so re-running ingest converges
    rather than duplicating. Returns the counts newly added. For the
    ``attached`` backend the rows land in the KG's own database, while the read
    of the shared corpus stays in the main warehouse (a cross-database copy).
    """
    require_valid_name(name)
    tables = create_namespace(conn, name, backend, db_path)
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


def namespace_sample(
    conn,
    name: str,
    docs: int = 6,
    entities: int = 12,
    claims: int = 6,
    backend: str = BACKEND_TABLE_PREFIX,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """The scoped panel family for a KG namespace: a sample of routed
    documents, the top derived entities, and a sample of scoped claims. This
    is what a per-namespace ``documents`` / ``entity_graph`` / ``claims`` view
    reads (the R9 scoped family)."""
    tables = _prepare(conn, name, backend, db_path)
    out: Dict[str, Any] = {"documents": [], "entities": [], "claims": []}
    if _table_exists(conn, tables["documents"]):
        rows = conn.execute(
            f"SELECT id, title, source, source_type, published_at "
            f"FROM {tables['documents']} ORDER BY published_at DESC NULLS LAST "
            f"LIMIT ?",
            [int(docs)],
        ).fetchall()
        out["documents"] = [
            {
                "id": r[0],
                "title": r[1],
                "source": r[2],
                "source_type": r[3],
                "published_at": str(r[4]) if r[4] is not None else None,
            }
            for r in rows
        ]
    if _table_exists(conn, tables["entities"]):
        rows = conn.execute(
            f"SELECT entity, mentions FROM {tables['entities']} "
            f"ORDER BY mentions DESC LIMIT ?",
            [int(entities)],
        ).fetchall()
        out["entities"] = [{"entity": r[0], "mentions": int(r[1])} for r in rows]
    if _table_exists(conn, tables["claims"]):
        rows = conn.execute(
            f"SELECT claim_id, claim_text, verdict FROM {tables['claims']} "
            f"LIMIT ?",
            [int(claims)],
        ).fetchall()
        out["claims"] = [
            {"claim_id": r[0], "text": (r[1] or "")[:180], "verdict": r[2]}
            for r in rows
        ]
    return out


def namespace_counts(
    conn, name: str, backend: str = BACKEND_TABLE_PREFIX, db_path: Optional[str] = None
) -> Dict[str, int]:
    """Live row counts for a KG's three namespace tables (0 when absent)."""
    tables = _prepare(conn, name, backend, db_path)
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
