"""Monitor income, poverty and inequality releases through subscriptions (#2583, IP10).

An income monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) following :mod:`src.kb.campaign_finance_monitoring` and the labour monitor: no watcher
table and no scheduler. Its target is one series, a place (with an optional indicator concept and provider) or an
indicator concept (with an optional provider). Each run evaluates one committed watermark against a snapshot of every
watched series' vintages; the subscription store turns new items into events, so a replay, an idempotent
re-acquisition or a restart emits nothing, and an unchanged re-publication (which adds no vintage) emits nothing.

Each vintage yields one notice per change it records, citing the new vintage, the one it revises and the release:
``new_period``, ``revised_value``, ``removed_period``, ``ppp_revision``, ``definition_change``, ``withdrawn``, and
``new_release`` for a release that changes nothing else (each PIP release is its own vintage). Notices are record
changes, never assessments.

:meth:`IncomeMonitor.refresh` acquires a source's declared documents through its runtime adapter within the source's
page budget, records a receipt per run, stops at the first rate-limit answer and refuses a refresh before the
provider's ``Retry-After`` has passed, and refuses a source whose optional feature the Society bundle has not
selected. Live releases from providers still ``unverified-live`` are withheld from notices.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.income_distribution_sources import (
    CONCEPTS,
    PROVIDER_CONTRACTS,
    SOURCE_FEATURES,
    unverified,
)
from src.kb.income_distribution_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    IncomeError,
    authorize,
    canonical,
    digest,
    feature_state,
    iso_from_ms,
    table_exists,
)
from src.kb.income_distribution_store import IncomeProjector, IncomeStore

CONTRACT = "noesis-income-notification-v1"
FILTER_KEYS = ("series_id", "place", "concept", "provider")
MESSAGES = {
    "new_period": "A release of a watched income series added reference years",
    "revised_value": "A release revised values of a watched income series",
    "removed_period": "A release no longer states reference years of a watched income series",
    "ppp_revision": "A PPP revision restated values of a watched income series",
    "definition_change": "A release changed the definition, methodology or version of a watched income series",
    "withdrawn": "The source no longer publishes a watched income series",
    "new_release": "A new release of a watched income series (no value changed)",
}
_DDL = """
CREATE TABLE IF NOT EXISTS income_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(source_revision: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return source_revision["evidence_origin"] != "live" or not unverified(source_revision["provider"])


def change_kinds(changes: Mapping[str, Any]) -> list[str]:
    if changes.get("withdrawn"):
        return ["withdrawn"]
    kinds = []
    if changes.get("new_periods"):
        kinds.append("new_period")
    if changes.get("ppp_revision"):
        kinds.append("ppp_revision")
    elif any(r["before"]["value"] != r["after"]["value"] or r["before"]["status"] != r["after"]["status"]
             or r["before"]["flags"] != r["after"]["flags"] for r in changes.get("revised") or []):
        kinds.append("revised_value")
    if changes.get("removed_periods"):
        kinds.append("removed_period")
    if changes.get("definition_change"):
        kinds.append("definition_change")
    return kinds or ["new_release"]


class IncomeMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = IncomeStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(target: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(target or {})
        if set(raw) - set(FILTER_KEYS):
            raise IncomeError("invalid_watch", f"an income monitor target uses {FILTER_KEYS}")
        if not raw.get("series_id") and not raw.get("place") and not raw.get("concept"):
            raise IncomeError("invalid_watch", "watch a series, a place or an indicator concept")
        if raw.get("concept") is not None and raw["concept"] not in CONCEPTS:
            raise IncomeError("invalid_watch", f"concept is one of {CONCEPTS}")
        if raw.get("provider") is not None and raw["provider"] not in PROVIDER_CONTRACTS:
            raise IncomeError("invalid_watch", f"provider is one of {sorted(PROVIDER_CONTRACTS)}")
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
        return {**created, "refresh": "the society-statistics source-pack schedule (or refresh()) acquires releases "
                "and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "income-monitor":
            raise IncomeError("monitor_not_found", "subscription is not an income monitor")
        return subscription

    def watched_series(self, namespace: str, wanted: Mapping[str, Any]) -> list[str]:
        if wanted.get("series_id"):
            self.store.series(namespace, wanted["series_id"])
            return [wanted["series_id"]]
        if wanted.get("place"):
            from src.kb.income_distribution_queries import IncomeQueries

            places = IncomeQueries(self.conn, now=self.now)._place_codes(namespace, wanted["place"])
            areas = [(c["scheme"], c["code"]) for c in places["codes"]]
            if not areas:
                return []
            found = self.store.find_series(namespace, provider=wanted.get("provider"), concept=wanted.get("concept"),
                                           areas=areas)
        else:
            found = self.store.find_series(namespace, provider=wanted.get("provider"), concept=wanted.get("concept"))
        return sorted(s["series_id"] for s in found)

    def _items(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        series = self.store.series(namespace, series_id)
        items, previous = [], None
        for vintage in self.store.vintage_rows(namespace, series_id):
            revision = self.store.source_revision(namespace, vintage["release_id"])
            changes = vintage["changes"]
            for kind in change_kinds(changes) if previous is not None else ["new_period"]:
                detail = {
                    "new_period": {"periods": changes.get("new_periods")},
                    "revised_value": {"revised": changes.get("revised")},
                    "removed_period": {"periods": changes.get("removed_periods")},
                    "ppp_revision": {"basis": changes.get("ppp_revision_basis"),
                                     "restated_periods": changes.get("restated_periods"),
                                     "revised": changes.get("revised")},
                    "definition_change": {"definition": changes.get("definition"),
                                          "release_version": changes.get("release_version")},
                    "withdrawn": {"removed_periods": changes.get("removed_periods")},
                    "new_release": {"release_version": changes.get("release_version")},
                }[kind]
                items.append({
                    "id": f"vintage:{vintage['vintage_id']}:{kind}", "item": kind, "series_id": series_id,
                    "series": {"provider": series["provider"], "native_key": series["native_key"],
                               "concept": series["indicator"]["concept"], "area": series["area"]["code"],
                               "welfare_concept": series["welfare_concept"], "poverty_line": series["poverty_line"],
                               "ppp_base_year": series["ppp_base_year"]},
                    "vintage_id": vintage["vintage_id"], "economic_vintage_id": vintage["economic_vintage_id"],
                    "previous_vintage_id": None if previous is None else previous["vintage_id"],
                    "release_at": vintage["release_at"], "definition_id": vintage["definition_id"],
                    "detail": detail, "source_revision": revision,
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
        return max(latest, highest + 1), {"income_generation": generation, "observed_at": iso_from_ms(latest)}

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
                "note": "notices report published record changes; nothing is nowcast, filled or assessed"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        revision = item["source_revision"]
        return [{
            "contract": CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['item']}",
            "object": key, "kind": item["item"],
            "message": f"{MESSAGES[item['item']]} ({revision['provider']}, {item['release_at']}).",
            "series_id": item["series_id"], "series": item["series"], "vintage_id": item["vintage_id"],
            "economic_vintage_id": item["economic_vintage_id"], "previous_vintage_id": item["previous_vintage_id"],
            "definition_id": item["definition_id"], "detail": item["detail"], "source_revision": revision,
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None,
                max_documents: int | None = None) -> dict[str, Any]:
        """Acquire a source's declared documents within its page budget; idempotent by file, one receipt per run."""
        from src.ingestion.income_distribution_sources import IncomeDistributionAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        adapter = IncomeDistributionAdapter(source, transport=transport)
        feature = SOURCE_FEATURES[adapter.declared["provider"]]
        if feature_state(self.conn, feature) == "not_selected":
            return self._receipt(namespace, source, now, "feature_not_selected", [], None, None, principal_id,
                                 note=f"the Society bundle's optional {feature} feature is not selected; nothing "
                                 "was requested")
        waiting = self.conn.execute(
            "SELECT max(retry_at_ms) FROM income_refresh_receipts WHERE namespace=? AND source_id=?",
            [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents))
        projector = IncomeProjector(self.conn)
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
        receipt_id = "inc-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO income_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["CONTRACT", "IncomeMonitor", "change_kinds", "notifiable"]
