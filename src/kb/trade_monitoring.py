"""Monitor new trade releases and revisions through subscriptions (#2210, TF09).

A trade monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) whose query carries a country pair (both report directions: a pair's reporter and its
mirror) and optionally product codes and providers, following :mod:`src.kb.demographics_monitoring`: no watcher
table and no scheduler. Each run evaluates one committed watermark against a snapshot of every watched series'
vintages, and the subscription store turns new items into events (poll or outbox delivery). A replay, an
idempotent re-acquisition or a restart emits nothing.

Notices are record changes, not trade alerts:

* ``new_release`` - the first vintage of a watched series, or a later release that repeats every value;
* ``revision`` - a release that changes values, stating each changed period with the value (and status) before
  and after, the new vintage and the one it revises.

:meth:`TradeMonitor.refresh` acquires a watched source's declared documents through its runtime adapter within the
source's page budget, records a receipt for each refresh and stops at the first rate-limit answer; a refresh
requested before the provider's ``Retry-After`` has passed is refused without contacting the provider. Live
releases from providers still ``unverified-live`` are withheld from notices.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.trade_sources import unverified
from src.kb.trade_flows import (
    READ_SCOPE,
    WRITE_SCOPE,
    TradeError,
    TradeFlowProjector,
    TradeFlowStore,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    table_exists,
)

CONTRACT = "noesis-trade-notification-v1"
FILTER_KEYS = ("reporter", "partner", "products", "providers")
MESSAGES = {
    "new_release": "A new release of a watched trade series was published",
    "revision": "A release revised values of a watched trade series",
}
_DDL = """
CREATE TABLE IF NOT EXISTS trade_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(source_revision: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return source_revision["evidence_origin"] != "live" or not unverified(source_revision["provider"])


class TradeMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = TradeFlowStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(flow_filter: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(flow_filter or {})
        if set(raw) - set(FILTER_KEYS) or not raw.get("reporter") or not raw.get("partner"):
            raise TradeError("invalid_watch", f"a trade monitor names a reporter and a partner; keys are {FILTER_KEYS}")
        return {
            "reporter": str(raw["reporter"]),
            "partner": str(raw["partner"]),
            "products": sorted({str(p) for p in raw.get("products") or []}),
            "providers": sorted({str(p) for p in raw.get("providers") or []}),
        }

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        flow_filter: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(flow_filter)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "economic",
                "query": {"operation": "search", "kind": "trade-monitor", "filter": wanted},
                "filters": {"watch": "trade-flows"},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "trade-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "the economic-statistics-and-filings source-pack schedule (or refresh()) acquires releases and "
            "commits the watermarks this monitor evaluates; no new scheduler",
        }

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "trade-monitor":
            raise TradeError("monitor_not_found", "subscription is not a trade monitor")
        return subscription

    def watched_series(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The pair's series in both report directions (reporter and mirror), optionally by product and provider."""
        from src.kb.trade_queries import TradeQueries

        queries = TradeQueries(self.conn)
        left = queries._identity(namespace, wanted["reporter"])["codes"]
        right = queries._identity(namespace, wanted["partner"])["codes"]
        out = []
        for series in self.store.find_series(namespace):
            r, p = series["reporter"]["code"], series["partner"]["code"]
            if not ((r in left and p in right) or (r in right and p in left)):
                continue
            if wanted["products"] and series["product"]["code"] not in wanted["products"]:
                continue
            if wanted["providers"] and series["provider"] not in wanted["providers"]:
                continue
            out.append(series)
        return out

    def _items(self, namespace: str, series: Mapping[str, Any]) -> list[dict[str, Any]]:
        items, previous, before = [], None, {}
        for vintage in self.store.vintage_rows(namespace, series["series_id"]):
            values = {
                o["period"]: {"value": o["value"], "status": o["status"]}
                for o in self.store.observations(namespace, vintage["vintage_id"])
            }
            changes = [
                {"period": period, "before": before.get(period), "after": values.get(period)}
                for period in sorted(set(values) | set(before))
                if values.get(period) != before.get(period)
            ]
            items.append(
                {
                    "id": f"vintage:{vintage['vintage_id']}",
                    "item": "revision" if previous is not None and changes else "new_release",
                    "series_id": series["series_id"],
                    "series": {
                        "provider": series["provider"],
                        "reporter": series["reporter"]["code"],
                        "partner": series["partner"]["code"],
                        "flow": series["flow"]["direction"],
                        "product": series["product"]["code"],
                        "classification": series["classification"]["vintage"],
                        "role": series["role"],
                    },
                    "vintage_id": vintage["vintage_id"],
                    "release_at": vintage["release_at"],
                    "previous_vintage_id": None if previous is None else previous["vintage_id"],
                    "changed_values": changes if previous is not None else [],
                    "source_revision": self.store.source_revision(namespace, vintage["release_id"]),
                }
            )
            previous, before = vintage, values
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        items = [item for series in self.watched_series(namespace, wanted) for item in self._items(namespace, series)]
        kept = [i for i in items if notifiable(i["source_revision"])]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        """The namespace's current trade state as a watermark: reused when this state was already committed (a
        restart replays it and emits nothing), else a new one after every committed watermark."""
        latest = self.store.latest_release_ms(namespace)
        if latest is None:
            raise TradeError("not_ready", "no trade release yet; acquire first")
        generation = digest(
            [r[0] for r in self.conn.execute(
                "SELECT vintage_id FROM trade_vintages WHERE namespace=? ORDER BY vintage_id", [namespace]).fetchall()]
        )[:24]
        detail = {"trade_generation": generation}
        rows = (
            self.conn.execute(
                "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
                [namespace],
            ).fetchall()
            if table_exists(self.conn, "knowledge_subscription_watermarks")
            else []
        )
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("trade_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {**detail, "observed_at": iso_from_ms(latest)}

    def run(
        self, subscription_id: str, watermark: int | None = None, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            watermark, detail = self.default_watermark(namespace)
            self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion", detail=detail)
        result, withheld = self.snapshot(subscription)
        evaluated = self.subscriptions.evaluate(
            subscription_id, int(watermark), result, principal_id=principal_id, scopes=scopes,
            observed_at_ms=self.now(),
        )
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": int(watermark),
            "notifications": notifications,
            "withheld_unverified_live_items": withheld,
            "delivery": subscription["delivery"],
            "note": "notices report published record changes; they are not trade alerts",
        }

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        revision = item["source_revision"]
        return [
            {
                "contract": CONTRACT,
                "event_id": event_id,
                "notification_id": f"{event_id}:{item['item']}",
                "object": key,
                "kind": item["item"],
                "message": f"{MESSAGES[item['item']]} ({revision['provider']}, {revision['published_on']}).",
                "source_revision": revision,
                "vintage_id": item["vintage_id"],
                "previous_vintage_id": item["previous_vintage_id"],
                "changed_values": item["changed_values"],
                "series": item["series"],
                "note": "a publication reported as published; nothing is estimated or concluded about the change",
            }
        ]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(
        self,
        namespace: str,
        source: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
        max_documents: int | None = None,
    ) -> dict[str, Any]:
        """Acquire a source's declared documents within its page budget; idempotent by file, one receipt per run,
        stopped at the first rate-limit answer and refused before the provider's Retry-After has passed."""
        from src.ingestion.source_packs import SourcePackError
        from src.ingestion.trade_sources import TradeFlowsAdapter

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT retry_at_ms FROM trade_refresh_receipts WHERE namespace=? AND source_id=? "
            "ORDER BY started_at_ms DESC LIMIT 1",
            [namespace, source["source_id"]],
        ).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = TradeFlowsAdapter(source, transport=transport, secret=secret)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents))
        projector = TradeFlowProjector(self.conn)
        projector.store.now = self.now
        releases, stopped, retry_at, cursor = [], None, None, None
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "release", "parameters": {},
                                           "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            except SourcePackError as exc:
                stopped = {"code": exc.code, "message": exc.message, **exc.details}
                if exc.code == "rate_limited":
                    retry_at = now + int(exc.details.get("retry_after_ms") or 60_000)
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
        body = {
            "source_id": source["source_id"],
            "status": status,
            "releases": releases,
            "new_releases": sum(1 for r in releases if r["status"] == "applied"),
            "unchanged_releases": sum(1 for r in releases if r["status"] == "unchanged"),
            "stopped": stopped,
            "retry_at": iso_from_ms(retry_at),
            "requested_by": principal_id,
            "at": iso_from_ms(now),
            "note": note,
        }
        receipt_id = "tf-refresh:" + digest([namespace, body])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO trade_refresh_receipts VALUES (?,?,?,?,?,?,?)",
            [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)],
        )
        return {"receipt_id": receipt_id, **body}


__all__ = ["TradeMonitor", "notifiable"]
