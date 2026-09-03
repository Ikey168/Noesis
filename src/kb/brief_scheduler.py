"""In-process daily KB brief delivery (no Airflow dependency)."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from src.kb.registry import DEFAULT_CONFIG_PATH

logger = logging.getLogger(__name__)
DEFAULT_BRIEF_CONFIG = Path(__file__).resolve().parents[2] / "config" / "daily-brief.yml"
_scheduler: Optional[object] = None


def load_brief_config(path: Optional[os.PathLike] = None) -> Dict[str, Any]:
    config_path = Path(path or os.getenv("NOESIS_DAILY_BRIEF_CONFIG") or DEFAULT_BRIEF_CONFIG)
    raw = yaml.safe_load(config_path.read_text()) if config_path.exists() else {}
    raw = raw or {}
    domains = raw.get("domains")
    if domains is not None and (
        not isinstance(domains, list) or not all(isinstance(value, str) for value in domains)
    ):
        raise ValueError("daily brief domains must be a list of domain names")
    budget = int(raw.get("budget", 15))
    if not 0 <= budget <= 15:
        raise ValueError("daily brief budget must be between 0 and 15")
    return {
        "domains": domains,
        "budget": budget,
        "hour_utc": int(raw.get("hour_utc", 7)),
        "minute_utc": int(raw.get("minute_utc", 0)),
        "recipient": os.getenv("NOESIS_DAILY_BRIEF_TO", str(raw.get("recipient") or "")).strip(),
    }


def deliver_daily_brief(*, config_path: Optional[os.PathLike] = None, conn=None) -> Dict[str, Any]:
    """Generate the configured watchlist and email it when a recipient exists."""
    from src.kb.brief import generate_brief

    cfg = load_brief_config(config_path)
    result = generate_brief(domains=cfg["domains"], budget=cfg["budget"], conn=conn)
    recipient = cfg["recipient"]
    if not recipient:
        result["delivery"] = {"status": "not_configured", "recipient": None}
        return result
    from src.reports.email_sender import send_markdown_email

    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    send_markdown_email(
        to=recipient,
        subject=f"[Noesis] Daily cross-domain brief — {day}",
        markdown=result["markdown"],
    )
    result["delivery"] = {"status": "sent", "recipient": recipient}
    return result


def start_scheduler() -> Optional[object]:
    """Start one idempotent UTC scheduler when delivery is configured."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    cfg = load_brief_config()
    if not cfg["recipient"] or os.getenv("NOESIS_DAILY_BRIEF_ENABLED", "true").lower() in {
        "0", "false", "no", "off",
    }:
        logger.info("Daily brief email is disabled or has no recipient")
        return None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler is unavailable; daily brief email is disabled")
        return None
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        deliver_daily_brief,
        CronTrigger(hour=cfg["hour_utc"], minute=cfg["minute_utc"]),
        id="daily_kb_brief",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)  # type: ignore[attr-defined]
        _scheduler = None
