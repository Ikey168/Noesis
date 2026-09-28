"""Monitor land-value publications, plan-stage changes and rent-index editions through subscriptions (#1912, U09).

A housing monitor is an ordinary knowledge subscription
(:class:`src.kb.subscriptions.SubscriptionStore`, ``platform.subscriptions``)
whose query carries a housing selector - a place, a district, a plan
identifier or a zone identifier - so there is no watcher table, scheduler,
outbox or delivery worker of its own. Each evaluation runs at a committed
source-pack watermark; the subscription store turns new items into events and
delivers them through its poll and outbox paths.

Items are derived only from acquired source revisions:

* ``new_land_value_publication`` - a zone's reading for a valuation date,
  citing the zone's previous valuation date it follows;
* ``plan_stage_recorded`` - a plan's stage record, citing the previous stage;
* ``new_rent_index_edition`` - an edition, citing the edition valid before it.

A correction of an item changes it and is delivered as a ``changed`` event
with the previous state. Re-acquiring an unchanged publication changes no item,
so it emits nothing; a missed run is recovered from the subscription's last
snapshot and watermark (everything acquired since then arrives once), and
replaying a watermark is idempotent. Event text says what changed and where it
is published; it never assesses what the change means.

Selectors match by published identifier (zone or plan) or by geometry: a
place's point inside the record's geometry, or a record with at least one
outline vertex inside the district's ALKIS boundary (a plan also matches by its
published district name). Rent-index editions are city-wide and match any place
or district selector.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.housing_sources import PLAN_STAGES, plan_key, reporting_area
from src.kb.housing import (
    READ_SCOPE,
    HousingError,
    HousingStore,
    authorize,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-housing-notification-v1"
SELECTOR_KEYS = ("place_id", "district_code", "plan_id", "zone_id")
GEO_READ = "knowledge:geospatial:read"
MESSAGES = {
    "new_land_value_publication": "A land-value publication for a new valuation date was acquired",
    "plan_stage_recorded": "A development-plan stage was recorded",
    "new_rent_index_edition": "A rent-index edition was acquired",
}
_STAGE_ORDER = {stage: index for index, stage in enumerate(PLAN_STAGES)}


class HousingMonitor:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.subscriptions import SubscriptionStore

        self.conn = conn
        self.store = HousingStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)

    def create(
        self,
        namespace: str,
        request_key: str,
        *,
        selector: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        geo_namespace: str = "global",
        delivery: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = {
            k: str(v) for k, v in dict(selector or {}).items() if v not in (None, "")
        }
        if len(wanted) != 1 or set(wanted) - set(SELECTOR_KEYS):
            raise HousingError(
                "invalid_watch",
                f"a housing monitor watches exactly one of {SELECTOR_KEYS}",
            )
        if "place_id" in wanted or "district_code" in wanted:
            require_scope(scopes, GEO_READ)
        if "place_id" in wanted:
            from src.kb.geospatial import GeospatialStore

            if (
                GeospatialStore(self.conn, initialize=False).place(
                    geo_namespace, wanted["place_id"], scopes={GEO_READ}
                )
                is None
            ):
                raise HousingError("not_found", "the watched place is not registered")
        if (
            "district_code" in wanted
            and self._district(geo_namespace, wanted["district_code"])[0] is None
        ):
            raise HousingError(
                "not_found", "no acquired ALKIS district has this Bezirk code"
            )
        created = self.subscriptions.create(
            {
                "namespace": namespace,
                "domain": "geospatial",
                "query": {
                    "operation": "search",
                    "kind": "housing-monitor",
                    "selector": wanted,
                    "geo_namespace": geo_namespace,
                },
                "filters": {"watch": "housing"},
                "cadence": {"trigger": "watermark"},
                "delivery": delivery or {"kind": "poll"},
            },
            "housing-monitor:" + request_key,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {
            **created,
            "refresh": "the geospatial-berlin source-pack schedule and the maintenance orchestrator "
            "commit the watermarks this monitor evaluates; no new scheduler",
        }

    def _subscription(
        self, subscription_id: str, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        subscription = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        if subscription["query"].get("kind") != "housing-monitor":
            raise HousingError(
                "monitor_not_found", "subscription is not a housing monitor"
            )
        return subscription

    # ------------------------------------------------------------------ selection

    def _geometry(self, geometry_id: str | None) -> dict[str, Any] | None:
        if not geometry_id:
            return None
        row = self.conn.execute(
            "SELECT geometry_type, coordinates_json FROM geospatial_geometries WHERE geometry_id=?",
            [geometry_id],
        ).fetchone()
        return (
            None if row is None else {"type": row[0], "coordinates": json.loads(row[1])}
        )

    def _place_points(self, geo_namespace: str, place_id: str) -> list[list[float]]:
        from src.kb.geospatial import GeospatialStore
        from src.kb.housing_places import _vertices

        store = GeospatialStore(self.conn, initialize=False)
        geometries = store.geometries(geo_namespace, place_id, scopes={GEO_READ})
        for geometry in geometries:
            if geometry["geometry"]["type"] in {"Point", "Polygon", "MultiPolygon"}:
                return _vertices(geometry["geometry"])
        return []

    def _district(
        self, geo_namespace: str, code: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        from src.kb.housing_places import (
            DISTRICT_CODE_PROPERTY,
            DISTRICT_COLLECTION,
            DISTRICT_NAME_PROPERTY,
        )

        if not table_exists(self.conn, "geospatial_features"):
            return None, None
        rows = self.conn.execute(
            "SELECT r.geometry_id, r.properties_json FROM geospatial_features f JOIN geospatial_feature_current c "
            "ON c.feature_id=f.feature_id JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE "
            "f.namespace IN (?, 'global') AND f.collection=? AND c.lifecycle='active' ORDER BY f.feature_id",
            [geo_namespace, DISTRICT_COLLECTION],
        ).fetchall()
        wanted = reporting_area("berlin-bezirk", code)
        for geometry_id, properties in rows:
            properties = json.loads(properties)
            if (
                reporting_area("berlin-bezirk", properties.get(DISTRICT_CODE_PROPERTY))
                == wanted
            ):
                return self._geometry(geometry_id), properties.get(
                    DISTRICT_NAME_PROPERTY
                )
        return None, None

    @staticmethod
    def _inside(geometry: Mapping[str, Any] | None, points: list[list[float]]) -> bool:
        from src.kb.geospatial_features import GeospatialFeatureStore

        if (
            not geometry
            or not points
            or geometry["type"] not in {"Polygon", "MultiPolygon"}
        ):
            return False
        candidates = [
            {"geometry_id": str(i), "geometry": {"type": "Point", "coordinates": p}}
            for i, p in enumerate(points)
        ]
        return bool(
            GeospatialFeatureStore._membership({"geometry": geometry}, candidates)
        )

    def _matches(self, subscription: Mapping[str, Any]):
        from src.kb.housing_places import _vertices

        selector = subscription["query"]["selector"]
        geo_namespace = subscription["query"].get("geo_namespace") or "global"
        if "zone_id" in selector:
            return lambda kind, record: (
                kind == "land" and record["zone_id"] == selector["zone_id"]
            )
        if "plan_id" in selector:
            key = plan_key(selector["plan_id"])
            return lambda kind, record: (
                kind == "plan" and plan_key(record["plan_id"]) == key
            )
        if "place_id" in selector:
            points = self._place_points(geo_namespace, selector["place_id"])
            return lambda kind, record: (
                kind == "edition"
                or self._inside(self._geometry(record.get("geometry_id")), points)
            )
        district, name = self._district(geo_namespace, selector["district_code"])

        def in_district(kind, record):
            if kind == "edition":
                return True
            if (
                kind == "plan"
                and name
                and str(record.get("district") or "").casefold() == name.casefold()
            ):
                return True
            geometry = self._geometry(record.get("geometry_id"))
            return bool(
                district and geometry and self._inside(district, _vertices(geometry))
            )

        return in_district

    # ------------------------------------------------------------------ items

    @staticmethod
    def _source(record: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "record_id": record["record_id"],
            "source_id": record["source_id"],
            "provider": record["provider"],
            "revision_no": record["revision_no"],
            "change": record["change"],
            **dict(record["source_revision"]),
        }

    def snapshot(self, subscription: Mapping[str, Any]) -> dict[str, Any]:
        namespace = subscription["namespace"]
        matches = self._matches(subscription)
        items = []
        land = [
            r
            for r in self.store.records("land_value_revision", namespace)
            if r.get("valuation_date")
        ]
        for record in land:
            if not matches("land", record):
                continue
            earlier = [
                r
                for r in land
                if r["source_id"] == record["source_id"]
                and r["zone_id"] == record["zone_id"]
                and r["valuation_date"] < record["valuation_date"]
            ]
            previous = (
                max(earlier, key=lambda r: r["valuation_date"]) if earlier else None
            )
            items.append(
                {
                    "id": f"land-value:{record['entity_key']}",
                    "item": "new_land_value_publication",
                    "zone_id": record["zone_id"],
                    "valuation_date": record["valuation_date"],
                    "value_text": record["value_text"],
                    "unit": record["unit"],
                    "currency": record["currency"],
                    "previous": None
                    if previous is None
                    else {
                        "valuation_date": previous["valuation_date"],
                        "value_text": previous["value_text"],
                        "record_id": previous["record_id"],
                    },
                    "source_revision": self._source(record),
                }
            )
        stages = self.store.records("plan_stage", namespace)
        for record in stages:
            if not matches("plan", record):
                continue
            earlier = [
                s
                for s in stages
                if plan_key(s["plan_id"]) == plan_key(record["plan_id"])
                and s["source_id"] == record["source_id"]
                and s["entity_key"] != record["entity_key"]
                and (s.get("stage_date") or "", _STAGE_ORDER.get(s["stage"], 99))
                < (
                    record.get("stage_date") or "",
                    _STAGE_ORDER.get(record["stage"], 99),
                )
            ]
            previous = max(
                earlier,
                key=lambda s: (
                    s.get("stage_date") or "",
                    _STAGE_ORDER.get(s["stage"], 99),
                ),
                default=None,
            )
            items.append(
                {
                    "id": f"plan-stage:{record['entity_key']}",
                    "item": "plan_stage_recorded",
                    "plan_id": record["plan_id"],
                    "stage": record["stage"],
                    "stage_label": record["stage_label"],
                    "stage_date": record["stage_date"],
                    "previous": None
                    if previous is None
                    else {
                        "stage": previous["stage"],
                        "stage_date": previous["stage_date"],
                        "record_id": previous["record_id"],
                    },
                    "source_revision": self._source(record),
                }
            )
        editions = self.store.editions(namespace)
        for record in editions:
            if not matches("edition", record):
                continue
            earlier = [
                e
                for e in editions
                if e["source_id"] == record["source_id"]
                and (e.get("valid_from") or "") < (record.get("valid_from") or "")
            ]
            previous = max(
                earlier, key=lambda e: e.get("valid_from") or "", default=None
            )
            items.append(
                {
                    "id": f"rent-index-edition:{record['entity_key']}",
                    "item": "new_rent_index_edition",
                    "edition_id": record["edition_id"],
                    "edition": record["edition"],
                    "valid_from": record["valid_from"],
                    "published_on": record["published_on"],
                    "publication_url": record.get("publication_url"),
                    "previous": None
                    if previous is None
                    else {
                        "edition_id": previous["edition_id"],
                        "valid_from": previous["valid_from"],
                        "record_id": previous["record_id"],
                    },
                    "source_revision": self._source(record),
                }
            )
        return {
            "items": sorted(items, key=lambda i: i["id"]),
            "coverage": {"complete": True},
        }

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
                raise HousingError(
                    "watermark_uncommitted",
                    "no committed watermark yet; source-pack runs and the "
                    "maintenance orchestrator commit them",
                )
            watermark = int(row[0])
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
                "SELECT event_type, object_key, before_json, after_json FROM knowledge_subscription_events "
                "WHERE event_id=?",
                [event_id],
            ).fetchone()
            notifications.extend(self._classify(event_id, *row))
        return {
            "subscription_id": subscription_id,
            "status": evaluated["status"],
            "watermark": watermark,
            "notifications": notifications,
            "delivery": subscription["delivery"],
        }

    @staticmethod
    def _classify(
        event_id: str, event_type: str, key: str, before: str | None, after: str | None
    ) -> list[dict[str, Any]]:
        if event_type not in {"added", "changed", "corrected"} or not after:
            return []
        item = json.loads(after)
        kind = item["item"]
        revision = item["source_revision"]
        where = (
            revision.get("publication_url")
            or revision.get("url")
            or revision.get("collection")
        )
        subject = item.get("zone_id") or item.get("plan_id") or item.get("edition")
        message = (
            f"{MESSAGES[kind]}: {subject}"
            f" ({item.get('valuation_date') or item.get('stage_date') or item.get('valid_from') or 'undated'}),"
            f" published by {revision['provider']} in {where}."
        )
        if event_type != "added":
            message = f"A correction was acquired for: {subject}, published by {revision['provider']} in {where}."
        return [
            {
                "contract": CONTRACT,
                "event_id": event_id,
                "notification_id": f"{event_id}:{kind}:{event_type}",
                "object": key,
                "kind": kind if event_type == "added" else f"{kind}_corrected",
                "message": message,
                "source_revision": revision,
                "supersedes": json.loads(before)["source_revision"]
                if before
                else item.get("previous"),
                "item": {
                    k: v
                    for k, v in item.items()
                    if k not in {"source_revision", "id", "item"}
                },
                "note": "reports what was published and where; nothing is concluded about the change",
            }
        ]

    def poll(
        self,
        subscription_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        cursor: str = "",
    ) -> dict:
        scopes = set(scopes)
        subscription = self._subscription(subscription_id, principal_id, scopes)
        authorize(subscription["namespace"], scopes, READ_SCOPE)
        return self.subscriptions.poll(
            subscription_id, principal_id=principal_id, scopes=scopes, cursor=cursor
        )
