"""Watch issuers, holders, managers and the warning list for new or corrected BaFin notices (#2106, BF10).

A BaFin notice monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`): its query names what is
watched, so there is no monitor table and no scheduler. Each evaluation runs at
a committed watermark and compares the notices in view with the previous
evaluation. The first evaluation is a baseline (``in_view``), and replaying a
watermark or an unchanged refresh produces nothing.

Events: a new or corrected voting-rights notification, a threshold crossing
(the notifier's thresholds reached differ from its previous notification), a
new managers' transaction, a new or ended net short position, a new warning or
measure naming a watched string or a reviewed match, a warning removed by the
source, and a change of an authorised entity's licences. Every alert carries a
receipt that cites the notice revision (id and record hash) and can be checked
against the store with :func:`verify_receipt`. Alerts describe source changes
only; they carry no advice or signal.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.domains.market.bafin_notices import (
    READ_SCOPE,
    THRESHOLDS,
    BafinError,
    BafinNoticeStore,
    authorize,
    correction_chains,
    digest,
    normalize_isin,
    party_key,
)

CONTRACT = "noesis-bafin-notice-alert-v1"
RECEIPT_CONTRACT = "noesis-bafin-alert-receipt-v1"
WATCH_KINDS = ("issuer", "holder", "manager", "warning-list")


def _reached(percentages: Mapping[str, Any]) -> dict[str, list[str]]:
    return {
        basis: [
            t
            for t in THRESHOLDS[basis]
            if percentages.get(basis) is not None
            and Decimal(percentages[basis]) >= Decimal(t)
        ]
        for basis in ("s33", "s38", "s39")
    }


def verify_receipt(conn: Any, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Check a caller-supplied alert receipt against the store: the revision exists with the cited hash."""
    if receipt.get("contract") != RECEIPT_CONTRACT:
        raise BafinError("invalid_request", "not a BaFin alert receipt")
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    if digest(body) != receipt.get("receipt_hash"):
        return {"valid": False, "reason": "receipt hash does not match its content"}
    try:
        stored = BafinNoticeStore(conn, initialize=False).revision(
            receipt["namespace"], receipt["revision_id"]
        )
    except BafinError as exc:
        return {"valid": False, "reason": exc.code}
    if stored["notice_id"] != receipt.get("notice_id") or stored[
        "record_hash"
    ] != receipt.get("record_hash"):
        return {
            "valid": False,
            "reason": "the cited revision differs from the stored one",
        }
    return {"valid": True, "revision": stored}


class BafinNoticeMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = BafinNoticeStore(conn, initialize=False, now=now)
        self.now = self.store.now
        self.initialize = initialize
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        watch: str,
        principal_id: str,
        scopes: Iterable[str],
        isin: str | None = None,
        name: str | None = None,
        person: str | None = None,
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS:
            raise BafinError("invalid_watch", f"watch one of {WATCH_KINDS}")
        query: dict[str, Any] = {
            "operation": "search",
            "kind": "bafin-notice-monitor",
            "watch": watch,
        }
        if watch == "issuer":
            if not isin:
                raise BafinError("invalid_watch", "an issuer is watched by ISIN")
            query["isin"] = normalize_isin(isin)
        elif watch == "holder":
            if not name:
                raise BafinError(
                    "invalid_watch", "a holder or entity is watched by name"
                )
            query.update({"name": name.strip(), "party": party_key(name)})
        elif watch == "manager":
            if not (isin and person):
                raise BafinError(
                    "invalid_watch",
                    "a manager is watched within an issuer (ISIN and person)",
                )
            query.update(
                {
                    "isin": normalize_isin(isin),
                    "person": party_key(person, kind="natural_person"),
                }
            )
        elif name:
            query.update({"name": name.strip(), "party": party_key(name)})
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "market",
                "query": query,
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "bafin-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "source-pack runs and the maintenance orchestrator commit the watermarks this "
            "monitor evaluates; there is no separate scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE "
            "table_name='knowledge_subscriptions'"
        ).fetchone():
            raise BafinError(
                "not_ready", "no BaFin notice monitor has been created yet"
            )
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "bafin-notice-monitor":
            raise BafinError(
                "monitor_not_found", "subscription is not a BaFin notice monitor"
            )
        return subscription

    # -------------------------------------------------------------- snapshot

    def snapshot(self, subscription: Mapping[str, Any]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        self.store.require_ready()
        watch = query["watch"]
        visible = self.store.visible(namespace)
        views = visible["notices"]
        chains = correction_chains(
            [
                v
                for v in views
                if v["notice"]["kind"]
                in {"voting_rights_notification", "managers_transaction"}
            ]
        )
        reviewed = set()
        if watch in {"holder", "warning-list"} and query.get("party"):
            from src.domains.market.bafin_identity import (
                BafinIdentity,
                organisation_key,
            )

            links = BafinIdentity(self.conn, initialize=False).accepted_links(namespace)
            reviewed = {
                k
                for k in links.get(organisation_key(query.get("name")), [])
                if k.startswith("bafin:named:")
            }
        # The notifier's previous notification, for threshold crossings.
        by_notifier: dict[tuple, list[Mapping[str, Any]]] = {}
        for view in views:
            notice = view["notice"]
            if (
                notice["kind"] == "voting_rights_notification"
                and chains[view["notice_id"]]["superseded_by"] is None
            ):
                key = (
                    notice["issuer"].get("isin"),
                    party_key(notice["notifier"]["name"]),
                )
                by_notifier.setdefault(key, []).append(view)
        items = []
        for view in views:
            notice = view["notice"]
            kind = notice["kind"]
            isin = (notice.get("issuer") or {}).get("isin")
            names = set()
            if kind == "voting_rights_notification":
                names = {party_key(notice["notifier"]["name"])} | {
                    party_key(m["name"]) for m in notice.get("chain") or []
                }
            elif kind == "net_short_position":
                names = {party_key(notice["holder"]["name"])}
            elif kind in {"bafin_warning", "bafin_measure"}:
                names = {party_key(n) for n in notice.get("named_entities") or []}
            elif kind == "authorised_entity":
                names = {party_key(notice["name"])}
            from src.domains.market.bafin_identity import named_key

            named = {named_key(n) for n in notice.get("named_entities") or []}
            if watch == "issuer":
                wanted = isin == query["isin"] and kind in {
                    "voting_rights_notification",
                    "managers_transaction",
                    "net_short_position",
                }
            elif watch == "holder":
                wanted = query["party"] in names or bool(reviewed & named)
            elif watch == "manager":
                person = notice.get("person") or {}
                wanted = (
                    kind == "managers_transaction"
                    and isin == query["isin"]
                    and not person.get("withdrawn")
                    and party_key(person.get("name"), kind="natural_person")
                    == query["person"]
                )
            else:
                wanted = kind in {"bafin_warning", "bafin_measure"} and (
                    not query.get("party")
                    or query["party"] in names
                    or bool(reviewed & named)
                )
            if not wanted:
                continue
            item = {
                "id": f"notice:{view['notice_id']}",
                "notice_id": view["notice_id"],
                "notice_kind": kind,
                "revision_id": view["revision_id"],
                "record_hash": view["record_hash"],
                "source": {
                    k: notice["source"].get(k) for k in ("provider", "source_id", "url")
                },
                "publication_date": notice.get("publication_date"),
                "listing": view["listing"]["state"],
                "withdrawn": bool(notice.get("withdrawn")),
            }
            if kind in {"voting_rights_notification", "managers_transaction"}:
                chain = chains[view["notice_id"]]
                item["corrects"] = chain["members"][
                    : chain["members"].index(view["notice_id"])
                ]
            if kind == "voting_rights_notification":
                item["thresholds_reached"] = _reached(notice["percentages"])
                earlier = sorted(
                    (
                        v
                        for v in by_notifier.get(
                            (isin, party_key(notice["notifier"]["name"])), []
                        )
                        if (v["notice"].get("publication_date") or "")
                        < (notice.get("publication_date") or "")
                        and v["notice_id"] not in item["corrects"]
                    ),
                    key=lambda v: (
                        v["notice"].get("publication_date") or "",
                        v["notice_id"],
                    ),
                )
                item["previous"] = (
                    {
                        "notice_id": earlier[-1]["notice_id"],
                        "thresholds_reached": _reached(
                            earlier[-1]["notice"]["percentages"]
                        ),
                    }
                    if earlier
                    else None
                )
                item["notifier"] = notice["notifier"]["name"]
            elif kind == "net_short_position":
                item.update(
                    {
                        "holder": notice["holder"]["name"],
                        "position_pct": notice["position_pct"],
                        "position_date": notice["position_date"],
                        "publication_ended": bool(notice.get("publication_ended")),
                    }
                )
            elif kind in {"bafin_warning", "bafin_measure"}:
                item.update(
                    {
                        "title": notice["title"],
                        "named_entities": notice.get("named_entities") or [],
                        "reviewed_match": bool(reviewed & named),
                    }
                )
            elif kind == "authorised_entity":
                item.update(
                    {
                        "bafin_id": notice["bafin_id"],
                        "licences": notice.get("licences") or [],
                    }
                )
            elif kind == "managers_transaction":
                item.update(
                    {
                        "nature": notice["nature"],
                        "transaction_date": notice.get("transaction_date"),
                    }
                )
            items.append(item)
        return {
            "items": items,
            "coverage": {
                "complete": not visible["unreadable"],
                "unreadable": len(visible["unreadable"]),
            },
        }

    # -------------------------------------------------------------- evaluation

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
                raise BafinError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the "
                    "maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        baseline = subscription.get("last_watermark") is None
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            self.snapshot(subscription),
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=self.now(),
        )
        evaluation = int(
            self.conn.execute(
                "SELECT count(*) FROM knowledge_subscription_snapshots WHERE subscription_id=? AND watermark<=?",
                [subscription_id, watermark],
            ).fetchone()[0]
        )
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM "
                "knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(
                self._classify(
                    namespace,
                    subscription_id,
                    watermark,
                    evaluation,
                    event_id,
                    *row,
                    baseline=baseline,
                )
            )
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "evaluation": evaluation,
            "baseline": baseline,
            "notifications": notifications,
            "delivery": subscription["delivery"],
        }

    @staticmethod
    def _classify(
        namespace,
        subscription_id,
        watermark,
        evaluation,
        event_id,
        event_type,
        key,
        before,
        after,
        *,
        baseline: bool,
    ) -> list[dict[str, Any]]:
        before_item = json.loads(before) if before else None
        after_item = json.loads(after) if after else None
        item = after_item or before_item or {}
        if not item.get("notice_id"):
            return []

        def note(kind: str, message: str, **extra: Any) -> dict[str, Any]:
            receipt = {
                "contract": RECEIPT_CONTRACT,
                "namespace": namespace,
                "subscription_id": subscription_id,
                "evaluation": evaluation,
                "watermark": watermark,
                "event_id": event_id,
                "notice_id": item["notice_id"],
                "revision_id": item["revision_id"],
                "record_hash": item["record_hash"],
                "source": item["source"],
                "publication_date": item.get("publication_date"),
            }
            receipt["receipt_hash"] = digest(receipt)
            return {
                "contract": CONTRACT,
                "notification_id": f"{event_id}:{kind}",
                "event_id": event_id,
                "kind": "in_view" if baseline else kind,
                "object": key,
                "notice_kind": item["notice_kind"],
                "message": message,
                "receipt": receipt,
                **extra,
            }

        kind = item["notice_kind"]
        label = item["source"].get("source_id")
        if event_type == "removed":
            return []
        if event_type == "added":
            if kind == "voting_rights_notification":
                notes = [
                    note(
                        "corrected_notification"
                        if item.get("corrects")
                        else "new_notification",
                        f"Voting-rights notification {label} published {item.get('publication_date')}"
                        + (
                            " (corrects an earlier notification)"
                            if item.get("corrects")
                            else ""
                        )
                        + ".",
                        corrects=item.get("corrects") or [],
                    )
                ]
                previous = item.get("previous")
                if (
                    previous
                    and previous["thresholds_reached"] != item["thresholds_reached"]
                ):
                    notes.append(
                        note(
                            "threshold_crossing",
                            f"{item['notifier']}: thresholds reached changed.",
                            previous_notice=previous["notice_id"],
                            thresholds={
                                "before": previous["thresholds_reached"],
                                "after": item["thresholds_reached"],
                            },
                        )
                    )
                return notes
            if kind == "managers_transaction":
                return [
                    note(
                        "corrected_managers_transaction"
                        if item.get("corrects")
                        else "new_managers_transaction",
                        f"Managers' transaction {label} ({item['nature']}) published "
                        f"{item.get('publication_date')}.",
                        corrects=item.get("corrects") or [],
                    )
                ]
            if kind == "net_short_position":
                if item.get("publication_ended"):
                    return [
                        note(
                            "short_position_ended",
                            f"{item['holder']}: published position below 0.5 % on "
                            f"{item['position_date']} (below publication threshold or "
                            "closed).",
                        )
                    ]
                return [
                    note(
                        "new_short_position",
                        f"{item['holder']}: {item['position_pct']} % on "
                        f"{item['position_date']}.",
                    )
                ]
            if kind in {"bafin_warning", "bafin_measure"}:
                return [
                    note(
                        "new_warning" if kind == "bafin_warning" else "new_measure",
                        item["title"],
                        reviewed_match=item.get("reviewed_match", False),
                    )
                ]
            if kind == "authorised_entity":
                return [
                    note(
                        "authorised_entity_in_view",
                        f"BaFin ID {item['bafin_id']} is listed.",
                    )
                ]
            return []
        # changed / corrected
        notes = []
        if (
            before_item
            and before_item.get("listing") != item.get("listing")
            and item.get("listing") == "no_longer_listed"
        ):
            ended = {
                "net_short_position": "short_position_ended",
                "bafin_warning": "warning_removed",
                "bafin_measure": "measure_removed",
            }.get(kind, "no_longer_listed")
            notes.append(note(ended, f"{label}: no longer listed by the source."))
        if before_item and before_item.get("revision_id") != item.get("revision_id"):
            if kind == "authorised_entity":
                notes.append(
                    note(
                        "authorisation_change",
                        f"BaFin ID {item['bafin_id']}: licences changed.",
                        licences={
                            "before": before_item.get("licences"),
                            "after": item.get("licences"),
                        },
                    )
                )
            else:
                notes.append(
                    note("revised_notice", f"{label}: the source revised the notice.")
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
        self._subscription(subscription_id, principal_id, scopes)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )
