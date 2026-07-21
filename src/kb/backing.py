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

    Wired against the provisioning plane in a later increment; until then
    only :meth:`coverage` answers. ``namespace`` defaults to the domain name
    at validation time, so the field is always present here.
    """

    backing_type = "namespace"

    def coverage(self) -> Dict[str, Any]:
        payload = super().coverage()
        payload["namespace"] = self.definition.namespace
        return payload
