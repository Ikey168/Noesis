"""Revisioned operational weather records and their geospatial projection (WX02, #2165; WX07 places).

One store owns ``noesis-weather-record-v1`` (namespace-scoped, revision-addressable, one owner):

* **Records** are keyed by ``namespace + record type + record key``: a station
  and valid-from date (location vintage), a station, report type and
  observation time (observation report), a provider, product, model, location
  and issue time (forecast issuance), or a CAP sender and identifier (warning).
  Revisions are append-only. Nothing is overwritten.
* **Idempotent and correction-aware.** A revision is compared on the
  normalised, source-independent content (:func:`weather_records.content_hash`,
  which leaves out locators and release metadata). Re-acquiring unchanged data
  adds nothing. The comparison is against the **current** revision only, so a
  reversion to earlier content is a new correction. A changed value or QC flag
  is appended as a ``correction`` or ``qc_change`` and never blocks as a
  conflict.
* **Current follows the source's own time.** The current revision is the
  greatest by (precedence tier, source time, acquisition time, sequence). DWD
  ``historical`` outranks ``recent``, a METAR ``receiptTime`` orders
  corrections, and without a source time acquisition order decides. Late
  older data is kept as ``late_history`` and never becomes current. It
  therefore never creates a correction event. Current is computed at read time
  from the revisions acquired by the cutoff, so the answer does not depend on
  arrival order.
* **Stations stay the environment owner's.** Environment ``station`` records
  that adapters emit are registered through
  :class:`src.kb.environment_store.EnvironmentStore` only when that owner holds
  no such station. An existing station is referenced and never rewritten.
* **Places stay the geospatial owner's.** Each location vintage becomes its
  own ``geospatial_places`` place with a point geometry and a feature in
  ``weather-station-locations``. Each CAP area with a published polygon becomes
  a feature in ``weather-warning-areas`` keyed by its warncell id or UGC zone,
  never by name. No spatial table is added.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb import weather_records as wr
from src.kb.weather_records import READ_SCOPE, WRITE_SCOPE, canonical, digest

LOCATION_COLLECTION = "weather-station-locations"
WARNING_AREA_COLLECTION = "weather-warning-areas"
GEO_SCOPES = {
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:calculate",
}
ENVIRONMENT_NAMESPACE = "environment"
PRODUCER = {"name": "noesis-weather-pack", "version": "1.0.0"}
TABLES = (
    "weather_records",
    "weather_revisions",
    "weather_forecast_elements",
    "weather_provider_state",
)

_DDL = """
CREATE TABLE IF NOT EXISTS weather_records(
 record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
 record_key TEXT NOT NULL, subject_key TEXT NOT NULL, reference_ms BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, record_type, record_key));
CREATE TABLE IF NOT EXISTS weather_revisions(
 revision_id TEXT PRIMARY KEY, record_id TEXT NOT NULL, namespace TEXT NOT NULL, seq BIGINT NOT NULL,
 content_hash TEXT NOT NULL, content_json TEXT NOT NULL, precedence INTEGER NOT NULL, source_time_ms BIGINT NOT NULL,
 source_time_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL, run_id TEXT NOT NULL, change_kind TEXT NOT NULL,
 previous_revision_id TEXT, evidence_json TEXT NOT NULL, principal_id TEXT NOT NULL, UNIQUE(record_id, seq));
CREATE TABLE IF NOT EXISTS weather_forecast_elements(
 revision_id TEXT NOT NULL, parameter TEXT NOT NULL, valid_ms BIGINT NOT NULL, valid_time TEXT NOT NULL,
 lead_s BIGINT NOT NULL, value TEXT, unit TEXT NOT NULL, element_kind TEXT NOT NULL,
 PRIMARY KEY(revision_id, parameter, valid_ms));
CREATE TABLE IF NOT EXISTS weather_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_run_id TEXT, PRIMARY KEY(namespace, provider));
"""


class WeatherError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    """The weather scope plus current namespace access (``operator`` bypasses)."""

    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise WeatherError(
            "unauthorized", f"{required} and namespace access are required"
        )


def record_id(namespace: str, record_type: str, record_key: str) -> str:
    return "wx:" + digest([namespace, record_type, record_key])[:24]


def _table(conn: Any, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]
        ).fetchone()
    )


def ready(conn: Any, namespace: str, providers: Iterable[str] | None = None) -> bool:
    """Whether any (or one of the named) provider has had a successful run in the namespace."""

    if not all(_table(conn, t) for t in TABLES):
        return False
    wanted = sorted(set(providers or ()))
    rows = conn.execute(
        "SELECT provider FROM weather_provider_state WHERE namespace=? AND last_success_ms IS NOT NULL",
        [namespace],
    ).fetchall()
    have = {r[0] for r in rows}
    return bool(have & set(wanted)) if wanted else bool(have)


def require_ready(
    conn: Any, namespace: str, providers: Iterable[str] | None = None
) -> None:
    if not ready(conn, namespace, providers):
        raise WeatherError(
            "not_ready",
            "no weather source has run in this namespace yet; run the weather source pack "
            "first (nothing is inferred before a source has run)",
        )


def _subject(record: Mapping[str, Any]) -> str:
    kind = record["record_type"]
    if kind in {"station_location_vintage", "observation_report"}:
        return wr.station_key(record["station"])
    if kind == "forecast_issuance":
        return record["location"]["ref"]
    return f"{record['sender']}|{record['identifier']}"


def _source_time(record: Mapping[str, Any], retrieved_at_ms: int) -> tuple[int, str]:
    if record.get("source_time"):
        return int(wr.ms(record["source_time"])), "source_stated"
    return int(retrieved_at_ms), "acquisition_order"


def order_key(revision: Mapping[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(revision["precedence"]),
        int(revision["source_time_ms"]),
        int(revision["retrieved_at_ms"]),
        int(revision["seq"]),
    )


def _qc_only(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """True when two observation contents differ only in QC flags."""

    if (
        before.get("record_type") != "observation_report"
        or after.get("record_type") != "observation_report"
    ):
        return False

    def strip(item: Mapping[str, Any]) -> Any:
        return {
            **item,
            "parameters": [
                {k: v for k, v in p.items() if k != "qc"} for p in item["parameters"]
            ],
        }

    return canonical(strip(before)) == canonical(strip(after))


class WeatherStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Any = None) -> None:
        from src.kb.geospatial_features import GeospatialFeatureStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.features = GeospatialFeatureStore(
            conn, initialize=initialize, now=self.now
        )
        self.geo = self.features.geo

    # ----------------------------------------------------------------- writes

    def apply(
        self,
        namespace: str,
        records: Iterable[Mapping[str, Any]],
        *,
        run_id: str,
        principal_id: str,
        scopes: Iterable[str],
        evidence: Mapping[str, Any] | None = None,
        retrieved_at_ms: int | None = None,
        environment_namespace: str = ENVIRONMENT_NAMESPACE,
        environment_scopes: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """Apply validated records; unchanged content is a no-op, changes append revisions."""

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if namespace == "global":
            raise WeatherError(
                "namespace_forbidden",
                "weather records are written to a caller namespace",
            )
        retrieved = int(retrieved_at_ms or self.now())
        stations, weather = [], []
        for item in records:
            if dict(item).get("contract") == "noesis-environment-record-v1":
                stations.append(dict(item))
            else:
                weather.append(wr.validate(dict(item)))
        counts = {
            "revisions": 0,
            "unchanged": 0,
            "corrections": 0,
            "late_history": 0,
            "places": 0,
            "features": 0,
            "stations_registered": 0,
            "stations_referenced": 0,
        }
        if stations:
            registered = self.register_stations(
                environment_namespace,
                stations,
                run_id=run_id,
                principal_id=principal_id,
                scopes=set(
                    environment_scopes if environment_scopes is not None else scopes
                ),
            )
            counts["stations_registered"] = registered["registered"]
            counts["stations_referenced"] = registered["referenced"]
        order = {t: i for i, t in enumerate(wr.RECORD_TYPES)}
        projections: dict[str, list[tuple[dict[str, Any], str]]] = {}
        for record in sorted(
            weather, key=lambda r: (order[r["record_type"]], r["record_key"])
        ):
            outcome, revision_id = self._apply_one(
                namespace,
                record,
                run_id=run_id,
                principal_id=principal_id,
                evidence=dict(evidence or {}),
                retrieved=retrieved,
            )
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome in {"revisions", "corrections"} and record["record_type"] in {
                "station_location_vintage",
                "warning",
            }:
                projections.setdefault(record["record_type"], []).append(
                    (record, revision_id)
                )
        for record, revision_id in projections.get("station_location_vintage", []):
            counts["places"] += self._place(
                namespace, record, revision_id, principal_id=principal_id
            )
        counts["features"] += self._project_locations(
            namespace,
            [r for r, _ in projections.get("station_location_vintage", [])],
            run_id=run_id,
            principal_id=principal_id,
        )
        counts["features"] += self._project_warning_areas(
            namespace,
            [r for r, _ in projections.get("warning", [])],
            run_id=run_id,
            principal_id=principal_id,
        )
        self._provider_state(
            namespace,
            {r["provider"] for r in weather},
            success=retrieved,
            run_id=run_id,
        )
        return counts

    def register_stations(
        self,
        environment_namespace: str,
        stations: list[dict[str, Any]],
        *,
        run_id: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Register stations unknown to the environment owner through that owner; reference the others."""

        from src.kb.environment_records import validate as validate_environment
        from src.kb.environment_store import EnvironmentStore

        env = EnvironmentStore(self.conn, now=self.now)
        missing, referenced = [], 0
        for station in stations:
            station = validate_environment(station)
            if station["record_type"] != "station":
                raise WeatherError(
                    "boundary",
                    "the Weather pack writes only station records to the environment owner",
                )
            if env.find(
                environment_namespace,
                "station",
                station["provider"],
                station["native_id"],
            ):
                referenced += 1  # owned there already: never rewritten from here
            else:
                missing.append(station)
        if missing:
            env.apply(
                environment_namespace,
                missing,
                run_id=f"{run_id}:environment-stations",
                principal_id=principal_id,
                scopes=scopes,
                evidence={"registered_by": "weather", "run_id": run_id},
                execution="weather-registration",
                observed_at_ms=self.now(),
            )
        return {"registered": len(missing), "referenced": referenced}

    def _revisions_raw(self, rid: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision_id, seq, content_hash, precedence, source_time_ms, retrieved_at_ms FROM weather_revisions "
            "WHERE record_id=? ORDER BY seq",
            [rid],
        ).fetchall()
        keys = (
            "revision_id",
            "seq",
            "content_hash",
            "precedence",
            "source_time_ms",
            "retrieved_at_ms",
        )
        return [dict(zip(keys, r)) for r in rows]

    def _apply_one(
        self,
        namespace: str,
        record: dict[str, Any],
        *,
        run_id: str,
        principal_id: str,
        evidence: dict[str, Any],
        retrieved: int,
    ) -> tuple[str, str | None]:
        rid = record_id(namespace, record["record_type"], record["record_key"])
        chash = wr.content_hash(record)
        source_ms, basis = _source_time(record, retrieved)
        existing = self.conn.execute(
            "SELECT 1 FROM weather_records WHERE record_id=?", [rid]
        ).fetchone()
        revisions = self._revisions_raw(rid) if existing else []
        current = max(revisions, key=order_key) if revisions else None
        if current is not None and current["content_hash"] == chash:
            return "unchanged", current["revision_id"]
        if any(
            r["content_hash"] == chash
            and r["precedence"] == record["precedence"]
            and r["source_time_ms"] == source_ms
            for r in revisions
        ):
            return (
                "unchanged",
                None,
            )  # the same statement from the same source time, acquired again
        if not existing:
            self.conn.execute(
                "INSERT INTO weather_records VALUES (?,?,?,?,?,?,?,?)",
                [
                    rid,
                    namespace,
                    record["record_type"],
                    record["provider"],
                    record["record_key"],
                    _subject(record),
                    int(wr.ms(record["reference_time"])),
                    retrieved,
                ],
            )
        seq = (max(r["seq"] for r in revisions) + 1) if revisions else 1
        candidate = {
            "precedence": record["precedence"],
            "source_time_ms": source_ms,
            "retrieved_at_ms": retrieved,
            "seq": seq,
        }
        if current is None:
            change, outcome = "initial", "revisions"
        elif order_key(candidate) > order_key(current):
            before = json.loads(
                self.conn.execute(
                    "SELECT content_json FROM weather_revisions WHERE revision_id=?",
                    [current["revision_id"]],
                ).fetchone()[0]
            )
            change = (
                "qc_change"
                if _qc_only(wr.content(before), wr.content(record))
                else "correction"
            )
            outcome = "corrections"
        else:
            change, outcome = "late_history", "late_history"
        revision_id = "wx-rev:" + digest([rid, seq, chash])[:24]
        self.conn.execute(
            "INSERT INTO weather_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                rid,
                namespace,
                seq,
                chash,
                canonical(record),
                int(record["precedence"]),
                source_ms,
                basis,
                retrieved,
                run_id,
                change,
                current["revision_id"] if current else None,
                canonical(evidence),
                principal_id,
            ],
        )
        if record["record_type"] == "forecast_issuance":
            for element in record["elements"]:
                self.conn.execute(
                    "INSERT INTO weather_forecast_elements VALUES (?,?,?,?,?,?,?,?)",
                    [
                        revision_id,
                        element["parameter"],
                        int(wr.ms(element["valid_time"])),
                        element["valid_time"],
                        int(element["lead_time_s"]),
                        element.get("value"),
                        element["unit"],
                        element["kind"],
                    ],
                )
        return outcome, revision_id

    # ------------------------------------------------------------- places

    def _place(
        self,
        namespace: str,
        record: dict[str, Any],
        revision_id: str,
        *,
        principal_id: str,
    ) -> int:
        """One geospatial place (point geometry) per location vintage; the station keeps one identity."""

        key = f"weather:location:{wr.station_key(record['station'])}:{record['valid_from']}"
        row = self.conn.execute(
            "SELECT place_id FROM geospatial_places WHERE namespace=? AND place_key=?",
            [namespace, key],
        ).fetchone()
        placed = 0
        name = f"{record.get('name') or wr.station_key(record['station'])} (from {record['valid_from']})"
        if row is None:
            place = self.geo.register_place(
                namespace,
                name,
                "weather-station-location",
                names=[{"value": name, "language": "und", "kind": "canonical"}],
                source_ids={record["provider"]: record["source_record_id"]},
                parent_ids=[],
                principal_id=principal_id,
                scopes=GEO_SCOPES,
                place_key=key,
                observed_at_ms=self.now(),
                producer=PRODUCER,
                provenance={
                    "source_url": record["locator"]["url"],
                    "revision_id": revision_id,
                    "station": record["station"],
                },
            )
            place_id = place["place_id"]
            placed = 1
        else:
            place_id = row[0]
        self.geo.store_geometry(
            namespace,
            {
                "type": "Point",
                "coordinates": [float(record["longitude"]), float(record["latitude"])],
            },
            place_id=place_id,
            crs="EPSG:4326",
            precision_m=0.0,
            simplified_from=None,
            disputed=False,
            admin_hierarchy=[],
            source={
                "kind": "weather-location-vintage",
                "revision_id": revision_id,
                "provider": record["provider"],
            },
            evidence=[
                {"kind": "provider-record", "source_url": record["locator"]["url"]}
            ],
            principal_id=principal_id,
            scopes=GEO_SCOPES,
            observed_at_ms=self.now(),
            producer=PRODUCER,
            policy={"crs": "explicit-v1", "precision": "provider coordinates"},
        )
        return placed

    def _observe_features(
        self,
        namespace: str,
        collection: str,
        provider: str,
        items: list[dict[str, Any]],
        *,
        run_id: str,
        principal_id: str,
    ) -> int:
        if not items:
            return 0
        feature_run = f"{run_id}:{collection}:{provider}"
        source_id = f"weather:{provider}"
        start = self.conn.execute(
            "SELECT count(*) FROM geospatial_feature_pages WHERE run_id=? AND source_id=?",
            [feature_run, source_id],
        ).fetchone()[0]
        scope = {"weather_selection": provider, "collection": collection}
        page = {
            "start_index": int(start),
            "number_matched": None,
            "number_returned": len(items),
            "provider_timestamp": None,
            "response_sha256": None,
            "scope": scope,
            "scope_hash": digest(scope),
            "complete_scope": False,
            "final_page": True,
        }
        for item in items:
            item["feature_page"] = page
        states = self.features.observe_page(
            feature_run,
            source_id,
            namespace,
            items,
            policy={
                "precision": {
                    "status": "unknown",
                    "basis": "provider coordinates without declared accuracy",
                }
            },
            documents={},
            provenance={
                "kind": "weather-pack",
                "provider": provider,
                "attribution": provider,
            },
            principal_id=principal_id,
        )
        # Bounded selections: the snapshot is partial, so absence never removes a feature.
        self.features.finish_snapshot(
            feature_run,
            source_id,
            namespace,
            provider=provider,
            collection=collection,
            status="complete",
            principal_id=principal_id,
        )
        return sum(states.values())

    def _project_locations(
        self,
        namespace: str,
        records: list[dict[str, Any]],
        *,
        run_id: str,
        principal_id: str,
    ) -> int:
        by_provider: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            native = f"{wr.station_key(record['station'])}@{record['valid_from']}"
            geometry = {
                "type": "Point",
                "coordinates": [float(record["longitude"]), float(record["latitude"])],
            }
            properties = {
                "station": wr.station_key(record["station"]),
                "valid_from": record["valid_from"],
                "valid_to": record.get("valid_to"),
                "open_ended": bool(record.get("open_ended")),
                "name": record.get("name"),
            }
            by_provider.setdefault(record["provider"], []).append(
                {
                    "id": native,
                    "title": record.get("name") or native,
                    "feature": {
                        "provider": record["provider"],
                        "collection": LOCATION_COLLECTION,
                        "native_id": native,
                        "properties": properties,
                        "geometry": geometry,
                        "source_geometry": geometry,
                        "source_crs": "EPSG:4326",
                        "axis_order": "east_north",
                        "source_sha256": wr.content_hash(record),
                    },
                }
            )
        return sum(
            self._observe_features(
                namespace,
                LOCATION_COLLECTION,
                provider,
                items,
                run_id=run_id,
                principal_id=principal_id,
            )
            for provider, items in sorted(by_provider.items())
        )

    @staticmethod
    def area_keys(area: Mapping[str, Any]) -> list[str]:
        """Feature native ids of one CAP area: warncell ids and UGC zones, never names."""

        return sorted(
            f"{'warncell' if c['scheme'] == 'WARNCELLID' else 'ugc'}:{c['value']}"
            for c in area.get("codes") or []
            if c["scheme"] in {"WARNCELLID", "UGC"}
        )

    def _project_warning_areas(
        self,
        namespace: str,
        records: list[dict[str, Any]],
        *,
        run_id: str,
        principal_id: str,
    ) -> int:
        by_provider: dict[str, dict[str, dict[str, Any]]] = {}
        for record in sorted(records, key=lambda r: (r["sent"], r["identifier"])):
            for area in record["areas"]:
                keys = self.area_keys(area)
                # A polygon is attributed to an area code only when the area names exactly one code.
                if len(keys) != 1 or not area["polygons"]:
                    continue
                polygons = area["polygons"]
                geometry = (
                    {"type": "Polygon", "coordinates": [polygons[0]]}
                    if len(polygons) == 1
                    else {
                        "type": "MultiPolygon",
                        "coordinates": [[p] for p in polygons],
                    }
                )
                by_provider.setdefault(record["provider"], {})[keys[0]] = {
                    "id": keys[0],
                    "title": area.get("description") or keys[0],
                    "feature": {
                        "provider": record["provider"],
                        "collection": WARNING_AREA_COLLECTION,
                        "native_id": keys[0],
                        "properties": {
                            "area_code": keys[0],
                            "description": area.get("description"),
                        },
                        "geometry": geometry,
                        "source_geometry": geometry,
                        "source_crs": "EPSG:4326",
                        "axis_order": "east_north",
                        "source_sha256": digest(geometry),
                    },
                }
        return sum(
            self._observe_features(
                namespace,
                WARNING_AREA_COLLECTION,
                provider,
                list(items.values()),
                run_id=run_id,
                principal_id=principal_id,
            )
            for provider, items in sorted(by_provider.items())
        )

    # ------------------------------------------------------------- provider state

    def _provider_state(
        self,
        namespace: str,
        providers: Iterable[str],
        *,
        success: int | None = None,
        failure: int | None = None,
        code: str | None = None,
        run_id: str | None = None,
    ) -> None:
        for provider in sorted(providers):
            self.conn.execute(
                "INSERT INTO weather_provider_state VALUES (?,?,?,?,?,?) ON CONFLICT (namespace, provider) DO UPDATE SET "
                "last_success_ms=coalesce(excluded.last_success_ms, weather_provider_state.last_success_ms), "
                "last_failure_ms=coalesce(excluded.last_failure_ms, weather_provider_state.last_failure_ms), "
                "last_failure_code=CASE WHEN excluded.last_failure_ms IS NULL THEN weather_provider_state.last_failure_code "
                "ELSE excluded.last_failure_code END, "
                "last_run_id=coalesce(excluded.last_run_id, weather_provider_state.last_run_id)",
                [namespace, provider, success, failure, code, run_id],
            )

    def record_success(
        self,
        namespace: str,
        provider: str,
        *,
        run_id: str,
        scopes: Iterable[str],
        at_ms: int | None = None,
    ) -> None:
        """A completed run with nothing new still counts as the source having run."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self._provider_state(
            namespace, [provider], success=int(at_ms or self.now()), run_id=run_id
        )

    def record_failure(
        self,
        namespace: str,
        provider: str,
        *,
        code: str,
        run_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        now = self.now()
        self._provider_state(
            namespace, [provider], failure=now, code=code, run_id=run_id
        )
        return {
            "provider": provider,
            "failure_code": code,
            "recorded_at_ms": now,
            "effect": "stored revisions unchanged; answers name the provider as stale",
        }

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        row = None
        if _table(self.conn, "weather_provider_state"):
            row = self.conn.execute(
                "SELECT last_success_ms, last_failure_ms, last_failure_code, last_run_id FROM "
                "weather_provider_state WHERE namespace=? AND provider=?",
                [namespace, provider],
            ).fetchone()
        if row is None:
            return {
                "provider": provider,
                "last_success_ms": None,
                "stale": True,
                "reason": "never acquired",
            }
        stale = row[0] is None or (row[1] is not None and row[1] > row[0])
        return {
            "provider": provider,
            "last_success_ms": row[0],
            "last_failure_ms": row[1],
            "last_failure_code": row[2],
            "last_run_id": row[3],
            "stale": stale,
        }

    def revisions(
        self,
        namespace: str,
        rid: str,
        *,
        scopes: Iterable[str],
        cutoff_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Every revision acquired by the cutoff, oldest acquisition first, with ``current`` marked."""

        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT revision_id, seq, content_hash, content_json, precedence, source_time_ms, source_time_basis, "
            "retrieved_at_ms, run_id, change_kind, previous_revision_id, evidence_json FROM weather_revisions "
            "WHERE namespace=? AND record_id=? AND (? IS NULL OR retrieved_at_ms<=?) ORDER BY seq",
            [namespace, rid, cutoff_ms, cutoff_ms],
        ).fetchall()
        keys = (
            "revision_id",
            "seq",
            "content_hash",
            "content",
            "precedence",
            "source_time_ms",
            "source_time_basis",
            "retrieved_at_ms",
            "run_id",
            "change_kind",
            "previous_revision_id",
            "evidence",
        )
        result = []
        for row in rows:
            item = dict(zip(keys, row))
            item["content"] = json.loads(item["content"])
            item["evidence"] = json.loads(item["evidence"])
            item["retrieved_at"] = wr.iso(item["retrieved_at_ms"])
            item["source_time"] = wr.iso(item["source_time_ms"])
            result.append(item)
        if result:
            top = max(result, key=order_key)
            for item in result:
                item["current"] = item is top
        return result

    def current(
        self,
        namespace: str,
        rid: str,
        *,
        scopes: Iterable[str],
        cutoff_ms: int | None = None,
    ) -> dict[str, Any] | None:
        revisions = self.revisions(namespace, rid, scopes=scopes, cutoff_ms=cutoff_ms)
        if not revisions:
            return None
        top = next(r for r in revisions if r["current"])
        head = self.conn.execute(
            "SELECT record_type, provider, record_key, subject_key FROM weather_records "
            "WHERE record_id=?",
            [rid],
        ).fetchone()
        superseded_by = [
            r["revision_id"] for r in revisions if order_key(r) > order_key(top)
        ]
        return {
            "record_id": rid,
            "record_type": head[0],
            "provider": head[1],
            "record_key": head[2],
            "subject_key": head[3],
            **top,
            "revision_count": len(revisions),
            "superseded": bool(superseded_by),
            "history": [
                {
                    "revision_id": r["revision_id"],
                    "change_kind": r["change_kind"],
                    "retrieved_at": r["retrieved_at"],
                    "source_time": r["source_time"],
                    "source_time_basis": r["source_time_basis"],
                    "current": r["current"],
                }
                for r in revisions
            ],
        }

    def record_ids(
        self,
        namespace: str,
        *,
        record_type: str,
        subject_keys: Iterable[str] | None = None,
        provider: str | None = None,
        reference_from_ms: int | None = None,
        reference_to_ms: int | None = None,
    ) -> list[str]:
        subjects = sorted(set(subject_keys)) if subject_keys is not None else None
        if subjects == []:
            return []
        sql = (
            "SELECT record_id FROM weather_records WHERE namespace=? AND record_type=? AND (? IS NULL OR provider=?) "
            "AND (? IS NULL OR reference_ms>=?) AND (? IS NULL OR reference_ms<?)"
        )
        params: list[Any] = [
            namespace,
            record_type,
            provider,
            provider,
            reference_from_ms,
            reference_from_ms,
            reference_to_ms,
            reference_to_ms,
        ]
        if subjects is not None:
            sql += " AND subject_key IN (" + ",".join("?" * len(subjects)) + ")"
            params += subjects
        return [
            r[0]
            for r in self.conn.execute(
                sql + " ORDER BY reference_ms, record_key", params
            ).fetchall()
        ]

    def currents(
        self,
        namespace: str,
        *,
        record_type: str,
        scopes: Iterable[str],
        cutoff_ms: int | None = None,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """Current revision per record (selected per record first, then the caller filters)."""

        result = []
        for rid in self.record_ids(namespace, record_type=record_type, **filters):
            item = self.current(namespace, rid, scopes=scopes, cutoff_ms=cutoff_ms)
            if item is not None:
                result.append(item)
        return result

    def elements(
        self,
        revision_id: str,
        *,
        parameter: str | None = None,
        valid_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT parameter, valid_time, lead_s, value, unit, element_kind FROM weather_forecast_elements "
            "WHERE revision_id=? AND (? IS NULL OR parameter=?) AND (? IS NULL OR valid_ms=?) ORDER BY parameter, valid_ms",
            [revision_id, parameter, parameter, valid_ms, valid_ms],
        ).fetchall()
        result = []
        for name, valid_time, lead, value, unit, kind in rows:
            item = {
                "parameter": name,
                "valid_time": valid_time,
                "lead_time_s": int(lead),
                "unit": unit,
                "kind": kind,
            }
            if value is not None:
                item["value"] = value
            else:
                item["missing"] = "not published"
            result.append(item)
        return result

    def location_vintages(
        self,
        namespace: str,
        station: str,
        *,
        scopes: Iterable[str],
        cutoff_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        items = self.currents(
            namespace,
            record_type="station_location_vintage",
            scopes=scopes,
            cutoff_ms=cutoff_ms,
            subject_keys=[station],
        )
        return sorted(items, key=lambda v: v["content"]["valid_from"])


class WeatherProjector:
    """Source-pack runtime projector for ``noesis-weather-record-v1`` pages."""

    @staticmethod
    def scopes_for(namespace: str, environment_namespace: str) -> set[str]:
        from src.kb.environment_records import READ_SCOPE as ENV_READ
        from src.kb.environment_records import WRITE_SCOPE as ENV_WRITE

        return {
            WRITE_SCOPE,
            READ_SCOPE,
            f"namespace:{namespace}:write",
            f"namespace:{namespace}:read",
            ENV_WRITE,
            ENV_READ,
            f"namespace:{environment_namespace}:write",
            f"namespace:{environment_namespace}:read",
        }

    def __init__(self, conn: Any) -> None:
        self.store = WeatherStore(conn)

    @staticmethod
    def _spec(source: Mapping[str, Any]) -> tuple[str, str, str]:
        spec = dict(source.get("weather") or {})
        return (
            str(spec.get("namespace") or "weather"),
            str(spec.get("environment_namespace") or ENVIRONMENT_NAMESPACE),
            str(spec.get("provider") or ""),
        )

    def project_page(
        self,
        *,
        run_id,
        manifest,
        source,
        records,
        documents,
        page_receipt,
        principal_id,
    ):
        namespace, env_namespace, _ = self._spec(source)
        payload = []
        for item in records:
            payload.extend(dict(r) for r in item.get("weather_records") or [])
        evidence = {
            "run_id": run_id,
            "source_id": source["source_id"],
            "pack_id": manifest["pack_id"],
            "pack_version": manifest["version"],
            "response_sha256": page_receipt.get("response_sha256"),
            "request": page_receipt.get("request"),
            "documents": sorted(str(d["document_id"]) for d in documents),
        }
        # The runtime's clock (document ingestion time) is the acquisition time, so as-of reads agree with receipts.
        retrieved = max(
            (
                int(d["ingested_at"])
                for d in documents
                if d.get("ingested_at") is not None
            ),
            default=None,
        )
        return self.store.apply(
            namespace,
            payload,
            run_id=run_id,
            principal_id=principal_id,
            scopes=self.scopes_for(namespace, env_namespace),
            evidence=evidence,
            retrieved_at_ms=retrieved,
            environment_namespace=env_namespace,
        )

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace, env_namespace, provider = self._spec(source)
        scopes = self.scopes_for(namespace, env_namespace)
        if status == "complete":
            self.store.record_success(namespace, provider, run_id=run_id, scopes=scopes)
        else:
            self.store.record_failure(
                namespace,
                provider,
                code="source_run_" + status,
                run_id=run_id,
                scopes=scopes,
            )
        return {
            "status": status,
            "provider": provider,
            "namespace": namespace,
            "provider_state": self.store.provider_state(namespace, provider),
        }
