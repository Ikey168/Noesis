"""Watch surveillance series for user-configured thresholds, new vintages and case-definition changes (#1917, I10).

A surveillance monitor is a :class:`~src.kb.clinical_monitoring.ClinicalMonitor`
(subclassed, not copied): a knowledge subscription stored through
:class:`~src.kb.subscriptions.SubscriptionStore`, registered in the clinical
monitor store (``clinical_monitors``), over one series or one
condition-within-boundary query. Evaluating it at a committed watermark yields
replay-safe events:

* ``threshold-exceeded`` - a value above (or at or above) a threshold **the user
  configured**; the threshold's unit is checked through pint against the series
  unit (a count is never compared with a rate) and the event cites the value,
  its vintage, reporting date and reference date. The pack never derives,
  suggests or adjusts a threshold;
* ``new-vintage`` / ``value-revised`` - a release of the series without / with
  changed values;
* ``case-definition-changed`` - a case-definition break, an event of its own
  that never suppresses or re-baselines an exceedance;
* ``geography-break`` - a code-list or boundary change under the series' code;
* ``stale-source`` - a provider refresh failed (the source-pack runtime records it).

No scheduler is added: sources refresh through the ``clinical-evidence`` source
pack's runtime schedule and the maintenance orchestrator (``refresh_schedule``).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.ingestion.surveillance_sources import UNITS, number_key
from src.kb.clinical_monitoring import ClinicalMonitor, refresh_schedule
from src.kb.surveillance import (
    NEVER_SENTENCE,
    READ_SCOPE,
    SurveillanceError,
    SurveillanceStore,
    authorize,
    digest,
)

CONTRACT = "noesis-surveillance-notification-v1"
KINDS = (
    "threshold-exceeded",
    "new-vintage",
    "value-revised",
    "case-definition-changed",
    "geography-break",
    "stale-source",
)
COMPARISONS = {
    "above": lambda value, limit: value > limit,
    "at_or_above": lambda value, limit: value >= limit,
}
# Exact factors to a base unit, used when the optional pint dependency is not installed.
_BASE = {
    "count": Decimal(1),
    "ppm": Decimal("0.000001"),
    "permille": Decimal("0.001"),
    "percent": Decimal("0.01"),
}


def threshold_in_series_unit(
    threshold: Mapping[str, Any], series_unit: str
) -> dict[str, Any]:
    """A user-configured threshold expressed in the series unit, through pint (exact fallback without pint)."""
    unit = str(threshold.get("unit") or "")
    if unit not in UNITS or series_unit not in UNITS:
        raise SurveillanceError(
            "invalid_threshold", f"threshold unit {unit!r} is not a surveillance unit"
        )
    source_pint, source_factor, source_kind = UNITS[unit]
    target_pint, target_factor, target_kind = UNITS[series_unit]
    if source_kind != target_kind:
        raise SurveillanceError(
            "incompatible_unit",
            f"a {source_kind} threshold is never compared with a {target_kind} series",
        )
    value = Decimal(str(threshold["value"]))
    quantity = value * source_factor
    try:
        from src.integrations.units import convert_physical

        converted = Decimal(
            convert_physical(str(quantity), source_pint, target_pint, precision=9)[
                "result"
            ]["value"]
        )
        method = f"pint convert_physical {source_pint} -> {target_pint}"
    except ModuleNotFoundError:
        converted = quantity * _BASE[source_pint] / _BASE[target_pint]
        method = "exact decimal factors (pint not installed)"
    in_series = (converted / target_factor).normalize()
    return {
        "value": str(in_series),
        "unit": series_unit,
        "configured": {"value": str(value), "unit": unit},
        "method": method,
    }


def _check_thresholds(thresholds: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for threshold in thresholds or []:
        if not isinstance(threshold, Mapping) or set(threshold) - {
            "value",
            "unit",
            "comparison",
            "label",
        }:
            raise SurveillanceError(
                "invalid_threshold", "a threshold names value, unit and comparison"
            )
        if threshold.get("comparison", "above") not in COMPARISONS:
            raise SurveillanceError(
                "invalid_threshold", f"comparison is one of {sorted(COMPARISONS)}"
            )
        try:
            value = Decimal(str(threshold["value"]))
        except Exception as exc:  # noqa: BLE001 - any unparsable value is refused
            raise SurveillanceError(
                "invalid_threshold", "threshold value is a number"
            ) from exc
        if not value.is_finite():
            raise SurveillanceError(
                "invalid_threshold", "threshold value is a finite number"
            )
        if threshold.get("unit") not in UNITS:
            raise SurveillanceError(
                "invalid_threshold",
                f"threshold unit {threshold.get('unit')!r} is not a surveillance unit",
            )
        out.append(
            {
                "value": str(value),
                "unit": threshold["unit"],
                "comparison": threshold.get("comparison", "above"),
                "label": threshold.get("label"),
            }
        )
    return out


def evidence_scopes(namespace: str, scopes: set[str]) -> set[str]:
    """The scopes a monitor's subscription retains as its evidence access: the clinical read scope in the namespace
    and the subscription operation scopes, not every scope the caller happened to hold."""
    if "operator" in scopes:
        return set(scopes)
    return {READ_SCOPE, f"namespace:{namespace}:read"} | (
        set(scopes) & {"knowledge:subscriptions:read", "knowledge:subscriptions:write"}
    )


class SurveillanceMonitor(ClinicalMonitor):
    def __init__(self, conn, *, initialize=True, now=None):
        super().__init__(conn, initialize=initialize, now=now)
        self.store = SurveillanceStore(conn, initialize=initialize, now=self.now)

    def create(
        self,
        namespace,
        request_key,
        *,
        watch,
        principal_id,
        scopes,
        thresholds=(),
        delivery=None,
    ):
        """Watch one series (``{"series_id"}``) or one condition within a boundary (``{"condition", "feature_id"}``)."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        watch = {k: v for k, v in dict(watch or {}).items() if v not in (None, "")}
        if set(watch) not in ({"series_id"}, {"condition", "feature_id"}):
            raise SurveillanceError(
                "invalid_watch",
                "watch one series_id, or a condition within a feature_id",
            )
        checked = _check_thresholds(thresholds)
        if "series_id" in watch:
            series = self.store.series(namespace, watch["series_id"])
            for threshold in checked:
                threshold_in_series_unit(
                    threshold, series["unit"]["label"]
                )  # refuses an incompatible unit now
        view_id = "surveillance:" + digest([watch, checked])[:24]
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "clinical",
                "query": {
                    "operation": "search",
                    "kind": "surveillance-monitor",
                    "watch": watch,
                    "thresholds": checked,
                },
                "filters": {"watch": "surveillance-series"},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "surveillance-monitor:" + request_key,
            principal_id=principal_id,
            scopes=evidence_scopes(namespace, scopes),
        )
        self.conn.execute(
            "INSERT INTO clinical_monitors VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            [created["subscription_id"], namespace, principal_id, view_id],
        )
        return {
            **created,
            "watch": watch,
            "thresholds": checked,
            "refresh": refresh_schedule(self.conn),
            "threshold_policy": "user-configured; the pack never derives, suggests or adjusts a threshold",
        }

    def _series(self, namespace, watch, scopes):
        if "series_id" in watch:
            return [self.store.series(namespace, watch["series_id"])]
        from src.kb.surveillance_places import SurveillancePlaces

        answer = SurveillancePlaces(self.conn, initialize=False).boundary_series(
            namespace,
            scopes=scopes,
            feature_id=watch["feature_id"],
            condition=watch["condition"],
        )
        ids = [c["series_id"] for c in answer["series"] + answer["contained_units"]]
        return [
            self.store.series(namespace, series_id) for series_id in sorted(set(ids))
        ]

    def _items(self, namespace, series, thresholds):
        items, previous = [], None
        vintages = self.store.vintage_rows(namespace, series["series_id"])
        previous_values: dict[str, Any] = {}
        for vintage in vintages:
            values = {
                v["value_key"]: (number_key(v["value"]), v["flags"])
                for v in self.store.value_rows(namespace, vintage["vintage_id"])
            }
            changed = sorted(
                k
                for k in set(values) | set(previous_values)
                if values.get(k) != previous_values.get(k)
            )
            kind = (
                "value-revised" if previous is not None and changed else "new-vintage"
            )
            items.append(
                {
                    "id": f"vintage:{vintage['vintage_id']}",
                    "item": kind,
                    "series_id": series["series_id"],
                    "vintage_id": vintage["vintage_id"],
                    "previous_vintage_id": None
                    if previous is None
                    else previous["vintage_id"],
                    "changed_values": changed if previous is not None else [],
                    "source_revision": self.store.source_revision(
                        namespace, vintage["release_id"]
                    ),
                }
            )
            previous, previous_values = vintage, values
        for brk in series["breaks"]:
            kind = {
                "case-definition": "case-definition-changed",
                "geography": "geography-break",
            }.get(brk["kind"])
            if kind:
                items.append(
                    {
                        "id": f"break:{brk['break_id']}",
                        "item": kind,
                        "series_id": series["series_id"],
                        "period": brk["period"],
                        "from": brk["from"],
                        "to": brk["to"],
                        "detail": brk["detail"],
                        "vintage_id": brk["first_vintage_id"],
                    }
                )
        if vintages and thresholds:
            current = vintages[-1]
            revision = self.store.source_revision(namespace, current["release_id"])
            for threshold in thresholds:
                try:
                    limit = threshold_in_series_unit(threshold, series["unit"]["label"])
                except SurveillanceError as exc:
                    items.append(
                        {
                            "id": f"threshold-na:{series['series_id']}:{digest(threshold)[:16]}",
                            "item": "threshold-not-applicable",
                            "series_id": series["series_id"],
                            "threshold": threshold,
                            "reason": str(exc),
                        }
                    )
                    continue
                compare = COMPARISONS[threshold["comparison"]]
                for value in self.store.value_rows(namespace, current["vintage_id"]):
                    if value["value"] is None or not compare(
                        Decimal(value["value"]), Decimal(limit["value"])
                    ):
                        continue
                    # Keyed by the value itself: an unchanged value re-published does not fire again; a revised
                    # value that still exceeds does.
                    key = digest(
                        [
                            series["series_id"],
                            value["value_key"],
                            number_key(value["value"]),
                            threshold,
                        ]
                    )[:24]
                    items.append(
                        {
                            "id": f"threshold:{key}",
                            "item": "threshold-exceeded",
                            "series_id": series["series_id"],
                            "value": value["value"],
                            "value_text": value["value_text"],
                            "unit": series["unit"]["label"],
                            "reference_period": value["reference_period"],
                            "reporting_date": value["reporting_date"],
                            "unknown": value["unknown"],
                            "kind": series["kind"],
                            "vintage_id": current["vintage_id"],
                            "threshold": {**threshold, "in_series_unit": limit},
                            "source_revision": revision,
                        }
                    )
        return items

    def snapshot_for(self, subscription):
        namespace, query = subscription["namespace"], subscription["query"]
        items, stale = [], set()
        for series in self._series(namespace, query["watch"], {"operator"}):
            items += self._items(namespace, series, query.get("thresholds") or [])
            if (
                self.records.provider_state(namespace, series["provider"]).get(
                    "last_failure_ms"
                )
                is not None
            ):
                stale.add(series["provider"])
        return {
            "items": items,
            "coverage": {"complete": not stale, "stale_providers": sorted(stale)},
        }

    def _subscription(self, subscription_id, principal_id, scopes):
        monitor = self._monitor(subscription_id, principal_id)
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "surveillance-monitor":
            raise SurveillanceError(
                "monitor_not_found", "subscription is not a surveillance monitor"
            )
        return monitor, subscription

    def run(self, subscription_id, watermark=None, *, principal_id, scopes):
        scopes = set(scopes)
        _, subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [namespace],
            ).fetchone()
            if row is None or row[0] is None:
                raise SurveillanceError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and "
                    "the maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        elif not self.conn.execute(
            "SELECT 1 FROM knowledge_subscription_watermarks WHERE namespace=? AND "
            "watermark=?",
            [namespace, int(watermark)],
        ).fetchone():
            self.subscriptions.commit_watermark(
                namespace,
                int(watermark),
                kind="ingestion",
                detail={"committed_by": "surveillance-monitor"},
            )
        result = self.snapshot_for(subscription)
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            int(watermark),
            result,
            principal_id=principal_id,
            scopes=evidence_scopes(namespace, scopes),
            observed_at_ms=self.now(),
        )
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, after_json FROM knowledge_subscription_events "
                "WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(_classify(event_id, *row))
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": int(watermark),
            "notifications": notifications,
            "coverage": result["coverage"],
            "not_applicable": [
                i for i in result["items"] if i["item"] == "threshold-not-applicable"
            ],
            "delivery": subscription["delivery"],
            "boundary": NEVER_SENTENCE,
        }


_MESSAGES = {
    "threshold-exceeded": "A published value exceeds a user-configured threshold",
    "new-vintage": "A new vintage of the series was published",
    "value-revised": "A new vintage revised published values",
    "case-definition-changed": "The case definition changed; earlier values stay under the earlier definition",
    "geography-break": "The geography under the series' code changed; nothing is re-aggregated",
}


def _classify(event_id, event_type, key, after):
    def note(kind, message, item):
        return {
            "contract": CONTRACT,
            "event_id": event_id,
            "notification_id": f"{event_id}:{kind}",
            "kind": kind,
            "object": key,
            "message": message,
            "item": item,
            "note": "a publication is reported as published; no cause, forecast or advice is given",
        }

    if event_type == "coverage-degraded":
        coverage = json.loads(after) if after else {}
        return [
            note(
                "stale-source",
                "A source refresh failed for "
                + ", ".join(coverage.get("stale_providers") or [])
                + "; nothing was changed.",
                coverage,
            )
        ]
    if event_type != "added" or not after:
        return []
    item = json.loads(after)
    kind = item["item"]
    if kind not in _MESSAGES:
        return []
    message = _MESSAGES[kind]
    if kind == "threshold-exceeded":
        threshold = item["threshold"]
        message = (
            f"{message}: {item['value_text']} {item['unit']} (reference {item['reference_period'] or 'unknown'}"
            f", reported {item['reporting_date'] or 'unknown'}) against the user-configured threshold "
            f"{threshold['value']} {threshold['unit']} ({threshold['comparison']})."
        )
    return [note(kind, message, item)]
