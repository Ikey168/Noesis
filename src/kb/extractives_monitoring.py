"""Monitor new EITI reports, report revisions and new commodity releases through subscriptions (#2653, EX10).

An extractives monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) following :mod:`src.kb.campaign_finance_monitoring` and the labour monitor: no watcher
table and no scheduler. Its target is a company (an extractives company key, or an ownership entity with its
ownership namespace and optional group), a country (ISO alpha-3) or a commodity (a name as published or an HS
code, optionally with a country). Each run evaluates one committed watermark against a snapshot of the watched
records; the subscription store turns new items into events, so a replay, an idempotent re-acquisition or a restart
emits nothing, and an unchanged re-publication (which adds no revision or vintage) emits nothing either.

Notices are record changes, not assessments. Each cites the new or revised record and states what changed:

* ``new_report`` - a first revision of an EITI report (country and fiscal period);
* ``report_revision`` - a later revision: changed, added or removed lines, changed discrepancies, a withdrawal;
* ``new_release`` - a commodity series vintage from a new USGS release or BGS edition (new years);
* ``revised_values`` - values or statuses a later publication changed; ``removed_periods`` - years a later
  publication no longer states (the earlier vintage keeps them).

:meth:`ExtractivesMonitor.refresh` acquires a source's declared documents through its runtime adapter within the
page budget, records a receipt per run, stops at the first rate-limit answer and refuses a refresh before the
provider's ``Retry-After`` has passed. Live releases from providers still ``unverified-live`` are withheld.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.extractives_sources import STATISTICS, unverified
from src.kb.extractives_queries import ExtractivesQueries
from src.kb.extractives_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    ExtractivesError,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    table_exists,
)
from src.kb.extractives_store import ExtractivesProjector, ExtractivesStore

CONTRACT = "noesis-extractives-notification-v1"
FILTER_KEYS = ("company", "ownership_namespace", "group", "country", "commodity", "statistic")
MESSAGES = {
    "new_report": "A new EITI report was published for a watched subject",
    "report_revision": "A watched EITI report was revised",
    "new_release": "A new commodity release added figures for a watched subject",
    "revised_values": "A commodity release revised figures of a watched series",
    "removed_periods": "A commodity release no longer states years of a watched series",
}
_DDL = """
CREATE TABLE IF NOT EXISTS ex_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(source_revision: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return source_revision["evidence_origin"] != "live" or not unverified(source_revision["provider"])


class ExtractivesMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = ExtractivesStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(target: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(target or {})
        if set(raw) - set(FILTER_KEYS):
            raise ExtractivesError("invalid_watch", f"an extractives monitor target uses {FILTER_KEYS}")
        if not (raw.get("company") or raw.get("country") or raw.get("commodity")):
            raise ExtractivesError("invalid_watch", "watch a company, a country or a commodity")
        if raw.get("statistic") is not None and raw["statistic"] not in STATISTICS:
            raise ExtractivesError("invalid_watch", f"statistic is one of {STATISTICS}")
        if raw.get("commodity") and not raw.get("country"):
            raw["country"] = None
        return {k: raw.get(k) for k in FILTER_KEYS}

    def create(self, namespace: str, request_key: str, *, target: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(target)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "economic",
             "query": {"operation": "search", "kind": "extractives-monitor", "filter": wanted},
             "filters": {"watch": "extractives"}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "extractives-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the economic-statistics-and-filings source-pack schedule (or refresh()) "
                "acquires releases and commits the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "extractives-monitor":
            raise ExtractivesError("monitor_not_found", "subscription is not an extractives monitor")
        return subscription

    # ------------------------------------------------------------------ snapshot

    def _report_items(self, namespace: str, report_keys: Iterable[str]) -> list[dict[str, Any]]:
        items = []
        for key in sorted(set(report_keys)):
            for report in self.store.report_revisions(namespace, key):
                revision = self.store.source_revision(namespace, report["release_id"])
                kind = "new_report" if report["revision"] == 1 else "report_revision"
                changes = report["changes"]
                items.append({
                    "id": f"report:{report['report_id']}", "item": kind, "record_kind": "eiti_report",
                    "record_id": report["report_id"], "previous_record_id": report["revision_of"],
                    "subject": {"country": report["country"]["code"], "fiscal_period": report["fiscal_period"],
                                "report": report["report"]},
                    "release_at": report["release_at"],
                    "detail": {"changes": changes, "status": report["status"]}, "source_revision": revision})
        return items

    def _series_items(self, namespace: str, series_ids: Iterable[str]) -> list[dict[str, Any]]:
        items = []
        for series_id in sorted(set(series_ids)):
            series = self.store.series(namespace, series_id)
            previous = None
            for vintage in self.store.vintage_rows(namespace, series_id):
                revision = self.store.source_revision(namespace, vintage["release_id"])
                changes = vintage["changes"]
                kinds = []
                if changes.get("new_periods") or changes.get("first"):
                    kinds.append("new_release")
                if changes.get("revised"):
                    kinds.append("revised_values")
                if changes.get("removed_periods"):
                    kinds.append("removed_periods")
                for kind in kinds:
                    detail = {"new_release": {"periods": changes.get("new_periods")},
                              "revised_values": {"revised": changes.get("revised")},
                              "removed_periods": {"periods": changes.get("removed_periods"),
                                                  "note": "the earlier vintage keeps these years"}}[kind]
                    items.append({
                        "id": f"vintage:{vintage['vintage_id']}:{kind}", "item": kind,
                        "record_kind": "commodity_series", "record_id": vintage["vintage_id"],
                        "previous_record_id": None if previous is None else previous["vintage_id"],
                        "subject": {"series_id": series_id, "provider": series["provider"],
                                    "commodity": series["commodity"].get("name"), "statistic": series["statistic"],
                                    "country": series["country"].get("name")},
                        "release_at": vintage["release_at"], "publication": vintage["publication"],
                        "detail": detail, "source_revision": revision})
                previous = vintage
        return items

    def watched(self, namespace: str, wanted: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        queries = ExtractivesQueries(self.conn, now=self.now)
        reports: set[str] = set()
        series: set[str] = set()
        if wanted.get("company"):
            answer = queries.payments_for_company(
                namespace, wanted["company"], scopes={"operator"}, ownership_namespace=wanted.get(
                    "ownership_namespace"), group=bool(wanted.get("group")))
            keys = {s["subject_key"] for s in answer["matched_companies"]}
            for report_key in self.store.report_keys(namespace):
                if any(p["company_key"] in keys for r in self.store.report_revisions(namespace, report_key)
                       for p in self.store.payments(namespace, r["report_id"])):
                    reports.add(report_key)
        if wanted.get("country") and not wanted.get("commodity"):
            reports |= set(self.store.report_keys(namespace, country_code=str(wanted["country"]).upper()))
        if wanted.get("commodity"):
            identity = queries._identity()
            keys = {k["commodity_key"] for k in identity.commodity_keys_for(namespace, wanted["commodity"])["keys"]}
            names = None
            if wanted.get("country"):
                names = {n["name"] for n in identity.country_names_for(namespace, wanted["country"])["names"]}
            series |= {s["series_id"] for s in self.store.find_series(
                namespace, statistic=wanted.get("statistic"), commodity_keys=keys, country_names=names)}
        return sorted(reports), sorted(series)

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        reports, series = self.watched(namespace, wanted)
        items = self._report_items(namespace, reports) + self._series_items(namespace, series)
        kept = [i for i in items if notifiable(i["source_revision"])]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        latest = self.store.latest_retrieval_ms(namespace)
        if latest is None:
            raise ExtractivesError("not_ready", "no extractives release yet; acquire first")
        records = [r[0] for r in self.conn.execute(
            "SELECT report_id FROM ex_reports WHERE namespace=? UNION ALL SELECT vintage_id FROM ex_vintages WHERE "
            "namespace=?", [namespace, namespace]).fetchall()]
        generation = digest(sorted(records))[:24]
        rows = self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("extractives_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"extractives_generation": generation, "observed_at": iso_from_ms(latest)}

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
                "note": "notices report published record changes; nothing is scored, estimated or forecast"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        revision = item["source_revision"]
        return [{"contract": CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['item']}",
                 "object": key, "kind": item["item"],
                 "message": f"{MESSAGES[item['item']]} ({revision['provider']}, {item['release_at']}).",
                 "record_kind": item["record_kind"], "record_id": item["record_id"],
                 "previous_record_id": item["previous_record_id"], "subject": item["subject"],
                 "detail": item["detail"], "source_revision": revision}]

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
        from src.ingestion.extractives_sources import ExtractivesAdapter
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT max(retry_at_ms) FROM ex_refresh_receipts WHERE namespace=? AND source_id=?",
            [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = ExtractivesAdapter(source, transport=transport)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents))
        projector = ExtractivesProjector(self.conn)
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
            releases += [{"release_id": a["release_id"], "status": a["status"],
                          "records": len(a.get("record_ids") or []), "provider_receipt": dict(page.receipt or {})}
                         for a in applied]
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
        receipt_id = "ex-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO ex_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}

    def receipts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "ex_refresh_receipts"):
            return []
        return [{"receipt_id": r[0], **json.loads(r[1])} for r in self.conn.execute(
            "SELECT receipt_id, receipt_json FROM ex_refresh_receipts WHERE namespace=? ORDER BY started_at_ms, "
            "receipt_id", [namespace]).fetchall()]


__all__ = ["ExtractivesMonitor", "notifiable"]
