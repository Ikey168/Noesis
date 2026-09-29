"""
Nightly fact-check batch runner (#97).

Run directly:
    python -m src.argument_mining.factcheck_scheduler

Or via cron / APScheduler:
    from src.argument_mining.factcheck_scheduler import schedule_factcheck_job
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def run_once(limit: int = 50) -> dict[str, int]:
    """Run one pass of the fact-check batch and return summary counts."""
    from src.database.local_analytics_connector import get_shared_connection
    from src.argument_mining.factcheck import run_factcheck_batch

    conn = get_shared_connection()
    lock = getattr(conn, "_lock", None) or threading.Lock()
    return run_factcheck_batch(conn, lock, limit=limit)


def schedule_factcheck_job(scheduler: object, hour: int = 2) -> None:
    """Register a nightly APScheduler job at *hour* UTC."""
    from apscheduler.triggers.cron import CronTrigger  # type: ignore[import]

    scheduler.add_job(  # type: ignore[attr-defined]
        run_once,
        trigger=CronTrigger(hour=hour, minute=0),
        id="factcheck_nightly",
        replace_existing=True,
        max_instances=1,
    )
    logger.info("Scheduled nightly fact-check batch at %02d:00 UTC", hour)


def suggest_checkworthiness_with_jev(*args, **kwargs) -> dict:
    """Explicit priority suggestion that does not change the scheduled batch."""
    from src.argument_mining.jev_nlp_adapters import suggest_checkworthiness

    return suggest_checkworthiness(*args, **kwargs)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    summary = run_once()
    print(f"Done: {summary}")
