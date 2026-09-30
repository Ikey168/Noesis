"""Monitor new cases, stage changes, decisions and aid awards through subscriptions (#2217, CS11).

A competition monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.courts_justice_monitoring`: its query names a **company** (an
ownership entity, optionally with group expansion as of the evaluation), a
**case** (record key), a **beneficiary** (an ownership entity reached through
accepted matches to award beneficiaries) or an **authority** (optionally one
instrument). There is no monitor table and no scheduler: the
``corporate-ownership`` source-pack schedule acquires, the maintenance
orchestrator commits watermarks, and each evaluation turns differences into
subscription events delivered through the existing poll and outbox paths.

Notifications cite the new and the previous revision: ``new_case``,
``case_revised``, ``stage_change``, ``new_decision_document``,
``new_aid_award`` and ``aid_award_changed`` (a correction or withdrawal).
Unchanged payloads emit nothing. A revision acquired *live* from a provider
whose access is still ``unverified-live`` is withheld (the latest notifiable
revision is used instead) until a dated live run verifies it; fixture replays
are notified and marked as fixture evidence. :meth:`CompetitionMonitor.refresh`
re-reads one declared source selection through the real adapter within its
budget; unchanged responses add nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.competition_sources import LIVE_VERIFICATION
from src.kb.competition import (
    READ_SCOPE,
    WRITE_SCOPE,
    CompetitionError,
    CompetitionProjector,
    CompetitionStore,
    authorize,
)

CONTRACT = "noesis-competition-notification-v1"
WATCH_KINDS = ("company", "case", "beneficiary", "authority")


def notifiable(source: Mapping[str, Any]) -> bool:
    origin = source.get("evidence_origin")
    return origin == "fixture" or LIVE_VERIFICATION.get(source.get("provider"), {}).get("status") == "verified-live"


class CompetitionMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = CompetitionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], ownership_namespace: str | None = None, group: bool = False,
               instrument: str | None = None, delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        from src.kb.competition_records import AUTHORITIES, INSTRUMENTS

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise CompetitionError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "case" and not key.startswith("competition:case:"):
            raise CompetitionError("invalid_watch", "a case is watched by its record key (competition:case:...)")
        if watch in {"company", "beneficiary"}:
            if not ownership_namespace:
                raise CompetitionError("invalid_watch", "companies and beneficiaries are ownership entities; name "
                                                        "the ownership namespace")
            authorize(ownership_namespace, scopes, READ_SCOPE)
        if watch == "authority" and (key not in AUTHORITIES or (instrument and instrument not in INSTRUMENTS)):
            raise CompetitionError("invalid_watch", f"an authority is one of {AUTHORITIES}, optionally with an "
                                                    f"instrument of {INSTRUMENTS}")
        query = {"operation": "search", "kind": "competition-monitor", "watch": watch, "key": key,
                 **({"ownership_namespace": ownership_namespace} if ownership_namespace else {}),
                 **({"group": True} if group else {}), **({"instrument": instrument} if instrument else {})}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "corporate-ownership", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "competition-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the corporate-ownership source-pack schedule and the maintenance orchestrator "
                                      "commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "competition-monitor":
            raise CompetitionError("monitor_not_found", "subscription is not a competition monitor")
        return subscription

    # ------------------------------------------------------------------ snapshots

    def _visible(self, namespace: str, key: str, withheld: list[int]) -> dict[str, Any] | None:
        history = self.store.history(namespace, key)
        visible = [v for v in history if notifiable(v["record"]["source"])]
        withheld[0] += len(history) - len(visible)
        return visible[-1] if visible else None

    @staticmethod
    def _cite(view: Mapping[str, Any]) -> dict[str, Any]:
        return {"record_key": view["record"]["record_key"], "revision_id": view["revision_id"],
                "revision": view["revision"], "provider": view["record"]["source"]["provider"],
                "evidence_origin": view["record"]["source"].get("evidence_origin")}

    def _case_items(self, namespace: str, case_keys: Iterable[str], items: list, withheld: list[int]) -> None:
        for key in sorted(set(case_keys)):
            view = self._visible(namespace, key, withheld)
            if view is None:
                continue
            body = view["record"]
            items.append({"id": f"case:{key}", "kind": "case", "cite": self._cite(view),
                          "summary": {"title": body.get("title"), "state_as_published": body.get("state_as_published"),
                                      "authority": body["authority"], "instrument": body["instrument"]}})
            children = self.store.case_children(namespace, key)
            for child in children["case_stage"] + children["decision_document"]:
                shown = self._visible(namespace, child["record"]["record_key"], withheld)
                if shown is None:
                    continue
                record = shown["record"]
                if record["kind"] == "case_stage":
                    items.append({"id": f"stage:{record['record_key']}", "kind": "stage", "cite": self._cite(shown),
                                  "summary": {"case_key": key, "stage_as_published": record["stage_as_published"],
                                              "stage_date": record.get("stage_date")}})
                else:
                    items.append({"id": f"document:{record['record_key']}", "kind": "document",
                                  "cite": self._cite(shown),
                                  "summary": {"case_key": key, "document_type": record["document_type_as_published"],
                                              "document_date": record.get("document_date"), "url": record["url"]}})

    def _award_items(self, namespace: str, award_keys: Iterable[str], items: list, withheld: list[int]) -> None:
        for key in sorted(set(award_keys)):
            view = self._visible(namespace, key, withheld)
            if view is None:
                continue
            body = view["record"]
            items.append({"id": f"award:{key}", "kind": "award", "cite": self._cite(view),
                          "summary": {"beneficiary_name_as_published": body["beneficiary_name_as_published"],
                                      "amount_as_published": body.get("amount_as_published"),
                                      "amount_range_as_published": body.get("amount_range_as_published"),
                                      "currency": body.get("currency"), "status": body["status"],
                                      "sa_number": body.get("sa_number"), "award_date": body.get("award_date")}})

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        from src.kb.competition_queries import awards_for_beneficiary, cases_for_company

        namespace, query = subscription["namespace"], subscription["query"]
        items: list[dict[str, Any]] = []
        withheld = [0]
        watch, key = query["watch"], query["key"]
        if watch == "case":
            self._case_items(namespace, [key], items, withheld)
        elif watch == "company":
            answer = cases_for_company(self.conn, namespace, key, ownership_namespace=query["ownership_namespace"],
                                       scopes=scopes, group=bool(query.get("group")))
            self._case_items(namespace, [r["case_key"] for r in answer["cases"]], items, withheld)
            self._award_items(namespace, [a["award_key"] for a in answer["awards_matched"]], items, withheld)
        elif watch == "beneficiary":
            answer = awards_for_beneficiary(self.conn, namespace, key, ownership_namespace=query["ownership_namespace"],
                                            scopes=scopes, include_unknowns=False)
            self._award_items(namespace, [a["award_key"] for a in answer["awards"]], items, withheld)
        else:
            cases = [v["record"]["record_key"] for v in self.store.views(namespace, ("competition_case",))
                     if v["record"]["authority"] == key
                     and (not query.get("instrument") or v["record"]["instrument"] == query["instrument"])]
            self._case_items(namespace, cases, items, withheld)
            if key == "ec" and query.get("instrument") in (None, "state_aid"):
                self._award_items(namespace, [v["record"]["record_key"]
                                              for v in self.store.views(namespace, ("state_aid_award",))],
                                  items, withheld)
        return {"items": items, "coverage": {"complete": True}}, withheld[0]

    # ------------------------------------------------------------------ evaluation

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise CompetitionError("watermark_uncommitted", "no committed watermark yet; source-pack runs and the "
                                                                "maintenance orchestrator commit them")
            watermark = int(row[0])
        result, withheld = self.snapshot(subscription, scopes)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, before_json, after_json FROM "
                                    "knowledge_subscription_events WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "notifications": notifications, "withheld_unverified_live_revisions": withheld,
                "delivery": subscription["delivery"]}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, before: str | None, after: str | None
                  ) -> list[dict[str, Any]]:
        old = json.loads(before) if before else None
        new = json.loads(after) if after else None
        if event_type == "removed" or new is None:
            return []

        def note(kind: str, message: str) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "message": message, "summary": new["summary"],
                    "previous_summary": (old or {}).get("summary"),
                    "cites": {**new["cite"], "previous": (old or {}).get("cite")},
                    "evidence_origin": new["cite"].get("evidence_origin")}

        kind, summary = new["kind"], new["summary"]
        if kind == "case":
            if old is None:
                return [note("new_case", f"{summary['authority']} {summary['instrument']} case: {summary['title']}")]
            if old["cite"]["revision_id"] != new["cite"]["revision_id"]:
                return [note("case_revised", f"{new['cite']['record_key']}: revision {new['cite']['revision']} "
                                             f"(state as published: {summary['state_as_published']})")]
        if kind == "stage" and old is None:
            return [note("stage_change", f"{summary['case_key']}: {summary['stage_as_published']} "
                                         f"({summary['stage_date'] or 'date not published'})")]
        if kind == "document" and old is None:
            return [note("new_decision_document", f"{summary['case_key']}: {summary['document_type']} "
                                                  f"{summary['document_date'] or ''}".strip())]
        if kind == "award":
            if old is None:
                return [note("new_aid_award", f"{summary['beneficiary_name_as_published']}: "
                                              f"{summary['amount_as_published'] or summary['amount_range_as_published']}"
                                              f" {summary['currency'] or ''} ({summary['sa_number']})")]
            if old["cite"]["revision_id"] != new["cite"]["revision_id"]:
                return [note("aid_award_changed", f"{new['cite']['record_key']}: {old['summary']['status']} "
                                                  f"{old['summary']['amount_as_published']} -> {summary['status']} "
                                                  f"{summary['amount_as_published']}")]
        return []

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, source: Mapping[str, Any], *, run_id: str, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Re-read one declared selection within its budget; idempotent, and every unit leaves a receipt."""
        from src.ingestion.competition_sources import CompetitionAdapter

        scopes = set(scopes)
        namespace = CompetitionProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = CompetitionAdapter(source, transport=transport)
        projector = CompetitionProjector(self.conn)
        projector.store.now = self.now
        projector.store.store.now = self.now
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = projector.project(namespace, [r["competition_record"] for r in page.records], run_id=run_id,
                                       receipt=dict(page.receipt or {}), observed_at_ms=self.now(),
                                       principal_id=principal_id)
            for change, count in result["counts"].items():
                counts[change] = counts.get(change, 0) + count
            units += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        return {"run_id": run_id, "source_id": source["source_id"], "units": units, "counts": counts,
                "complete": cursor is None, "receipts": self.store.receipts(namespace, run_id, scopes=scopes)}
