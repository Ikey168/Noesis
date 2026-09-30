"""Monitor labour releases and revisions through subscriptions (#2219, LB10).

A labour monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) following :mod:`src.kb.demographics_monitoring` and the trade monitor: no watcher table
and no scheduler. Its target is one labour series, or a place, sector or occupation with an optional indicator
concept (resolved exactly as :class:`src.kb.labour_statistics.LabourQueries` resolves them, accepted mappings only).
Each run evaluates one committed watermark against a snapshot of every watched series' vintages; the subscription
store turns new items into events, so a replay, an idempotent re-acquisition or a restart emits nothing, and an
unchanged re-publication (which adds no vintage) emits nothing either.

Each vintage yields one notice per change it records, citing the vintage before and after (and the Economics
vintage row it lives in):

* ``new_period`` - periods the previous vintage did not state;
* ``revised_value`` - values, statuses, flags or footnotes that changed within the regular revision window
  (a preliminary value and its revision are distinct vintages);
* ``benchmark_revision`` - changed values beyond the declared revision window or a declared benchmark release;
* ``definition_change`` - a new definition revision or a changed dataflow version.

:meth:`LabourMonitor.refresh` acquires a source's declared documents through its runtime adapter within the
source's page budget, records a receipt per run, stops at the first rate-limit answer and refuses a refresh before
the provider's ``Retry-After`` has passed. Live releases from providers still ``unverified-live`` are withheld.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.labour_sources import CONCEPTS, unverified
from src.kb.labour_statistics import (
    READ_SCOPE,
    WRITE_SCOPE,
    LabourError,
    LabourProjector,
    LabourQueries,
    LabourStore,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    table_exists,
)

CONTRACT = "noesis-labour-notification-v1"
FILTER_KEYS = ("series_id", "place", "sector", "occupation", "concept")
MESSAGES = {
    "new_period": "A release of a watched labour series added periods",
    "revised_value": "A release revised values of a watched labour series",
    "benchmark_revision": "A benchmark revision changed values of a watched labour series",
    "definition_change": "A release changed the definition or metadata of a watched labour series",
}
_DDL = """
CREATE TABLE IF NOT EXISTS labour_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(source_revision: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return source_revision["evidence_origin"] != "live" or not unverified(source_revision["provider"])


def change_kinds(changes: Mapping[str, Any]) -> list[str]:
    kinds = []
    if changes.get("new_periods"):
        kinds.append("new_period")
    value_changes = [r for r in changes.get("revised") or []]
    if value_changes:
        kinds.append("benchmark_revision" if changes.get("benchmark_revision") else "revised_value")
    if changes.get("definition_change"):
        kinds.append("definition_change")
    return kinds


class LabourMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = LabourStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(target: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(target or {})
        if set(raw) - set(FILTER_KEYS):
            raise LabourError("invalid_watch", f"a labour monitor target uses {FILTER_KEYS}")
        if not raw.get("series_id") and not (raw.get("place") or raw.get("sector") or raw.get("occupation")):
            raise LabourError("invalid_watch", "watch a series, or a place, sector or occupation")
        if raw.get("concept") is not None and raw["concept"] not in CONCEPTS:
            raise LabourError("invalid_watch", f"concept is one of {CONCEPTS}")
        return {k: raw.get(k) for k in FILTER_KEYS}

    def create(self, namespace: str, request_key: str, *, target: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(target)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "economic",
                "query": {"operation": "search", "kind": "labour-monitor", "filter": wanted},
                "filters": {"watch": "labour-statistics"},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "labour-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {**created, "refresh": "the economic-statistics-and-filings source-pack schedule (or refresh()) "
                "acquires releases and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "labour-monitor":
            raise LabourError("monitor_not_found", "subscription is not a labour monitor")
        return subscription

    def watched_series(self, namespace: str, wanted: Mapping[str, Any]) -> list[str]:
        if wanted.get("series_id"):
            self.store.series(namespace, wanted["series_id"])
            return [wanted["series_id"]]
        answer = LabourQueries(self.conn, now=self.now).indicators(
            namespace, scopes={"operator"}, place=wanted.get("place"), sector=wanted.get("sector"),
            occupation=wanted.get("occupation"), concept=wanted.get("concept"))
        return sorted({r["series_id"] for r in answer["results"]} | {u["series_id"]
                                                                     for u in answer["unavailable_by_as_of"]})

    def _items(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        series = self.store.series(namespace, series_id)
        items, previous = [], None
        for vintage in self.store.vintage_rows(namespace, series_id):
            revision = self.store.source_revision(namespace, vintage["release_id"])
            changes = vintage["changes"]
            for kind in change_kinds(changes):
                detail = {
                    "new_period": {"periods": changes.get("new_periods")},
                    "revised_value": {"revised": changes.get("revised")},
                    "benchmark_revision": {"revised": changes.get("revised"),
                                           "basis": changes.get("benchmark_basis")},
                    "definition_change": {"definition": changes.get("definition"),
                                          "dataflow_version": changes.get("dataflow_version")},
                }[kind]
                items.append({
                    "id": f"vintage:{vintage['vintage_id']}:{kind}",
                    "item": kind,
                    "series_id": series_id,
                    "series": {"provider": series["provider"], "native_key": series["native_key"],
                               "concept": series["indicator"]["concept"],
                               "seasonal_adjustment": series["seasonal_adjustment"],
                               "area": series["area"]["code"]},
                    "vintage_id": vintage["vintage_id"],
                    "economic_vintage_id": vintage["economic_vintage_id"],
                    "previous_vintage_id": None if previous is None else previous["vintage_id"],
                    "release_at": vintage["release_at"],
                    "detail": detail,
                    "source_revision": revision,
                })
            previous = vintage
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        items = [i for s in self.watched_series(namespace, wanted) for i in self._items(namespace, s)]
        kept = [i for i in items if notifiable(i["source_revision"])]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        latest = self.store.latest_release_ms(namespace)
        if latest is None:
            raise LabourError("not_ready", "no labour release yet; acquire first")
        generation = digest([r[0] for r in self.conn.execute(
            "SELECT vintage_id FROM labour_vintages WHERE namespace=? ORDER BY vintage_id", [namespace]).fetchall()])[:24]
        rows = self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("labour_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"labour_generation": generation, "observed_at": iso_from_ms(latest)}

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            watermark, detail = self.default_watermark(namespace)
            self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion", detail=detail)
        result, withheld = self.snapshot(subscription)
        evaluated = self.subscriptions.evaluate(subscription_id, int(watermark), result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": int(watermark),
                "notifications": notifications, "withheld_unverified_live_items": withheld,
                "delivery": subscription["delivery"],
                "note": "notices report published record changes; nothing is nowcast or forecast"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        revision = item["source_revision"]
        return [{
            "contract": CONTRACT,
            "event_id": event_id,
            "notification_id": f"{event_id}:{item['item']}",
            "object": key,
            "kind": item["item"],
            "message": f"{MESSAGES[item['item']]} ({revision['provider']}, {item['release_at']}).",
            "series_id": item["series_id"],
            "series": item["series"],
            "vintage_id": item["vintage_id"],
            "economic_vintage_id": item["economic_vintage_id"],
            "previous_vintage_id": item["previous_vintage_id"],
            "detail": item["detail"],
            "source_revision": revision,
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, secret: str | None = None,
                max_documents: int | None = None) -> dict[str, Any]:
        """Acquire a source's declared documents within its page budget; idempotent by file, one receipt per run."""
        from src.ingestion.labour_sources import LabourStatisticsAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT max(retry_at_ms) FROM labour_refresh_receipts WHERE namespace=? AND source_id=?",
            [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = LabourStatisticsAdapter(source, transport=transport, secret=secret)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents))
        projector = LabourProjector(self.conn)
        projector.store.now = self.now
        releases, stopped, retry_at, cursor = [], None, None, None
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "release", "parameters": {},
                                           "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            except SourcePackError as exc:
                stopped = {"code": exc.code, "message": exc.message, **exc.details}
                if exc.code == "rate_limited":
                    retry_at = now + int(exc.details.get("retry_after_ms") or 86_400_000)
                break
            applied = projector.project_page(run_id=f"refresh:{source['source_id']}:{now}", manifest=None,
                                             source=source, records=page.records, documents=None,
                                             page_receipt=page.receipt, principal_id=principal_id)
            releases += [{"release_id": a["release_id"], "status": a["status"], "vintages": a.get("vintages", 0),
                          "provider_receipt": dict(page.receipt or {})} for a in applied]
            cursor = page.next_cursor
            if cursor is None:
                break
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, releases, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, releases, stopped, retry_at, principal_id, note=None):
        body = {"source_id": source["source_id"], "status": status, "releases": releases,
                "new_releases": sum(1 for r in releases if r["status"] == "applied"),
                "unchanged_releases": sum(1 for r in releases if r["status"] == "unchanged"),
                "stopped": stopped, "retry_at": iso_from_ms(retry_at), "requested_by": principal_id,
                "at": iso_from_ms(now), "note": note}
        receipt_id = "lb-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO labour_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["LabourMonitor", "change_kinds", "notifiable"]
