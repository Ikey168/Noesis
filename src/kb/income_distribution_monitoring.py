"""Monitor income, poverty and inequality releases through subscriptions (IP10, #2633).

An income monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) following :mod:`src.kb.campaign_finance_monitoring` and :mod:`src.kb.labour_monitoring`:
no watcher table and no scheduler. Its target is one series, an indicator concept, or a place (a Geospatial place
through accepted IP06 matches, or a published area code) with an optional concept. Each run evaluates one committed
watermark against a snapshot of every watched series' vintages; the subscription store turns new items into events,
so a replay, an idempotent re-acquisition or an unchanged re-publication emits nothing.

Notices are record changes, not assessments. Each cites the vintage before and after and states what changed:

* ``new_release`` - a release of a series not stated before (a new series);
* ``new_period`` - reference years the previous vintage did not state;
* ``revised_value`` - values, statuses, flags or estimation labels that changed (before and after as published);
* ``ppp_revision`` - the release restates values under another PPP revision;
* ``definition_change`` - a new definition revision or dataflow version;
* ``removed_by_source`` - a complete release no longer states the series.

:meth:`IncomeMonitor.refresh` re-reads one declared source through its real adapter within the source's document
budget, records a receipt per run and is idempotent (an unchanged file adds nothing). Live releases from providers
still ``unverified-live`` are withheld from notices until a dated live run verifies them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.income_distribution_sources import CONCEPTS, unverified
from src.kb.income_distribution_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    IncomeError,
    authorize,
    canonical,
    digest,
    iso,
    table_exists,
)
from src.kb.income_distribution_store import (
    IncomeDistributionProjector,
    IncomeDistributionStore,
    citation,
)

CONTRACT = "noesis-income-notification-v1"
FILTER_KEYS = ("series_id", "place_id", "area", "concept")
MESSAGES = {
    "new_release": "A source released a new watched income series",
    "new_period": "A release added reference years to a watched income series",
    "revised_value": "A release revised published values of a watched income series",
    "ppp_revision": "A release restated a watched income series under a PPP revision",
    "definition_change": "A release changed the definition or dataflow version of a watched income series",
    "removed_by_source": "A release no longer states a watched income series",
}
_DDL = """
CREATE TABLE IF NOT EXISTS income_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def change_kinds(changes: Mapping[str, Any]) -> list[str]:
    kinds = []
    if changes.get("new_series"):
        return ["new_release"]
    if changes.get("removed_by_source"):
        return ["removed_by_source"]
    if changes.get("new_periods"):
        kinds.append("new_period")
    if changes.get("revised"):
        kinds.append("revised_value")
    if changes.get("ppp_revision"):
        kinds.append("ppp_revision")
    if changes.get("definition_change"):
        kinds.append("definition_change")
    return kinds


def notifiable(release: Mapping[str, Any]) -> bool:
    """Fixture evidence, or live evidence from a provider whose live access is verified."""
    return release["evidence_origin"] != "live" or not unverified(release["provider"])


class IncomeMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = IncomeDistributionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(target: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(target or {})
        if set(raw) - set(FILTER_KEYS):
            raise IncomeError("invalid_watch", f"an income monitor target uses {FILTER_KEYS}")
        if not (raw.get("series_id") or raw.get("place_id") or raw.get("area") or raw.get("concept")):
            raise IncomeError("invalid_watch", "watch a series, an indicator concept or a place")
        if raw.get("concept") is not None and raw["concept"] not in CONCEPTS:
            raise IncomeError("invalid_watch", f"concept is one of {CONCEPTS}")
        return {k: raw.get(k) for k in FILTER_KEYS}

    def create(self, namespace: str, request_key: str, *, target: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(target)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "society",
             "query": {"operation": "search", "kind": "income-monitor", "filter": wanted},
             "filters": {"watch": "income-distribution"}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "income-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the society-income-distribution source-pack schedule (or refresh()) acquires "
                "releases and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "income-monitor":
            raise IncomeError("monitor_not_found", "subscription is not an income monitor")
        return subscription

    def watched_series(self, namespace: str, wanted: Mapping[str, Any]) -> list[str]:
        if wanted.get("series_id"):
            self.store.series(namespace, wanted["series_id"])
            return [wanted["series_id"]]
        codes = None
        if wanted.get("place_id") or wanted.get("area"):
            from src.kb.income_distribution_identity import IncomeIdentity

            identity = IncomeIdentity(self.conn, initialize=False, now=self.now)
            if wanted.get("place_id"):
                codes = [(c["scheme"], c["code"]) for c in identity.areas_for_place(namespace, wanted["place_id"])]
            else:
                area = dict(wanted["area"])
                codes = [(area["scheme"], str(area["code"]))]
        return [s["series_id"] for s in self.store.find_series(namespace, concept=wanted.get("concept"),
                                                               area_codes=codes)]

    def _items(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        series = self.store.series(namespace, series_id)
        items, previous = [], None
        for vintage in self.store.vintage_rows(namespace, series_id):
            release = self.store.release(namespace, vintage["release_id"])
            changes = vintage["changes"]
            for kind in change_kinds(changes):
                detail = {
                    "new_release": {"periods": changes.get("new_periods")},
                    "new_period": {"periods": changes.get("new_periods")},
                    "revised_value": {"revised": changes.get("revised")},
                    "ppp_revision": changes.get("ppp_revision"),
                    "definition_change": changes.get("definition_change"),
                    "removed_by_source": changes.get("removed_by_source"),
                }[kind]
                items.append({
                    "id": f"vintage:{vintage['vintage_id']}:{kind}", "item": kind, "series_id": series_id,
                    "series": {"provider": series["provider"], "native_key": series["native_key"],
                               "concept": series["indicator"]["concept"], "area": series["area"]},
                    "vintage_id": vintage["vintage_id"],
                    "previous_vintage_id": None if previous is None else previous["vintage_id"],
                    "release_at": vintage["release_at"], "detail": detail,
                    "citation": citation(series, vintage, release),
                    "evidence_origin": release["evidence_origin"], "provider": release["provider"],
                })
            previous = vintage
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        items = [i for s in self.watched_series(namespace, wanted) for i in self._items(namespace, s)]
        kept = [i for i in items if notifiable(i)]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        latest = self.store.latest_release_ms(namespace)
        if latest is None:
            raise IncomeError("not_ready", "no income release yet; acquire first")
        generation = digest([r[0] for r in self.conn.execute(
            "SELECT vintage_id FROM income_vintages WHERE namespace=? ORDER BY vintage_id", [namespace]).fetchall()])[:24]
        rows = self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("income_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"income_generation": generation, "observed_at": iso(latest)}

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
                "note": "notices report published record changes; nothing is nowcast, assessed or combined"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        return [{
            "contract": CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['item']}",
            "object": key, "kind": item["item"],
            "message": f"{MESSAGES[item['item']]} ({item['series']['provider']}, {item['release_at']}).",
            "series_id": item["series_id"], "series": item["series"], "vintage_id": item["vintage_id"],
            "previous_vintage_id": item["previous_vintage_id"], "what_changed": item["detail"],
            "citation": item["citation"],
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None,
                max_documents: int | None = None, retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Re-read a source's declared documents within its budget; idempotent by file, one receipt per run."""
        from src.ingestion.income_distribution_sources import IncomeDistributionAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT max(retry_at_ms) FROM income_refresh_receipts WHERE namespace=? AND source_id=?",
            [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = IncomeDistributionAdapter(source, transport=transport)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents), int(source["budgets"]["max_pages"]))
        projector = IncomeDistributionProjector(self.conn)
        projector.store.now = self.now
        releases, stopped, retry_at, cursor = [], None, None, None
        stamp = [{"ingested_at": retrieved_at_ms}] if retrieved_at_ms is not None else None
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
                                             source=source, records=page.records, documents=stamp,
                                             page_receipt=page.receipt, principal_id=principal_id)
            releases += [{"release_id": a["release_id"], "status": a["status"], "vintages": a.get("vintages", 0),
                          "removed_series": a.get("removed_series", 0)} for a in applied]
            cursor = page.next_cursor
            if cursor is None:
                break
        if stopped:
            projector.store.record_failure(namespace, adapter.declared["provider"], code=stopped["code"],
                                           run_id=f"refresh:{source['source_id']}:{now}",
                                           source_id=source["source_id"], scopes=scopes)
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, releases, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, releases, stopped, retry_at, principal_id, note=None):
        body = {"source_id": source["source_id"], "status": status, "releases": releases,
                "new_releases": sum(1 for r in releases if r["status"] == "applied"),
                "unchanged_releases": sum(1 for r in releases if r["status"] == "unchanged"),
                "stopped": stopped, "retry_at": iso(retry_at), "requested_by": principal_id, "at": iso(now),
                "note": note}
        receipt_id = "inc-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO income_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["CONTRACT", "IncomeMonitor", "change_kinds", "notifiable"]
