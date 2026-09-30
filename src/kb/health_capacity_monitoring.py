"""Watch a place's capacity indicators for new releases, revisions and definition changes (#2215, HS09).

A capacity monitor is a :class:`~src.kb.surveillance_monitoring.SurveillanceMonitor` (subclassed, not copied): a
knowledge subscription stored through :class:`~src.kb.subscriptions.SubscriptionStore` and registered in the clinical
monitor store, over the capacity indicators of one place (a Geospatial place id or a published code, resolved
through the HS06 resolutions), optionally narrowed to domains or source indicator codes. Evaluating it at a committed
watermark yields replay-safe, deduplicated deliveries:

* ``new-release`` - a new vintage of an indicator without changed values;
* ``revision`` - a new vintage that revised published values: old and new value per period, both vintages cited
  (the :mod:`src.kb.surveillance_vintages` comparison);
* ``definition-change`` - a change of definition edition, reported as a break (earlier values keep their
  definition; nothing is restated);
* ``threshold-exceeded`` - a published value against a threshold **the user configured** (unit-checked); the pack
  derives, suggests or adjusts none;
* ``stale-source`` - a provider refresh failed.

No scheduler is added: sources refresh through the ``clinical-evidence`` source pack's runtime schedule and the
maintenance orchestrator.
"""

from __future__ import annotations

from typing import Any

from src.kb.health_capacity import DOMAINS, NEVER_SENTENCE, SCHEME, HealthCapacityError
from src.kb.surveillance import READ_SCOPE, authorize, digest
from src.kb.surveillance_monitoring import (
    SurveillanceMonitor,
    _check_thresholds,
    evidence_scopes,
    refresh_schedule,
)

CONTRACT = "noesis-health-capacity-notification-v1"
KIND = "health-capacity-monitor"
EVENTS = {
    "new-vintage": ("new-release", "A new release of the indicator was published"),
    "value-revised": ("revision", "A new release revised published values (old and new values, both vintages "
                                  "cited)"),
    "case-definition-changed": ("definition-change", "The indicator's definition changed: a break in the series; "
                                                     "earlier values keep the earlier definition"),
    "threshold-exceeded": ("threshold-exceeded", None),
    "geography-break": ("geography-break", None),
    "stale-source": ("stale-source", None),
}
WATCH_KEYS = {"place_id", "place_code", "domains", "indicators"}


class HealthCapacityMonitor(SurveillanceMonitor):
    def create(self, namespace, request_key, *, watch, principal_id, scopes, thresholds=(), delivery=None):
        """Watch the capacity indicators of one place (``place_id`` or ``place_code``), optionally narrowed to
        ``domains`` and source ``indicators``; thresholds are user-configured and unit-checked when evaluated."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        watch = {k: v for k, v in dict(watch or {}).items() if v not in (None, "", [])}
        if set(watch) - WATCH_KEYS or ("place_id" in watch) == ("place_code" in watch):
            raise HealthCapacityError("invalid_watch", "watch one place_id or place_code, optionally narrowed to "
                                                       "domains and indicators")
        if set(watch.get("domains") or []) - set(DOMAINS):
            raise HealthCapacityError("invalid_domain", f"domains are among {DOMAINS}")
        for key in ("domains", "indicators"):
            if key in watch:
                watch[key] = sorted({str(v) for v in watch[key]})
        checked = _check_thresholds(thresholds)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "clinical",
                "query": {"operation": "search", "kind": KIND, "watch": watch, "thresholds": checked},
                "filters": {"watch": "health-capacity"},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "health-capacity-monitor:" + request_key,
            principal_id=principal_id,
            scopes=evidence_scopes(namespace, scopes),
        )
        self.conn.execute(
            "INSERT INTO clinical_monitors VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            [created["subscription_id"], namespace, principal_id, "health-capacity:" + digest([watch, checked])[:24]],
        )
        return {
            **created,
            "watch": watch,
            "thresholds": checked,
            "refresh": refresh_schedule(self.conn),
            "threshold_policy": "user-configured; the pack never derives, suggests or adjusts a threshold",
        }

    def _series(self, namespace, watch, scopes):
        from src.kb.health_capacity_comparability import HealthCapacityComparability
        from src.kb.health_capacity_queries import _resolve

        tool = HealthCapacityComparability(self.conn, initialize=False)
        _, pairs, _, _ = _resolve(tool, namespace, watch.get("place_id"), watch.get("place_code"))
        domains = set(watch.get("domains") or DOMAINS)
        indicators = set(watch.get("indicators") or [])
        found = {}
        for system, code in pairs:
            for series in self.store.find_series(namespace, condition_scheme=SCHEME, geography_system=system,
                                                 geography_code=code):
                if series["condition"]["code"] in domains and (
                        not indicators or series["indicator_code"] in indicators):
                    found[series["series_id"]] = series
        return [found[k] for k in sorted(found)]

    def _items(self, namespace, series, thresholds):
        from src.kb.surveillance_vintages import compare

        items = super()._items(namespace, series, thresholds)
        for item in items:
            item["indicator"] = {"provider": series["provider"], "source_code": series["indicator_code"],
                                 "domain": series["condition"]["code"], "place": series["geography"],
                                 "unit": series["unit"]["label"]}
            if item["item"] == "value-revised":
                comparison = compare(self.conn, namespace, series["series_id"], scopes={"operator"},
                                     left=item["previous_vintage_id"], right=item["vintage_id"])
                item["changes"] = [{k: c[k] for k in ("reference_period", "change", "left", "right", "unit",
                                                     "attribution")} for c in comparison["changes"]]
                item["vintages"] = {"previous": comparison["left"], "current": comparison["right"]}
        return items

    def _subscription(self, subscription_id, principal_id, scopes):
        monitor = self._monitor(subscription_id, principal_id)
        subscription = self.subscriptions.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if subscription["query"].get("kind") != KIND:
            raise HealthCapacityError("monitor_not_found", "subscription is not a health-capacity monitor")
        return monitor, subscription

    def run(self, subscription_id, watermark=None, *, principal_id, scopes) -> dict[str, Any]:
        result = super().run(subscription_id, watermark, principal_id=principal_id, scopes=scopes)
        notifications = []
        for note in result["notifications"]:
            kind, message = EVENTS.get(note["kind"], (note["kind"], None))
            notifications.append({**note, "contract": CONTRACT, "kind": kind, "surveillance_kind": note["kind"],
                                  "message": message or note["message"],
                                  "notification_id": f"{note['event_id']}:{kind}"})
        return {**result, "notifications": notifications, "boundary": NEVER_SENTENCE}


__all__ = ["CONTRACT", "EVENTS", "HealthCapacityMonitor", "KIND"]
