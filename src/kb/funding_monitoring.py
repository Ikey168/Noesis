"""Monitor new calls, eligibility changes and funding deadlines.

Monitoring reuses ``SubscriptionStore``: a funding monitor is a knowledge
subscription whose evaluated result set is the profile's current view of each
opportunity (state, verdict, next deadline, rule digest). Committed
watermarks give replay-safe event IDs; replaying a watermark creates no new
events. A failed or partial provider refresh degrades coverage and marks the
affected items uncertain rather than removing or closing them. Delivery uses
the subscription's configured channel (poll by default) and its existing
outbox; nothing is sent to funders.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.kb.funding_records import READ_SCOPE, canonical, digest

CONTRACT = "noesis-funding-notification-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS funding_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 profile_id TEXT NOT NULL, providers_json TEXT NOT NULL, schedule_json TEXT NOT NULL);
"""


class MonitorError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _next_delivery(now_ms, schedule):
    zone = ZoneInfo(schedule["timezone"])
    local = datetime.fromtimestamp(now_ms / 1000, tz=UTC).astimezone(zone)
    hour, minute = (int(v) for v in schedule["local_time"].split(":"))
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target < local:
        target += timedelta(days=1)
    return target.isoformat()


class FundingMonitor:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.funding_ranking import ShortlistService
        from src.kb.subscriptions import SubscriptionStore

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        self.shortlists = ShortlistService(conn, initialize=initialize, now=self.now)
        self.eligibility = self.shortlists.eligibility

    def create(self, namespace, profile_id, request_key, *, providers, principal_id, scopes,
               timezone=None, local_time="08:00", delivery=None):
        profile = self.eligibility.profiles.inspect(namespace, profile_id, principal_id=principal_id, scopes=scopes)
        timezone = timezone or profile["sections"]["preferences"].get("preferences.timezone", {}).get("value")
        if not timezone:
            raise MonitorError("timezone_required", "state a timezone (or the preferences.timezone fact); none is assumed")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise MonitorError("invalid_timezone", "unknown IANA timezone") from exc
        schedule = {"timezone": timezone, "local_time": local_time}
        _next_delivery(self.now(), schedule)
        created = self.subscriptions.create({
            "namespace": namespace, "domain": "funding",
            "query": {"operation": "search", "kind": "funding-opportunities", "profile_id": profile_id,
                      "providers": sorted(providers)},
            "filters": {"profile_id": profile_id},
            "cadence": {"trigger": "watermark", "timezone": timezone, "local_time": local_time},
            "delivery": delivery or {"kind": "poll"},
        }, "funding-monitor:" + request_key, principal_id=principal_id, scopes=scopes)
        self.conn.execute("INSERT INTO funding_monitors VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [created["subscription_id"], namespace, principal_id, profile_id,
                           canonical(sorted(providers)), canonical(schedule)])
        return {**created, "profile_id": profile_id, "schedule": schedule}

    def _monitor(self, subscription_id, principal_id):
        row = self.conn.execute(
            "SELECT namespace, owner, profile_id, providers_json, schedule_json FROM funding_monitors WHERE subscription_id=?",
            [subscription_id]).fetchone()
        if not row or row[1] != principal_id:
            raise MonitorError("monitor_not_found", "funding monitor is unavailable")
        return {"namespace": row[0], "profile_id": row[2], "providers": json.loads(row[3]), "schedule": json.loads(row[4])}

    def snapshot(self, namespace, profile_id, providers, *, principal_id, scopes, as_of_ms):
        """The profile's current per-opportunity view that the subscription diffs."""
        shortlist = self.shortlists.build(namespace, profile_id, principal_id=principal_id, scopes=scopes,
                                          providers=providers, as_of_ms=as_of_ms)
        states = {p: self.eligibility.opportunities.provider_state(namespace, p) for p in providers}
        stale = sorted(p for p, s in states.items() if s["stale"])
        items = []
        for item in shortlist["items"]:
            opportunity = self.eligibility.opportunities.get(namespace, item["opportunity_id"], scopes=scopes, as_of_ms=as_of_ms)
            items.append({
                "id": item["opportunity_id"], "title": item["title"], "provider": item["provider"],
                "revision": item["revision"], "state": item["state"], "verdict": item["verdict"],
                "matching": item["bucket"] in {"apply_now", "consider"}, "bucket": item["bucket"],
                "next_deadline": (item["next_deadline"] or {}).get("instant") or (item["next_deadline"] or {}).get("date"),
                "rules_digest": digest(opportunity["record"].get("requirements") or []),
                "uncertain": item["provider"] in stale,
            })
        return {"items": items, "coverage": {"complete": not stale, "stale_providers": stale}}, shortlist

    def run(self, subscription_id, watermark, *, principal_id, scopes, as_of_ms=None):
        monitor = self._monitor(subscription_id, principal_id)
        namespace, as_of_ms = monitor["namespace"], as_of_ms or self.now()
        # Current profile access is required on every run: revocation stops monitoring.
        self.eligibility.profiles.inspect(namespace, monitor["profile_id"], principal_id=principal_id, scopes=scopes)
        result, shortlist = self.snapshot(namespace, monitor["profile_id"], monitor["providers"],
                                          principal_id=principal_id, scopes=scopes, as_of_ms=as_of_ms)
        self.subscriptions.commit_watermark(namespace, watermark, kind="ingestion",
                                            detail={"funding_snapshot": digest(result)})
        evaluated = self.subscriptions.evaluate(subscription_id, watermark, result, principal_id=principal_id,
                                                scopes=scopes, observed_at_ms=as_of_ms)
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events WHERE event_id=?",
                [event_id]).fetchone()
            notifications.extend(self._classify(event_id, *row, schedule=monitor["schedule"], now_ms=as_of_ms))
        affected = sorted({n["opportunity_id"] for n in notifications if n.get("opportunity_id")})
        workspaces = sorted({w for w, o in self.conn.execute(
            """SELECT view_id, opportunity_id FROM funding_view_dependencies WHERE namespace=? AND owner=?
               AND view_id LIKE 'funding-workspace:%'""", [namespace, principal_id]).fetchall() if o in affected})
        return {"subscription_id": subscription_id, "status": evaluated["status"], "watermark": watermark,
                "shortlist_id": shortlist["shortlist_id"], "coverage": result["coverage"],
                "notifications": notifications, "workspaces_to_reassess": workspaces,
                "delivery": "configured subscription channel; no funder contact"}

    @staticmethod
    def _classify(event_id, event_type, key, before, after, *, schedule, now_ms):
        before = json.loads(before) if before else None
        after = json.loads(after) if after else None
        deliver = _next_delivery(now_ms, schedule)

        def note(kind, message):
            return {"contract": CONTRACT, "notification_id": f"{event_id}:{kind}", "event_id": event_id,
                    "kind": kind, "opportunity_id": None if key == "__coverage__" else key,
                    "message": message, "deliver_not_before": deliver, "timezone": schedule["timezone"]}

        if event_type == "coverage-degraded":
            stale = ", ".join((after or {}).get("stale_providers") or [])
            return [note("stale_source", f"Refresh failed for {stale}; affected calls are marked uncertain, not closed.")]
        if event_type == "added":
            if after.get("matching"):
                return [note("new_matching_call", f"New matching call: {after['title']} ({after['verdict']}).")]
            return []
        if event_type == "removed":
            return [note("no_longer_listed", f"{before['title']} is no longer in the result set; not treated as closed.")]
        result = []
        if before["rules_digest"] != after["rules_digest"]:
            result.append(note("rules_changed", f"Requirements changed for {after['title']} (revision {after['revision']})."))
        if before["verdict"] != after["verdict"]:
            result.append(note("eligibility_changed", f"{after['title']}: {before['verdict']} → {after['verdict']}."))
        if before["next_deadline"] != after["next_deadline"] and after["state"] != "closed":
            result.append(note("deadline_shift", f"{after['title']}: next deadline {before['next_deadline']} → {after['next_deadline']}."))
        if before["state"] != "closed" and after["state"] == "closed":
            result.append(note("closed", f"{after['title']} is closed."))
        if after.get("uncertain") and not before.get("uncertain"):
            result.append(note("stale_source", f"{after['title']}: source refresh failed; status uncertain."))
        return result

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        self._monitor(subscription_id, principal_id)
        if READ_SCOPE not in scopes:
            raise MonitorError("unauthorized", "funding read scope is required")
        return self.subscriptions.poll(subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor)
