"""Monitor supplementary budgets, outturn vintages, payment publications and audit findings (#1909, B09).

A public-finance monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names what is
watched - a budget line, a programme or a beneficiary - so there is no watcher
table and no scheduler. Refresh follows the ``economic-statistics-and-filings``
source-pack schedule; each evaluation runs at a committed source-pack watermark
and the subscription store turns new items into events delivered through its
poll and outbox paths. Replaying the same run (or evaluating the same
watermark after a restart) emits no duplicate event.

Items and their notifications:

* a plan or supplementary-plan revision of a watched line -> ``plan_revision`` /
  ``supplementary_plan``;
* an outturn release stating a watched line's figure -> ``outturn_vintage``
  (every vintage, including one that repeats the previous figure);
* a published payment row (or its revision) of a watched programme or
  beneficiary -> ``payment_publication``;
* an audit finding (or its revision) citing a watched line -> ``audit_finding``.

Each notification names the source revision, the record kind and the prior
revision it supersedes, and states no conclusion about the change. A release
acquired *live* from a provider whose access decision is still
``unverified-live`` is withheld until a dated live run verifies the provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.public_finance_sources import unverified
from src.kb.public_finance import (
    READ_SCOPE,
    PublicFinanceError,
    PublicFinanceStore,
    authorize,
)

CONTRACT = "noesis-public-finance-notification-v1"
WATCH_KINDS = ("budget_line", "programme", "beneficiary")
MESSAGES = {
    "plan_revision": "A plan figure was published",
    "supplementary_plan": "A supplementary-plan figure was published",
    "outturn_vintage": "An outturn vintage was published",
    "payment_publication": "A payment record was published",
    "audit_finding": "An audit finding citing the line was recorded",
}


def notifiable(source_revision: Mapping[str, Any]) -> bool:
    """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
    return source_revision["evidence_origin"] != "live" or not unverified(
        source_revision["provider"]
    )


class PublicFinanceMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = PublicFinanceStore(conn, initialize=initialize, now=now)
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
            raise PublicFinanceError(
                "invalid_watch", f"watch one of {WATCH_KINDS} with a key"
            )
        if watch == "budget_line":
            self.store.line(namespace, key)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "economic",
                "query": {
                    "operation": "search",
                    "kind": "public-finance-monitor",
                    "watch": watch,
                    "key": str(key),
                },
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "public-finance-monitor:" + request_key,
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
        if subscription["query"].get("kind") != "public-finance-monitor":
            raise PublicFinanceError(
                "monitor_not_found", "subscription is not a public-finance monitor"
            )
        return subscription

    def _line_items(self, namespace: str, line_id: str) -> list[dict[str, Any]]:
        items = []
        for series_key in self.store.series_for_line(namespace, line_id):
            series = self.store.figure_series(namespace, series_key)
            kind = series["revisions"][0]["figure_kind"]
            if kind == "outturn":
                previous = None
                for vintage in series["vintages"]:
                    revision = vintage["source_revision"]
                    items.append(
                        {
                            "id": f"vintage:{revision['release_id']}:{series_key}",
                            "item": "outturn_vintage",
                            "record_kind": "outturn",
                            "line_id": line_id,
                            "fiscal_year": series["revisions"][0]["fiscal_year"],
                            "figure_id": vintage["figure_id"],
                            "amount_text": vintage["amount_text"],
                            "unit": vintage["unit"],
                            "source_revision": revision,
                            "supersedes": previous,
                        }
                    )
                    previous = {
                        "release_id": revision["release_id"],
                        "figure_id": vintage["figure_id"],
                        "published_on": revision["published_on"],
                    }
                continue
            for revision in series["revisions"]:
                items.append(
                    {
                        "id": f"figure:{revision['figure_id']}",
                        "item": "supplementary_plan"
                        if kind == "supplementary_plan"
                        else "plan_revision",
                        "record_kind": kind,
                        "line_id": line_id,
                        "fiscal_year": revision["fiscal_year"],
                        "plan_key": revision["plan_key"],
                        "figure_id": revision["figure_id"],
                        "amount_text": revision["amount_text"],
                        "unit": revision["unit"],
                        "source_revision": revision["source_revision"],
                        "supersedes": None
                        if revision["previous_figure_id"] is None
                        else {"figure_id": revision["previous_figure_id"]},
                    }
                )
        for finding in self.store.findings(
            namespace, line_id=line_id, current_only=False
        ):
            items.append(
                {
                    "id": f"finding:{finding['finding_id']}",
                    "item": "audit_finding",
                    "record_kind": "finding",
                    "line_id": line_id,
                    "finding_id": finding["finding_id"],
                    "report_id": finding["report_id"],
                    "passage": finding["passage"],
                    "source_revision": finding["source_revision"],
                    "supersedes": None
                    if finding["previous_finding_id"] is None
                    else {"finding_id": finding["previous_finding_id"]},
                }
            )
        return items

    def _payment_items(
        self,
        namespace: str,
        *,
        programme: str | None = None,
        beneficiary_key: str | None = None,
    ) -> list[dict[str, Any]]:
        items = []
        for current in self.store.payments(
            namespace, programme=programme, beneficiary_key=beneficiary_key
        ):
            for payment in self.store.payment_history(namespace, current["series_key"]):
                items.append(
                    {
                        "id": f"payment:{payment['payment_id']}",
                        "item": "payment_publication",
                        "record_kind": "payment",
                        "payment_id": payment["payment_id"],
                        "payment_kind": payment["payment_kind"],
                        "beneficiary": payment["beneficiary"],
                        "programme": payment["programme"],
                        "fiscal_year": payment["fiscal_year"],
                        "amount_text": payment["amount_text"],
                        "currency": payment["currency"],
                        "source_revision": payment["source_revision"],
                        "supersedes": None
                        if payment["previous_payment_id"] is None
                        else {"payment_id": payment["previous_payment_id"]},
                    }
                )
        return items

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        namespace, query = subscription["namespace"], subscription["query"]
        if query["watch"] == "budget_line":
            items = self._line_items(namespace, query["key"])
        elif query["watch"] == "programme":
            items = self._payment_items(namespace, programme=query["key"])
        else:
            items = self._payment_items(namespace, beneficiary_key=query["key"])
        kept = [i for i in items if notifiable(i["source_revision"])]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

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
                raise PublicFinanceError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        result, withheld = self.snapshot(subscription)
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
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
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
                "record_kind": item["record_kind"],
                "message": f"{MESSAGES[kind]} ({revision['provider']}, {revision['published_on']}).",
                "source_revision": revision,
                "supersedes": item.get("supersedes"),
                "item": {
                    k: v
                    for k, v in item.items()
                    if k not in {"source_revision", "supersedes", "id", "item"}
                },
                "cites": {"source_revision": revision},
                "note": "a new publication is reported as published; nothing is concluded about the change",
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
