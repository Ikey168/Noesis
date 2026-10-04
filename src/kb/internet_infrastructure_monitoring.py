"""Monitor registration, routing and certificate changes through subscriptions (#2743, II10).

An internet-infrastructure monitor is an ordinary knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`,
``platform.subscriptions``) following :mod:`src.kb.business_statistics_monitoring`: no watcher table and no scheduler.
Its target is one *declared* ASN, prefix or domain (IP-keyed, person-keyed, wildcard and undeclared targets are
refused), optionally narrowed to one provider, or a provider alone (e.g. the CT log list). Each run evaluates one
committed watermark against a snapshot of every watched record's revisions and observations; the subscription store
turns new items into events, so a replay, an idempotent re-acquisition or an unchanged answer emits nothing.

Notices are record changes, not assessments. Each cites the new or changed record (and the one before) and states
what changed:

* ``registration_change`` - an RDAP registration revision (first registration seen, changed events, holder or RIR);
* ``peeringdb_update`` - a PeeringDB network, organisation, IX presence, IX or facility revision (a changed
  ``updated``);
* ``new_certificate`` - a certificate crt.sh states for the watched domain;
* ``ct_log_state_change`` - a CT log revision (a changed state or another log list statement);
* ``routing_observation_changed`` - a RIPEstat answer whose content differs from the previous observation of the
  same data call (an unchanged later answer is recorded but notifies nothing);
* ``removed_by_source`` - a 404, ``status=deleted`` or absence from a complete listing.

They never carry hijack, risk or misconfiguration verdicts. :meth:`InfrastructureMonitor.refresh` re-reads one declared
source through its real adapter within the source's page budget, records a receipt per run and is idempotent (an
unchanged unit adds nothing); a failed unit stops the run and records a failure receipt only, so a failed run never
produces a removal notice. Live answers of providers still ``unverified-live`` are withheld from notices.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.internet_infrastructure_sources import PROVIDERS, unverified
from src.kb.internet_infrastructure_queries import resolve_resource
from src.kb.internet_infrastructure_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    InfrastructureRecordError,
    authorize,
    canonical,
    digest,
    forbidden_paths,
    iso,
    table_exists,
)
from src.kb.internet_infrastructure_store import (
    InternetInfrastructureProjector,
    InternetInfrastructureStore,
)

CONTRACT = "noesis-internet-infrastructure-notification-v1"
FILTER_KEYS = ("resource", "provider")
MESSAGES = {
    "registration_change": "An RDAP registration of a watched resource has a new revision",
    "peeringdb_update": "A PeeringDB self-declaration of a watched resource has a new revision",
    "new_certificate": "crt.sh states a new certificate for a watched domain",
    "ct_log_state_change": "A CT log has a new revision in the CT log list",
    "routing_observation_changed": "A RIPEstat answer for a watched resource differs from the previous one",
    "removed_by_source": "A source no longer states a watched record",
}
_DDL = """
CREATE TABLE IF NOT EXISTS ii_refresh_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, source_id TEXT NOT NULL, started_at_ms BIGINT NOT NULL,
  status TEXT NOT NULL, retry_at_ms BIGINT, receipt_json TEXT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""


def revision_kind(provider: str, revision: Mapping[str, Any]) -> str:
    if revision["state"] == "removed_by_source":
        return "removed_by_source"
    return {"rdap": "registration_change", "peeringdb": "peeringdb_update", "crtsh": "new_certificate",
            "ct-log-list": "ct_log_state_change"}[provider]


class InfrastructureMonitor:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = InternetInfrastructureStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def _filter(self, namespace: str, target: Mapping[str, Any]) -> dict[str, Any]:
        raw = dict(target or {})
        if set(raw) - set(FILTER_KEYS) or not any(raw.get(k) for k in FILTER_KEYS):
            raise InfrastructureRecordError("invalid_watch", "watch a declared resource, a provider, or both")
        provider = raw.get("provider")
        if provider is not None and provider not in PROVIDERS:
            raise InfrastructureRecordError("invalid_watch", f"provider is one of {PROVIDERS}")
        resource = None
        if raw.get("resource") not in (None, ""):
            resource = resolve_resource(raw["resource"])  # refuses IP-keyed, person-keyed and wildcard targets
            if not self.store.is_declared(namespace, resource):
                raise InfrastructureRecordError("undeclared_resource", "only declared resources can be watched")
        return {"resource": resource, "provider": provider}

    def create(self, namespace: str, request_key: str, *, target: Mapping[str, Any], principal_id: str,
               scopes: Iterable[str], delivery: dict[str, Any] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = self._filter(namespace, target)
        created = self.subscriptions.create(
            {"namespace": namespace, "domain": "technical",
             "query": {"operation": "search", "kind": "internet-infrastructure-monitor", "filter": wanted},
             "filters": {"watch": "internet-infrastructure"}, "cadence": {"trigger": "watermark"},
             "delivery": delivery or {"kind": "poll"}},
            "internet-infrastructure-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        return {**created, "refresh": "the technology-internet-infrastructure source-pack runs (or refresh()) "
                "acquire units and commit the watermarks this monitor evaluates; no new scheduler"}

    def _subscription(self, subscription_id: str, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "internet-infrastructure-monitor":
            raise InfrastructureRecordError("monitor_not_found", "subscription is not an internet-infrastructure "
                                                                 "monitor")
        return subscription

    def _items(self, namespace: str, wanted: Mapping[str, Any]) -> list[dict[str, Any]]:
        items = []
        for obj in self.store.objects(namespace, provider=wanted.get("provider"), resource=wanted.get("resource")):
            unit_origin = {}
            if obj["shape"] == "observations":
                for observation in self.store.observations(namespace, obj["object_id"]):
                    if not observation["changed"] or observation["previous_observation_id"] is None:
                        continue  # the first answer is the baseline; an unchanged later answer notifies nothing
                    unit = unit_origin.setdefault(observation["unit_id"],
                                                  self.store.unit(namespace, observation["unit_id"]))
                    items.append(self._item(obj, "routing_observation_changed", observation["observation_id"],
                                            observation["previous_observation_id"], observation["stated_time"],
                                            {"data_call": observation["data_call"],
                                             "data_call_version": observation["data_call_version"],
                                             "content_after": observation["content"]},
                                            self.store.cite(namespace, obj, observation), unit))
                continue
            previous = None
            for revision in self.store.revisions(namespace, obj["object_id"]):
                unit = unit_origin.setdefault(revision["unit_id"], self.store.unit(namespace, revision["unit_id"]))
                items.append(self._item(obj, revision_kind(obj["provider"], revision), revision["revision_id"],
                                        previous and previous["revision_id"], revision["valid_from"],
                                        {"revision_no": revision["revision_no"], "basis": revision["basis"],
                                         "source_revision": revision["source_revision"],
                                         "changes": revision["changes"]},
                                        self.store.cite(namespace, obj, revision), unit))
                previous = revision
        return items

    @staticmethod
    def _item(obj, kind, record_id, previous_id, at, detail, citation, unit) -> dict[str, Any]:
        return {"id": f"record:{record_id}:{kind}", "item": kind, "object_id": obj["object_id"],
                "object": {"provider": obj["provider"], "object_kind": obj["object_kind"],
                           "native_id": obj["native_id"], "resource": obj["resource"]},
                "record_id": record_id, "previous_record_id": previous_id, "as_of": at, "detail": detail,
                "citation": citation, "evidence_origin": unit["evidence_origin"], "provider": obj["provider"]}

    @staticmethod
    def notifiable(item: Mapping[str, Any]) -> bool:
        """Fixture or operator evidence, or live evidence from a provider whose live access is verified."""
        return item["evidence_origin"] != "live" or not unverified(item["provider"])

    def snapshot(self, subscription: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        items = self._items(subscription["namespace"], subscription["query"]["filter"])
        kept = [i for i in items if self.notifiable(i)]
        return {"items": kept, "coverage": {"complete": True}}, len(items) - len(kept)

    def default_watermark(self, namespace: str) -> tuple[int, dict[str, Any]]:
        latest = self.store.latest_ms(namespace)
        if latest is None:
            raise InfrastructureRecordError("not_ready", "nothing acquired yet; acquire first")
        generation = digest([r[0] for r in self.conn.execute(
            "SELECT revision_id FROM ii_revisions WHERE namespace=? UNION ALL SELECT observation_id FROM "
            "ii_observations WHERE namespace=? ORDER BY 1", [namespace, namespace]).fetchall()])[:24]
        rows = self.conn.execute(
            "SELECT watermark, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark",
            [namespace]).fetchall() if table_exists(self.conn, "knowledge_subscription_watermarks") else []
        for watermark, committed in reversed(rows):
            if json.loads(committed or "{}").get("internet_infrastructure_generation") == generation:
                return int(watermark), json.loads(committed)
        highest = int(rows[-1][0]) if rows else 0
        return max(latest, highest + 1), {"internet_infrastructure_generation": generation,
                                          "observed_at": iso(latest)}

    def run(self, subscription_id: str, watermark: int | None = None, *, principal_id: str,
            scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            watermark, detail = self.default_watermark(namespace)
            self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion", detail=detail)
        result, withheld = self.snapshot(subscription)
        evaluated = self.subscriptions.evaluate(subscription_id, int(watermark), result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        if forbidden_paths(notifications):
            raise InfrastructureRecordError("excluded_field", "notices never carry verdicts or rankings")
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": int(watermark),
                "notifications": notifications, "withheld_unverified_live_items": withheld,
                "delivery": subscription["delivery"],
                "note": "notices report record changes as the sources state them; no hijack, risk or "
                        "misconfiguration verdict"}

    @staticmethod
    def _classify(event_id: str, event_type: str, key: str, after: str | None) -> list[dict[str, Any]]:
        if event_type != "added" or not after:
            return []  # a record disappearing from a snapshot is never a removal notice; removals are revisions
        item = json.loads(after)
        return [{
            "contract": CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{item['item']}",
            "object": key, "kind": item["item"],
            "message": f"{MESSAGES[item['item']]} ({item['provider']}, {item['as_of']}).",
            "record": item["object"], "record_id": item["record_id"], "previous_record_id": item["previous_record_id"],
            "what_changed": item["detail"], "citation": item["citation"],
        }]

    def poll(self, subscription_id: str, *, principal_id: str, scopes: Iterable[str], cursor: str = "") -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

    # ------------------------------------------------------------------ bounded refresh

    def refresh(self, namespace: str, source: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                transport: Callable[..., Mapping[str, Any]] | None = None, secret: str | None = None,
                retrieved_at_ms: int | None = None, **adapter_options: Any) -> dict[str, Any]:
        """Re-read a source's declared resources within its page budget; idempotent, one receipt per run."""
        from src.ingestion.internet_infrastructure_sources import (
            InternetInfrastructureAdapter,
        )
        from src.ingestion.source_packs import SourcePackError

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        source = dict(source)
        now = self.now()
        waiting = self.conn.execute(
            "SELECT max(retry_at_ms) FROM ii_refresh_receipts WHERE namespace=? AND source_id=?",
            [namespace, source["source_id"]]).fetchone()
        if waiting and waiting[0] is not None and waiting[0] > now:
            return self._receipt(namespace, source, now, "rate_limited_wait", [], None, waiting[0], principal_id,
                                 note="the provider asked to wait; nothing was requested")
        adapter = InternetInfrastructureAdapter(source, transport=transport, secret=secret, **adapter_options)
        limit = min(len(adapter.declared["units"]), int(source["budgets"]["max_pages"]))
        projector = InternetInfrastructureProjector(self.conn)
        projector.store.now = (lambda: retrieved_at_ms) if retrieved_at_ms is not None else self.now
        units, stopped, retry_at, cursor = [], None, None, None
        run_id = f"refresh:{source['source_id']}:{now}"
        for _ in range(limit):
            try:
                page = adapter.fetch_page({"operation": "selection", "parameters": {},
                                           "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
            except SourcePackError as exc:
                stopped = {"code": exc.code, "message": str(exc)}
                if exc.code == "rate_limited":
                    retry_at = now + int(getattr(exc, "details", {}).get("retry_after_ms") or 3_600_000)
                break
            applied = projector.project_page(run_id=run_id, manifest=None, source=source, records=page.records,
                                             documents=None, page_receipt=page.receipt, principal_id=principal_id)
            units += [{k: a.get(k, 0) for k in ("unit_id", "status", "revisions", "observations", "removed")}
                      for a in applied]
            cursor = page.next_cursor
            if cursor is None:
                break
        if stopped:
            projector.store.record_failure(namespace, adapter.provider, code=stopped["code"], run_id=run_id,
                                           source_id=source["source_id"], scopes=scopes)
        status = "stopped" if stopped else "complete" if cursor is None else "bounded"
        return self._receipt(namespace, source, now, status, units, stopped, retry_at, principal_id)

    def _receipt(self, namespace, source, now, status, units, stopped, retry_at, principal_id, note=None):
        body = {"source_id": source["source_id"], "status": status, "units": units,
                "applied_units": sum(1 for u in units if u["status"] == "applied"),
                "unchanged_units": sum(1 for u in units if u["status"] == "unchanged"),
                "stopped": stopped, "retry_at": iso(retry_at), "requested_by": principal_id, "at": iso(now),
                "note": note or ("a failed unit records a failure receipt only; earlier revisions stay current and "
                                 "nothing is marked removed" if stopped else None)}
        receipt_id = "ii-refresh:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT OR IGNORE INTO ii_refresh_receipts VALUES (?,?,?,?,?,?,?)",
                          [namespace, receipt_id, source["source_id"], now, status, retry_at, canonical(body)])
        return {"receipt_id": receipt_id, **body}


__all__ = ["CONTRACT", "FILTER_KEYS", "MESSAGES", "InfrastructureMonitor", "revision_kind"]
