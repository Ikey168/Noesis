"""Tourism series linked to Geospatial boundaries and Labour series by citation, shared code or accepted match (TO06).

Track #2739. Two kinds of link, each recording its **basis** and pinning **specific revisions** on both sides (the
tourism series vintage in force when the link was made, and the target's current feature revision or latest vintage):

* ``boundary`` - the Geospatial boundary feature of the series' place: a feature in the GISCO NUTS collection of the
  series' NUTS version (``gisco:nuts:2021``) whose ``NUTS_ID`` is the published code, as
  :mod:`src.kb.public_finance_places` links districts (basis ``shared-identifier``). Nothing is matched by name or
  geometry; a code another NUTS version states is a different key and is never looked up in this version's
  collection. When a reviewer accepted the place key as a Geospatial place (TO05), the place is recorded too.
* ``labour`` - Economics Labour series (``economics.labour``, ``labour_series``) for accommodation and food services
  (NACE Rev.2 section ``I``) for the same place: by the same published area code (basis ``shared-identifier``) or
  because a reviewer accepted both the tourism place key and the labour area as the same Geospatial place (basis
  ``accepted-match``, citing both assertions). The link lists the reference years the two pinned vintages share and
  cites both; it never computes a ratio, share, per-employee or per-bed figure.

When the Geospatial or Labour provider is not composed (its store is absent) the link is recorded as
``provider_absent``; no held target is ``target_not_held``; a code several features carry is ``unresolved`` -
reported, never dropped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.tourism_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    TourismError,
    authorize,
    canonical,
    digest,
    iso,
    load,
    table_exists,
)
from src.kb.tourism_store import TourismStore, citation

CONTRACT = "noesis-tourism-link-v1"
KINDS = ("boundary", "labour")
BASES = ("citation", "shared-identifier", "accepted-match")
STATES = ("linked", "unresolved", "provider_absent", "target_not_held")
NO_DERIVATION = ("A link records what is related and on which basis; no value is combined and no ratio, share, "
                 "per-capita, per-bed or other derived figure is computed.")
LABOUR_SECTOR = {"scheme": "NACE", "version": "Rev.2", "code": "I"}
BOUNDARY_PROPERTY = "NUTS_ID"
_DDL = """
CREATE TABLE IF NOT EXISTS tourism_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT, kind TEXT NOT NULL,
  basis TEXT, reference_json TEXT NOT NULL, target_json TEXT, state TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def boundary_collection(nuts_version: str) -> str:
    return f"gisco:nuts:{nuts_version}"


def _years(periods: Iterable[str]) -> set[str]:
    return {str(p)[:4] for p in periods}


class TourismLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = TourismStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _identity(self):
        from src.kb.tourism_identity import TourismIdentity

        return TourismIdentity(self.conn, initialize=False, now=self.now)

    def _current(self, namespace: str, series_id: str) -> dict[str, Any] | None:
        published = [v for v in self.store.vintage_rows(namespace, series_id) if v["status"] == "published"]
        return published[-1] if published else None

    def _put(self, namespace, series, vintage, kind, basis, reference, target, state, evidence, principal_id):
        link_id = "to-link:" + digest([namespace, series["series_id"], vintage and vintage["vintage_id"], kind,
                                       reference, target, state])[:24]
        if self.conn.execute("SELECT 1 FROM tourism_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return self.link(namespace, link_id), False
        self.conn.execute("INSERT INTO tourism_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, series["series_id"], vintage and vintage["vintage_id"], kind, basis,
                           canonical(reference), None if target is None else canonical(target), state,
                           canonical({**evidence, "note": NO_DERIVATION}), principal_id, self.now()])
        return self.link(namespace, link_id), True

    def _periods(self, namespace: str, vintage_id: str) -> set[str]:
        return {o["period"] for o in self.store.observations(namespace, vintage_id) if o["status"] == "reported"}

    def _place(self, namespace: str, series: Mapping[str, Any]) -> dict[str, Any] | None:
        if not table_exists(self.conn, "tourism_identity_assertions"):
            return None
        return self._identity().place_for_area(namespace, series["area"])

    def _cite(self, namespace: str, series: Mapping[str, Any], vintage: Mapping[str, Any]) -> dict[str, Any]:
        return citation(series, vintage, self.store.release(namespace, vintage["release_id"]))

    @staticmethod
    def _reference(series: Mapping[str, Any]) -> dict[str, Any]:
        area = series["area"]
        return {"scheme": area["scheme"], "nuts_version": area["nuts_version"], "code": area["code"]}

    # ------------------------------------------------------------------ geospatial boundaries

    def link_boundaries(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                        geo_namespace: str = "global") -> dict[str, Any]:
        """Link each series' place key to the boundary feature carrying its NUTS code in its NUTS version."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        held = table_exists(self.conn, "geospatial_features")
        if held and "knowledge:geospatial:read" not in scopes and "operator" not in scopes:
            raise TourismError("unauthorized", "knowledge:geospatial:read is required to read the boundaries")
        out: dict[str, list[str]] = {"linked": [], "missing": []}
        for series in self.store.find_series(namespace):
            vintage = self._current(namespace, series["series_id"])
            if vintage is None:
                continue
            reference = self._reference(series)
            collection = boundary_collection(reference["nuts_version"])
            if not held:
                link, new = self._put(namespace, series, vintage, "boundary", None, reference, None,
                                      "provider_absent", {"reason": "the Geospatial provider is not composed "
                                                                    "(geospatial_features)",
                                                          "collection": collection}, principal_id)
                out["missing"] += [link["link_id"]] if new else []
                continue
            rows = self.conn.execute(
                "SELECT f.feature_id, f.native_id, r.revision_id, r.title, r.properties_json FROM geospatial_features f "
                "JOIN geospatial_feature_current c ON c.feature_id=f.feature_id JOIN geospatial_feature_revisions r ON "
                "r.revision_id=c.revision_id WHERE f.namespace IN (?, 'global') AND f.collection=? AND "
                "c.lifecycle='active' ORDER BY f.feature_id", [geo_namespace, collection]).fetchall()
            matches = [r for r in rows if str(json.loads(r[4] or "{}").get(BOUNDARY_PROPERTY)) == reference["code"]]
            place = self._place(namespace, series)
            evidence = {"collection": collection, "property": BOUNDARY_PROPERTY, "code": reference["code"],
                        "nuts_version": reference["nuts_version"], "tourism_citation": self._cite(
                            namespace, series, vintage),
                        "place": None if place is None else {k: place[k] for k in ("place_id", "place_revision_id",
                                                                                   "assertion_id")}}
            if len(matches) == 1:
                feature_id, native_id, revision_id, title, _props = matches[0]
                target = {"kind": "geospatial-feature", "provider_id": "geospatial.core", "id": feature_id,
                          "native_id": native_id, "revision_id": revision_id, "title": title,
                          "collection": collection}
                link, new = self._put(namespace, series, vintage, "boundary", "shared-identifier", reference, target,
                                      "linked", evidence, principal_id)
                out["linked"] += [link["link_id"]] if new else []
            else:
                state = "unresolved" if matches else "target_not_held"
                reason = ("more than one boundary feature states this code" if matches else
                          f"no boundary feature in {collection} states this code")
                link, new = self._put(namespace, series, vintage, "boundary", None, reference, None, state,
                                      {**evidence, "reason": reason}, principal_id)
                out["missing"] += [link["link_id"]] if new else []
        return out

    # ------------------------------------------------------------------ labour

    def link_labour(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                    labour_namespace: str = "global") -> dict[str, Any]:
        """Link each series to Labour series for NACE Rev.2 section I for the same place, by code or accepted match."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        held = table_exists(self.conn, "labour_series")
        if held and "knowledge:labour:read" not in scopes and "operator" not in scopes:
            raise TourismError("unauthorized", "knowledge:labour:read is required to read the linked provider")

        def labour_place(scheme: str, code: str) -> dict[str, Any] | None:
            if not table_exists(self.conn, "labour_identity_assertions"):
                return None
            from src.kb.labour_identity import LabourIdentity

            latest = LabourIdentity(self.conn, initialize=False).place_for_area(labour_namespace, scheme, code)
            return latest if latest and latest["state"] == "accepted" else None

        out: dict[str, list[str]] = {"linked": [], "missing": []}
        for series in self.store.find_series(namespace):
            vintage = self._current(namespace, series["series_id"])
            if vintage is None:
                continue
            reference = self._reference(series)
            if not held:
                link, new = self._put(namespace, series, vintage, "labour", None, reference, None, "provider_absent",
                                      {"reason": "the Labour provider is not composed (labour_series)"},
                                      principal_id)
                out["missing"] += [link["link_id"]] if new else []
                continue
            place = self._place(namespace, series)
            ours = self._periods(namespace, vintage["vintage_id"])
            targets = []
            for series_id, provider, native_key, area, sector, indicator in self.conn.execute(
                    "SELECT series_id, provider, native_key, area_json, sector_json, indicator_json FROM labour_series "
                    "WHERE namespace=? ORDER BY series_id", [labour_namespace]).fetchall():
                area, sector = load(area, {}), load(sector, None) or {}
                if (sector.get("scheme"), str(sector.get("version")), sector.get("code")) != (
                        LABOUR_SECTOR["scheme"], LABOUR_SECTOR["version"], LABOUR_SECTOR["code"]):
                    continue
                if (area.get("scheme"), str(area.get("code"))) == (reference["scheme"], reference["code"]):
                    evidence = {"basis": "shared-identifier",
                                "code": {"scheme": area["scheme"], "code": area["code"]},
                                "nuts_version": {"tourism": reference["nuts_version"],
                                                 "labour": "not stated by the labour series"}}
                else:
                    theirs = labour_place(area.get("scheme"), str(area.get("code")))
                    if place is None or theirs is None or theirs["target"]["place_id"] != place["place_id"]:
                        continue
                    evidence = {"basis": "accepted-match", "place_id": place["place_id"],
                                "tourism_assertion_id": place["assertion_id"],
                                "labour_assertion_id": theirs["assertion_id"]}
                labour_vintage = self.conn.execute(
                    "SELECT vintage_id, release_at_ms FROM labour_vintages WHERE namespace=? AND series_id=? "
                    "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1", [labour_namespace, series_id]).fetchone()
                if labour_vintage is None:
                    continue
                periods = [r[0] for r in self.conn.execute(
                    "SELECT period FROM labour_observations WHERE namespace=? AND vintage_id=?",
                    [labour_namespace, labour_vintage[0]]).fetchall()]
                targets.append(({"kind": "labour-series", "provider_id": "economics.labour",
                                 "namespace": labour_namespace, "id": series_id, "provider": provider,
                                 "native_key": native_key, "concept": load(indicator, {}).get("concept"),
                                 "sector": sector, "vintage_id": labour_vintage[0],
                                 "release_at": iso(labour_vintage[1])},
                                {**evidence, "shared_reference_years": sorted(_years(ours) & _years(periods)),
                                 "tourism_citation": self._cite(namespace, series, vintage),
                                 "labour_citation": {"provider_id": "economics.labour", "series_id": series_id,
                                                     "vintage_id": labour_vintage[0],
                                                     "as_of": iso(labour_vintage[1])}}))
            for target, evidence in targets:
                link, new = self._put(namespace, series, vintage, "labour", evidence["basis"], reference, target,
                                      "linked", evidence, principal_id)
                out["linked"] += [link["link_id"]] if new else []
            if not targets:
                link, new = self._put(namespace, series, vintage, "labour", None, reference, None, "target_not_held",
                                      {"reason": "no Labour series for NACE Rev.2 section I is held for this place"},
                                      principal_id)
                out["missing"] += [link["link_id"]] if new else []
        return out

    # ------------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, series_id, vintage_id, kind, basis, reference_json, target_json, state, evidence_json, "
            "created_by FROM tourism_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise TourismError("not_found", "no such link")
        return {"contract": CONTRACT, "link_id": row[0], "series_id": row[1], "vintage_id": row[2], "kind": row[3],
                "basis": row[4], "reference": load(row[5], {}), "target": load(row[6], None), "state": row[7],
                "evidence": load(row[8], {}), "created_by": row[9]}

    def links(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None, kind: str | None = None,
              state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "tourism_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM tourism_links WHERE namespace=? AND (? IS NULL OR series_id=?) AND (? IS NULL OR "
            "kind=?) AND (? IS NULL OR state=?) ORDER BY series_id, kind, link_id",
            [namespace, series_id, series_id, kind, kind, state, state]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


__all__ = ["BASES", "CONTRACT", "KINDS", "LABOUR_SECTOR", "NO_DERIVATION", "STATES", "TourismLinks",
           "boundary_collection"]
