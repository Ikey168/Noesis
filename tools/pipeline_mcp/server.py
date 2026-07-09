"""
NeuroNews Pipeline-stage runner — MCP server.

A thin developer tool that exposes each ETL pipeline boundary as a typed tool so
you can run a single stage against a fixture/source and inspect a SUMMARY or
DIFF — never the full payload. It drives the project's own pipeline code
(`src.ingestion.scrapy_integration`) and the local docker-compose stack
(DuckDB warehouse, Postgres/pgvector, S3/MinIO).

Tools:
  list_sources(category?)              -> the configured RSS connectors (DEFAULT_FEEDS)
  list_connector_types()               -> all registered document connector source types
  run_connector(source, sample=5,      -> fetch+parse one source, summary only (no write).
                query?)                   source = RSS feed name OR a source_type from
                                          list_connector_types(); query = JSON list of paths/
                                          IDs passed to that connector's discover().
  run_stage(stage, input_ref?, ...)         -> run one named stage: fetch|sentiment|store|ingest|positions
  trace_article(id)                         -> where an article exists across warehouse / vector / s3
  query_positions(actor?, topic?, ...)      -> query policy_positions table for actor commitments (#110)
  query_position_updates(position_id?, ...) -> query position_updates follow-through events (#111)
  trigger_followthrough_check(limit?)       -> run nightly follow-through batch on-demand (#111)
  query_conflicts(topic?, source_type?, ...) -> query claim_conflicts table for conflict pairs (#112)
  compute_conflicts(limit?, date_range?)    -> run semantic-similarity conflict detection batch (#112)
  article_stats(days?)                      -> headline warehouse counts (signal-summary panel)
  latest_articles(topic?, limit?)           -> newest matching article summaries
  document_stats(source_type?)              -> ingested-document counts by source type
  sentiment_by_topic(days?)                 -> average sentiment per topic
  sentiment_heatmap(days?)                  -> topic-by-day sentiment grid
  coverage_clusters(days?)                  -> grouped coverage summary (clusters panel)

Design constraints (learned the hard way in this repo):
  * Lazy imports inside tools. The top of this module imports only stdlib +
    fastmcp so the server starts instantly and never triggers the repo's heavy
    ML import graph (transformers/torch), which can hang on import.
  * The DuckDB warehouse is SINGLE-WRITER. We open it READ-ONLY for inspection
    so we don't fight the API server's writer lock, and report the lock cleanly
    instead of crashing. Writes (store/ingest) require apply=True and a free
    warehouse.
  * Postgres/S3 checks are best-effort with short timeouts; an unreachable
    backend yields a status string, not a hang or traceback.
"""

from __future__ import annotations

import os
import sys
import socket
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP

# Make the repo importable (server lives at <repo>/tools/pipeline_mcp/server.py).
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Stdlib-only helper for the analytics honesty contract (R5); safe at import.
from src.analytics.honesty import INTERVAL_SCHEMA, honesty_output_schema  # noqa: E402

mcp = FastMCP("neuronews-pipeline")

# Caps so we always return summaries, not payloads.
MAX_LIST = 25
CONTENT_PREVIEW = 200


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _pipeline():
    """Lazy-import the (light) ingestion pipeline module."""
    from src.ingestion import scrapy_integration as si
    return si


def _db_path() -> str:
    from src.config.env import warehouse_path
    return warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))


def _warehouse_ro():
    """Open the DuckDB warehouse READ-ONLY. Raises with a clear message if the
    file is missing or locked by another (writer) process."""
    import duckdb

    path = _db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"warehouse not found at {path} — start the API once to seed it, "
            f"or set NEURONEWS_DB_PATH"
        )
    try:
        return duckdb.connect(path, read_only=True)
    except Exception as exc:  # IOException when a writer holds the lock
        raise RuntimeError(
            f"warehouse at {path} is not readable (likely locked by a running "
            f"API/ingester — DuckDB is single-writer): {exc}"
        ) from exc


def _warehouse_rw():
    """Open the DuckDB warehouse READ-WRITE. Use only for write-stage tools."""
    import duckdb

    path = _db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"warehouse not found at {path} — start the API once to seed it, "
            f"or set NEURONEWS_DB_PATH"
        )
    try:
        return duckdb.connect(path, read_only=False)
    except Exception as exc:
        raise RuntimeError(
            f"warehouse at {path} is locked (DuckDB single-writer): {exc}"
        ) from exc


def _find_feed(si, source: str):
    """Resolve a source by exact or case-insensitive name; return Feed or None."""
    for f in si.DEFAULT_FEEDS:
        if f.name == source:
            return f
    low = source.strip().lower()
    for f in si.DEFAULT_FEEDS:
        if f.name.lower() == low:
            return f
    return None


def _sentiment_distribution(articles) -> dict:
    dist = {"positive": 0, "neutral": 0, "negative": 0}
    for a in articles:
        dist[a.sentiment_label] = dist.get(a.sentiment_label, 0) + 1
    return dist


def _port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #

@mcp.tool
def list_sources(category: Optional[str] = None) -> dict:
    """List the configured news connectors (RSS/Atom feeds in DEFAULT_FEEDS).

    Args:
        category: optional filter, e.g. "Technology", "Economy", "World".

    Returns a summary: total count, the categories present, and the feeds
    (name, url, category).
    """
    si = _pipeline()
    feeds = list(si.DEFAULT_FEEDS)
    if category:
        feeds = [f for f in feeds if f.category.lower() == category.lower()]
    return {
        "count": len(feeds),
        "categories": sorted({f.category for f in si.DEFAULT_FEEDS}),
        "sources": [{"name": f.name, "url": f.url, "category": f.category} for f in feeds],
    }


@mcp.tool
def list_connector_types() -> dict:
    """List all registered document connector source types.

    Returns the source types that have a connector registered in the connector
    registry (e.g. "news", "paper", "book", "transcript"). Each type can be
    passed as the ``source`` argument to ``run_connector``.
    """
    import src.ingestion.connectors  # noqa: F401 — triggers @register_connector for all built-ins
    from src.ingestion.connectors.registry import available_source_types
    types = available_source_types()
    return {
        "count": len(types),
        "source_types": types,
        "usage": (
            "Pass a source_type as `source` to run_connector(), "
            "and supply `query` as a JSON list of paths/IDs for that connector "
            "(e.g. query='[\"/books/foo.epub\"]' for book, "
            "query='[\"2312.00752\"]' for paper arXiv IDs, "
            "query='[\"/podcasts/ep.mp3\"]' for transcript). "
            "For `news`, omit query to use the default RSS feeds."
        ),
    }


@mcp.tool
def run_connector(source: str, sample: int = 5, query: Optional[str] = None) -> dict:
    """Run a single connector: fetch + parse up to `sample` records from one
    source and return a SUMMARY only (no warehouse write).

    Two dispatch modes, selected automatically:

    1. **RSS feed** (legacy): ``source`` is a feed name from ``list_sources``
       (e.g. ``"BBC Technology"``). ``query`` is ignored.

    2. **Document connector**: ``source`` is a source type from
       ``list_connector_types`` (e.g. ``"book"``, ``"paper"``, ``"transcript"``).
       ``query`` is a JSON list of paths or IDs to pass to ``discover()``.
       Examples::

         run_connector("book",       query='["/books/foo.epub"]')
         run_connector("paper",      query='["2312.00752", "1706.03762"]')
         run_connector("transcript", query='["/pods/ep42.mp3"]')
         run_connector("news")       # uses DEFAULT_FEEDS, no query needed

    Args:
        source: RSS feed name OR a source_type from list_connector_types.
        sample: max records to harvest (1-25).
        query:  JSON list of paths/IDs for document connectors (ignored for RSS).

    Returns a summary: source type, record count, and sample titles/snippets.
    """
    sample = max(1, min(int(sample), 25))

    # --- Try document connector registry first ----------------------------- #
    import src.ingestion.connectors as _conn_pkg  # noqa: F401 — trigger registrations
    from src.ingestion.connectors.registry import available_source_types, get_connector, is_registered

    if is_registered(source):
        return _run_document_connector(source, sample, query, get_connector)

    # --- Fall back to legacy RSS path -------------------------------------- #
    si = _pipeline()
    feed = _find_feed(si, source)
    if feed is None:
        types = available_source_types()
        return {
            "error": f"unknown source {source!r}",
            "hint": (
                f"For RSS feeds call list_sources(). "
                f"For document connectors use one of: {types}"
            ),
        }

    try:
        raw = si._http_get(feed.url)
        articles = si.parse_feed(raw, feed, limit=sample)
    except Exception as exc:
        return {"error": f"fetch/parse failed for {feed.name}: {exc}", "url": feed.url}

    dates = sorted(a.publish_date for a in articles) if articles else []
    return {
        "source": feed.name,
        "category": feed.category,
        "url": feed.url,
        "fetched": len(articles),
        "date_range": (
            {"earliest": dates[0].isoformat(), "latest": dates[-1].isoformat()}
            if dates else None
        ),
        "sentiment": _sentiment_distribution(articles),
        "sample_titles": [a.title for a in articles[:MAX_LIST]],
    }


def _run_document_connector(
    source_type: str,
    sample: int,
    query_json: Optional[str],
    get_connector,
) -> dict:
    """Harvest up to ``sample`` Documents from a registry connector and return a summary."""
    import json as _json

    query = None
    if query_json:
        try:
            query = _json.loads(query_json)
        except _json.JSONDecodeError as exc:
            return {"error": f"query must be a JSON list: {exc}", "example": '["path/to/file"]'}

    try:
        connector = get_connector(source_type)
    except KeyError as exc:
        return {"error": str(exc)}

    docs = []
    errors = []
    for ref in list(connector.discover(query))[:sample]:
        try:
            raw = connector.fetch(ref)
            parsed = connector.parse(raw)
            docs.extend(parsed)
        except Exception as exc:
            errors.append({"locator": ref.locator, "error": str(exc)})
        if len(docs) >= sample:
            break

    docs = docs[:sample]
    sample_snippets = []
    for d in docs[:MAX_LIST]:
        snippet = {
            "document_id": d.document_id,
            "title": d.title,
            "language": d.language,
        }
        # Include a few type-specific metadata highlights.
        for key in ("section_path", "start_s", "end_s", "speaker", "arxiv_id", "doi"):
            if key in (d.metadata or {}):
                snippet[key] = d.metadata[key]
        if d.content:
            snippet["content_preview"] = d.content[:CONTENT_PREVIEW]
        sample_snippets.append(snippet)

    result: dict = {
        "source_type": source_type,
        "connector": type(connector).__name__,
        "query": query,
        "harvested": len(docs),
        "documents": sample_snippets,
    }
    if errors:
        result["errors"] = errors
    return result


@mcp.tool
def run_stage(
    stage: str,
    input_ref: Optional[str] = None,
    sample: int = 5,
    apply: bool = False,
) -> dict:
    """Run a single pipeline stage against a fixture/source and return a
    summary or diff.

    Stages:
      fetch     input_ref = source name. Fetch+parse, return article refs
                (id, title, sentiment) — no write.
      sentiment input_ref = raw text, OR an article id present in the warehouse.
                Returns {score, label}.
      store     input_ref = source name. Fetch that source, then DIFF against
                the warehouse: which ids are new vs already present. Read-only
                by default; pass apply=true to actually insert (requires the
                warehouse to be free — DuckDB single-writer).
      ingest    input_ref = source name (or omit for all sources). Full
                fetch->store. Read-only diff unless apply=true.
      positions input_ref = article/document id already in the warehouse.
                Runs position extraction and writes results to policy_positions.
                Use query_positions() to read them back.

    Args:
        stage: one of fetch | sentiment | store | ingest.
        input_ref: source name, text, or article id depending on the stage.
        sample: max articles per feed for fetch/store/ingest (1-25).
        apply: actually write to the warehouse (store/ingest only).
    """
    si = _pipeline()
    stage = stage.strip().lower()
    sample = max(1, min(int(sample), 25))

    if stage == "fetch":
        if not input_ref:
            return {"error": "fetch needs input_ref = a source name"}
        feed = _find_feed(si, input_ref)
        if feed is None:
            return {"error": f"unknown source {input_ref!r}", "hint": "call list_sources()"}
        try:
            raw = si._http_get(feed.url)
            articles = si.parse_feed(raw, feed, limit=sample)
        except Exception as exc:
            return {"error": f"fetch failed for {feed.name}: {exc}"}
        return {
            "stage": "fetch",
            "source": feed.name,
            "fetched": len(articles),
            "articles": [
                {"id": a.id, "title": a.title, "sentiment": a.sentiment_label}
                for a in articles[:MAX_LIST]
            ],
        }

    if stage == "sentiment":
        if not input_ref:
            return {"error": "sentiment needs input_ref = text or an article id"}
        text = input_ref
        resolved_from = "text"
        # If it looks like an id rather than a sentence, try the warehouse.
        if " " not in input_ref.strip():
            try:
                con = _warehouse_ro()
                row = con.execute(
                    "SELECT title, content FROM news_articles WHERE id = ?", [input_ref]
                ).fetchone()
                con.close()
                if row:
                    text = f"{row[0]}. {row[1] or ''}"
                    resolved_from = "article_id"
            except Exception:
                pass  # fall back to treating input_ref as literal text
        score, label = si.score_sentiment(text)
        return {
            "stage": "sentiment",
            "resolved_from": resolved_from,
            "score": score,
            "label": label,
            "scored_chars": len(text),
        }

    if stage in ("store", "ingest"):
        feeds = None
        if input_ref and input_ref.lower() != "all":
            feed = _find_feed(si, input_ref)
            if feed is None:
                return {"error": f"unknown source {input_ref!r}", "hint": "call list_sources()"}
            feeds = [feed]
        try:
            articles = si.fetch_articles(feeds, limit_per_feed=sample)
        except Exception as exc:
            return {"error": f"fetch failed: {exc}"}

        # Read-only diff against the warehouse.
        existing: set[str] = set()
        warehouse_status = "ok"
        try:
            con = _warehouse_ro()
            ids = [a.id for a in articles]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                rows = con.execute(
                    f"SELECT id FROM news_articles WHERE id IN ({placeholders})", ids
                ).fetchall()
                existing = {r[0] for r in rows}
            con.close()
        except Exception as exc:
            warehouse_status = str(exc)

        new = [a for a in articles if a.id not in existing]
        result = {
            "stage": stage,
            "source": input_ref or "all",
            "fetched": len(articles),
            "would_insert": len(new),
            "already_present": len(existing),
            "warehouse": warehouse_status,
            "applied": False,
            "sample_new_titles": [a.title for a in new[:MAX_LIST]],
        }
        if apply:
            try:
                inserted = si.store_articles(articles, replace=False)
                result["applied"] = True
                result["inserted"] = inserted
            except Exception as exc:
                result["apply_error"] = (
                    f"{exc} (warehouse likely locked by a running API/ingester; "
                    f"stop it or set NEURONEWS_DB_PATH to a free file)"
                )
        return result

    if stage == "positions":
        if not input_ref:
            return {"error": "positions needs input_ref = an article/document id"}
        try:
            con_rw = _warehouse_rw()
        except Exception as exc:
            return {"error": f"warehouse unavailable: {exc}"}
        try:
            row = con_rw.execute(
                "SELECT id, title, content, publish_date, source, category "
                "FROM news_articles WHERE id = ? LIMIT 1",
                [input_ref],
            ).fetchone()
        except Exception as exc:
            con_rw.close()
            return {"error": f"document lookup failed: {exc}"}
        if not row:
            con_rw.close()
            return {"error": f"document {input_ref!r} not found in warehouse"}
        doc_id, title, content, publish_date, source, category = row
        if not content:
            con_rw.close()
            return {"error": "document has no content"}
        try:
            import time as _time
            from services.ingest.common.document_model import Document
            from src.argument_mining.positions import run_position_pipeline
            meta: dict[str, Any] = {}
            if category:
                meta["category"] = category
            created_ms: Optional[int] = None
            if publish_date is not None:
                try:
                    created_ms = int(publish_date.timestamp() * 1000)
                except Exception:
                    pass
            doc = Document(
                document_id=doc_id,
                source_type="news",
                language="en",
                ingested_at=int(_time.time() * 1000),
                source_id=source,
                title=title,
                content=content,
                created_at=created_ms,
                metadata=meta,
            )
            records = run_position_pipeline(doc, con_rw)
            con_rw.close()
        except Exception as exc:
            con_rw.close()
            return {"error": f"position extraction failed: {exc}"}
        return {
            "stage": "positions",
            "document_id": doc_id,
            "positions_extracted": len(records),
            "positions": [
                {
                    "actor": r.actor,
                    "topic": r.topic,
                    "confidence": round(r.confidence, 4),
                    "text_preview": r.position_text[:CONTENT_PREVIEW],
                }
                for r in records[:MAX_LIST]
            ],
        }

    return {
        "error": f"unknown stage {stage!r}",
        "valid_stages": ["fetch", "sentiment", "store", "ingest", "positions"],
    }


@mcp.tool
def trace_article(id: str) -> dict:
    """Trace an article id across the pipeline's stores: the DuckDB warehouse,
    the Postgres/pgvector store, and S3/MinIO. Returns a presence summary and a
    truncated warehouse row — never the full content.

    Args:
        id: the article id (warehouse ids are a url hash; seed ids look like
            "art-0001"). Use run_stage("fetch", ...) to discover ids.
    """
    trace: dict[str, Any] = {"id": id, "warehouse": {}, "vector_store": {}, "object_store": {}}

    # 1) DuckDB warehouse (read-only).
    try:
        con = _warehouse_ro()
        row = con.execute(
            "SELECT title, url, source, category, publish_date, sentiment_score, "
            "sentiment_label, content FROM news_articles WHERE id = ?",
            [id],
        ).fetchone()
        con.close()
        if row:
            content = row[7] or ""
            trace["warehouse"] = {
                "present": True,
                "title": row[0],
                "url": row[1],
                "source": row[2],
                "category": row[3],
                "publish_date": row[4].isoformat() if row[4] else None,
                "sentiment": {"score": row[5], "label": row[6]},
                "content_length": len(content),
                "content_preview": content[:CONTENT_PREVIEW],
            }
        else:
            trace["warehouse"] = {"present": False}
    except Exception as exc:
        trace["warehouse"] = {"status": str(exc)}

    # 2) Postgres / pgvector (best-effort, short timeout).
    pg_host = os.getenv("PGVECTOR_HOST", "localhost")
    pg_port = int(os.getenv("PGVECTOR_PORT", "5433"))
    if not _port_open(pg_host, pg_port):
        trace["vector_store"] = {"reachable": False, "endpoint": f"{pg_host}:{pg_port}"}
    else:
        try:
            import psycopg2

            dsn = os.getenv(
                "PGVECTOR_DSN",
                f"postgresql://neuronews:neuronews_vector_pass@{pg_host}:{pg_port}/neuronews_vector",
            )
            conn = psycopg2.connect(dsn, connect_timeout=2)
            cur = conn.cursor()
            # Find any table with an 'id'-like column; report presence generically.
            cur.execute(
                "SELECT table_name FROM information_schema.columns "
                "WHERE column_name IN ('id','article_id','doc_id') "
                "AND table_schema='public' LIMIT 5"
            )
            tables = [r[0] for r in cur.fetchall()]
            hits = []
            for t in tables:
                for col in ("id", "article_id", "doc_id"):
                    try:
                        cur.execute(
                            f"SELECT 1 FROM {t} WHERE {col} = %s LIMIT 1", (id,)
                        )
                        if cur.fetchone():
                            hits.append(f"{t}.{col}")
                    except Exception:
                        conn.rollback()
            cur.close()
            conn.close()
            trace["vector_store"] = {
                "reachable": True,
                "endpoint": f"{pg_host}:{pg_port}",
                "candidate_tables": tables,
                "found_in": hits,
            }
        except Exception as exc:
            trace["vector_store"] = {"reachable": True, "error": str(exc)}

    # 3) S3 / MinIO (reachability only — don't hang on a full object scan).
    s3_endpoint = os.getenv("S3_ENDPOINT_URL") or os.getenv("AWS_ENDPOINT_URL")
    if s3_endpoint:
        host = s3_endpoint.split("://", 1)[-1].split("/", 1)[0]
        h, _, p = host.partition(":")
        port = int(p) if p else (443 if s3_endpoint.startswith("https") else 80)
        trace["object_store"] = {"endpoint": s3_endpoint, "reachable": _port_open(h, port)}
    else:
        trace["object_store"] = {"status": "no S3_ENDPOINT_URL configured"}

    return trace


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "positions": {"type": "array"}},
        "additionalProperties": True,
    },
)
def query_positions(
    actor: Optional[str] = None,
    topic: Optional[str] = None,
    source_type: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """Query the ``policy_positions`` table for actor policy commitments (#110).

    Returns compact position summaries — never full content payloads.
    Use ``run_stage("positions", input_ref=<id>)`` to first populate the table
    for a specific document.

    Args:
        actor:       ILIKE filter on the actor name (e.g. "government", "Johnson").
        topic:       ILIKE filter on topic (e.g. "economy", "healthcare", "law").
        source_type: Exact filter on source type (news/blog/paper/transcript/book/note).
        limit:       Max rows to return (capped at 25).
    """
    limit = max(1, min(int(limit), MAX_LIST))
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": f"warehouse unavailable: {exc}"}

    where_parts: list[str] = []
    params: list[Any] = []
    if actor:
        where_parts.append("actor ILIKE ?")
        params.append(f"%{actor}%")
    if topic:
        where_parts.append("topic ILIKE ?")
        params.append(f"%{topic}%")
    if source_type:
        where_parts.append("source_type = ?")
        params.append(source_type)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    try:
        rows = con.execute(
            f"""
            SELECT position_id, actor, topic, source_type, document_id,
                   position_date, confidence,
                   LEFT(position_text, {CONTENT_PREVIEW}) AS preview
            FROM policy_positions
            {where_clause}
            ORDER BY position_date DESC NULLS LAST, confidence DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        con.close()
    except Exception as exc:
        con.close()
        return {"error": f"query failed: {exc}", "hint": "table may not exist yet — run ensure_schema"}

    return {
        "filters": {k: v for k, v in {"actor": actor, "topic": topic, "source_type": source_type}.items() if v},
        "count": len(rows),
        "positions": [
            {
                "position_id": r[0],
                "actor": r[1],
                "topic": r[2],
                "source_type": r[3],
                "document_id": r[4],
                "date": r[5] or "",
                "confidence": round(float(r[6] or 0), 4),
                "text_preview": r[7],
            }
            for r in rows
        ],
    }


@mcp.tool
def query_position_updates(
    position_id: Optional[str] = None,
    actor: Optional[str] = None,
    topic: Optional[str] = None,
    update_type: Optional[str] = None,
    limit: int = 15,
) -> dict:
    """Query ``position_updates`` for follow-through events (#111).

    Returns compact update summaries (evidence truncated to 120 chars).
    Filter by position, actor name, topic, or update_type
    (reaffirmed / reversed / updated / no_signal).

    Args:
        position_id: exact position ID to look up.
        actor:       ILIKE filter on the actor name (joined from policy_positions).
        topic:       ILIKE filter on topic (joined from policy_positions).
        update_type: exact filter — reaffirmed | reversed | updated | no_signal.
        limit:       max rows (capped at 25).
    """
    limit = max(1, min(int(limit), MAX_LIST))
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": f"warehouse unavailable: {exc}"}

    join = "LEFT JOIN policy_positions p ON u.position_id = p.position_id"
    where_parts: list[str] = []
    params: list[Any] = []

    if position_id:
        where_parts.append("u.position_id = ?")
        params.append(position_id)
    if actor:
        where_parts.append("p.actor ILIKE ?")
        params.append(f"%{actor}%")
    if topic:
        where_parts.append("p.topic ILIKE ?")
        params.append(f"%{topic}%")
    if update_type:
        where_parts.append("u.update_type = ?")
        params.append(update_type)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    try:
        rows = con.execute(
            f"""
            SELECT u.update_id, u.position_id, p.actor, p.topic,
                   u.article_id, u.update_type, u.confidence, u.detected_at,
                   LEFT(u.evidence_text, 120) AS evidence_preview
            FROM position_updates u
            {join}
            {where_clause}
            ORDER BY u.detected_at DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
        con.close()
    except Exception as exc:
        con.close()
        return {"error": f"query failed: {exc}"}

    return {
        "filters": {k: v for k, v in {
            "position_id": position_id, "actor": actor,
            "topic": topic, "update_type": update_type,
        }.items() if v},
        "count": len(rows),
        "updates": [
            {
                "update_id":       r[0],
                "position_id":     r[1],
                "actor":           r[2] or "",
                "topic":           r[3] or "",
                "article_id":      r[4],
                "update_type":     r[5],
                "confidence":      round(float(r[6] or 0), 4),
                "detected_at":     r[7] or "",
                "evidence_preview": r[8] or "",
            }
            for r in rows
        ],
    }


@mcp.tool
def trigger_followthrough_check(limit: int = 50) -> dict:
    """Run one pass of the nightly follow-through batch on-demand (#111).

    Checks up to ``limit`` stored positions against recent warehouse articles
    and stores classified update events (reaffirmed/reversed/updated/no_signal)
    in ``position_updates``.  Use ``query_position_updates()`` to read results.

    Args:
        limit: max positions to process (capped at 200).
    """
    limit = max(1, min(int(limit), 200))
    try:
        import threading
        from src.argument_mining.position_tracker import run_followthrough_batch

        con = _warehouse_rw()
        lock = threading.Lock()
        counts = run_followthrough_batch(con, lock, limit=limit)
        con.close()
        return {"status": "ok", **counts}
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "conflicts": {"type": "array"}},
        "additionalProperties": True,
    },
)
def query_conflicts(
    topic: Optional[str] = None,
    source_type: Optional[str] = None,
    conflict_type: Optional[str] = None,
    limit: int = 15,
) -> dict:
    """Query ``claim_conflicts`` for semantic similarity conflict pairs (#112).

    Returns compact summaries — claim text truncated to 100 chars each.
    Populate the table first with ``compute_conflicts()``.

    Args:
        topic:         ILIKE filter on topic label.
        source_type:   filter where source_type_a OR source_type_b matches.
        conflict_type: exact filter — direct | implied.
        limit:         max rows (capped at 25).
    """
    limit = max(1, min(int(limit), MAX_LIST))
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": f"warehouse unavailable: {exc}"}

    where_parts: list[str] = []
    params: list[Any] = []
    if topic:
        where_parts.append("cf.topic ILIKE ?")
        params.append(f"%{topic}%")
    if source_type:
        where_parts.append("(cf.source_type_a = ? OR cf.source_type_b = ?)")
        params.extend([source_type, source_type])
    if conflict_type:
        where_parts.append("cf.conflict_type = ?")
        params.append(conflict_type)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    params.append(limit)

    try:
        rows = con.execute(
            f"""
            SELECT cf.claim_id_a, cf.claim_id_b, cf.conflict_type,
                   cf.similarity_score, cf.source_type_a, cf.source_type_b,
                   cf.topic, cf.computed_at,
                   LEFT(ca.claim_text, 100) AS text_a,
                   LEFT(cb.claim_text, 100) AS text_b,
                   COALESCE(na.source, ca.source_type) AS source_a,
                   COALESCE(nb.source, cb.source_type) AS source_b
            FROM claim_conflicts cf
            JOIN argument_claims ca ON cf.claim_id_a = ca.claim_id
            JOIN argument_claims cb ON cf.claim_id_b = cb.claim_id
            LEFT JOIN news_articles na ON ca.document_id = na.id
            LEFT JOIN news_articles nb ON cb.document_id = nb.id
            {where_clause}
            ORDER BY cf.similarity_score DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        con.close()
    except Exception as exc:
        con.close()
        return {"error": f"query failed: {exc}"}

    return {
        "filters": {k: v for k, v in {
            "topic": topic, "source_type": source_type, "conflict_type": conflict_type,
        }.items() if v},
        "count": len(rows),
        "conflicts": [
            {
                "claim_id_a":      r[0],
                "claim_id_b":      r[1],
                "conflict_type":   r[2],
                "similarity_score": round(float(r[3] or 0), 4),
                "source_type_a":   r[4],
                "source_type_b":   r[5],
                "topic":           r[6],
                "computed_at":     r[7] or "",
                "source_a":        r[10],
                "source_b":        r[11],
                "text_a_preview":  r[8],
                "text_b_preview":  r[9],
            }
            for r in rows
        ],
    }


@mcp.tool
def compute_conflicts(limit: int = 300, date_range: Optional[str] = None) -> dict:
    """Run semantic-similarity conflict detection on stored claims (#112).

    Pairs claims within the same topic window, computes bag-of-words cosine
    similarity, and stores conflicts (similarity ≥ 0.65 with opposing stance
    signals) in ``claim_conflicts``.  Use ``query_conflicts()`` to read results.

    Args:
        limit:      max claims to scan (capped at 1000).
        date_range: ISO date (YYYY-MM-DD); skip claims older than this.
    """
    limit = max(1, min(int(limit), 1000))
    try:
        import threading
        from src.argument_mining.conflict_graph import compute_claim_conflicts

        con = _warehouse_rw()
        lock = threading.Lock()
        counts = compute_claim_conflicts(con, lock, limit=limit, date_range=date_range)
        con.close()
        return {"status": "ok", **counts}
    except Exception as exc:
        return {"error": str(exc)}


# --------------------------------------------------------------------------- #
# Panel-shaped warehouse summaries (R2 discovery counterparts; see            #
# docs/architecture/decisions/ADR-001-tool-panel-annotation.md). Read-only, capped,     #
# and never full payloads — same house rules as the tools above.             #
# --------------------------------------------------------------------------- #

def _cutoff(days: int):
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(days=max(1, days))


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "total_articles": {"type": "integer"},
            "window_articles": {"type": "integer"},
            "sources": {"type": "integer"},
            "categories": {"type": "array"},
        },
        "additionalProperties": True,
    },
)
def article_stats(days: int = 7) -> dict:
    """Headline warehouse counts: total articles, articles in the window,
    distinct sources, and per-category counts.

    Args:
        days: Look-back window in days (default 7).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        total = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
        window = con.execute(
            "SELECT COUNT(*) FROM news_articles WHERE publish_date >= ?", [_cutoff(days)]
        ).fetchone()[0]
        sources = con.execute("SELECT COUNT(DISTINCT source) FROM news_articles").fetchone()[0]
        cats = con.execute(
            "SELECT category, COUNT(*) FROM news_articles GROUP BY category "
            "ORDER BY COUNT(*) DESC LIMIT 12"
        ).fetchall()
        return {
            "total_articles": total,
            "window_articles": window,
            "window_days": max(1, days),
            "sources": sources,
            "categories": [{"category": c, "articles": n} for c, n in cats],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "articles": {"type": "array"}},
        "additionalProperties": True,
    },
)
def latest_articles(topic: Optional[str] = None, limit: int = 10) -> dict:
    """Newest articles as compact summaries (title, source, date, sentiment
    label) — never full content.

    Args:
        topic: Substring match against the title (ILIKE).
        limit: Max rows (default 10, max 20).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    limit = min(max(1, limit), 20)
    where, params = "", []
    if topic:
        where = "WHERE title ILIKE ?"
        params.append(f"%{topic}%")
    params.append(limit)
    try:
        rows = con.execute(
            f"""
            SELECT id, title, source, category, publish_date, sentiment_label
            FROM news_articles {where}
            ORDER BY publish_date DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "count": len(rows),
            "articles": [
                {
                    "id": r[0],
                    "title": r[1],
                    "source": r[2],
                    "category": r[3],
                    "publish_date": r[4].isoformat() if r[4] else None,
                    "sentiment_label": r[5],
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "count": {"type": "integer"},
            "articles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": ["string", "null"]},
                        "title": {"type": ["string", "null"]},
                        "url": {"type": ["string", "null"]},
                        "publish_date": {"type": ["string", "null"]},
                        "source": {"type": ["string", "null"]},
                        "category": {"type": ["string", "null"]},
                        "sentiment_score": {"type": ["number", "null"]},
                        "sentiment_label": {"type": ["string", "null"]},
                    },
                },
            },
        },
        "additionalProperties": True,
    },
    # Data-mode (R12 #619): a full-payload variant of the `articles` panel,
    # equivalent to the /api/v1/news/articles REST route. The `data` meta block
    # marks it callable through the /api/v1/ui/data proxy allowlist; it is not a
    # planner-facing stats tool.
)
def articles_data(
    source: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = 50,
) -> dict:
    """Full article rows for the `articles` panel (data mode): the same fields
    the /api/v1/news/articles REST route returns, served through the MCP layer.

    Args:
        source: optional exact source filter.
        category: optional exact category filter.
        limit: max rows (default 50, max 200).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    limit = min(max(1, limit), 200)
    where, params = [], []
    if source:
        where.append("source = ?")
        params.append(source)
    if category:
        where.append("category = ?")
        params.append(category)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    try:
        rows = con.execute(
            f"""
            SELECT id, title, url, publish_date, source, category,
                   sentiment_score, sentiment_label
            FROM news_articles {clause}
            ORDER BY publish_date DESC NULLS LAST
            LIMIT ?
            """,
            params,
        ).fetchall()
        return {
            "count": len(rows),
            "articles": [
                {
                    "id": r[0],
                    "title": r[1],
                    "url": r[2],
                    "publish_date": r[3].isoformat() if r[3] else None,
                    "source": r[4],
                    "category": r[5],
                    "sentiment_score": float(r[6]) if r[6] is not None else None,
                    "sentiment_label": r[7],
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"total_documents": {"type": "integer"}, "by_source_type": {"type": "array"}},
        "additionalProperties": True,
    },
)
def document_stats(source_type: Optional[str] = None) -> dict:
    """Ingested-document counts by source type from the ``documents`` corpus
    table; reports ``table_missing`` when the corpus has not been created yet.

    Args:
        source_type: Restrict counts to one source type.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        where, params = "", []
        if source_type:
            where = "WHERE source_type = ?"
            params.append(source_type)
        rows = con.execute(
            f"SELECT source_type, COUNT(*) FROM documents {where} "
            "GROUP BY source_type ORDER BY COUNT(*) DESC",
            params,
        ).fetchall()
        return {
            "total_documents": sum(r[1] for r in rows),
            "by_source_type": [{"source_type": r[0], "documents": r[1]} for r in rows],
        }
    except Exception as exc:
        msg = str(exc)
        if "does not exist" in msg or "not found" in msg.lower():
            return {"total_documents": 0, "by_source_type": [], "status": "table_missing: documents"}
        return {"error": msg}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "topics": {"type": "array"}},
        "additionalProperties": True,
    },
)
def sentiment_by_topic(days: int = 30) -> dict:
    """Average sentiment score and article count per topic (category) in the
    window.

    Args:
        days: Look-back window in days (default 30, max 90).
    """
    days = min(max(1, days), 90)
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        rows = con.execute(
            """
            SELECT category, AVG(sentiment_score), COUNT(*)
            FROM news_articles WHERE publish_date >= ?
            GROUP BY category ORDER BY COUNT(*) DESC LIMIT 20
            """,
            [_cutoff(days)],
        ).fetchall()
        return {
            "count": len(rows),
            "window_days": days,
            "topics": [
                {
                    "topic": r[0],
                    "avg_sentiment": float(r[1]) if r[1] is not None else 0.0,
                    "articles": r[2],
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "cells": {"type": "array"}},
        "additionalProperties": True,
    },
)
def sentiment_heatmap(days: int = 14) -> dict:
    """Topic-by-day average sentiment cells for the heatmap panel.

    Args:
        days: Look-back window in days (default 14, max 60).
    """
    days = min(max(1, days), 60)
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        rows = con.execute(
            """
            SELECT category, CAST(publish_date AS DATE), AVG(sentiment_score), COUNT(*)
            FROM news_articles WHERE publish_date >= ?
            GROUP BY 1, 2 ORDER BY 2 DESC, 4 DESC LIMIT 200
            """,
            [_cutoff(days)],
        ).fetchall()
        return {
            "count": len(rows),
            "window_days": days,
            "cells": [
                {
                    "topic": r[0],
                    "date": r[1].isoformat() if r[1] else None,
                    "avg_sentiment": float(r[2]) if r[2] is not None else 0.0,
                    "articles": r[3],
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"count": {"type": "integer"}, "clusters": {"type": "array"}},
        "additionalProperties": True,
    },
)
def coverage_clusters(days: int = 7) -> dict:
    """Cluster-shaped coverage summary: articles grouped by category with
    volume, distinct-source count and latest publish time. This is a plain
    warehouse aggregation, not the API's event-clustering pipeline — it
    answers "where is coverage concentrated" for planners and inspection.

    Args:
        days: Look-back window in days (default 7).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        rows = con.execute(
            """
            SELECT category, COUNT(*), COUNT(DISTINCT source), MAX(publish_date)
            FROM news_articles WHERE publish_date >= ?
            GROUP BY category ORDER BY COUNT(*) DESC LIMIT 15
            """,
            [_cutoff(days)],
        ).fetchall()
        return {
            "count": len(rows),
            "window_days": max(1, days),
            "clusters": [
                {
                    "label": r[0],
                    "articles": r[1],
                    "sources": r[2],
                    "latest": r[3].isoformat() if r[3] else None,
                }
                for r in rows
            ],
        }
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Analytics plane (R5 / Track DS): the reference batch analytic and its        #
# panel-facing read tool, under the statistical-honesty contract.              #
# --------------------------------------------------------------------------- #

_ANOMALY_WINDOW_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "window_date": {"type": "string"},
            "metric": {"type": "string"},
            "value": {"type": "number"},
            "robust_z": {"type": "number"},
            "is_anomaly": {"type": "boolean"},
            "expected_band": INTERVAL_SCHEMA,
        },
    },
}


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "topic": {"type": ["string", "null"]},
            "metric": {"type": ["string", "null"]},
            "threshold": {"type": "number"},
            "flagged": {"type": "integer"},
            "windows": _ANOMALY_WINDOW_SCHEMA,
        }
    ),
)
def detect_anomalies(topic: Optional[str] = None, metric: Optional[str] = None) -> dict:
    """Windows where a topic's daily coverage volume or mean sentiment deviates
    from its own recent history (robust z-score over median/MAD).

    Reads the precomputed ``analytics_anomalies`` result table
    (trigger_detect_anomalies writes it); computes on-demand for a single
    topic when nothing is stored. Every window carries its expected band and
    the output carries its sample size, method and assumptions.

    Args:
        topic:  Restrict to one topic (category). Omit for all topics.
        metric: "volume" or "sentiment". Omit for both.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.anomalies import detect_anomalies_payload

        return detect_anomalies_payload(con, topic=topic, metric=metric)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def trigger_detect_anomalies(threshold: float = 3.5) -> dict:
    """Run the anomaly-detection batch fit and persist ``analytics_anomalies``
    (RW). Logs the fit to MLflow when available. Idempotent — re-running
    overwrites each (topic, metric, window) row.

    Args:
        threshold: robust-z magnitude above which a window is flagged.
    """
    import threading

    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.anomalies import AnomalyJob
        from src.analytics.framework import run_job

        return run_job(AnomalyJob(threshold=threshold), conn=con, lock=threading.Lock())
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Analytics breadth (R6 / Track DS Wave 1b): lead-lag, narratives, drift,      #
# forecast. Read-only reads over news_articles; on-demand (cheap) with an      #
# optional batch trigger for the precompute pattern.                           #
# --------------------------------------------------------------------------- #

def _outlet_list(outlets: Optional[str]):
    if not outlets:
        return None
    return [o.strip() for o in outlets.split(",") if o.strip()]


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "topic": {"type": "string"},
            "outlets": {"type": "array"},
            "pairs": {"type": "array"},
        }
    ),
)
def lead_lag(topic: str, outlets: Optional[str] = None) -> dict:
    """Which outlets lead vs follow on a topic, by cross-correlation of their
    daily coverage series. Positive lag means the leader publishes first.

    Args:
        topic:   the topic (category) to analyze.
        outlets: optional comma-separated outlet allowlist.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.lead_lag import lead_lag_payload

        return lead_lag_payload(con, topic, _outlet_list(outlets))
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema(
        {"topic": {"type": ["string", "null"]}, "clusters": {"type": "array"}}
    ),
)
def cluster_narratives(topic: Optional[str] = None, days: Optional[int] = None) -> dict:
    """Competing narrative threads on a topic: documents clustered by shared
    vocabulary, reported with cluster size, cohesion and top terms.

    Args:
        topic: the topic (category) to cluster. Omit for the whole corpus.
        days:  optional look-back window in days.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.narratives import cluster_narratives_payload

        return cluster_narratives_payload(con, topic, days)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "term": {"type": "string"},
            "drift": INTERVAL_SCHEMA,
            "rising_terms": {"type": "array"},
            "falling_terms": {"type": "array"},
        }
    ),
)
def semantic_drift(term: str, window: int = 90) -> dict:
    """How a term's *meaning* (its coverage context) shifts across a window,
    comparing the early and late halves, with a bootstrap interval.

    Args:
        term:   the term or entity to track.
        window: look-back window in days (default 90).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.drift import semantic_drift_payload

        return semantic_drift_payload(con, term, window)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "topic": {"type": "string"},
            "horizon": {"type": "integer"},
            "history": {"type": "array"},
            "points": {"type": "array"},
        }
    ),
)
def forecast_topic(topic: str, horizon: int = 7) -> dict:
    """Forecast a topic's daily coverage velocity with Holt exponential
    smoothing. Every step carries a prediction interval.

    Args:
        topic:   the topic (category) to forecast.
        horizon: days ahead to project (default 7, max 30).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.drift import forecast_topic_payload

        return forecast_topic_payload(con, topic, horizon)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def trigger_lead_lag() -> dict:
    """Run the lead-lag batch fit across all topics into ``analytics_lead_lag``
    (RW). Logs to MLflow when available."""
    import threading

    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.lead_lag import LeadLagJob
        from src.analytics.framework import run_job

        return run_job(LeadLagJob(), conn=con, lock=threading.Lock())
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def trigger_cluster_narratives() -> dict:
    """Run the narrative-clustering batch fit across all topics into
    ``analytics_narratives`` (RW). Logs to MLflow when available."""
    import threading

    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.narratives import NarrativeJob
        from src.analytics.framework import run_job

        return run_job(NarrativeJob(), conn=con, lock=threading.Lock())
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "figures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "document_id": {"type": "string"},
                        "source_type": {"type": ["string", "null"]},
                        "title": {"type": ["string", "null"]},
                        "content": {"type": ["string", "null"]},
                        "content_ref": {"type": ["string", "null"]},
                        "parent_document_id": {"type": ["string", "null"]},
                        "figure_label": {"type": ["string", "null"]},
                    },
                    "additionalProperties": True,
                },
            },
            "count": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
        "additionalProperties": True,
    },
)
def figure_evidence(topic: Optional[str] = None) -> dict:
    """Figure documents (metadata.modality='image') matching an optional topic,
    each with its description, image content_ref, and parent-document citation.

    Args:
        topic: optional case-insensitive substring over the figure text.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"figures": [], "count": 0, "note": str(exc)}
    try:
        from src.ingestion.describers.figure_query import figure_evidence as _fe

        return _fe(con, topic)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {
            "entries": {"type": "array"},
            "count": {"type": "integer"},
        },
        "additionalProperties": True,
    },
)
def corrections_ledger(change_class: Optional[str] = None) -> dict:
    """Documents whose content changed after ingest, classified.

    Args:
        change_class: optional filter (silent_substantive, correction_notice,
            retraction, takedown).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"entries": [], "count": 0, "note": str(exc)}
    try:
        from src.ingestion.corrections import corrections_ledger as _cl

        return _cl(con, change_class=change_class)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema={
        "type": "object",
        "properties": {"places": {"type": "array"}, "count": {"type": "integer"}},
        "additionalProperties": True,
    },
)
def geo_map(topic: Optional[str] = None) -> dict:
    """Geocoded places across the documents matching a topic, with per-place
    independent-source counts.

    Args:
        topic: optional topic filter.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"places": [], "count": 0, "note": str(exc)}
    try:
        from src.analytics.geospatial import place_coverage

        return place_coverage(con, topic)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema({
        "speakers": {"type": "array"},
        "interruptions": {"type": "array"},
        "speaker_count": {"type": "integer"},
        "total_airtime_s": {"type": "number"},
    }),
)
def speaker_balance(media: Optional[str] = None) -> dict:
    """Per-speaker airtime, floor share and interruptions over diarized
    transcript segments.

    Args:
        media: optional substring of the recording's url/content_ref.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.speaker_turns import speaker_balance as _sb

        return _sb(con, media)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Semantic search over the document embedding sink (document_embeddings).
# The index is built by trigger_embed_documents; queries embed with the
# env-configured provider (EMBEDDING_PROVIDER; "hashing" for an offline default).
# --------------------------------------------------------------------------- #


@mcp.tool
def semantic_search(query: str, top_k: int = 10) -> dict:
    """Documents most semantically similar to a free-text query, by embedding
    cosine, each cited to its source. Returns a note if the corpus is not yet
    embedded (run trigger_embed_documents first).

    Args:
        query: the free-text query to search for.
        top_k: number of results to return (default 10).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.semantic_search import semantic_search as _search

        return _search(con, query, top_k=top_k)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def similar_documents(document_id: str, top_k: int = 10) -> dict:
    """Documents most similar to a given document (by embedding cosine),
    excluding the document itself.

    Args:
        document_id: the document to find neighbours for.
        top_k: number of neighbours to return (default 10).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.semantic_search import similar_documents as _sim

        return _sim(con, document_id, top_k=top_k)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def near_duplicate_documents(threshold: float = 0.9) -> dict:
    """Clusters of near-identical documents by embedding cosine (>= threshold);
    flags reuse/echo. Similarity can be coincidental (wire copy, quotations).

    Args:
        threshold: cosine cutoff for two documents to count as near-duplicates.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.semantic_search import near_duplicates as _nd

        return _nd(con, threshold=threshold)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def trigger_embed_documents(limit: Optional[int] = None) -> dict:
    """Embed documents that have no embedding yet into ``document_embeddings``
    (RW), using the env-configured embedding provider. Idempotent; returns the
    number embedded.

    Args:
        limit: optional cap on how many documents to embed this pass.
    """
    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.ingestion.embed import embed_documents

        embedded = embed_documents(con, limit=limit)
        return {"embedded": embedded}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Summarization over the corpus (document_summaries sink).
# The batch index is built by trigger_summarize_documents; single-document and
# per-topic summaries are computed read-only (extractive) when not pre-stored.
# --------------------------------------------------------------------------- #


@mcp.tool
def summarize_document(document_id: str) -> dict:
    """A short summary of one document — the stored summary if the batch has run,
    else an extractive summary computed on the fly from its content.

    Args:
        document_id: the document to summarize.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.ingestion.summarize import document_summary

        return document_summary(con, document_id)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def summarize_topic(topic: str) -> dict:
    """A short brief for a topic (category), summarizing its most recent
    documents extractively, with the documents it drew on.

    Args:
        topic: the topic (category) to summarize.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.ingestion.summarize import summarize_topic as _st

        return _st(con, topic)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def trigger_summarize_documents(limit: Optional[int] = None) -> dict:
    """Summarize documents that have no summary yet into ``document_summaries``
    (RW), using the extractive summarizer. Idempotent; returns the count.

    Args:
        limit: optional cap on how many documents to summarize this pass.
    """
    try:
        con = _warehouse_rw()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.ingestion.summarize import summarize_documents

        return {"summarized": summarize_documents(con, limit=limit)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# Embedding-based topic modelling over document_embeddings. Read-only; the
# lexical cluster_narratives tool is the bag-of-words counterpart.
# --------------------------------------------------------------------------- #


@mcp.tool
def topic_model(min_similarity: float = 0.35, min_cluster_size: int = 3) -> dict:
    """Unsupervised topics over the document embeddings — clusters of similar
    documents, each labelled with its salient terms. Requires the corpus to be
    embedded (run trigger_embed_documents first).

    Args:
        min_similarity: cosine threshold for grouping documents into a topic.
        min_cluster_size: drop topics smaller than this many documents.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.analytics.topics import model_topics

        return model_topics(con, min_similarity=min_similarity,
                            min_cluster_size=min_cluster_size)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


if __name__ == "__main__":
    from src.mcp_host.transport import run_server

    run_server(mcp)  # stdio by default; HTTP via NOESIS_MCP_TRANSPORT=http
