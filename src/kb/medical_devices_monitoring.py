"""Monitor devices, manufacturers and product codes through subscriptions (#2654, MD11).

A medical-device monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.campaign_finance_monitoring`: its query names a device (K or P
number, DI, Basic UDI-DI or record key), a manufacturer (a manufacturer key
from the identity subjects) or an FDA product code. There is no monitor table
and no scheduler: the ``clinical-evidence`` source-pack schedule acquires, the
maintenance orchestrator commits the watermarks, and each evaluation turns
record differences into subscription events delivered through the existing
poll and outbox paths.

Notices are record changes, not assessments. Each cites the new (and the
previous) record revision and states what changed: ``clearance_published``,
``approval_published``, ``supplement_published``, ``recall_published``,
``recall_status_changed`` / ``recall_class_changed`` (before and after, as
published), ``certificate_published``, ``certificate_status_changed``,
``record_removed_by_source`` and ``record_revised``. A revision acquired
*live* from a provider whose access is still ``unverified-live`` is withheld
until a dated live run verifies it; fixture replays are notified and marked
as fixture evidence. :meth:`MedicalDeviceMonitor.refresh` re-reads one declared
source selection through the real adapter within its page budget; re-reading
unchanged responses adds nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.medical_devices_sources import LIVE_VERIFICATION
from src.kb.medical_devices_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    MedicalDeviceError,
    MedicalDeviceProjector,
    MedicalDeviceStore,
    authorize,
)

CONTRACT = "noesis-medical-device-notification-v1"
WATCH_KINDS = ("device", "manufacturer", "product-code")
WATCHED_KINDS = ("clearance", "approval", "approval-supplement", "recall", "eudamed-certificate")


def notifiable(row: Mapping[str, Any]) -> bool:
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(row["record"]["provider"], {}).get("status") == "verified-live")


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row["record"]["fields"]
    summary = {"publication_state": row["publication_state"]}
    for field in ("k_number", "pma_number", "supplement_number", "decision_code", "decision_date", "recall_number",
                  "recall_class", "status_as_published", "event_date_initiated", "event_date_terminated",
                  "certificate_number", "notified_body_number", "issue_date", "expiry_date", "product_code",
                  "supplement_type"):
        if field in fields:
            summary[field] = fields[field]
    return summary


class MedicalDeviceMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = MedicalDeviceStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        from src.kb.medical_devices_queries import resolve_subject

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise MedicalDeviceError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "product-code" and resolve_subject(key)["kind"] != "product-code":
            raise MedicalDeviceError("invalid_watch", "a product code is three capital letters")
        if watch == "device":
            resolve_subject(key)  # a published identifier form, never a name
        if watch == "manufacturer" and not key.startswith(("medical-devices:manufacturer:",
                                                            "medical-devices:eudamed:actor:")):
            raise MedicalDeviceError("invalid_watch", "a manufacturer is watched by its identity subject key")
        query = {"operation": "search", "kind": "medical-device-monitor", "watch": watch, "key": key}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "clinical", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "medical-device-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the clinical-evidence source-pack schedule and the maintenance orchestrator "
                                      "commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "medical-device-monitor":
            raise MedicalDeviceError("monitor_not_found", "subscription is not a medical-device monitor")
        return subscription

    def _rows(self, subscription: Mapping[str, Any], scopes: set[str]) -> list[dict[str, Any]]:
        from src.kb.medical_devices_identity import MedicalDeviceIdentity
        from src.kb.medical_devices_queries import MedicalDeviceQueries

        namespace, query = subscription["namespace"], subscription["query"]
        if query["watch"] == "manufacturer":
            identity = MedicalDeviceIdentity(self.conn, initialize=False)
            subject = next((m for m in identity.manufacturers(namespace, scopes=scopes) if m["key"] == query["key"]),
                           None)
            keys = sorted({r["record_key"] for r in (subject or {}).get("records") or []})
        else:
            reached = MedicalDeviceQueries(self.conn).reach(namespace, query["key"], scopes=scopes)
            keys = [r["record_key"] for r in reached["records"]]
        return self.store.records(namespace, scopes=scopes, kinds=WATCHED_KINDS, record_keys=keys) if keys else []

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        items, withheld = [], 0
        for row in self._rows(subscription, scopes):
            if not notifiable(row):
                withheld += 1
                continue
            items.append({"id": row["record_key"], "kind": row["record_kind"], "record_key": row["record_key"],
                          "provider": row["provider"], "jurisdiction": row["jurisdiction"],
                          "revision_id": row["revision_id"], "revision_no": row["revision_no"],
                          "source_id": row["source_id"], "evidence_origin": row["evidence_origin"],
                          "summary": _summary(row)})
        return {"items": items, "coverage": {"complete": True}}, withheld

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
                raise MedicalDeviceError("watermark_uncommitted", "no committed watermark yet; source-pack runs and "
                                                                  "the maintenance orchestrator commit them")
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
                "delivery": subscription["delivery"],
                "notice": "notices are record changes as published, not assessments"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, before: str | None, after: str | None
                  ) -> list[dict[str, Any]]:
        old = json.loads(before) if before else None
        new = json.loads(after) if after else None
        if event_type == "removed" or new is None or (old and old["revision_id"] == new["revision_id"]):
            return []

        def note(kind: str, message: str, **detail: Any) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "record_key": new["record_key"], "message": message,
                    "cites": {"record_key": new["record_key"], "revision_id": new["revision_id"],
                              "revision_no": new["revision_no"],
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_id": new["source_id"], "provider": new["provider"]},
                    "evidence_origin": new["evidence_origin"], **detail}

        summary, prior = new["summary"], (old or {}).get("summary") or {}
        label = new["record_key"]
        if summary.get("publication_state") != "published":
            return [note("record_removed_by_source", f"{label}: the publisher no longer answers this record; earlier "
                                                     "revisions stay on record.")]
        if old is None:
            kind = {"clearance": "clearance_published", "approval": "approval_published",
                    "approval-supplement": "supplement_published", "recall": "recall_published",
                    "eudamed-certificate": "certificate_published"}[new["kind"]]
            date = summary.get("decision_date") or summary.get("event_date_initiated") or summary.get("issue_date")
            return [note(kind, f"{label}: published ({date or 'date not published'}).", after=summary)]
        notes = []
        if new["kind"] == "recall":
            if summary.get("status_as_published") != prior.get("status_as_published"):
                notes.append(note("recall_status_changed", f"{label}: status now {summary.get('status_as_published')!r}"
                                                           f" (was {prior.get('status_as_published')!r}), as "
                                                           "published.",
                                  before=prior.get("status_as_published"), after=summary.get("status_as_published")))
            if summary.get("recall_class") != prior.get("recall_class"):
                notes.append(note("recall_class_changed", f"{label}: class now {summary.get('recall_class')!r} (was "
                                                          f"{prior.get('recall_class')!r}), as published.",
                                  before=prior.get("recall_class"), after=summary.get("recall_class")))
        if new["kind"] == "eudamed-certificate" and summary.get("status_as_published") != prior.get(
                "status_as_published"):
            notes.append(note("certificate_status_changed", f"{label}: status now "
                                                            f"{summary.get('status_as_published')!r} (was "
                                                            f"{prior.get('status_as_published')!r}), as published.",
                              before=prior.get("status_as_published"), after=summary.get("status_as_published")))
        changed = sorted(k for k in set(summary) | set(prior) if summary.get(k) != prior.get(k))
        return notes or [note("record_revised", f"{label}: revision {new['revision_no']} recorded.",
                              changed_fields=changed, before={k: prior.get(k) for k in changed},
                              after={k: summary.get(k) for k in changed})]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, source: Mapping[str, Any], *, run_id: str, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Re-read one declared selection within its budget; idempotent, and every unit leaves a receipt."""
        from src.ingestion.medical_devices_sources import MedicalDevicesAdapter

        scopes = set(scopes)
        namespace = MedicalDeviceProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = MedicalDevicesAdapter(source, transport=transport)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = self.store.project(namespace, [r["medical_device_record"] for r in page.records],
                                        run_id=run_id, source_id=source["source_id"], receipt=dict(page.receipt or {}))
            for change, count in result["counts"].items():
                counts[change] = counts.get(change, 0) + count
            units += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        del principal_id
        return {"run_id": run_id, "source_id": source["source_id"], "units": units, "counts": counts,
                "complete": cursor is None,
                "receipts": self.store.receipts(namespace, run_id, scopes=scopes | {READ_SCOPE})}


__all__ = ["CONTRACT", "WATCH_KINDS", "MedicalDeviceMonitor"]
