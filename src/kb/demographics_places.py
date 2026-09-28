"""Regional series projected onto Geospatial boundaries and answered as of a release date (#1914, M07).

Geography codes (NUTS, country codes, AGS, Berlin Bezirk, COD-AB p-codes) are
resolved to boundary features the Geospatial feature store already holds
(:mod:`src.kb.geospatial_features`; for Berlin, ``alkis_bezirke:bezirksgrenzen``
by its ``gem`` code) and, where one is registered, to the
:class:`src.kb.geospatial.GeospatialStore` place whose source identifiers carry
the code. Matching is by the published code only - never by name, geometry or
nearest match - and a code matching no feature, or several, stays unresolved
with the reason. Every resolution records the code-list version and the
boundary vintage (feature revision, observation time and collection snapshot
timestamp) it used, and is reviewable: a reviewer accepts or rejects it, and a
rejected resolution is not used. No spatial store is introduced.

A boundary query returns the series whose geography resolves to that boundary,
with definition, vintage, unit, level and comparability notes. Series at other
levels of the same geography family are listed as available at a different
level; nothing is aggregated or apportioned to the requested boundary. As-of
queries select the vintage released on or before the requested date (the
release-cutoff semantics of :mod:`src.domains.economic.releases`) and report
``historical_vintage_unavailable`` when none is retained. Each answer carries a
replayable receipt; pinning a receipt keeps the vintages it used, and a later
revision shows the pin as ``stale`` rather than replacing its values.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from itertools import combinations
from typing import Any

from src.ingestion.demographic_sources import geography_code
from src.kb.demographics import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    DemographicError,
    DemographicStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

GEO_READ = "knowledge:geospatial:read"
RECEIPT_CONTRACT = "noesis-demographic-query-receipt-v1"
# Default boundary collections per code list: (collection, the feature property carrying the published code).
BOUNDARY_COLLECTIONS: dict[str, tuple[str, str]] = {
    "berlin-bezirk": ("alkis_bezirke:bezirksgrenzen", "gem"),
    "nuts": ("gisco:nuts:2021", "NUTS_ID"),
    "eu-country": ("gisco:countries", "CNTR_ID"),
    "ags": ("bkg:vg250:lan", "AGS"),
    "iso3166-1-alpha3": ("gisco:countries", "ISO3_CODE"),
    "cod-ab-pcode": ("cod-ab:admin1", "pcode"),
}
# The place source-identifier key carrying each code list's codes.
PLACE_KEYS = {
    "berlin-bezirk": "berlin-bezirk",
    "nuts": "nuts",
    "eu-country": "eu-country",
    "ags": "ags",
    "iso3166-1-alpha3": "iso3166-1-alpha3",
    "cod-ab-pcode": "cod-ab-pcode",
}
_ISO3 = {
    "DEU": "DE",
    "FRA": "FR",
    "AUT": "AT",
    "POL": "PL",
    "NLD": "NL",
    "ITA": "IT",
    "ESP": "ES",
}
_DDL = """
CREATE TABLE IF NOT EXISTS demographic_geo_resolutions (
  namespace TEXT NOT NULL, resolution_id TEXT NOT NULL, level_id TEXT NOT NULL, geography_code TEXT NOT NULL,
  scheme TEXT NOT NULL, code_list_version TEXT, geo_namespace TEXT NOT NULL, collection TEXT NOT NULL,
  code_property TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, feature_id TEXT, feature_revision_id TEXT,
  feature_revision BIGINT, feature_title TEXT, boundary_vintage_json TEXT, place_id TEXT, place_revision_id TEXT,
  evaluation_no INTEGER NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, resolution_id)
);
CREATE TABLE IF NOT EXISTS demographic_geo_reviews (
  namespace TEXT NOT NULL, review_id TEXT NOT NULL, resolution_id TEXT NOT NULL, revision INTEGER NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, review_id)
);
CREATE TABLE IF NOT EXISTS demographic_pins (
  namespace TEXT NOT NULL, pin_id TEXT NOT NULL, receipt_digest TEXT NOT NULL, series_id TEXT NOT NULL,
  vintage_id TEXT NOT NULL, receipt_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, pin_id)
);
"""


def ancestry(scheme: str, code: str) -> tuple[tuple[str, str], set[tuple[str, str]]]:
    """(the code's own token, every token it lies within) for the published code-list hierarchies."""
    code = str(code).upper()
    if scheme == "eu-country":
        return ("C", code), {("C", code)}
    if scheme == "iso3166-1-alpha3":
        own = ("C", _ISO3.get(code, code))
        return own, {own}
    if scheme == "nuts":
        tokens = {("C", code[:2])} | {("N", code[:k]) for k in range(3, len(code) + 1)}
        return ("N", code), tokens
    if scheme == "ags":
        tokens = {("C", "DE")} | {("A", code[:k]) for k in range(2, len(code) + 1)}
        return ("A", code), tokens
    if scheme == "berlin-bezirk":
        # Berlin's districts lie within the Land (AGS 11, NUTS DE3/DE30/DE300) and Germany.
        tokens = {
            ("C", "DE"),
            ("A", "11"),
            ("N", "DE3"),
            ("N", "DE30"),
            ("N", "DE300"),
            ("B", code),
        }
        return ("B", code), tokens
    tokens = {("C", _ISO3.get(code[:3], code[:3]))} | {
        ("P", code[:k]) for k in range(3, len(code) + 1)
    }
    return ("P", code), tokens


def related(a: tuple[str, str], b: tuple[str, str]) -> bool:
    own_a, anc_a = ancestry(*a)
    own_b, anc_b = ancestry(*b)
    return own_a in anc_b or own_b in anc_a


class DemographicPlaces:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = DemographicStore(conn, initialize=initialize, now=now)
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
                    "SELECT namespace, provider, provider_timestamp FROM geospatial_feature_snapshot_current "
                    "WHERE collection=?",
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
        self, geo_namespace: str, scheme: str, code: str
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
            if geography_code(
                scheme, json.loads(source_ids or "{}").get(PLACE_KEYS[scheme])
            )
            == code
        ]

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
            scheme: (
                str(
                    dict(collections or {}).get(scheme, {}).get("collection")
                    or default[0]
                ),
                str(
                    dict(collections or {}).get(scheme, {}).get("property")
                    or default[1]
                ),
            )
            for scheme, default in BOUNDARY_COLLECTIONS.items()
        }
        pairs = self.conn.execute(
            "SELECT DISTINCT s.level_id, s.geography_code, l.scheme, l.code_list_version FROM demographic_series s "
            "JOIN demographic_geography_levels l ON l.namespace=s.namespace AND l.level_id=s.level_id WHERE "
            "s.namespace=? ORDER BY 1, 2",
            [namespace],
        ).fetchall()
        feature_cache: dict[str, list[dict[str, Any]]] = {}
        linked, unresolved = [], []
        for level_id, code, scheme, version in pairs:
            collection, prop = chosen[scheme]
            features = feature_cache.setdefault(
                collection, self._features(geo_namespace, collection)
            )
            matches = [
                f
                for f in features
                if geography_code(scheme, f["properties"].get(prop)) == code
            ]
            places = self._places(geo_namespace, scheme, code)
            if len(matches) == 1 and len(places) <= 1:
                feature, state, reason = matches[0], "matched", None
            else:
                feature, state = None, "unresolved"
                reason = (
                    "no boundary feature states this code"
                    if not matches
                    else "more than one boundary feature states this code"
                    if len(matches) > 1
                    else "more than one place carries this code"
                )
            place_id, place_revision = places[0] if feature and places else (None, None)
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
            current = self._latest(namespace, level_id, code)
            if (
                current is not None
                and [
                    current["state"],
                    current["reason"],
                    current["collection"],
                    current["code_property"],
                    current["feature_id"],
                    current["feature_revision_id"],
                    current["place_id"],
                    current["place_revision_id"],
                ]
                == outcome
            ):
                continue  # the latest evaluation already says this
            number = 1 + (current["evaluation_no"] if current else 0)
            resolution_id = (
                "dm-geo:" + digest([namespace, level_id, code, number, outcome])[:24]
            )
            self.conn.execute(
                "INSERT INTO demographic_geo_resolutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    resolution_id,
                    level_id,
                    code,
                    scheme,
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
            (linked if state == "matched" else unresolved).append(resolution_id)
        return {
            "matched": linked,
            "unresolved": unresolved,
            "geo_namespace": geo_namespace,
        }

    _KEYS = (
        "resolution_id",
        "level_id",
        "geography_code",
        "scheme",
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

    def _view(self, namespace: str, row: tuple) -> dict[str, Any]:
        view = dict(zip(self._KEYS, row))
        view["boundary_vintage"] = (
            json.loads(view["boundary_vintage"]) if view["boundary_vintage"] else None
        )
        review = None
        if table_exists(self.conn, "demographic_geo_reviews"):
            review = self.conn.execute(
                "SELECT review_id, decision, reason, principal_id, revision FROM demographic_geo_reviews WHERE "
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
            f"published {view['scheme']} code equals the feature's {view['code_property']} property"
        )
        return view

    def _latest(
        self, namespace: str, level_id: str, code: str
    ) -> dict[str, Any] | None:
        if not table_exists(self.conn, "demographic_geo_resolutions"):
            return None
        row = self.conn.execute(
            "SELECT "
            + ", ".join(
                k if k != "boundary_vintage" else "boundary_vintage_json"
                for k in self._KEYS
            )
            + " FROM demographic_geo_resolutions WHERE namespace=? AND level_id=? AND geography_code=? "
            "ORDER BY evaluation_no DESC LIMIT 1",
            [namespace, level_id, code],
        ).fetchone()
        return None if row is None else self._view(namespace, row)

    def resolution(
        self, namespace: str, resolution_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT "
            + ", ".join(
                k if k != "boundary_vintage" else "boundary_vintage_json"
                for k in self._KEYS
            )
            + " FROM demographic_geo_resolutions WHERE namespace=? AND resolution_id=?",
            [namespace, resolution_id],
        ).fetchone()
        if row is None:
            raise DemographicError(
                "not_found", "resolution is not visible in this namespace"
            )
        return self._view(namespace, row)

    def resolutions(
        self, namespace: str, *, scopes: Iterable[str], current_only: bool = True
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "demographic_geo_resolutions"):
            return []
        rows = self.conn.execute(
            "SELECT resolution_id, level_id, geography_code, evaluation_no FROM demographic_geo_resolutions WHERE "
            "namespace=? ORDER BY level_id, geography_code, evaluation_no",
            [namespace],
        ).fetchall()
        latest = {}
        for resolution_id, level_id, code, _n in rows:
            latest[(level_id, code)] = resolution_id
        wanted = latest.values() if current_only else [r[0] for r in rows]
        return [self.resolution(namespace, rid, scopes={"operator"}) for rid in wanted]

    def review(
        self, namespace, resolution_id, decision, reason, *, principal_id, scopes
    ):
        """Accept or reject a resolution; a later review replaces an earlier one for that resolution only."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise DemographicError("invalid_decision", "accept or reject with a reason")
        view = self.resolution(namespace, resolution_id, scopes={"operator"})
        if view["state"] != "matched":
            raise DemographicError(
                "invalid_state",
                "an unresolved code has nothing to accept; it stays unresolved",
            )
        revision = 1 + (view["review"]["revision"] if view["review"] else 0)
        review_id = (
            "dm-geo-review:"
            + digest([namespace, resolution_id, revision, decision, reason])[:24]
        )
        self.conn.execute(
            "INSERT INTO demographic_geo_reviews VALUES (?,?,?,?,?,?,?,?)",
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

    def boundary_series(
        self,
        namespace: str,
        feature_id: str,
        *,
        scopes: Iterable[str],
        period_from: str | None = None,
        period_to: str | None = None,
        as_of_ms: int | None = None,
        concept: str | None = None,
    ) -> dict[str, Any]:
        """Series resolved to one boundary with their values as of a date; other levels listed, never apportioned."""
        from src.kb.demographics_comparability import DemographicComparability

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        current = [
            r
            for r in self.resolutions(namespace, scopes={"operator"})
            if r["feature_id"] == feature_id
        ]
        matched = [r for r in current if r["effective"] == "linked"]
        if not current:
            raise DemographicError(
                "not_found", "no series geography is resolved to this boundary"
            )
        keys = {(r["level_id"], r["geography_code"]) for r in matched}
        own = {(r["scheme"], r["geography_code"]) for r in matched}
        columns, other_levels = [], []
        comparability = DemographicComparability(self.conn, initialize=False)
        for series in self.store.find_series(namespace, concept=concept):
            key = (series["level_id"], series["geography_code"])
            geography = (series["geography_level"]["scheme"], series["geography_code"])
            if key in keys:
                resolution = next(
                    r for r in matched if (r["level_id"], r["geography_code"]) == key
                )
                # The vintage released by the cutoff is selected first; the period filter applies to its values.
                values = self.store.values(
                    namespace,
                    series["series_id"],
                    as_of_ms=as_of_ms,
                    period_from=period_from,
                    period_to=period_to,
                )
                columns.append(
                    {
                        "series_id": series["series_id"],
                        "provider": series["provider"],
                        "series_code": series["series_code"],
                        "indicator": series["indicator"],
                        "dimensions": series["dimensions"],
                        "geography_level": series["geography_level"],
                        "unit": series["unit"],
                        "status": values["status"],
                        "reason": values.get("reason"),
                        "definition": values.get("definition"),
                        "vintage": values.get("vintage"),
                        "coverage_notes": values.get("coverage_notes", []),
                        "breaks": series["breaks"],
                        "observations": values["observations"],
                        "resolution": {
                            k: resolution[k]
                            for k in (
                                "resolution_id",
                                "code_list_version",
                                "boundary_vintage",
                                "review_state",
                                "place_id",
                            )
                        },
                    }
                )
            elif any(related(geography, g) for g in own):
                other_levels.append(
                    {
                        "series_id": series["series_id"],
                        "provider": series["provider"],
                        "series_code": series["series_code"],
                        "indicator": series["indicator"],
                        "geography": {
                            "code": series["geography_code"],
                            "label": series["geography_label"],
                        },
                        "geography_level": series["geography_level"],
                        "availability": "available_at_different_level",
                    }
                )
        pairs = [
            comparability._pair(
                namespace,
                {"kind": "series", "id": x["series_id"]},
                {"kind": "series", "id": y["series_id"]},
            )
            for x, y in combinations(columns, 2)
        ]
        request = {
            "namespace": namespace,
            "feature_id": feature_id,
            "period_from": period_from,
            "period_to": period_to,
            "as_of_ms": as_of_ms,
            "concept": concept,
        }
        selected = [
            {
                "series_id": c["series_id"],
                "vintage_id": (c["vintage"] or {}).get("vintage_id"),
                "status": c["status"],
                "reason": c["reason"],
            }
            for c in columns
        ]
        receipt = {
            "contract": RECEIPT_CONTRACT,
            "request": request,
            "resolutions": sorted(r["resolution_id"] for r in matched),
            "selected": selected,
        }
        receipt["digest"] = digest(receipt)
        return {
            "contract": ANSWER_CONTRACT,
            "feature_id": feature_id,
            "boundary": [
                {
                    k: r[k]
                    for k in (
                        "collection",
                        "feature_title",
                        "feature_revision_id",
                        "boundary_vintage",
                        "effective",
                    )
                }
                for r in current
            ],
            "series": columns,
            "other_levels": other_levels,
            "pairs": pairs,
            "receipt": receipt,
            "note": "series whose published code resolves to this boundary; series at other levels are listed, never "
            "aggregated or apportioned to it",
        }

    def replay(
        self, receipt: Mapping[str, Any], *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Re-run a receipt's request: ``reproduced`` when the same vintages are selected, else ``changed``."""
        request = dict(receipt["request"])
        answer = self.boundary_series(
            request.pop("namespace"),
            request.pop("feature_id"),
            scopes=scopes,
            **request,
        )
        same = answer["receipt"]["digest"] == receipt.get("digest")
        return {
            "status": "reproduced" if same else "changed",
            "receipt": answer["receipt"],
            "previous_digest": receipt.get("digest"),
        }

    # ------------------------------------------------------------------ pins

    def pin(
        self,
        namespace: str,
        receipt: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Keep the vintages a receipt used; idempotent per receipt."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        body = {k: v for k, v in dict(receipt).items() if k != "digest"}
        if receipt.get("contract") != RECEIPT_CONTRACT or digest(body) != receipt.get(
            "digest"
        ):
            raise DemographicError(
                "invalid_receipt", "the receipt is not a demographic query receipt"
            )
        if dict(receipt["request"]).get("namespace") != namespace:
            raise DemographicError(
                "invalid_receipt", "the receipt belongs to another namespace"
            )
        for item in receipt["selected"]:
            if not item.get("vintage_id"):
                continue
            pin_id = (
                "dm-pin:"
                + digest(
                    [
                        namespace,
                        receipt["digest"],
                        item["series_id"],
                        item["vintage_id"],
                    ]
                )[:24]
            )
            if not self.conn.execute(
                "SELECT 1 FROM demographic_pins WHERE namespace=? AND pin_id=?",
                [namespace, pin_id],
            ).fetchone():
                self.store.vintage(
                    namespace, item["vintage_id"]
                )  # the vintage must exist here
                self.conn.execute(
                    "INSERT INTO demographic_pins VALUES (?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        pin_id,
                        receipt["digest"],
                        item["series_id"],
                        item["vintage_id"],
                        canonical(dict(receipt)),
                        principal_id,
                        self.now(),
                    ],
                )
        return self.pins(
            namespace, scopes={"operator"}, receipt_digest=receipt["digest"]
        )

    def pins(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        receipt_digest: str | None = None,
    ) -> dict[str, Any]:
        """Pinned vintages with their own values; a later vintage marks a pin ``stale`` and never replaces it."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "demographic_pins"):
            return {"pins": []}
        rows = self.conn.execute(
            "SELECT pin_id, receipt_digest, series_id, vintage_id, created_by, created_at_ms FROM demographic_pins "
            "WHERE namespace=? AND (? IS NULL OR receipt_digest=?) ORDER BY created_at_ms, pin_id",
            [namespace, receipt_digest, receipt_digest],
        ).fetchall()
        out = []
        for pin_id, receipt, series_id, vintage_id, by, at in rows:
            vintages = self.store.vintage_rows(namespace, series_id)
            pinned = next(v for v in vintages if v["vintage_id"] == vintage_id)
            later = [
                v for v in vintages if v["release_at_ms"] > pinned["release_at_ms"]
            ]
            out.append(
                {
                    "pin_id": pin_id,
                    "receipt_digest": receipt,
                    "series_id": series_id,
                    "vintage_id": vintage_id,
                    "status": "stale" if later else "current",
                    "newer_vintage_ids": [v["vintage_id"] for v in later],
                    "observations": self.store.observations(namespace, vintage_id),
                    "pinned_by": by,
                    "pinned_at_ms": at,
                }
            )
        return {"pins": out}
