"""Monitor new actions, decisions, penalties, appeals and corrections through subscriptions (#2651, EN11).

An enforcement monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`), following
:mod:`src.kb.competition_monitoring` and :mod:`src.kb.campaign_finance_monitoring`:
its query names an **entity** (an ownership entity reached through accepted
respondent matches, optionally with its group), a published **identifier**
(``scheme:value``), an **authority**, a **legal basis** or one **action**
(record key). There is no monitor table and no scheduler: the
``legal-research`` source-pack schedule acquires, the maintenance orchestrator
commits watermarks, and each evaluation turns differences into subscription
events delivered through the existing poll and outbox paths. A natural person
is never a monitor target.

Notifications cite the new and the previous revision and state what changed
(the fields whose published value differs): ``new_action``,
``action_revised``, ``action_corrected``, ``action_removed_by_source``,
``new_decision``, ``decision_revised``, ``new_penalty``, ``penalty_revised``,
``new_appeal``, ``appeal_revised``, ``new_notice_document`` and
``notice_document_revised``. Unchanged payloads emit nothing. A revision
acquired *live* from a provider whose access is still ``unverified-live`` is
withheld (the latest notifiable revision is used instead) until a dated live
run verifies it; fixture replays are notified and marked as fixture evidence.
Notices are record changes, not assessments.
:meth:`EnforcementMonitor.refresh` re-reads one declared source selection
through the real adapter within its budget; unchanged responses add nothing
and every unit leaves a receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.enforcement_sources import LIVE_VERIFICATION
from src.kb.enforcement import (
    READ_SCOPE,
    WRITE_SCOPE,
    EnforcementError,
    EnforcementProjector,
    EnforcementStore,
    authorize,
)

CONTRACT = "noesis-enforcement-notification-v1"
WATCH_KINDS = ("entity", "identifier", "authority", "legal_basis", "action")
# Summary fields per kind; a change in any of them is stated in the notification.
SUMMARY_FIELDS = {
    "enforcement_action": ("title", "authority", "action_type_as_published", "legal_bases", "initiated_on",
                           "decided_on", "published_on", "outcome_as_published", "settled", "admission_wording",
                           "appeal_status_as_published", "publication_status"),
    "decision": ("decision_type_as_published", "decided_on", "outcome_as_published", "settled", "admission_wording",
                 "corrective_measures"),
    "penalty": ("penalty_type", "amount_as_published", "currency", "status", "stage"),
    "appeal": ("forum_as_published", "reference", "status_as_published", "stated_on"),
    "notice_document": ("url", "title", "published_on", "content_sha256"),
}
ITEM_KIND = {"enforcement_action": "action", "decision": "decision", "penalty": "penalty", "appeal": "appeal",
             "notice_document": "notice_document"}


def notifiable(source: Mapping[str, Any]) -> bool:
    origin = source.get("evidence_origin")
    return origin == "fixture" or LIVE_VERIFICATION.get(source.get("provider"), {}).get("status") == "verified-live"


class EnforcementMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = EnforcementStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, key: str, principal_id: str,
               scopes: Iterable[str], ownership_namespace: str | None = None, group: bool = False,
               delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        from src.kb.enforcement_records import authority_valid

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        key = str(key or "").strip()
        if watch not in WATCH_KINDS or not key:
            raise EnforcementError("invalid_watch", f"watch one of {WATCH_KINDS} with a key")
        if watch == "action" and not key.startswith("enforcement:action:"):
            raise EnforcementError("invalid_watch", "an action is watched by its record key (enforcement:action:...)")
        if watch == "authority" and not authority_valid(key):
            raise EnforcementError("invalid_watch", "an authority is us-sec, uk-fca, us-epa or eu-sa-xx")
        if watch == "identifier" and ":" not in key:
            raise EnforcementError("invalid_watch", "an identifier is watched as scheme:value (e.g. sec-cik:...)")
        if watch == "entity":
            if not ownership_namespace:
                raise EnforcementError("invalid_watch", "entities are ownership entities; name the ownership namespace")
            authorize(ownership_namespace, scopes, "knowledge:ownership:read")
            self._refuse_person(ownership_namespace, key, principal_id, scopes)
        query = {"operation": "search", "kind": "enforcement-monitor", "watch": watch, "key": key,
                 **({"ownership_namespace": ownership_namespace} if ownership_namespace else {}),
                 **({"group": True} if group else {})}
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "legal", "query": query, "filters": {"watch": watch},
             "cadence": {"trigger": "watermark"}, "delivery": delivery or {"kind": "poll"}},
            "enforcement-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the legal-research source-pack schedule and the maintenance orchestrator "
                                      "commit the watermarks this monitor evaluates; no new scheduler"}

    def _refuse_person(self, ownership_namespace: str, key: str, principal_id: str, scopes: set[str]) -> None:
        from src.kb.enforcement import table_exists

        if not table_exists(self.conn, "ownership_records"):
            return
        from src.kb.ownership_store import OwnershipStore

        for view in OwnershipStore(self.conn, initialize=False).records(
                ownership_namespace, principal_id=principal_id, scopes=scopes, kinds=("person",)):
            if key in {view["record_id"], view["record"].get("record_key")}:
                raise EnforcementError("natural_person_not_a_query_key",
                                       "natural persons are never a monitor target (EN01 minimisation decision)")

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "enforcement-monitor":
            raise EnforcementError("monitor_not_found", "subscription is not an enforcement monitor")
        return subscription

    # ------------------------------------------------------------------ snapshots

    def _visible(self, namespace: str, key: str, withheld: list[int]) -> dict[str, Any] | None:
        history = self.store.history(namespace, key)
        visible = [v for v in history if notifiable(v["record"]["source"])]
        withheld[0] += len(history) - len(visible)
        return visible[-1] if visible else None

    @staticmethod
    def _cite(view: Mapping[str, Any]) -> dict[str, Any]:
        return {"record_key": view["record"]["record_key"], "revision_id": view["revision_id"],
                "revision": view["revision"], "provider": view["record"]["source"]["provider"],
                "observed_at_ms": view["observed_at_ms"],
                "evidence_origin": view["record"]["source"].get("evidence_origin")}

    def _action_items(self, namespace: str, action_keys: Iterable[str], items: list, withheld: list[int]) -> None:
        for key in sorted(set(action_keys)):
            view = self._visible(namespace, key, withheld)
            if view is None:
                continue
            children = self.store.action_children(namespace, key)
            for shown in [view] + [self._visible(namespace, c["record"]["record_key"], withheld)
                                   for kind in ("decision", "penalty", "appeal", "notice_document")
                                   for c in children[kind]]:
                if shown is None:
                    continue
                body = shown["record"]
                summary = {f: body.get(f) for f in SUMMARY_FIELDS[body["kind"]]}
                items.append({"id": f"{ITEM_KIND[body['kind']]}:{body['record_key']}", "kind": ITEM_KIND[body["kind"]],
                              "action_key": key, "cite": self._cite(shown), "summary": summary,
                              **({"corrected": True} if body.get("publication_status") == "corrected" else {})})

    def _action_keys(self, subscription: Mapping[str, Any], scopes: set[str]) -> list[str]:
        from src.kb.enforcement_links import basis_keys, parse_references
        from src.kb.enforcement_queries import (
            actions_for_entity,
            actions_for_identifier,
        )

        namespace, query = subscription["namespace"], subscription["query"]
        watch, key = query["watch"], query["key"]
        if watch == "action":
            return [key]
        if watch == "entity":
            answer = actions_for_entity(self.conn, namespace, key, ownership_namespace=query["ownership_namespace"],
                                        scopes=scopes, group=bool(query.get("group")))
            return [row["action_key"] for row in answer["actions"]]
        if watch == "identifier":
            scheme, value = key.split(":", 1)
            return [row["action_key"] for row in actions_for_identifier(self.conn, namespace, scheme, value,
                                                                         scopes=scopes)["actions"]]
        actions = self.store.views(namespace, ("enforcement_action",))
        if watch == "authority":
            return [v["record"]["record_key"] for v in actions if v["record"]["authority"] == key]
        wanted = {c["key"] for c in parse_references(key)}
        normal = " ".join(key.lower().split())
        return [v["record"]["record_key"] for v in actions
                if normal in {" ".join(b.lower().split()) for b in v["record"].get("legal_bases") or []}
                or (wanted and wanted & basis_keys(v["record"]))]

    def snapshot(self, subscription: Mapping[str, Any], scopes: set[str]) -> tuple[dict[str, Any], int]:
        items: list[dict[str, Any]] = []
        withheld = [0]
        self._action_items(subscription["namespace"], self._action_keys(subscription, scopes), items, withheld)
        return {"items": items, "coverage": {"complete": True}}, withheld[0]

    # ------------------------------------------------------------------ evaluation

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
                raise EnforcementError("watermark_uncommitted", "no committed watermark yet; source-pack runs and the "
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
        if event_type == "removed" or new is None:
            return []
        changed = sorted(f for f in new["summary"] if old is None or old["summary"].get(f) != new["summary"].get(f))
        kind = new["kind"]
        if old is None:
            label = f"new_{kind}"
        elif kind == "action" and new["summary"].get("publication_status") == "removed_by_source":
            label = "action_removed_by_source"
        elif kind == "action" and new.get("corrected"):
            label = "action_corrected"
        else:
            label = f"{kind}_revised"
        if old is not None and old["cite"]["revision_id"] == new["cite"]["revision_id"]:
            return []
        message = (f"{new['action_key']}: new {kind.replace('_', ' ')}" if old is None else
                   f"{new['action_key']}: {kind.replace('_', ' ')} revision {new['cite']['revision']} changed "
                   + ", ".join(changed))
        return [{"contract": CONTRACT, "notification_id": f"{event_id}:{label}", "event_id": event_id,
                 "kind": label, "object": key, "action_key": new["action_key"], "message": message,
                 "changed_fields": changed if old is not None else [],
                 "changes": {f: {"before": (old or {}).get("summary", {}).get(f), "after": new["summary"].get(f)}
                             for f in changed} if old is not None else {},
                 "summary": new["summary"], "previous_summary": (old or {}).get("summary"),
                 "cites": {**new["cite"], "previous": (old or {}).get("cite")},
                 "evidence_origin": new["cite"].get("evidence_origin"),
                 "note": "a record change as the regulator published it; not an assessment"}]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""
             ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    def refresh(self, source: Mapping[str, Any], *, run_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Re-read one declared selection within its budget; idempotent, and every unit leaves a receipt."""
        from src.ingestion.enforcement_sources import EnforcementAdapter

        scopes = set(scopes)
        namespace = EnforcementProjector._namespace(source)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        adapter = EnforcementAdapter(source, transport=transport)
        projector = EnforcementProjector(self.conn)
        projector.store.now = self.now
        counts: dict[str, int] = {}
        cursor, units = None, 0
        while units < min(len(adapter.units), int(source["budgets"]["max_pages"])):
            page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                       "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            result = projector.project(namespace, [r["enforcement_record"] for r in page.records], run_id=run_id,
                                       receipt=dict(page.receipt or {}), observed_at_ms=self.now())
            for change, count in result["counts"].items():
                counts[change] = counts.get(change, 0) + count
            units += 1
            cursor = page.next_cursor
            if cursor is None:
                break
        return {"run_id": run_id, "source_id": source["source_id"], "units": units, "counts": counts,
                "complete": cursor is None, "receipts": self.store.receipts(namespace, run_id, scopes=scopes)}
