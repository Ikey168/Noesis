"""As-of observation, forecast-as-issued and warnings-in-force queries (WX08, #2171).

Every answer states its knowledge cutoff (only revisions acquired by then are
read), its sources and attributions, and the rule it applied. Three clocks stay
apart: the source's reference time (observation, issue or ``sent`` time), the
valid time of a forecast element, and the acquisition time of each revision.
The current revision is selected **per record first** (by the source's own
time, then acquisition order) and only then filtered. A time with no data
returns ``no report on record``, never an inferred value.

* :meth:`WeatherQueries.observations` — reports at a station, or at the
  stations whose location vintage *valid at each report's own time* lies within
  a radius of a place (Geospatial proximity receipts), with QC flags (native and
  common), corrections and the location vintage used.
* :meth:`WeatherQueries.forecast_as_issued` — per provider, product and model,
  the latest issuance at or before ``issued_before`` that covers the valid time,
  with lead time. :meth:`WeatherQueries.forecast_evolution` lists every issuance.
* :meth:`WeatherQueries.warnings_in_force` — CAP chains (Alert → Update →
  Cancel, threaded by references in any arrival order) evaluated at ``as_of``
  over the areas that contain the place (``GeospatialFeatureStore.containing``,
  a replayable receipt). Warnings are quoted as issued, with no advice.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb import weather_records as wr
from src.kb.weather_identity import WeatherStationIdentity, distance_m
from src.kb.weather_links import WeatherClimateLinks
from src.kb.weather_normalise import common_parameter, normalise, qc_common
from src.kb.weather_records import READ_SCOPE
from src.kb.weather_store import (
    WARNING_AREA_COLLECTION,
    WeatherError,
    WeatherStore,
    authorize,
    require_ready,
)

GEO_SCOPES = {
    "knowledge:geospatial:read",
    "knowledge:geospatial:calculate",
    "knowledge:geospatial:write",
}
DEFAULT_RADIUS_M = 10_000.0
NO_REPORT = "no report on record"


def _cutoff(value: str | None) -> int | None:
    return None if value is None else wr.ms(wr.utc(value))


def _day_in(vintage: Mapping[str, Any], day: str) -> bool:
    return vintage["valid_from"] <= day and (
        vintage.get("valid_to") is None or day <= vintage["valid_to"]
    )


def _centroid(geometry: Mapping[str, Any]) -> list[float]:
    if geometry["type"] == "Point":
        return list(geometry["coordinates"])
    ring = geometry["coordinates"][0][:-1]
    return [sum(p[0] for p in ring) / len(ring), sum(p[1] for p in ring) / len(ring)]


class WeatherQueries:
    def __init__(self, conn: Any, *, now: Any = None) -> None:
        self.conn = conn
        self.store = WeatherStore(conn, initialize=False, now=now)
        self.identity = WeatherStationIdentity(conn, initialize=False, now=now)
        self.links = WeatherClimateLinks(
            conn,
            initialize=False,
            now=now,
            environment_namespace=self.identity.environment_namespace,
        )

    # ------------------------------------------------------------------ places

    def _point(self, namespace: str, place: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(place, Mapping):
            raise WeatherError(
                "invalid_place",
                "place is {point: [lon, lat]} or {place_id}, with optional radius_m",
            )
        radius = float(place.get("radius_m") or DEFAULT_RADIUS_M)
        if place.get("point") is not None:
            lon, lat = (float(v) for v in place["point"])
            return {"point": [lon, lat], "radius_m": radius, "basis": "caller point"}
        if place.get("place_id"):
            geometries = self.store.geo.geometries(
                namespace, str(place["place_id"]), scopes={"knowledge:geospatial:read"}
            )
            point = next(
                (
                    g["geometry"]["coordinates"]
                    for g in geometries
                    if g["geometry"]["type"] == "Point"
                ),
                None,
            )
            if point is None:
                raise WeatherError("invalid_place", "the place has no point geometry")
            return {
                "point": list(point),
                "radius_m": radius,
                "place_id": place["place_id"],
                "basis": "Geospatial place point geometry",
            }
        raise WeatherError("invalid_place", "place needs a point or a place_id")

    def _vintage_geometry(
        self, namespace: str, station: str, valid_from: str
    ) -> str | None:
        row = self.conn.execute(
            "SELECT g.geometry_id FROM geospatial_places p JOIN geospatial_geometries g ON g.place_id=p.place_id "
            "WHERE p.namespace=? AND p.place_key=? ORDER BY g.observed_at_ms DESC LIMIT 1",
            [namespace, f"weather:location:{station}:{valid_from}"],
        ).fetchone()
        return row[0] if row else None

    def _station_geometry(self, station: str) -> dict[str, Any] | None:
        """The environment owner's station point, read (never written) for stations without location vintages."""

        from src.kb.environment_store import record_id as environment_record_id

        provider, native = station.split(":", 1)
        env_ns = self.identity.environment_namespace
        rid = environment_record_id(env_ns, "station", provider, native)
        row = self.conn.execute(
            "SELECT revision_id, geometry_id FROM environment_record_revisions WHERE record_id=? AND geometry_id IS "
            "NOT NULL ORDER BY revision DESC LIMIT 1",
            [rid],
        ).fetchone()
        if row is None:
            return None
        return {
            "record_id": rid,
            "revision_id": row[0],
            "geometry_id": row[1],
            "namespace": env_ns,
        }

    def _vintage_at(
        self, vintages: list[dict[str, Any]], at: str
    ) -> dict[str, Any] | None:
        day = at[:10]
        matching = [v for v in vintages if _day_in(v["content"], day)]
        return (
            max(matching, key=lambda v: v["content"]["valid_from"])
            if matching
            else None
        )

    # ------------------------------------------------------------ observations

    def observations(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        principal_id: str,
        window_from: str,
        window_to: str,
        station: str | None = None,
        place: Mapping[str, Any] | None = None,
        knowledge_cutoff: str | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_ready(self.conn, namespace)
        if bool(station) == bool(place):
            raise WeatherError(
                "invalid_request", "give exactly one of station or place"
            )
        start, end, cutoff = (
            wr.ms(wr.utc(window_from)),
            wr.ms(wr.utc(window_to)),
            _cutoff(knowledge_cutoff),
        )
        located = self._point(namespace, place) if place else None
        stations = [station] if station else self.identity.station_keys(namespace)
        reports = self.store.currents(
            namespace,
            record_type="observation_report",
            scopes=scopes,
            cutoff_ms=cutoff,
            subject_keys=stations,
            reference_from_ms=start,
            reference_to_ms=end + 1,
        )
        vintages = {
            key: self.store.location_vintages(
                namespace, key, scopes=scopes, cutoff_ms=cutoff
            )
            for key in {r["subject_key"] for r in reports}
        }
        receipts: dict[str, dict[str, Any]] = {}
        references: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
        rows, excluded = [], 0
        for report in reports:
            content = report["content"]
            vintage = self._vintage_at(
                vintages.get(report["subject_key"], []), content["observed_at"]
            )
            used = None
            if vintage is not None:
                used = {
                    "revision_id": vintage["revision_id"],
                    "valid_from": vintage["content"]["valid_from"],
                    "valid_to": vintage["content"].get("valid_to"),
                    "latitude": vintage["content"]["latitude"],
                    "longitude": vintage["content"]["longitude"],
                }
            if vintage is None and not vintages.get(report["subject_key"]):
                station_point = self._station_geometry(report["subject_key"])
                if station_point is not None:
                    used = {
                        "basis": "environment station point (the source states no dated location history)",
                        "environment_record_id": station_point["record_id"],
                        "revision_id": station_point["revision_id"],
                    }
            if located is not None:
                if used is None:
                    excluded += 1  # no published location for the report's time: never placed by guess
                    continue
                if "valid_from" in used:
                    key = f"{report['subject_key']}@{used['valid_from']}"
                    geometry = self._vintage_geometry(
                        namespace, report["subject_key"], used["valid_from"]
                    )
                    geo_namespace = namespace
                else:
                    key = report["subject_key"]
                    geometry, geo_namespace = (
                        station_point["geometry_id"],
                        station_point["namespace"],
                    )
                if key not in receipts:
                    receipts[key] = self.store.geo.relation(
                        geo_namespace,
                        "proximity",
                        geometry,
                        located["point"],
                        scopes=GEO_SCOPES,
                        principal_id=principal_id,
                        tolerance_m=located["radius_m"],
                    )
                if not receipts[key]["result"]["within_tolerance"]:
                    continue
                used["proximity_receipt_id"] = receipts[key]["receipt_id"]
                used["distance_m"] = receipts[key]["result"]["distance_m"]
            parameters = []
            for item in content["parameters"]:
                entry = {
                    **item,
                    "common_parameter": common_parameter(
                        content["provider"], item["parameter"]
                    ),
                    "qc": qc_common(
                        item["qc"]["scheme"],
                        item["qc"].get("native"),
                        raw_text=content.get("raw_text"),
                    ),
                }
                converted = normalise(item.get("value"), item["unit"])
                if converted is not None:
                    entry["normalised"] = {
                        k: converted[k]
                        for k in ("value", "unit", "method", "receipt_sha256")
                    }
                parameters.append(entry)
            rows.append(
                {
                    "station": report["subject_key"],
                    "observed_at": content["observed_at"],
                    "report_type": content["report_type"],
                    "provider": content["provider"],
                    "issuer": content["issuer"],
                    "attribution": content["attribution"],
                    "parameters": parameters,
                    "correction": content.get("correction"),
                    "raw_text": content.get("raw_text"),
                    "location_vintage": used,
                    "revision_id": report["revision_id"],
                    "retrieved_at": report["retrieved_at"],
                    "change_kind": report["change_kind"],
                    "history": report["history"],
                    "locator": content["locator"],
                    "environment": references.setdefault(
                        (
                            report["subject_key"],
                            tuple(p["parameter"] for p in parameters),
                        ),
                        self.links.environment_references(
                            report["subject_key"], [p["parameter"] for p in parameters]
                        ),
                    ),
                }
            )
        rows.sort(key=lambda r: (r["observed_at"], r["station"], r["report_type"]))
        return {
            "contract": "noesis-weather-observations-v1",
            "status": "ok" if rows else NO_REPORT,
            "knowledge_cutoff": knowledge_cutoff,
            "window": [window_from, window_to],
            "station": station,
            "place": located,
            "n": len(rows),
            "reports": rows,
            "excluded_without_location": excluded,
            "semantics": (
                "current revision per report among revisions acquired by the knowledge cutoff; the "
                "location vintage valid at each report's own time; QC flags verbatim with the common "
                "state; missing values stay absent"
            ),
        }

    # --------------------------------------------------------------- forecasts

    def _issuance_matches(
        self,
        namespace: str,
        content: Mapping[str, Any],
        *,
        members: set[str] | None,
        located: Mapping[str, Any] | None,
        scopes: set[str],
    ) -> dict[str, Any] | None:
        location = content["location"]
        if members is not None:
            if (
                location.get("station")
                and wr.station_key(location["station"]) in members
            ):
                return {
                    "rule": "same-station"
                    if len(members) == 1
                    else "equivalent-station",
                    "station": wr.station_key(location["station"]),
                }
            if (
                location.get("declared_station")
                and wr.station_key(location["declared_station"]) in members
            ):
                return {
                    "rule": "declared-grid-point",
                    "station": wr.station_key(location["declared_station"]),
                }
            return None
        geometry = location.get("geometry")
        if geometry is None and location.get("station"):
            point = self.identity.point(
                namespace, wr.station_key(location["station"]), scopes=scopes
            )
        else:
            point = _centroid(geometry) if geometry else None
        if point is None:
            return None
        gap = distance_m(point, located["point"])
        if gap > located["radius_m"]:
            return None
        return {
            "rule": "location within radius of the place",
            "distance_m": round(gap, 1),
        }

    def _issuances(
        self,
        namespace: str,
        scopes: set[str],
        *,
        station: str | None,
        place: Mapping[str, Any] | None,
        cutoff: int | None,
    ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        if bool(station) == bool(place):
            raise WeatherError(
                "invalid_request", "give exactly one of station or place"
            )
        members = (
            set(
                self.identity.equivalent(
                    namespace, station, scopes=scopes, cutoff_ms=cutoff
                )["members"]
            )
            if station
            else None
        )
        located = self._point(namespace, place) if place else None
        result = []
        for item in self.store.currents(
            namespace, record_type="forecast_issuance", scopes=scopes, cutoff_ms=cutoff
        ):
            match = self._issuance_matches(
                namespace,
                item["content"],
                members=members,
                located=located,
                scopes=scopes,
            )
            if match is not None:
                result.append((item, match))
        return result

    def _entry(
        self,
        item: Mapping[str, Any],
        match: Mapping[str, Any],
        valid_ms: int,
        parameter: str | None,
    ) -> dict[str, Any] | None:
        elements = self.store.elements(
            item["revision_id"], parameter=parameter, valid_ms=valid_ms
        )
        if not elements:
            return None
        content = item["content"]
        for element in elements:
            converted = normalise(element.get("value"), element["unit"])
            element["common_parameter"] = common_parameter(
                content["provider"], element["parameter"]
            )
            if converted is not None:
                element["normalised"] = {
                    k: converted[k]
                    for k in ("value", "unit", "method", "receipt_sha256")
                }
        return {
            "provider": content["provider"],
            "product": content["product"],
            "model": content.get("model"),
            "issuer": content["issuer"],
            "originator": content.get("originator"),
            "attribution": content["attribution"],
            "issued_at": content["issued_at"],
            "location": content["location"],
            "match": dict(match),
            "elements": elements,
            "revision_id": item["revision_id"],
            "retrieved_at": item["retrieved_at"],
            "notice": content["notice"],
            "locator": content["locator"],
        }

    def forecast_as_issued(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        valid_time: str,
        issued_before: str,
        station: str | None = None,
        place: Mapping[str, Any] | None = None,
        parameter: str | None = None,
        knowledge_cutoff: str | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_ready(self.conn, namespace)
        valid_ms, before_ms = wr.ms(wr.utc(valid_time)), wr.ms(wr.utc(issued_before))
        knowledge = knowledge_cutoff or issued_before
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for item, match in self._issuances(
            namespace, scopes, station=station, place=place, cutoff=_cutoff(knowledge)
        ):
            content = item["content"]
            if wr.ms(content["issued_at"]) > before_ms:
                continue
            entry = self._entry(item, match, valid_ms, parameter)
            if entry is None:
                continue
            key = (content["provider"], content["product"], content.get("model") or "-")
            if key not in latest or entry["issued_at"] > latest[key]["issued_at"]:
                latest[key] = entry
        forecasts = [latest[k] for k in sorted(latest)]
        return {
            "contract": "noesis-weather-forecast-as-issued-v1",
            "status": "ok" if forecasts else "no forecast on record",
            "valid_time": wr.utc(valid_time),
            "issued_before": wr.utc(issued_before),
            "knowledge_cutoff": knowledge,
            "n": len(forecasts),
            "forecasts": forecasts,
            "semantics": (
                "per provider, product and model the latest issuance at or before issued_before that "
                "has an element at the valid time; published forecasts only, never made by Noesis"
            ),
        }

    def forecast_evolution(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        valid_time: str,
        station: str | None = None,
        place: Mapping[str, Any] | None = None,
        parameter: str | None = None,
        knowledge_cutoff: str | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_ready(self.conn, namespace)
        valid_ms = wr.ms(wr.utc(valid_time))
        issuances = []
        for item, match in self._issuances(
            namespace,
            scopes,
            station=station,
            place=place,
            cutoff=_cutoff(knowledge_cutoff),
        ):
            entry = self._entry(item, match, valid_ms, parameter)
            if entry is not None:
                issuances.append(entry)
        issuances.sort(key=lambda e: (e["issued_at"], e["provider"], e["product"]))
        return {
            "contract": "noesis-weather-forecast-evolution-v1",
            "status": "ok" if issuances else "no forecast on record",
            "valid_time": wr.utc(valid_time),
            "knowledge_cutoff": knowledge_cutoff,
            "n": len(issuances),
            "issuances": issuances,
        }

    # ---------------------------------------------------------------- warnings

    @staticmethod
    def chains(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Group CAP messages by their references (union-find), independent of arrival order."""

        parent: dict[str, str] = {}

        def find(key: str) -> str:
            parent.setdefault(key, key)
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        for message in messages:
            key = f"{message['sender']}|{message['identifier']}"
            find(key)
            for ref in message.get("references") or []:
                a, b = find(key), find(f"{ref['sender']}|{ref['identifier']}")
                if a != b:
                    parent[max(a, b)] = min(a, b)
        groups: dict[str, list[dict[str, Any]]] = {}
        for message in messages:
            groups.setdefault(
                find(f"{message['sender']}|{message['identifier']}"), []
            ).append(message)
        return [
            sorted(group, key=lambda m: (m["sent"], m["identifier"]))
            for _, group in sorted(groups.items())
        ]

    def warnings_in_force(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        principal_id: str,
        place: Mapping[str, Any],
        as_of: str,
        knowledge_cutoff: str | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_ready(self.conn, namespace)
        located = self._point(namespace, place)
        as_of_iso = wr.utc(as_of)
        knowledge = knowledge_cutoff or as_of_iso
        containing = self.store.features.containing(
            namespace,
            collection=WARNING_AREA_COLLECTION,
            point=located["point"],
            principal_id=principal_id,
            scopes=GEO_SCOPES,
        )
        area_keys = {m["native_id"] for m in containing["members"]}
        messages = []
        for item in self.store.currents(
            namespace,
            record_type="warning",
            scopes=scopes,
            cutoff_ms=_cutoff(knowledge),
        ):
            content = item["content"]
            if wr.ms(content["sent"]) <= wr.ms(as_of_iso):
                messages.append(
                    {
                        **content,
                        "revision_id": item["revision_id"],
                        "retrieved_at": item["retrieved_at"],
                    }
                )
        in_force, ended = [], []
        for chain in self.chains(messages):
            latest = chain[-1]
            keys = set()
            for message in chain:
                for index in range(len(message["areas"])):
                    keys.update(WeatherStore.area_keys(message, index))
            if not keys & area_keys:
                continue
            latest_keys = {
                k
                for i in range(len(latest["areas"]))
                for k in WeatherStore.area_keys(latest, i)
            }
            history = [
                {
                    "identifier": m["identifier"],
                    "msg_type": m["msg_type"],
                    "sent": m["sent"],
                    "severity": m.get("severity"),
                    "onset": m.get("onset"),
                    "expires": m.get("expires"),
                    "revision_id": m["revision_id"],
                }
                for m in chain
            ]
            quoted = {
                k: latest.get(k)
                for k in (
                    "event",
                    "severity",
                    "urgency",
                    "certainty",
                    "headline",
                    "description",
                    "instruction",
                    "onset",
                    "effective",
                    "expires",
                    "sender",
                    "sent",
                    "identifier",
                    "msg_type",
                    "provider",
                    "issuer",
                    "attribution",
                    "locator",
                    "language",
                )
                if latest.get(k) is not None
            }
            state, reason = "in_force", None
            begins = latest.get("onset") or latest.get("effective") or latest["sent"]
            if latest["msg_type"] == "Cancel":
                state, reason = (
                    "cancelled",
                    f"cancelled by {latest['identifier']} at {latest['sent']}",
                )
            elif latest.get("expires") is not None and wr.ms(
                latest["expires"]
            ) <= wr.ms(as_of_iso):
                state, reason = "expired", f"expired at {latest['expires']}"
            elif wr.ms(begins) > wr.ms(as_of_iso):
                state, reason = "not_yet_in_force", f"onset {begins}"
            elif latest["areas"] and not latest_keys & area_keys:
                state, reason = (
                    "area_no_longer_covers_place",
                    "the latest message's areas do not contain the place",
                )
            entry = {
                "state": state,
                "reason": reason,
                "message": quoted,
                "chain": history,
                "expires_published": latest.get("expires") is not None,
            }
            (in_force if state == "in_force" else ended).append(entry)
        return {
            "contract": "noesis-weather-warnings-in-force-v1",
            "status": "ok" if in_force else "no warning on record in force",
            "as_of": as_of_iso,
            "knowledge_cutoff": knowledge,
            "place": located,
            "n": len(in_force),
            "in_force": in_force,
            "not_in_force": ended,
            "containment": {
                "receipt_id": (containing.get("receipt") or {}).get("receipt_id"),
                "areas": sorted(area_keys),
                "status": containing["status"],
            },
            "notice": "warnings are quoted as issued by their issuer; Noesis adds no advice",
            "semantics": (
                "messages sent by as_of and acquired by the knowledge cutoff; chains threaded by CAP "
                "references; the latest message decides (Cancel ends it; onset/effective to expires)"
            ),
        }
