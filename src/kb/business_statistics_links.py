"""Business statistics series linked to Labour, Trade and methodology documents by citation or accepted match (IB07).

Track #2738. Three kinds of link, each recording its **basis** and pinning **specific revisions** on both sides (the
business series vintage in force when the link was made, and the target's latest vintage or document):

* ``labour`` - an Economics Labour series (``economics.labour``, ``labour_series``) for the same place: by the same
  published area code (basis ``shared-identifier``) or because a reviewer accepted both the business area and the
  labour area as the same Geospatial place (basis ``accepted-match``, citing both assertions). The classification
  relation of the two series (same code, an accepted IB06 candidate link, or none) is recorded as evidence only: CBP
  employment and BLS or Eurostat LFS employment are different series, linked by place and citation, never merged.
* ``trade`` - Economics Trade series (``economics.trade``, ``trade_series``) whose reporter is the same place: by the
  same published code (``eurostat-geo`` ``DE``) or by a code the accepted place carries (``m49`` ``276``). Product and
  activity classifications are not mapped here.
* ``methodology`` - every reference a source document states (Eurostat ESMS pages, the business-demography manual,
  CBP methodology, CELEX acts) is kept as published and resolved only by its exact URL to an acquired document (the
  shared ``documents`` table); basis ``citation``. A reference that resolves to nothing stays ``unresolved``.

Every link lists the reference years the two pinned vintages share; no value is combined and no rate, share or
per-establishment figure is computed. When the Labour or Trade provider is not installed the link is recorded as
``provider_absent``; no held target is ``target_not_held`` - reported, never dropped.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.business_statistics_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    BusinessError,
    authorize,
    canonical,
    digest,
    iso,
    load,
    table_exists,
)
from src.kb.business_statistics_store import BusinessStatisticsStore

CONTRACT = "noesis-business-link-v1"
KINDS = ("labour", "trade", "methodology")
BASES = ("citation", "shared-identifier", "accepted-match")
STATES = ("linked", "unresolved", "provider_absent", "target_not_held")
NO_DERIVATION = "A link records what is related and on which basis; no value is combined and no indicator derived."
# Place source-identifier keys and the area scheme each one's code is published under by the linked providers.
PLACE_SCHEMES = {"nuts": "eurostat-geo", "eurostat-geo": "eurostat-geo", "iso3166-1-alpha2": "eurostat-geo",
                 "iso3166-1-alpha3": "iso3166-1-alpha3", "m49": "m49", "us-fips-state": "us-fips-state"}
_DDL = """
CREATE TABLE IF NOT EXISTS business_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT, kind TEXT NOT NULL,
  basis TEXT, reference_json TEXT NOT NULL, target_json TEXT, state TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def _url(reference: Mapping[str, Any]) -> str | None:
    for value in (reference.get("url"), reference.get("identifier")):
        if isinstance(value, str) and value.startswith("https://"):
            return value
    return None


def _years(periods: Iterable[str]) -> set[str]:
    return {str(p)[:4] for p in periods}


class BusinessLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = BusinessStatisticsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _identity(self):
        from src.kb.business_statistics_identity import BusinessIdentity

        return BusinessIdentity(self.conn, initialize=False, now=self.now)

    def _current(self, namespace: str, series_id: str) -> dict[str, Any] | None:
        published = [v for v in self.store.vintage_rows(namespace, series_id) if v["status"] == "published"]
        return published[-1] if published else None

    def _put(self, namespace, series, vintage, kind, basis, reference, target, state, evidence, principal_id):
        link_id = "bs-link:" + digest([namespace, series["series_id"], vintage and vintage["vintage_id"], kind,
                                       reference, target, state])[:24]
        if self.conn.execute("SELECT 1 FROM business_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return self.link(namespace, link_id), False
        self.conn.execute("INSERT INTO business_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, series["series_id"], vintage and vintage["vintage_id"], kind, basis,
                           canonical(reference), None if target is None else canonical(target), state,
                           canonical({**evidence, "note": NO_DERIVATION}), principal_id, self.now()])
        return self.link(namespace, link_id), True

    def _periods(self, namespace: str, vintage_id: str) -> set[str]:
        return {o["period"] for o in self.store.observations(namespace, vintage_id) if o["status"] == "reported"}

    def _place(self, namespace: str, series: Mapping[str, Any]) -> dict[str, Any] | None:
        if not table_exists(self.conn, "business_identity_assertions"):
            return None
        return self._identity().place_for_area(namespace, series["area"]["scheme"], series["area"]["code"])

    def _place_codes(self, place_id: str) -> list[tuple[str, str]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        row = self.conn.execute(
            "SELECT r.source_ids_json FROM geospatial_place_current c JOIN geospatial_place_revisions r ON "
            "r.revision_id=c.revision_id WHERE c.place_id=?", [place_id]).fetchone()
        return sorted({(PLACE_SCHEMES[k], str(v)) for k, v in json.loads(row[0] or "{}").items()
                       if k in PLACE_SCHEMES}) if row else []

    def _link_targets(self, namespace: str, kind: str, *, provider_table: str, required_scope: str,
                      finder: Callable[[Mapping[str, Any], dict[str, Any] | None], list[dict[str, Any]]],
                      principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        held = table_exists(self.conn, provider_table)
        if held and required_scope not in scopes and "operator" not in scopes:
            raise BusinessError("unauthorized", f"{required_scope} is required to read the linked provider")
        out: dict[str, list[str]] = {"linked": [], "missing": []}
        for series in self.store.find_series(namespace):
            vintage = self._current(namespace, series["series_id"])
            if vintage is None:
                continue
            if not held:
                link, new = self._put(namespace, series, vintage, kind, None, {"provider_table": provider_table},
                                      None, "provider_absent",
                                      {"reason": f"the {kind} provider is not installed ({provider_table})"},
                                      principal_id)
                out["missing"] += [link["link_id"]] if new else []
                continue
            ours = self._periods(namespace, vintage["vintage_id"])
            targets = finder(series, self._place(namespace, series))
            for target in targets:
                evidence = target.pop("evidence")
                periods = target.pop("periods")
                shared = sorted(_years(ours) & _years(periods))
                link, new = self._put(namespace, series, vintage, kind, evidence["basis"],
                                      {"scheme": series["area"]["scheme"], "code": series["area"]["code"]}, target,
                                      "linked", {**evidence, "shared_reference_years": shared}, principal_id)
                out["linked"] += [link["link_id"]] if new else []
            if not targets:
                link, new = self._put(namespace, series, vintage, kind, None,
                                      {"scheme": series["area"]["scheme"], "code": series["area"]["code"]}, None,
                                      "target_not_held", {"reason": f"no {kind} series is held for this place"},
                                      principal_id)
                out["missing"] += [link["link_id"]] if new else []
        return out

    # ------------------------------------------------------------------ labour

    def link_labour(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                    labour_namespace: str = "global") -> dict[str, Any]:
        """Link each series to the Labour series for the same place (pinned latest vintage), by place and citation."""
        identity = self._identity() if table_exists(self.conn, "business_identity_assertions") else None

        def labour_place(scheme: str, code: str) -> dict[str, Any] | None:
            if not table_exists(self.conn, "labour_identity_assertions"):
                return None
            from src.kb.labour_identity import LabourIdentity

            latest = LabourIdentity(self.conn, initialize=False).place_for_area(labour_namespace, scheme, code)
            return latest if latest and latest["state"] == "accepted" else None

        def finder(series: Mapping[str, Any], place: dict[str, Any] | None) -> list[dict[str, Any]]:
            out = []
            for series_id, provider, native_key, area, sector, indicator in self.conn.execute(
                    "SELECT series_id, provider, native_key, area_json, sector_json, indicator_json FROM labour_series "
                    "WHERE namespace=? ORDER BY series_id", [labour_namespace]).fetchall():
                area, sector = load(area, {}), load(sector, None)
                if (area.get("scheme"), str(area.get("code"))) == (series["area"]["scheme"],
                                                                   str(series["area"]["code"])):
                    evidence = {"basis": "shared-identifier", "code": {"scheme": area["scheme"], "code": area["code"]}}
                else:
                    theirs = labour_place(area.get("scheme"), str(area.get("code")))
                    if place is None or theirs is None or theirs["target"]["place_id"] != place["place_id"]:
                        continue
                    evidence = {"basis": "accepted-match", "place_id": place["place_id"],
                                "business_assertion_id": place["assertion_id"],
                                "labour_assertion_id": theirs["assertion_id"]}
                vintage = self.conn.execute(
                    "SELECT vintage_id, release_at_ms FROM labour_vintages WHERE namespace=? AND series_id=? "
                    "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1", [labour_namespace, series_id]).fetchone()
                if vintage is None:
                    continue
                periods = [r[0] for r in self.conn.execute(
                    "SELECT period FROM labour_observations WHERE namespace=? AND vintage_id=?",
                    [labour_namespace, vintage[0]]).fetchall()]
                evidence["classification"] = self._classification_relation(namespace, identity, series, sector)
                out.append({"kind": "labour-series", "provider_id": "economics.labour",
                            "namespace": labour_namespace, "id": series_id, "provider": provider,
                            "native_key": native_key, "concept": load(indicator, {}).get("concept"),
                            "sector": sector, "vintage_id": vintage[0], "release_at": iso(vintage[1]),
                            "periods": periods, "evidence": evidence})
            return out

        return self._link_targets(namespace, "labour", provider_table="labour_series",
                                  required_scope="knowledge:labour:read", finder=finder, principal_id=principal_id,
                                  scopes=scopes)

    @staticmethod
    def _classification_relation(namespace, identity, series, sector) -> dict[str, Any]:
        ours = series["classification"]
        if not sector:
            return {"relation": "none", "statement": "the labour series states no sector; linked by place only"}
        same = (sector.get("scheme"), str(sector.get("version")), sector.get("code")) == (
            ours["scheme"], str(ours["version"]), ours["code"])
        if same:
            return {"relation": "same-code", "statement": f"both state {ours['scheme']} {ours['version']} "
                                                          f"{ours['code']}; still different series"}
        if identity is not None:
            for linked in identity.linked_codes(namespace, ours):
                if (linked["scheme"], linked["version"], linked["code"]) == (
                        sector.get("scheme"), str(sector.get("version")), sector.get("code")):
                    return {"relation": f"accepted-candidate-link ({linked['relation']})",
                            "assertion_id": linked["assertion_id"],
                            "statement": "a reviewed concordance candidate link; the series are never merged"}
        return {"relation": "different-classification",
                "statement": "different classifications without an accepted candidate link; linked by place only"}

    # ------------------------------------------------------------------ trade

    def link_trade(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                   trade_namespace: str = "global") -> dict[str, Any]:
        """Link each series to the Trade series reported by the same place, by place and citation only."""

        def finder(series: Mapping[str, Any], place: dict[str, Any] | None) -> list[dict[str, Any]]:
            codes = {(series["area"]["scheme"], str(series["area"]["code"])): {"basis": "shared-identifier"}}
            if place is not None:
                for code in self._place_codes(place["place_id"]):
                    codes.setdefault(code, {"basis": "accepted-match", "place_id": place["place_id"],
                                            "business_assertion_id": place["assertion_id"]})
            out = []
            for (scheme, code), basis in sorted(codes.items()):
                for series_id, provider, partner, flow, product, vintage_scheme, vintage_version in self.conn.execute(
                        "SELECT series_id, provider, partner_json, flow_direction, product_code, "
                        "classification_scheme, classification_vintage FROM trade_series WHERE namespace=? AND "
                        "reporter_scheme=? AND reporter_code=? ORDER BY series_id",
                        [trade_namespace, scheme, code]).fetchall():
                    vintage = self.conn.execute(
                        "SELECT vintage_id, release_at_ms FROM trade_vintages WHERE namespace=? AND series_id=? "
                        "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1", [trade_namespace, series_id]).fetchone()
                    if vintage is None:
                        continue
                    periods = [r[0] for r in self.conn.execute(
                        "SELECT period FROM trade_observations WHERE namespace=? AND vintage_id=?",
                        [trade_namespace, vintage[0]]).fetchall()]
                    out.append({"kind": "trade-series", "provider_id": "economics.trade",
                                "namespace": trade_namespace, "id": series_id, "provider": provider,
                                "reporter": {"scheme": scheme, "code": code}, "partner": load(partner, {}),
                                "flow": flow, "product": {"scheme": vintage_scheme, "vintage": vintage_version,
                                                          "code": product},
                                "vintage_id": vintage[0], "release_at": iso(vintage[1]), "periods": periods,
                                "evidence": {**basis, "code": {"scheme": scheme, "code": code},
                                             "classification": "product and activity classifications are not "
                                                               "mapped; linked by place and citation only"}})
            return out

        return self._link_targets(namespace, "trade", provider_table="trade_series",
                                  required_scope="knowledge:trade:read", finder=finder, principal_id=principal_id,
                                  scopes=scopes)

    # ------------------------------------------------------------------ methodology documents

    def link_methodology(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Resolve each stated reference by its exact URL; the rest stay published citations."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        out: dict[str, list[str]] = {"linked": [], "unresolved": []}
        held = table_exists(self.conn, "documents")
        for series in self.store.find_series(namespace):
            vintage = self._current(namespace, series["series_id"])
            if vintage is None:
                continue
            definition = self.store.definition(namespace, vintage["definition_id"]) or {"content": {}}
            for reference in definition["content"].get("references") or []:
                url = _url(reference)
                rows = self.conn.execute(
                    "SELECT document_id, title, url FROM documents WHERE url=? OR canonical_url=? ORDER BY document_id",
                    [url, url]).fetchall() if held and url else []
                if len(rows) == 1:
                    target = {"kind": "document", "id": rows[0][0], "title": rows[0][1], "url": rows[0][2]}
                    link, new = self._put(namespace, series, vintage, "methodology", "citation", reference, target,
                                          "linked", {"stated_in": vintage["definition_id"]}, principal_id)
                    out["linked"] += [link["link_id"]] if new else []
                else:
                    reason = ("several documents carry this URL" if rows else
                              "the cited document is not held; kept as the published citation" if url else
                              "the reference has no resolvable URL; kept as the published citation")
                    link, new = self._put(namespace, series, vintage, "methodology", "citation", reference, None,
                                          "unresolved", {"stated_in": vintage["definition_id"], "reason": reason},
                                          principal_id)
                    out["unresolved"] += [link["link_id"]] if new else []
        return out

    # ------------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, series_id, vintage_id, kind, basis, reference_json, target_json, state, evidence_json, "
            "created_by FROM business_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise BusinessError("not_found", "no such link")
        return {"contract": CONTRACT, "link_id": row[0], "series_id": row[1], "vintage_id": row[2], "kind": row[3],
                "basis": row[4], "reference": load(row[5], {}), "target": load(row[6], None), "state": row[7],
                "evidence": load(row[8], {}), "created_by": row[9]}

    def links(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None, kind: str | None = None,
              state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "business_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM business_links WHERE namespace=? AND (? IS NULL OR series_id=?) AND (? IS NULL OR "
            "kind=?) AND (? IS NULL OR state=?) ORDER BY series_id, kind, link_id",
            [namespace, series_id, series_id, kind, kind, state, state]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


__all__ = ["BASES", "CONTRACT", "KINDS", "NO_DERIVATION", "STATES", "BusinessLinks"]
