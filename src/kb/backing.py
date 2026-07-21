"""
The backing abstraction: one read interface, two storage realizations.

Consumers of a knowledge domain must never be able to tell whether it is
served from the shared corpus (``corpus-view``) or from a provisioned
namespace (``namespace``). Every read the KB contract will expose is declared
here; a backing that has not yet implemented a call raises
:class:`NotImplementedError` so the gap is loud, not silent.

The read surface mirrors the planned ``noesis-kb-v1`` contract:

- retrieve: :meth:`DomainBacking.documents`, :meth:`DomainBacking.search`,
  :meth:`DomainBacking.claims`, :meth:`DomainBacking.entities`
- diff:     :meth:`DomainBacking.diff`
- meta:     :meth:`DomainBacking.coverage` (always implemented — it reports
  the backing type and readiness even before the data paths land)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from src.kb.registry import DomainDefinition


def _since_to_epoch_ms(since: str) -> int:
    """Parse an ISO-8601 ``since`` into epoch ms, honouring UTC offsets.

    Naive timestamps are interpreted as UTC (casting in SQL would silently
    drop non-zero offsets); malformed input raises ``ValueError`` loudly.
    """
    parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


class DomainBacking:
    """Read interface every knowledge domain answers, whatever its storage.

    Subclasses implement the data paths incrementally; until then each call
    raises :class:`NotImplementedError` naming the backing, so a consumer
    hitting an unfinished path gets a diagnosable error instead of silence.
    """

    #: machine-readable backing discriminator, overridden per subclass
    backing_type: str = "abstract"

    def __init__(self, definition: "DomainDefinition", conn: Any = None) -> None:
        self.definition = definition
        self._conn = conn
        # True when we lazily adopted the process-wide shared connection —
        # every execute must then hold the module's lock (the shared handle
        # is not safe for concurrent use).
        self._on_shared_conn = False

    @property
    def conn(self) -> Any:
        """Warehouse connection, defaulting to the shared connection.

        The default is **in-process only**: it adopts the warehouse-owning
        process's shared DuckDB handle (creating and seeding the warehouse
        file if this process is the first opener — a write side effect).
        DuckDB allows a single read-write process per file, so a second
        process taking this default while the API holds the file will fail
        to connect. Out-of-process callers (jobs, CLIs, tests) must inject
        their own connection instead.
        """
        if self._conn is None:
            from src.database.local_analytics_connector import get_shared_connection

            self._conn = get_shared_connection()
            self._on_shared_conn = True
        return self._conn

    def _lock(self):
        """Serialize shared-connection access; no-op for injected conns."""
        if self._on_shared_conn or self._conn is None:
            # Touch .conn first so adoption happens before locking.
            _ = self.conn
        if self._on_shared_conn:
            from src.database.local_analytics_connector import _LOCK

            return _LOCK
        from contextlib import nullcontext

        return nullcontext()

    # -- retrieve -----------------------------------------------------------

    def documents(
        self,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Member documents, newest first, each row citing its source."""
        raise self._not_implemented("documents")

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Semantic + lexical search scoped to this domain."""
        raise self._not_implemented("search")

    def claims(
        self,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Clustered, cited claims for this domain."""
        raise self._not_implemented("claims")

    def entities(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Canonical entities (with aliases) mentioned in this domain."""
        raise self._not_implemented("entities")

    # -- diff ---------------------------------------------------------------

    def diff(self, since: str) -> Dict[str, Any]:
        """What changed since ``since`` — the primitive consumers reduce to."""
        raise self._not_implemented("diff")

    # -- meta ---------------------------------------------------------------

    def coverage(self) -> Dict[str, Any]:
        """Domain metadata: backing, sources, freshness, embedding model.

        Always answerable. Subclasses extend the payload with real corpus
        stats once their data paths exist; ``ready`` flips to True when the
        retrieve surface is implemented.
        """
        return {
            "domain": self.definition.name,
            "backing": self.backing_type,
            "embedding_model": self.definition.embedding_model,
            "feeds": [feed.url for feed in self.definition.feeds],
            "tags": list(self.definition.tags),
            "ready": False,
        }

    # -- helpers ------------------------------------------------------------

    def _not_implemented(self, call: str) -> NotImplementedError:
        return NotImplementedError(
            f"{call}() is not implemented yet for domain "
            f"{self.definition.name!r} (backing {self.backing_type!r})"
        )


class CorpusViewBacking(DomainBacking):
    """Domain served by membership rows + views over the shared corpus.

    Reads go through the per-domain view (``kb_domain_<name>``) built from
    ``document_domains`` — membership is data written by the membership pass,
    never a per-query classification. ``claims``/``entities``/``diff`` arrive
    with the consolidation increments.
    """

    backing_type = "corpus-view"

    _DOCUMENT_COLUMNS = (
        "document_id", "source_type", "source_id", "url", "title",
        "language", "ingested_at", "created_at",
        "domain_score", "domain_method", "sentiment_score", "sentiment_label",
    )

    def _view(self) -> str:
        from src.kb.membership import ensure_domain_views, view_name

        name = view_name(self.definition.name)
        with self._lock():
            exists = self.conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name = ?",
                [name],
            ).fetchone()
            if exists is None:
                from src.kb.registry import KnowledgeDomainRegistry

                ensure_domain_views(
                    self.conn, KnowledgeDomainRegistry([self.definition])
                )
        return name

    def _rows(self, sql: str, params: List[Any]) -> List[Dict[str, Any]]:
        with self._lock():
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(zip(self._DOCUMENT_COLUMNS, row)) for row in rows]

    @staticmethod
    def _like_pattern(query: str) -> str:
        """Contains-pattern with LIKE wildcards escaped (used with ESCAPE '\\')."""
        escaped = (
            query.lower()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        return f"%{escaped}%"

    def documents(
        self,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Member documents, newest *arrival* first.

        ``since`` filters on ingestion time — "what entered the domain since
        T" — not on publication date; a backfilled 2020 paper ingested today
        is new domain content today. Publication time (``created_at``) is
        returned on each row for display.
        """
        view = self._view()
        columns = ", ".join(self._DOCUMENT_COLUMNS)
        params: List[Any] = []
        where = ""
        if since:
            where = "WHERE COALESCE(ingested_at, 0) >= ?"
            params.append(_since_to_epoch_ms(since))
        params.append(int(limit))
        return self._rows(
            f"SELECT {columns} FROM {view} {where}"
            " ORDER BY COALESCE(ingested_at, 0) DESC LIMIT ?",
            params,
        )

    def claims(
        self,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Clustered, cited claims for this domain (presentation merge).

        Each entry is a cluster: representative + full citation list,
        corroboration count, cross-cluster contradictions, and supersedence
        flags. See :func:`src.kb.clusters.cluster_claims`.
        """
        from src.kb.clusters import cluster_claims

        since_ms = _since_to_epoch_ms(since) if since else None
        with self._lock():
            return cluster_claims(
                self.conn,
                domain=self.definition.name,
                limit=limit,
                since=since_ms,
            )

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Lexical search within the domain (semantic lands with the contract)."""
        view = self._view()
        columns = ", ".join(self._DOCUMENT_COLUMNS)
        pattern = self._like_pattern(query)
        return self._rows(
            f"SELECT {columns} FROM {view}"
            " WHERE lower(COALESCE(title, '')) LIKE ? ESCAPE '\\'"
            "    OR lower(COALESCE(content, '')) LIKE ? ESCAPE '\\'"
            " ORDER BY COALESCE(ingested_at, 0) DESC LIMIT ?",
            [pattern, pattern, int(limit)],
        )

    def entities(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Canonical entities mentioned in this domain, aliases folded.

        Mentions come from ``document_actors`` scoped through membership and
        resolved through ``entity_aliases``; surfaces with no alias row yet
        group under their normalized form.
        """
        from src.kb.entities import ensure_entity_schema, normalize_surface

        with self._lock():
            ensure_entity_schema(self.conn)
            mention_rows = self.conn.execute(
                """
                SELECT a.actor_name, COUNT(*)
                FROM document_actors a
                JOIN document_domains m
                  ON m.document_id = a.document_id AND m.domain = ?
                GROUP BY a.actor_name
                """,
                [self.definition.name],
            ).fetchall()
            aliases = dict(
                self.conn.execute(
                    "SELECT surface_form, canonical_id FROM entity_aliases"
                ).fetchall()
            )
            preferred = dict(
                self.conn.execute(
                    "SELECT canonical_id, preferred_name FROM canonical_entities"
                ).fetchall()
            )

        folded: Dict[str, Dict[str, Any]] = {}
        for actor_name, count in mention_rows:
            normalized = normalize_surface(actor_name)
            canonical = aliases.get(normalized, f"raw:{normalized}")
            entry = folded.setdefault(
                canonical,
                {
                    "canonical_id": canonical,
                    "name": preferred.get(canonical, actor_name),
                    "mentions": 0,
                    "aliases": [],
                },
            )
            entry["mentions"] += int(count)
            if actor_name not in entry["aliases"]:
                entry["aliases"].append(actor_name)

        results = sorted(
            folded.values(), key=lambda entry: entry["mentions"], reverse=True
        )
        if name:
            needle = name.lower()
            results = [
                entry
                for entry in results
                if needle in entry["name"].lower()
                or any(needle in alias.lower() for alias in entry["aliases"])
            ]
        return results

    def diff(self, since: str) -> Dict[str, Any]:
        """What changed in this domain since ``since`` (ISO-8601, UTC).

        Computed from consolidation outputs — see :mod:`src.kb.diffs`.
        """
        from src.kb.diffs import compute_corpus_diff

        with self._lock():
            return compute_corpus_diff(
                self.conn, self.definition.name, _since_to_epoch_ms(since)
            )

    def coverage(self) -> Dict[str, Any]:
        payload = super().coverage()
        view = self._view()
        with self._lock():
            total, first_ingested, last_ingested = self.conn.execute(
                f"SELECT COUNT(*), MIN(ingested_at), MAX(ingested_at) FROM {view}"
            ).fetchone()
            methods = dict(
                self.conn.execute(
                    "SELECT method, COUNT(*) FROM document_domains"
                    " WHERE domain = ? GROUP BY method",
                    [self.definition.name],
                ).fetchall()
            )
            sources = [
                row[0]
                for row in self.conn.execute(
                    f"SELECT source_id FROM {view} WHERE source_id IS NOT NULL"
                    " GROUP BY source_id ORDER BY COUNT(*) DESC LIMIT 25"
                ).fetchall()
            ]
        payload.update(
            {
                "ready": True,
                "documents": int(total or 0),
                "first_ingested_ms": first_ingested,
                "last_ingested_ms": last_ingested,
                "assignment_methods": methods,
                "sources": sources,
            }
        )
        return payload


class NamespaceBacking(DomainBacking):
    """Domain served by a provisioned namespace with its own storage.

    Reads go against the provisioning plane's namespaced tables
    (``kg_<name>_documents/entities/claims``), either table-prefixed in the
    shared warehouse or alias-qualified in the namespace's own attached
    DuckDB file (``namespace_backend: attached`` — one engine, so
    cross-backing SQL joins stay possible). The base namespaced schema is
    thin; promotion (:mod:`src.kb.promotion`) extends it with ``content``
    and ``ingested_at`` columns, and every read here tolerates their
    absence. ``diff`` arrives with the change-feed increment.
    """

    backing_type = "namespace"

    def _tables(self) -> Dict[str, str]:
        from src.provisioning.namespaces import (
            BACKEND_ATTACHED,
            BACKEND_TABLE_PREFIX,
            create_namespace,
        )

        backend = (
            BACKEND_ATTACHED
            if self.definition.namespace_backend == "attached"
            else BACKEND_TABLE_PREFIX
        )
        with self._lock():
            return create_namespace(self.conn, self.definition.namespace, backend)

    def _has_column(self, table: str, column: str) -> bool:
        from src.provisioning.namespaces import _has_column

        return _has_column(self.conn, table, column)

    def documents(
        self,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        tables = self._tables()
        docs = tables["documents"]
        has_content = self._has_column(docs, "content")
        has_ingested = self._has_column(docs, "ingested_at")
        content_expr = "content" if has_content else "NULL AS content"
        arrival = "ingested_at" if has_ingested else "epoch_ms(routed_at)"
        params: List[Any] = []
        where = ""
        if since:
            where = f"WHERE COALESCE({arrival}, 0) >= ?"
            params.append(_since_to_epoch_ms(since))
        params.append(int(limit))
        with self._lock():
            rows = self.conn.execute(
                f"SELECT id, title, source, source_type, url,"
                f" epoch_ms(published_at), COALESCE({arrival}, 0), {content_expr}"
                f" FROM {docs} {where}"
                f" ORDER BY COALESCE({arrival}, 0) DESC LIMIT ?",
                params,
            ).fetchall()
        return [
            {
                "document_id": row[0],
                "title": row[1],
                "source_id": row[2],
                "source_type": row[3],
                "url": row[4],
                "created_at": row[5],
                "ingested_at": row[6],
                "content": row[7],
            }
            for row in rows
        ]

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        tables = self._tables()
        docs = tables["documents"]
        has_content = self._has_column(docs, "content")
        pattern = CorpusViewBacking._like_pattern(query)
        content_clause = (
            " OR lower(COALESCE(content, '')) LIKE ? ESCAPE '\\'"
            if has_content
            else ""
        )
        params: List[Any] = [pattern] + ([pattern] if has_content else [])
        params.append(int(limit))
        with self._lock():
            rows = self.conn.execute(
                f"SELECT id, title, source, url FROM {docs}"
                f" WHERE lower(COALESCE(title, '')) LIKE ? ESCAPE '\\'{content_clause}"
                f" ORDER BY routed_at DESC NULLS LAST LIMIT ?",
                params,
            ).fetchall()
        return [
            {"document_id": row[0], "title": row[1], "source_id": row[2], "url": row[3]}
            for row in rows
        ]

    def claims(
        self,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Namespace claims in the same cluster shape corpus domains serve.

        Namespace-native claims are clusters of one until the cross-backing
        link pass connects them; links they participate in live in the
        shared ``claim_links`` table and are cited here.
        """
        tables = self._tables()
        with self._lock():
            rows = self.conn.execute(
                f"SELECT claim_id, claim_text, verdict, document_id"
                f" FROM {tables['claims']} ORDER BY claim_id LIMIT ?",
                [int(limit)],
            ).fetchall()
            links = self.conn.execute(
                "SELECT claim_a, claim_b, relation, confidence, prediction_mode"
                " FROM claim_links WHERE relation IN ('supports', 'contradicts')"
            ).fetchall() if self._link_table_exists() else []
        by_claim: Dict[str, List[Dict[str, Any]]] = {}
        for claim_a, claim_b, relation, confidence, mode in links:
            by_claim.setdefault(claim_a, []).append(
                {"claim_id": claim_b, "relation": relation,
                 "confidence": confidence, "prediction_mode": mode}
            )
            by_claim.setdefault(claim_b, []).append(
                {"claim_id": claim_a, "relation": relation,
                 "confidence": confidence, "prediction_mode": mode}
            )
        clusters = []
        for claim_id, text, verdict, document_id in rows:
            related = by_claim.get(claim_id, [])
            clusters.append(
                {
                    "cluster_id": f"cl-{claim_id}",
                    "representative": {
                        "claim_id": claim_id,
                        "claim_text": text,
                        "document_id": document_id,
                        "verdict": verdict,
                        "superseded": False,
                    },
                    "citations": [
                        {"claim_id": claim_id, "claim_text": text,
                         "document_id": document_id, "verdict": verdict,
                         "superseded": False}
                    ],
                    "corroboration": 1,
                    "contradictions": [
                        link for link in related if link["relation"] == "contradicts"
                    ],
                    "supports": [
                        link for link in related if link["relation"] == "supports"
                    ],
                    "size": 1,
                }
            )
        return clusters

    def entities(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        tables = self._tables()
        params: List[Any] = []
        where = ""
        if name:
            where = "WHERE lower(entity) LIKE ? ESCAPE '\\'"
            params.append(CorpusViewBacking._like_pattern(name))
        with self._lock():
            rows = self.conn.execute(
                f"SELECT entity, mentions FROM {tables['entities']} {where}"
                " ORDER BY mentions DESC NULLS LAST",
                params,
            ).fetchall()
        return [{"entity": row[0], "mentions": row[1]} for row in rows]

    def diff(self, since: str) -> Dict[str, Any]:
        """Namespace change feed — same shape as corpus diffs, honest gaps
        (entity surges are ``None``: no mention timeline exists here)."""
        from src.kb.diffs import compute_namespace_diff

        with self._lock():
            return compute_namespace_diff(
                self.conn, self.definition, _since_to_epoch_ms(since)
            )

    def _link_table_exists(self) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables"
                " WHERE table_name = 'claim_links'"
            ).fetchone()
            is not None
        )

    def coverage(self) -> Dict[str, Any]:
        payload = super().coverage()
        payload["namespace"] = self.definition.namespace
        payload["namespace_backend"] = self.definition.namespace_backend
        tables = self._tables()
        with self._lock():
            documents = self.conn.execute(
                f"SELECT COUNT(*) FROM {tables['documents']}"
            ).fetchone()[0]
            claims = self.conn.execute(
                f"SELECT COUNT(*) FROM {tables['claims']}"
            ).fetchone()[0]
            # Shared-embedding-space guard: vectors for this namespace's
            # documents embedded under a *different* model are a loud
            # mismatch, not silently bad similarity.
            mismatches = 0
            if (
                self.conn.execute(
                    "SELECT 1 FROM information_schema.tables"
                    " WHERE table_name = 'document_embeddings'"
                ).fetchone()
                is not None
            ):
                mismatches = self.conn.execute(
                    f"SELECT COUNT(*) FROM document_embeddings e"
                    f" JOIN {tables['documents']} n ON n.id = e.document_id"
                    f" WHERE e.model <> ?",
                    [self.definition.embedding_model],
                ).fetchone()[0]
        payload.update(
            {
                "ready": True,
                "documents": int(documents or 0),
                "claims": int(claims or 0),
                "embedding_model_mismatches": int(mismatches or 0),
            }
        )
        return payload
