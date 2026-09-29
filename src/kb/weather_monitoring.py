"""Watch a place, station, provider parameter or warning type through subscriptions (WX11, #2174).

A monitor is a knowledge subscription (:class:`src.kb.subscriptions.SubscriptionStore`);
there is no scheduler. It is evaluated only at a **committed** watermark (the
source-pack runtime and the maintenance orchestrator commit them), and
replaying a watermark creates no new events. Each evaluation's result set is
the watched view:

* ``place`` — warning chains whose areas contain the place (Geospatial
  containment, as in :meth:`WeatherQueries.warnings_in_force`);
* ``station`` — the station's current observation reports and its latest
  location vintage;
* ``provider-parameter`` — the provider's forecast issuances that carry the
  parameter;
* ``warning-event`` — warning chains of an event type at or above a severity.

Events: ``warning_issued``, ``warning_updated``, ``warning_cancelled``,
``forecast_issued``, ``observation_corrected``, ``station_relocated``. The
first evaluation is a baseline and raises none of them. Every notification
quotes the issuer's own text and cites the message or revision. No advice is
added. The evaluation time is the latest acquisition time on record, never the
wall clock, and "current" follows the source's own time, so late older data
raises no correction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb import weather_records as wr
from src.kb.weather_records import READ_SCOPE, WRITE_SCOPE, canonical
from src.kb.weather_store import (
    WeatherError,
    WeatherStore,
    _table,
    authorize,
    require_ready,
)

CONTRACT = "noesis-weather-notification-v1"
TARGETS = ("place", "station", "provider-parameter", "warning-event")
SEVERITIES = ("Unknown", "Minor", "Moderate", "Severe", "Extreme")
_DDL = """
CREATE TABLE IF NOT EXISTS weather_monitors(
 subscription_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, target_json TEXT NOT NULL);
"""


def _target(target: Any) -> dict[str, Any]:
    if not isinstance(target, Mapping) or target.get("kind") not in TARGETS:
        raise WeatherError("invalid_target", f"target kind is one of {TARGETS}")
    kind = target["kind"]
    if kind == "place":
        point = target.get("point")
        if not isinstance(point, list) or len(point) != 2:
            raise WeatherError(
                "invalid_target", "a place target needs point [lon, lat]"
            )
        return {"kind": kind, "point": [float(point[0]), float(point[1])]}
    if kind == "station":
        wr.station_ref(*str(target.get("station") or ":").split(":", 1))
        return {"kind": kind, "station": target["station"]}
    if kind == "provider-parameter":
        if target.get("provider") not in wr.PROVIDERS or not target.get("parameter"):
            raise WeatherError(
                "invalid_target",
                "provider-parameter targets name a provider and a parameter",
            )
        return {
            "kind": kind,
            "provider": target["provider"],
            "parameter": str(target["parameter"]),
        }
    if not target.get("event") or target.get("min_severity", "Minor") not in SEVERITIES:
        raise WeatherError(
            "invalid_target",
            f"warning-event targets name an event and a severity in {SEVERITIES}",
        )
    return {
        "kind": kind,
        "event": str(target["event"]),
        "min_severity": target.get("min_severity", "Minor"),
    }


class WeatherMonitor:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Any = None) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = WeatherStore(conn, initialize=False, now=now)
        if initialize:
            conn.execute(_DDL)
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        target: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        delivery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_ready(self.conn, namespace)
        parsed = _target(target)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "weather",
                "query": {
                    "operation": "search",
                    "kind": "weather-monitor",
                    "target": parsed,
                },
                "filters": {"target": parsed},
                "cadence": {"trigger": "watermark"},
                "delivery": dict(delivery or {"kind": "poll"}),
            },
            "weather-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        self.conn.execute(_DDL)
        self.conn.execute(
            "INSERT INTO weather_monitors VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            [created["subscription_id"], namespace, principal_id, canonical(parsed)],
        )
        return {
            **created,
            "target": parsed,
            "refresh": "source-pack schedules and the maintenance orchestrator commit the watermarks this "
            "monitor evaluates; there is no separate scheduler",
        }

    def _monitor(self, subscription_id: str, principal_id: str) -> dict[str, Any]:
        row = None
        if _table(self.conn, "weather_monitors"):
            row = self.conn.execute(
                "SELECT namespace, owner, target_json FROM weather_monitors WHERE subscription_id=?",
                [subscription_id],
            ).fetchone()
        if not row or row[1] != principal_id:
            raise WeatherError("monitor_not_found", "weather monitor is unavailable")
        return {
            "subscription_id": subscription_id,
            "namespace": row[0],
            "target": json.loads(row[2]),
        }

    # ------------------------------------------------------------------ view

    def _evaluation_time(self, namespace: str) -> str:
        row = self.conn.execute(
            "SELECT max(retrieved_at_ms) FROM weather_revisions WHERE namespace=?",
            [namespace],
        ).fetchone()
        return wr.iso(int(row[0]))

    def _chains(
        self,
        namespace: str,
        scopes: set[str],
        principal_id: str,
        point: list[float] | None,
    ) -> list[dict[str, Any]]:
        from src.kb.weather_queries import WeatherQueries

        queries = WeatherQueries(self.conn, now=self.store.now)
        as_of = self._evaluation_time(namespace)
        if point is not None:
            answer = queries.warnings_in_force(
                namespace,
                scopes=scopes,
                principal_id=principal_id,
                place={"point": point},
                as_of=as_of,
            )
            return answer["in_force"] + answer["not_in_force"]
        messages = [
            {**w["content"], "revision_id": w["revision_id"]}
            for w in self.store.currents(
                namespace, record_type="warning", scopes=scopes
            )
        ]
        result = []
        for chain in WeatherQueries.chains(messages):
            latest = chain[-1]
            result.append(
                {
                    "message": latest,
                    "chain": [
                        {
                            "identifier": m["identifier"],
                            "msg_type": m["msg_type"],
                            "sent": m["sent"],
                            "revision_id": m["revision_id"],
                            "severity": m.get("severity"),
                        }
                        for m in chain
                    ],
                }
            )
        return result

    def snapshot(
        self, monitor: Mapping[str, Any], *, scopes: Iterable[str], principal_id: str
    ) -> dict[str, Any]:
        namespace, target = monitor["namespace"], monitor["target"]
        scopes = set(scopes) | {READ_SCOPE}
        items: list[dict[str, Any]] = []
        if target["kind"] in {"place", "warning-event"}:
            for chain in self._chains(
                namespace,
                scopes,
                principal_id,
                target.get("point") if target["kind"] == "place" else None,
            ):
                message = chain["message"]
                if target["kind"] == "warning-event":
                    severity = message.get("severity") or "Unknown"
                    if message.get("event") != target["event"] or SEVERITIES.index(
                        severity if severity in SEVERITIES else "Unknown"
                    ) < SEVERITIES.index(target["min_severity"]):
                        continue
                root = chain["chain"][0]["identifier"]
                latest = chain["chain"][-1]
                items.append(
                    {
                        "id": f"warning:{root}",
                        "kind": "warning",
                        "latest_identifier": latest["identifier"],
                        "msg_type": message["msg_type"],
                        "sent": message["sent"],
                        "revision_id": latest["revision_id"],
                        "state": chain.get("state"),
                        "quote": {
                            k: message.get(k)
                            for k in (
                                "sender",
                                "event",
                                "severity",
                                "headline",
                                "description",
                                "instruction",
                                "onset",
                                "expires",
                                "issuer",
                            )
                            if message.get(k) is not None
                        },
                    }
                )
        elif target["kind"] == "station":
            for report in self.store.currents(
                namespace,
                record_type="observation_report",
                scopes=scopes,
                subject_keys=[target["station"]],
            ):
                items.append(
                    {
                        "id": f"observation:{report['record_id']}",
                        "kind": "observation",
                        "observed_at": report["content"]["observed_at"],
                        "revision_id": report["revision_id"],
                        "change_kind": report["change_kind"],
                        "correction": report["content"].get("correction"),
                        "raw_text": report["content"].get("raw_text"),
                    }
                )
            vintages = self.store.location_vintages(
                namespace, target["station"], scopes=scopes
            )
            if vintages:
                latest = vintages[-1]
                items.append(
                    {
                        "id": f"location:{target['station']}",
                        "kind": "location",
                        "valid_from": latest["content"]["valid_from"],
                        "revision_id": latest["revision_id"],
                        "latitude": latest["content"]["latitude"],
                        "longitude": latest["content"]["longitude"],
                    }
                )
        else:
            for issuance in self.store.currents(
                namespace,
                record_type="forecast_issuance",
                scopes=scopes,
                provider=target["provider"],
            ):
                if not self.store.elements(
                    issuance["revision_id"], parameter=target["parameter"]
                ):
                    continue
                content = issuance["content"]
                items.append(
                    {
                        "id": f"issuance:{issuance['record_id']}",
                        "kind": "issuance",
                        "issued_at": content["issued_at"],
                        "product": content["product"],
                        "location": content["location"]["ref"],
                        "revision_id": issuance["revision_id"],
                    }
                )
        stale = sorted(
            {
                p
                for (p,) in self.conn.execute(
                    "SELECT provider FROM weather_provider_state WHERE namespace=? AND last_failure_ms IS NOT NULL AND "
                    "(last_success_ms IS NULL OR last_failure_ms > last_success_ms)",
                    [namespace],
                ).fetchall()
            }
        )
        return {
            "items": items,
            "coverage": {"complete": not stale, "stale_providers": stale},
        }

    # ------------------------------------------------------------------ runs

    def run(
        self,
        subscription_id: str,
        watermark: int | None = None,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        monitor = self._monitor(subscription_id, principal_id)
        authorize(monitor["namespace"], scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [monitor["namespace"]],
            ).fetchone()
            if row is None or row[0] is None:
                raise WeatherError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs commit them",
                )
            watermark = int(row[0])
        baseline = (
            self.subscriptions.inspect(
                subscription_id, principal_id=principal_id, scopes=scopes
            )["last_watermark"]
            is None
        )
        result = self.snapshot(monitor, scopes=scopes, principal_id=principal_id)
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            result,
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=self.store.now(),
        )
        notifications = []
        if not baseline:
            for event_id in evaluated.get("event_ids", []):
                row = self.conn.execute(
                    "SELECT event_type, object_key, before_json, after_json FROM "
                    "knowledge_subscription_events WHERE event_id=?",
                    [event_id],
                ).fetchone()
                notifications.extend(self._classify(event_id, *row))
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "baseline": baseline,
            "coverage": result["coverage"],
            "notifications": notifications,
            "delivery": "configured subscription channel (poll by default)",
        }

    @staticmethod
    def _classify(
        event_id: str, event_type: str, key: str, before: str | None, after: str | None
    ) -> list[dict]:
        before_item = json.loads(before) if before else None
        after_item = json.loads(after) if after else None
        item = after_item or before_item or {}

        def note(
            kind: str,
            message: str,
            cites: Mapping[str, Any],
            quote: Mapping[str, Any] | None = None,
        ):
            return {
                "contract": CONTRACT,
                "notification_id": f"{event_id}:{kind}",
                "event_id": event_id,
                "event": kind,
                "object": key,
                "message": message,
                "cites": dict(cites),
                "quote": dict(quote or {}),
                "advice": None,
            }

        if event_type == "coverage-degraded":
            return [
                note(
                    "stale_source",
                    "A source refresh failed; watched items are uncertain, not removed.",
                    {"stale_providers": (after_item or {}).get("stale_providers")},
                )
            ]
        if event_type == "removed" or after_item is None:
            return []
        kind = item.get("kind")
        if kind == "warning":
            label = {
                "Alert": "warning_issued",
                "Update": "warning_updated",
                "Cancel": "warning_cancelled",
            }.get(after_item["msg_type"])
            if label is None or (
                before_item
                and before_item["latest_identifier"] == after_item["latest_identifier"]
            ):
                return []
            quote = after_item["quote"]
            return [
                note(
                    label,
                    f"{quote.get('issuer') or quote.get('sender')}: {after_item['msg_type']} "
                    f"{quote.get('headline') or quote.get('event')} (sent {after_item['sent']})",
                    {
                        "identifier": after_item["latest_identifier"],
                        "revision_id": after_item["revision_id"],
                    },
                    quote,
                )
            ]
        if kind == "issuance" and event_type == "added":
            return [
                note(
                    "forecast_issued",
                    f"{after_item['product']} issued {after_item['issued_at']} for "
                    f"{after_item['location']}",
                    {"revision_id": after_item["revision_id"]},
                )
            ]
        if (
            kind == "observation"
            and event_type in {"changed", "corrected"}
            and after_item["change_kind"] in {"correction", "qc_change"}
        ):
            return [
                note(
                    "observation_corrected",
                    f"report at {after_item['observed_at']} was corrected "
                    f"({after_item['change_kind']})",
                    {
                        "revision_id": after_item["revision_id"],
                        "previous_revision_id": before_item["revision_id"],
                    },
                    {"raw_text": after_item.get("raw_text")}
                    if after_item.get("raw_text")
                    else None,
                )
            ]
        if (
            kind == "location"
            and event_type == "changed"
            and before_item["valid_from"] != after_item["valid_from"]
        ):
            return [
                note(
                    "station_relocated",
                    f"new published location from {after_item['valid_from']} "
                    f"({after_item['latitude']}, {after_item['longitude']})",
                    {
                        "revision_id": after_item["revision_id"],
                        "previous_revision_id": before_item["revision_id"],
                    },
                )
            ]
        return []

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict:
        scopes = set(scopes)
        monitor = self._monitor(subscription_id, principal_id)
        authorize(monitor["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )
