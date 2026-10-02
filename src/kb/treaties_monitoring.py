"""Monitor new treaty actions, depositary corrections and entry into force through subscriptions (#2581, TR10).

A treaties monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`, capability
``platform.subscriptions``), following
:mod:`src.kb.campaign_finance_monitoring`: its query names a treaty (a key or
a published identifier) or a participant (a key, an exact published name or a
geospatial place reached through accepted TR06 matches). There is no monitor
table and no scheduler: the ``legal-research`` source-pack schedule acquires,
the maintenance orchestrator commits the watermarks, and each evaluation turns
differences into subscription events delivered through the existing poll and
outbox paths.

Notices are record changes, not assessments. Each cites the new (and the
previous) record revision and the depositary's revision stamp, and states what
changed: ``action_recorded``, ``statement_recorded`` (a reservation,
declaration, objection or note, verbatim), ``depositary_correction`` (the
fields before and after), ``entry_into_force_recorded`` (a treaty or a
participant's entry-into-force date now published), ``removed_by_source`` (a
row the depositary no longer shows) and ``treaty_recorded``.

A revision acquired *live* from a provider whose access is still
``unverified-live`` is withheld until a dated live run verifies it; fixture
replays are notified and marked as fixture evidence.
:meth:`TreatiesMonitor.refresh` re-reads one declared source selection through
the real adapter within its page budget; re-reading unchanged pages adds
nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.treaties_sources import LIVE_VERIFICATION
from src.kb.treaties_queries import TreatiesQueries
from src.kb.treaties_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    TreatiesError,
    TreatiesStore,
    authorize,
)

CONTRACT = "noesis-treaty-notification-v1"
WATCH_KINDS = ("treaty", "participant")
_SUMMARY_FIELDS = ("action_type", "action_type_as_published", "action_date", "deposit_date", "effective_date",
                   "date_text_as_published", "statement_kind", "text_verbatim", "objects_to_statement_key",
                   "made_on", "entry_into_force", "status_as_published", "name_as_published")


def notifiable(row: Mapping[str, Any]) -> bool:
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(row["provider"], {}).get("status") == "verified-live")


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row["record"]["fields"]
    return {k: fields[k] for k in _SUMMARY_FIELDS if k in fields}


class TreatiesMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = TreatiesStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise TreatiesError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "treaty" and ":participant:" in key:
            raise TreatiesError("invalid_watch", "a treaty is watched by its key or a published identifier")
        if watch == "participant" and key.startswith("treaties:") and ":participant:" not in key:
            raise TreatiesError("invalid_watch", "a participant is watched by its key, published name or place")
        query = {"operation": "search", "kind": "treaties-monitor", "watch": watch, "key": key}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "legal", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "treaties-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the legal-research source-pack schedule and the maintenance orchestrator "
                                      "commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "treaties-monitor":
            raise TreatiesError("monitor_not_found", "subscription is not a treaties monitor")
        return subscription

    def _rows(self, subscription: Mapping[str, Any], scopes: set[str]) -> list[dict[str, Any]]:
        namespace, query = subscription["namespace"], subscription["query"]
        ask = TreatiesQueries(self.conn)
        kinds = ["treaty", "treaty-action", "treaty-statement"]
        rows = []
        if query["watch"] == "treaty":
            for key in ask.resolve_treaty(namespace, query["key"], scopes=scopes):
                rows += self.store.records(namespace, scopes=scopes, kinds=kinds, treaty_key=key,
                                           include_removed=True)
        else:
            for item in ask.resolve_participant(namespace, query["key"], scopes=scopes):
                rows += self.store.records(namespace, scopes=scopes, kinds=kinds[1:],
                                           participant_key=item["participant_key"], include_removed=True)
        return rows

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        items, withheld = [], 0
        for row in self._rows(subscription, scopes):
            if not notifiable(row):
                withheld += 1
                continue
            items.append({
                "id": f"{row['source_id']}:{row['record_key']}", "kind": row["record_kind"],
                "record_key": row["record_key"], "treaty_key": row["treaty_key"], "source_id": row["source_id"],
                "provider": row["provider"], "revision_id": row["revision_id"], "revision_no": row["revision_no"],
                "depositary_revision": row["native_revision"], "publication_status": row["publication_status"],
                "evidence_origin": row["evidence_origin"], "summary": _summary(row),
            })
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
                raise TreatiesError("watermark_uncommitted", "no committed watermark yet; source-pack runs and the "
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
        if event_type == "removed" or new is None or (old and old["revision_id"] == new["revision_id"]):
            return []

        def note(kind: str, message: str, **detail: Any) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "record_key": new["record_key"], "treaty_key": new["treaty_key"],
                    "message": message,
                    "cites": {"record_key": new["record_key"], "revision_id": new["revision_id"],
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_id": new["source_id"], "depositary_revision": new["depositary_revision"]},
                    "evidence_origin": new["evidence_origin"], **detail}

        summary, prior = new["summary"], (old or {}).get("summary") or {}
        label = new["record_key"]
        if new["publication_status"] == "no-longer-published":
            return [note("removed_by_source", f"{label}: the depositary no longer shows this record (status "
                                              f"{new['depositary_revision']}); it is kept, not deleted.")]
        notes = []
        eif_now = (summary.get("entry_into_force") or {}).get("date") if new["kind"] == "treaty" else (
            summary.get("effective_date") if summary.get("action_type") == "entry-into-force" else None)
        eif_before = (prior.get("entry_into_force") or {}).get("date") if new["kind"] == "treaty" else (
            prior.get("effective_date") if prior.get("action_type") == "entry-into-force" else None)
        if eif_now and eif_now != eif_before:
            notes.append(note("entry_into_force_recorded", f"{label}: entry into force {eif_now} as published.",
                              effective_date=eif_now))
        if old is None:
            if new["kind"] == "treaty-action" and summary.get("action_type") != "entry-into-force":
                notes.append(note("action_recorded", f"{label}: {summary.get('action_type_as_published')} "
                                                     f"{summary.get('date_text_as_published')} as published.",
                                  action_type=summary.get("action_type")))
            elif new["kind"] == "treaty-statement":
                notes.append(note("statement_recorded", f"{label}: {summary.get('statement_kind')} as published.",
                                  text_verbatim=summary.get("text_verbatim")))
            elif new["kind"] == "treaty":
                notes.append(note("treaty_recorded", f"{label}: treaty record acquired."))
            return notes
        changed = {k: {"before": prior.get(k), "after": summary.get(k)} for k in sorted(set(prior) | set(summary))
                   if prior.get(k) != summary.get(k) and k != "status_as_published"}
        if old.get("publication_status") == "no-longer-published":
            notes.append(note("action_recorded" if new["kind"] == "treaty-action" else "statement_recorded",
                              f"{label}: shown again by the depositary."))
        if changed:
            notes.append(note("depositary_correction", f"{label}: revision {new['revision_no']} recorded; "
                                                       f"changed {', '.join(changed)}.", changed=changed))
        return notes

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, source: Mapping[str, Any], *, run_id: str, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Re-read one declared selection within its budget; idempotent, and every unit leaves a receipt."""
        from src.ingestion.treaties_sources import TreatiesAdapter
        from src.kb.treaties_records import TreatiesProjector

        scopes = set(scopes)
        namespace = TreatiesProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = TreatiesAdapter(source, transport=transport)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = self.store.project(namespace, [r["treaty_record"] for r in page.records], run_id=run_id,
                                        source_id=source["source_id"], receipt=dict(page.receipt or {}))
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
