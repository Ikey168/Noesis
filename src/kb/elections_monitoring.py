"""Monitor new result vintages, recounts, corrections and poll readings through subscriptions (#1908, L10).

An elections monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names what is
watched - a contest, a constituency (all its contests), a poll series or all
poll series of an election - so there is no watcher table and no scheduler.
Refresh follows the ``official-political-records`` source-pack schedule; each
evaluation runs at a committed watermark and the subscription store turns new
items into events delivered through its poll and outbox paths.

Items are result vintages and poll readings. A new vintage produces a
``preliminary_result``, ``certified_result`` (listing the figures that changed
against the preliminary vintage, each side cited to its source revision),
``recount`` or ``correction`` notification; a new poll reading produces a
``poll_reading`` notification with publisher, fieldwork window and sample size
and is never described as a result change. A vintage acquired *live* from a
provider whose access decision is still ``unverified-live`` is withheld until
a dated live run verifies the provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.election_sources import unverified
from src.kb.elections import (
    CERTIFIED_CLASS,
    READ_SCOPE,
    ElectionError,
    ElectionStore,
    authorize,
)

CONTRACT = "noesis-election-notification-v1"
WATCH_KINDS = ("contest", "constituency", "poll_series", "election_polls")
EVENT_KINDS = {
    "preliminary": "preliminary_result",
    "certified": "certified_result",
    "recount": "recount",
    "corrected": "correction",
}


def notifiable(vintage: Mapping[str, Any]) -> bool:
    """Fixture evidence, or live evidence from a provider whose live access is verified."""
    return vintage["evidence_origin"] == "fixture" or not unverified(
        vintage["source_revision"]["provider"]
    )


class ElectionMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = ElectionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        watch: str,
        key: str,
        principal_id: str,
        scopes: Iterable[str],
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS or not str(key or "").strip():
            raise ElectionError(
                "invalid_watch", f"watch one of {WATCH_KINDS} with a key"
            )
        if watch == "contest":
            self.store.contest(namespace, key)
        elif watch == "constituency":
            self.store.constituency(namespace, key)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "political",
                "query": {
                    "operation": "search",
                    "kind": "elections-monitor",
                    "watch": watch,
                    "key": str(key),
                },
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "elections-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "the official-political-records source-pack schedule and the maintenance orchestrator "
            "commit the watermarks this monitor evaluates; no new scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "elections-monitor":
            raise ElectionError(
                "monitor_not_found", "subscription is not an elections monitor"
            )
        return subscription

    def _contests(self, namespace: str, watch: str, key: str) -> list[str]:
        if watch == "contest":
            return [key]
        if watch == "constituency":
            return [
                c["contest_id"]
                for c in self.store.contests(namespace, constituency_id=key)
            ]
        return []

    def snapshot(
        self, subscription: Mapping[str, Any], scopes: set[str]
    ) -> tuple[dict[str, Any], int]:
        from src.kb.elections_polls import ElectionPolls

        namespace, query = subscription["namespace"], subscription["query"]
        items, withheld = [], 0
        for contest_id in self._contests(namespace, query["watch"], query["key"]):
            for vintage in self.store.history(namespace, contest_id):
                if not notifiable(vintage):
                    withheld += 1
                    continue
                items.append(
                    {
                        "id": f"vintage:{vintage['vintage_id']}",
                        "item": "result_vintage",
                        "contest_id": contest_id,
                        "vintage_id": vintage["vintage_id"],
                        "kind": vintage["kind"],
                        "published_on": vintage["published_on"],
                        "evidence_origin": vintage["evidence_origin"],
                        "source_revision": vintage["source_revision"],
                    }
                )
        if query["watch"] in {"poll_series", "election_polls"}:
            polls = ElectionPolls(self.conn, initialize=False)
            readings = polls.readings(
                namespace,
                scopes=scopes,
                series_id=query["key"] if query["watch"] == "poll_series" else None,
                election_id=query["key"]
                if query["watch"] == "election_polls"
                else None,
                current_only=False,
            )
            for reading in readings:
                items.append(
                    {
                        "id": f"poll:{reading['reading_id']}",
                        "item": "poll_reading",
                        "typed_as": "poll",
                        "series_id": reading["series_id"],
                        "publisher": reading["publisher"],
                        "option": reading["option"],
                        "value": reading["value"],
                        "fieldwork_start": reading["fieldwork_start"],
                        "fieldwork_end": reading["fieldwork_end"],
                        "sample_size": reading["sample_size"],
                        "source_revision": reading["source_revision"],
                    }
                )
        return {"items": items, "coverage": {"complete": True}}, withheld

    def run(
        self,
        subscription_id: str,
        watermark: int | None = None,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [namespace],
            ).fetchone()
            if row is None or row[0] is None:
                raise ElectionError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        result, withheld = self.snapshot(subscription, scopes)
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            result,
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=self.now(),
        )
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(self._classify(namespace, event_id, *row))
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
            "withheld_unverified_live_vintages": withheld,
            "delivery": subscription["delivery"],
        }

    def _classify(
        self,
        namespace: str,
        event_id: str,
        event_type: str,
        key: str,
        after: str | None,
    ):
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        base = {"contract": CONTRACT, "event_id": event_id, "object": key}
        if item["item"] == "poll_reading":
            return [
                {
                    **base,
                    "notification_id": f"{event_id}:poll_reading",
                    "kind": "poll_reading",
                    "typed_as": "poll",
                    "message": f"New poll reading from {item['publisher']} (fieldwork {item['fieldwork_start']} to "
                    f"{item['fieldwork_end']}, n={item['sample_size']}); a poll, not a result.",
                    **{
                        k: item[k]
                        for k in (
                            "publisher",
                            "fieldwork_start",
                            "fieldwork_end",
                            "sample_size",
                            "option",
                            "value",
                            "series_id",
                        )
                    },
                    "cites": {"source_revision": item["source_revision"]},
                }
            ]
        kind = EVENT_KINDS[item["kind"]]
        note = {
            **base,
            "notification_id": f"{event_id}:{kind}",
            "kind": kind,
            "contest_id": item["contest_id"],
            "message": f"{kind.replace('_', ' ')} published on {item['published_on']} for contest {item['contest_id']}.",
            "cites": {
                "vintage_id": item["vintage_id"],
                "source_revision": item["source_revision"],
            },
            "evidence_origin": item["evidence_origin"],
        }
        if item["kind"] in CERTIFIED_CLASS:
            compare_to = (
                ("preliminary",) if item["kind"] == "certified" else CERTIFIED_CLASS
            )
            note.update(self._changes(namespace, item, compare_to))
        return [note]

    def _changes(
        self, namespace: str, item: Mapping[str, Any], compare_to
    ) -> dict[str, Any]:
        history = self.store.history(namespace, item["contest_id"])
        position = next(
            i for i, v in enumerate(history) if v["vintage_id"] == item["vintage_id"]
        )
        earlier = [v for v in history[:position] if v["kind"] in compare_to]
        if not earlier:
            return {"compared_with": None, "changed_figures": None}
        before, after = earlier[-1], history[position]
        old, new = (
            ElectionStore._flatten(before["figures"]),
            ElectionStore._flatten(after["figures"]),
        )
        return {
            "compared_with": {
                "vintage_id": before["vintage_id"],
                "kind": before["kind"],
                "source_revision": before["source_revision"],
            },
            "changed_figures": [
                {
                    "entry": k[0],
                    "field": k[1],
                    "before": {"value": old.get(k), "vintage_id": before["vintage_id"]},
                    "after": {"value": new.get(k), "vintage_id": after["vintage_id"]},
                }
                for k in sorted(set(old) | set(new))
                if old.get(k) != new.get(k)
            ],
        }

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )
