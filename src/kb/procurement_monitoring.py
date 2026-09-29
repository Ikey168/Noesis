"""Monitor new notices, corrigenda, deadline changes, cancellations and awards.

Monitoring reuses the knowledge subscription owner (``SubscriptionStore``)
exactly as Funding & Grants does: a procurement monitor is a subscription
whose evaluated result set is the supplier profile's current view of each
procedure (per-lot state and verdict, next deadline with its published text,
notice revision) plus award rows for watched buyers or CPV branches.

There is no new scheduler. Refreshes are the ``procurement`` source pack's
own schedule; a monitor run evaluates at a *committed source-pack watermark*
(``source_pack_watermarks``), as of that watermark's commit time, so
replaying a watermark creates no new events. Before evaluating, assessments
whose notice or profile revision moved on are recomputed; shortlists and
workspaces pinned to changed procedures are reported stale until rebuilt or
refreshed. Every notification cites the changed notice and procedure
revision. Delivery uses the subscription's configured channel (poll by
default); nothing is sent to buyers.
"""

from __future__ import annotations

import json
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.kb.funding_monitoring import _next_delivery
from src.kb.funding_records import canonical, digest
from src.kb.procurement_records import READ_SCOPE, cpv_relation

CONTRACT = "noesis-procurement-notification-v1"
SOURCE_PACK = "procurement"
_DDL = """
CREATE TABLE IF NOT EXISTS procurement_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, profile_id TEXT NOT NULL,
 providers_json TEXT NOT NULL, watch_json TEXT NOT NULL, schedule_json TEXT NOT NULL);
"""


class MonitorError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _watch(value):
    value = dict(value or {})
    if set(value) - {"buyers", "cpv"}:
        raise MonitorError("invalid_watch", "watch uses buyers (names or identifiers) and cpv (codes)")
    return {"buyers": sorted({str(b) for b in value.get("buyers") or []}), "cpv": sorted({str(c) for c in value.get("cpv") or []})}


class ProcurementMonitor:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.procurement_ranking import ShortlistService
        from src.kb.subscriptions import SubscriptionStore

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        self.shortlists = ShortlistService(conn, initialize=initialize, now=self.now)
        self.eligibility = self.shortlists.eligibility
        self.notices = self.shortlists.notices

    def create(self, namespace, profile_id, request_key, *, providers, principal_id, scopes, watch=None, timezone=None,
               local_time="08:00", delivery=None):
        profile = self.eligibility.profiles.inspect(namespace, profile_id, principal_id=principal_id, scopes=scopes)
        timezone = timezone or profile["sections"]["preferences"].get("preferences.timezone", {}).get("value")
        if not timezone:
            raise MonitorError("timezone_required", "state a timezone (or the preferences.timezone fact); none is assumed")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise MonitorError("invalid_timezone", "unknown IANA timezone") from exc
        watch = _watch(watch)
        schedule = {"timezone": timezone, "local_time": local_time}
        _next_delivery(self.now(), schedule)
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "procurement",
            "query": {"operation": "search", "kind": "procurement-notices", "profile_id": profile_id,
                      "providers": sorted(providers), "watch": watch},
            "filters": {"profile_id": profile_id},
            "cadence": {"trigger": "watermark", "source_pack": SOURCE_PACK, "timezone": timezone, "local_time": local_time},
            "delivery": delivery or {"kind": "poll"},
        }, "procurement-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        self.conn.execute("INSERT INTO procurement_monitors VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, profile_id, canonical(sorted(providers)),
                           canonical(watch), canonical(schedule)])
        return {**created, "profile_id": profile_id, "watch": watch, "schedule": schedule,
                "refresh": "runs at committed procurement source-pack watermarks; no separate scheduler"}

    def _monitor(self, subscription_id, principal_id):
        row = self.conn.execute(
            "SELECT namespace, owner, profile_id, providers_json, watch_json, schedule_json FROM procurement_monitors WHERE subscription_id=?",
            [subscription_id]).fetchone()
        if not row or row[1] != principal_id:
            raise MonitorError("monitor_not_found", "procurement monitor is unavailable")
        return {"namespace": row[0], "profile_id": row[2], "providers": json.loads(row[3]), "watch": json.loads(row[4]),
                "schedule": json.loads(row[5])}

    def _source_watermark(self, watermark):
        row = self.conn.execute(
            "SELECT watermark, committed_at_ms FROM source_pack_watermarks WHERE pack_id=? AND (? IS NULL OR watermark=?) "
            "ORDER BY watermark DESC LIMIT 1", [SOURCE_PACK, watermark, watermark]).fetchone()
        if not row:
            raise MonitorError("watermark_uncommitted", "no committed procurement source-pack watermark to evaluate")
        return int(row[0]), int(row[1])

    @staticmethod
    def _watched(watch, buyer, codes):
        by_buyer = any(b.casefold() in {buyer["name"].casefold(), *(i["id"].casefold() for i in buyer.get("identifiers") or [])}
                       for b in watch["buyers"])
        by_cpv = any(cpv_relation(w, c) == "covers" for w in watch["cpv"] for c in codes)
        return by_buyer or by_cpv

    def snapshot(self, namespace, profile_id, providers, watch, *, principal_id, scopes, as_of_ms):
        """The profile's current per-procedure view plus watched award rows; the subscription diffs this."""
        shortlist = self.shortlists.build(namespace, profile_id, principal_id=principal_id, scopes=scopes, providers=providers,
                                          as_of_ms=as_of_ms)
        states = {p: self.notices.provider_state(namespace, p) for p in providers}
        stale = sorted(p for p, s in states.items() if s["stale"])
        grouped = {}
        for item in shortlist["items"]:
            grouped.setdefault(item["procedure_key"], []).append(item)
        items = []
        for key, lots in sorted(grouped.items()):
            first = lots[0]
            procedure = self.notices.get(namespace, key, scopes=scopes, as_of_ms=as_of_ms)
            codes = sorted({c["code"] for c in procedure["record"].get("classifications") or [] if c["scheme"] == "CPV"}
                           | {c["code"] for lot in procedure["record"].get("lots") or [] for c in lot.get("classifications") or []
                              if c["scheme"] == "CPV"})
            deadline = procedure["status"]["next_deadline"] or {}
            items.append({
                "id": key, "kind": "procedure", "title": first["title"], "provider": first["provider"],
                "revision": procedure["revision"], "notice_id": procedure["cause"]["notice_id"], "notice_stage": procedure["cause"]["stage"],
                "state": procedure["status"]["state"], "lots": {i["lot_id"] or "_": {"state": i["state"], "verdict": i["verdict"],
                                                                                    "bucket": i["bucket"]} for i in lots},
                "matching": any(i["bucket"] in {"apply_now", "consider"} for i in lots) or self._watched(watch, procedure["record"]["buyer"], codes),
                "next_deadline": deadline.get("instant") or deadline.get("date"), "deadline_text": deadline.get("text"),
                "rules_digest": digest(procedure["record"].get("requirements") or []), "uncertain": first["provider"] in stale,
            })
        for award in self.notices.award_history(namespace, scopes=scopes, limit=5000):
            if award["provider"] in providers and self._watched(watch, award["buyer"], award["cpv"]):
                items.append({"id": award["award_key"], "kind": "award", "title": f"Award: {award['buyer']['name']}",
                              "provider": award["provider"], "notice_id": award["notice_id"], "procedure_key": award["procedure_key"],
                              "date": award["date"], "suppliers": [s["name"] for s in award["suppliers"]], "value": award["value"],
                              "source_url": award["source_url"], "matching": True, "stage": award["stage"]})
        return {"items": items, "coverage": {"complete": not stale, "stale_providers": stale}}, shortlist

    def run(self, subscription_id, watermark=None, *, principal_id, scopes, as_of_ms=None):
        monitor = self._monitor(subscription_id, principal_id)
        namespace = monitor["namespace"]
        watermark, committed_at_ms = self._source_watermark(watermark)
        as_of_ms = as_of_ms or committed_at_ms
        # Current profile access is required on every run: revocation stops monitoring.
        self.eligibility.profiles.inspect(namespace, monitor["profile_id"], principal_id=principal_id, scopes=scopes)
        reassessed = self.eligibility.reassess(namespace, principal_id=principal_id, scopes=scopes)
        result, shortlist = self.snapshot(namespace, monitor["profile_id"], monitor["providers"], monitor["watch"],
                                          principal_id=principal_id, scopes=scopes, as_of_ms=as_of_ms)
        self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion",
                                            detail={"source_pack": SOURCE_PACK, "source_watermark": watermark},
                                            committed_at_ms=committed_at_ms)
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id, scopes=scopes,
                                                observed_at_ms=as_of_ms)
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events WHERE event_id=?", [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row, schedule=monitor["schedule"], now_ms=as_of_ms))
        affected = {n.get("procedure_key") for n in notifications if n.get("procedure_key")}
        views = self.conn.execute(
            "SELECT DISTINCT view_id FROM procurement_view_dependencies WHERE namespace=? AND owner=? ORDER BY view_id",
            [namespace, principal_id]).fetchall()
        stale_views = []
        for (view_id,) in views:
            status = self.notices.view_status(view_id)
            # Assessments are recomputed above (see "reassessed"); shortlists and
            # workspaces stay stale until rebuilt or refreshed by their owner.
            if not status["current"] and not view_id.startswith("procurement-assessment:"):
                stale_views.append({"view_id": view_id, "kind": view_id.split(":", 1)[0],
                                    "procedures": sorted({i["procedure_key"] for i in status["invalidations"]})})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark, "as_of_ms": as_of_ms,
                "shortlist_id": shortlist["shortlist_id"], "coverage": result["coverage"], "notifications": notifications,
                "reassessed": reassessed, "stale_views": stale_views,
                "workspaces_to_refresh": sorted(v["view_id"] for v in stale_views if v["kind"] == "procurement-workspace"),
                "changed_procedures": sorted(affected),
                "delivery": "configured subscription channel; no buyer or portal is contacted"}

    @staticmethod
    def _classify(event_id, event_type, key, before, after, *, schedule, now_ms):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None
        deliver = _next_delivery(now_ms, schedule)
        subject = after or before or {}

        def note(kind, message):
            cite = {"procedure_key": subject.get("procedure_key") or (None if key == "__coverage__" else key),
                    "notice_id": subject.get("notice_id"), "revision": subject.get("revision")}
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id, "kind": kind,
                    **cite, "message": message, "deliver_not_before": deliver, "timezone": schedule["timezone"]}

        if event_type == "coverage-degraded":
            stale = ", ".join((after or {}).get("stale_providers") or [])
            return [note("stale_source", f"Refresh failed for {stale}; affected notices are marked uncertain, not closed.")]
        if subject.get("kind") == "award":
            if event_type == "added":
                return [note("award", f"Award notice {after['notice_id']}: {after['title']} to {', '.join(after['suppliers']) or 'unstated supplier'} "
                                      f"({after['date']}). Context only; it does not open any procedure.")]
            return []
        if event_type == "added":
            if after.get("matching"):
                return [note("new_matching_notice", f"New matching notice {after['notice_id']}: {after['title']} ({after['state']}).")]
            return []
        if event_type == "removed":
            return [note("no_longer_listed", f"{before['title']} is no longer in the result set; it is not treated as closed.")]
        result = []
        if before["revision"] != after["revision"] and after["notice_stage"] == "corrigendum":
            result.append(note("corrigendum", f"Corrigendum {after['notice_id']} changes {after['title']} (procedure revision {after['revision']})."))
        if before["next_deadline"] != after["next_deadline"] and after["state"] not in {"closed", "cancelled", "awarded"}:
            result.append(note("deadline_change", f"{after['title']}: next deadline '{before.get('deadline_text')}' -> '{after.get('deadline_text')}' "
                                                  f"(notice {after['notice_id']}, revision {after['revision']})."))
        if before["state"] != "cancelled" and after["state"] == "cancelled":
            result.append(note("cancellation", f"{after['title']} was cancelled (notice {after['notice_id']})."))
        if before["state"] != "awarded" and after["state"] == "awarded":
            result.append(note("award", f"{after['title']} was awarded; see its award notice."))
        if before["rules_digest"] != after["rules_digest"]:
            result.append(note("requirements_changed", f"Requirements changed for {after['title']} (revision {after['revision']})."))
        verdicts_before = {k: v["verdict"] for k, v in before["lots"].items()}
        verdicts_after = {k: v["verdict"] for k, v in after["lots"].items()}
        if verdicts_before != verdicts_after:
            changed = ", ".join(f"{k}: {verdicts_before.get(k)} -> {v}" for k, v in verdicts_after.items() if verdicts_before.get(k) != v)
            result.append(note("eligibility_changed", f"{after['title']}: {changed}."))
        if after.get("uncertain") and not before.get("uncertain"):
            result.append(note("stale_source", f"{after['title']}: source refresh failed; status uncertain, not closed."))
        return result

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        self._monitor(subscription_id, principal_id)
        if READ_SCOPE not in scopes:
            raise MonitorError("unauthorized", "procurement read scope is required")
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
