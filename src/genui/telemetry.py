"""
Pack-supplied empty-canvas telemetry (R3 / Track N2).

The empty canvas's ambient signal (KPI strip, movers list, ticker) is no
longer hardcoded to news hooks: enabled domain packs advertise telemetry
via ``DomainPack.telemetry`` and this module collects it. When no enabled
pack contributes a slot, the engine-level **library** telemetry fills it
from the document corpus (the documents table, falling back to
news_articles — both are documents in the corpus sense), so a canvas with
the news pack disabled still shows a live ambient signal: recently
ingested documents instead of an empty gap.

Every failure degrades to an empty slot; telemetry must never break the
canvas or the API.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_MOVERS = 5
MAX_TICKER = 6


def _clean_signals(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("label"), str):
            out.append({"label": item["label"], "value": item.get("value", 0)})
    return out


def _clean_movers(raw: Any) -> List[Dict[str, Any]]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if (
            isinstance(item, dict)
            and isinstance(item.get("label"), str)
            and isinstance(item.get("intent"), str)
        ):
            mover = {"label": item["label"], "intent": item["intent"]}
            if isinstance(item.get("change"), (int, float)):
                mover["change"] = item["change"]
            out.append(mover)
    return out


def _clean_ticker(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("label"), str):
        return None
    items = [i for i in raw.get("items") or [] if isinstance(i, str) and i.strip()]
    return {"label": raw["label"], "items": items[:MAX_TICKER]} if items else None


def _library_telemetry() -> Dict[str, Any]:
    """Engine-level fallback: the document corpus itself is the signal."""
    from src.database.local_analytics_connector import _LOCK, get_shared_connection

    conn = get_shared_connection()
    with _LOCK:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'main'"
            ).fetchall()
        }
        if "documents" in tables:
            total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            rows = conn.execute(
                "SELECT title, source_type FROM documents "
                "ORDER BY created_at DESC NULLS LAST LIMIT ?",
                [MAX_TICKER],
            ).fetchall()
            types = conn.execute(
                "SELECT source_type, COUNT(*) FROM documents "
                "GROUP BY source_type ORDER BY COUNT(*) DESC LIMIT ?",
                [MAX_MOVERS],
            ).fetchall()
        elif "news_articles" in tables:
            total = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
            rows = conn.execute(
                "SELECT title, category FROM news_articles "
                "ORDER BY publish_date DESC NULLS LAST LIMIT ?",
                [MAX_TICKER],
            ).fetchall()
            types = conn.execute(
                "SELECT category, COUNT(*) FROM news_articles "
                "GROUP BY category ORDER BY COUNT(*) DESC LIMIT ?",
                [MAX_MOVERS],
            ).fetchall()
        else:
            return {}

    movers = [
        {
            "label": str(kind),
            "intent": f"library documents about {str(kind).lower()}",
            "change": int(count),
        }
        for kind, count in types
        if kind
    ]
    return {
        "signals": [{"label": "DOCS", "value": int(total)}],
        "movers": movers,
        "ticker": {
            "label": "NEW IN LIBRARY",
            "items": [str(r[0]) for r in rows if r[0]],
        },
    }


def pack_telemetry() -> Dict[str, Any]:
    """Collect ambient telemetry from whichever packs are enabled.

    Returns ``{signals, movers, ticker, packs}``. ``packs`` lists the
    contributors; ``"library"`` marks the engine fallback.
    """
    signals: List[Dict[str, Any]] = []
    movers: List[Dict[str, Any]] = []
    ticker: Optional[Dict[str, Any]] = None
    contributing: List[str] = []

    try:
        from src.domains.registry import get_enabled_packs

        packs = get_enabled_packs()
    except Exception:
        packs = []

    for pack in packs:
        provider = getattr(pack, "telemetry", None)
        if provider is None:
            continue
        try:
            supplied = provider() or {}
        except Exception:
            logger.warning("telemetry provider of pack %r failed", pack.name, exc_info=True)
            continue
        pack_signals = _clean_signals(supplied.get("signals"))
        pack_movers = _clean_movers(supplied.get("movers"))
        pack_ticker = _clean_ticker(supplied.get("ticker"))
        if not (pack_signals or pack_movers or pack_ticker):
            continue
        signals.extend(pack_signals)
        movers.extend(pack_movers)
        if ticker is None:
            ticker = pack_ticker
        contributing.append(pack.name)

    # Engine fallback: fill whatever no pack supplied from the corpus.
    if not (signals and movers and ticker):
        try:
            library = _library_telemetry()
        except Exception:
            library = {}
        if library:
            if not signals:
                signals = _clean_signals(library.get("signals"))
            if not movers:
                movers = _clean_movers(library.get("movers"))
            if ticker is None:
                ticker = _clean_ticker(library.get("ticker"))
            contributing.append("library")

    return {
        "signals": signals,
        "movers": movers[:MAX_MOVERS],
        "ticker": ticker,
        "packs": contributing,
    }
