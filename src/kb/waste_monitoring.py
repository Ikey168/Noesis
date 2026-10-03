"""Monitor waste releases, revisions and removals through subscriptions (#2740, WC10).

A waste monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) following :mod:`src.kb.business_statistics_monitoring`: no watcher table and no scheduler.
Its target is a place (a Geospatial place through accepted WC06 matches, or a published area code), an indicator
concept, a facility (INSPIRE id), one series or a provider, combinable. Each run evaluates one committed watermark
against a snapshot of every watched record's vintages; the subscription store turns new items into events, so a
replay, an idempotent re-acquisition or an unchanged re-publication emits nothing.

Notices are record changes, not assessments. Each cites the record revision before and after and states what changed:

* ``new_release`` - a series or transfer row not stated before;
* ``new_period`` - years a series' previous vintage did not state;
* ``revised_value`` - values, statuses or flags that changed, including a resubmitted past year (before and after as
  published); for a transfer row, a corrected quantity or method code for its reporting year;
* ``definition_change`` - a new definition revision or dataflow version;
* ``removed_by_source`` - a complete release no longer states the series or row.

:meth:`WasteMonitor.refresh` re-reads one declared source through its real adapter within the source's document
budget, records a receipt per run and is idempotent (an unchanged file adds nothing). A failed document stops the run,
records the failure (the source reads stale) and never produces a removal notice, because nothing is written. OECD
requests are paced at one per 60 seconds. Live releases from providers still ``unverified-live`` are withheld from
notices until a dated live run verifies them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.waste_sources import (
    CAPS,
    CONCEPTS,
    PROVIDERS,
    TRANSFER_PROVIDER,
    unverified,
)
from src.kb.waste_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    WasteError,
    authorize,
    canonical,
    digest,
    iso,
    table_exists,
)
from src.kb.waste_store import WasteProjector, WasteStore, citation

CONTRACT = "noesis-waste-notification-v1"
FILTER_KEYS = ("series_id", "provider", "place_id", "area", "concept", "inspire_id")
MESSAGES = {
    "new_release": "A source released a new watched waste series or transfer row",
    "new_period": "A release added years to a watched waste series",
    "revised_value": "A release revised published values, flags or a past year of a watched waste record",
    "definition_change": "A release changed the definition or dataflow version of a watched waste series",
    "removed_by_source": "A release no longer states a watched waste series or transfer row",
}
_DDL = """
CREATE TABLE IF NOT EXISTS waste_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def change_kinds(changes: Mapping[str, Any]) -> list[str]:
    if changes.get("new_series"):
        return ["new_release"]
    if changes.get("removed_by_source"):
        return ["removed_by_source"]
    kinds = []
    if changes.get("new_periods"):
        kinds.append("new_period")
    if changes.get("revised"):
        kinds.append("revised_value")
    if changes.get("definition_change"):
        kinds.append("definition_change")
    return kinds


def notifiable(item: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return item["evidence_origin"] != "live" or not unverified(item["provider"])


class WasteMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = WasteStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(target: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(target or {})
        if set(raw) - set(FILTER_KEYS):
            raise WasteError("invalid_watch", f"a waste monitor target uses {FILTER_KEYS}")
        if not any(raw.get(k) for k in FILTER_KEYS):
            raise WasteError("invalid_watch", "watch a place, an indicator, a facility, a series or a provider")
        if raw.get("concept") is not None and raw["concept"] not in CONCEPTS:
            raise WasteError("invalid_watch", f"concept is one of {CONCEPTS}")
        if raw.get("provider") is not None and raw["provider"] not in PROVIDERS:
            raise WasteError("invalid_watch", f"provider is one of {PROVIDERS}")
        return {k: raw.get(k) for k in FILTER_KEYS}

    def create(self, namespace: str, request_key: str, *, target: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(target)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "environment",
             "query": {"operation": "search", "kind": "waste-monitor", "filter": wanted},
             "filters": {"watch": "waste"}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "waste-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the climate-environment-waste source-pack schedule (or refresh()) acquires "
                                      "releases and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "waste-monitor":
            raise WasteError("monitor_not_found", "subscription is not a waste monitor")
        return subscription

    # ------------------------------------------------------------------ what is watched

    def watched(self, namespace: str, wanted: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        """(series ids, transfer row ids) a target covers."""
        if wanted.get("series_id"):
            self.store.series(namespace, wanted["series_id"])
            return [wanted["series_id"]], []
        rows: list[str] = []
        if wanted.get("inspire_id") or wanted.get("provider") == TRANSFER_PROVIDER:
            rows = [r["row_id"] for r in self.store.transfer_rows(namespace, inspire_id=wanted.get("inspire_id"))]
            if wanted.get("inspire_id"):
                return [], rows
        codes = None
        if wanted.get("place_id") or wanted.get("area"):
            if wanted.get("place_id"):
                from src.kb.waste_identity import WasteIdentity

                identity = WasteIdentity(self.conn, initialize=False, now=self.now)
                codes = [(c["scheme"], c["code"]) for c in identity.area_codes_for_place(namespace,
                                                                                         wanted["place_id"])]
            else:
                area = dict(wanted["area"])
                codes = [(area["scheme"], str(area["code"]))]
        if wanted.get("provider") == TRANSFER_PROVIDER:
            return [], rows
        series = [s["series_id"] for s in self.store.find_series(
            namespace, provider=wanted.get("provider"), concept=wanted.get("concept"), area_codes=codes)]
        return series, rows

    def _series_items(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
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
                    "definition_change": changes.get("definition_change"),
                    "removed_by_source": changes.get("removed_by_source"),
                }[kind]
                items.append({
                    "id": f"vintage:{vintage['vintage_id']}:{kind}", "item": kind, "record_kind": "series",
                    "record_id": series_id,
                    "record": {"provider": series["provider"], "dataset": series["dataset"],
                               "native_key": series["native_key"], "concept": series["indicator"]["concept"],
                               "area": series["area"], "unit": series["unit"]},
                    "vintage_id": vintage["vintage_id"],
                    "previous_vintage_id": None if previous is None else previous["vintage_id"],
                    "release_at": vintage["release_at"], "detail": detail,
                    "citation": citation(series, vintage, release),
                    "evidence_origin": release["evidence_origin"], "provider": release["provider"],
                })
            previous = vintage
        return items

    def _row_items(self, namespace: str, row_id: str) -> list[dict[str, Any]]:
        row = self.store.transfer_row(namespace, row_id)
        items, previous = [], None
        for vintage in self.store.transfer_vintages(namespace, row_id):
            release = self.store.release(namespace, vintage["release_id"]) if vintage["release_id"] else {}
            if previous is None:
                kind, detail = "new_release", {"quantity": vintage["quantity"], "method": vintage["method"]}
            elif vintage["status"] == "removed_by_source":
                kind, detail = "removed_by_source", {"before": {"quantity": previous["quantity"],
                                                                "method": previous["method"]},
                                                     "statement": "the source's complete response no longer states "
                                                                  "this row; it is not zero"}
            elif previous["status"] == "removed_by_source":
                kind, detail = "new_release", {"quantity": vintage["quantity"], "method": vintage["method"],
                                               "restated_after_removal": True}
            else:
                kind, detail = "revised_value", {"reporting_year": row["reporting_year"],
                                                 "before": {"quantity": previous["quantity"],
                                                            "method": previous["method"]},
                                                 "after": {"quantity": vintage["quantity"],
                                                           "method": vintage["method"]}}
            items.append({
                "id": f"vintage:{vintage['vintage_id']}:{kind}", "item": kind, "record_kind": "transfer_row",
                "record_id": row_id,
                "record": {"provider": TRANSFER_PROVIDER, "inspire_id": row["inspire_id"],
                           "reporting_year": row["reporting_year"], "hazardous": row["hazardous"],
                           "treatment": row["treatment"], "destination": row["destination"], "unit": "t"},
                "vintage_id": vintage["vintage_id"],
                "previous_vintage_id": None if previous is None else previous["vintage_id"],
                "release_at": vintage["release_at"], "detail": detail,
                "citation": {"provider": TRANSFER_PROVIDER, "row_id": row_id, "vintage_id": vintage["vintage_id"],
                             "environment_record_id": row["environment_record_id"],
                             "release_id": vintage["release_id"], "release_label": release.get("release_label"),
                             "dataset_version": release.get("dataset_version"), "as_of": vintage["release_at"],
                             "retrieved_at": vintage["retrieved_at"], "url": release.get("url")},
                "evidence_origin": release.get("evidence_origin") or "live", "provider": TRANSFER_PROVIDER,
            })
            previous = vintage
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        series, rows = self.watched(namespace, wanted)
        items = [i for s in series for i in self._series_items(namespace, s)]
        items += [i for r in rows for i in self._row_items(namespace, r)]
        kept = [i for i in items if notifiable(i)]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        latest = self.store.latest_release_ms(namespace)
        if latest is None:
            raise WasteError("not_ready", "no waste release yet; acquire first")
        vintages = [r[0] for r in self.conn.execute(
            "SELECT vintage_id FROM waste_vintages WHERE namespace=? ORDER BY vintage_id", [namespace]).fetchall()]
        if table_exists(self.conn, "waste_transfer_rows") and table_exists(self.conn, "environment_vintages"):
            vintages += [r[0] for r in self.conn.execute(
                "SELECT v.vintage_id FROM environment_vintages v JOIN waste_transfer_rows w ON "
                "w.environment_record_id=v.record_id WHERE w.namespace=? ORDER BY v.vintage_id",
                [namespace]).fetchall()]
        generation = digest(sorted(vintages))[:24]
        rows = self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("waste_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"waste_generation": generation, "observed_at": iso(latest)}

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
                "note": "notices report published record changes; nothing is assessed, filled, summed or derived"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        return [{
            "contract": CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['item']}",
            "object": key, "kind": item["item"],
            "message": f"{MESSAGES[item['item']]} ({item['provider']}, {item['release_at']}).",
            "record_kind": item["record_kind"], "record_id": item["record_id"], "record": item["record"],
            "vintage_id": item["vintage_id"], "previous_vintage_id": item["previous_vintage_id"],
            "what_changed": item["detail"], "citation": item["citation"],
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, max_documents: int | None = None,
                retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Re-read a source's declared documents within its budget; idempotent by file, one receipt per run."""
        from src.ingestion.source_packs import SourcePackError
        from src.ingestion.waste_sources import WasteAdapter

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT max(retry_at_ms) FROM waste_refresh_receipts WHERE namespace=? AND source_id=?",
            [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait or the request pacing applies; nothing was "
                                      "requested")
        adapter = WasteAdapter(source, transport=transport)
        provider = adapter.declared["provider"]
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents), int(source["budgets"]["max_pages"]))
        projector = WasteProjector(self.conn)
        retrieved = retrieved_at_ms if retrieved_at_ms is not None else now
        releases, stopped, retry_at, cursor = [], None, None, None
        run_id = f"refresh:{source['source_id']}:{now}"
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "release", "parameters": {},
                                           "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            except SourcePackError as exc:
                stopped = {"code": exc.code, "message": exc.message, **exc.details}
                if exc.code == "rate_limited":
                    retry_at = now + int(exc.details.get("retry_after_ms") or 86_400_000)
                break
            applied = projector.project_page(run_id=run_id, manifest=None, source=source, records=page.records,
                                             documents=[{"ingested_at": retrieved}], page_receipt=page.receipt,
                                             principal_id=principal_id)
            releases += [{"release_id": a["release_id"], "status": a["status"], "vintages": a.get("vintages", 0),
                          "removed": a.get("removed", 0), "truncated": a.get("truncated", False)} for a in applied]
            cursor = page.next_cursor
            if cursor is None:
                break
        if stopped:
            projector.store.record_failure(namespace, provider, code=stopped["code"], run_id=run_id,
                                           source_id=source["source_id"], scopes=scopes)
        pacing = int(CAPS[provider].get("min_request_interval_s") or 0) * 1000
        if pacing and retry_at is None:
            retry_at = now + pacing  # OECD: the stricter recorded limit until verified
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, releases, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, releases, stopped, retry_at, principal_id, note=None):
        body = {"source_id": source["source_id"], "status": status, "releases": releases,
                "new_releases": sum(1 for r in releases if r["status"] == "applied"),
                "unchanged_releases": sum(1 for r in releases if r["status"] == "unchanged"),
                "stopped": stopped, "retry_at": iso(retry_at), "requested_by": principal_id, "at": iso(now),
                "note": note or ("a failed run wrote nothing: no vintage was revised or removed" if stopped else None)}
        receipt_id = "waste-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO waste_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["CONTRACT", "FILTER_KEYS", "WasteMonitor", "change_kinds", "notifiable"]
