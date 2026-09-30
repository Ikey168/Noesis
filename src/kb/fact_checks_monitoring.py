"""Monitor new and updated fact-checks and publisher status changes through subscriptions (#2659, FC10).

A fact-checks monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.campaign_finance_monitoring`: its query names a **claimant** (a
canonical entity reached through accepted claimant matches, or the claimant's
name as published), a **topic query** (words that must all appear in the claim
as quoted - a filter on published text, never a claim match) or a
**publisher** (its website domain: its fact-checks and its IFCN signatory
status). There is no monitor table and no scheduler: the
``bounded-public-osint`` source-pack schedule acquires, the maintenance
orchestrator commits the watermarks, and each evaluation turns differences into
subscription events delivered through the existing poll and outbox paths.

Notices are record changes, not assessments. Each cites the new (and the
previous) record revision and states what changed: ``fact_check_published``,
``rating_changed`` (the publisher's rating text before and after, verbatim),
``review_date_changed``, ``fact_check_revised``, ``fact_check_absent_from_source``
(a later release no longer carries it), ``publisher_listed``,
``publisher_status_changed`` (IFCN status as published, before and after) and
``publisher_absent_from_listing``.

A revision acquired *live* from a provider whose access is still
``unverified-live`` is withheld until a dated live run verifies it; fixture
replays are notified and marked as fixture evidence.
:meth:`FactCheckMonitor.refresh` re-reads one declared source selection through
the real adapter within its page budget; re-reading unchanged responses adds
nothing and every unit leaves a receipt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.fact_checks_sources import LIVE_VERIFICATION, domain
from src.kb.fact_checks_identity import FactCheckIdentity, claimant_key
from src.kb.fact_checks_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    FactCheckError,
    FactCheckProjector,
    FactCheckStore,
    authorize,
)

CONTRACT = "noesis-fact-check-notification-v1"
WATCH_KINDS = ("claimant", "query", "publisher")


def notifiable(row: Mapping[str, Any]) -> bool:
    provider = row.get("provider") or row["record"].get("provider")
    return (row["evidence_origin"] == "fixture"
            or LIVE_VERIFICATION.get(provider, {}).get("status") == "verified-live")


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", str(text or "").casefold()))


def _summary(row: Mapping[str, Any]) -> dict[str, Any]:
    fields = row["record"]["fields"]
    if row["record_kind"] == "publisher":
        return {"ifcn_status": fields.get("ifcn_status"), "status_as_published": fields.get("status_as_published"),
                "status_date": fields.get("status_date"), "presence": fields.get("status"),
                "name_as_published": fields.get("name_as_published")}
    claim = fields["claims"][0]
    return {"rating_as_published": claim["rating"], "review_date": fields.get("review_date"),
            "claim_as_quoted": claim["claim_text"], "presence": fields.get("status"),
            "publisher": fields["publisher"]["name_as_published"] or fields["publisher"]["domain"],
            "review_url": fields.get("review_url")}


class FactCheckMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = FactCheckStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = " ".join(str(key or "").split())
        if watch not in WATCH_KINDS or not key:
            raise FactCheckError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "publisher":
            key = domain(key) or ""
            if not key:
                raise FactCheckError("invalid_watch", "a publisher is watched by its website domain")
        if watch == "query" and not _words(key):
            raise FactCheckError("invalid_watch", "a topic query names at least one word")
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
        key = query["key"]
        if query["watch"] == "publisher":
            return self.store.records(namespace, scopes=scopes, publisher_key=f"fact-check:publisher:{key}")
        views = self.store.records(namespace, scopes=scopes, kinds=["fact-check"])
        if query["watch"] == "query":
            wanted = _words(key)
            return [v for v in views if wanted <= _words(v["record"]["fields"]["claims"][0]["claim_text"])]
        identity = FactCheckIdentity(self.conn, initialize=False)
        if key.startswith("ent-"):
            subjects = {m["left_key"] for m in identity.accepted(namespace, "claimant", scopes=scopes, key=key)}
        else:
            subjects = {key if key.startswith("fact-check:claimant:") else claimant_key({"name_as_published": key})}
        return [v for v in views if claimant_key(v["record"]["fields"]["claims"][0].get("claimant")) in subjects]

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        items, withheld = [], 0
        for row in self._rows(subscription, scopes):
            if not notifiable(row):
                withheld += 1
                continue
            items.append({"id": f"{row['source_id']}:{row['record_key']}", "kind": row["record_kind"],
                          "record_key": row["record_key"], "source_id": row["source_id"], "provider": row["provider"],
                          "revision_id": row["revision_id"], "revision_no": row["revision_no"],
                          "observed_at": row["citation"]["observed_at"], "locator": row["citation"]["locator"],
                          "evidence_origin": row["evidence_origin"], "summary": _summary(row)})
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
                              "previous_revision_id": old["revision_id"] if old else None,
                              "source_id": new["source_id"], "provider": new["provider"],
                              "locator": new["locator"], "observed_at": new["observed_at"]},
                    "evidence_origin": new["evidence_origin"], **detail}

        summary, prior = new["summary"], (old or {}).get("summary") or {}
        if new["kind"] == "publisher":
            if old is None:
                return [note("publisher_listed", f"{summary['name_as_published']}: IFCN status "
                                                 f"'{summary['status_as_published']}' as published.",
                             after=summary["ifcn_status"])]
            if summary["presence"] != prior.get("presence") and summary["presence"] != "published":
                return [note("publisher_absent_from_listing", f"{summary['name_as_published']} is no longer on the "
                                                              "IFCN signatory listing as acquired.")]
            return [note("publisher_status_changed", f"{summary['name_as_published']}: IFCN status now "
                                                     f"'{summary['status_as_published']}' (dated "
                                                     f"{summary['status_date']}).",
                         before=prior.get("status_as_published"), after=summary["status_as_published"])]
        label = f"{summary['publisher']} on \"{summary['claim_as_quoted']}\""
        if old is None:
            return [note("fact_check_published", f"{label}: rated '{summary['rating_as_published']['text']}' as "
                                                 f"published on {summary['review_date']}.",
                         rating_as_published=summary["rating_as_published"])]
        if summary["presence"] != prior.get("presence") and summary["presence"] != "published":
            return [note("fact_check_absent_from_source", f"{label}: no longer carried by {new['provider']} as "
                                                          "acquired; the earlier revision stays on record.")]
        notes = []
        if summary["rating_as_published"] != prior.get("rating_as_published"):
            notes.append(note("rating_changed", f"{label}: the publisher's rating changed from "
                                                f"'{(prior.get('rating_as_published') or {}).get('text')}' to "
                                                f"'{summary['rating_as_published']['text']}'.",
                              before=prior.get("rating_as_published"), after=summary["rating_as_published"]))
        if summary["review_date"] != prior.get("review_date"):
            notes.append(note("review_date_changed", f"{label}: review date now {summary['review_date']}.",
                              before=prior.get("review_date"), after=summary["review_date"]))
        return notes or [note("fact_check_revised", f"{label}: revision {new['revision_no']} recorded.")]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, source: Mapping[str, Any], *, run_id: str, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, secret: str | None = None,
                observed_at_ms: int | None = None) -> dict[str, Any]:
        """Re-read one declared selection within its budget; idempotent, and every unit leaves a receipt."""
        from src.ingestion.fact_checks_sources import FactChecksAdapter

        scopes = set(scopes)
        namespace = FactCheckProjector.namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = FactChecksAdapter(source, transport=transport, secret=secret)
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < len(adapter.units):  # the declared selection bounds the run; each unit its pages
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = self.store.project(namespace, [r["fact_check_record"] for r in page.records], run_id=run_id,
                                        source_id=source["source_id"], receipt=dict(page.receipt or {}),
                                        observed_at_ms=observed_at_ms)
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
