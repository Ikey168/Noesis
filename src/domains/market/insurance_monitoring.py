"""Watch insurers, markets and events for new statistics, reports and loss-estimate revisions (#2230, IN10).

An insurance monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), and its query names what is
watched. There is no monitor table and no scheduler of its own: evaluations run
at the watermarks that source-pack runs commit, so the checks stay within the
IN01 run budgets (weekly at most for statistics and estimates, monthly for SFCRs).
The first evaluation is a baseline (``in_view``). A replayed watermark, or a
publication that has not changed, produces nothing.

Event kinds:

* ``new_vintage``: a new supervisory release, or a revised figure within one;
* ``new_insurer_report`` / ``corrected_insurer_report``: an SFCR that is new or republished;
* ``new_loss_estimate_revision``: a publisher's new estimate for a watched event;
* ``identity_match_change``: a candidate for a watched insurer was proposed, accepted, rejected or reverted.

Every notification carries a receipt citing the record revision (id, hash,
source URL and publication date), which :func:`verify_receipt` checks against
the store. Notifications describe publications only. They carry no solvency,
rating or loss-trend verdict.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.domains.market.insurance import (
    READ_SCOPE,
    InsuranceError,
    InsuranceLinks,
    InsuranceStore,
    authorize,
    digest,
    fold,
)

CONTRACT = "noesis-insurance-alert-v1"
RECEIPT_CONTRACT = "noesis-insurance-alert-receipt-v1"
WATCH_KINDS = ("insurer", "market", "event")


def verify_receipt(conn: Any, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Check an alert receipt against the store: the cited revision exists with the cited hash."""
    if receipt.get("contract") != RECEIPT_CONTRACT:
        raise InsuranceError("invalid_request", "not an insurance alert receipt")
    body = {k: v for k, v in receipt.items() if k != "receipt_hash"}
    if digest(body) != receipt.get("receipt_hash"):
        return {"valid": False, "reason": "receipt hash does not match its content"}
    if not receipt.get("revision_id"):
        return {"valid": True, "revision": None}
    try:
        stored = InsuranceStore(conn, initialize=False).revision(receipt["namespace"], receipt["revision_id"])
    except InsuranceError as exc:
        return {"valid": False, "reason": exc.code}
    if stored["record_id"] != receipt.get("record_id") or stored["record_hash"] != receipt.get("record_hash"):
        return {"valid": False, "reason": "the cited revision differs from the stored one"}
    return {"valid": True, "revision": stored}


class InsuranceMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = InsuranceStore(conn, initialize=False, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        watch: str,
        target: str,
        principal_id: str,
        scopes: Iterable[str],
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Watch an insurer (LEI, NAIC code or party key), a market (country) or an event (name or identifier)."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS:
            raise InsuranceError("invalid_watch", f"watch one of {WATCH_KINDS}")
        target = str(target or "").strip()
        if not target:
            raise InsuranceError("invalid_watch", "name the insurer, market or event to watch")
        query = {"operation": "search", "kind": "insurance-monitor", "watch": watch, "target": target}
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "market",
                "query": query,
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "insurance-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "source-pack runs commit the watermarks this monitor evaluates, within the pack's run "
            "budgets; there is no separate scheduler",
        }

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='knowledge_subscriptions'"
        ).fetchone():
            raise InsuranceError("not_ready", "no insurance monitor has been created yet")
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "insurance-monitor":
            raise InsuranceError("monitor_not_found", "subscription is not an insurance monitor")
        return subscription

    # -------------------------------------------------------------- snapshot

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> dict[str, Any]:
        from src.domains.market.insurance_identity import InsuranceIdentity

        namespace, query = subscription["namespace"], subscription["query"]
        self.store.require_ready()
        watch, target = query["watch"], query["target"]
        items: list[dict[str, Any]] = []
        identity = InsuranceIdentity(self.conn, initialize=False)
        keys: set[str] = set()
        linked: set[str] = set()
        if watch == "insurer":
            keys = set(identity.resolve(namespace, target, scopes=scopes)["record_keys"])
            for candidate in identity.candidates(namespace, scopes=scopes):
                if keys & set(candidate["records"]):
                    items.append({"id": "identity:" + candidate["candidate_id"], "item": "identity",
                                  "candidate_id": candidate["candidate_id"], "state": candidate["state"],
                                  "basis": candidate["basis"], "records": candidate["records"]})
        if watch == "event" and target.startswith("hazard:"):
            linked = {link["record_id"] for link in InsuranceLinks(self.conn).links(namespace, scopes=scopes,
                                                                                     target_id=target)
                      if link["effective"]}
        for view in self.store.visible(namespace):
            record = view["record"]
            if watch == "insurer":
                insurer = record.get("insurer")
                if not insurer or identity.insurer_key(insurer) not in keys:
                    continue
            elif watch == "market":
                if record["kind"] != "supervisory_indicator" or (record["dimensions"].get("country") or "").upper() \
                        != target.upper():
                    continue
            else:
                if record["kind"] != "catastrophe_loss_estimate":
                    continue
                names = {fold(record["event"]["name"]), fold(record["event"].get("reference"))}
                names |= {fold(v) for v in record["event"]["identifiers"].values()}
                if view["record_id"] not in linked and fold(target) not in names:
                    continue
            items.append({
                "id": view["record_id"],
                "item": "record",
                "record_id": view["record_id"],
                "revision_id": view["revision_id"],
                "record_hash": view["record_hash"],
                "record_kind": record["kind"],
                "provider": record["source"]["provider"],
                "url": record["source"]["url"],
                "publication_date": record.get("publication_date"),
                "release": record.get("release"),
                "label": (record.get("event") or {}).get("name") or (record.get("insurer") or {}).get("name")
                or record.get("indicator_label") or record.get("title"),
                "corrects": record.get("corrects"),
            })
        if watch == "market":
            for release in self.store.releases(namespace):
                items.append({"id": f"release:{release['provider']}|{release['dataset']}|{release['release']}",
                              "item": "release", **release})
        return {"items": items, "coverage": {"complete": True}}

    # -------------------------------------------------------------- evaluation

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?", [namespace]
            ).fetchone()
            if row is None or row[0] is None:
                raise InsuranceError("watermark_uncommitted",
                                     "no committed watermark yet; source-pack runs commit them")
            watermark = int(row[0])
        baseline = subscription.get("last_watermark") is None
        evaluated = self.subscriptions.evaluate(
            subscription_id, watermark, self.snapshot(subscription, scopes), principal_id=principal_id,
            scopes=scopes, observed_at_ms=self.now(),
        )
        evaluation = int(self.conn.execute(
            "SELECT count(*) FROM knowledge_subscription_snapshots WHERE subscription_id=? AND watermark<=?",
            [subscription_id, watermark],
        ).fetchone()[0])
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                "WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(namespace, subscription_id, watermark, evaluation, event_id, *row,
                                                baseline=baseline))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "evaluation": evaluation, "baseline": baseline, "notifications": notifications,
                "delivery": subscription["delivery"]}

    @staticmethod
    def _classify(namespace, subscription_id, watermark, evaluation, event_id, event_type, key, before, after, *,
                  baseline: bool) -> list[dict[str, Any]]:
        before_item = json.loads(before) if before else None
        after_item = json.loads(after) if after else None
        item = after_item or before_item or {}
        if event_type == "removed" or not item.get("item"):
            return []

        def note(kind: str, message: str, **extra: Any) -> dict[str, Any]:
            receipt = {
                "contract": RECEIPT_CONTRACT,
                "namespace": namespace,
                "subscription_id": subscription_id,
                "evaluation": evaluation,
                "watermark": watermark,
                "event_id": event_id,
                "record_id": item.get("record_id"),
                "revision_id": item.get("revision_id"),
                "record_hash": item.get("record_hash"),
                "url": item.get("url"),
                "publication_date": item.get("publication_date") or item.get("release_date"),
            }
            receipt["receipt_hash"] = digest(receipt)
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": "in_view" if baseline else kind, "object": key, "message": message,
                    "receipt": receipt, **extra}

        if item["item"] == "identity":
            if event_type == "added" or (before_item and before_item.get("state") != item.get("state")):
                return [note("identity_match_change", f"Identity candidate {item['candidate_id']} is "
                                                      f"{item['state']} ({item['basis']}).",
                             state={"before": (before_item or {}).get("state"), "after": item["state"]})]
            return []
        if item["item"] == "release":
            return [note("new_vintage", f"{item['provider']} published release {item['release']} of "
                                        f"{item['dataset']}.")] if event_type == "added" else []
        kind = item["record_kind"]
        changed = event_type != "added"
        if kind == "supervisory_indicator":
            return [note("new_vintage", f"{item['label']}: a figure was published or revised in release "
                                        f"{item.get('release')}.", revised=changed)]
        if kind == "insurer_report":
            return [note("corrected_insurer_report" if changed or item.get("corrects") else "new_insurer_report",
                         f"{item['label']}: SFCR published {item.get('publication_date')}.")]
        if kind == "catastrophe_loss_estimate":
            return [note("new_loss_estimate_revision",
                         f"{item['provider']} published an estimate for {item['label']} on "
                         f"{item.get('publication_date')}.")]
        return [note("new_publication_reference", f"{item['label']} (metadata only).")]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        self._subscription(subscription_id, principal_id, scopes)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
