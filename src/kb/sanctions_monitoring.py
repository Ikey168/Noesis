"""Monitor designations, programmes and control codes through ``platform.subscriptions`` (#1907, S09).

A sanctions monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`); its query names what is
watched - a per-list designation, a programme, or a control code - so there is
no monitor table and no scheduler. Each evaluation runs at a committed
watermark and compares the current list statements with the previous
evaluation; the subscription store turns differences into events and delivers
them through its existing poll/outbox paths. Re-evaluating the same watermark,
or a snapshot that changed nothing, produces no event.

Events describe the source change only - ``listed``, ``amended``, ``delisted``,
``relisted`` or ``new_edition`` - each citing the two snapshots (or the two
annex versions) compared. They carry no screening or compliance
interpretation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from src.kb.sanctions import (
    CONTROL_LISTS,
    READ_SCOPE,
    SanctionsError,
    SanctionsStore,
    authorize,
)

CONTRACT = "noesis-sanctions-notification-v1"
WATCH_KINDS = ("designation", "programme", "control-code")


class SanctionsMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = SanctionsStore(conn, initialize=initialize, now=now)
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
        list_id: str | None = None,
        control_list: str = "eu-dual-use",
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS or not str(key or "").strip():
            raise SanctionsError(
                "invalid_watch", f"watch one of {WATCH_KINDS} with a key"
            )
        query: dict[str, Any] = {
            "operation": "search",
            "kind": "sanctions-monitor",
            "watch": watch,
            "key": str(key).strip(),
        }
        if watch == "designation":
            self.store.designation(namespace, query["key"])  # a per-list designation id
        elif watch == "programme":
            if not list_id:
                raise SanctionsError(
                    "invalid_watch", "a programme is watched within one list"
                )
            query["list_id"] = list_id
        else:
            if control_list not in CONTROL_LISTS:
                raise SanctionsError(
                    "invalid_watch",
                    f"control list must be one of {sorted(CONTROL_LISTS)}",
                )
            query.update({"key": query["key"].upper(), "control_list": control_list})
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "legal",
                "query": query,
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "sanctions-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "source-pack runs and the maintenance orchestrator commit the watermarks "
            "this monitor evaluates",
        }

    def _query(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "sanctions-monitor":
            raise SanctionsError(
                "monitor_not_found", "subscription is not a sanctions monitor"
            )
        return subscription

    def _designation_item(
        self, namespace: str, designation_id: str
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT revision_id FROM sanctions_revisions WHERE namespace=? AND designation_id=? "
            "ORDER BY revision_no DESC LIMIT 1",
            [namespace, designation_id],
        ).fetchone()
        if row is None:
            return None
        revision = self.store.revision(namespace, row[0])
        designation = self.store.designation(namespace, designation_id)
        return {
            "id": f"designation:{designation_id}",
            "kind": "designation",
            "list_id": designation["list_id"],
            "list_entry_id": designation["list_entry_id"],
            "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"],
            "change": revision["change"],
            "listed": revision["change"] != "delisted",
            "compared_snapshots": revision["compared_snapshots"],
        }

    def snapshot(self, subscription: dict[str, Any]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        items = []
        if query["watch"] == "designation":
            item = self._designation_item(namespace, query["key"])
            items = [item] if item else []
        elif query["watch"] == "programme":
            # Every designation any revision placed in the programme, so a delisting stays visible as a change.
            rows = self.conn.execute(
                "SELECT designation_id, statement_json FROM sanctions_revisions WHERE namespace=? AND list_id=? "
                "AND statement_json IS NOT NULL ORDER BY designation_id, revision_no",
                [namespace, query["list_id"]],
            ).fetchall()
            members = sorted(
                {
                    d
                    for d, s in rows
                    if any(
                        p.get("code") == query["key"]
                        for p in json.loads(s).get("programmes") or []
                    )
                }
            )
            items = [
                i for i in (self._designation_item(namespace, d) for d in members) if i
            ]
        else:
            legal_namespace = str(query.get("legal_namespace") or namespace)
            self.store.sync_control_entries(
                namespace, query["control_list"], legal_namespace=legal_namespace
            )
            entries = self.store.control_entries(
                namespace,
                query["key"],
                query["control_list"],
                legal_namespace=legal_namespace,
            )
            if entries:
                latest = max(
                    entries, key=lambda e: (e["edition"] or "", e["version_id"])
                )
                items = [
                    {
                        "id": f"control:{query['control_list']}:{query['key']}",
                        "kind": "control-code",
                        "control_code": query["key"],
                        "version_id": latest["version_id"],
                        "edition": latest["edition"],
                        "text_sha256": latest["text_sha256"],
                        "locator": latest["locator"],
                    }
                ]
        return {"items": items, "coverage": {"complete": True}}

    def run(
        self,
        subscription_id: str,
        watermark: int | None = None,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._query(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [namespace],
            ).fetchone()
            if row is None or row[0] is None:
                raise SanctionsError(
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
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM "
                "knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
            "delivery": subscription["delivery"],
        }

    @staticmethod
    def _classify(
        event_id: str, event_type: str, key: str, before: str | None, after: str | None
    ) -> list[dict[str, Any]]:
        before_item = json.loads(before) if before else None
        after_item = json.loads(after) if after else None

        def note(kind: str, message: str, cites: dict[str, Any]) -> dict[str, Any]:
            return {
                "contract": CONTRACT,
                "notification_id": f"{event_id}:{kind}",
                "event_id": event_id,
                "kind": kind,
                "object": key,
                "message": message,
                "cites": cites,
            }

        item = after_item or before_item or {}
        if item.get("kind") == "control-code":
            if event_type == "added":
                return [
                    note(
                        "edition_in_view",
                        f"{item['control_code']} is stated in edition {item['edition']}.",
                        {"version_id": item["version_id"]},
                    )
                ]
            if (
                event_type == "changed"
                and before_item["version_id"] != after_item["version_id"]
            ):
                return [
                    note(
                        "new_edition",
                        f"{item['control_code']}: edition {after_item['edition']} follows "
                        f"{before_item['edition']}.",
                        {
                            "versions_compared": [
                                before_item["version_id"],
                                after_item["version_id"],
                            ],
                            "text_changed": before_item["text_sha256"]
                            != after_item["text_sha256"],
                        },
                    )
                ]
            return []
        if event_type == "removed" or item.get("kind") != "designation":
            return []
        change = after_item["change"]
        if event_type == "added" and change == "delisted":
            return []
        label = {
            "listed": "listed",
            "relisted": "relisted",
            "amended": "amended",
            "delisted": "delisted",
        }[change]
        if (
            event_type == "changed"
            and before_item["revision_id"] == after_item["revision_id"]
        ):
            return []
        return [
            note(
                label,
                f"{after_item['list_id'].upper()} {after_item['list_entry_id']}: {label} "
                f"(revision {after_item['revision_no']}).",
                {
                    "revision_id": after_item["revision_id"],
                    "snapshots_compared": after_item["compared_snapshots"],
                },
            )
        ]

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict[str, Any]:
        scopes = set(scopes)
        self._query(subscription_id, principal_id, scopes)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )
