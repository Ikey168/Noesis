"""Watch a place, an operator or an asset for status, capacity and ownership changes (CI11, #2390).

A monitor is a knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`)
whose evaluated result set is the subject's current infrastructure view from
:mod:`src.kb.infrastructure_queries`. Each asset view is one item, holding its
status, its capacities, its owner and operator assertions, and the citation of
the revision it was read from.

Evaluation happens only at a committed watermark. :meth:`InfrastructureMonitor.refresh`
runs one bounded selection through the receipted acquisition (the same plan
and bounds as the source-pack runtime) and commits one; source-pack runs and
the maintenance orchestrator commit them too. There is no scheduler here.
Replaying a watermark, or an unchanged view, creates no new events.

Notifications classify the subscription events. Every change cites both the
old and the new revision:

* ``new_asset``: an asset entered the view;
* ``status_change``: the status in force changed;
* ``capacity_change``: a published capacity changed (old and new value with units);
* ``ownership_change``: owner, operator or parent assertions or shares changed as published;
* ``no_longer_in_view``: an asset left the view (never read as demolished or retired);
* ``stale_source``: a refresh failed; affected assets are uncertain, not removed.
"""

from __future__ import annotations

import json

from src.kb.infrastructure_assets import READ_SCOPE, WRITE_SCOPE, InfrastructureError, authorize, canonical

CONTRACT = "noesis-infrastructure-notification-v1"
SUBJECT_KINDS = ("place", "operator", "asset")
_DDL = """
CREATE TABLE IF NOT EXISTS infra_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, subject_json TEXT NOT NULL);
"""


def _subject(subject):
    if not isinstance(subject, dict) or subject.get("kind") not in SUBJECT_KINDS:
        raise InfrastructureError("invalid_subject", f"subject.kind is one of {SUBJECT_KINDS}")
    if subject["kind"] == "place" and not (subject.get("place_id") or subject.get("bbox")
                                           or subject.get("geometry_id")):
        raise InfrastructureError("invalid_subject", "a place subject names a place_id, geometry_id or bbox")
    if subject["kind"] == "operator" and not subject.get("operator"):
        raise InfrastructureError("invalid_subject", "an operator subject names the operator")
    if subject["kind"] == "asset" and not subject.get("asset_id"):
        raise InfrastructureError("invalid_subject", "an asset subject names the asset_id")
    return {k: subject[k] for k in ("kind", "place_id", "geometry_id", "bbox", "operator", "asset_id") if subject.get(k)}


class InfrastructureMonitor:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.infrastructure_assets import InfrastructureStore
        from src.kb.infrastructure_queries import InfrastructureQueries
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = InfrastructureStore(conn, initialize=initialize, now=now)
        self.queries = InfrastructureQueries(conn, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace, request_key, *, subject, principal_id, scopes, delivery=None):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        subject = _subject(subject)
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "geospatial-infrastructure",
            "query": {"operation": "search", "kind": "infrastructure-subject", "subject": subject},
            "filters": {"subject": subject}, "cadence": {"trigger": "watermark"},
            "delivery": delivery or {"kind": "poll"},
        }, "infrastructure-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        self.conn.execute("INSERT INTO infra_monitors VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, canonical(subject)])
        return {**created, "subject": subject,
                "refresh": "refresh() or source-pack runs commit the watermarks this monitor evaluates"}

    def _monitor(self, subscription_id, principal_id):
        row = self.conn.execute("SELECT namespace, owner, subject_json FROM infra_monitors WHERE subscription_id=?",
                                [subscription_id]).fetchone()
        if not row or row[1] != principal_id:
            raise InfrastructureError("monitor_not_found", "infrastructure monitor is unavailable")
        return {"namespace": row[0], "subject": json.loads(row[2])}

    def snapshot(self, monitor, *, principal_id, scopes):
        namespace, subject = monitor["namespace"], monitor["subject"]
        read = set(scopes) | {READ_SCOPE}
        if subject["kind"] == "place":
            answer = self.queries.assets_in_place(namespace, place_id=subject.get("place_id"),
                                                  geometry_id=subject.get("geometry_id"), bbox=subject.get("bbox"),
                                                  scopes=read, principal_id=principal_id)
        elif subject["kind"] == "operator":
            answer = self.queries.assets_of_operator(namespace, subject["operator"], scopes=read)
        else:
            answer = self.queries._answer(namespace, [subject["asset_id"]], scopes=read, as_of_ms=None,
                                          known_by_ms=None, query={"kind": "asset", "asset_id": subject["asset_id"]})
        items, stale = [], set()
        for group in answer.get("assets") or []:
            for view in group["sources"] + group["components"]:
                if self.store.provider_state(namespace, view["provider"])["stale"]:
                    stale.add(view["provider"])
                items.append({
                    "id": f"asset:{view['asset_id']}", "asset_id": view["asset_id"], "provider": view["provider"],
                    "name": view["name"], "asset_class": view["asset_class"], "revision_id": view["revision_id"],
                    "status": None if view["status"] is None else {k: view["status"][k] for k in (
                        "published", "normalized", "effective_date")},
                    "capacities": {f"{c['metric']}|{c['direction'] or ''}|{c['period'] or ''}": {
                        "value": c["value"], "unit": c["unit"], "estimate": c["estimate"]} for c in view["capacities"]},
                    "owners": [{"role": o["role"], "name": o["name"], "share": o["share"]} for o in view["owners"]],
                    "citation": {k: view["citation"][k] for k in ("provider", "dataset", "native_id", "revision_id",
                                                                   "release", "published_at", "source_url")}})
        return {"items": items, "coverage": {"complete": not stale, "stale_providers": sorted(stale),
                                             "status": answer.get("status")}}

    def run(self, subscription_id, watermark=None, *, principal_id, scopes):
        monitor = self._monitor(subscription_id, principal_id)
        namespace = monitor["namespace"]
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise InfrastructureError("watermark_uncommitted",
                                          "no committed watermark yet; refresh() or a source-pack run commits one")
            watermark = int(row[0])
        result = self.snapshot(monitor, principal_id=principal_id, scopes=scopes)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, before_json, after_json FROM "
                                    "knowledge_subscription_events WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "coverage": result["coverage"], "notifications": notifications,
                "delivery": "configured subscription channel (poll by default)"}

    def refresh(self, namespace, provider, selection, *, fetch, principal_id, scopes, execution="injected"):
        """One bounded, receipted acquisition (source-runtime plan and bounds), then a committed watermark."""

        from src.ingestion.infrastructure_sources import acquire

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        result = acquire(self.conn, provider, selection, namespace=namespace, scopes=scopes, principal_id=principal_id,
                         fetch=fetch, execution=execution, now=self.now)
        row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                [namespace]).fetchone()
        watermark = int(row[0] or 0) + 1 if row else 1
        self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion",
                                            detail={"provider": provider, "run_id": result["run_id"],
                                                    "receipts": [r["receipt_id"] for r in result["receipts"]]})
        return {**result, "watermark": watermark}

    @staticmethod
    def _classify(event_id, event_type, key, before, after):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None

        def note(kind, message, cites, **extra):
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id, "kind": kind,
                    "object": key, "message": message, "cites": cites, **extra}

        if event_type == "coverage-degraded":
            return [note("stale_source", "Refresh failed or never succeeded for "
                         + ", ".join((after or {}).get("stale_providers") or [])
                         + "; affected assets are uncertain, not removed.", {})]
        if event_type == "removed":
            return [note("no_longer_in_view", f"{before.get('name')} left the monitored view (not read as retired or "
                                              "demolished).", {"revision": before.get("citation")})]
        if event_type == "added":
            return [note("new_asset", f"{after.get('name') or after['asset_id']} ({after['provider']}) entered the view.",
                         {"revision": after["citation"]})]
        if before is None or after is None or before.get("revision_id") == after.get("revision_id"):
            return []
        cites = {"old_revision": before["citation"], "new_revision": after["citation"]}
        found = []
        if before.get("status") != after.get("status"):
            found.append(note("status_change", f"{after['name']}: status {(before.get('status') or {}).get('published')}"
                                                f" -> {(after.get('status') or {}).get('published')}.", cites,
                              old=before.get("status"), new=after.get("status")))
        if before.get("capacities") != after.get("capacities"):
            changes = [{"capacity": k, "old": before["capacities"].get(k), "new": after["capacities"].get(k)}
                       for k in sorted(set(before["capacities"]) | set(after["capacities"]))
                       if before["capacities"].get(k) != after["capacities"].get(k)]
            found.append(note("capacity_change", f"{after['name']}: {len(changes)} capacity figure(s) changed as "
                                                 "published.", cites, changes=changes))
        if before.get("owners") != after.get("owners"):
            found.append(note("ownership_change", f"{after['name']}: owner/operator assertions changed as published.",
                              cites, old=before.get("owners"), new=after.get("owners")))
        return found

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        self._monitor(subscription_id, principal_id)
        if READ_SCOPE not in scopes and "operator" not in scopes:
            raise InfrastructureError("unauthorized", "infrastructure read scope is required")
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
