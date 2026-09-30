"""Monitor bill actions, stages, new text versions and votes through subscriptions (#2208, LT10).

A legislation monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.lobbying_monitoring`: its query names a bill (``us-bill:…`` /
``uk-bill:…``) or a sponsor (a member key, watching every bill that member
sponsored or cosponsored). There is no monitor table and no scheduler: the
``official-political-records`` source-pack schedule acquires, the maintenance
orchestrator commits the watermarks, and each evaluation turns differences
into subscription events delivered through the existing poll and outbox paths.

Notices state what a provider recorded and cite the new and the previous
record revision: ``actions_recorded`` (the new actions verbatim),
``law_cited``, ``text_version_published``, ``stage_recorded`` /
``stage_revised``, ``royal_assent_recorded``, ``vote_held``,
``division_corrected``, ``debate_referenced`` and ``revised`` otherwise.
Nothing predicts passage.

A revision acquired *live* from a provider whose access is still
``unverified-live`` is withheld until a dated live run verifies it; fixture
replays are notified and marked as fixture evidence. :meth:`LegislationMonitor.refresh`
re-reads one declared source selection through the real adapter within its
budget; re-reading unchanged responses adds nothing and every unit leaves a
receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.legislation_sources import LIVE_VERIFICATION
from src.kb.legislation import READ_SCOPE, WRITE_SCOPE, LegislationError, LegislationStore, authorize

CONTRACT = "noesis-legislation-notification-v1"
WATCH_KINDS = ("bill", "sponsor")


def notifiable(row: Mapping[str, Any]) -> bool:
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(row["provider"], {}).get("status") == "verified-live")


def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    kind = record["record_kind"]
    if kind in {"us-bill", "us-bill-status"}:
        return {"actions": [[a["action_date"], a["action_code"], a["text"]] for a in fields.get("actions") or []],
                "laws": sorted(law["citation"] for law in fields.get("laws") or []),
                "cosponsors": sorted(f"{c['member_id']}:{c.get('withdrawn_date') or 'active'}"
                                     for c in fields.get("cosponsors") or [])}
    if kind == "us-text-version":
        return {"version_code": fields.get("version_code"), "date_issued": fields.get("date_issued"),
                "content_sha256": fields.get("content_sha256")}
    if kind in {"us-roll-call", "uk-division"}:
        return {"date": fields.get("date"), "result": fields.get("result") or fields.get("title"),
                "positions": len(fields.get("positions") or []),
                "totals": fields.get("totals_as_published") or fields.get("counts_as_published")}
    if kind == "uk-bill":
        return {"royal_assent": fields.get("royal_assent"), "current_house": fields.get("current_house"),
                "is_act": fields.get("is_act")}
    if kind == "uk-stage":
        return {"description": fields.get("description"), "house": fields.get("house"),
                "sittings": [s["date"] for s in fields.get("sittings") or []]}
    if kind == "uk-publication":
        return {"title": fields.get("title"), "publication_type": fields.get("publication_type"),
                "display_date": fields.get("display_date")}
    return {"date": fields.get("date"), "title": fields.get("title")}


class LegislationMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = LegislationStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise LegislationError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "bill" and not key.startswith(("us-bill:", "uk-bill:")):
            raise LegislationError("invalid_watch", "a bill is watched by its bill key (us-bill:… or uk-bill:…)")
        if watch == "sponsor" and not key.startswith("legislation:member:"):
            raise LegislationError("invalid_watch", "a sponsor is watched by its member key")
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "political",
             "query": {"operation": "search", "kind": "legislation-monitor", "watch": watch, "key": key},
             "filters": {"watch": watch}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "legislation-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the official-political-records source-pack schedule and the maintenance "
                                      "orchestrator commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "legislation-monitor":
            raise LegislationError("monitor_not_found", "subscription is not a legislation monitor")
        return subscription

    def _bills(self, namespace: str, query: Mapping[str, Any], scopes: set[str]) -> list[str]:
        if query["watch"] == "bill":
            return [query["key"]]
        from src.kb.legislation_identity import LegislationIdentity

        members = LegislationIdentity(self.conn, initialize=False).members(namespace, scopes=scopes)
        member = next((m for m in members if m["member_key"] == query["key"]), None)
        return sorted({a["bill_key"] for a in (member or {}).get("appearances", [])
                       if a["role"] in {"sponsor", "cosponsor"} and a["bill_key"]})

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        namespace = subscription["namespace"]
        items, withheld = [], 0
        for bill_key in self._bills(namespace, subscription["query"], scopes):
            found = self.store.records_for_bill(namespace, bill_key, scopes=scopes)
            for row in found["linked"] + found["candidates"]:
                if not notifiable(row):
                    withheld += 1
                    continue
                items.append({
                    "id": f"{row['source_id']}:{row['record_key']}", "kind": row["record_kind"],
                    "bill_key": bill_key, "record_key": row["record_key"], "source_id": row["source_id"],
                    "provider": row["provider"], "document_id": row["document_id"],
                    "revision_id": row["revision_id"], "revision_no": row["revision_no"],
                    "native_revision": row["native_revision"], "evidence_origin": row["evidence_origin"],
                    "linked": row["bill_key"] is not None, "summary": _summary(row["record"] or {}),
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
                raise LegislationError("watermark_uncommitted", "no committed watermark yet; source-pack runs and "
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
                    "kind": kind, "object": key, "bill_key": new["bill_key"], "record_key": new["record_key"],
                    "message": message,
                    "cites": {"document_id": new["document_id"], "revision_id": new["revision_id"],
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_id": new["source_id"], "native_revision": new["native_revision"]},
                    "evidence_origin": new["evidence_origin"], **detail}

        kind, summary, label = new["kind"], new["summary"], new["record_key"]
        prior = (old or {}).get("summary") or {}
        if kind in {"us-bill", "us-bill-status"}:
            if old is None:
                return [note("bill_recorded", f"{label}: {len(summary['actions'])} actions on record "
                                              f"({new['provider']}).")]
            added = summary["actions"][len(prior.get("actions") or []):]
            notes = []
            if added and summary["actions"][:len(prior.get("actions") or [])] == prior.get("actions"):
                notes.append(note("actions_recorded", f"{label}: {len(added)} new action(s) recorded by "
                                                      f"{new['provider']}.",
                                  actions=[{"action_date": a[0], "action_code": a[1], "text": a[2]} for a in added]))
            laws = sorted(set(summary["laws"]) - set(prior.get("laws") or []))
            if laws:
                notes.append(note("law_cited", f"{label}: {', '.join(laws)} cited by {new['provider']}.", laws=laws))
            return notes or [note("revised", f"{label}: revision {new['revision_no']} recorded.")]
        if kind in {"us-text-version", "uk-publication"}:
            if old is None:
                what = summary.get("version_code") or summary.get("publication_type")
                return [note("text_version_published", f"{label}: {what} published "
                                                       f"{summary.get('date_issued') or summary.get('display_date')}.")]
            return [note("revised", f"{label}: revision {new['revision_no']} recorded.")]
        if kind in {"us-roll-call", "uk-division"}:
            if old is None:
                state = "" if new["linked"] else " (an unlinked candidate until reviewed)"
                return [note("vote_held", f"{label} on {summary['date']}: {summary['result']}{state}.")]
            return [note("division_corrected", f"{label}: the published list changed (revision "
                                               f"{new['revision_no']}).", previous_totals=prior.get("totals"),
                         totals=summary.get("totals"))]
        if kind == "uk-stage":
            if old is None:
                if "royal assent" in str(summary.get("description") or "").casefold():
                    return [note("royal_assent_recorded", f"{label}: Royal Assent recorded "
                                                          f"({', '.join(summary['sittings'])}).")]
                return [note("stage_recorded", f"{label}: {summary['description']} ({summary['house']}) on "
                                               f"{', '.join(summary['sittings'])}.")]
            return [note("stage_revised", f"{label}: sittings now {', '.join(summary['sittings'])}.")]
        if kind == "uk-debate-reference" and old is None:
            return [note("debate_referenced", f"{label}: {summary.get('title')} on {summary.get('date')}.")]
        return [note("revised" if old else "recorded", f"{label}: revision {new['revision_no']} recorded.")]

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
        from src.ingestion.legislation_sources import LegislationAdapter
        from src.kb.legislation import LegislationProjector

        scopes = set(scopes)
        namespace = LegislationProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = LegislationAdapter(source, transport=transport, secret=secret)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = self.store.project(namespace, [r["legislation_record"] for r in page.records], run_id=run_id,
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
