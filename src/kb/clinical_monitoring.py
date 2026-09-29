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


# ------------------------------------------------------------------ medicines regulation (#2214, MR11)

MEDICINES_CONTRACT = "noesis-clinical-medicines-notification-v1"
MEDICINES_EVENTS = {
    "authorisation-status-change": "Authorisation status event published",
    "authorisation-change": "Authorisation change (submission or variation) published",
    "label-revision": "New label revision published",
    "safety-communication": "Safety communication published",
    "safety-communication-updated": "Safety communication updated",
}


class MedicinesMonitor(ClinicalMonitor):
    """Watch one medicine or substance for authorisation changes, label revisions and safety communications.

    A knowledge subscription (``SubscriptionStore``) registered in ``clinical_monitors``; each evaluation at a
    committed watermark reports the records its accepted identity matches reach. Items are keyed by record (and, for
    a communication, by revision), so a re-run over unchanged records delivers nothing (deduplicated) and every new
    authorisation event, label revision (with its section changes, both revisions cited) or communication update is
    delivered once, quoting the regulator. No scheduler: sources refresh through the source-pack runtime.
    """

    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.clinical_medicines import MedicinesService

        super().__init__(conn, initialize=initialize, now=now)
        self.medicines = MedicinesService(conn, initialize=initialize, now=self.now)

    def create_medicine(self, namespace, medicine, request_key, *, principal_id, scopes, delivery=None):
        from src.kb.clinical_records import _require_read

        _require_read(namespace, scopes)
        if not str(medicine or "").strip():
            raise ClinicalRecordError("invalid_medicine", "name the medicine or substance to watch")
        view_id = "medicines:" + digest([namespace, str(medicine).strip().casefold()])[:24]
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "clinical",
            "query": {"operation": "search", "kind": "medicines-monitor", "medicine": str(medicine).strip()},
            "filters": {"watch": "medicines-regulation"}, "cadence": {"trigger": "watermark"},
            "delivery": delivery or {"kind": "poll"},
        }, "medicines-monitor:" + request_key, principal_id=principal_id,
            scopes=_evidence_scopes(namespace, set(scopes)))
        self.conn.execute("INSERT INTO clinical_monitors VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, view_id])
        return {**created, "medicine": str(medicine).strip(), "refresh": refresh_schedule(self.conn)}

    def medicine_snapshot(self, namespace, medicine):
        from src.kb.clinical_medicines import STATUS_EVENTS

        scopes = {"operator"}
        _, rows, subjects = self.medicines.reach(namespace, medicine, scopes=scopes)
        items, stale, labels = [], set(), {}
        for row in rows:
            item = row["record"]
            cite = self.medicines._cite(namespace, row, scopes)
            match = subjects[row["provider"] + "\x00" + row["native_id"]]["match"]
            if row["record_kind"] == "marketing-authorisation":
                kind = ("authorisation-status-change" if item["event"]["kind"] in STATUS_EVENTS
                        else "authorisation-change")
                items.append({"id": f"authorisation:{row['record_id']}", "item": kind, "event": item["event"],
                              "procedure": item["procedure"], "submission": item.get("submission"),
                              "jurisdiction": item["jurisdiction"], "citation": cite, "identity_match": match})
            elif row["record_kind"] == "label-revision":
                labels.setdefault((row["provider"], item["document"]["id"]), []).append(row)
            elif row["record_kind"] == "safety-communication":
                history = self.records.history(namespace, row["record_id"], scopes=scopes)["revisions"]
                previous = None
                for revision in history:
                    content = revision["record"]
                    added = [u for u in content.get("updates") or []
                             if previous is None or u not in (previous.get("updates") or [])]
                    items.append({"id": f"dsc:{row['record_id']}:{revision['revision']}",
                                  "item": "safety-communication" if previous is None else
                                  "safety-communication-updated", "title": content["title"],
                                  "issued": content.get("issued"), "updates_added": added if previous else [],
                                  "named_substances": content.get("named_substances") or [],
                                  "citation": {**cite, "revision": revision["revision"]}, "identity_match": match})
                    previous = content
            if self.records.provider_state(namespace, _state_provider(item["provider"])).get("last_failure_ms"):
                stale.add(_state_provider(item["provider"]))
        for _, group in sorted(labels.items()):
            group.sort(key=lambda r: (r["record"]["document"].get("effective_date") or "",
                                      r["record"]["document"]["version"]))
            previous = None
            for row in group:
                entry = {"id": f"label:{row['record_id']}", "item": "label-revision",
                         "document": row["record"]["document"],
                         "citation": self.medicines._cite(namespace, row, scopes),
                         "identity_match": subjects[row["provider"] + "\x00" + row["native_id"]]["match"],
                         "section_changes": None}
                if previous is not None:
                    diff = self.medicines._store_diff(
                        namespace, self.records.get(namespace, previous["record_id"], scopes=scopes),
                        self.records.get(namespace, row["record_id"], scopes=scopes))
                    entry["section_changes"] = {"record_id": diff["record_id"], "from_revision": diff["from_revision"],
                                                "to_revision": diff["to_revision"], "changes": diff["changes"]}
                items.append(entry)
                previous = row
        return {"items": items, "coverage": {"complete": not stale, "stale_providers": sorted(stale)}}

    def run_medicine(self, subscription_id, watermark=None, *, principal_id, scopes):
        from src.kb.clinical_medicines import BOUNDARY
        from src.kb.clinical_records import _require_read

        scopes = set(scopes)
        self._monitor(subscription_id, principal_id)
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != "medicines-monitor":
            raise ClinicalRecordError("monitor_not_found", "subscription is not a medicines monitor")
        namespace = subscription["namespace"]
        _require_read(namespace, scopes)
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise ClinicalRecordError("watermark_uncommitted", "no committed watermark yet; source-pack runs and "
                                                                   "the maintenance orchestrator commit them")
            watermark = int(row[0])
        elif not self.conn.execute("SELECT 1 FROM knowledge_subscription_watermarks WHERE namespace=? AND watermark=?",
                                   [namespace, int(watermark)]).fetchone():
            self.subscriptions.commit_watermark(namespace, int(watermark), kind="ingestion",
                                                detail={"committed_by": "medicines-monitor"})
        result = self.medicine_snapshot(namespace, subscription["query"]["medicine"])
        evaluated = self.subscriptions.evaluate(subscription_id, int(watermark), result, principal_id=principal_id,
                                                scopes=_evidence_scopes(namespace, scopes),
                                                observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, after_json FROM knowledge_subscription_events "
                                    "WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(_classify_medicine(event_id, *row))
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": int(watermark),
                "medicine": subscription["query"]["medicine"], "notifications": notifications,
                "coverage": result["coverage"], "delivery": subscription["delivery"], "boundary": BOUNDARY}


def _state_provider(provider):
    return {"ema": "ema-epar", "openfda": "drugs-at-fda"}.get(provider, provider)


def _evidence_scopes(namespace, scopes):
    if "operator" in scopes:
        return set(scopes)
    return {READ_SCOPE, f"namespace:{namespace}:read"} | (
        set(scopes) & {"knowledge:subscriptions:read", "knowledge:subscriptions:write"})


def _classify_medicine(event_id, event_type, key, after):
    def note(kind, message, item):
        return {"contract": MEDICINES_CONTRACT, "event_id": event_id, "notification_id": f"{event_id}:{kind}",
                "kind": kind, "object": key, "message": message, "item": item,
                "note": "quoted as the regulator published it; no advice or verdict is given"}

    if event_type == "coverage-degraded":
        coverage = json.loads(after) if after else {}
        return [note("stale-source", "A source refresh failed for " + ", ".join(coverage.get("stale_providers") or [])
                     + "; nothing was changed.", coverage)]
    if event_type not in {"added", "changed"} or not after:
        return []
    item = json.loads(after)
    kind = item["item"]
    if kind not in MEDICINES_EVENTS:
        return []
    message = MEDICINES_EVENTS[kind]
    if kind.startswith("authorisation"):
        event = item["event"]
        message += (f": {item['jurisdiction']} {event['kind']} ({event.get('native_status')}) effective "
                    f"{event.get('effective_date') or 'date not published'}.")
    elif kind == "label-revision":
        changes = (item.get("section_changes") or {}).get("changes") or []
        message += (f": {item['document']['kind'].upper()} {item['document']['id']} version "
                    f"{item['document']['version']}" + (f", {len(changes)} section change(s)" if item.get(
                        "section_changes") else "") + ".")
    else:
        message += f": {item['title']}."
    return [note(kind, message, item)]
