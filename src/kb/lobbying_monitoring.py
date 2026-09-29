"""Monitor registrations, deregistrations, spend and client revisions and meetings through subscriptions (#1911, T09).

A lobbying monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`); its query names what is
watched - a dossier, a registrant, a client (by its register string or a
reviewed entity) or an office holder - so there is no monitor table and no
scheduler. Refresh follows the source-pack schedule of
``official-political-records``; each evaluation runs at a committed watermark
and the subscription store turns differences into events delivered through its
existing poll and outbox paths.

Events describe what a register filed: ``registration``, ``deregistration``,
``reregistration``, ``spend_range_revised`` (the old and the new range, never a
difference of point values), ``clients_revised`` (names added and removed as
filed), ``revision`` for other changes and ``meeting_declared``. Each cites the
new and the previous register revision. Items are per register record, so
declarations from different registers about one organisation are delivered
separately with their own sources.

A revision acquired *live* from a source whose access decision is still
``unverified-live`` is withheld: it produces no notification until the source
has a dated live run and its decision is updated. Offline fixture replays are
notified and marked as fixture evidence.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.lobbying_sources import PROVIDER_CONTRACTS
from src.kb.lobbying import (
    READ_SCOPE,
    LobbyingError,
    LobbyingStore,
    authorize,
    client_key,
    normalize_name,
)

CONTRACT = "noesis-lobbying-notification-v1"
WATCH_KINDS = ("dossier", "registrant", "client", "official")
_DECISIONS = {
    c["register"]: c["access_decision"]
    for c in PROVIDER_CONTRACTS.values()
    if c.get("register")
}


def notifiable(revision: Mapping[str, Any]) -> bool:
    """Fixture evidence, or live evidence from a source whose live access is verified."""
    return (
        revision["evidence_origin"] == "fixture"
        or _DECISIONS.get(revision["register"]) == "verified-live"
    )


class LobbyingMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = LobbyingStore(conn, initialize=initialize, now=now)
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
        dossier_namespace: str | None = None,
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS or not str(key or "").strip():
            raise LobbyingError(
                "invalid_watch", f"watch one of {WATCH_KINDS} with a key"
            )
        query: dict[str, Any] = {
            "operation": "search",
            "kind": "lobbying-monitor",
            "watch": watch,
            "key": str(key).strip(),
        }
        if watch == "registrant":
            self.store.entry(
                namespace, query["key"]
            )  # an entry id: one register record
        elif watch == "dossier":
            if not dossier_namespace:
                raise LobbyingError(
                    "invalid_watch", "a dossier is watched with its dossier namespace"
                )
            query["dossier_namespace"] = dossier_namespace
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "political",
                "query": query,
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "lobbying-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "the official-political-records source-pack schedule and the maintenance "
            "orchestrator commit the watermarks this monitor evaluates; no new scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "lobbying-monitor":
            raise LobbyingError(
                "monitor_not_found", "subscription is not a lobbying monitor"
            )
        return subscription

    def _latest_notifiable(
        self, namespace: str, entry_id: str
    ) -> tuple[dict[str, Any] | None, int]:
        """The latest notifiable revision by the register's dates, and how many later ones are withheld."""
        withheld = 0
        for revision in reversed(self.store.history(namespace, entry_id)):
            if notifiable(revision):
                return revision, withheld
            withheld += 1
        return None, withheld

    def _item(self, namespace: str, entry_id: str) -> tuple[dict[str, Any] | None, int]:
        revision, withheld = self._latest_notifiable(namespace, entry_id)
        if revision is None:
            return None, withheld
        statement = revision["statement"] or {}
        entry = self.store.entry(namespace, entry_id)
        item = {
            "id": f"entry:{entry_id}",
            "kind": entry["entry_kind"],
            "register": entry["register"],
            "native_id": entry["native_id"],
            "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"],
            "lifecycle": revision["lifecycle"],
            "change": revision["change"],
            "effective_on": revision["effective_on"],
            "evidence_origin": revision["evidence_origin"],
            "source_revision": revision["source_revision"],
        }
        if entry["entry_kind"] == "meeting":
            item.update(
                {
                    "date": statement.get("date"),
                    "official": statement.get("official"),
                    "subject": statement.get("subject"),
                }
            )
        else:
            item.update(
                {
                    "name": statement.get("name"),
                    "spend": sorted(
                        (
                            {
                                k: s.get(k)
                                for k in (
                                    "kind",
                                    "lower",
                                    "upper",
                                    "currency",
                                    "period",
                                    "party",
                                )
                            }
                            for s in statement.get("spend") or []
                        ),
                        key=json.dumps,
                    ),
                    "clients": sorted(
                        {c.get("name") or "" for c in statement.get("clients") or []}
                    ),
                }
            )
        return item, withheld

    def _entries(self, subscription: Mapping[str, Any], scopes: set[str]) -> list[str]:
        namespace, query = subscription["namespace"], subscription["query"]
        if query["watch"] == "registrant":
            return [query["key"]]
        if query["watch"] == "official":
            return sorted(
                {
                    entry_id
                    for entry_id, statement in self.conn.execute(
                        "SELECT entry_id, statement_json FROM lobbying_revisions WHERE namespace=? AND "
                        "statement_json IS NOT NULL",
                        [namespace],
                    ).fetchall()
                    if dict(json.loads(statement).get("official") or {}).get("id")
                    == query["key"]
                }
            )
        if query["watch"] == "client":
            from src.kb.lobbying_identity import LobbyingIdentity

            wanted = normalize_name(query["key"])
            keys = {query["key"]}
            for candidate in LobbyingIdentity(self.conn, initialize=False).candidates(
                namespace, scopes=scopes
            ):
                if (
                    candidate["state"] == "accepted"
                    and query["key"] in candidate["entities"] + candidate["records"]
                ):
                    keys |= set(candidate["records"])
            found = set()
            for entry_id, register, native, statement in self.conn.execute(
                "SELECT r.entry_id, e.register, e.native_id, r.statement_json FROM lobbying_revisions r JOIN "
                "lobbying_entries e ON e.namespace=r.namespace AND e.entry_id=r.entry_id WHERE r.namespace=? "
                "AND r.statement_json IS NOT NULL",
                [namespace],
            ).fetchall():
                for client in json.loads(statement).get("clients") or []:
                    if (
                        normalize_name(client.get("name")) == wanted
                        or client_key(register, native, client.get("name")) in keys
                    ):
                        found.add(entry_id)
            return sorted(found)
        from src.kb.lobbying_links import LobbyingDossierLinks

        links = LobbyingDossierLinks(self.conn, initialize=False).links(
            namespace,
            query["dossier_namespace"],
            query["key"],
            scopes=scopes,
            states=("linked", "accepted"),
        )
        return sorted({link["entry_id"] for link in links})

    def snapshot(
        self, subscription: Mapping[str, Any], scopes: set[str]
    ) -> tuple[dict[str, Any], int]:
        items, withheld = [], 0
        for entry_id in self._entries(subscription, scopes):
            item, held = self._item(subscription["namespace"], entry_id)
            withheld += held
            if item:
                items.append(item)
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
                raise LobbyingError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the "
                    "maintenance orchestrator commit them",
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
                "SELECT event_type, object_key, before_json, after_json FROM "
                "knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(
                self._classify(event_id, *row, watch=subscription["query"]["watch"])
            )
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
            "withheld_unverified_live_revisions": withheld,
            "delivery": subscription["delivery"],
        }

    @staticmethod
    def _classify(
        event_id: str,
        event_type: str,
        key: str,
        before: str | None,
        after: str | None,
        *,
        watch: str = "registrant",
    ) -> list[dict[str, Any]]:
        old = json.loads(before) if before else None
        new = json.loads(after) if after else None
        if event_type == "removed" or new is None:
            return []

        def note(kind: str, message: str, **detail: Any) -> dict[str, Any]:
            return {
                "contract": CONTRACT,
                "notification_id": f"{event_id}:{kind}",
                "event_id": event_id,
                "kind": kind,
                "object": key,
                "register": new["register"],
                "native_id": new["native_id"],
                "message": message,
                "cites": {
                    "revision_id": new["revision_id"],
                    "previous_revision_id": old["revision_id"] if old else None,
                    "source_revision": new["source_revision"],
                },
                "evidence_origin": new["evidence_origin"],
                **detail,
            }

        label = f"{new['register']} {new['native_id']}"
        if old is not None and old["revision_id"] == new["revision_id"]:
            return []
        if new["kind"] == "meeting":
            if old is None:
                return [
                    note(
                        "meeting_declared",
                        f"{label}: meeting on {new['date']} declared.",
                    )
                ]
            return [
                note(
                    "revision",
                    f"{label}: the meeting declaration changed (revision {new['revision_no']}).",
                )
            ]
        if (
            old is None
            and watch in {"client", "dossier"}
            and new["change"] != "registered"
        ):
            # The record newly enters the watched set: it now declares the client or the dossier.
            kind = "client_declared" if watch == "client" else "interest_declared"
            return [
                note(
                    kind,
                    f"{label}: {kind.replace('_', ' ')} in revision {new['revision_no']}.",
                )
            ]
        if old is None:
            kind = (
                "deregistration"
                if new["lifecycle"] == "deregistered"
                else "registration"
            )
            return [
                note(kind, f"{label}: {kind} as filed (revision {new['revision_no']}).")
            ]
        notes = []
        if old["lifecycle"] != new["lifecycle"]:
            kind = (
                "deregistration"
                if new["lifecycle"] == "deregistered"
                else "reregistration"
            )
            notes.append(
                note(kind, f"{label}: {kind} (revision {new['revision_no']}).")
            )
        if new["lifecycle"] == "active":
            if old.get("spend") != new.get("spend"):
                notes.append(
                    note(
                        "spend_range_revised",
                        f"{label}: declared ranges revised as filed.",
                        old_ranges=old.get("spend"),
                        new_ranges=new.get("spend"),
                    )
                )
            if old.get("clients") != new.get("clients"):
                added = sorted(
                    set(new.get("clients") or []) - set(old.get("clients") or [])
                )
                removed = sorted(
                    set(old.get("clients") or []) - set(new.get("clients") or [])
                )
                notes.append(
                    note(
                        "clients_revised",
                        f"{label}: declared clients revised as filed.",
                        clients_added=added,
                        clients_removed=removed,
                    )
                )
        if not notes:
            notes.append(
                note(
                    "revision", f"{label}: new register revision {new['revision_no']}."
                )
            )
        return notes

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )
