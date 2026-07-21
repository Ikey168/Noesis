"""
Backing promotion: a registry pointer flip plus a data migration.

``corpus-view → namespace`` copies the domain's member documents and claims
into the provisioning plane's namespaced tables **preserving ids**, so every
inbound link (``claim_links`` endpoints, ``document_domains`` provenance)
survives; then the domain's entry in ``config/domains.yml`` flips to
``backing: namespace``. The shared corpus is never mutated — the rows stay
where they were (link-don't-merge), the domain simply stops being *served*
from them. The reverse (``namespace → corpus-view``) upserts any
namespace-native rows back into the shared sink and flips the pointer back.

Because consumers only ever hold the ``DomainBacking`` interface, nothing
downstream changes on either flip.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from src.kb.registry import (
    DomainConfigError,
    KnowledgeDomainRegistry,
    load_registry,
)

_EXTENDED_COLUMNS = (
    ("content", "VARCHAR"),
    ("ingested_at", "BIGINT"),
)


def _flip_config(config_path: Path, domain: str, updates: Dict[str, Any]) -> None:
    raw = yaml.safe_load(config_path.read_text())
    for entry in raw.get("domains", []):
        if entry.get("name") == domain:
            for key, value in updates.items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            break
    else:
        raise DomainConfigError(f"domain {domain!r} not in {config_path}")
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))


def _extend_documents_table(conn, table: str) -> None:
    from src.provisioning.namespaces import _has_column

    for column, column_type in _EXTENDED_COLUMNS:
        if not _has_column(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def promote_to_namespace(
    conn,
    domain: str,
    config_path: Path,
    backend: str = "table-prefix",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Promote a corpus-view domain to a provisioned namespace."""
    from src.provisioning.namespaces import (
        create_namespace,
        require_valid_name,
    )
    from src.kb.membership import view_name

    registry = load_registry(config_path)
    definition = registry.get(domain)
    if definition.backing != "corpus-view":
        raise DomainConfigError(f"domain {domain!r} is not corpus-view backed")

    from src.database.local_warehouse_seed import ensure_schema

    ensure_schema(conn)  # argument_claims and friends on a fresh warehouse
    namespace = require_valid_name(domain.replace("-", "_"))
    tables = create_namespace(conn, namespace, backend, db_path)
    _extend_documents_table(conn, tables["documents"])

    conn.execute("BEGIN TRANSACTION")
    try:
        copied_docs = conn.execute(
            f"""
            INSERT INTO {tables['documents']}
                (id, title, source, source_type, url, published_at, routed_at,
                 content, ingested_at)
            SELECT d.document_id, d.title, d.source_id, d.source_type, d.url,
                   CASE WHEN d.created_at IS NULL THEN NULL
                        ELSE to_timestamp(d.created_at / 1000.0) END,
                   now(), d.content, d.ingested_at
            FROM documents d
            JOIN document_domains m
              ON m.document_id = d.document_id AND m.domain = ?
            WHERE d.document_id NOT IN (SELECT id FROM {tables['documents']})
            """,
            [domain],
        ).fetchone()
        copied_claims = conn.execute(
            f"""
            INSERT INTO {tables['claims']}
                (claim_id, claim_text, verdict, document_id, routed_at)
            SELECT c.claim_id, c.claim_text, c.factcheck_verdict, c.document_id, now()
            FROM argument_claims c
            JOIN document_domains m
              ON m.document_id = c.document_id AND m.domain = ?
            WHERE c.claim_id NOT IN (SELECT claim_id FROM {tables['claims']})
            """,
            [domain],
        ).fetchone()
        # The stale per-domain view must not keep serving corpus reads.
        conn.execute(f"DROP VIEW IF EXISTS {view_name(domain)}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    _flip_config(
        Path(config_path),
        domain,
        {
            "backing": "namespace",
            "namespace": namespace,
            "namespace_backend": backend,
        },
    )
    return {
        "domain": domain,
        "namespace": namespace,
        "backend": backend,
        "documents_copied": int(copied_docs[0]) if copied_docs else 0,
        "claims_copied": int(copied_claims[0]) if copied_claims else 0,
    }


def demote_to_corpus_view(
    conn,
    domain: str,
    config_path: Path,
) -> Dict[str, Any]:
    """Reverse flip: serve the domain from the shared corpus again.

    Namespace-native documents (ids the shared sink has never seen) are
    upserted into ``documents`` and given membership rows, so nothing is
    lost; ids are preserved so links survive. The namespace tables are left
    in place (teardown stays a provisioning-plane decision).
    """
    from src.provisioning.namespaces import (
        BACKEND_ATTACHED,
        BACKEND_TABLE_PREFIX,
        namespace_tables,
        ensure_attached,
    )
    from src.kb.membership import ensure_membership_schema

    registry = load_registry(config_path)
    definition = registry.get(domain)
    if definition.backing != "namespace":
        raise DomainConfigError(f"domain {domain!r} is not namespace backed")

    backend = (
        BACKEND_ATTACHED
        if definition.namespace_backend == "attached"
        else BACKEND_TABLE_PREFIX
    )
    if backend == BACKEND_ATTACHED:
        ensure_attached(conn, definition.namespace)
    tables = namespace_tables(definition.namespace, backend)
    ensure_membership_schema(conn)

    from src.provisioning.namespaces import _has_column

    has_content = _has_column(conn, tables["documents"], "content")
    has_ingested = _has_column(conn, tables["documents"], "ingested_at")
    content_expr = "n.content" if has_content else "NULL"
    ingested_expr = (
        "COALESCE(n.ingested_at, epoch_ms(n.routed_at))"
        if has_ingested
        else "epoch_ms(n.routed_at)"
    )

    conn.execute("BEGIN TRANSACTION")
    try:
        restored = conn.execute(
            f"""
            INSERT INTO documents
                (document_id, source_type, ingested_at, created_at,
                 source_id, url, title, content)
            SELECT n.id, COALESCE(n.source_type, 'news'), {ingested_expr},
                   epoch_ms(n.published_at), n.source, n.url, n.title,
                   {content_expr}
            FROM {tables['documents']} n
            WHERE n.id NOT IN (SELECT document_id FROM documents)
            """
        ).fetchone()
        now = int(time.time() * 1000)
        conn.execute(
            f"""
            INSERT INTO document_domains (document_id, domain, score, method, run_id, assigned_at)
            SELECT n.id, ?, 1.0, 'source', 'demotion', ?
            FROM {tables['documents']} n
            WHERE (n.id, ?) NOT IN (
                SELECT document_id, domain FROM document_domains
            )
            """,
            [domain, now, domain],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    _flip_config(
        Path(config_path),
        domain,
        {"backing": "corpus-view", "namespace": None, "namespace_backend": None},
    )
    return {
        "domain": domain,
        "documents_restored": int(restored[0]) if restored else 0,
    }
