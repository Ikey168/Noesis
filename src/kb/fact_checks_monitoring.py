"""Monitor new and updated fact-checks and publisher status changes through subscriptions (#2659, FC10).

A fact-checks monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.campaign_finance_monitoring`: its query names a claimant (as named,
or a canonical entity reached through accepted matches), a topic query (a
search over quoted claims) or a publisher site. There is no monitor table and
no scheduler: the ``bounded-public-osint`` source-pack schedule acquires, the
maintenance orchestrator commits the watermarks, and each evaluation turns
differences into subscription events delivered through the existing poll and
outbox paths.

Notices are record changes, not assessments. Each cites the new (and the
previous) record revision and states what changed: ``fact_check_published``,
``fact_check_revised`` (rating text, numeric rating, review date or claims as
published, before and after), ``fact_check_withdrawn`` (absent from a later
release) and ``publisher_status_changed`` (the IFCN status label as published,
before and after). A claimant monitor needs the claimant scope (FC01).

A revision acquired *live* from a provider whose access is still
``unverified-live`` is withheld until a dated live run verifies it; fixture
replays are notified and marked as fixture evidence.
:meth:`FactChecksMonitor.refresh` re-reads one declared source selection
through the real adapter within its page budget; re-reading unchanged
responses adds nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.fact_checks_sources import LIVE_VERIFICATION, site_of
from src.kb.fact_checks_queries import FactCheckQueries
from src.kb.fact_checks_records import (
    CLAIMANT_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    FactCheckError,
    FactChecksStore,
    authorize,
)

CONTRACT = "noesis-fact-checks-notification-v1"
WATCH_KINDS = ("claimant", "query", "publisher")


def notifiable(row: Mapping[str, Any]) -> bool:
    provider = row.get("provider") or row["record"].get("provider")
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(provider, {}).get("status") == "verified-live")


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row["record"]["fields"]
    if row["record_kind"] == "publisher":
        return {"name_as_published": fields.get("name_as_published"),
                "status_as_published": fields.get("status_as_published"),
                "status_date_as_published": fields.get("status_date_as_published"), "listing_status": row["status"]}
    return {"review_date": fields.get("review_date"), "review_title": fields.get("review_title"),
            "listing_status": row["status"],
            "claims": [{"claim_text_as_quoted": c.get("claim_text_as_quoted"),
                        "claimant_as_named": c.get("claimant_as_named"), "rating": c.get("rating")}
                       for c in fields.get("claims") or []]}


class FactChecksMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = FactChecksStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = " ".join(str(key or "").split())
        if watch not in WATCH_KINDS or not key:
            raise FactCheckError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "claimant" and "operator" not in scopes and CLAIMANT_SCOPE not in scopes:
            raise FactCheckError("unauthorized", f"claimant monitors need {CLAIMANT_SCOPE} (FC01)")
        if watch == "publisher":
            key = site_of(key) or ""
            if not key:
                raise FactCheckError("invalid_watch", "a publisher is watched by its site")
        if watch == "query" and len(key) < 3:
            raise FactCheckError("invalid_watch", "a topic query needs at least three characters")
        query = {"operation": "search", "kind": "fact-checks-monitor", "watch": watch, "key": key}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "news", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "fact-checks-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the bounded-public-osint source-pack schedule and the maintenance orchestrator "
                                      "commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "fact-checks-monitor":
            raise FactCheckError("monitor_not_found", "subscription is not a fact-checks monitor")
        return subscription

    def _rows(self, subscription: Mapping[str, Any], scopes: set[str]) -> list[dict[str, Any]]:
        namespace, query = subscription["namespace"], subscription["query"]
        if query["watch"] == "publisher":
            return self.store.records(namespace, scopes=scopes, publisher_site=query["key"])
        ask = FactCheckQueries(self.conn)
        if query["watch"] == "claimant":
            answer = ask.for_claimant(namespace, query["key"], scopes=scopes)
        else:
            answer = ask.for_claim(namespace, scopes=scopes, text=query["key"])
        keys = sorted({item["record_key"] for item in answer["fact_checks"]})
        return self.store.records(namespace, scopes=scopes, record_keys=keys) if keys else []

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        items, withheld = [], 0
        for row in self._rows(subscription, scopes):
            if not notifiable(row):
                withheld += 1
                continue
            items.append({
                "id": f"{row['source_id']}:{row['record_key']}", "kind": row["record_kind"],
                "record_key": row["record_key"], "source_id": row["source_id"], "provider": row["provider"],
                "revision_id": row["revision_id"], "revision_no": row["revision_no"], "status": row["status"],
                "evidence_origin": row["evidence_origin"], "summary": _summary(row)})
        return {"items": items, "coverage": {"complete": True}}, withheld

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if subscription["query"]["watch"] == "claimant" and "operator" not in scopes and CLAIMANT_SCOPE not in scopes:
            raise FactCheckError("unauthorized", f"claimant monitors need {CLAIMANT_SCOPE} (FC01)")
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise FactCheckError("watermark_uncommitted", "no committed watermark yet; source-pack runs and the "
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
                    "kind": kind, "object": key, "record_key": new["record_key"], "message": message,
                    "cites": {"record_key": new["record_key"], "revision_id": new["revision_id"],
                              "revision_no": new["revision_no"],
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_id": new["source_id"], "provider": new["provider"]},
                    "evidence_origin": new["evidence_origin"], **detail}

        summary, prior = new["summary"], (old or {}).get("summary") or {}
        label = new["record_key"]
        if new["kind"] == "publisher":
            if old is None:
                return [note("publisher_listed", f"{label}: listed with status "
                                                 f"“{summary.get('status_as_published')}” as published.",
                             after=summary.get("status_as_published"))]
            return [note("publisher_status_changed", f"{label}: status “{prior.get('status_as_published')}” "
                                                     f"-> “{summary.get('status_as_published')}” as "
                                                     f"published ({summary.get('listing_status')}).",
                         before={"status_as_published": prior.get("status_as_published"),
                                 "status_date_as_published": prior.get("status_date_as_published"),
                                 "listing_status": prior.get("listing_status")},
                         after={"status_as_published": summary.get("status_as_published"),
                                "status_date_as_published": summary.get("status_date_as_published"),
                                "listing_status": summary.get("listing_status")})]
        if new["status"] != "published":
            return [note("fact_check_withdrawn", f"{label}: no longer in the source's release "
                                                 f"({new['status']}); earlier revisions stay on record.")]
        if old is None:
            return [note("fact_check_published", f"{label}: reviewed {summary.get('review_date')} with "
                                                 f"{len(summary.get('claims') or [])} claim(s), ratings as published.",
                         claims=summary.get("claims"))]
        changed = [field for field in ("review_date", "review_title", "claims", "listing_status")
                   if summary.get(field) != prior.get(field)]
        return [note("fact_check_revised", f"{label}: revision {new['revision_no']} changed {', '.join(changed) or 'the record'}"
                                           " as published.", changed=changed,
                     before={f: prior.get(f) for f in changed}, after={f: summary.get(f) for f in changed})]

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
        from src.ingestion.fact_checks_sources import FactChecksAdapter
        from src.kb.fact_checks_records import FactChecksProjector

        scopes = set(scopes)
        namespace = FactChecksProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = FactChecksAdapter(source, transport=transport, secret=secret)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = self.store.project(namespace, [r["fact_check_record"] for r in page.records], run_id=run_id,
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
