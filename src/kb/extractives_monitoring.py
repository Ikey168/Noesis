"""Monitor new EITI reports, revisions and new commodity releases through subscriptions (#2653, EX10).

An extractives monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) whose query names companies (EITI company record keys, or ownership record keys and
entity ids reached through accepted company matches), countries (ISO alpha-2) and/or commodities (source commodity
codes); no watcher table and no scheduler, following :mod:`src.kb.campaign_finance_monitoring` and
:mod:`src.kb.logistics_monitoring`. Each run evaluates a committed watermark against a snapshot of the watched
records and the subscription store turns new items into events; a replay, an unchanged release or a restart emits
nothing (an unchanged release adds no revision or vintage in the first place).

Notices, each citing the new or revised record revision and its source release and stating what changed:

* ``new_report`` / ``report_revised`` - the first or a later version of a watched country's EITI report, with the
  record revisions the version added, changed or removed;
* ``new_payment`` / ``payment_revised`` / ``payment_removed`` - a payment revision of a watched company, with the
  amounts before and after as reported;
* ``new_commodity_release`` / ``revised_values`` - a vintage of a watched commodity's series (new periods only, or
  changed published values with before and after).

Notices are record changes, never assessments. :meth:`ExtractivesMonitor.refresh` re-reads a source's declared
documents within its page budget, idempotently, with a receipt per run; live releases from providers still
``unverified-live`` are withheld.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.extractives_sources import unverified
from src.kb.extractives_records import (
    NOTIFICATION_CONTRACT,
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

FILTER_KEYS = ("companies", "countries", "commodities")
EVENT_TYPES = ("new_report", "report_revised", "new_payment", "payment_revised", "payment_removed",
               "new_commodity_release", "revised_values")
MESSAGES = {
    "new_report": "A new EITI report version was published for a watched country",
    "report_revised": "A revised EITI report version changed records of a watched country",
    "new_payment": "A new EITI report version states a payment of a watched company",
    "payment_revised": "A revised EITI report version changed a payment of a watched company",
    "payment_removed": "A revised EITI report version no longer states a payment of a watched company",
    "new_commodity_release": "A new release of a watched commodity series was published",
    "revised_values": "A release revised published values of a watched commodity series",
}
_AMOUNTS = ("government_reported", "company_reported", "discrepancy_as_published")
_DDL = """
CREATE TABLE IF NOT EXISTS extractives_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def notifiable(citation: Mapping[str, Any]) -> bool:
    return citation["evidence_origin"] != "live" or not unverified(citation["provider"])


class ExtractivesMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.extractives_identity import ExtractivesIdentity
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = ExtractivesStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.identity = ExtractivesIdentity(conn, initialize=initialize, now=self.now)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _filter(watch: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(watch or {})
        if set(raw) - set(FILTER_KEYS) or not any(raw.get(k) for k in FILTER_KEYS):
            raise ExtractivesError("invalid_watch", f"an extractives monitor names companies, countries or "
                                                    f"commodities; keys are {FILTER_KEYS}")
        out = {k: sorted({str(v).strip() for v in raw.get(k) or []}) for k in FILTER_KEYS}
        out["countries"] = sorted({c.upper() for c in out["countries"]})
        return out

    def create(self, namespace: str, request_key: str, *, watch: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "economic",
             "query": {"operation": "search", "kind": "extractives-monitor", "filter": self._filter(watch)},
             "filters": {"watch": "extractives"}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "extractives-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the economic-extractives source-pack schedule (or refresh()) acquires releases "
                "within each source's budget and commits the watermarks; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "extractives-monitor":
            raise ExtractivesError("monitor_not_found", "subscription is not an extractives monitor")
        return subscription

    # -------------------------------------------------------------- snapshot

    def _company_keys(self, namespace: str, wanted: Iterable[str], scopes: set[str]) -> set[str]:
        keys = {w for w in wanted if w.startswith("extractives:")}
        others = {w for w in wanted if not w.startswith("extractives:")}
        if others:
            keys |= {link["subject_key"] for link in
                     self.identity.accepted_company_links(namespace, others, scopes=scopes)}
        return keys

    @staticmethod
    def _amounts(body: Mapping[str, Any] | None) -> dict[str, Any] | None:
        return None if body is None else {k: body.get(k) for k in _AMOUNTS}

    def _report_items(self, namespace: str, report_key: str) -> list[dict[str, Any]]:
        items = []
        for number, release in enumerate(self.store.releases(namespace, report_key=report_key)):
            changes = []
            rows = self.conn.execute(
                "SELECT record_key FROM extractives_records WHERE namespace=? AND release_id=? ORDER BY record_key",
                [namespace, release["release_id"]]).fetchall()
            for (key,) in rows:
                view = next(v for v in self.store.history(namespace, key) if v["release_id"] == release["release_id"])
                changes.append({"record_key": key, "record_type": view["record_type"], "revision_id": view["revision_id"],
                                "revision": view["revision"], "state": view["state"],
                                "change": "removed" if view["state"] == "removed" else
                                "added" if view["revision"] == 1 else "revised"})
            items.append({"id": f"report:{release['release_id']}", "item": "new_report" if number == 0
                          else "report_revised", "report_key": report_key,
                          "report_version": release["release_version"], "release_id": release["release_id"],
                          "changes": changes,
                          "summary": {c: sum(1 for x in changes if x["change"] == c)
                                      for c in ("added", "revised", "removed")},
                          "citation": self.store.source_revision(namespace, release["release_id"])})
        return items

    def _payment_items(self, namespace: str, company_keys: set[str]) -> list[dict[str, Any]]:
        items = []
        for view in self.store.records(namespace, record_types=("company_payment",), include_removed=True):
            if view["record"]["company_key"] not in company_keys:
                continue
            before = None
            for revision in self.store.history(namespace, view["record_key"]):
                after = self._amounts(revision["record"])
                kind = "payment_removed" if revision["state"] == "removed" else \
                    "new_payment" if before is None else "payment_revised"
                items.append({"id": f"payment:{revision['revision_id']}", "item": kind,
                              "record_key": view["record_key"], "company_key": view["record"]["company_key"],
                              "revision_id": revision["revision_id"], "previous_revision_id": revision["revision_of"],
                              "report_version": revision["report_version"],
                              "before": before, "after": None if revision["state"] == "removed" else after,
                              "citation": self.store.source_revision(namespace, revision["release_id"])})
                before = after
        return items

    def _series_items(self, namespace: str, series: Mapping[str, Any]) -> list[dict[str, Any]]:
        items, before = [], None
        head = {"provider": series["provider"], "commodity": series["commodity"]["code"],
                "statistic": series["statistic"], "unit": series["unit"], "country": series["country"]}
        for vintage in self.store.vintage_rows(namespace, series["series_id"]):
            values = {v["period"]: {"value_text": v["value_text"], "status": v["status"], "estimated": v["estimated"],
                                    "revised": v["revised"]}
                      for v in self.store.values(namespace, vintage["vintage_id"])}
            changes = [] if before is None else [{"period": p, "before": before[p], "after": values[p]}
                                                 for p in sorted(before) if p in values and values[p] != before[p]]
            items.append({"id": f"vintage:{vintage['vintage_id']}",
                          "item": "revised_values" if changes else "new_commodity_release",
                          "series_id": series["series_id"], "series": head, "vintage_id": vintage["vintage_id"],
                          "previous_vintage_id": vintage["revision_of"], "changed_values": changes,
                          "added_periods": sorted(set(values) - set(before or {})),
                          "periods_not_restated": sorted(set(before or {}) - set(values)),
                          "citation": self.store.source_revision(namespace, vintage["release_id"])})
            before = values
        return items

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        items = []
        for country in wanted["countries"]:
            for report_key in self.store.report_keys(namespace, country=country):
                items += self._report_items(namespace, report_key)
        if wanted["companies"]:
            items += self._payment_items(namespace, self._company_keys(namespace, wanted["companies"], scopes))
        series = {}
        for commodity in wanted["commodities"]:
            for s in self.store.find_series(namespace, commodity=commodity):
                if not wanted["countries"] or s["country"].get("iso2") in wanted["countries"]:
                    series[s["series_id"]] = s
        for series_id in sorted(series):
            items += self._series_items(namespace, series[series_id])
        kept = [i for i in items if notifiable(i["citation"])]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        latest = self.store.latest_release_ms(namespace)
        if latest is None:
            raise ExtractivesError("not_ready", "no extractives release yet; acquire first")
        state = [r[0] for r in self.conn.execute(
            "SELECT revision_id FROM extractives_records WHERE namespace=? UNION ALL SELECT vintage_id FROM "
            "extractives_vintages WHERE namespace=? ORDER BY 1", [namespace, namespace]).fetchall()]
        generation = digest(state)[:24]
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
        result, withheld = self.snapshot(subscription, scopes)
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
                "note": "notices report published record changes; no assessment, score or forecast"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        citation = item["citation"]
        body = {k: v for k, v in item.items() if k not in {"id", "item", "citation"}}
        return [{"contract": NOTIFICATION_CONTRACT, "event_id": event_id,
                 "notification_id": f"{event_id}:{item['item']}", "object": key, "kind": item["item"],
                 "message": f"{MESSAGES[item['item']]} ({citation['provider']}, version "
                            f"{citation['release_version']}, published {citation['published_on']}).",
                 "citation": citation, **body,
                 "note": "a publication reported as published; a record change, not an assessment"}]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None,
                max_documents: int | None = None) -> dict[str, Any]:
        """Acquire a source's declared documents within its page budget; one receipt per run, stopped at the first
        rate-limit answer and refused before the provider's Retry-After has passed. Idempotent: an unchanged
        release adds nothing."""
        from src.ingestion.extractives_sources import ExtractivesAdapter
        from src.ingestion.source_packs import SourcePackError

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT retry_at_ms FROM extractives_refresh_receipts WHERE namespace=? AND source_id=? "
            "ORDER BY started_at_ms DESC LIMIT 1", [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id)
        adapter = ExtractivesAdapter(source, transport=transport)
        documents = len(adapter.declared["documents"])
        limit = min(documents, int(max_documents or documents), int(source["budgets"]["max_pages"]))
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
                    retry_at = now + int(exc.details.get("retry_after_ms") or 60_000)
                break
            applied = projector.project_page(run_id=f"refresh:{source['source_id']}:{now}", manifest=None,
                                             source=source, records=page.records, documents=None,
                                             page_receipt=page.receipt, principal_id=principal_id)
            releases += [{"release_id": a["release_id"], "status": a["status"], "vintages": a["vintages"],
                          "revisions": a["revisions"]} for a in applied]
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
        receipt_id = "extractives-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO extractives_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["EVENT_TYPES", "ExtractivesMonitor", "notifiable"]
