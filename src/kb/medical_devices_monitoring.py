"""Monitor new clearances, supplements, recalls and recall status changes through subscriptions (#2654, MD11).

A medical-devices monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`, capability
``platform.subscriptions``), following :mod:`src.kb.campaign_finance_monitoring`:
its query names a device (GUDID DI, EUDAMED Basic UDI-DI or a K or P number), a
manufacturer (EUDAMED SRN or an MD07 manufacturer subject) or a product code, and
reaches records exactly as :meth:`MedicalDevicesQueries.subject_records` does.
There is no monitor table and no scheduler: the ``clinical-evidence`` source-pack
schedule acquires, the maintenance orchestrator commits the watermarks, and each
evaluation turns differences into subscription events delivered through the
existing poll and outbox paths.

Notices are record changes, not assessments. Each cites the new (and the
previous) record revision and states what changed: ``clearance_published``,
``approval_published``, ``supplement_published``, ``recall_published``,
``recall_status_changed`` and ``recall_class_changed`` (as published, before and
after), ``certificate_published``, ``certificate_status_changed``,
``device_version_published``, ``classification_published``, ``registration_published``,
``supplement_listed`` and ``record_revised``.
Adverse-event reports and report counts are never notified: a monitor is not a
safety-signal detector.

A revision acquired *live* from a provider whose access is still
``unverified-live`` is withheld until a dated live run verifies it; fixture
replays are notified and marked as fixture evidence.
:meth:`MedicalDevicesMonitor.refresh` re-reads one declared source selection
through the real adapter within its page budget; re-reading unchanged responses
adds nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.medical_devices_sources import LIVE_VERIFICATION, PRODUCT_CODE
from src.kb.medical_devices_queries import MedicalDevicesQueries, resolve_subject
from src.kb.medical_devices_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    MedicalDevicesError,
    MedicalDevicesProjector,
    MedicalDevicesStore,
    authorize,
)

CONTRACT = "noesis-medical-device-notification-v1"
WATCH_KINDS = ("device", "manufacturer", "product-code")
NOTIFIED_KINDS = ("classification", "clearance", "approval", "supplement", "recall", "device-identifier",
                  "eudamed-device", "certificate", "actor")


def notifiable(row: Mapping[str, Any]) -> bool:
    provider = row.get("provider") or row["record"].get("provider")
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(provider, {}).get("status") == "verified-live")


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    fields, kind = row["record"]["fields"], row["record_kind"]
    if kind == "recall":
        return {"recall_number": fields.get("recall_number"),
                "status": fields.get("recall_status") or fields.get("status"),
                "recall_class": fields.get("recall_class"),
                "terminated": fields.get("event_date_terminated") or fields.get("termination_date")}
    if kind in {"clearance", "approval", "supplement"}:
        return {"number": fields.get("k_number") or fields.get("pma_number"),
                "supplement_number": fields.get("supplement_number"), "decision_code": fields.get("decision_code"),
                "decision_date": fields.get("decision_date"),
                "supplements_as_published": fields.get("supplements_as_published")}
    if kind == "certificate":
        return {"certificate_number": fields.get("certificate_number"), "status": fields.get("status"),
                "status_date": fields.get("status_date"), "revision": fields.get("revision")}
    if kind == "device-identifier":
        return {"primary_di": fields.get("primary_di"), "version": fields.get("public_version_number"),
                "commercial_distribution_status": fields.get("commercial_distribution_status")}
    return {"title": row["record"].get("title"), "status": fields.get("status")}


class MedicalDevicesMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = MedicalDevicesStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise MedicalDevicesError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "product-code" and not PRODUCT_CODE.fullmatch(key.upper()):
            raise MedicalDevicesError("invalid_watch", "a product code is three letters")
        if watch == "manufacturer" and not (resolve_subject(key)[1].startswith(
                ("medical-devices:eudamed:actor:", "medical-devices:fda:manufacturer:",
                 "medical-devices:gudid:labeler:"))):
            raise MedicalDevicesError("invalid_watch", "a manufacturer is watched by its SRN or subject key")
        query = {"operation": "search", "kind": "medical-devices-monitor", "watch": watch,
                 "key": key.upper() if watch == "product-code" else key}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "clinical", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "medical-devices-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the clinical-evidence source-pack schedule and the maintenance orchestrator "
                                      "commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "medical-devices-monitor":
            raise MedicalDevicesError("monitor_not_found", "subscription is not a medical-devices monitor")
        return subscription

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        scope = MedicalDevicesQueries(self.conn).subject_records(subscription["namespace"],
                                                                 subscription["query"]["key"], scopes=scopes)
        items, withheld = [], 0
        for row in scope["rows"]:
            if row["record_kind"] not in NOTIFIED_KINDS:
                continue  # adverse-event reports and counts are never notified
            if not notifiable(row):
                withheld += 1
                continue
            items.append({"id": f"{row['source_id']}:{row['record_key']}", "kind": row["record_kind"],
                          "record_key": row["record_key"], "source_id": row["source_id"],
                          "provider": row["provider"], "revision_id": row["revision_id"],
                          "revision_no": row["revision_no"], "evidence_origin": row["evidence_origin"],
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
                raise MedicalDevicesError("watermark_uncommitted", "no committed watermark yet; source-pack runs "
                                                                   "and the maintenance orchestrator commit them")
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
        if event_type == "removed" or new is None or (old and old["revision_id"] == new["revision_id"]):
            return []

        def note(kind: str, message: str, **detail: Any) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "record_key": new["record_key"], "message": message,
                    "cites": {"record_key": new["record_key"], "revision_id": new["revision_id"],
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_id": new["source_id"]},
                    "evidence_origin": new["evidence_origin"], **detail}

        summary, prior = new["summary"], (old or {}).get("summary") or {}
        label, kind = new["record_key"], new["kind"]
        if old is None:
            published = {"clearance": "clearance_published", "approval": "approval_published",
                         "supplement": "supplement_published", "recall": "recall_published",
                         "certificate": "certificate_published", "device-identifier": "device_version_published",
                         "classification": "classification_published"}
            return [note(published.get(kind, "registration_published"), f"{label}: {kind} on record "
                         f"({summary}).", after=summary)]
        notes = []
        if kind == "recall":
            if summary.get("status") != prior.get("status"):
                notes.append(note("recall_status_changed", f"{label}: status now published as "
                                                           f"{summary.get('status')!r} (was {prior.get('status')!r}).",
                                  before=prior.get("status"), after=summary.get("status")))
            if summary.get("recall_class") != prior.get("recall_class"):
                notes.append(note("recall_class_changed", f"{label}: class now published as "
                                                          f"{summary.get('recall_class')!r}.",
                                  before=prior.get("recall_class"), after=summary.get("recall_class")))
        elif kind == "approval" and summary.get("supplements_as_published") != prior.get("supplements_as_published"):
            added = sorted(set(summary.get("supplements_as_published") or []) -
                           set(prior.get("supplements_as_published") or []))
            notes.append(note("supplement_listed", f"{label}: supplements now listed: {added}.",
                              before=prior.get("supplements_as_published"),
                              after=summary.get("supplements_as_published")))
        elif kind == "certificate" and summary.get("status") != prior.get("status"):
            notes.append(note("certificate_status_changed", f"{label}: status now published as "
                                                            f"{summary.get('status')!r} (was {prior.get('status')!r}).",
                              before=prior.get("status"), after=summary.get("status")))
        elif kind == "device-identifier" and summary.get("version") != prior.get("version"):
            notes.append(note("device_version_published", f"{label}: GUDID version {summary.get('version')} "
                                                          f"published.", before=prior, after=summary))
        return notes or [note("record_revised", f"{label}: revision {new['revision_no']} recorded.", before=prior,
                              after=summary)]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, source: Mapping[str, Any], *, run_id: str, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, secret: str | None = None
                ) -> dict[str, Any]:
        """Re-read one declared selection within its budget; idempotent, and every unit leaves a receipt."""
        from src.ingestion.medical_devices_sources import MedicalDevicesAdapter

        scopes = set(scopes)
        namespace = MedicalDevicesProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = MedicalDevicesAdapter(source, transport=transport, secret=secret)
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
