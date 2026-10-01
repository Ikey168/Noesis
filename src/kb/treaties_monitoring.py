"""Monitor treaties and participants through subscriptions (#2581, TR10).

A treaties monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`, ``platform.subscriptions``),
following :mod:`src.kb.campaign_finance_monitoring` and
:mod:`src.kb.courts_justice_monitoring`: its query names a **treaty** (record
key or published identifier such as ``cets:990``) or a **participant**
(participant key, ISO 3166-1 code through an accepted identity match, or the
name as published). There is no monitor table and no scheduler: the
``legal-research`` source-pack schedule acquires, the maintenance orchestrator
commits watermarks, and each evaluation turns differences into subscription
events delivered through the existing poll and outbox paths.

Notices are record changes, not assessments. Each cites the new (and the
previous) record revision and states what changed: ``new_action``,
``entry_into_force``, ``depositary_correction`` (the changed fields),
``action_removed_by_source``, ``new_treaty`` and ``treaty_revised``. A revision
acquired *live* from a provider whose access is not ``verified-live`` is
withheld until a dated live run verifies it; fixture replays are notified and
marked as fixture evidence. :meth:`TreatiesMonitor.refresh` re-reads one
declared source through the real adapter within its budget; unchanged
responses add nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.treaties_sources import LIVE_VERIFICATION
from src.kb.treaties_records import READ_SCOPE, WRITE_SCOPE, TreatiesError, authorize
from src.kb.treaties_store import TreatiesProjector, TreatyStore, cite

CONTRACT = "noesis-treaty-notification-v1"
WATCH_KINDS = ("treaty", "participant")
_SUMMARY = ("action_type", "action_date", "deposit_date", "effective_date", "text", "change")


def notifiable(provider: str, origin: str | None) -> bool:
    return origin == "fixture" or LIVE_VERIFICATION.get(provider, {}).get("status") == "verified-live"


class TreatiesMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = TreatyStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def _queries(self):
        from src.kb.treaties_queries import TreatyQueries

        return TreatyQueries(self.conn, now=self.now)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise TreatiesError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "treaty" and not (key.startswith("treaties:treaty:") or ":" in key):
            raise TreatiesError("invalid_watch", "a treaty is watched by record key or scheme:identifier "
                                                 "(untc:, celex:, cets:)")
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

    # ------------------------------------------------------------------ snapshots

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, query = subscription["namespace"], subscription["query"]
        queries = self._queries()
        if query["watch"] == "treaty":
            treaty_keys, _ = queries.resolve_treaty(namespace, query["key"])
            action_keys = self.store.action_keys(namespace, treaty_keys=treaty_keys)
        else:
            participant_keys, _ = queries.resolve_participant(namespace, query["key"])
            action_keys = self.store.action_keys(namespace, participant_keys=participant_keys)
            treaty_keys = []
        items, withheld = [], 0
        for key in treaty_keys:
            visible = [r for r in self.store.revisions(namespace, key) if notifiable(r["provider"],
                                                                                     r["evidence_origin"])]
            withheld += len(self.store.revisions(namespace, key)) - len(visible)
            if visible:
                row = visible[-1]
                fields = self.store.record(row)["fields"]
                items.append({"id": f"treaty:{key}", "kind": "treaty", "cite": cite(row),
                              "summary": {"title": row["title"],
                                          "entry_into_force": dict(fields.get("entry_into_force") or {}).get("date"),
                                          "entry_into_force_dates": dict(fields.get("entry_into_force") or {}).get(
                                              "dates"),
                                          "change": row["change"]}})
        for key in action_keys:
            revisions = self.store.revisions(namespace, key)
            visible = [r for r in revisions if notifiable(r["provider"], r["evidence_origin"])]
            withheld += len(revisions) - len(visible)
            if visible:
                row = visible[-1]
                items.append({"id": f"action:{key}", "kind": "action", "cite": cite(row),
                              "summary": {"participant": row["participant_name"], "treaty_key": row["treaty_key"],
                                          **{k: row[k] for k in _SUMMARY}}})
        return {"items": items, "coverage": {"complete": True}}, withheld

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
                raise TreatiesError("watermark_uncommitted", "no committed watermark yet; source-pack runs and the "
                                                             "maintenance orchestrator commit them")
            watermark = int(row[0])
        result, withheld = self.snapshot(subscription)
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
        summary, previous = new["summary"], (old or {}).get("summary") or {}
        changed = sorted(k for k in set(summary) | set(previous) if old is not None and previous.get(k) != summary.get(k)
                         and k != "change")

        def note(kind: str, message: str) -> dict[str, Any]:
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "object": key, "message": message, "changed_fields": changed,
                    "summary": summary, "previous_summary": previous or None,
                    "cites": {**new["cite"], "previous": (old or {}).get("cite")},
                    "evidence_origin": new["cite"].get("evidence_origin"),
                    "notice": "a record change as the source published it; not an assessment"}

        record = new["cite"]["record_key"]
        revision = f"revision {new['cite']['revision_no']} ({new['cite']['depositary_revision'] or 'no stamp'})"
        if new["kind"] == "treaty":
            if old is None:
                return [note("new_treaty", f"{summary['title']}: first acquired, {revision}")]
            if "entry_into_force" in changed or "entry_into_force_dates" in changed:
                return [note("entry_into_force", f"{summary['title']}: entry into force as published changed from "
                                                 f"{previous.get('entry_into_force')} to "
                                                 f"{summary['entry_into_force']} ({revision})")]
            return [note("treaty_revised", f"{summary['title']}: {revision} changed {', '.join(changed) or 'the record'}")]
        who, what = summary["participant"], summary["action_type"]
        if old is None:
            kind = "entry_into_force" if what == "entry-into-force" else "new_action"
            when = summary["action_date"] or summary["deposit_date"] or summary["effective_date"] or "undated"
            return [note(kind, f"{who}: {what} dated {when} recorded ({record}, {revision})")]
        if summary["change"] == "removed-by-source":
            return [note("action_removed_by_source", f"{who}: {what} is no longer listed by the source ({revision}); "
                                                     "earlier revisions stay on record")]
        details = "; ".join(f"{k}: {previous.get(k)!r} -> {summary.get(k)!r}" for k in changed if k != "text")
        if "text" in changed:
            details = (details + "; " if details else "") + "text as published changed"
        return [note("depositary_correction", f"{who}: {what} corrected in {revision}: {details or 'record changed'}")]

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
                                        source_id=source["source_id"], receipt=dict(page.receipt or {}),
                                        observed_at_ms=self.now())
            for change, count in result.items():
                counts[change] = counts.get(change, 0) + count
            units += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        del principal_id
        return {"run_id": run_id, "source_id": source["source_id"], "units": units, "counts": counts,
                "complete": cursor is None,
                "receipts": self.store.receipts(namespace, run_id, scopes=scopes | {READ_SCOPE})}


__all__ = ["CONTRACT", "WATCH_KINDS", "TreatiesMonitor", "notifiable"]
