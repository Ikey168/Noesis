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

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from src.kb.registry import DomainDefinition


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

    @property
    def conn(self) -> Any:
        """Warehouse connection, defaulting to the shared read connection."""
        if self._conn is None:
            from src.database.local_analytics_connector import get_shared_connection

            self._conn = get_shared_connection()
        return self._conn

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
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(zip(self._DOCUMENT_COLUMNS, row)) for row in rows]

    def documents(
        self,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        view = self._view()
        columns = ", ".join(self._DOCUMENT_COLUMNS)
        params: List[Any] = []
        where = ""
        if since:
            where = (
                "WHERE COALESCE(created_at, ingested_at, 0)"
                " >= epoch_ms(CAST(? AS TIMESTAMP))"
            )
            params.append(since)
        params.append(int(limit))
        return self._rows(
            f"SELECT {columns} FROM {view} {where}"
            " ORDER BY COALESCE(created_at, ingested_at, 0) DESC LIMIT ?",
            params,
        )

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Lexical search within the domain (semantic lands with the contract)."""
        view = self._view()
        columns = ", ".join(self._DOCUMENT_COLUMNS)
        pattern = f"%{query.lower()}%"
        return self._rows(
            f"SELECT {columns} FROM {view}"
            " WHERE lower(COALESCE(title, '')) LIKE ?"
            "    OR lower(COALESCE(content, '')) LIKE ?"
            " ORDER BY COALESCE(created_at, ingested_at, 0) DESC LIMIT ?",
            [pattern, pattern, int(limit)],
        )

    def coverage(self) -> Dict[str, Any]:
        payload = super().coverage()
        view = self._view()
        total, first_seen, last_seen = self.conn.execute(
            f"SELECT COUNT(*), MIN(COALESCE(created_at, ingested_at)),"
            f" MAX(COALESCE(created_at, ingested_at)) FROM {view}"
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
                "first_seen_ms": first_seen,
                "last_seen_ms": last_seen,
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
