"""Acquired vector features projected into the existing geospatial store.

Source-pack documents keep native records as metadata; that alone is not a
spatial import.  This module owns the bridge:

* **Identity.** A feature is ``provider + collection + native ID``; content
  changes create immutable revisions with the source properties, source-CRS
  geometry, axis order, transform receipt, acquisition document and page
  evidence retained.  Features stay distinct from named places and are never
  merged across providers automatically.
* **Projection.** Each changed revision stores one WGS84 geometry through
  :class:`~src.kb.geospatial.GeospatialStore`.  Projection is keyed by the
  source revision, so replaying a page after a crash is idempotent.  A feature
  that cannot be transformed is recorded as ``failed`` with its native record
  and can be retried without re-acquisition.
* **Refresh.** Only a *complete* snapshot of an unbounded collection scope, or
  an explicit provider tombstone, removes a feature.  Bounded (bbox), partial,
  interrupted, failed or out-of-order snapshots never imply deletion, and no
  valid-time end is inferred from observation times.
* **Queries.** Points inside a boundary are evaluated with exact ring parity
  over the stored WGS84 geometries and recorded as a replayable receipt whose
  replay recomputes membership from the pinned geometry revisions.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from src.kb.geospatial import (
    CALCULATE_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    GeospatialError,
    GeospatialStore,
    _point_in_ring,
    _require,
    _validate_geometry,
)

FEATURE_CONTRACT = "noesis-geospatial-feature-v1"
QUERY_CONTRACT = "noesis-geospatial-feature-query-v1"
SNAPSHOT_CONTRACT = "noesis-geospatial-feature-snapshot-v1"
READINESS_CONTRACT = "noesis-geospatial-pack-readiness-v1"
PRODUCER = {"name": "noesis-geospatial-pack", "version": "1.0.0"}
WITHIN_ALGORITHM = "wgs84-ring-parity-multipolygon-v1"
DEFAULT_NAMESPACE = "global"
MAX_QUERY_CANDIDATES = 20_000

_DDL = """
CREATE TABLE IF NOT EXISTS geospatial_features (
  feature_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL,
  collection TEXT NOT NULL, native_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,provider,collection,native_id)
);
CREATE TABLE IF NOT EXISTS geospatial_feature_revisions (
  revision_id TEXT PRIMARY KEY, feature_id TEXT NOT NULL, namespace TEXT NOT NULL,
  revision BIGINT NOT NULL, lifecycle TEXT NOT NULL, content_hash TEXT NOT NULL,
  title TEXT, properties_json TEXT NOT NULL, source_geometry_json TEXT,
  source_crs TEXT, axis_order TEXT, geometry_id TEXT, geometry_type TEXT,
  transform_json TEXT NOT NULL, provenance_json TEXT NOT NULL,
  temporal_json TEXT NOT NULL, precision_json TEXT NOT NULL, run_id TEXT NOT NULL,
  document_id TEXT, observed_at_ms BIGINT NOT NULL, principal_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(feature_id,revision)
);
CREATE TABLE IF NOT EXISTS geospatial_feature_current (
  feature_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, revision BIGINT NOT NULL,
  lifecycle TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geospatial_feature_observations (
  run_id TEXT NOT NULL, source_id TEXT NOT NULL, feature_id TEXT NOT NULL,
  namespace TEXT NOT NULL, content_hash TEXT, state TEXT NOT NULL,
  revision_id TEXT, error_json TEXT, record_json TEXT, start_index BIGINT,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(run_id,source_id,feature_id)
);
CREATE TABLE IF NOT EXISTS geospatial_feature_pages (
  run_id TEXT NOT NULL, source_id TEXT NOT NULL, start_index BIGINT NOT NULL,
  scope_hash TEXT NOT NULL, scope_json TEXT NOT NULL, number_matched BIGINT,
  number_returned BIGINT NOT NULL, provider_timestamp TEXT, response_sha256 TEXT,
  complete_scope BOOLEAN NOT NULL, final_page BOOLEAN NOT NULL,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(run_id,source_id,start_index)
);
CREATE TABLE IF NOT EXISTS geospatial_feature_snapshots (
  snapshot_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL,
  collection TEXT NOT NULL, scope_hash TEXT, run_id TEXT NOT NULL,
  source_id TEXT NOT NULL, completeness TEXT NOT NULL, reasons_json TEXT NOT NULL,
  summary_json TEXT NOT NULL, provider_timestamp TEXT, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS geospatial_feature_snapshot_current (
  namespace TEXT NOT NULL, provider TEXT NOT NULL, collection TEXT NOT NULL,
  snapshot_id TEXT NOT NULL, provider_timestamp TEXT, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace,provider,collection)
);
CREATE INDEX IF NOT EXISTS idx_geospatial_feature_collection
  ON geospatial_features(namespace,provider,collection);
"""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def ensure_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _feature_key(provider: str, collection: str, native_id: str) -> str:
    from src.ingestion.geojson_features import feature_key

    return feature_key(provider, collection, native_id)


def _precision(policy: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(policy.get("precision") or {})
    if declared.get("status") == "declared":
        value = float(declared["value_m"])
        if value < 0:
            raise GeospatialError("invalid_precision", "declared precision must be >= 0")
        return {"status": "declared", "value_m": value,
                "basis": str(declared.get("basis") or "source metadata")}
    return {"status": "unknown", "value_m": None,
            "basis": str(declared.get("basis") or "not declared by the source")}


class GeospatialFeatureStore:
    """Feature revisions, snapshots and source-aware spatial queries."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.geo = GeospatialStore(conn, initialize=initialize, now=self.now)
        if initialize:
            ensure_schema(conn)

    # ------------------------------------------------------------------ writes
    def _project(
        self,
        namespace: str,
        feature: Mapping[str, Any],
        *,
        policy: Mapping[str, Any],
        run_id: str,
        source_id: str,
        document_id: str | None,
        page: Mapping[str, Any],
        provenance: Mapping[str, Any],
        tombstone: bool,
        principal_id: str,
    ) -> dict[str, Any]:
        """Project one decoded feature inside the caller's transaction."""

        from src.integrations.spatial import transform_geometry

        provider, collection = str(feature["provider"]), str(feature["collection"])
        native_id = str(feature["native_id"])
        feature_id = _feature_key(provider, collection, native_id)
        observed_at_ms = self.now()
        existing = self.conn.execute(
            "SELECT namespace FROM geospatial_features WHERE feature_id=?", [feature_id]
        ).fetchone()
        if existing and existing[0] != namespace:
            raise GeospatialError(
                "namespace_conflict", "feature identity already belongs to another namespace"
            )
        precision = _precision(policy)
        temporal = {
            "valid_from_ms": policy.get("valid_from_ms"),
            "valid_to_ms": None,
            "valid_time": "declared" if policy.get("valid_from_ms") is not None else "unknown",
            "observed_at_ms": observed_at_ms,
            "provider_timestamp": page.get("provider_timestamp"),
        }
        content = {
            "properties": feature.get("properties") or {},
            "source_geometry": feature.get("source_geometry"),
            "source_crs": feature.get("source_crs"),
            "axis_order": feature.get("axis_order"),
            "precision": precision,
            "valid_from_ms": temporal["valid_from_ms"],
            "lifecycle": "removed" if tombstone else "active",
        }
        content_hash = _digest(content)
        current = self.conn.execute(
            "SELECT r.revision,r.content_hash,r.revision_id FROM geospatial_feature_current c JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE c.feature_id=?",
            [feature_id],
        ).fetchone()
        if current and current[1] == content_hash:
            return {"feature_id": feature_id, "state": "unchanged",
                    "revision_id": current[2], "content_hash": content_hash}
        # Everything that can reject a feature happens before the first write,
        # so a failed feature leaves no partial rows in the page transaction.
        geometry_id = None
        transform: dict[str, Any] = {}
        transformed: dict[str, Any] = {}
        if not tombstone:
            transformed = transform_geometry(
                dict(feature["geometry"]), str(feature["source_crs"])
            )
            _validate_geometry(transformed["result"]["geometry"])
            transform = {
                "producer": transformed["producer"],
                "sha256": transformed["sha256"],
                **{
                    key: value
                    for key, value in transformed["result"].items()
                    if key != "geometry"
                },
            }
        if not existing:
            self.conn.execute(
                "INSERT INTO geospatial_features VALUES (?,?,?,?,?,?)",
                [feature_id, namespace, provider, collection, native_id, observed_at_ms],
            )
        if not tombstone:
            stored = self.geo._store_geometry(
                namespace,
                transformed["result"]["geometry"],
                place_id=None,
                crs="EPSG:4326",
                precision_m=precision["value_m"] or 0.0,
                simplified_from=None,
                disputed=False,
                admin_hierarchy=[],
                source={
                    "kind": "acquired-feature",
                    "feature_id": feature_id,
                    "provider": provider,
                    "collection": collection,
                    "native_id": native_id,
                    "coordinate_transform": transform,
                },
                evidence=[
                    {
                        "kind": "source-feature-revision",
                        "content_hash": content_hash,
                        "document_id": document_id,
                        "response_sha256": page.get("response_sha256"),
                    }
                ],
                principal_id=principal_id,
                context={
                    "generation": 0,
                    "valid_from_ms": temporal["valid_from_ms"],
                    "valid_to_ms": None,
                    "observed_at_ms": observed_at_ms,
                    "producer": dict(PRODUCER),
                    # precision_m=0 is not a claim when precision.status is unknown.
                    "policy": {"crs": "explicit-v1", "precision": precision},
                },
            )
            geometry_id = stored["geometry_id"]
        revision = int(current[0]) + 1 if current else 1
        revision_id = "geofeature-rev:" + _digest([feature_id, revision, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO geospatial_feature_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                feature_id,
                namespace,
                revision,
                content["lifecycle"],
                content_hash,
                feature.get("title"),
                _canonical(content["properties"]),
                None if tombstone else _canonical(feature.get("source_geometry")),
                feature.get("source_crs"),
                feature.get("axis_order"),
                geometry_id,
                None if tombstone else str(dict(feature["geometry"]).get("type")),
                _canonical(transform),
                _canonical(
                    {
                        **dict(provenance),
                        "run_id": run_id,
                        "source_id": source_id,
                        "document_id": document_id,
                        "page": {k: page.get(k) for k in (
                            "start_index", "number_matched", "number_returned",
                            "provider_timestamp", "response_sha256", "scope_hash")},
                        "source_sha256": feature.get("source_sha256"),
                        "basis": "provider_tombstone" if tombstone else "acquired",
                    }
                ),
                _canonical(temporal),
                _canonical(precision),
                run_id,
                document_id,
                observed_at_ms,
                principal_id,
                observed_at_ms,
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO geospatial_feature_current VALUES (?,?,?,?)",
            [feature_id, revision_id, revision, content["lifecycle"]],
        )
        return {
            "feature_id": feature_id,
            "state": "tombstoned" if tombstone else "projected",
            "revision_id": revision_id,
            "revision": revision,
            "geometry_id": geometry_id,
            "content_hash": content_hash,
        }

    def _observe(
        self,
        run_id: str,
        source_id: str,
        namespace: str,
        record: Mapping[str, Any],
        *,
        policy: Mapping[str, Any],
        document_id: str | None,
        provenance: Mapping[str, Any],
        principal_id: str,
    ) -> str:
        page = dict(record.get("feature_page") or {})
        rejection = record.get("rejection")
        if rejection:
            rejection = dict(rejection)
            if rejection.get("native_id") is None:
                return "rejected"
            feature_id = _feature_key(
                str(rejection["provider"]), str(rejection["collection"]),
                str(rejection["native_id"]),
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO geospatial_feature_observations VALUES (?,?,?,?,NULL,'rejected',NULL,?,?,?,?)",
                [run_id, source_id, feature_id, namespace,
                 _canonical({"code": rejection.get("code")}), None,
                 page.get("start_index"), self.now()],
            )
            return "rejected"
        feature = dict(record["feature"])
        feature["title"] = record.get("title")
        feature_id = _feature_key(
            str(feature["provider"]), str(feature["collection"]), str(feature["native_id"])
        )
        prior = self.conn.execute(
            "SELECT state FROM geospatial_feature_observations WHERE run_id=? AND source_id=? AND feature_id=?",
            [run_id, source_id, feature_id],
        ).fetchone()
        if prior and prior[0] in {"projected", "unchanged", "tombstoned"}:
            return "replayed"
        tombstone = bool(
            record.get("deleted") is True
            or str(record.get("status") or "").casefold() in {"deleted", "removed", "tombstone"}
        )
        try:
            outcome = self._project(
                namespace,
                feature,
                policy=policy,
                run_id=run_id,
                source_id=source_id,
                document_id=document_id,
                page=page,
                provenance=provenance,
                tombstone=tombstone,
                principal_id=principal_id,
            )
        except (GeospatialError, ValueError) as exc:
            # Raised before any write (see _project); retried later.
            code = str(getattr(exc, "code", "") or "projection_failed")
            self.conn.execute(
                "INSERT OR REPLACE INTO geospatial_feature_observations VALUES (?,?,?,?,NULL,'failed',NULL,?,?,?,?)",
                [run_id, source_id, feature_id, namespace,
                 _canonical({"code": code, "message": str(exc)}),
                 _canonical({**dict(record), "document_id": document_id}),
                 page.get("start_index"), self.now()],
            )
            return "failed"
        self.conn.execute(
            "INSERT OR REPLACE INTO geospatial_feature_observations VALUES (?,?,?,?,?,?,?,NULL,NULL,?,?)",
            [run_id, source_id, feature_id, namespace, outcome["content_hash"],
             outcome["state"], outcome["revision_id"], page.get("start_index"),
             self.now()],
        )
        return outcome["state"]

    def _record_page(self, run_id: str, source_id: str, page: Mapping[str, Any]) -> None:
        if not page or page.get("start_index") is None:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO geospatial_feature_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                run_id,
                source_id,
                int(page["start_index"]),
                str(page.get("scope_hash") or ""),
                _canonical(page.get("scope") or {}),
                page.get("number_matched"),
                int(page.get("number_returned") or 0),
                page.get("provider_timestamp"),
                page.get("response_sha256"),
                bool(page.get("complete_scope")),
                bool(page.get("final_page")),
                self.now(),
            ],
        )

    def observe_page(
        self,
        run_id: str,
        source_id: str,
        namespace: str,
        records: Sequence[Mapping[str, Any]],
        *,
        policy: Mapping[str, Any],
        documents: Mapping[str, str],
        provenance: Mapping[str, Any],
        principal_id: str,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        pages = {
            int(dict(record.get("feature_page") or {}).get("start_index", -1)):
            dict(record.get("feature_page") or {})
            for record in records
        }
        self.conn.execute("BEGIN")
        try:
            for page in pages.values():
                self._record_page(run_id, source_id, page)
            for record in records:
                state = self._observe(
                    run_id,
                    source_id,
                    namespace,
                    record,
                    policy=policy,
                    document_id=documents.get(str(record.get("id"))),
                    provenance=provenance,
                    principal_id=principal_id,
                )
                counts[state] = counts.get(state, 0) + 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def finish_snapshot(
        self,
        run_id: str,
        source_id: str,
        namespace: str,
        *,
        provider: str,
        collection: str,
        status: str,
        principal_id: str,
    ) -> dict[str, Any]:
        """Classify snapshot completeness and apply removals only when complete."""

        pages = self.conn.execute(
            "SELECT start_index,scope_hash,scope_json,number_matched,number_returned,provider_timestamp,complete_scope,final_page FROM geospatial_feature_pages WHERE run_id=? AND source_id=? ORDER BY start_index",
            [run_id, source_id],
        ).fetchall()
        observations = dict(
            self.conn.execute(
                "SELECT state,count(*) FROM geospatial_feature_observations WHERE run_id=? AND source_id=? GROUP BY state",
                [run_id, source_id],
            ).fetchall()
        )
        reasons: list[str] = []
        if status != "complete":
            reasons.append("source_run_not_complete")
        if not pages:
            reasons.append("no_pages_recorded")
        expected = 0
        matched_values = {row[3] for row in pages}
        for row in pages:
            if int(row[0]) != expected:
                reasons.append("pages_not_contiguous")
                break
            expected += int(row[4])
        if pages and not all(bool(row[6]) for row in pages):
            reasons.append("bounded_scope")
        if pages and not bool(pages[-1][7]):
            reasons.append("final_page_missing")
        if len(matched_values) > 1:
            reasons.append("number_matched_changed")
        matched = next(iter(matched_values)) if len(matched_values) == 1 else None
        if pages and (matched is None or int(matched) != expected):
            reasons.append("returned_count_differs_from_matched")
        if observations.get("failed"):
            reasons.append("projection_failed")
        provider_timestamp = pages[0][5] if pages else None
        marker = self.conn.execute(
            "SELECT snapshot_id,provider_timestamp FROM geospatial_feature_snapshot_current WHERE namespace=? AND provider=? AND collection=?",
            [namespace, provider, collection],
        ).fetchone()
        if (
            not reasons
            and marker
            and marker[1]
            and provider_timestamp
            and str(provider_timestamp) < str(marker[1])
        ):
            reasons.append("older_than_current_snapshot")
        completeness = "complete" if not reasons else "partial"
        # A resumed run re-evaluates its snapshot; the id pins the evaluated
        # inputs so a failed attempt never masks a later complete one.
        snapshot_id = "geofeature-snapshot:" + _digest(
            [run_id, source_id, status, [list(row) for row in pages],
             sorted(observations.items())]
        )[:24]
        existing = self.conn.execute(
            "SELECT completeness,summary_json,reasons_json FROM geospatial_feature_snapshots WHERE snapshot_id=?",
            [snapshot_id],
        ).fetchone()
        if existing:
            return {
                "contract": SNAPSHOT_CONTRACT,
                "snapshot_id": snapshot_id,
                "completeness": existing[0],
                "reasons": _load(existing[2], []),
                **_load(existing[1], {}),
                "idempotent": True,
            }
        removed: list[str] = []
        self.conn.execute("BEGIN")
        try:
            if completeness == "complete":
                rows = self.conn.execute(
                    "SELECT f.feature_id FROM geospatial_features f JOIN geospatial_feature_current c ON c.feature_id=f.feature_id WHERE f.namespace=? AND f.provider=? AND f.collection=? AND c.lifecycle='active' AND f.feature_id NOT IN (SELECT feature_id FROM geospatial_feature_observations WHERE run_id=? AND source_id=?) ORDER BY f.feature_id",
                    [namespace, provider, collection, run_id, source_id],
                ).fetchall()
                for (feature_id,) in rows:
                    self._remove(
                        feature_id, namespace, run_id=run_id, snapshot_id=snapshot_id,
                        principal_id=principal_id,
                    )
                    removed.append(feature_id)
                self.conn.execute(
                    "INSERT OR REPLACE INTO geospatial_feature_snapshot_current VALUES (?,?,?,?,?,?)",
                    [namespace, provider, collection, snapshot_id, provider_timestamp,
                     self.now()],
                )
            summary = {
                "namespace": namespace,
                "provider": provider,
                "collection": collection,
                "run_id": run_id,
                "source_id": source_id,
                "number_matched": matched,
                "pages": len(pages),
                "observed": sum(int(value) for value in observations.values()),
                "states": {str(key): int(value) for key, value in sorted(observations.items())},
                "removed": removed,
                "provider_timestamp": provider_timestamp,
                "scope": _load(pages[0][2], {}) if pages else None,
            }
            self.conn.execute(
                "INSERT INTO geospatial_feature_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [snapshot_id, namespace, provider, collection,
                 pages[0][1] if pages else None, run_id, source_id, completeness,
                 _canonical(sorted(set(reasons))), _canonical(summary),
                 provider_timestamp, self.now()],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "contract": SNAPSHOT_CONTRACT,
            "snapshot_id": snapshot_id,
            "completeness": completeness,
            "reasons": sorted(set(reasons)),
            **summary,
            "idempotent": False,
        }

    def _remove(
        self, feature_id: str, namespace: str, *, run_id: str, snapshot_id: str,
        principal_id: str,
    ) -> None:
        row = self.conn.execute(
            "SELECT r.revision,r.properties_json,r.title,r.precision_json FROM geospatial_feature_current c JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE c.feature_id=?",
            [feature_id],
        ).fetchone()
        revision = int(row[0]) + 1
        now = self.now()
        content_hash = _digest({"removed_after_revision": int(row[0]), "snapshot": snapshot_id})
        revision_id = "geofeature-rev:" + _digest([feature_id, revision, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO geospatial_feature_revisions VALUES (?,?,?,?,'removed',?,?,?,NULL,NULL,NULL,NULL,NULL,'{}',?,?,?,?,NULL,?,?,?)",
            [
                revision_id, feature_id, namespace, revision, content_hash, row[2],
                row[1],
                _canonical({"basis": "absent_from_complete_snapshot",
                            "snapshot_id": snapshot_id, "run_id": run_id}),
                # Absence is observed; the real-world end of validity is unknown.
                _canonical({"valid_to_ms": None, "valid_time": "unknown",
                            "removal_observed_at_ms": now}),
                row[3], run_id, now, principal_id, now,
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO geospatial_feature_current VALUES (?,?,?,'removed')",
            [feature_id, revision_id, revision],
        )

    def retry_failed(self, run_id: str, *, principal_id: str, scopes: set[str],
                     provenance: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Re-project failed observations from their retained native records."""

        _require(scopes, WRITE_SCOPE)
        rows = self.conn.execute(
            "SELECT source_id,namespace,record_json FROM geospatial_feature_observations WHERE run_id=? AND state='failed' ORDER BY feature_id",
            [run_id],
        ).fetchall()
        counts: dict[str, int] = {}
        self.conn.execute("BEGIN")
        try:
            self._retry_rows(run_id, rows, counts, principal_id, provenance or {})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"run_id": run_id, "retried": len(rows), "states": counts}

    def _retry_rows(self, run_id, rows, counts, principal_id, provenance) -> None:
        for source_id, namespace, record_json in rows:
            record = _load(record_json, {})
            document_id = record.pop("document_id", None)
            self.conn.execute(
                "DELETE FROM geospatial_feature_observations WHERE run_id=? AND source_id=? AND feature_id=?",
                [run_id, source_id, _feature_key(
                    str(record["feature"]["provider"]), str(record["feature"]["collection"]),
                    str(record["feature"]["native_id"]))],
            )
            state = self._observe(
                run_id, source_id, namespace, record, policy=record.get("policy") or {},
                document_id=document_id, provenance=provenance,
                principal_id=principal_id,
            )
            counts[state] = counts.get(state, 0) + 1

    def import_feature_collection(
        self,
        namespace: str,
        payload: bytes | str | Mapping[str, Any],
        *,
        provider: str,
        collection: str,
        source_crs: str,
        axis_order: str = "east_north",
        id_property: str | None = None,
        title_property: str | None = None,
        snapshot: str = "partial",
        precision: Mapping[str, Any] | None = None,
        attribution: str | None = None,
        principal_id: str,
        scopes: set[str],
        max_features: int = 10_000,
    ) -> dict[str, Any]:
        """Import one bounded local FeatureCollection into the caller's namespace.

        The caller must hold the geospatial write scope for ``namespace``.
        Only ``snapshot="complete"`` lets absent features be removed.
        """

        from src.ingestion.geojson_features import FeatureBudget, decode_feature_collection

        _require(scopes, WRITE_SCOPE)
        if namespace == DEFAULT_NAMESPACE:
            raise GeospatialError(
                "namespace_forbidden", "local imports write to a caller namespace, not global"
            )
        if snapshot not in {"complete", "partial"}:
            raise GeospatialError("invalid_snapshot", "snapshot must be complete or partial")
        decoded = decode_feature_collection(
            payload,
            provider=provider,
            collection=collection,
            source_crs=source_crs,
            axis_order=axis_order,
            id_property=id_property,
            title_property=title_property,
            budget=FeatureBudget(max_features=max_features),
        )
        run_id = "geofeature-import:" + _digest(
            [namespace, provider, collection, decoded["source_sha256"], snapshot]
        )[:24]
        count = len(decoded["records"]) + len(decoded["rejections"])
        page = {
            "start_index": 0,
            "number_matched": count,
            "number_returned": count,
            "provider_timestamp": decoded["metadata"]["provider_timestamp"],
            "response_sha256": decoded["source_sha256"],
            "scope": {"local_import": True, "collection": collection},
            "scope_hash": _digest({"local_import": True, "collection": collection}),
            "complete_scope": snapshot == "complete",
            "final_page": True,
        }
        policy = {"precision": dict(precision or {})}
        records = [
            {**record, "feature_page": page, "policy": policy}
            for record in decoded["records"] + decoded["rejections"]
        ]
        states = self.observe_page(
            run_id,
            "local-import",
            namespace,
            records,
            policy=policy,
            documents={},
            provenance={"kind": "local-import", "attribution": attribution,
                        "provider": provider},
            principal_id=principal_id,
        )
        finished = self.finish_snapshot(
            run_id, "local-import", namespace, provider=provider, collection=collection,
            status="complete", principal_id=principal_id,
        )
        return {
            "contract": "noesis-geospatial-feature-import-v1",
            "run_id": run_id,
            "namespace": namespace,
            "source_sha256": decoded["source_sha256"],
            "states": states,
            "rejections": [item["rejection"] for item in decoded["rejections"]],
            "snapshot": finished,
        }

    # ------------------------------------------------------------------- reads
    def _visible(self, namespace: str) -> tuple[str, str]:
        return (namespace, DEFAULT_NAMESPACE)

    def _revision(self, row: Sequence[Any]) -> dict[str, Any]:
        return {
            "revision_id": row[0],
            "revision": int(row[1]),
            "lifecycle": row[2],
            "content_hash": row[3],
            "title": row[4],
            "properties": _load(row[5], {}),
            "source_geometry": _load(row[6], None),
            "source_crs": row[7],
            "axis_order": row[8],
            "geometry_id": row[9],
            "geometry_type": row[10],
            "coordinate_transform": _load(row[11], {}),
            "provenance": _load(row[12], {}),
            "temporal": _load(row[13], {}),
            "precision": _load(row[14], {}),
            "run_id": row[15],
            "document_id": row[16],
            "observed_at_ms": int(row[17]),
        }

    _REVISION_COLUMNS = (
        "revision_id,revision,lifecycle,content_hash,title,properties_json,"
        "source_geometry_json,source_crs,axis_order,geometry_id,geometry_type,"
        "transform_json,provenance_json,temporal_json,precision_json,run_id,"
        "document_id,observed_at_ms"
    )

    def feature(
        self, namespace: str, feature_id: str, *, scopes: set[str],
        include_history: bool = True,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT namespace,provider,collection,native_id,created_at_ms FROM geospatial_features WHERE feature_id=? AND namespace IN (?,?)",
            [feature_id, *self._visible(namespace)],
        ).fetchone()
        if not row:
            raise GeospatialError("not_found", "feature does not exist in namespace")
        revisions = [
            self._revision(item)
            for item in self.conn.execute(
                f"SELECT {self._REVISION_COLUMNS} FROM geospatial_feature_revisions WHERE feature_id=? ORDER BY revision",
                [feature_id],
            ).fetchall()
        ]
        snapshot = self.conn.execute(
            "SELECT s.snapshot_id,s.completeness,s.provider_timestamp,s.created_at_ms FROM geospatial_feature_snapshot_current c JOIN geospatial_feature_snapshots s ON s.snapshot_id=c.snapshot_id WHERE c.namespace=? AND c.provider=? AND c.collection=?",
            [row[0], row[1], row[2]],
        ).fetchone()
        return {
            "contract": FEATURE_CONTRACT,
            "feature_id": feature_id,
            "namespace": row[0],
            "provider": row[1],
            "collection": row[2],
            "native_id": row[3],
            "created_at_ms": int(row[4]),
            "current": revisions[-1],
            "history": revisions if include_history else None,
            "collection_snapshot": None
            if not snapshot
            else {"snapshot_id": snapshot[0], "completeness": snapshot[1],
                  "provider_timestamp": snapshot[2], "recorded_at_ms": int(snapshot[3])},
        }

    def resolve_boundary(
        self, namespace: str, name: str, *, collection: str | None = None,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Match active polygon features by title; more than one stays unresolved."""

        _require(scopes, READ_SCOPE)
        wanted = " ".join(str(name).split()).casefold()
        if not wanted:
            raise GeospatialError("invalid_name", "boundary name is required")
        rows = self.conn.execute(
            "SELECT f.feature_id,f.namespace,f.provider,f.collection,f.native_id,r.title,r.geometry_type FROM geospatial_features f JOIN geospatial_feature_current c ON c.feature_id=f.feature_id JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE f.namespace IN (?,?) AND c.lifecycle='active' AND r.geometry_type IN ('Polygon','MultiPolygon') AND (? IS NULL OR f.collection=?) ORDER BY f.collection,f.native_id",
            [*self._visible(namespace), collection, collection],
        ).fetchall()
        candidates = [
            {"feature_id": row[0], "namespace": row[1], "provider": row[2],
             "collection": row[3], "native_id": row[4], "title": row[5]}
            for row in rows
            if " ".join(str(row[5] or "").split()).casefold() == wanted
        ]
        status = (
            "resolved" if len(candidates) == 1
            else "not_found" if not candidates
            else "needs_review"
        )
        return {
            "name": name,
            "status": status,
            "selected_feature_id": candidates[0]["feature_id"] if status == "resolved" else None,
            "candidates": candidates,
            "review": None
            if status != "needs_review"
            else "Select one candidate by feature_id; names are not merged across collections.",
        }

    def _current_geometry(self, namespace: str, feature_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT r.geometry_id,r.revision_id,c.lifecycle FROM geospatial_features f JOIN geospatial_feature_current c ON c.feature_id=f.feature_id JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE f.feature_id=? AND f.namespace IN (?,?)",
            [feature_id, *self._visible(namespace)],
        ).fetchone()
        if not row:
            raise GeospatialError("not_found", "boundary feature does not exist")
        if row[2] != "active" or not row[0]:
            raise GeospatialError("feature_removed", "boundary feature is not active")
        geometry_row = self.conn.execute(
            "SELECT namespace FROM geospatial_geometries WHERE geometry_id=?", [row[0]]
        ).fetchone()
        geometry = self.geo.geometry(geometry_row[0], row[0], scopes={READ_SCOPE})
        return {"geometry": geometry, "revision_id": row[1]}

    def _geometries(self, geometry_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Load pinned geometry revisions in one query (ids come from receipts/revisions)."""

        rows = self.conn.execute(
            "SELECT geometry_id,geometry_type,coordinates_json FROM geospatial_geometries WHERE list_contains(?, geometry_id)",
            [geometry_ids],
        ).fetchall()
        loaded = {
            row[0]: {"geometry_id": row[0],
                     "geometry": {"type": row[1], "coordinates": _load(row[2], [])}}
            for row in rows
        }
        missing = set(geometry_ids) - set(loaded)
        if missing:
            raise GeospatialError("not_found", "pinned geometry revision is missing")
        return loaded

    @staticmethod
    def _membership(boundary: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]) -> list[str]:
        """Exact ring parity, identical to ``_contains`` at zero tolerance.

        The boundary is validated once and each part gets a bounding-box
        prefilter, instead of revalidating the full boundary per point.
        """

        kind, coordinates = _validate_geometry(boundary["geometry"])
        if kind not in {"Polygon", "MultiPolygon"}:
            raise GeospatialError("invalid_boundary", "boundary must be a Polygon or MultiPolygon")
        polygons = [coordinates] if kind == "Polygon" else coordinates
        parts = [
            (
                min(x for x, _ in polygon[0]), min(y for _, y in polygon[0]),
                max(x for x, _ in polygon[0]), max(y for _, y in polygon[0]),
                polygon,
            )
            for polygon in polygons
        ]
        members = []
        for item in candidates:
            x, y = item["geometry"]["coordinates"]
            for west, south, east, north, polygon in parts:
                if (west <= x <= east and south <= y <= north
                        and _point_in_ring([x, y], polygon[0])
                        and not any(_point_in_ring([x, y], ring) for ring in polygon[1:])):
                    members.append(item["geometry_id"])
                    break
        return sorted(members)

    def within(
        self,
        namespace: str,
        *,
        collection: str,
        boundary_feature_id: str | None = None,
        boundary_name: str | None = None,
        boundary_collection: str | None = None,
        principal_id: str,
        scopes: set[str],
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Points of ``collection`` inside one boundary feature, with evidence."""

        _require(scopes, CALCULATE_SCOPE)
        read = scopes | {READ_SCOPE}
        if not 1 <= int(limit) <= 5000:
            raise GeospatialError("invalid_limit", "limit must be between 1 and 5000")
        if bool(boundary_feature_id) == bool(boundary_name):
            raise GeospatialError(
                "invalid_boundary", "provide exactly one of boundary_feature_id or boundary_name"
            )
        resolution = None
        if boundary_name:
            resolution = self.resolve_boundary(
                namespace, boundary_name, collection=boundary_collection, scopes=read
            )
            if resolution["status"] != "resolved":
                return {"contract": QUERY_CONTRACT, "status": resolution["status"],
                        "resolution": resolution, "members": [], "receipt": None}
            boundary_feature_id = resolution["selected_feature_id"]
        boundary = self._current_geometry(namespace, str(boundary_feature_id))
        rows = self.conn.execute(
            f"SELECT f.feature_id,f.namespace,f.provider,f.native_id,{', '.join('r.' + c for c in self._REVISION_COLUMNS.split(','))} FROM geospatial_features f JOIN geospatial_feature_current c ON c.feature_id=f.feature_id JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE f.namespace IN (?,?) AND f.collection=? AND c.lifecycle='active' ORDER BY f.native_id LIMIT ?",
            [*self._visible(namespace), collection, MAX_QUERY_CANDIDATES + 1],
        ).fetchall()
        if len(rows) > MAX_QUERY_CANDIDATES:
            raise GeospatialError("input_limit", "candidate collection exceeds the query budget")
        excluded = 0
        by_geometry: dict[str, dict[str, Any]] = {}
        for row in rows:
            revision = self._revision(row[4:])
            if revision["geometry_type"] != "Point" or not revision["geometry_id"]:
                excluded += 1
                continue
            by_geometry[revision["geometry_id"]] = {
                "feature_id": row[0], "namespace": row[1], "provider": row[2],
                "native_id": row[3], "revision": revision,
            }
        loaded = self._geometries(list(by_geometry))
        candidates = list(loaded.values())
        member_ids = self._membership(boundary["geometry"], candidates)
        members = []
        for geometry_id in member_ids:
            item = by_geometry[geometry_id]
            revision = item["revision"]
            members.append(
                {
                    "feature_id": item["feature_id"],
                    "native_id": item["native_id"],
                    "title": revision["title"],
                    "geometry_id": geometry_id,
                    "coordinates": loaded[geometry_id]["geometry"]["coordinates"],
                    "revision": revision["revision"],
                    "revision_id": revision["revision_id"],
                    "document_id": revision["document_id"],
                    "provenance": revision["provenance"],
                    "observed_at_ms": revision["observed_at_ms"],
                    "precision": revision["precision"],
                }
            )
        coverage = self.collection_coverage(namespace, collection, scopes=read)
        request = {
            "operation": "points_within",
            "boundary_feature_id": boundary_feature_id,
            "boundary_geometry_id": boundary["geometry"]["geometry_id"],
            "boundary_revision_id": boundary["revision_id"],
            "collection": collection,
        }
        result = {"member_geometry_ids": member_ids, "candidates": len(candidates),
                  "excluded_non_point": excluded}
        receipt = self.geo._receipt(
            namespace,
            "points_within",
            request,
            result,
            [boundary["geometry"]["geometry_id"], *sorted(by_geometry)],
            principal_id,
            algorithm=WITHIN_ALGORITHM,
        )
        return {
            "contract": QUERY_CONTRACT,
            "status": "complete" if coverage["completeness"] == "complete" else "coverage_incomplete",
            "resolution": resolution,
            "boundary": {
                "feature_id": boundary_feature_id,
                "geometry_id": boundary["geometry"]["geometry_id"],
                "revision_id": boundary["revision_id"],
            },
            "collection": collection,
            "coverage": coverage,
            "semantics": (
                "exact ring parity over stored WGS84 points and boundary rings; "
                "boundary-edge points count as inside; not a distance or routing result"
            ),
            "total_members": len(members),
            "members": members[: int(limit)],
            "truncated": len(members) > int(limit),
            "receipt": receipt,
        }

    def replay_within(self, namespace: str, receipt_id: str, *, scopes: set[str]) -> dict[str, Any]:
        """Recompute a points-within receipt from its pinned geometry revisions."""

        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT operation,request_json,result_json,input_ids_json,algorithm,calculation_hash FROM spatial_receipts WHERE namespace=? AND receipt_id=?",
            [namespace, receipt_id],
        ).fetchone()
        if not row or row[0] != "points_within":
            raise GeospatialError("not_found", "points-within receipt does not exist")
        request, stored, input_ids = _load(row[1], {}), _load(row[2], {}), _load(row[3], [])

        loaded = self._geometries(list(input_ids))
        boundary = loaded[request["boundary_geometry_id"]]
        candidates = [
            loaded[item] for item in input_ids if item != request["boundary_geometry_id"]
        ]
        recomputed = self._membership(boundary, candidates)
        replay_result = {**stored, "member_geometry_ids": recomputed}
        replayed_hash = _digest(
            [namespace, row[0], request, replay_result, sorted(input_ids), row[4]]
        )
        return {
            "receipt_id": receipt_id,
            "algorithm": row[4],
            "stored_hash": row[5],
            "replayed_hash": replayed_hash,
            "deterministic": replayed_hash == row[5],
            "recomputed_members": len(recomputed),
        }

    def collection_coverage(self, namespace: str, collection: str, *, scopes: set[str]) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        latest = self.conn.execute(
            "SELECT snapshot_id,completeness,reasons_json,summary_json,provider_timestamp,created_at_ms FROM geospatial_feature_snapshots WHERE namespace IN (?,?) AND collection=? ORDER BY created_at_ms DESC,snapshot_id DESC LIMIT 1",
            [*self._visible(namespace), collection],
        ).fetchone()
        complete = self.conn.execute(
            "SELECT s.snapshot_id,s.provider_timestamp,s.created_at_ms FROM geospatial_feature_snapshot_current c JOIN geospatial_feature_snapshots s ON s.snapshot_id=c.snapshot_id WHERE c.namespace IN (?,?) AND c.collection=? ORDER BY s.created_at_ms DESC LIMIT 1",
            [*self._visible(namespace), collection],
        ).fetchone()
        counts = dict(
            self.conn.execute(
                "SELECT c.lifecycle,count(*) FROM geospatial_features f JOIN geospatial_feature_current c ON c.feature_id=f.feature_id WHERE f.namespace IN (?,?) AND f.collection=? GROUP BY c.lifecycle",
                [*self._visible(namespace), collection],
            ).fetchall()
        )
        summary = _load(latest[3], {}) if latest else {}
        return {
            "collection": collection,
            "active": int(counts.get("active", 0)),
            "removed": int(counts.get("removed", 0)),
            "completeness": "complete" if latest and latest[1] == "complete" else
            "partial" if latest else "unknown",
            "latest_snapshot": None if not latest else {
                "snapshot_id": latest[0], "completeness": latest[1],
                "reasons": _load(latest[2], []), "provider_timestamp": latest[4],
                "recorded_at_ms": int(latest[5]), "run_id": summary.get("run_id"),
            },
            "last_complete_snapshot": None if not complete else {
                "snapshot_id": complete[0], "provider_timestamp": complete[1],
                "recorded_at_ms": int(complete[2]),
            },
        }


class GeospatialFeatureProjector:
    """Source-pack runtime projector for ``noesis-geospatial-feature-v1``."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = GeospatialFeatureStore(conn)

    @staticmethod
    def _context(source: Mapping[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
        geospatial = dict(source.get("geospatial") or {})
        namespace = str(geospatial.get("namespace") or DEFAULT_NAMESPACE)
        policy = {"precision": dict(geospatial.get("precision") or {})}
        provenance = {
            "kind": "source-pack",
            "publisher": source.get("publisher"),
            "endpoint": source.get("endpoint"),
            "license": dict(source.get("license") or {}),
            "attribution": geospatial.get("attribution"),
            "metadata_url": geospatial.get("metadata_url"),
            "source_hash": source.get("source_hash"),
        }
        return namespace, policy, provenance

    def project_page(
        self,
        *,
        run_id: str,
        manifest: Mapping[str, Any],
        source: Mapping[str, Any],
        records: Sequence[Mapping[str, Any]],
        documents: Sequence[Mapping[str, Any]],
        page_receipt: Mapping[str, Any],
        principal_id: str,
    ) -> dict[str, int]:
        del manifest, page_receipt
        namespace, policy, provenance = self._context(source)
        document_ids = {
            str(dict(item.get("metadata") or {}).get("source_pack_record_id")): str(item["document_id"])
            for item in documents
        }
        return self.store.observe_page(
            run_id,
            str(source["source_id"]),
            namespace,
            [{**record, "policy": policy} for record in records],
            policy=policy,
            documents=document_ids,
            provenance=provenance,
            principal_id=principal_id,
        )

    def finish_source(
        self,
        *,
        run_id: str,
        manifest: Mapping[str, Any],
        source: Mapping[str, Any],
        status: str,
        principal_id: str,
    ) -> dict[str, Any]:
        del manifest
        namespace, _, _ = self._context(source)
        wfs = dict(source.get("wfs") or {})
        geospatial = dict(source.get("geospatial") or {})
        return self.store.finish_snapshot(
            run_id,
            str(source["source_id"]),
            namespace,
            provider=str(source["publisher"]),
            collection=str(wfs.get("type_names") or geospatial.get("collection")),
            status=status,
            principal_id=principal_id,
        )


def pack_readiness(conn: Any, pack_id: str = "geospatial-berlin") -> dict[str, Any]:
    """Actionable readiness: optional dependencies, pack state and accepted terms."""

    from src.integrations.spatial import transform_capability
    from src.ingestion.source_packs import _digest as source_digest

    transform = transform_capability()
    try:
        import shapely  # noqa: F401

        shapely_available = True
    except ImportError:
        shapely_available = False
    blockers: list[dict[str, str]] = []
    if not transform["pyproj_available"]:
        blockers.append({
            "code": "optional_dependency_missing",
            "dependency": "pyproj",
            "impact": "only EPSG:4326 and ETRS89/WGS 84 UTM sources can be projected",
            "action": "pip install 'noesis[spatial]' or pyproj==3.7.2",
            "severity": "degraded",
        })
    if not shapely_available:
        blockers.append({
            "code": "optional_dependency_missing",
            "dependency": "shapely",
            "impact": "exact multipart intersects/covers and simplification are unavailable",
            "action": "pip install shapely==2.1.2",
            "severity": "degraded",
        })
    sources: list[dict[str, Any]] = []
    try:
        row = conn.execute(
            "SELECT c.enabled,v.manifest_json FROM source_pack_current c JOIN source_pack_versions v ON v.pack_id=c.pack_id AND v.version=c.version WHERE c.pack_id=?",
            [pack_id],
        ).fetchone()
    except Exception:  # noqa: BLE001 - runtime tables absent until first install
        row = None
    enabled = bool(row and row[0])
    if not row:
        blockers.append({"code": "pack_not_installed", "action": "install config/source_packs/geospatial.json",
                         "severity": "blocking"})
    elif not enabled:
        blockers.append({"code": "pack_disabled", "action": "enable the geospatial source pack",
                         "severity": "blocking"})
    if row:
        manifest = _load(row[1], {})
        for source in manifest.get("sources", []):
            license_policy = source["license"]
            terms_hash = source_digest({"terms_url": license_policy["terms_url"],
                                        "redistribution": license_policy["redistribution"]})
            try:
                accepted = bool(conn.execute(
                    "SELECT 1 FROM source_pack_license_acceptance WHERE pack_id=? AND source_id=? AND license_id=? AND terms_hash=?",
                    [pack_id, source["source_id"], license_policy["id"], terms_hash],
                ).fetchone())
            except Exception:  # noqa: BLE001 - runtime not yet initialized
                accepted = False
            sources.append({"source_id": source["source_id"], "license": license_policy["id"],
                            "terms_accepted": accepted})
            if not accepted:
                blockers.append({"code": "license_not_accepted", "source_id": source["source_id"],
                                 "action": "record source terms acceptance before a live run",
                                 "severity": "blocking-live"})
    return {
        "contract": READINESS_CONTRACT,
        "pack_id": pack_id,
        "installed": bool(row),
        "enabled": enabled,
        "transform": transform,
        "shapely_available": shapely_available,
        "sources": sources,
        "modes": {
            "fixture": "ready" if row and enabled else "blocked",
            "live": "ready" if row and enabled and all(s["terms_accepted"] for s in sources)
            else "blocked",
        },
        "blockers": blockers,
    }
