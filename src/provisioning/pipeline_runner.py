"""Live pipeline runner for provisioned KGs (M3.1).

Track P2 wired ``Provisioner.ingest`` to run bound pipelines through an injected
``pipeline_runner`` callable, but left that callable unbound in the serving path
so ``kg_ingest`` degraded to routing already-ingested documents. This module is
the real runner: it runs a bound connector through the ingestion connector
registry (the same connectors ``pipeline_mcp`` exposes).

By default it drives the connector through :meth:`Connector.harvest_run` (#896)
— so every source gets retry, ``SourceHealthTracker`` drift detection, and
scheduling — persisting the harvested documents through a :class:`DocumentStore`
into the unified ``documents`` sink (#894). ``news_articles`` is now a view over
``documents`` (#909), so KG ingest routing (which reads ``news_articles``) sees
the harvested documents without a separate mirror.

Persistence is idempotent by document id, so re-ingesting a connector converges
rather than duplicating. The harvester (legacy records path) is still injectable
so existing tests exercise the persist-and-route path without a live fetch.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

# Max records to harvest per connector run (a sample-sized live pull, matching
# the pipeline_mcp connector sample cap).
DEFAULT_LIMIT = 25

_ARTICLE_COLUMNS = (
    "id",
    "title",
    "url",
    "content",
    "publish_date",
    "source",
    "category",
)


def _ensure_articles(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS news_articles ("
        "id VARCHAR, title VARCHAR, url VARCHAR, content VARCHAR, "
        "publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )


def _existing_ids(conn) -> set:
    try:
        return {r[0] for r in conn.execute("SELECT id FROM news_articles").fetchall()}
    except Exception:
        return set()


def persist_records(conn, source: str, records: List[Dict[str, Any]]) -> int:
    """Write harvested records into ``news_articles`` keyed by ``source``,
    skipping ids already present (idempotent). Returns the number written."""
    if not records:
        return 0
    _ensure_articles(conn)
    seen = _existing_ids(conn)
    rows = []
    for rec in records:
        rid = rec.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        rows.append(
            (
                rid,
                rec.get("title") or "",
                rec.get("url") or "",
                rec.get("content") or "",
                rec.get("publish_date"),
                rec.get("source") or source,
                rec.get("category"),
            )
        )
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO news_articles (id, title, url, content, publish_date, source, category) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _doc_to_record(doc: Any, connector_type: str) -> Dict[str, Any]:
    """Normalize a connector ``Document`` (or article) to a news_articles row."""
    meta = getattr(doc, "metadata", None) or {}
    published = (
        getattr(doc, "published_at", None)
        or getattr(doc, "publish_date", None)
        or meta.get("published_at")
    )
    return {
        "id": getattr(doc, "document_id", None) or getattr(doc, "id", None) or meta.get("id"),
        "title": getattr(doc, "title", None) or "",
        "url": getattr(doc, "url", None) or meta.get("url") or "",
        "content": getattr(doc, "content", None) or "",
        "publish_date": published.isoformat() if hasattr(published, "isoformat") else published,
        "category": connector_type,
    }


def default_harvester(connector_type: str, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run a real connector from the ingestion registry and return normalized
    records. Best-effort: an unknown connector type or a fetch error yields an
    empty list (ingest then degrades to routing), never a raise."""
    query = config.get("query") if isinstance(config, dict) else None
    try:
        import src.ingestion.connectors  # noqa: F401 - trigger registrations
        from src.ingestion.connectors.registry import get_connector, is_registered
    except Exception:
        return []
    if not is_registered(connector_type):
        return []
    try:
        connector = get_connector(connector_type)
    except Exception:
        return []
    records: List[Dict[str, Any]] = []
    try:
        refs = list(connector.discover(query))[:DEFAULT_LIMIT]
    except Exception:
        return []
    for ref in refs:
        try:
            for doc in connector.parse(connector.fetch(ref)):
                rec = _doc_to_record(doc, connector_type)
                if rec.get("id"):
                    records.append(rec)
        except Exception:
            continue
        if len(records) >= DEFAULT_LIMIT:
            break
    return records[:DEFAULT_LIMIT]


def _resolve_connector(connector_type: str):
    """Resolve a registered connector instance, or None if unavailable."""
    try:
        import src.ingestion.connectors  # noqa: F401 - trigger registrations
        from src.ingestion.connectors.registry import get_connector, is_registered
    except Exception:  # noqa: BLE001
        return None
    if not connector_type or not is_registered(connector_type):
        return None
    try:
        return get_connector(connector_type)
    except Exception:  # noqa: BLE001
        return None


def build_pipeline_runner(
    conn,
    harvester: Optional[Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]] = None,
    connector_resolver: Optional[Callable[[str], Any]] = None,
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """Return the ``pipeline_runner`` callable ``Provisioner`` invokes per bound pipeline.

    Default (no ``harvester``): resolve the connector and run it through
    ``harvest_run`` into the ``documents`` sink, bridging to ``news_articles``.
    If a ``harvester`` is injected, the legacy records path is used instead (it
    persists to ``news_articles`` only) — this preserves existing tests and
    custom record-shaped harvesters. ``connector_resolver`` is injectable so the
    live path is testable with a fake connector.
    """
    resolve = connector_resolver or _resolve_connector

    def runner(spec: Dict[str, Any]) -> Dict[str, Any]:
        connector = spec.get("connector")
        connector_type = spec.get("connector_type") or ""
        config = spec.get("config") or {}
        source = config.get("source") or connector
        query = config.get("query") if isinstance(config, dict) else None

        # Legacy injected records path — persist to news_articles only.
        if harvester is not None:
            records = harvester(connector_type, config) or []
            for rec in records:
                rec.setdefault("source", source)
            written = persist_records(conn, source, records)
            return {
                "connector": connector, "source": source,
                "fetched": len(records), "written": written, "documents": 0,
            }

        # Default live path — harvest_run into the unified documents sink.
        connector_obj = resolve(connector_type)
        if connector_obj is None:
            return {
                "connector": connector, "source": source,
                "fetched": 0, "written": 0, "documents": 0,
            }

        from src.database.news_articles_compat import ensure_news_articles_view
        from src.ingestion.document_store import DocumentStore
        from src.ingestion.source_health import SourceHealthTracker

        # documents(+enrichments) + the news_articles view, so KG ingest routing
        # (which reads news_articles) sees the harvested documents (#909).
        ensure_news_articles_view(conn)
        health_path = config.get("health_path") if isinstance(config, dict) else None
        store = DocumentStore(conn)
        summary = connector_obj.harvest_run(
            query=query,
            store=store,
            health=SourceHealthTracker(health_path),
            respect_schedule=False,
        )
        return {
            "connector": connector, "source": source,
            "fetched": summary.documents, "written": summary.inserted,
            "documents": summary.inserted,
        }

    return runner
