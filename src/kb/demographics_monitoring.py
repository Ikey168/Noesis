"""Monitor new releases, vintage revisions, definition revisions and breaks through subscriptions (#1914, M09).

A demographic monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`, ``platform.subscriptions``)
whose query carries a series filter - publisher, dataset or table code,
geography code and definition identity - so there is no watcher table and no
scheduler. Each evaluation runs at a committed source-pack watermark and the
subscription store turns new items into events delivered through its poll and
outbox paths; evaluating the same state again (a replay, an idempotent
re-ingestion or a restart) emits nothing.

Items, following the revision comparison of :mod:`src.kb.environment_vintages`:

* ``new_release`` - the first vintage of a watched series, or a later release that
  repeats every value;
* ``vintage_revision`` - a release that changes values, with the changed periods
  and the previous and new vintage;
* ``definition_revision`` - a new definition revision, citing the previous one;
* ``series_break`` - a marked break (publisher flag, definition or census base).

A revision of a pinned vintage is reported as a ``stale`` pin; the pinned values
are never replaced. A documented release calendar is expected-release evidence
only: an overdue release is reported as ``release_pending``, never predicted.
Live releases from providers still ``unverified-live`` are withheld.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from src.ingestion.demographic_sources import unverified
from src.kb.demographics import (
    READ_SCOPE,
    DemographicError,
    DemographicStore,
    authorize,
)

CONTRACT = "noesis-demographic-notification-v1"
FILTER_KEYS = ("provider", "series_code", "geography_code", "definition_key")
MESSAGES = {
    "new_release": "A new release of the series was published",
    "vintage_revision": "A release revised values of the series",
    "definition_revision": "The publisher's definition of the series changed",
    "series_break": "A break in the series was marked",
}


def notifiable(source_revision: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return source_revision["evidence_origin"] != "live" or not unverified(
        source_revision["provider"]
    )


class DemographicMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = DemographicStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        series_filter: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = {
            k: str(v)
            for k, v in dict(series_filter or {}).items()
            if v not in (None, "")
        }
        if not wanted or set(wanted) - set(FILTER_KEYS):
            raise DemographicError(
                "invalid_watch", f"a demographic filter uses {FILTER_KEYS}"
            )
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "economic",
                "query": {
                    "operation": "search",
                    "kind": "demographic-monitor",
                    "filter": wanted,
                },
                "filters": {"watch": "demographic-series"},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "demographic-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "the economic-statistics-and-filings source-pack schedule and the maintenance orchestrator "
            "commit the watermarks this monitor evaluates; no new scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "demographic-monitor":
            raise DemographicError(
                "monitor_not_found", "subscription is not a demographic monitor"
            )
        return subscription

    def _series(
        self, namespace: str, wanted: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        return self.store.find_series(
            namespace, **{k: wanted.get(k) for k in FILTER_KEYS}
        )

    def _items(self, namespace: str, series: Mapping[str, Any]) -> list[dict[str, Any]]:
        items = []
        previous = None
        previous_values: dict[str, Any] = {}
        for vintage in self.store.vintage_rows(namespace, series["series_id"]):
            observations = {
                o["period"]: (o["value"], o["flags"])
                for o in self.store.observations(namespace, vintage["vintage_id"])
            }
            changed = sorted(
                p
                for p in set(observations) | set(previous_values)
                if observations.get(p) != previous_values.get(p)
            )
            kind = (
                "vintage_revision"
                if previous is not None and changed
                else "new_release"
            )
            items.append(
                {
                    "id": f"vintage:{vintage['vintage_id']}",
                    "item": kind,
                    "series_id": series["series_id"],
                    "vintage_id": vintage["vintage_id"],
                    "previous_vintage_id": None
                    if previous is None
                    else previous["vintage_id"],
                    "changed_periods": changed if previous is not None else [],
                    "source_revision": self.store.source_revision(
                        namespace, vintage["release_id"]
                    ),
                }
            )
            previous, previous_values = vintage, observations
        history = self.store.definition_history(namespace, series["definition_key"])
        for before, after in zip(history, history[1:]):
            items.append(
                {
                    "id": f"definition:{after['definition_id']}:{series['series_id']}",
                    "item": "definition_revision",
                    "series_id": series["series_id"],
                    "definition_id": after["definition_id"],
                    "previous_definition_id": before["definition_id"],
                    "changed_fields": sorted(
                        k
                        for k in set(before["content"]) | set(after["content"])
                        if before["content"].get(k) != after["content"].get(k)
                    ),
                    "source_revision": after["source_revision"],
                }
            )
        for item in series["breaks"]:
            vintage = self.store.vintage(namespace, item["first_vintage_id"])
            items.append(
                {
                    "id": f"break:{item['break_id']}",
                    "item": "series_break",
                    "series_id": series["series_id"],
                    "break_kind": item["kind"],
                    "period": item["period"],
                    "from_definition_id": item["from_definition_id"],
                    "to_definition_id": item["to_definition_id"],
                    "vintage_id": item["first_vintage_id"],
                    "source_revision": vintage["source_revision"],
                }
            )
        return items

    def pending_releases(
        self, namespace: str, series: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Expected releases (from the publisher's calendar, as declared) that are overdue; nothing predicted."""
        today = (
            datetime.fromtimestamp(self.now() / 1000, tz=timezone.utc)
            .date()
            .isoformat()
        )
        vintages = self.store.vintage_rows(namespace, series["series_id"])
        if not vintages:
            return []
        latest = self.store.release(namespace, vintages[-1]["release_id"])
        expected = dict(latest["document"].get("expected_release") or {})
        on = expected.get("on")
        if not on or on > today or latest["published_on"] >= on:
            return []
        return [
            {
                "series_id": series["series_id"],
                "status": "release_pending",
                "expected_on": on,
                "calendar_url": expected.get("calendar_url"),
                "latest_release": self.store.source_revision(
                    namespace, latest["release_id"]
                ),
                "note": "the documented calendar date has passed without a release; nothing is predicted",
            }
        ]

    def snapshot(
        self, subscription: Mapping[str, Any]
    ) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
        namespace, wanted = subscription["namespace"], subscription["query"]["filter"]
        items, pending = [], []
        for series in self._series(namespace, wanted):
            items += self._items(namespace, series)
            pending += self.pending_releases(namespace, series)
        kept = [i for i in items if notifiable(i["source_revision"])]
        return (
            {"items": kept, "coverage": {"complete": True}},
            len(items) - len(kept),
            pending,
        )

    def run(
        self,
        subscription_id: str,
        watermark: int | None = None,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        from src.kb.demographics_places import DemographicPlaces

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
                raise DemographicError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        result, withheld, pending = self.snapshot(subscription)
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
            notifications.extend(self._classify(event_id, *row))
        watched = {
            s["series_id"]
            for s in self._series(namespace, subscription["query"]["filter"])
        }
        stale = [
            {
                k: p[k]
                for k in (
                    "pin_id",
                    "series_id",
                    "vintage_id",
                    "newer_vintage_ids",
                    "status",
                )
            }
            for p in DemographicPlaces(self.conn, initialize=False).pins(
                namespace, scopes={"operator"}
            )["pins"]
            if p["series_id"] in watched and p["status"] == "stale"
        ]
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
            "release_pending": pending,
            "stale_pins": stale,
            "withheld_unverified_live_items": withheld,
            "delivery": subscription["delivery"],
        }

    @staticmethod
    def _classify(
        event_id: str, event_type: str, key: str, after: str | None
    ) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []
        item = json.loads(after)
        kind = item["item"]
        revision = item["source_revision"]
        return [
            {
                "contract": CONTRACT,
                "event_id": event_id,
                "notification_id": f"{event_id}:{kind}",
                "object": key,
                "kind": kind,
                "message": f"{MESSAGES[kind]} ({revision['provider']}, {revision['published_on']}).",
                "source_revision": revision,
                "item": {
                    k: v
                    for k, v in item.items()
                    if k not in {"source_revision", "id", "item"}
                },
                "note": "a publication is reported as published; nothing is concluded about the change",
            }
        ]

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
