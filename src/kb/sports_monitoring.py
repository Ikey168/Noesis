"""Monitor fixture moves, result corrections and table changes through subscriptions (#2145, SP10).

A sports monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names what is
watched - a competition season, a team or a match - so there is no watcher
table and no scheduler: refresh follows the ``sports-records`` source-pack
schedule, each evaluation runs at a committed watermark, and the subscription
store turns new items into events delivered through its poll and outbox paths.

Events: ``fixture_rescheduled``, ``result_published``, ``result_corrected``,
``forfeit_awarded``, ``table_changed`` and ``forecast_resolvable``; every
notification cites the old and the new revision and the source. A revision
that arrives late with an older publication date is history, not news: it is
counted as backfilled and never announced as a correction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from src.kb.sports_records import (
    OFFICIAL_CLASS,
    READ_SCOPE,
    SportsError,
    authorize,
    table_exists,
)
from src.kb.sports_store import SportsStore

CONTRACT = "noesis-sports-notification-v1"
WATCH_KINDS = ("competition", "team", "match")
EVENT_KINDS = (
    "fixture_rescheduled",
    "result_published",
    "result_corrected",
    "forfeit_awarded",
    "table_changed",
    "forecast_resolvable",
)


def _cite(revision: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if revision is None:
        return None
    body = revision["body"]
    return {
        k: v
        for k, v in {
            "revision_id": revision["revision_id"],
            "status": revision.get("status") or body.get("status"),
            "published_at": revision.get("published_at"),
            "score": body.get("score"),
            "score_text": body.get("score_text"),
            "kickoff": body.get("kickoff"),
            "deciding_body": body.get("deciding_body"),
            "decision": body.get("decision"),
            "source": {
                k2: v2
                for k2, v2 in revision["source"].items()
                if k2 in {"provider", "url", "locator", "attribution", "acquisition_id"}
            },
        }.items()
        if v is not None
    }


class SportsMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = SportsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self, namespace, request_key, *, watch, key, principal_id, scopes, delivery=None
    ):
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS or not str(key or "").strip():
            raise SportsError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        self.store.require_ready(namespace)
        record_type = {"competition": "season", "team": "team", "match": "fixture"}[
            watch
        ]
        if self.store.current(namespace, record_type, key) is None:
            raise SportsError(
                "not_found", f"{key} is not a {record_type} record in this namespace"
            )
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "sports",
                "query": {
                    "operation": "search",
                    "kind": "sports-monitor",
                    "watch": watch,
                    "key": str(key),
                },
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "sports-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "the sports-records source-pack schedule and the maintenance orchestrator commit "
            "the watermarks this monitor evaluates; no new scheduler",
        }

    def _subscription(self, subscription_id, principal_id, scopes):
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "sports-monitor":
            raise SportsError(
                "monitor_not_found", "subscription is not a sports monitor"
            )
        return subscription

    def _fixtures(self, namespace, watch, key) -> list[str]:
        if watch == "match":
            return [key]
        if watch == "competition":
            return self.store.records(namespace, "fixture", parent=key)
        from src.kb.sports_identity import SportsIdentity

        keys = set(
            SportsIdentity(self.conn, initialize=False).linked(namespace, key)
            if table_exists(self.conn, "ownership_identity_candidates")
            else [key]
        )
        return [
            f
            for f in self.store.records(namespace, "fixture")
            if set(
                dict(
                    (self.store.body(namespace, "fixture", f) or {}).get("sides") or {}
                ).values()
            )
            & keys
        ]

    def snapshot(self, subscription: Mapping[str, Any]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        items = []
        for fixture_key in self._fixtures(namespace, query["watch"], query["key"]):
            for record_type, item in (
                ("fixture_schedule_revision", "schedule"),
                ("match_result_revision", "result"),
            ):
                for revision in self.store.history(namespace, record_type, fixture_key):
                    items.append(
                        {
                            "id": f"{item}:{revision['revision_id']}",
                            "item": item,
                            "fixture_key": fixture_key,
                            "revision_id": revision["revision_id"],
                            "published_ms": revision["published_ms"],
                        }
                    )
        if query["watch"] == "competition":
            from src.kb.sports_queries import SportsQueries

            today = (
                datetime.fromtimestamp(self.now() / 1000, tz=timezone.utc)
                .date()
                .isoformat()
            )
            table = SportsQueries(self.conn).standings_as_of(
                namespace, query["key"], today, scopes={"operator"}
            )
            items.append(
                {
                    "id": f"table:{query['key']}",
                    "item": "table",
                    "season_key": query["key"],
                    "as_of": today,
                    "rows": [
                        [r["team_key"], r.get("team_name"), r["position"], r["points"]]
                        for r in table["table"]
                    ],
                    "counted": sorted(
                        [c["result"]["revision_id"] for c in table["counted_results"]]
                        + [d["revision_id"] for d in table["deductions"]]
                    ),
                }
            )
        return {"items": items, "coverage": {"complete": True}}

    def run(
        self, subscription_id, watermark=None, *, principal_id, scopes
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
                raise SportsError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the "
                    "maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            self.snapshot(subscription),
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=self.now(),
        )
        notifications, backfilled = [], 0
        for event_id in evaluated.get("event_ids", []):
            event_type, key, before, after = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                "WHERE event_id=?",
                [event_id],
            ).fetchone()
            made, late = self._classify(
                namespace, event_id, event_type, key, before, after
            )
            notifications += made
            backfilled += late
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
            "backfilled_history": backfilled,
            "delivery": subscription["delivery"],
        }

    def _classify(self, namespace, event_id, event_type, key, before, after):
        base = {"contract": CONTRACT, "event_id": event_id, "object": key}
        if event_type == "changed" and after:
            old, new = json.loads(before), json.loads(after)
            if new["item"] != "table" or old["rows"] == new["rows"]:
                return [], 0
            positions = {r[0]: r for r in old["rows"]}
            return [
                {
                    **base,
                    "notification_id": f"{event_id}:table_changed",
                    "kind": "table_changed",
                    "season_key": new["season_key"],
                    "changed_rows": [
                        {
                            "team_key": r[0],
                            "team_name": r[1],
                            "before": positions.get(r[0], [None] * 4)[2:],
                            "after": r[2:],
                        }
                        for r in new["rows"]
                        if positions.get(r[0]) != r
                    ],
                    "cites": {
                        "old": {
                            "as_of": old["as_of"],
                            "counted_revisions": old["counted"],
                        },
                        "new": {
                            "as_of": new["as_of"],
                            "counted_revisions": new["counted"],
                        },
                    },
                }
            ], 0
        if event_type != "added" or not after:
            return [], 0
        item = json.loads(after)
        if item["item"] == "table":
            return [], 0
        record_type = (
            "fixture_schedule_revision"
            if item["item"] == "schedule"
            else "match_result_revision"
        )
        history = self.store.labelled_history(
            namespace, record_type, item["fixture_key"]
        )
        index = next(
            i for i, r in enumerate(history) if r["revision_id"] == item["revision_id"]
        )
        new, old = history[index], history[index - 1] if index else None
        if any(
            later["observed_seq"] < new["observed_seq"]
            for later in history[index + 1 :]
        ):
            return (
                [],
                1,
            )  # published earlier than revisions already known: history, not a change
        cites = {"old": _cite(old), "new": _cite(new)}
        if item["item"] == "schedule":
            if old is None or (
                old["body"].get("kickoff") == new["body"].get("kickoff")
                and old["body"].get("status") == new["body"].get("status")
            ):
                return [], 0
            return [
                {
                    **base,
                    "notification_id": f"{event_id}:fixture_rescheduled",
                    "kind": "fixture_rescheduled",
                    "fixture_key": item["fixture_key"],
                    "status": new["status"],
                    "from": old["body"].get("kickoff"),
                    "to": new["body"].get("kickoff"),
                    "cites": cites,
                }
            ], 0
        declared = new["body"]["status"]
        kind = (
            "forfeit_awarded"
            if declared == "forfeit_awarded"
            else "result_corrected"
            if new["status"] in {"corrected", "annulled"}
            else "result_published"
        )
        notes = [
            {
                **base,
                "notification_id": f"{event_id}:{kind}",
                "kind": kind,
                "fixture_key": item["fixture_key"],
                "status": new["status"],
                "cites": cites,
            }
        ]
        if declared in OFFICIAL_CLASS:
            forecasts = self._forecasts(namespace, item["fixture_key"])
            if forecasts:
                notes.append(
                    {
                        **base,
                        "notification_id": f"{event_id}:forecast_resolvable",
                        "kind": "forecast_resolvable",
                        "fixture_key": item["fixture_key"],
                        "forecasts": forecasts,
                        "cites": cites,
                        "note": "an official result exists; resolution stays a reviewed ledger decision",
                    }
                )
        return notes, 0

    def _forecasts(self, namespace, fixture_key) -> list[dict[str, str]]:
        if not table_exists(self.conn, "sports_forecast_rules"):
            return []
        season = (self.store.body(namespace, "fixture", fixture_key) or {}).get(
            "season_key"
        )
        out = []
        for forecast_namespace, forecast_id, rule in self.conn.execute(
            "SELECT forecast_namespace, forecast_id, rule_json FROM sports_forecast_rules WHERE namespace=? "
            "ORDER BY forecast_namespace, forecast_id",
            [namespace],
        ).fetchall():
            rule = json.loads(rule)
            if rule.get("fixture_key") == fixture_key or (
                season and rule.get("season_key") == season
            ):
                out.append(
                    {
                        "forecast_namespace": forecast_namespace,
                        "forecast_id": forecast_id,
                    }
                )
        return out

    def poll(self, subscription_id, *, principal_id, scopes, cursor="") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )


__all__ = ["EVENT_KINDS", "SportsMonitor", "WATCH_KINDS"]
