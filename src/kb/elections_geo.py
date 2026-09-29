"""Constituency boundaries in the Geospatial store and place-based result queries as of a date (#1908, L07).

A constituency record carries the provider's geometry reference (layer,
boundary vintage, CRS). The boundary geometry itself is projected through the
Geospatial pack's own projector
(:class:`src.kb.geospatial_features.GeospatialFeatureProjector`) into
:class:`~src.kb.geospatial_features.GeospatialFeatureStore` as a collection per
unit scheme and boundary vintage, keeping provider, native id, vintage and
source CRS on every feature revision. A collection the Geospatial pack already
holds (for example Berlin district boundaries) is *registered* for a vintage
and queried in place, never copied.

A boundary vintage has a validity window. "Which constituency contained this
point on date D" is answered from the vintage valid at D with the feature
store's point-in-polygon query (a replayable receipt), and the answer returns
that constituency's result vintages in force at D. Boundary and result
evidence are cited separately; a boundary change never alters a stored result.
Constituency-to-place links go through the reviewable place-resolution flow
(``record_geospatial_resolution`` / ``review_geospatial_resolution``);
ambiguous or missing geometry stays unresolved.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.election_sources import normalize_unit_id
from src.kb.elections import (
    READ_SCOPE,
    WRITE_SCOPE,
    ElectionError,
    ElectionStore,
    _day,
    _load,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

GEO_READ = "knowledge:geospatial:read"
GEO_WRITE = "knowledge:geospatial:write"
GEO_CALCULATE = "knowledge:geospatial:calculate"
GEO_REVIEW = "knowledge:geospatial:review"
_DDL = """
CREATE TABLE IF NOT EXISTS election_boundary_vintages (
  namespace TEXT NOT NULL, scheme TEXT NOT NULL, boundary_vintage TEXT NOT NULL, collection TEXT NOT NULL,
  provider TEXT NOT NULL, feature_namespace TEXT NOT NULL, source_crs TEXT, valid_from TEXT NOT NULL, valid_to TEXT,
  origin TEXT NOT NULL, source_json TEXT NOT NULL, run_id TEXT, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, scheme, boundary_vintage)
);
CREATE TABLE IF NOT EXISTS election_place_links (
  namespace TEXT NOT NULL, constituency_id TEXT NOT NULL, geo_namespace TEXT NOT NULL, resolution_id TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, constituency_id, resolution_id)
);
"""


def collection_name(scheme: str, boundary_vintage: str) -> str:
    return f"elections:{scheme}:{boundary_vintage}"


class ElectionGeography:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ElectionStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ boundary vintages

    def _reference(
        self, namespace: str, scheme: str, boundary_vintage: str
    ) -> dict[str, Any]:
        refs = {
            canonical(
                {k: v for k, v in (c["geometry_ref"] or {}).items() if k != "native_id"}
            )
            for c in self.store.constituencies(namespace, scheme=scheme)
            if c["boundary_vintage"] == boundary_vintage and c["geometry_ref"]
        }
        if len(refs) != 1:
            raise ElectionError(
                "no_geometry_reference",
                "no constituency of this scheme and vintage carries a geometry reference",
            )
        return _load(refs.pop(), {})

    def _window(self, namespace, scheme, boundary_vintage, valid_from, valid_to):
        valid_from, valid_to = (
            _day(valid_from),
            None if valid_to is None else _day(valid_to),
        )
        if valid_to is not None and valid_to <= valid_from:
            raise ElectionError(
                "invalid_window",
                "a boundary vintage is valid from a date before its end",
            )
        overlaps = self.conn.execute(
            "SELECT boundary_vintage FROM election_boundary_vintages WHERE namespace=? AND scheme=? AND "
            "boundary_vintage<>? AND valid_from < coalesce(?, '9999-12-31') AND coalesce(valid_to, '9999-12-31') > ?",
            [namespace, scheme, boundary_vintage, valid_to, valid_from],
        ).fetchall()
        if overlaps:
            raise ElectionError(
                "overlapping_vintages", f"validity overlaps vintage {overlaps[0][0]!r}"
            )
        return valid_from, valid_to

    def project_boundaries(
        self,
        namespace: str,
        *,
        scheme: str,
        boundary_vintage: str,
        feature_collection: Mapping[str, Any],
        id_property: str,
        valid_from: str,
        principal_id: str,
        scopes: Iterable[str],
        valid_to: str | None = None,
        source_crs: str | None = None,
        title_property: str | None = None,
        feature_namespace: str = "global",
        snapshot: str = "complete",
    ) -> dict[str, Any]:
        """Project one boundary vintage through the Geospatial projector as its own collection.

        Feature ids are normalised like the constituency numbers (``1`` and ``001`` are one Wahlkreis); a feature
        whose id cannot be read is reported and left out, so its constituency stays unresolved.
        """
        from src.ingestion.geojson_features import decode_feature_collection
        from src.kb.geospatial_features import GeospatialFeatureProjector

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_WRITE)
        if snapshot not in {"complete", "partial"}:
            raise ElectionError("invalid_snapshot", "snapshot is complete or partial")
        ref = self._reference(namespace, scheme, boundary_vintage)
        crs = source_crs or ref["crs"]
        if crs != ref["crs"]:
            raise ElectionError(
                "crs_mismatch",
                f"the provider's reference states {ref['crs']}; reproject nothing on import",
            )
        valid_from, valid_to = self._window(
            namespace, scheme, boundary_vintage, valid_from, valid_to
        )
        features, unreadable = [], []
        for index, feature in enumerate(list(feature_collection.get("features") or [])):
            properties = dict(dict(feature).get("properties") or {})
            native = normalize_unit_id(scheme, properties.get(id_property))
            if native is None:
                unreadable.append(
                    {
                        "index": index,
                        "id_property": id_property,
                        "value": properties.get(id_property),
                    }
                )
                continue
            features.append(
                {
                    **dict(feature),
                    "properties": {**properties, "noesis_native_id": native},
                }
            )
        collection = collection_name(scheme, boundary_vintage)
        payload = {**dict(feature_collection), "features": features}
        decoded = decode_feature_collection(
            payload,
            provider=str(ref["provider"]),
            collection=collection,
            source_crs=crs,
            id_property="noesis_native_id",
            title_property=title_property,
        )
        run_id = (
            "election-boundaries:"
            + digest([namespace, scheme, boundary_vintage, decoded["source_sha256"]])[
                :24
            ]
        )
        count = len(decoded["records"]) + len(decoded["rejections"])
        page = {
            "start_index": 0,
            "number_matched": count,
            "number_returned": count,
            "provider_timestamp": decoded["metadata"]["provider_timestamp"],
            "response_sha256": decoded["source_sha256"],
            "scope": {"boundary_vintage": boundary_vintage, "collection": collection},
            "scope_hash": digest(
                {"boundary_vintage": boundary_vintage, "collection": collection}
            ),
            "complete_scope": snapshot == "complete",
            "final_page": True,
        }
        source = {
            "source_id": f"elections:{scheme}:{boundary_vintage}",
            "publisher": str(ref["provider"]),
            "endpoint": ref.get("url"),
            "license": {},
            "source_hash": decoded["source_sha256"],
            "geospatial": {
                "namespace": feature_namespace,
                "collection": collection,
                "attribution": f"{ref['provider']} {ref['layer']} ({boundary_vintage})",
                "metadata_url": ref.get("url"),
            },
        }
        projector = GeospatialFeatureProjector(self.conn)
        states = projector.project_page(
            run_id=run_id,
            manifest=None,
            source=source,
            records=[
                {**r, "feature_page": page}
                for r in decoded["records"] + decoded["rejections"]
            ],
            documents=[],
            page_receipt={},
            principal_id=principal_id,
        )
        finished = projector.finish_source(
            run_id=run_id,
            manifest=None,
            source=source,
            status="complete",
            principal_id=principal_id,
        )
        self._record_vintage(
            namespace,
            scheme,
            boundary_vintage,
            collection,
            str(ref["provider"]),
            feature_namespace,
            crs,
            valid_from,
            valid_to,
            "projected",
            ref,
            run_id,
        )
        return {
            "collection": collection,
            "boundary_vintage": boundary_vintage,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "run_id": run_id,
            "states": states,
            "snapshot": finished,
            "unreadable_ids": unreadable,
            "rejections": [r["rejection"] for r in decoded["rejections"]],
        }

    def register_collection(
        self,
        namespace: str,
        *,
        scheme: str,
        boundary_vintage: str,
        collection: str,
        provider: str,
        valid_from: str,
        principal_id: str,
        scopes: Iterable[str],
        valid_to: str | None = None,
        feature_namespace: str = "global",
    ) -> dict[str, Any]:
        """Use a collection the Geospatial store already holds for a boundary vintage (queried, never copied)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        if not self.conn.execute(
            "SELECT 1 FROM geospatial_features WHERE namespace IN (?, 'global') AND collection=? AND provider=? "
            "LIMIT 1",
            [feature_namespace, collection, provider],
        ).fetchone():
            raise ElectionError(
                "not_found", "the Geospatial store holds no such collection"
            )
        valid_from, valid_to = self._window(
            namespace, scheme, boundary_vintage, valid_from, valid_to
        )
        del principal_id
        self._record_vintage(
            namespace,
            scheme,
            boundary_vintage,
            collection,
            provider,
            feature_namespace,
            None,
            valid_from,
            valid_to,
            "registered",
            {"provider": provider, "collection": collection},
            None,
        )
        return self.vintages(namespace, scopes=scopes, scheme=scheme)[-1]

    def _record_vintage(
        self,
        namespace,
        scheme,
        vintage,
        collection,
        provider,
        feature_namespace,
        crs,
        valid_from,
        valid_to,
        origin,
        source,
        run_id,
    ):
        self.conn.execute(
            "INSERT OR REPLACE INTO election_boundary_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                scheme,
                vintage,
                collection,
                provider,
                feature_namespace,
                crs,
                valid_from,
                valid_to,
                origin,
                canonical(source),
                run_id,
                self.now(),
            ],
        )

    def vintages(
        self, namespace: str, *, scopes: Iterable[str], scheme: str | None = None
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "election_boundary_vintages"):
            return []
        rows = self.conn.execute(
            "SELECT scheme, boundary_vintage, collection, provider, feature_namespace, source_crs, valid_from, "
            "valid_to, origin, source_json, run_id FROM election_boundary_vintages WHERE namespace=? AND "
            "(? IS NULL OR scheme=?) ORDER BY scheme, valid_from",
            [namespace, scheme, scheme],
        ).fetchall()
        keys = (
            "scheme",
            "boundary_vintage",
            "collection",
            "provider",
            "feature_namespace",
            "source_crs",
            "valid_from",
            "valid_to",
            "origin",
            "source",
            "run_id",
        )
        return [{**dict(zip(keys, r)), "source": _load(r[9], {})} for r in rows]

    def vintage_at(
        self, namespace: str, scheme: str, as_of: str, *, scopes: Iterable[str]
    ) -> dict[str, Any] | None:
        day = _day(as_of)
        for item in self.vintages(namespace, scopes=scopes, scheme=scheme):
            if item["valid_from"] <= day and (
                item["valid_to"] is None or day < item["valid_to"]
            ):
                return item
        return None

    # ------------------------------------------------------------------ place-based answers

    def results_at_point(
        self,
        namespace: str,
        *,
        scheme: str,
        point: Sequence[float],
        as_of: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """The constituency containing a point on a date (from the boundary vintage valid then) and its results.

        Boundary evidence (collection, vintage, feature revision, receipt) and result evidence (vintages and their
        source revisions) are cited separately.
        """
        from src.kb.geospatial_features import GeospatialFeatureStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, GEO_CALCULATE)
        day = _day(as_of)
        vintage = self.vintage_at(namespace, scheme, day, scopes=scopes)
        base = {"scheme": scheme, "point": list(point), "as_of": day}
        if vintage is None:
            return {
                **base,
                "status": "no-boundary-vintage",
                "boundary": None,
                "constituencies": [],
            }
        found = GeospatialFeatureStore(self.conn, initialize=False).containing(
            vintage["feature_namespace"],
            collection=vintage["collection"],
            point=point,
            principal_id=principal_id,
            scopes=scopes | {GEO_READ},
        )
        boundary = {
            "collection": vintage["collection"],
            "boundary_vintage": vintage["boundary_vintage"],
            "valid_from": vintage["valid_from"],
            "valid_to": vintage["valid_to"],
            "provider": vintage["provider"],
            "origin": vintage["origin"],
            "reference": vintage["source"],
            "receipt_id": found["receipt"]["receipt_id"],
            "coverage": found["coverage"]["completeness"],
            "features": [
                {
                    k: m[k]
                    for k in (
                        "feature_id",
                        "native_id",
                        "revision_id",
                        "revision",
                        "source_crs",
                    )
                }
                for m in found["members"]
            ],
        }
        constituencies = []
        for member in found["members"]:
            native = (
                normalize_unit_id(scheme, member["native_id"]) or member["native_id"]
            )
            matches = self.store.find_constituency(
                namespace, scheme, native, vintage["boundary_vintage"]
            )
            for constituency in matches:
                contests = self.store.contests(
                    namespace, constituency_id=constituency["constituency_id"]
                )
                constituencies.append(
                    {
                        "constituency": constituency,
                        "boundary_feature": member["feature_id"],
                        "results": [
                            self.store.results(namespace, c["contest_id"], as_of=day)
                            for c in contests
                        ],
                    }
                )
            if not matches:
                constituencies.append(
                    {
                        "constituency": None,
                        "boundary_feature": member["feature_id"],
                        "results": [],
                        "status": "no-constituency-record-for-feature",
                    }
                )
        status = {
            "outside": "outside-all-boundaries",
            "ambiguous": "ambiguous",
            "resolved": "resolved",
        }[found["status"]]
        return {
            **base,
            "status": status,
            "boundary": boundary,
            "constituencies": constituencies,
            "note": "boundary vintages and result vintages are cited separately; a boundary change never alters a "
            "stored result",
        }

    def link_place(
        self,
        namespace: str,
        constituency_id: str,
        *,
        geo_namespace: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Record a reviewable place resolution for a constituency name (the Geospatial place-resolution flow)."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_WRITE)
        constituency = self.store.constituency(namespace, constituency_id)
        if not constituency["name"]:
            raise ElectionError(
                "unresolvable", "the constituency has no published name to resolve"
            )
        geo = GeospatialStore(self.conn, initialize=False)
        result = geo.resolve(
            geo_namespace,
            constituency["name"],
            context={
                "scheme": constituency["scheme"],
                "native_id": constituency["native_id"],
                "boundary_vintage": constituency["boundary_vintage"],
            },
            scopes={GEO_READ},
        )
        saved = geo.save_resolution(result, principal_id=principal_id, scopes=scopes)
        self.conn.execute(
            "INSERT OR IGNORE INTO election_place_links VALUES (?,?,?,?,?,?)",
            [
                namespace,
                constituency_id,
                geo_namespace,
                saved["resolution_id"],
                principal_id,
                self.now(),
            ],
        )
        return {
            **self.place(namespace, constituency_id, scopes=scopes),
            "resolution": saved,
        }

    def place(
        self, namespace: str, constituency_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """The constituency's place link: accepted by review, or unresolved (ambiguous, missing or rejected)."""
        authorize(namespace, set(scopes), READ_SCOPE)
        links = []
        if table_exists(self.conn, "election_place_links"):
            links = self.conn.execute(
                "SELECT l.resolution_id, l.geo_namespace, g.status, g.selected_place_id, g.candidates_json FROM "
                "election_place_links l JOIN geocode_resolutions g ON g.resolution_id=l.resolution_id WHERE "
                "l.namespace=? AND l.constituency_id=? ORDER BY l.created_at_ms",
                [namespace, constituency_id],
            ).fetchall()
        views = []
        for resolution_id, geo_namespace, status, selected, candidates in links:
            review = (
                self.conn.execute(
                    "SELECT decision, selected_place_id, reason, principal_id, revision FROM geocode_reviews WHERE "
                    "resolution_id=? ORDER BY revision DESC LIMIT 1",
                    [resolution_id],
                ).fetchone()
                if table_exists(self.conn, "geocode_reviews")
                else None
            )
            state = "unreviewed"
            place_id = None
            if review is not None:
                state = {
                    "accept": "accepted",
                    "reject": "rejected",
                    "defer": "deferred",
                }[review[0]]
                place_id = review[1] if review[0] == "accept" else None
            views.append(
                {
                    "resolution_id": resolution_id,
                    "geo_namespace": geo_namespace,
                    "resolution_status": status,
                    "candidates": [c["place_id"] for c in _load(candidates, [])],
                    "review_state": state,
                    "place_id": place_id,
                    "reviewer": None if review is None else review[3],
                }
            )
        accepted = [v for v in views if v["place_id"]]
        return {
            "constituency_id": constituency_id,
            "state": "linked" if accepted else "unresolved",
            "place_id": accepted[-1]["place_id"] if accepted else None,
            "links": views,
            "note": "a place link is a reviewed resolution; ambiguous or missing places stay unresolved",
        }
