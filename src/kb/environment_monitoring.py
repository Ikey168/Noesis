"""Watch a place for threshold crossings, grid unavailability, permits/releases and new vintages (E11).

Monitoring reuses ``SubscriptionStore``: a monitor is a knowledge
subscription whose evaluated result set is the place's current view
(threshold crossings, unavailability events, facility permits/releases and
series vintages), each item carrying the record revision or vintage it came
from. Evaluation happens only at a *committed* watermark — the ingestion
watermarks the maintenance orchestrator commits after source-pack runs (or a
source run commits) — so there is no scheduler here and replaying a
watermark creates no new events.

Thresholds are the user's. A threshold may cite a source (document and quote)
if the user wants it shown next to one; the pack never asserts a legal limit
of its own. Only ``observation`` values are compared with thresholds; model
output and forecasts are never treated as observed exceedances.
"""

from __future__ import annotations

import json
from decimal import Decimal

from src.kb import environment_records as er
from src.kb.environment_records import READ_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.environment_store import GEO_SCOPES, EnvironmentStoreError, authorize

CONTRACT = "noesis-environment-notification-v1"
_OPS = {"gt": lambda a, b: a > b, "gte": lambda a, b: a >= b, "lt": lambda a, b: a < b, "lte": lambda a, b: a <= b}
WATCHES = ("thresholds", "unavailability", "facilities", "vintages")
_DDL = """
CREATE TABLE IF NOT EXISTS environment_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, place_id TEXT NOT NULL,
 thresholds_json TEXT NOT NULL, watch_json TEXT NOT NULL, radius_m DOUBLE NOT NULL);
"""


def _threshold(item, index):
    if not isinstance(item, dict) or item.get("op") not in _OPS:
        raise EnvironmentStoreError("invalid_threshold", "thresholds need indicator, op (gt/gte/lt/lte), value and unit")
    indicator, unit = str(item.get("indicator") or "").strip(), item.get("unit")
    value = er.decimal_text(str(item.get("value")), f"thresholds[{index}].value") if item.get("value") is not None else None
    if not indicator or value is None or er.unit_expression(unit) is None:
        raise EnvironmentStoreError("invalid_threshold", "thresholds need an indicator, a decimal value and a known unit")
    basis = item.get("basis") or "user-declared"
    if basis != "user-declared":
        cited = basis.get("cited") if isinstance(basis, dict) else None
        if not isinstance(cited, dict) or not (cited.get("document_id") or cited.get("source_url")) or not cited.get("quote"):
            raise EnvironmentStoreError("invalid_threshold", "a cited threshold names its source document/URL and quote")
        basis = {"cited": {k: cited.get(k) for k in ("document_id", "source_url", "quote")}}
    return {"threshold_id": f"t{index + 1}", "indicator": indicator.casefold(), "op": item["op"], "value": value,
            "unit": unit, "basis": basis,
            "notice": "user threshold" if basis == "user-declared" else "user threshold citing a source; not asserted by the pack"}


class EnvironmentMonitor:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.environment_places import EnvironmentDossiers
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.dossiers = EnvironmentDossiers(conn, initialize=initialize, now=now)
        self.store = self.dossiers.store
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(self, namespace, request_key, *, place_id, principal_id, scopes, thresholds=(), watch=WATCHES,
               radius_m=5000, delivery=None):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if set(watch) - set(WATCHES) or not watch:
            raise EnvironmentStoreError("invalid_watch", f"watch is a non-empty subset of {WATCHES}")
        parsed = [_threshold(item, index) for index, item in enumerate(thresholds or [])]
        located, _ = self.dossiers._resolve(namespace, place_id=place_id, mention=None, as_of_ms=None)
        if located["point"] is None:
            raise EnvironmentStoreError("invalid_place", "the place has no point geometry")
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "environment",
            "query": {"operation": "search", "kind": "environment-place", "place_id": place_id,
                      "thresholds": parsed, "watch": sorted(watch), "radius_m": radius_m},
            "filters": {"place_id": place_id}, "cadence": {"trigger": "watermark"},
            "delivery": delivery or {"kind": "poll"},
        }, "environment-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        self.conn.execute("INSERT INTO environment_monitors VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, place_id, canonical(parsed),
                           canonical(sorted(watch)), float(radius_m)])
        return {**created, "thresholds": parsed, "watch": sorted(watch),
                "refresh": "source-pack schedules and the maintenance orchestrator commit the watermarks this monitor evaluates"}

    def _monitor(self, subscription_id, principal_id):
        row = self.conn.execute("SELECT namespace, owner, place_id, thresholds_json, watch_json, radius_m FROM "
                                "environment_monitors WHERE subscription_id=?", [subscription_id]).fetchone()
        if not row or row[1] != principal_id:
            raise EnvironmentStoreError("monitor_not_found", "environment monitor is unavailable")
        return {"namespace": row[0], "place_id": row[2], "thresholds": json.loads(row[3]), "watch": json.loads(row[4]),
                "radius_m": row[5]}

    def snapshot(self, monitor, *, scopes, principal_id):
        namespace = monitor["namespace"]
        read = set(scopes) | {READ_SCOPE}
        located, _ = self.dossiers._resolve(namespace, place_id=monitor["place_id"], mention=None, as_of_ms=None)
        point = located["point"]["geometry"]["coordinates"]
        items, stale_providers = [], set()
        nearby_records = set()
        for record_type in ("station", "facility"):
            for record in self.store.records(namespace, scopes=read, record_type=record_type):
                geometry_id = self.dossiers._place_point(namespace, record["record_id"])
                if geometry_id is None:
                    continue
                relation = self.store.geo.relation(namespace, "proximity", geometry_id, point, scopes=GEO_SCOPES,
                                                   principal_id=principal_id, tolerance_m=float(monitor["radius_m"]))
                if relation["result"]["within_tolerance"]:
                    nearby_records.add((record["record_id"], record["provider"], record["native_id"], record_type))
                    if self.store.provider_state(namespace, record["provider"])["stale"]:
                        stale_providers.add(record["provider"])
        series_ids = []
        for record_id, provider, native, record_type in sorted(nearby_records):
            series_ids += [(sid, record_type, record_id) for sid in self.dossiers._series_at(namespace, f"{provider}:{native}")]
        if "facilities" in monitor["watch"]:
            for record_id, _, _, record_type in sorted(nearby_records):
                if record_type != "facility":
                    continue
                current = self.store.record(namespace, record_id, scopes=read)
                content = current["content"]
                releases = [self.store.select_vintage(namespace, sid, scopes=read)[0] for sid, kind, parent in series_ids
                            if parent == record_id]
                items.append({"id": f"facility:{record_id}", "kind": "facility", "title": content["title"],
                              "revision_id": current["revision_id"], "permits_digest": digest(content.get("permits") or []),
                              "permits": content.get("permits") or [],
                              "release_vintages": sorted(v["vintage_id"] for v in releases if v)})
        if "vintages" in monitor["watch"] or "thresholds" in monitor["watch"]:
            for sid, _, _ in series_ids:
                series = self.store.series(namespace, sid, scopes=read)
                vintage = series["vintage"]
                if vintage is None:
                    continue
                if "vintages" in monitor["watch"]:
                    items.append({"id": f"vintage:{sid}", "kind": "vintage", "title": series["title"],
                                  "vintage_id": vintage["vintage_id"], "status": vintage["status"],
                                  "series_kind": series["kind"]})
                if "thresholds" in monitor["watch"] and series["kind"] == "observation":
                    code = str((series["indicator"] or {}).get("code") or "").casefold()
                    for threshold in monitor["thresholds"]:
                        if threshold["indicator"] != code:
                            continue
                        for value in series["values"]:
                            if value["value"] is None:
                                continue
                            converted = er.normalise(value["value"], value["unit"], target=threshold["unit"])
                            if converted is None:
                                continue
                            if _OPS[threshold["op"]](Decimal(converted["value"]), Decimal(threshold["value"])):
                                items.append({"id": f"threshold:{threshold['threshold_id']}:{sid}:{value['start']}",
                                              "kind": "threshold", "title": series["title"], "period_start": value["start"],
                                              "value": value["value"], "unit": value["unit"],
                                              "value_in_threshold_unit": converted["value"], "threshold": threshold,
                                              "status": value["status"], "vintage_id": vintage["vintage_id"],
                                              "series_kind": "observation"})
        if "unavailability" in monitor["watch"]:
            zones = self.dossiers.ensure_zones(namespace, principal_id=principal_id)
            for zone in zones:
                relation = self.store.geo.relation(namespace, "contains", zone["geometry_id"], point, scopes=GEO_SCOPES,
                                                   principal_id=principal_id)
                if not relation["result"]["contains"]:
                    continue
                codes = {zone["code"], zone["name"], *zone["members"]}
                for record in self.store.records(namespace, scopes=read, record_type="grid_event"):
                    content = record["content"]
                    if content["event_type"] != "unavailability" or content["bidding_zone"].get("code") not in codes:
                        continue
                    if self.store.provider_state(namespace, record["provider"])["stale"]:
                        stale_providers.add(record["provider"])
                    details = content["unavailability"]
                    items.append({"id": f"unavailability:{record['record_id']}", "kind": "unavailability",
                                  "title": content["title"], "revision_id": record["revision_id"],
                                  "document_revision": (content.get("document") or {}).get("revision"),
                                  "unavailability_kind": details["kind"], "start": details["start"], "end": details["end"],
                                  "status": details["status"], "reason": details["reason"]})
        stale = sorted(stale_providers)
        return {"items": items, "coverage": {"complete": not stale, "stale_providers": stale}}

    def run(self, subscription_id, watermark=None, *, principal_id, scopes):
        monitor = self._monitor(subscription_id, principal_id)
        namespace = monitor["namespace"]
        if watermark is None:
            row = self.conn.execute("SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                                    [namespace]).fetchone()
            if row is None or row[0] is None:
                raise EnvironmentStoreError("watermark_uncommitted",
                                            "no committed watermark yet; source-pack runs and the maintenance orchestrator commit them")
            watermark = int(row[0])
        result = self.snapshot(monitor, scopes=scopes, principal_id=principal_id)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=self.now())
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute("SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                                    "WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row))
        dossiers = []
        for dossier_id, pins in self.conn.execute("SELECT dossier_id, pins_json FROM environment_dossiers WHERE namespace=? "
                                                  "AND owner=? ORDER BY dossier_id", [namespace, principal_id]).fetchall():
            reasons = self.dossiers.staleness(namespace, json.loads(pins))
            if reasons:
                dossiers.append({"dossier_id": dossier_id, "stale": True, "reasons": len(reasons)})
        evidence = []
        if self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='environment_obligation_evidence'").fetchone():
            from src.kb.environment_identity import EnvironmentIdentity

            identity = EnvironmentIdentity(self.conn, initialize=False)
            for (evidence_id,) in self.conn.execute("SELECT evidence_id FROM environment_obligation_evidence WHERE namespace=? "
                                                    "ORDER BY evidence_id", [namespace]).fetchall():
                item = identity.evidence(namespace, evidence_id, scopes=scopes)
                if item["stale"]:
                    evidence.append({"evidence_id": evidence_id, "stale": True, "newer_vintages": item["newer_vintages"]})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "coverage": result["coverage"], "notifications": notifications,
                "stale_dossiers": dossiers, "stale_obligation_evidence": evidence,
                "delivery": "configured subscription channel (poll by default)"}

    @staticmethod
    def _classify(event_id, event_type, key, before, after):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None
        item = after or before or {}

        def note(kind, message, cites):
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id, "kind": kind,
                    "object": key, "message": message, "cites": cites}

        if event_type == "coverage-degraded":
            return [note("stale_source", "Refresh failed or never succeeded for " + ", ".join(after.get("stale_providers") or [])
                         + "; affected items are uncertain, not removed.", {})]
        if event_type == "removed":
            return [note("no_longer_in_view", f"{before.get('title')} left the monitored view (not treated as ended).",
                         {"revision_id": before.get("revision_id"), "vintage_id": before.get("vintage_id")})]
        kind = item.get("kind")
        if kind == "threshold" and event_type == "added":
            threshold = after["threshold"]
            return [note("threshold_crossed",
                         f"{after['title']}: {after['value']} {after['unit']} at {after['period_start']} "
                         f"{threshold['op']} {threshold['value']} {threshold['unit']} ({threshold['notice']}; value status {after['status']}).",
                         {"vintage_id": after["vintage_id"], "threshold_basis": threshold["basis"]})]
        if kind == "unavailability":
            label = "new_unavailability" if event_type == "added" else "updated_unavailability"
            return [note(label, f"{after['title']}: {after['unavailability_kind']} {after['start']}–{after['end']} "
                                f"(document revision {after['document_revision']}), status {after['status']}.",
                         {"revision_id": after["revision_id"]})]
        if kind == "facility":
            if event_type == "added":
                return [note("facility_in_view", f"{after['title']} is within the monitored radius.",
                             {"revision_id": after["revision_id"]})]
            result = []
            if before["permits_digest"] != after["permits_digest"]:
                result.append(note("permit_changed", f"{after['title']}: permits changed.", {"revision_id": after["revision_id"]}))
            if before["release_vintages"] != after["release_vintages"]:
                result.append(note("releases_changed", f"{after['title']}: new release vintage.",
                                   {"vintages": after["release_vintages"]}))
            return result
        if kind == "vintage":
            if event_type == "added":
                return [note("series_in_view", f"{after['title']} ({after['series_kind']}) is monitored.",
                             {"vintage_id": after["vintage_id"]})]
            return [note("new_vintage", f"{after['title']}: new vintage {after['vintage_id']} ({after['status']}) "
                                        f"replaces {before['vintage_id']} ({before['status']}).",
                         {"vintage_id": after["vintage_id"], "previous_vintage_id": before["vintage_id"]})]
        return []

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        self._monitor(subscription_id, principal_id)
        if READ_SCOPE not in scopes and "operator" not in scopes:
            raise EnvironmentStoreError("unauthorized", "environment read scope is required")
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)

