"""Keep a clinical evidence map current through knowledge subscriptions (H11).

A clinical monitor is a :class:`~src.kb.subscriptions.SubscriptionStore`
subscription whose evaluated result set is the current state of every trial
and regulatory record in one evidence map. Evaluating it at a committed
watermark yields replay-safe events for trial status changes, new registry
versions, results postings, label revisions, new linked publications and
retractions; each notification cites the changed record and its revisions.
The same record changes invalidate the map, which stays stale until it is
recomputed.

No scheduler is added: sources are refreshed by the ``clinical-evidence``
source pack's own schedule in the source-pack runtime (and so by the
maintenance orchestrator); a monitor run only evaluates what is committed.
"""

from __future__ import annotations

import json
import time

from src.kb.clinical_records import READ_SCOPE, ClinicalRecordError, ClinicalRecordStore, digest

CONTRACT = "noesis-clinical-notification-v1"
SOURCE_PACK = "clinical-evidence"
_DDL = """
CREATE TABLE IF NOT EXISTS clinical_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, view_id TEXT NOT NULL);
"""


class ClinicalMonitor:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.clinical_evidence import EvidenceMapService
        from src.kb.clinical_publications import PublicationLinker
        from src.kb.subscriptions import SubscriptionStore

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        self.maps = EvidenceMapService(conn, initialize=initialize, now=self.now)
        self.records = ClinicalRecordStore(conn, initialize=initialize, now=self.now)
        self.publications = PublicationLinker(conn, initialize=initialize, now=self.now)

    def create(self, namespace, view_id, request_key, *, principal_id, scopes, delivery=None):
        view = self.maps.inspect(namespace, view_id, scopes=scopes)
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "clinical",
            "query": {"operation": "evidence", "kind": "clinical-evidence-map", "view_id": view_id},
            "filters": {"view_id": view_id}, "cadence": {"trigger": "watermark"},
            "delivery": delivery or {"kind": "poll"},
        }, "clinical-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        self.conn.execute("INSERT INTO clinical_monitors VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, view_id])
        return {**created, "view_id": view_id, "question": view["question"], "refresh": refresh_schedule(self.conn)}

    def _monitor(self, subscription_id, principal_id):
        row = self.conn.execute("SELECT namespace, owner, view_id FROM clinical_monitors WHERE subscription_id=?",
                                [subscription_id]).fetchone()
        if not row or row[1] != principal_id:
            raise ClinicalRecordError("monitor_not_found", "clinical monitor is unavailable")
        return {"namespace": row[0], "view_id": row[2]}

    def snapshot(self, namespace, view_id, *, principal_id, scopes):
        """Current state of every record in the map (read from records, not from the stale map)."""
        view = self.maps.inspect(namespace, view_id, scopes=scopes)
        items, stale = [], set()
        for trial in view["trials"]:
            current = self.records.get(namespace, trial["record_id"], scopes=scopes)
            record = current["record"]
            postings = self.records.find(namespace, scopes=scopes, kinds={"result-posting"},
                                         provider=record["registry"], native_id=record["identifier"])
            publications = self.publications.publications(namespace, trial["record_id"], principal_id=principal_id,
                                                           scopes=scopes)
            items.append({
                "id": trial["record_id"], "kind": "trial", "identifier": record["identifier"],
                "registry": record["registry"], "revision": current["revision"], "version_key": current["version_key"],
                "status": (record.get("status") or {}).get("normalized"),
                "results": [{"record_id": p["record_id"], "revision": p["revision"],
                             "first_posted": p["record"]["posted"].get("first_posted")} for p in postings],
                "publications": [p["document_id"] for p in publications["publications"]],
                "retractions": sorted(n["notice_id"] for n in publications["retractions"]),
            })
            if self.records.provider_state(namespace, record["registry"])["stale"]:
                stale.add(record["registry"])
        for item in view["regulatory"]:
            current = self.records.get(namespace, item["record_id"], scopes=scopes)
            record = current["record"]
            items.append({"id": item["record_id"], "kind": "regulatory", "regulatory_kind": record["regulatory_kind"],
                          "title": record.get("title"), "revision": current["revision"],
                          "native_version": record["native_version"], "status": record.get("status")})
            if self.records.provider_state(namespace, record.get("provider"))["stale"]:
                stale.add(record.get("provider"))
        return {"items": items, "coverage": {"complete": not stale, "stale_providers": sorted(stale)}}

    def run(self, subscription_id, watermark, *, principal_id, scopes):
        monitor = self._monitor(subscription_id, principal_id)
        namespace, view_id = monitor["namespace"], monitor["view_id"]
        result = self.snapshot(namespace, view_id, principal_id=principal_id, scopes=scopes)
        self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion",
                                            detail={"clinical_snapshot": digest(result)})
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                "WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(_classify(event_id, *row))
        freshness = self.records.view_status(view_id)
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "view_id": view_id, "coverage": result["coverage"], "notifications": notifications,
                "view_freshness": freshness,
                "next_step": "recompute the evidence map" if freshness["stale"] else "map is current",
                "delivery": "configured subscription channel (poll by default)"}

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        self._monitor(subscription_id, principal_id)
        if READ_SCOPE not in scopes and "operator" not in scopes:
            raise ClinicalRecordError("unauthorized", "clinical read scope is required")
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)


def _classify(event_id, event_type, key, before, after):
    before = json.loads(before) if before else None
    after = json.loads(after) if after else None

    def note(kind, message, cite):
        return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id, "kind": kind,
                "record_id": None if key == "__coverage__" else key, "message": message, "cites": cite}

    if event_type == "coverage-degraded":
        return [note("stale_source", "A source refresh failed for " + ", ".join((after or {}).get("stale_providers")
                                                                                 or []) + "; nothing was changed.", {})]
    if event_type in {"added", "removed"}:
        item = after or before
        return [note("map_membership", f"{item.get('identifier') or item.get('title')} "
                                       f"{'entered' if event_type == 'added' else 'left'} the monitored map.",
                     {"revision": item.get("revision")})]
    cite = {"record_id": key, "before_revision": before.get("revision"), "after_revision": after.get("revision")}
    result = []
    if after["kind"] == "trial":
        name = after["identifier"]
        if before["status"] != after["status"]:
            result.append(note("trial_status_change", f"{name}: registry status {before['status']} -> "
                                                      f"{after['status']}.", cite))
        if before["version_key"] != after["version_key"]:
            result.append(note("new_registry_version", f"{name}: new registry version {after['version_key']}.", cite))
        old_results = {r["record_id"] for r in before["results"]}
        for posting in after["results"]:
            if posting["record_id"] not in old_results:
                posted = posting.get("first_posted") or "date unknown"
                result.append(note("results_posted", f"{name}: results posted ({posted}).",
                                   {**cite, "result_record_id": posting["record_id"],
                                    "result_revision": posting["revision"]}))
        for document_id in sorted(set(after["publications"]) - set(before["publications"])):
            result.append(note("new_linked_publication", f"{name}: new linked publication {document_id}.",
                               {**cite, "document_id": document_id}))
        for notice in sorted(set(after["retractions"]) - set(before["retractions"])):
            result.append(note("retraction", f"{name}: a linked publication was retracted.",
                               {**cite, "notice_id": notice}))
    else:
        if canonical_version(before) != canonical_version(after):
            kind = "label_revision" if after["regulatory_kind"] == "label-revision" else "regulatory_update"
            result.append(note(kind, f"{after['title']}: {after['regulatory_kind']} changed to version "
                                     f"{after['native_version'].get('version') or after['native_version'].get('date')}.",
                               {**cite, "native_version": after["native_version"]}))
        elif before["revision"] != after["revision"]:
            result.append(note("regulatory_update", f"{after['title']}: record revised.", cite))
    return result


def canonical_version(item):
    return json.dumps(item.get("native_version"), sort_keys=True)


def refresh_schedule(conn):
    """The clinical-evidence source pack's runtime schedule (the only refresh mechanism)."""
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='source_pack_schedules'").fetchone():
        return {"source_pack": SOURCE_PACK, "schedule": None,
                "note": "no source-pack schedule is configured; refreshes run only when the runtime runs the pack"}
    from src.ingestion.source_pack_runtime import schedule_owners

    rows = conn.execute("SELECT schedule_json, enabled, next_run_at_ms FROM source_pack_schedules WHERE pack_id=?",
                        [SOURCE_PACK]).fetchone()
    return {"source_pack": SOURCE_PACK,
            "schedule": None if not rows else {"schedule": json.loads(rows[0]), "enabled": bool(rows[1]),
                                               "next_run_at_ms": rows[2]},
            "owners": schedule_owners(conn, SOURCE_PACK) if rows else [],
            "note": "refreshes run through the source-pack runtime schedule and the maintenance orchestrator; "
                    "this pack adds no scheduler"}
