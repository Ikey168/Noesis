"""Regional surveillance series on Geospatial boundaries, answered as of a reporting date (#1917, I07).

Geography codes (NUTS, AGS, ISO and EU country codes) are resolved to boundary
features the Geospatial feature store already holds
(:mod:`src.kb.geospatial_features`) - and, where one is registered, to the
:class:`src.kb.geospatial.GeospatialStore` place whose source identifiers
carry the code - by the published code only: never by name, geometry or
nearest match. A code matching no feature, or several, stays unresolved with
the reason; WHO regions and ECDC aggregates have no boundary and stay
unresolved. Every resolution records the code system, the code-list version and
the boundary revision it used (feature revision, observation time and the
collection's snapshot timestamp) and is reviewable; a rejected resolution is
not used. When a later evaluation resolves a code to another boundary revision
(a district merger, a NUTS revision) or to none, that is recorded as a
**geography break** on every series with the code; nothing is re-aggregated.

A boundary query ("series for condition X within boundary Y") takes a boundary
feature id or a boundary name (resolved through the existing
``resolve_boundary`` place-resolution path) and returns the series resolved to
that boundary plus the series whose published code lies within it by the code
list's own hierarchy (AGS and NUTS prefixes, the country of a NUTS or AGS code),
each with its own values - never aggregated. Values are selected by reporting
date: the vintage released by the requested day, and only values whose
reporting date is on or before it; values with an unknown reporting date are
listed apart. Values of different sources for the same period are shown side
by side, never merged. Each answer carries a receipt that records every
request parameter and is replayed with them. No spatial store, geometry table
or coordinate transform is added.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.surveillance_sources import number_key
from src.kb.surveillance import (
    ANSWER_CONTRACT,
    NEVER_SENTENCE,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    SurveillanceError,
    SurveillanceStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

GEO_READ = "knowledge:geospatial:read"
RECEIPT_CONTRACT = "noesis-surveillance-query-receipt-v1"
# Default boundary collections per code system (and AGS level by code length): (collection, code property).
BOUNDARY_COLLECTIONS: dict[str, tuple[str, str]] = {
    "ags:2": ("bkg:vg250:lan", "AGS"),
    "ags:3": ("bkg:vg250:rbz", "AGS"),
    "ags:5": ("bkg:vg250:krs", "AGS"),
    "ags:8": ("bkg:vg250:gem", "AGS"),
    "rki-landkreis": ("rki:landkreise", "IdLandkreis"),
    "nuts": ("gisco:nuts:2021", "NUTS_ID"),
    "eu-country": ("gisco:countries", "CNTR_ID"),
    "iso3166-1-alpha2": ("gisco:countries", "CNTR_ID"),
    "iso3166-1-alpha3": ("gisco:countries", "ISO3_CODE"),
}
NO_BOUNDARY = ("who-region", "who-global", "ecdc-aggregate")
REQUEST_KEYS = frozenset(
    {
        "namespace",
        "feature_id",
        "boundary_name",
        "boundary_collection",
        "geo_namespace",
        "condition",
        "reporting_as_of",
        "period_from",
        "period_to",
        "kind",
    }
)
_ISO3 = {
    "DEU": "DE",
    "FRA": "FR",
    "AUT": "AT",
    "POL": "PL",
    "NLD": "NL",
    "ITA": "IT",
    "ESP": "ES",
    "GRC": "EL",
}
_DDL = """
CREATE TABLE IF NOT EXISTS surveillance_geo_resolutions (
  namespace TEXT NOT NULL, resolution_id TEXT NOT NULL, geography_system TEXT NOT NULL, geography_code TEXT NOT NULL,
  code_list_version TEXT, geo_namespace TEXT NOT NULL, collection TEXT, code_property TEXT, state TEXT NOT NULL,
  reason TEXT, feature_id TEXT, feature_revision_id TEXT, feature_revision BIGINT, feature_title TEXT,
  boundary_vintage_json TEXT, place_id TEXT, place_revision_id TEXT, evaluation_no INTEGER NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, resolution_id)
);
CREATE TABLE IF NOT EXISTS surveillance_geo_reviews (
  namespace TEXT NOT NULL, review_id TEXT NOT NULL, resolution_id TEXT NOT NULL, revision INTEGER NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, review_id)
);
"""


def normalise_code(code: Any) -> str:
    """One normalisation for both sides of a match: trimmed and upper-cased."""
    return str(code if code is not None else "").strip().upper()


def collection_key(system: str, code: str) -> str:
    return f"ags:{len(code)}" if system == "ags" else system


def ancestry(system: str, code: str) -> tuple[tuple[str, str], set[tuple[str, str]]]:
    """(the code's own token, every token it lies within) in the published code-list hierarchies."""
    code = normalise_code(code)
    if system in {"eu-country", "iso3166-1-alpha2"}:
        own = ("C", "EL" if code == "GR" else code)
        return own, {own}
    if system == "iso3166-1-alpha3":
        own = ("C", _ISO3.get(code, code))
        return own, {own}
    if system == "nuts":
        return ("N", code), {("C", code[:2])} | {
            ("N", code[:k]) for k in range(3, len(code) + 1)
        }
    if system in {"ags", "rki-landkreis"}:
        return ("A", code), {("C", "DE")} | {
            ("A", code[:k]) for k in range(2, len(code) + 1)
        }
    return ("X", f"{system}:{code}"), {("X", f"{system}:{code}")}


def within(inner: tuple[str, str], outer: tuple[str, str]) -> bool:
    """Whether ``inner``'s code lies strictly within ``outer``'s code by the code lists' own hierarchy."""
    own_inner, tokens = ancestry(*inner)
    own_outer, _ = ancestry(*outer)
    return own_outer in tokens and own_outer != own_inner


class SurveillancePlaces:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = SurveillanceStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ resolution

    def _features(self, geo_namespace: str, collection: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_features"):
            return []
        rows = self.conn.execute(
            "SELECT f.feature_id, f.provider, r.revision_id, r.revision, r.title, r.properties_json, r.observed_at_ms "
            "FROM geospatial_features f JOIN geospatial_feature_current c ON c.feature_id=f.feature_id JOIN "
            "geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE f.namespace IN (?, 'global') AND "
            "f.collection=? AND c.lifecycle='active' ORDER BY f.feature_id",
            [geo_namespace, collection],
        ).fetchall()
        snapshot = {}
        if table_exists(self.conn, "geospatial_feature_snapshot_current"):
            snapshot = {
                (r[0], r[1]): r[2]
                for r in self.conn.execute(
                    "SELECT namespace, provider, provider_timestamp FROM geospatial_feature_snapshot_current WHERE "
                    "collection=?",
                    [collection],
                ).fetchall()
            }
        return [
            {
                "feature_id": r[0],
                "provider": r[1],
                "revision_id": r[2],
                "revision": int(r[3]),
                "title": r[4],
                "properties": json.loads(r[5] or "{}"),
                "observed_at_ms": int(r[6]),
                "snapshot_timestamp": snapshot.get((geo_namespace, r[1]))
                or snapshot.get(("global", r[1])),
            }
            for r in rows
        ]

    def _places(
        self, geo_namespace: str, system: str, code: str
    ) -> list[tuple[str, str]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, r.revision_id, r.source_ids_json FROM geospatial_places p JOIN "
            "geospatial_place_current c ON c.place_id=p.place_id JOIN geospatial_place_revisions r ON "
            "r.revision_id=c.revision_id WHERE p.namespace IN (?, 'global') ORDER BY p.place_id",
            [geo_namespace],
        ).fetchall()
        return [
            (place_id, revision_id)
            for place_id, revision_id, source_ids in rows
            if normalise_code(json.loads(source_ids or "{}").get(system)) == code
        ]

    def _pairs(self, namespace: str) -> list[tuple[str, str, str | None]]:
        """Every (code system, code, code-list version) the namespace's current vintages state."""
        pairs = set()
        for series_id, system, code in self.conn.execute(
            "SELECT series_id, geography_system, geography_code FROM surveillance_series WHERE namespace=?",
            [namespace],
        ).fetchall():
            rows = self.store.vintage_rows(namespace, series_id)
            pairs.add(
                (
                    system,
                    code,
                    rows[-1]["metadata"].get("code_list_version") if rows else None,
                )
            )
        return sorted(pairs, key=lambda p: (p[0], p[1], p[2] or ""))

    def resolve_geographies(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        geo_namespace: str = "global",
        collections: Mapping[str, Mapping[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Resolve every series geography code to a boundary feature (and place) by the published code; idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        chosen = {
            key: (
                str(
                    dict(collections or {}).get(key, {}).get("collection") or default[0]
                ),
                str(dict(collections or {}).get(key, {}).get("property") or default[1]),
            )
            for key, default in BOUNDARY_COLLECTIONS.items()
        }
        for key, override in dict(collections or {}).items():
            if key not in chosen:
                chosen[key] = (str(override["collection"]), str(override["property"]))
        cache: dict[str, list[dict[str, Any]]] = {}
        matched, unresolved, breaks = [], [], 0
        for system, code, version in self._pairs(namespace):
            wanted = normalise_code(code)
            key = collection_key(system, wanted)
            if system in NO_BOUNDARY or key not in chosen:
                collection = prop = None
                features, places = [], []
            else:
                collection, prop = chosen[key]
                features = [
                    f
                    for f in cache.setdefault(
                        collection, self._features(geo_namespace, collection)
                    )
                    if normalise_code(f["properties"].get(prop)) == wanted
                ]
                places = self._places(geo_namespace, system, wanted)
            if len(features) == 1 and len(places) <= 1:
                feature, state, reason = features[0], "matched", None
            else:
                feature, state = None, "unresolved"
                reason = (
                    "the code system has no boundary collection"
                    if collection is None
                    else "no boundary feature states this code"
                    if not features
                    else "more than one boundary feature states this code"
                    if len(features) > 1
                    else "more than one place carries this code"
                )
            place_id, place_revision = places[0] if feature and places else (None, None)
            outcome = [
                state,
                reason,
                collection,
                prop,
                None if feature is None else feature["feature_id"],
                None if feature is None else feature["revision_id"],
                place_id,
                place_revision,
            ]
            current = self._latest(namespace, system, code, version)
            if (
                current is not None
                and [
                    current[k]
                    for k in (
                        "state",
                        "reason",
                        "collection",
                        "code_property",
                        "feature_id",
                        "feature_revision_id",
                        "place_id",
                        "place_revision_id",
                    )
                ]
                == outcome
            ):
                continue  # the latest evaluation already says this
            number = 1 + (current["evaluation_no"] if current else 0)
            resolution_id = (
                "sv-geo:"
                + digest([namespace, system, code, version, number, outcome])[:24]
            )
            vintage = (
                None
                if feature is None
                else {
                    "collection": collection,
                    "feature_revision_id": feature["revision_id"],
                    "feature_revision": feature["revision"],
                    "observed_at_ms": feature["observed_at_ms"],
                    "snapshot_timestamp": feature["snapshot_timestamp"],
                }
            )
            self.conn.execute(
                "INSERT INTO surveillance_geo_resolutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    resolution_id,
                    system,
                    code,
                    version,
                    geo_namespace,
                    collection,
                    prop,
                    state,
                    reason,
                    None if feature is None else feature["feature_id"],
                    None if feature is None else feature["revision_id"],
                    None if feature is None else feature["revision"],
                    None if feature is None else feature["title"],
                    None if vintage is None else canonical(vintage),
                    place_id,
                    place_revision,
                    number,
                    principal_id,
                    self.now(),
                ],
            )
            (matched if state == "matched" else unresolved).append(resolution_id)
            if (
                current is not None
                and current["state"] == "matched"
                and current["feature_revision_id"] != outcome[5]
            ):
                breaks += self._geography_break(
                    namespace, system, code, current, feature, resolution_id
                )
        return {
            "matched": matched,
            "unresolved": unresolved,
            "geography_breaks": breaks,
            "geo_namespace": geo_namespace,
        }

    def _geography_break(
        self, namespace, system, code, before, feature, resolution_id
    ) -> int:
        """A boundary revision (or a boundary gone) under a code is a break on every series with that code."""
        added = 0
        for (series_id,) in self.conn.execute(
            "SELECT series_id FROM surveillance_series WHERE namespace=? AND geography_system=? AND "
            "geography_code=?",
            [namespace, system, code],
        ).fetchall():
            rows = self.store.vintage_rows(namespace, series_id)
            added += self.store.add_break(
                namespace,
                series_id,
                "geography",
                period=None,
                from_ref=before["feature_revision_id"],
                to_ref=None if feature is None else feature["revision_id"],
                detail={
                    "basis": "boundary revision under the published code",
                    "from_resolution": before["resolution_id"],
                    "to_resolution": resolution_id,
                    "from_feature_id": before["feature_id"],
                    "to_feature_id": None if feature is None else feature["feature_id"],
                },
                note="the boundary under this code changed; values are not re-aggregated to either boundary",
                vintage_id=rows[-1]["vintage_id"] if rows else None,
            )
        return added

    _KEYS = (
        "resolution_id",
        "geography_system",
        "geography_code",
        "code_list_version",
        "geo_namespace",
        "collection",
        "code_property",
        "state",
        "reason",
        "feature_id",
        "feature_revision_id",
        "feature_revision",
        "feature_title",
        "boundary_vintage",
        "place_id",
        "place_revision_id",
        "evaluation_no",
        "created_by",
        "created_at_ms",
    )

    def _select(self) -> str:
        return "SELECT " + ", ".join(
            "boundary_vintage_json" if k == "boundary_vintage" else k
            for k in self._KEYS
        )

    def _view(self, namespace: str, row: tuple) -> dict[str, Any]:
        view = dict(zip(self._KEYS, row))
        view["boundary_vintage"] = (
            json.loads(view["boundary_vintage"]) if view["boundary_vintage"] else None
        )
        review = None
        if table_exists(self.conn, "surveillance_geo_reviews"):
            review = self.conn.execute(
                "SELECT review_id, decision, reason, principal_id, revision FROM surveillance_geo_reviews WHERE "
                "namespace=? AND resolution_id=? ORDER BY revision DESC LIMIT 1",
                [namespace, view["resolution_id"]],
            ).fetchone()
        view["review"] = (
            None
            if review is None
            else dict(
                zip(("review_id", "decision", "reason", "reviewer", "revision"), review)
            )
        )
        view["review_state"] = (
            "unreviewed"
            if review is None
            else {"accept": "accepted", "reject": "rejected"}[review[1]]
        )
        view["effective"] = (
            "linked"
            if view["state"] == "matched" and view["review_state"] != "rejected"
            else "rejected"
            if view["state"] == "matched"
            else "unresolved"
        )
        view["basis"] = (
            f"published {view['geography_system']} code equals the feature's {view['code_property']} "
            "property"
            if view["code_property"]
            else "no boundary collection for this code system"
        )
        return view

    def _latest(self, namespace, system, code, version) -> dict[str, Any] | None:
        if not table_exists(self.conn, "surveillance_geo_resolutions"):
            return None
        row = self.conn.execute(
            self._select()
            + " FROM surveillance_geo_resolutions WHERE namespace=? AND geography_system=? AND "
            "geography_code=? AND coalesce(code_list_version, '')=coalesce(?, '') ORDER BY evaluation_no DESC "
            "LIMIT 1",
            [namespace, system, code, version],
        ).fetchone()
        return None if row is None else self._view(namespace, row)

    def resolution(
        self, namespace: str, resolution_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            self._select() + " FROM surveillance_geo_resolutions WHERE namespace=? AND "
            "resolution_id=?",
            [namespace, resolution_id],
        ).fetchone()
        if row is None:
            raise SurveillanceError(
                "not_found", "resolution is not visible in this namespace"
            )
        return self._view(namespace, row)

    def resolutions(
        self, namespace: str, *, scopes: Iterable[str], current_only: bool = True
    ) -> list[dict]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "surveillance_geo_resolutions"):
            return []
        rows = self.conn.execute(
            "SELECT resolution_id, geography_system, geography_code, coalesce(code_list_version, ''), evaluation_no "
            "FROM surveillance_geo_resolutions WHERE namespace=? ORDER BY 2, 3, 4, 5",
            [namespace],
        ).fetchall()
        latest = {}
        for resolution_id, system, code, version, _n in rows:
            latest[(system, code, version)] = resolution_id
        wanted = latest.values() if current_only else [r[0] for r in rows]
        return [self.resolution(namespace, rid, scopes={"operator"}) for rid in wanted]

    def review(
        self, namespace, resolution_id, decision, reason, *, principal_id, scopes
    ):
        """Accept or reject a resolution; a later review replaces an earlier one for that resolution only."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise SurveillanceError(
                "invalid_decision", "accept or reject with a reason"
            )
        view = self.resolution(namespace, resolution_id, scopes={"operator"})
        if view["state"] != "matched":
            raise SurveillanceError(
                "invalid_state",
                "an unresolved code has nothing to accept; it stays unresolved",
            )
        revision = 1 + (view["review"]["revision"] if view["review"] else 0)
        review_id = (
            "sv-geo-review:"
            + digest([namespace, resolution_id, revision, decision, reason])[:24]
        )
        self.conn.execute(
            "INSERT INTO surveillance_geo_reviews VALUES (?,?,?,?,?,?,?,?)",
            [
                namespace,
                review_id,
                resolution_id,
                revision,
                decision,
                reason.strip(),
                principal_id,
                self.now(),
            ],
        )
        return self.resolution(namespace, resolution_id, scopes={"operator"})

    # ------------------------------------------------------------------ queries

    def _boundary(
        self,
        namespace,
        feature_id,
        boundary_name,
        boundary_collection,
        geo_namespace,
        scopes,
    ):
        if bool(feature_id) == bool(boundary_name):
            raise SurveillanceError(
                "invalid_boundary", "name exactly one of feature_id or boundary_name"
            )
        if feature_id:
            return feature_id, None
        # A name is resolved through the existing place-resolution path (read scope checked at call time).
        require_scope(scopes, GEO_READ)
        from src.kb.geospatial_features import GeospatialFeatureStore

        resolved = GeospatialFeatureStore(self.conn, initialize=False).resolve_boundary(
            geo_namespace,
            boundary_name,
            collection=boundary_collection,
            scopes={GEO_READ},
        )
        if resolved["status"] != "resolved":
            raise SurveillanceError(
                "boundary_unresolved",
                "the boundary name does not resolve to one feature",
                resolution=resolved,
            )
        return resolved["selected_feature_id"], resolved

    def boundary_series(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        feature_id: str | None = None,
        boundary_name: str | None = None,
        boundary_collection: str | None = None,
        geo_namespace: str = "global",
        condition: str | None = None,
        reporting_as_of: str | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Series for a condition within one boundary, as of a reporting date; sources side by side."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        requested_feature_id = feature_id
        feature_id, name_resolution = self._boundary(
            namespace,
            feature_id,
            boundary_name,
            boundary_collection,
            geo_namespace,
            scopes,
        )
        current = [
            r
            for r in self.resolutions(namespace, scopes={"operator"})
            if r["feature_id"] == feature_id
        ]
        linked = [r for r in current if r["effective"] == "linked"]
        boundary_codes = self._feature_codes(feature_id)
        if not current and not boundary_codes:
            raise SurveillanceError(
                "not_found",
                "no series geography is resolved to this boundary and the boundary "
                "states no code of a known code list",
            )
        expansion = None
        wanted_series = None
        if condition:
            from src.kb.clinical_terms import ClinicalTerms

            expansion = ClinicalTerms(self.conn, initialize=False).expand_surveillance(
                namespace, condition, scopes=scopes
            )
            wanted_series = {s["series_id"] for s in expansion["series"]}
        own = {
            (r["geography_system"], normalise_code(r["geography_code"])) for r in linked
        } | boundary_codes
        by_code = {(r["geography_system"], r["geography_code"]): r for r in linked}
        columns, contained = [], []
        for series in self.store.find_series(namespace, kind=kind, limit=5000):
            if wanted_series is not None and series["series_id"] not in wanted_series:
                continue
            geography = (
                series["geography"]["system"],
                normalise_code(series["geography"]["code"]),
            )
            resolution = by_code.get(
                (series["geography"]["system"], series["geography"]["code"])
            )
            if resolution is None and not any(within(geography, g) for g in own):
                continue
            answer = self.store.answer(
                namespace,
                series["series_id"],
                scopes={"operator"},
                reporting_as_of=reporting_as_of,
                period_from=period_from,
                period_to=period_to,
            )
            column = {
                "series_id": series["series_id"],
                "provider": series["provider"],
                "condition": series["condition"],
                "indicator": series["indicator"],
                "geography": series["geography"],
                "kind": series["kind"],
                "unit": series["unit"],
                "interval": series["interval"],
                "dimensions": series["dimensions"],
                "status": answer["status"],
                "reason": answer.get("reason"),
                "vintage": answer.get("vintage"),
                "values": answer["values"],
                "reporting_date_unknown": answer["reporting_date_unknown"],
                "breaks": series["breaks"],
                "delay_note": series["delay_note"],
            }
            if resolution is not None:
                column["resolution"] = {
                    k: resolution[k]
                    for k in (
                        "resolution_id",
                        "geography_system",
                        "code_list_version",
                        "collection",
                        "boundary_vintage",
                        "review_state",
                        "place_id",
                    )
                }
                columns.append(column)
            else:
                column["relation"] = (
                    "published code lies within the boundary's code (code-list hierarchy)"
                )
                contained.append(column)
        request = {
            "namespace": namespace,
            "feature_id": requested_feature_id,
            "boundary_name": boundary_name,
            "boundary_collection": boundary_collection,
            "geo_namespace": geo_namespace,
            "condition": condition,
            "reporting_as_of": reporting_as_of,
            "period_from": period_from,
            "period_to": period_to,
            "kind": kind,
        }
        selected = sorted(
            (
                {
                    "series_id": c["series_id"],
                    "vintage_id": (c["vintage"] or {}).get("vintage_id"),
                    "status": c["status"],
                }
                for c in columns + contained
            ),
            key=lambda c: c["series_id"],
        )
        receipt = {
            "contract": RECEIPT_CONTRACT,
            "request": request,
            "resolved_feature_id": feature_id,
            "resolutions": sorted(r["resolution_id"] for r in linked),
            "selected": selected,
        }
        receipt["digest"] = digest(receipt)
        return {
            "contract": ANSWER_CONTRACT,
            "feature_id": feature_id,
            "boundary_name_resolution": name_resolution,
            "boundary": [
                {
                    k: r[k]
                    for k in (
                        "collection",
                        "feature_title",
                        "feature_revision_id",
                        "boundary_vintage",
                        "effective",
                        "geography_system",
                        "geography_code",
                    )
                }
                for r in current
            ],
            "series": columns,
            "contained_units": contained,
            "side_by_side": side_by_side(columns),
            "expansion": expansion,
            "receipt": receipt,
            "boundary_sentence": NEVER_SENTENCE,
            "note": "series whose published code resolves to this boundary, and series whose code lies within it; "
            "each with its own values, never aggregated, apportioned or merged",
        }

    def _feature_codes(self, feature_id: str) -> set[tuple[str, str]]:
        """The published codes a boundary feature itself states (for series that lie within it by code)."""
        if not table_exists(self.conn, "geospatial_features"):
            return set()
        row = self.conn.execute(
            "SELECT f.collection, r.properties_json FROM geospatial_features f JOIN geospatial_feature_current c ON "
            "c.feature_id=f.feature_id JOIN geospatial_feature_revisions r ON r.revision_id=c.revision_id WHERE "
            "f.feature_id=? AND c.lifecycle='active'",
            [feature_id],
        ).fetchone()
        if row is None:
            return set()
        properties = json.loads(row[1] or "{}")
        codes = set()
        for key, (collection, prop) in BOUNDARY_COLLECTIONS.items():
            if collection == row[0] and normalise_code(properties.get(prop)):
                codes.add((key.split(":", 1)[0], normalise_code(properties.get(prop))))
        return codes

    def replay(
        self, receipt: Mapping[str, Any], *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Re-run a receipt with every recorded parameter: ``reproduced`` when the same vintages are selected."""
        if receipt.get("contract") != RECEIPT_CONTRACT or not isinstance(
            receipt.get("request"), Mapping
        ):
            raise SurveillanceError(
                "invalid_receipt", "the receipt is not a surveillance query receipt"
            )
        request = dict(receipt["request"])
        if set(request) != REQUEST_KEYS:
            raise SurveillanceError(
                "invalid_receipt",
                "the receipt's request does not name exactly the query parameters",
            )
        namespace = request.pop("namespace")
        answer = self.boundary_series(namespace, scopes=scopes, **request)
        return {
            "status": "reproduced"
            if answer["receipt"]["digest"] == receipt.get("digest")
            else "changed",
            "receipt": answer["receipt"],
            "previous_digest": receipt.get("digest"),
        }


def side_by_side(columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Periods several sources publish for the same kind and unit, their values listed together, never merged."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for column in columns:
        for value in column["values"]:
            if value["reference_period"] is None:
                continue
            groups.setdefault(
                (value["reference_period"], column["kind"], column["unit"]["label"]), []
            ).append(
                {
                    "provider": column["provider"],
                    "series_id": column["series_id"],
                    "value": value["value"],
                    "value_text": value["value_text"],
                    "reporting_date": value["reporting_date"],
                    "vintage_id": (column["vintage"] or {}).get("vintage_id"),
                }
            )
    out = []
    for (period, kind, unit), values in sorted(groups.items()):
        providers = {v["provider"] for v in values}
        if len(providers) < 2:
            continue
        numbers = {number_key(v["value"]) for v in values if v["value"] is not None}
        out.append(
            {
                "reference_period": period,
                "kind": kind,
                "unit": unit,
                "values": values,
                "differ": len(numbers) > 1,
                "note": "values of different sources kept side by side; nothing merged, averaged or preferred",
            }
        )
    return out
