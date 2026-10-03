"""Revisioned environmental records, series vintages and their geospatial projection (E02-E07, E09).

One store owns the ``noesis-environment-record-v1`` records (C01.2 rules:
namespace-scoped, revision-addressable, one owner):

* **Records** (stations, series metadata, facilities, grid events) are keyed
  by ``namespace + record type + provider + native id``. A content change is
  a new immutable revision; nothing is overwritten.
* **Places stay with the geospatial owner.** Stations, grid cells and
  facilities become ``geospatial_places`` with point geometries through
  :class:`~src.kb.geospatial.GeospatialStore`, and stations/facilities are
  also projected as features (collections ``environment-stations`` and
  ``environment-facilities``) through
  :class:`~src.kb.geospatial_features.GeospatialFeatureStore`, so the
  existing ``within`` query, relations and receipts answer spatial questions.
  Bounded selections are recorded as *partial* snapshots: absence never
  removes a station.
* **Vintages** follow the economic provider-vintage pattern
  (``docs/guides/economic-provider-vintages.md``): every re-published value
  set is a new vintage carrying ``release_at_ms`` / ``retrieved_at_ms`` with
  ``release_at_basis``, ``retrieved_at_basis``, ``vintage_basis`` and
  ``release_time_status``; earlier vintages stay addressable.
* **Kind is part of identity.** A series never changes kind; forecast and
  model values are stored with their kind and are never returned as
  observations.
* **Overlap is linked, not merged.** Stations published by two providers
  (e.g. OpenAQ re-publishing UBA) are joined by a link row with its basis.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from typing import Any

from src.kb import environment_records as er
from src.kb.environment_records import READ_SCOPE, WRITE_SCOPE, canonical, digest

STATION_COLLECTION = "environment-stations"
FACILITY_COLLECTION = "environment-facilities"
GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate"}
LINK_DISTANCE_M = 250.0
PRODUCER = {"name": "noesis-environment-pack", "version": "1.0.0"}

_DDL = """
CREATE TABLE IF NOT EXISTS environment_records(
 record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
 native_id TEXT NOT NULL, kind TEXT, created_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, record_type, provider, native_id));
CREATE TABLE IF NOT EXISTS environment_record_revisions(
 revision_id TEXT PRIMARY KEY, record_id TEXT NOT NULL, namespace TEXT NOT NULL, revision BIGINT NOT NULL,
 content_hash TEXT NOT NULL, content_json TEXT NOT NULL, place_id TEXT, geometry_id TEXT, feature_id TEXT,
 observed_at_ms BIGINT NOT NULL, run_id TEXT NOT NULL, evidence_json TEXT NOT NULL, principal_id TEXT NOT NULL,
 UNIQUE(record_id, revision));
CREATE TABLE IF NOT EXISTS environment_record_current(
 record_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS environment_vintages(
 vintage_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_id TEXT NOT NULL, provider TEXT NOT NULL,
 sequence BIGINT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL, release_at_ms BIGINT NOT NULL,
 release_at_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL, retrieved_at_basis TEXT NOT NULL,
 vintage_basis TEXT NOT NULL, release_time_status TEXT NOT NULL, revision_of TEXT, values_hash TEXT NOT NULL,
 unit TEXT, run_id TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 UNIQUE(record_id, sequence));
CREATE TABLE IF NOT EXISTS environment_values(
 vintage_id TEXT NOT NULL, start_ms BIGINT NOT NULL, period_start TEXT NOT NULL, period_end TEXT, value TEXT,
 unit TEXT, normalized_value TEXT, normalized_unit TEXT, status TEXT NOT NULL, quality TEXT, flags_json TEXT NOT NULL,
 PRIMARY KEY(vintage_id, start_ms));
CREATE TABLE IF NOT EXISTS environment_record_links(
 link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, relation TEXT NOT NULL, left_record TEXT NOT NULL,
 right_record TEXT NOT NULL, state TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS environment_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_execution TEXT, last_run_id TEXT, PRIMARY KEY(namespace, provider));
"""


class EnvironmentStoreError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def authorize(namespace, scopes, required, *, write=False):
    """Environment scope plus current namespace access (operator bypasses)."""

    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise EnvironmentStoreError("unauthorized", f"{required} and namespace access are required")


def ms(value):
    """ISO date/instant -> epoch ms (dates are UTC midnight)."""

    if value is None:
        return None
    text = str(value).replace("Z", "+00:00")
    if len(text) == 10:
        text += "T00:00:00+00:00"
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def record_id(namespace, record_type, provider, native_id):
    return "env:" + digest([namespace, record_type, provider, native_id])[:24]


def _load(value, default):
    return default if value in (None, "") else json.loads(value)


class EnvironmentStore:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.geospatial_features import GeospatialFeatureStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.features = GeospatialFeatureStore(conn, initialize=initialize, now=self.now)
        self.geo = self.features.geo

    # ----------------------------------------------------------------- writes

    def apply(self, namespace, records, *, run_id, principal_id, scopes, evidence=None, execution="injected",
              observed_at_ms=None, record_provider_state=True):
        """Apply validated records; unchanged content is a no-op, changes are new revisions/vintages.

        ``record_provider_state=False`` is for another provider's rows stored here (the environment.waste transfer
        rows, #2740): they never mark the environment.core provider's own acquisition as fresh.
        """

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if namespace == "global":
            raise EnvironmentStoreError("namespace_forbidden", "environment records are written to a caller namespace")
        observed = int(observed_at_ms or self.now())
        ordered = sorted((er.validate(dict(r)) for r in records),
                         key=lambda r: ["station", "facility", "observation_series", "grid_event",
                                        "indicator_vintage"].index(r["record_type"]))
        counts = {"revisions": 0, "unchanged": 0, "vintages": 0, "links": 0, "places": 0}
        features: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record in ordered:
            outcome = self._apply_one(namespace, record, run_id=run_id, principal_id=principal_id,
                                      evidence=dict(evidence or {}), observed=observed, features=features)
            for key, value in outcome.items():
                counts[key] = counts.get(key, 0) + value
        counts["features"] = self._project_features(namespace, features, run_id=run_id, principal_id=principal_id)
        if record_provider_state:
            self._provider_state(namespace, {r["provider"] for r in ordered}, success=observed, execution=execution,
                                 run_id=run_id)
        return counts

    def _apply_one(self, namespace, record, *, run_id, principal_id, evidence, observed, features):
        rid = record_id(namespace, record["record_type"], record["provider"], record["native_id"])
        kind = record.get("kind")
        existing = self.conn.execute("SELECT kind FROM environment_records WHERE record_id=?", [rid]).fetchone()
        if existing and existing[0] != kind:
            raise EnvironmentStoreError("kind_conflict", "a series or grid event never changes kind")
        values = None
        if record["record_type"] == "observation_series":
            values = record["values"]
        elif record["record_type"] == "grid_event" and record["event_type"] != "unavailability":
            values = record["points"]
        content = {k: v for k, v in record.items() if k not in {"values", "points"}}
        if values is not None:
            content["values_digest"] = digest(values)
        elif record["record_type"] == "grid_event":
            content["points"] = record["points"]
        counts = {"revisions": 0, "unchanged": 0, "vintages": 0, "links": 0, "places": 0}
        content_hash = digest(content)
        current = self.conn.execute(
            "SELECT r.revision, r.content_hash, r.place_id, r.geometry_id FROM environment_record_current c "
            "JOIN environment_record_revisions r USING(revision_id) WHERE c.record_id=?", [rid]).fetchone()
        place_id = geometry_id = feature_id = None
        if current and current[1] == content_hash:
            counts["unchanged"] += 1
            place_id = current[2]
        else:
            if not existing:
                self.conn.execute("INSERT INTO environment_records VALUES (?,?,?,?,?,?,?)",
                                  [rid, namespace, record["record_type"], record["provider"], record["native_id"],
                                   kind, observed])
            place_id, geometry_id, feature_id, placed = self._place(namespace, record, rid, principal_id=principal_id,
                                                                    prior_place=current[2] if current else None,
                                                                    observed=observed)
            counts["places"] += placed
            revision = int(current[0]) + 1 if current else 1
            revision_id = "env-rev:" + digest([rid, revision, content_hash])[:24]
            self.conn.execute(
                "INSERT INTO environment_record_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [revision_id, rid, namespace, revision, content_hash, canonical(content), place_id, geometry_id,
                 feature_id, observed, run_id, canonical(evidence), principal_id])
            self.conn.execute("INSERT OR REPLACE INTO environment_record_current VALUES (?,?,?)", [rid, revision_id, revision])
            counts["revisions"] += 1
            if record["record_type"] in {"station", "facility"} and record.get("geometry"):
                collection = STATION_COLLECTION if record["record_type"] == "station" else FACILITY_COLLECTION
                features.setdefault((collection, record["provider"]), []).append(record)
        if values is not None:
            counts["vintages"] += self._vintage(namespace, rid, record, values, run_id=run_id, evidence=evidence,
                                                observed=observed)
        if record["record_type"] == "facility":
            for release_series in self._release_series(record):
                counts["vintages"] += self._apply_one(namespace, release_series, run_id=run_id, principal_id=principal_id,
                                                      evidence=evidence, observed=observed, features=features)["vintages"]
        counts["links"] += self._link(namespace, rid, record, observed=observed)
        return counts

    def _release_series(self, record):
        """Facility releases as annual observation series (one per pollutant and medium)."""

        grouped: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
        for item in record.get("releases") or []:
            grouped.setdefault((item["pollutant"], item.get("medium")), []).append(item)
        result = []
        for (pollutant, medium), items in sorted(grouped.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            unit = items[0]["unit"]
            result.append(er.series(
                record["provider"], f"{record['native_id']}:{pollutant}:{medium or 'unknown'}",
                f"{record['title']} {pollutant} to {medium or 'unknown medium'}", source_url=record["source_url"],
                location={"kind": "facility", "ref": f"{record['provider']}:{record['native_id']}"},
                indicator={"code": pollutant, "name": pollutant, "scheme": "E-PRTR pollutant", "medium": medium},
                unit=unit, interval="P1Y", aggregation="total", kind=items[0]["kind"],
                values=[{"start": f"{i['year']}-01-01", "end": f"{i['year'] + 1}-01-01", "value": i["value"],
                         "status": "unknown", "flags": {"method": i.get("method"), "accidental": i.get("accidental")}}
                        for i in sorted(items, key=lambda i: i["year"])],
                status_basis="reported releases; determination method (M/C/E) kept as published",
                release={"basis": "acquisition"}))
        return result

    def _place(self, namespace, record, rid, *, principal_id, prior_place, observed):
        """Register stations/facilities/grid cells as geospatial places with point geometry."""

        geometry, place_type, precision = None, None, 10.0
        if record["record_type"] == "station":
            geometry, place_type = record["geometry"], "environment-station"
        elif record["record_type"] == "facility" and record.get("geometry"):
            geometry, place_type = record["geometry"], "environment-facility"
        elif record["record_type"] == "observation_series" and record["location"]["kind"] == "grid-cell":
            geometry, place_type = record["location"]["geometry"], "environment-grid-cell"
            precision = float(record["location"].get("precision_m") or 0)
        if geometry is None:
            return prior_place, None, None, 0
        key = (f"environment:{record['provider']}:{record['location']['ref']}" if place_type == "environment-grid-cell"
               else f"environment:{record['provider']}:{record['record_type']}:{record['native_id']}")
        row = self.conn.execute("SELECT place_id FROM geospatial_places WHERE namespace=? AND place_key=?",
                                [namespace, key]).fetchone()
        placed = 0
        if row is None:
            name = record["title"] if place_type != "environment-grid-cell" else record["location"]["ref"]
            place = self.geo.register_place(
                namespace, name, place_type, names=[{"value": name, "language": "und", "kind": "canonical"}],
                source_ids={record["provider"]: record["native_id"] if place_type != "environment-grid-cell"
                            else record["location"]["ref"]},
                parent_ids=[], principal_id=principal_id, scopes=GEO_SCOPES, place_key=key, observed_at_ms=observed,
                producer=PRODUCER, provenance={"source_url": record["source_url"], "record_id": rid})
            place_id = place["place_id"]
            placed = 1
        else:
            place_id = row[0]
        stored = self.geo.store_geometry(
            namespace, geometry, place_id=place_id, crs="EPSG:4326", precision_m=precision, simplified_from=None,
            disputed=False, admin_hierarchy=[], source={"kind": "environment-record", "record_id": rid,
                                                        "provider": record["provider"]},
            evidence=[{"kind": "provider-record", "source_url": record["source_url"]}], principal_id=principal_id,
            scopes=GEO_SCOPES, observed_at_ms=observed, producer=PRODUCER,
            policy={"crs": "explicit-v1", "precision": "declared" if precision else "provider coordinates"})
        feature_id = None
        if record["record_type"] in {"station", "facility"}:
            from src.ingestion.geojson_features import feature_key

            collection = STATION_COLLECTION if record["record_type"] == "station" else FACILITY_COLLECTION
            feature_id = feature_key(record["provider"], collection, record["native_id"])
        return place_id, stored["geometry_id"], feature_id, placed

    def _project_features(self, namespace, features, *, run_id, principal_id):
        projected = 0
        for (collection, provider), records in sorted(features.items()):
            feature_run = f"{run_id}:{collection}:{provider}"
            source_id = f"environment:{provider}"
            start = self.conn.execute("SELECT count(*) FROM geospatial_feature_pages WHERE run_id=? AND source_id=?",
                                      [feature_run, source_id]).fetchone()[0]
            scope = {"environment_selection": provider, "collection": collection}
            page = {"start_index": int(start), "number_matched": None, "number_returned": len(records),
                    "provider_timestamp": None, "response_sha256": None, "scope": scope, "scope_hash": digest(scope),
                    "complete_scope": False, "final_page": True}
            items = [{"id": r["native_id"], "title": r["title"], "feature_page": page,
                      "feature": {"provider": provider, "collection": collection, "native_id": r["native_id"],
                                  "properties": {"record_type": r["record_type"], "title": r["title"],
                                                 "identifiers": r.get("identifiers") or {}},
                                  "geometry": r["geometry"], "source_geometry": r["geometry"],
                                  "source_crs": "EPSG:4326", "axis_order": "east_north",
                                  "source_sha256": digest(r)}} for r in records]
            policy = {"precision": {"status": "unknown", "basis": "provider coordinates without declared accuracy"}}
            states = self.features.observe_page(feature_run, source_id, namespace, items, policy=policy, documents={},
                                                provenance={"kind": "environment-pack", "provider": provider,
                                                            "attribution": provider},
                                                principal_id=principal_id)
            self.features.finish_snapshot(feature_run, source_id, namespace, provider=provider, collection=collection,
                                          status="complete", principal_id=principal_id)
            projected += sum(states.values())
        return projected

    def _vintage(self, namespace, rid, record, values, *, run_id, evidence, observed):
        unit = record["unit"]
        values_hash = digest({"values": values, "unit": unit, "kind": record["kind"], "model": record.get("model")})
        latest = self.conn.execute(
            "SELECT vintage_id, sequence, values_hash FROM environment_vintages WHERE record_id=? ORDER BY sequence DESC LIMIT 1",
            [rid]).fetchone()
        if latest and latest[2] == values_hash:
            return 0
        release = dict(record.get("release") or {})
        document = dict(record.get("document") or {})
        released_at = release.get("released_at") or document.get("created")
        if released_at:
            release_at_ms = ms(released_at)
            release_at_basis = "caller_supplied" if release.get("basis") == "caller_supplied" else "provider_reported"
            release_time_status = "release timestamp published by the provider" if release_at_basis == "provider_reported" \
                else "release timestamp supplied by the source selection"
        else:
            release_at_ms, release_at_basis = observed, "provider_vintage_fallback"
            release_time_status = "provider release timestamp unavailable; acquisition time used as fallback"
        if release_at_ms > observed:
            release_at_ms = observed
        statuses = sorted({v["status"] for v in values})
        status = statuses[0] if len(statuses) == 1 else "mixed"
        sequence = int(latest[1]) + 1 if latest else 1
        vintage_id = "env-vintage:" + digest([rid, sequence, values_hash])[:24]
        self.conn.execute(
            "INSERT INTO environment_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [vintage_id, namespace, rid, record["provider"], sequence, record["kind"], status, release_at_ms,
             release_at_basis, observed, "connector_acquisition", "acquisition" if not released_at else "provider_release",
             release_time_status, latest[0] if latest else None, values_hash, unit, run_id, canonical(evidence), observed])
        for item in values:
            normalized = None
            if item["value"] is not None:
                try:
                    normalized = er.normalise(item["value"], unit)
                except Exception:  # noqa: BLE001 - an unconvertible unit stays as published
                    normalized = None
            self.conn.execute(
                "INSERT INTO environment_values VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [vintage_id, ms(item["start"]), item["start"], item.get("end"), item["value"], unit,
                 None if normalized is None else normalized["value"], None if normalized is None else normalized["unit"],
                 item["status"], item.get("quality"), canonical(item.get("flags") or {})])
        return 1

    def _link(self, namespace, rid, record, *, observed):
        """Link overlapping stations (shared EEA code) and ETS installations to E-PRTR facilities."""

        created = 0
        if record["record_type"] == "station" and record["identifiers"].get("eea_station_code"):
            code = record["identifiers"]["eea_station_code"]
            for other_id, content in self.conn.execute(
                    "SELECT r.record_id, v.content_json FROM environment_records r JOIN environment_record_current c "
                    "USING(record_id) JOIN environment_record_revisions v ON v.revision_id=c.revision_id "
                    "WHERE r.namespace=? AND r.record_type='station' AND r.provider<>? ORDER BY r.record_id",
                    [namespace, record["provider"]]).fetchall():
                other = json.loads(content)
                if other["identifiers"].get("eea_station_code") != code:
                    continue
                from src.kb.geospatial import _distance_m

                distance = _distance_m(record["geometry"]["coordinates"], other["geometry"]["coordinates"])
                state = "linked" if distance <= LINK_DISTANCE_M else "candidate"
                created += self._link_row(namespace, "same-station", rid, other_id, state,
                                          "shared EEA station code" + ("" if state == "linked" else "; distance exceeds threshold, needs review"),
                                          {"eea_station_code": code, "distance_m": round(distance, 1),
                                           "threshold_m": LINK_DISTANCE_M}, observed)
        if record["record_type"] == "facility" and record["identifiers"].get("ets_identifier"):
            ets = record["identifiers"]["ets_identifier"]
            for other_id, provider, content in self.conn.execute(
                    "SELECT r.record_id, r.provider, v.content_json FROM environment_records r JOIN environment_record_current c "
                    "USING(record_id) JOIN environment_record_revisions v ON v.revision_id=c.revision_id "
                    "WHERE r.namespace=? AND r.record_type='facility' AND r.provider<>? ORDER BY r.record_id",
                    [namespace, record["provider"]]).fetchall():
                other = json.loads(content)
                if other["identifiers"].get("ets_identifier") == ets:
                    created += self._link_row(namespace, "same-installation", rid, other_id, "linked",
                                              "ETS identifier published in the EEA installation record",
                                              {"ets_identifier": ets}, observed)
        return created

    def _link_row(self, namespace, relation, left, right, state, basis, evidence, observed):
        a, b = sorted([left, right])
        link_id = "env-link:" + digest([namespace, relation, a, b])[:24]
        inserted = self.conn.execute(
            "INSERT INTO environment_record_links VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING link_id",
            [link_id, namespace, relation, a, b, state, basis, canonical(evidence), observed]).fetchall()
        return len(inserted)

    def _provider_state(self, namespace, providers, *, success=None, failure=None, code=None, execution=None, run_id=None):
        for provider in providers:
            self.conn.execute(
                "INSERT INTO environment_provider_state VALUES (?,?,?,?,?,?,?) ON CONFLICT (namespace, provider) DO UPDATE SET "
                "last_success_ms=coalesce(excluded.last_success_ms, environment_provider_state.last_success_ms), "
                "last_failure_ms=coalesce(excluded.last_failure_ms, environment_provider_state.last_failure_ms), "
                "last_failure_code=CASE WHEN excluded.last_failure_ms IS NULL THEN environment_provider_state.last_failure_code ELSE excluded.last_failure_code END, "
                "last_execution=coalesce(excluded.last_execution, environment_provider_state.last_execution), "
                "last_run_id=coalesce(excluded.last_run_id, environment_provider_state.last_run_id)",
                [namespace, provider, success, failure, code, execution, run_id])

    def record_failure(self, namespace, provider, *, code, run_id, scopes):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        now = self.now()
        self.conn.execute("CREATE TABLE IF NOT EXISTS environment_provider_state(namespace TEXT NOT NULL, provider TEXT NOT NULL, "
                          "last_success_ms BIGINT, last_failure_ms BIGINT, last_failure_code TEXT, last_execution TEXT, "
                          "last_run_id TEXT, PRIMARY KEY(namespace, provider))")
        self._provider_state(namespace, [provider], failure=now, code=code, run_id=run_id)
        return {"provider": provider, "failure_code": code, "recorded_at_ms": now,
                "effect": "stored records and vintages unchanged; dependent views read as stale"}

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace, provider):
        try:
            row = self.conn.execute("SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution, last_run_id "
                                    "FROM environment_provider_state WHERE namespace=? AND provider=?",
                                    [namespace, provider]).fetchone()
        except Exception:  # noqa: BLE001 - table absent before the first acquisition
            row = None
        if row is None:
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        stale = row[0] is None or (row[1] is not None and row[1] > row[0])
        return {"provider": provider, "last_success_ms": row[0], "last_failure_ms": row[1], "last_failure_code": row[2],
                "last_execution": row[3], "last_run_id": row[4], "stale": stale}

    def _revision_row(self, rid, *, revision=None, as_of_ms=None):
        return self.conn.execute(
            "SELECT revision_id, revision, content_json, place_id, geometry_id, feature_id, observed_at_ms, run_id, evidence_json "
            "FROM environment_record_revisions WHERE record_id=? AND (? IS NULL OR revision=?) AND (? IS NULL OR observed_at_ms<=?) "
            "ORDER BY revision DESC LIMIT 1", [rid, revision, revision, as_of_ms, as_of_ms]).fetchone()

    def record(self, namespace, rid, *, scopes, revision=None, as_of_ms=None):
        authorize(namespace, scopes, READ_SCOPE)
        head = self.conn.execute("SELECT record_type, provider, native_id, kind FROM environment_records "
                                 "WHERE record_id=? AND namespace=?", [rid, namespace]).fetchone()
        if head is None:
            raise EnvironmentStoreError("not_found", "record is not visible in this namespace")
        row = self._revision_row(rid, revision=revision, as_of_ms=as_of_ms)
        if row is None:
            raise EnvironmentStoreError("not_found", "no revision of this record existed at that time")
        latest = self.conn.execute("SELECT revision FROM environment_record_current WHERE record_id=?", [rid]).fetchone()[0]
        return {"record_id": rid, "record_type": head[0], "provider": head[1], "native_id": head[2], "kind": head[3],
                "revision_id": row[0], "revision": int(row[1]), "latest_revision": int(latest),
                "superseded": int(row[1]) < int(latest), "content": json.loads(row[2]), "place_id": row[3],
                "geometry_id": row[4], "feature_id": row[5], "observed_at_ms": int(row[6]), "run_id": row[7],
                "evidence": _load(row[8], {})}

    def find(self, namespace, record_type, provider, native_id):
        rid = record_id(namespace, record_type, provider, native_id)
        return rid if self.conn.execute("SELECT 1 FROM environment_records WHERE record_id=?", [rid]).fetchone() else None

    def records(self, namespace, *, scopes, record_type=None, provider=None, kind=None):
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT record_id FROM environment_records WHERE namespace=? AND (? IS NULL OR record_type=?) "
            "AND (? IS NULL OR provider=?) AND (? IS NULL OR kind=?) ORDER BY record_type, provider, native_id",
            [namespace, record_type, record_type, provider, provider, kind, kind]).fetchall()
        return [self.record(namespace, r[0], scopes=scopes) for r in rows]

    def vintages(self, namespace, rid, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT vintage_id, sequence, kind, status, release_at_ms, release_at_basis, retrieved_at_ms, retrieved_at_basis, "
            "vintage_basis, release_time_status, revision_of, values_hash, unit, run_id, evidence_json FROM environment_vintages "
            "WHERE namespace=? AND record_id=? ORDER BY sequence", [namespace, rid]).fetchall()
        keys = ("vintage_id", "sequence", "kind", "status", "release_at_ms", "release_at_basis", "retrieved_at_ms",
                "retrieved_at_basis", "vintage_basis", "release_time_status", "revision_of", "values_hash", "unit", "run_id")
        result = [{**dict(zip(keys, row[:14])), "evidence": _load(row[14], {})} for row in rows]
        for item in result:
            item["latest"] = item["sequence"] == len(result)
        return result

    def select_vintage(self, namespace, rid, *, scopes, as_of_ms=None, vintage_id=None):
        """The vintage a reader had at ``as_of_ms`` (released and retrieved by then), or an exact one."""

        vintages = self.vintages(namespace, rid, scopes=scopes)
        if vintage_id is not None:
            chosen = next((v for v in vintages if v["vintage_id"] == vintage_id), None)
            if chosen is None:
                raise EnvironmentStoreError("not_found", "vintage does not belong to this series")
            return chosen, vintages
        eligible = [v for v in vintages if as_of_ms is None
                    or (v["retrieved_at_ms"] <= as_of_ms and v["release_at_ms"] <= as_of_ms)]
        return (eligible[-1] if eligible else None), vintages

    def values(self, vintage_id, *, period_from_ms=None, period_to_ms=None):
        rows = self.conn.execute(
            "SELECT period_start, period_end, value, unit, normalized_value, normalized_unit, status, quality, flags_json "
            "FROM environment_values WHERE vintage_id=? AND (? IS NULL OR start_ms>=?) AND (? IS NULL OR start_ms<?) "
            "ORDER BY start_ms", [vintage_id, period_from_ms, period_from_ms, period_to_ms, period_to_ms]).fetchall()
        keys = ("start", "end", "value", "unit", "normalized_value", "normalized_unit", "status", "quality")
        return [{**dict(zip(keys, row[:8])), "flags": _load(row[8], {})} for row in rows]

    def series(self, namespace, rid, *, scopes, as_of_ms=None, vintage_id=None, unit=None, period_from=None,
               period_to=None):
        """Series metadata and values from the selected vintage, with kind and as-of explicit."""

        record = self.record(namespace, rid, scopes=scopes, as_of_ms=as_of_ms)
        vintage, vintages = self.select_vintage(namespace, rid, scopes=scopes, as_of_ms=as_of_ms, vintage_id=vintage_id)
        values = [] if vintage is None else self.values(vintage["vintage_id"], period_from_ms=ms(period_from),
                                                        period_to_ms=ms(period_to))
        if unit is not None:
            for item in values:
                converted = None if item["value"] is None else er.normalise(item["value"], item["unit"], target=unit)
                item["converted"] = converted
        content = record["content"]
        newer = [v["vintage_id"] for v in vintages if vintage and v["sequence"] > vintage["sequence"]]
        return {"record_id": rid, "kind": record["kind"], "provider": record["provider"], "title": content["title"],
                "source_url": content["source_url"], "indicator": content.get("indicator"),
                "location": content.get("location"), "unit": content.get("unit"), "interval": content.get("interval")
                or content.get("resolution"), "aggregation": content.get("aggregation"), "model": content.get("model"),
                "status_basis": content.get("status_basis"), "revision_id": record["revision_id"],
                "vintage": vintage, "newer_vintages": newer, "stale": bool(newer),
                "as_of_ms": as_of_ms, "values": values, "unknowns": content.get("unknowns", []),
                "kind_notice": None if record["kind"] == "observation" else
                f"{record['kind']} values; never an observation"}

    def links(self, namespace, *, scopes, record=None):
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT link_id, relation, left_record, right_record, state, basis, evidence_json FROM environment_record_links "
            "WHERE namespace=? AND (? IS NULL OR left_record=? OR right_record=?) ORDER BY link_id",
            [namespace, record, record, record]).fetchall()
        return [{"link_id": r[0], "relation": r[1], "records": [r[2], r[3]], "state": r[4], "basis": r[5],
                 "evidence": _load(r[6], {}), "merged": False} for r in rows]

    def located(self, namespace, location_ref):
        """Series/facility records attached to a location reference (``provider:native``)."""

        provider, native = location_ref.split(":", 1)
        for record_type in ("station", "facility"):
            rid = self.find(namespace, record_type, provider, native)
            if rid:
                return rid
        return None


class EnvironmentProjector:
    """Source-pack runtime projector for ``noesis-environment-record-v1`` pages."""

    SCOPES_FOR = staticmethod(lambda namespace: {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write",
                                                 f"namespace:{namespace}:read"})

    def __init__(self, conn):
        self.store = EnvironmentStore(conn)

    @staticmethod
    def _namespace(source):
        return str(dict(source.get("environment") or {}).get("namespace") or "environment")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        namespace = self._namespace(source)
        payload = [dict(item["environment_record"]) for item in records if item.get("environment_record")]
        evidence = {"run_id": run_id, "source_id": source["source_id"], "pack_id": manifest["pack_id"],
                    "pack_version": manifest["version"], "response_sha256": page_receipt.get("response_sha256"),
                    "documents": sorted(str(d["document_id"]) for d in documents)}
        # The runtime's clock (document ingestion time) is the observation time, so
        # as-of reads agree with the source-run receipts.
        observed = max((int(d["ingested_at"]) for d in documents if d.get("ingested_at") is not None), default=None)
        return self.store.apply(namespace, payload, run_id=run_id, principal_id=principal_id,
                                scopes=self.SCOPES_FOR(namespace), evidence=evidence, execution="source-pack",
                                observed_at_ms=observed)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("environment") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + status, run_id=run_id,
                                      scopes=self.SCOPES_FOR(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}


class EnvironmentEvidenceStore:
    """DurableHTTP captures persisted as documents, then applied to the store."""

    def __init__(self, conn, *, now=None):
        from src.ingestion.document_store import DocumentStore

        self.conn = conn
        self.documents = DocumentStore(conn)
        self.store = EnvironmentStore(conn, now=now)

    def ingest(self, provider, parsed, captured, *, namespace, scopes, reuse_notice, principal_id):
        from services.ingest.common.document_model import Document
        from src.ingestion.provider_execution import ProviderError

        if not reuse_notice:
            raise ValueError("explicit reuse notice required")
        captures = captured if isinstance(captured, list) else [captured]
        if not captures or any(hashlib.sha256(c.content).hexdigest() != c.receipt.get("digest") for c in captures):
            raise ProviderError("source_changed", "capture digest mismatch")
        digests = [c.receipt["digest"] for c in captures]
        observed = max(c.receipt["observed_at_ms"] for c in captures)
        run_id = "environment-observation:" + digest([namespace, provider, digests])[:32]
        documents = [Document(
            document_id="environment:" + digest([namespace, r["provider"], r["record_type"], r["native_id"]])[:32],
            source_type="web", source_id=f"{r['provider']}:{r['native_id']}", language="en", ingested_at=observed,
            url=r["source_url"], title=r["title"], content=canonical(r),
            metadata={"environment_contract": er.CONTRACT, "namespace": namespace, "record_type": r["record_type"],
                      "kind": r.get("kind"), "source_license": reuse_notice, "native_capture_sha256": ",".join(digests)})
            for r in parsed["records"]]
        outcome = self.documents.upsert(documents) if documents else None
        if outcome is not None and outcome.invalid:
            raise ProviderError("document_validation", "environment evidence failed document validation")
        executions = sorted({c.receipt.get("execution") or "unknown" for c in captures})
        evidence = {"capture_digests": digests, "execution": executions[0] if len(executions) == 1 else "mixed",
                    "snapshots": [(c.receipt.get("snapshot") or {}).get("digest") for c in captures],
                    "documents": [{"document_id": c["document_id"], "revision_id": c["revision_id"]}
                                  for c in (outcome.changes if outcome else [])]}
        applied = self.store.apply(namespace, parsed["records"], run_id=run_id, principal_id=principal_id, scopes=scopes,
                                   evidence=evidence, execution=evidence["execution"], observed_at_ms=observed)
        return {"observation_id": run_id, "applied": applied, "coverage": parsed.get("coverage"),
                "evidence": evidence, "execution": evidence["execution"], "records": len(parsed["records"])}

    def fail(self, provider, error, *, namespace, scopes, observation_id):
        code = getattr(error, "code", type(error).__name__)
        return self.store.record_failure(namespace, provider, code=code, run_id=observation_id, scopes=scopes)
