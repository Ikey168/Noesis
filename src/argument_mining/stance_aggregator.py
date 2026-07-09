"""
Aggregate per-source stance predictions into the `source_stances` DuckDB table.

For each (source, source_type, topic, time_window), runs the StanceClassifier
over recent documents and writes one row per stance label with counts.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from src.database.news_articles_compat import corpus_table

logger = logging.getLogger(__name__)

_WINDOW_DAYS = 7
_DOC_LIMIT = 200


def run_stance_aggregation(
    conn,
    lock: threading.Lock,
    window_days: int = _WINDOW_DAYS,
    *,
    limit: int = _DOC_LIMIT,
) -> dict:
    """
    Process recent documents, classify stance per (source, topic), and upsert
    aggregated counts into `source_stances`.

    Returns {"documents": n, "rows_written": m}.
    """
    from src.argument_mining.models import get_stance_classifier
    from services.ingest.common.document_model import Document

    now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=window_days)).date().isoformat()
    window_end = now.date().isoformat()
    computed_at = now.isoformat()

    with lock:
        rows = conn.execute(
            f"""
            SELECT id, title, content, source, category
            FROM {corpus_table(conn)}
            WHERE publish_date >= ?
            ORDER BY publish_date DESC
            LIMIT ?
            """,
            [window_start, limit],
        ).fetchall()

    if not rows:
        logger.info("stance_aggregator: no documents in window %s – %s", window_start, window_end)
        return {"documents": 0, "rows_written": 0}

    clf = get_stance_classifier()

    # agg[(source, source_type, topic)][stance] = {count, conf_sum}
    agg: dict[tuple, dict[str, dict]] = {}

    for doc_id, title, content, source, category in rows:
        if not content or not source:
            continue
        topic = category or "general"
        key = (source, "news", topic)
        if key not in agg:
            agg[key] = {
                s: {"count": 0, "conf_sum": 0.0}
                for s in ("supportive", "critical", "neutral", "ambiguous")
            }

        doc = Document(
            document_id=doc_id,
            source_type="news",
            language="en",
            ingested_at=int(time.time() * 1000),
            title=title,
            content=content,
        )
        for pred in clf.predict(doc, topic):
            bucket = agg[key].get(pred.stance)
            if bucket is None:
                continue
            bucket["count"] += 1
            bucket["conf_sum"] += pred.confidence

    rows_written = 0
    with lock:
        for (source, source_type, topic), stances in agg.items():
            # Replace any existing rows for this (source, source_type, topic, window_start)
            conn.execute(
                """
                DELETE FROM source_stances
                WHERE source = ? AND source_type = ? AND topic = ? AND window_start = ?
                """,
                [source, source_type, topic, window_start],
            )
            for stance, data in stances.items():
                if data["count"] == 0:
                    continue
                avg_conf = data["conf_sum"] / data["count"]
                conn.execute(
                    """
                    INSERT INTO source_stances
                        (source, source_type, topic, stance, confidence,
                         document_count, window_start, window_end, computed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [source, source_type, topic, stance, avg_conf,
                     data["count"], window_start, window_end, computed_at],
                )
                rows_written += 1

    logger.info(
        "stance_aggregator: processed %d docs, wrote %d rows (window %s – %s)",
        len(rows), rows_written, window_start, window_end,
    )
    return {"documents": len(rows), "rows_written": rows_written}


def run_once() -> None:
    """Entry point for scheduled execution."""
    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()
    result = run_stance_aggregation(conn, lock)
    logger.info("Stance aggregation complete: %s", result)


def schedule_stance_job(scheduler, *, hour: int = 3) -> None:
    """Register a nightly 03:00 UTC stance aggregation job."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        run_once,
        CronTrigger(hour=hour, minute=0),
        id="stance_aggregation",
        replace_existing=True,
    )
    logger.info("Stance aggregation scheduled at %02d:00 UTC", hour)
