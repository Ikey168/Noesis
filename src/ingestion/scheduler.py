"""
Health-aware harvest scheduler (orchestration reintegration).

The adaptive layer (``SourceHealthTracker.due()`` / quarantine back-off, #879)
was consulted *inside* ``Connector.harvest_run`` but nothing drove repeated,
health-aware harvests — the only scheduled orchestration (the ``news_pipeline``
Airflow DAG) ran mock data. This is the missing driver: a small, dependency-light
loop that runs every registered connector through ``harvest_run`` with a shared,
persistent health tracker, so each :meth:`HarvestScheduler.run_once` lets the
adaptive scheduling decide what actually fetches.

Because ``harvest_run(respect_schedule=True)`` already skips sources that are not
:meth:`~src.ingestion.source_health.SourceHealthTracker.due` (backed off or
quarantined), invoking ``run_once`` from cron / a loop / a scheduled Routine
gives adaptive cadence — healthy sources at their base interval, empty ones
backing off, quarantined ones probed rarely — without a heavy orchestrator.

The store and health tracker are injected, so the scheduler is offline-testable.
:func:`main` wires it to the local warehouse for a real (or looped) run.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_SUMMARY_KEYS = (
    "discovered", "skipped", "fetched", "fetch_errors", "parsed",
    "parse_errors", "documents", "inserted", "duplicate", "invalid",
)


class HarvestScheduler:
    """Runs registered connectors through ``harvest_run`` with shared health."""

    def __init__(self, store, health, connectors: Optional[Sequence[Tuple[str, Any]]] = None):
        """``connectors`` is an explicit ``[(name, Connector), …]`` (tests); when
        omitted, the registered connectors are resolved from the registry."""
        self.store = store
        self.health = health
        self._connectors = list(connectors) if connectors is not None else None

    def _iter_connectors(self) -> List[Tuple[str, Any]]:
        if self._connectors is not None:
            return self._connectors
        import src.ingestion.connectors  # noqa: F401 - trigger registrations
        from src.ingestion.connectors.registry import available_source_types, get_connector

        return [(name, get_connector(name)) for name in available_source_types()]

    def run_once(self, query: Optional[Any] = None, now_ms: Optional[int] = None) -> Dict[str, Any]:
        """Run every connector once (health-aware); return a per-connector + total summary.

        Each connector runs with ``respect_schedule=True``, so sources that are
        not due (backed off / quarantined) are skipped this pass. A connector
        that raises is recorded and does not abort the others.
        """
        per_connector: Dict[str, Any] = {}
        totals: Dict[str, int] = {k: 0 for k in _SUMMARY_KEYS}

        for name, connector in self._iter_connectors():
            try:
                summary = connector.harvest_run(
                    query=query, store=self.store, health=self.health,
                    respect_schedule=True, now_ms=now_ms,
                )
            except Exception as exc:  # noqa: BLE001 - one connector never aborts the run
                logger.warning("harvest-scheduler: connector %s failed: %s", name, exc)
                per_connector[name] = {"error": str(exc)}
                continue
            d = summary.as_dict()
            per_connector[name] = d
            for k in _SUMMARY_KEYS:
                totals[k] += int(d.get(k, 0))

        logger.info(
            "harvest-scheduler: ran %d connectors — inserted=%d duplicate=%d "
            "invalid=%d skipped=%d",
            len(per_connector), totals["inserted"], totals["duplicate"],
            totals["invalid"], totals["skipped"],
        )
        return {"connectors": per_connector, "totals": totals}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the harvest scheduler against the local warehouse (once or on a loop).

    A lightweight alternative to a full orchestrator: cron, a scheduled Routine,
    or ``--loop`` drives adaptive, health-aware harvesting into the ``documents``
    corpus, with per-source back-off persisted at ``--health-path``.
    """
    import argparse
    import json
    import time

    parser = argparse.ArgumentParser(description="Run the health-aware harvest scheduler")
    parser.add_argument("--health-path", default=None,
                        help="JSON file for persistent per-source health/scheduling state")
    parser.add_argument("--loop", action="store_true",
                        help="Keep running, sleeping --interval seconds between passes")
    parser.add_argument("--interval", type=float, default=1800.0,
                        help="Seconds between passes when --loop (default 30 min)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    from src.database.local_analytics_connector import get_shared_connection
    from src.ingestion.document_store import DocumentStore
    from src.ingestion.source_health import SourceHealthTracker

    store = DocumentStore(get_shared_connection())
    health = SourceHealthTracker(args.health_path)
    scheduler = HarvestScheduler(store, health)

    def _pass() -> None:
        result = scheduler.run_once()
        print(json.dumps(result["totals"]))

    if not args.loop:
        _pass()
        return 0

    logger.info("harvest-scheduler: looping every %.0fs (Ctrl-C to stop)", args.interval)
    try:
        while True:
            _pass()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("harvest-scheduler: stopped")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
