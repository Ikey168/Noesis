"""Watch an object, operator or registering State for registrations, status changes and re-entries (#2224, SO11).

Extends the Astronomy monitoring (:mod:`src.kb.astronomy_monitoring`, #2159):
a registration monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`) whose query names what is
watched, so there is no monitor table and no scheduler. Refresh is the
astronomy-and-space source-pack schedule within the SO01 bounds (each run
leaves page receipts, reported with every evaluation) plus the subscription
watermarks the runtime and the maintenance orchestrator commit. The first
evaluation is a baseline; replaying a watermark or an unchanged refresh
notifies nothing.

Events, each dated and citing the new (and old) revision and its source:
``registration_published``, ``status_changed`` (a change-of-status notice or
a changed index status), ``supervision_transferred``, ``operator_changed``,
``reentry_predicted`` and ``reentry_confirmed``. None carries a prediction,
verdict or attribution of Noesis's own.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.astronomy_records import READ_SCOPE, AstronomyError, authorize
from src.kb.astronomy_registration import RegistrationStore, identifier_keys, own_date, party_key

CONTRACT = "noesis-astronomy-registration-notification-v1"
WATCH_KINDS = ("object", "operator", "registering_state")
EVENTS = (
    "registration_published",
    "status_changed",
    "supervision_transferred",
    "operator_changed",
    "reentry_predicted",
    "reentry_confirmed",
)
QUERY_KIND = "astronomy-registration-monitor"


def _cite(view: Mapping[str, Any]) -> dict[str, Any]:
    source = view["record"]["source"]
    return {
        "record_id": view["record_id"],
        "revision_id": view["revision_id"],
        "record_hash": view["record_hash"],
        "provider": source["provider"],
        "source_record_id": source["source_record_id"],
        "dated": own_date(view["record"]),
        **({"url": source["url"]} if source.get("url") else {}),
    }


class RegistrationMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = RegistrationStore(conn, initialize=False, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace: str, request_key: str, *, watch: str, target: str, principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS:
            raise AstronomyError("invalid_watch", f"watch one of {WATCH_KINDS}")
        if not str(target or "").strip():
            raise AstronomyError("invalid_watch", f"a {watch} monitor names what it watches")
        target = str(target).strip()
        keys = (identifier_keys(target) if watch == "object"
                else [("operator:" if watch == "operator" else "state:") + party_key(target)])
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "astronomy",
                "query": {"operation": "search", "kind": QUERY_KIND, "watch": watch, "target": target, "keys": keys},
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "astronomy-registration-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {**created, "refresh": "astronomy-and-space source-pack schedules (within the SO01 bounds, with "
                "receipts) and the maintenance orchestrator commit the watermarks; no separate scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='knowledge_subscriptions'"
        ).fetchone():
            raise AstronomyError("not_ready", "no registration monitor has been created yet")
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != QUERY_KIND:
            raise AstronomyError("monitor_not_found", "subscription is not a registration monitor")
        return subscription

    def snapshot(self, subscription: Mapping[str, Any]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        self.store.require_ready()
        keys = set(query["keys"])
        if query["watch"] == "object":
            first = self.store.visible(namespace, keys=keys)["records"]
            for view in first:
                keys |= {f"{k}:{view['record'][k]}" for k in ("cospar", "norad") if view["record"].get(k)}
        visible = self.store.visible(namespace, keys=keys)
        items: list[dict[str, Any]] = []
        operators: dict[str, dict[str, Any]] = {}
        for view in visible["records"]:
            record = view["record"]
            if record["kind"] == "registration_entry":
                items.append({
                    "id": f"registration:{view['record_id']}", "kind": "registration_entry",
                    "entry_kind": record["entry_kind"], "citation": _cite(view),
                    **{k: record[k] for k in ("cospar", "object_name", "status", "un_document", "registering_state",
                                              "supervision", "status_change", "reentry", "un_registered")
                       if k in record},
                })
            elif record["kind"] == "operator_assertion":
                obj = record.get("cospar") or record.get("norad") or record.get("discos_id") or record.get(
                    "object_name")
                group = operators.setdefault(f"operators:{record['source']['provider']}:{obj}:{record['role']}", {
                    "id": f"operators:{record['source']['provider']}:{obj}:{record['role']}",
                    "kind": "operator_assertion", "role": record["role"], "object": obj, "names": [],
                    "citation": _cite(view)})
                group["names"] = sorted({*group["names"], record["operator_name"]})
                if (own_date(record) or "") >= (group["citation"].get("dated") or ""):
                    group["citation"] = _cite(view)
            elif record["kind"] == "reentry_report":
                items.append({
                    "id": f"reentry:{view['record_id']}", "kind": "reentry_report", "citation": _cite(view),
                    **{k: record[k] for k in ("report_kind", "issued_at", "reported_time", "cospar", "norad")
                       if k in record},
                })
        items.extend(operators.values())
        return {"items": items, "coverage": {"complete": not visible["unreadable"],
                                             "unreadable": len(visible["unreadable"])}}

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
                raise AstronomyError("watermark_uncommitted", "no committed watermark yet; source-pack runs and the "
                                     "maintenance orchestrator commit them")
            watermark = int(row[0])
        baseline = subscription.get("last_watermark") is None
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, self.snapshot(subscription),
                                                principal_id=principal_id, scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                "WHERE event_id=?",
                [event_id],
            ).fetchone()
            note = None if baseline else self._classify(*row)
            if note:
                notifications.append({"contract": CONTRACT, "subscription_id": subscription_id,
                                      "watermark": watermark, "event_id": event_id, **note})
        receipts = self.store.receipts(namespace)
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "baseline": baseline,
            "n": len(notifications),
            "notifications": notifications,
            "refresh": {"receipts": len(receipts), "last_runs": sorted({r["run_id"] for r in receipts})[-5:]},
            "delivery": subscription["delivery"],
        }

    @staticmethod
    def _classify(event_type, key, before, after) -> dict[str, Any] | None:
        del event_type
        old = json.loads(before) if before else None
        new = json.loads(after) if after else None
        if new is None:
            return None  # nothing is deleted; an item that left the watched scope is not an event

        def note(event: str, message: str) -> dict[str, Any]:
            return {"event": event, "message": message, "object_key": key, "dated": new["citation"].get("dated"),
                    "old_revision": old and old["citation"], "new_revision": new["citation"],
                    "source": new["citation"]["provider"],
                    "policy": "describes what the publisher stated; no prediction, verdict or attribution"}

        kind = new["kind"]
        if kind == "registration_entry":
            entry = new["entry_kind"]
            if old is None:
                if entry == "transfer_of_supervision":
                    return note("supervision_transferred",
                                f"supervision transferred to {new['supervision']['to']} as notified in "
                                f"{new.get('un_document')}")
                if entry == "change_of_status":
                    return note("status_changed", f"status {new['status_change']['status']} as notified in "
                                f"{new.get('un_document')}")
                if entry == "re_entry_notice":
                    return note("reentry_confirmed", f"re-entry notified in {new.get('un_document')}")
                if entry == "index_entry" and not new.get("un_registered"):
                    return None
                return note("registration_published",
                            f"registration published in {new.get('un_document') or 'the UN index'}")
            if entry == "index_entry" and old.get("status") != new.get("status"):
                return note("status_changed", f"the UN index states status {new.get('status')}")
            if entry == "index_entry" and new.get("un_registered") and not old.get("un_registered"):
                return note("registration_published", "the UN index now lists a registration document")
            return None
        if kind == "operator_assertion" and (old is None or old.get("names") != new.get("names")):
            return note("operator_changed", f"{new['role']} of {new['object']} stated as {', '.join(new['names'])}")
        if kind == "reentry_report" and (old is None or old["citation"]["revision_id"] !=
                                         new["citation"]["revision_id"]):
            if new.get("report_kind") == "post_event":
                if old is not None and old.get("report_kind") == "post_event" and \
                        old.get("reported_time") == new.get("reported_time"):
                    return None
                return note("reentry_confirmed", f"re-entry reported at {new.get('reported_time')} (post-event)")
            return note("reentry_predicted",
                        f"prediction issued {new.get('issued_at')}: {new.get('reported_time')} as published")
        return None

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = ""):
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
