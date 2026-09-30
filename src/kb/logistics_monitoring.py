"""Monitor new logistics releases, revisions, series breaks and port-code changes through subscriptions (#2229,
SL10 #2547).

A logistics monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`) whose
query names ports (UN/LOCODEs or source codes ``scheme:code``), countries and/or series ids, following
:mod:`src.kb.trade_monitoring`: no watcher table and no scheduler. Each run evaluates a committed watermark against a
snapshot of the watched records, and the subscription store turns new items into events; a replay, an unchanged
release or a restart emits nothing (an unchanged release adds no vintage in the first place).

Events, each carrying record ids and the citation of the source release:

* ``new_vintage`` - the first vintage of a watched series, or a later one that only adds periods;
* ``revised_value`` - a later vintage that changes published periods (before and after, as published);
* ``series_break`` - a break recorded from a published methodology note;
* ``port_code_change`` - a UN/LOCODE revision after the first of a watched port (changed, marked for removal or
  removed) with the re-matches it triggered.

Notices report published record changes; they contain no forecast or trend verdict. :meth:`LogisticsMonitor.refresh`
acquires a source's declared documents within its page budget (the source-pack schedule runs the same path) and
stops at the first rate-limit answer. Live releases from providers still ``unverified-live`` are withheld.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.logistics_sources import unverified
from src.kb.logistics_ports import LogisticsPorts
from src.kb.logistics_records import (
    NOTIFICATION_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    LogisticsError,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    table_exists,
)
from src.kb.logistics_series import LogisticsProjector, LogisticsStore

FILTER_KEYS = ("ports", "countries", "series")
EVENT_TYPES = ("new_vintage", "revised_value", "series_break", "port_code_change")
MESSAGES = {
    "new_vintage": "A new release of a watched logistics series was published",
    "revised_value": "A release revised published values of a watched logistics series",
    "series_break": "A series break was recorded for a watched logistics series",
    "port_code_change": "A UN/LOCODE release changed or removed a watched port code",
}
_DDL = """
CREATE TABLE IF NOT EXISTS logistics_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(citation: Mapping[str, Any]) -> bool:
    return citation["evidence_origin"] != "live" or not unverified(citation["provider"])


class LogisticsMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = LogisticsStore(conn, initialize=initialize, now=now)
        self.ports = LogisticsPorts(conn, initialize=initialize, now=self.store.now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(watch: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(watch or {})
        if set(raw) - set(FILTER_KEYS) or not any(raw.get(k) for k in FILTER_KEYS):
            raise LogisticsError("invalid_watch", f"a logistics monitor names ports, countries or series; keys are "
                                                  f"{FILTER_KEYS}")
        return {k: sorted({str(v).strip() for v in raw.get(k) or []}) for k in FILTER_KEYS}

    def create(self, namespace: str, request_key: str, *, watch: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "economic",
             "query": {"operation": "search", "kind": "logistics-monitor", "filter": self._filter(watch)},
             "filters": {"watch": "logistics"}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "logistics-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the economic-shipping-and-logistics source-pack schedule (or refresh()) "
                "acquires releases within each source's budget and commits the watermarks; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "logistics-monitor":
            raise LogisticsError("monitor_not_found", "subscription is not a logistics monitor")
        return subscription

    def _watched(self, namespace: str, wanted: Mapping[str, Any]) -> tuple[list[dict], list[str]]:
        series: dict[str, dict[str, Any]] = {}
        unlocodes: set[str] = set()
        for port in wanted["ports"]:
            if ":" in port:
                scheme, code = port.split(":", 1)
                codes = [(scheme, code)]
                resolved = self.ports.resolve_source(namespace, scheme, code)
                if resolved["unlocode"]:
                    unlocodes.add(resolved["unlocode"])
            else:
                code = port.replace(" ", "").upper()
                unlocodes.add(code)
                codes = [("unlocode", code)] + [(s["scheme"], s["code"]) for s in self.ports.source_codes(namespace,
                                                                                                         code)]
            for s in self.store.find_series(namespace, codes=codes):
                series[s["series_id"]] = s
            for s in self.store.find_series(namespace, geo_kind="route", partner_codes=codes):
                series[s["series_id"]] = s
        for country in wanted["countries"]:
            scheme, code = country.split(":", 1) if ":" in country else (None, country)
            for s in self.store.find_series(namespace, geo_kind="country", codes=[(scheme, code)]):
                series[s["series_id"]] = s
            if len(code) == 2:
                unlocodes |= {p["unlocode"] for p in self.ports.ports(namespace, country=code.upper(),
                                                                       include_removed=True)}
        for series_id in wanted["series"]:
            try:
                series[series_id] = self.store.series(namespace, series_id)
            except LogisticsError:
                continue
        return [series[k] for k in sorted(series)], sorted(unlocodes)

    def _series_items(self, namespace: str, series: Mapping[str, Any]) -> list[dict[str, Any]]:
        items, before = [], None
        head = {"provider": series["provider"], "concept": series["concept"],
                "source_series_id": series["source_series_id"], "geography": series["geography"],
                "partner": series["partner"]}
        for vintage in self.store.vintage_rows(namespace, series["series_id"]):
            values = {v["period"]: {"value_text": v["value_text"], "status": v["status"]}
                      for v in self.store.values(namespace, vintage["vintage_id"])}
            changes = [] if before is None else [
                {"period": p, "before": before[p], "after": values.get(p)}
                for p in sorted(before) if values.get(p) != before[p]]
            items.append({"id": f"vintage:{vintage['vintage_id']}",
                          "item": "revised_value" if changes else "new_vintage",
                          "series_id": series["series_id"], "series": head, "vintage_id": vintage["vintage_id"],
                          "previous_vintage_id": vintage["revision_of"], "changed_values": changes,
                          "added_periods": sorted(set(values) - set(before or {})) if before is not None else [],
                          "citation": self.store.source_revision(namespace, vintage["release_id"])})
            before = values
        for brk in self.store.breaks(namespace, series["series_id"]):
            items.append({"id": f"break:{brk['break_id']}", "item": "series_break", "series_id": series["series_id"],
                          "series": head, "break_id": brk["break_id"], "period": brk["period"], "note": brk["note"],
                          "citation": self.store.source_revision(namespace, brk["release_id"])})
        return items

    def _port_items(self, namespace: str, unlocode: str) -> list[dict[str, Any]]:
        history = self.ports.port_history(namespace, unlocode)
        items = []
        for previous, revision in zip(history, history[1:]):
            rematches = [r for m in self.ports.matches(namespace, unlocode=unlocode) for r in m["rematches"]
                         if r["to_revision"] == revision["revision"]]
            items.append({"id": f"port:{unlocode}:{revision['revision']}", "item": "port_code_change",
                          "unlocode": unlocode, "revision": revision["revision"],
                          "previous_revision": previous["revision"], "state": revision["state"],
                          "change_indicator": revision["change_indicator"],
                          "release_version": revision["release_version"], "rematches": rematches,
                          "citation": self.store.source_revision(namespace, revision["release_id"])})
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        series, unlocodes = self._watched(namespace, wanted)
        items = [i for s in series for i in self._series_items(namespace, s)]
        items += [i for code in unlocodes for i in self._port_items(namespace, code)]
        kept = [i for i in items if notifiable(i["citation"])]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        latest = self.store.latest_release_ms(namespace)
        if latest is None:
            raise LogisticsError("not_ready", "no logistics release yet; acquire first")
        state = [r[0] for r in self.conn.execute(
            "SELECT vintage_id FROM logistics_vintages WHERE namespace=? UNION ALL SELECT break_id FROM "
            "logistics_breaks WHERE namespace=? UNION ALL SELECT unlocode || ':' || revision FROM logistics_ports "
            "WHERE namespace=? ORDER BY 1", [namespace, namespace, namespace]).fetchall()]
        generation = digest(state)[:24]
        rows = self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("logistics_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"logistics_generation": generation, "observed_at": iso_from_ms(latest)}

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
                "note": "notices report published record changes; no forecast or trend verdict"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        citation = item["citation"]
        body = {k: v for k, v in item.items() if k not in {"id", "item", "citation"}}
        return [{"contract": NOTIFICATION_CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['item']}",
                 "object": key, "kind": item["item"],
                 "message": f"{MESSAGES[item['item']]} ({citation['provider']}, "
                            f"{citation['release_version'] or citation['published_on'] or citation['release_at']}).",
                 "citation": citation, **body,
                 "note": "a publication reported as published; no forecast or trend verdict"}]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None,
                max_documents: int | None = None) -> dict[str, Any]:
        """Acquire a source's declared documents within its page budget; one receipt per run, stopped at the first
        rate-limit answer and refused before the provider's Retry-After has passed."""
        from src.ingestion.logistics_sources import LogisticsAdapter
        from src.ingestion.source_packs import SourcePackError

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT retry_at_ms FROM logistics_refresh_receipts WHERE namespace=? AND source_id=? "
            "ORDER BY started_at_ms DESC LIMIT 1", [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id)
        adapter = LogisticsAdapter(source, transport=transport)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents), int(source["budgets"]["max_pages"]))
        projector = LogisticsProjector(self.conn)
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
            releases += [{"release_id": a["release_id"], "status": a["status"], "vintages": a["vintages"]}
                         for a in applied]
            cursor = page.next_cursor
            if cursor is None:
                break
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, releases, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, releases, stopped, retry_at, principal_id):
        body = {"source_id": source["source_id"], "status": status, "releases": releases,
                "new_releases": sum(1 for r in releases if r["status"] == "applied"),
                "unchanged_releases": sum(1 for r in releases if r["status"] == "unchanged"), "stopped": stopped,
                "retry_at": iso_from_ms(retry_at), "requested_by": principal_id, "at": iso_from_ms(now)}
        receipt_id = "logistics-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO logistics_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["EVENT_TYPES", "LogisticsMonitor", "notifiable"]
