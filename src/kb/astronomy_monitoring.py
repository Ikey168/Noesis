"""Watch small bodies, exoplanets, orbital objects, launch providers or sites and SWPC products (#2149, AS10).

An astronomy monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`): its query names what is
watched, so there is no monitor table and no scheduler. Refresh is the
source-pack schedule plus the subscription watermarks the runtime and the
maintenance orchestrator commit. Each evaluation compares the items in view
with the previous evaluation; the first is a baseline (nothing is notified),
and replaying a watermark or an unchanged refresh produces nothing.

Events: ``designation_identified``, ``orbit_solution_published``,
``risk_listing_changed`` (the published listing, quoted), ``disposition_changed``,
``launch_outcome_published``, ``object_decayed``, ``space_weather_issued`` and
``space_weather_cancelled``. Every notification cites the old and the new
revision and the source; none carries a verdict or advice. Watched objects
use the same equivalences as the queries
(:mod:`src.kb.astronomy_identity`).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.astronomy_identity import (
    AstronomyIdentity,
    _planet_keys,
    designation_group,
    exoplanet_group,
    orbital_group,
)
from src.kb.astronomy_records import (
    READ_SCOPE,
    AstronomyError,
    authorize,
    designation_key,
    object_name_key,
    object_keys,
)
from src.kb.astronomy_store import AstronomyStore

CONTRACT = "noesis-astronomy-monitor-notification-v1"
WATCH_KINDS = (
    "small_body",
    "exoplanet",
    "host",
    "orbital_object",
    "launch_provider",
    "launch_site",
    "space_weather",
)
EVENTS = (
    "designation_identified",
    "orbit_solution_published",
    "risk_listing_changed",
    "disposition_changed",
    "launch_outcome_published",
    "object_decayed",
    "space_weather_issued",
    "space_weather_cancelled",
)
SWPC_FAMILIES = {
    "watch": "watch",
    "warning": "warning",
    "extended_warning": "warning",
    "alert": "alert",
    "summary": "summary",
    "cancel_watch": "watch",
    "cancel_warning": "warning",
    "cancel_alert": "alert",
    "cancel_summary": "summary",
    "other": "other",
}


def _cite(view: Mapping[str, Any]) -> dict[str, Any]:
    source = view["record"]["source"]
    return {
        "record_id": view["record_id"],
        "revision_id": view["revision_id"],
        "record_hash": view["record_hash"],
        "provider": source["provider"],
        "source_record_id": source["source_record_id"],
        **({"url": source["url"]} if source.get("url") else {}),
    }


class AstronomyMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = AstronomyStore(conn, initialize=False, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        watch: str,
        target: str | None = None,
        principal_id: str,
        scopes: Iterable[str],
        scale: str | None = None,
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if watch not in WATCH_KINDS:
            raise AstronomyError("invalid_watch", f"watch one of {WATCH_KINDS}")
        query: dict[str, Any] = {
            "operation": "search",
            "kind": "astronomy-monitor",
            "watch": watch,
        }
        if watch == "space_weather":
            if target is not None and target not in {
                "watch",
                "warning",
                "alert",
                "summary",
            }:
                raise AstronomyError(
                    "invalid_watch",
                    "space weather is watched by product type (or all types)",
                )
            if scale is not None and not (
                len(scale) == 2 and scale[0] in "GSR" and scale[1] in "12345"
            ):
                raise AstronomyError(
                    "invalid_watch", "a scale threshold is a NOAA level such as G2"
                )
            query.update(
                {k: v for k, v in (("product_type", target), ("scale", scale)) if v}
            )
        else:
            if not str(target or "").strip():
                raise AstronomyError(
                    "invalid_watch", f"a {watch} monitor names what it watches"
                )
            query["target"] = str(target).strip()
            if watch == "small_body":
                query["designation"] = designation_key(target)
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "astronomy",
                "query": query,
                "filters": {"watch": watch},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "astronomy-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "source-pack schedules and the maintenance orchestrator commit the watermarks "
            "this monitor evaluates; there is no separate scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE "
            "table_name='knowledge_subscriptions'"
        ).fetchone():
            raise AstronomyError(
                "not_ready", "no astronomy monitor has been created yet"
            )
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "astronomy-monitor":
            raise AstronomyError(
                "monitor_not_found", "subscription is not an astronomy monitor"
            )
        return subscription

    # -------------------------------------------------------------- snapshot

    def snapshot(self, subscription: Mapping[str, Any]) -> dict[str, Any]:
        namespace, query = subscription["namespace"], subscription["query"]
        self.store.require_ready()
        visible = self.store.visible(namespace)
        views = visible["records"]
        records = [v["record"] for v in views]
        watch, target = query["watch"], query.get("target")
        items = []

        def item(key: str, view: Mapping[str, Any], **fields: Any) -> None:
            items.append(
                {
                    "id": key,
                    "kind": view["kind"],
                    "citation": _cite(view),
                    **{k: v for k, v in fields.items() if v is not None},
                }
            )

        if watch == "small_body":
            group = designation_group(records, target)
            for view in views:
                record = view["record"]
                if not {k.removeprefix("des:") for k in object_keys(record)} & group:
                    continue
                if view["kind"] == "identification":
                    item(
                        f"identification:{record['designation']}",
                        view,
                        designation=record["designation"],
                        identified_with=record["identified_with"],
                        announced_in=record.get("announced_in"),
                    )
                elif view["kind"] == "orbit_solution":
                    item(
                        f"orbit:{record['publisher']}:{record['object_designation']}:{record['solution_id']}",
                        view,
                        publisher=record["publisher"],
                        solution_id=record["solution_id"],
                        epoch=record["epoch"]["jd"],
                    )
                elif view["kind"] == "impact_risk_listing":
                    item(
                        f"risk:{record['publisher']}:{record['object_designation']}",
                        view,
                        listing_status=record["listing_status"],
                        figures=record.get("figures"),
                        listing_date=record.get("listing_date"),
                        removed_at=record.get("removed_at"),
                    )
        elif watch in {"exoplanet", "host"}:
            if watch == "exoplanet":
                accepted = AstronomyIdentity(
                    self.conn, initialize=False
                ).accepted_pairs(namespace)
                group = exoplanet_group(records, target, accepted)

                def wanted(record):
                    return bool(set(_planet_keys(record)) & group)
            else:
                host = "host:" + object_name_key(target)

                def wanted(record):
                    return host in object_keys(record)

            for view in views:
                record = view["record"]
                if view["kind"] == "exoplanet_status_assertion" and wanted(record):
                    item(
                        f"disposition:{record['source_table']}:{record['object_name']}",
                        view,
                        object_name=record["object_name"],
                        source_table=record["source_table"],
                        disposition=record.get("disposition"),
                        native_disposition=record["native_disposition"],
                    )
        elif watch == "orbital_object":
            group = orbital_group(records, target)
            for view in views:
                record = view["record"]
                if (
                    view["kind"] == "orbital_object"
                    and {
                        f"{p}:{record[p]}"
                        for p in ("cospar", "norad", "jcat")
                        if record.get(p)
                    }
                    & group["keys"]
                ):
                    item(
                        f"object:{record['source']['provider']}:{record['source']['source_record_id']}",
                        view,
                        status=record.get("status"),
                        decay_date=record.get("decay_date"),
                        name=record.get("name"),
                    )
        elif watch in {"launch_provider", "launch_site"}:
            outcomes = {
                v["record"]["launch_tag"]: v
                for v in views
                if v["kind"] == "launch_outcome"
            }
            for view in views:
                record = view["record"]
                if view["kind"] != "launch":
                    continue
                if watch == "launch_provider" and target.upper() not in {
                    c.upper() for c in record.get("agency_codes") or []
                }:
                    continue
                if (
                    watch == "launch_site"
                    and (record.get("site_code") or "").upper() != target.upper()
                ):
                    continue
                outcome = outcomes.get(record["launch_tag"])
                item(
                    f"launch:{record['launch_tag']}",
                    outcome or view,
                    launch_tag=record["launch_tag"],
                    time=record.get("time"),
                    outcome=outcome and outcome["record"].get("outcome"),
                    native_code=outcome and outcome["record"]["native_code"],
                )
        else:
            family, scale = query.get("product_type"), query.get("scale")
            for view in views:
                record = view["record"]
                if view["kind"] != "space_weather_product":
                    continue
                if family and SWPC_FAMILIES[record["product_kind"]] != family:
                    continue
                if (
                    scale
                    and not record["product_kind"].startswith("cancel")
                    and not any(
                        s[0] == scale[0] and s[1] >= scale[1]
                        for s in record.get("scales") or []
                    )
                ):
                    continue
                item(
                    f"swpc:{record['product_id']}:{record['serial']}",
                    view,
                    serial=record["serial"],
                    product_kind=record["product_kind"],
                    issue_time=record["issue_time"],
                    scales=record.get("scales"),
                    references=record.get("references"),
                )
        return {
            "items": items,
            "coverage": {
                "complete": not visible["unreadable"],
                "unreadable": len(visible["unreadable"]),
            },
        }

    # -------------------------------------------------------------- evaluation

    def run(
        self,
        subscription_id: str,
        watermark: int | None = None,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        namespace = subscription["namespace"]
        authorize(namespace, scopes, READ_SCOPE)
        if watermark is None:
            row = self.conn.execute(
                "SELECT max(watermark) FROM knowledge_subscription_watermarks WHERE namespace=?",
                [namespace],
            ).fetchone()
            if row is None or row[0] is None:
                raise AstronomyError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the "
                    "maintenance orchestrator commit them",
                )
            watermark = int(row[0])
        baseline = subscription.get("last_watermark") is None
        evaluated = self.subscriptions.evaluate(
            subscription_id,
            watermark,
            self.snapshot(subscription),
            principal_id=principal_id,
            scopes=scopes,
            observed_at_ms=self.now(),
        )
        notifications = []
        for event_id in evaluated.get("event_ids", []):
            row = self.conn.execute(
                "SELECT event_type, object_key, before_json, after_json FROM "
                "knowledge_subscription_events WHERE event_id=?",
                [event_id],
            ).fetchone()
            note = None if baseline else self._classify(*row)
            if note:
                notifications.append(
                    {
                        "contract": CONTRACT,
                        "subscription_id": subscription_id,
                        "watermark": watermark,
                        "event_id": event_id,
                        **note,
                    }
                )
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "baseline": baseline,
            "n": len(notifications),
            "notifications": notifications,
            "delivery": subscription["delivery"],
        }

    @staticmethod
    def _classify(event_type, key, before, after) -> dict[str, Any] | None:
        old = json.loads(before) if before else None
        new = json.loads(after) if after else None
        if new is None:
            return None  # a record never disappears from the store; a removed item left the watched scope
        kind = new["kind"]

        def note(event: str, message: str) -> dict[str, Any]:
            return {
                "event": event,
                "message": message,
                "object_key": key,
                "old_revision": old and old["citation"],
                "new_revision": new["citation"],
                "source": new["citation"]["provider"],
                "policy": "describes what the publisher changed; no verdict, prediction or advice",
            }

        if kind == "identification" and (
            old is None or old.get("identified_with") != new.get("identified_with")
        ):
            return note(
                "designation_identified",
                f"{new['designation']} identified with {new['identified_with']} as published by the MPC",
            )
        if kind == "orbit_solution" and old is None:
            return note(
                "orbit_solution_published",
                f"{new['publisher']} published orbit solution {new['solution_id']}",
            )
        if kind == "impact_risk_listing" and (
            old is None
            or {
                k: old.get(k)
                for k in ("listing_status", "figures", "listing_date", "removed_at")
            }
            != {
                k: new.get(k)
                for k in ("listing_status", "figures", "listing_date", "removed_at")
            }
        ):
            return note(
                "risk_listing_changed",
                f"the published risk listing is now {new['listing_status']} "
                "(quoted, no verdict)",
            )
        if kind == "exoplanet_status_assertion" and (
            old is None
            or (old.get("disposition"), old.get("native_disposition"))
            != (new.get("disposition"), new.get("native_disposition"))
        ):
            return note(
                "disposition_changed",
                f"{new['source_table']} disposition of {new['object_name']} is "
                f"{new['native_disposition']} as published",
            )
        if kind == "launch_outcome" and (
            old is None or old.get("native_code") != new.get("native_code")
        ):
            return note(
                "launch_outcome_published",
                f"launch {new['launch_tag']} outcome coded {new['native_code']} by the source",
            )
        if (
            kind == "orbital_object"
            and new.get("decay_date")
            and (old is None or not old.get("decay_date"))
        ):
            return note(
                "object_decayed", f"the catalogue states decay on {new['decay_date']}"
            )
        if kind == "space_weather_product" and old is None:
            if new["product_kind"].startswith("cancel"):
                return note(
                    "space_weather_cancelled",
                    f"serial {new['serial']} cancels serial "
                    f"{(new.get('references') or {}).get('cancels')}",
                )
            return note(
                "space_weather_issued",
                f"{new['product_kind']} serial {new['serial']} issued",
            )
        return None

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict[str, Any]:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )
