"""Income series linked to Demographics, Labour and methodology documents by citation and accepted matches (IP07).

Delivery issue #2619. Three kinds of link, each recording its **basis** and pinning **specific revisions** on both
sides (the income series vintage in force when the link was made, and the target's vintage or document):

* ``methodology`` - every reference a source document states (PIP methodology handbook, Eurostat ESMS pages, the
  OECD IDD terms of reference, CELEX acts) is kept as published and resolved only by its exact URL to an acquired
  document (the shared ``documents`` table); basis ``citation``. A reference that resolves to nothing stays an
  ``unresolved`` citation with the published string.
* ``denominator`` - an Economics Demographics series (``economics.demographics``, ``demographic_series``) for the
  same place: by the same published geography code (basis ``shared-identifier``) or through an accepted IP06 place
  match whose place carries the code (basis ``accepted-match``, citing the assertion).
* ``labour`` - an Economics Labour series (``economics.labour``, ``labour_series``) for the same place, on the same
  bases.

Every link lists the reference years the two pinned vintages share; no value is combined, no rate or share is
computed (no derived indicators). When the Demographics or Labour provider is not installed the link is recorded as
``provider_absent``; a named target that is not held is ``target_not_held`` - reported, never dropped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.income_distribution_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    IncomeError,
    authorize,
    canonical,
    digest,
    iso,
    load,
    table_exists,
)
from src.kb.income_distribution_store import IncomeDistributionStore

CONTRACT = "noesis-income-link-v1"
KINDS = ("methodology", "denominator", "labour")
BASES = ("citation", "shared-identifier", "accepted-match")
NO_DERIVATION = "A link records what is related and on which basis; no value is combined and no indicator derived."
# The Geospatial source-identifier keys under which a place carries each area scheme's code.
# Population denominators by their published series code (Eurostat population on 1 January; World Bank total).
DENOMINATOR_CODES = ("demo_pjan", "SP.POP.TOTL")
SCHEME_KEYS = {"iso3166-1-alpha3": "iso3166-1-alpha3", "eurostat-geo": "nuts", "wb-region": "wb-region"}
_DDL = """
CREATE TABLE IF NOT EXISTS income_links (
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


class IncomeLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = IncomeDistributionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _current(self, namespace: str, series_id: str) -> dict[str, Any] | None:
        published = [v for v in self.store.vintage_rows(namespace, series_id) if v["status"] == "published"]
        return published[-1] if published else None

    def _put(self, namespace, series, vintage, kind, basis, reference, target, state, evidence, principal_id):
        link_id = "inc-link:" + digest([namespace, series["series_id"], vintage and vintage["vintage_id"], kind,
                                        reference, target, state])[:24]
        if self.conn.execute("SELECT 1 FROM income_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return self.link(namespace, link_id), False
        self.conn.execute("INSERT INTO income_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, series["series_id"], vintage and vintage["vintage_id"], kind, basis,
                           canonical(reference), None if target is None else canonical(target), state,
                           canonical({**evidence, "note": NO_DERIVATION}), principal_id, self.now()])
        return self.link(namespace, link_id), True

    # ------------------------------------------------------------------ methodology documents

    def link_methodology(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Resolve each stated methodology reference by its exact URL; the rest stay published citations."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        out = {"linked": [], "unresolved": []}
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
                    if new:
                        out["linked"].append(link["link_id"])
                else:
                    reason = ("several documents carry this URL" if rows else
                              "the cited document is not held; kept as the published citation" if url else
                              "the reference has no resolvable URL; kept as the published citation")
                    link, new = self._put(namespace, series, vintage, "methodology", "citation", reference, None,
                                          "unresolved", {"stated_in": vintage["definition_id"], "reason": reason},
                                          principal_id)
                    if new:
                        out["unresolved"].append(link["link_id"])
        return out

    # ------------------------------------------------------------------ demographics and labour

    def _codes(self, namespace: str, series: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        """(scheme, code, basis evidence): the series' own published code, then codes of its accepted place."""
        from src.kb.income_distribution_identity import IncomeIdentity

        area = series["area"]
        codes = [(area["scheme"], str(area["code"]), {"basis": "shared-identifier"})]
        accepted = IncomeIdentity(self.conn, initialize=False).place_for_area(namespace, area["scheme"], area["code"]) \
            if table_exists(self.conn, "income_identity_assertions") else None
        if accepted and table_exists(self.conn, "geospatial_place_revisions"):
            row = self.conn.execute(
                "SELECT r.source_ids_json FROM geospatial_place_current c JOIN geospatial_place_revisions r ON "
                "r.revision_id=c.revision_id WHERE c.place_id=?", [accepted["place_id"]]).fetchone()
            for key, value in sorted(json.loads(row[0] or "{}").items()) if row else []:
                for scheme, wanted in {**SCHEME_KEYS, "eurostat-geo-iso2": "iso3166-1-alpha2"}.items():
                    if key == wanted:
                        codes.append(("eurostat-geo" if scheme == "eurostat-geo-iso2" else scheme, str(value),
                                      {"basis": "accepted-match", "assertion_id": accepted["assertion_id"],
                                       "place_id": accepted["place_id"]}))
        seen, unique = set(), []
        for scheme, code, basis in codes:
            if (scheme, code) not in seen:
                seen.add((scheme, code))
                unique.append((scheme, code, basis))
        return unique

    def _periods(self, namespace: str, vintage_id: str) -> set[str]:
        return {o["period"] for o in self.store.observations(namespace, vintage_id) if o["status"] == "reported"}

    def _link_targets(self, namespace, kind, *, provider_table, finder, principal_id, scopes, required_scope):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        scopes = set(scopes)
        out = {"linked": [], "missing": []}
        held = table_exists(self.conn, provider_table)
        if held and required_scope not in scopes and "operator" not in scopes:
            raise IncomeError("unauthorized", f"{required_scope} is required to read the linked provider")
        for series in self.store.find_series(namespace):
            vintage = self._current(namespace, series["series_id"])
            if vintage is None:
                continue
            if not held:
                link, new = self._put(namespace, series, vintage, kind, None, {"provider_table": provider_table},
                                      None, "provider_absent",
                                      {"reason": f"the {kind} provider is not installed ({provider_table})"},
                                      principal_id)
                if new:
                    out["missing"].append(link["link_id"])
                continue
            found = False
            for scheme, code, basis in self._codes(namespace, series):
                for target in finder(scheme, code):
                    found = True
                    shared = sorted(self._periods(namespace, vintage["vintage_id"]) & set(target.pop("periods")))
                    link, new = self._put(namespace, series, vintage, kind, basis["basis"],
                                          {"scheme": scheme, "code": code}, target, "linked",
                                          {**basis, "shared_reference_years": shared}, principal_id)
                    if new:
                        out["linked"].append(link["link_id"])
            if not found:
                link, new = self._put(namespace, series, vintage, kind, None,
                                      {"scheme": series["area"]["scheme"], "code": series["area"]["code"]}, None,
                                      "target_not_held", {"reason": f"no {kind} series is held for this place"},
                                      principal_id)
                if new:
                    out["missing"].append(link["link_id"])
        return out

    def link_demographics(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                          demographic_namespace: str = "global",
                          series_codes: Iterable[str] = DENOMINATOR_CODES) -> dict[str, Any]:
        """Link each series to the population denominator series held for its place (pinned latest vintage)."""
        codes = sorted(set(series_codes))

        def finder(scheme: str, code: str) -> list[dict[str, Any]]:
            rows = self.conn.execute(
                "SELECT series_id, provider, series_code, indicator FROM demographic_series WHERE namespace=? AND "
                f"geography_code=? AND series_code IN ({','.join('?' for _ in codes)}) ORDER BY series_id",
                [demographic_namespace, code, *codes]).fetchall()
            out = []
            for series_id, provider, series_code, name in rows:
                vintage = self.conn.execute(
                    "SELECT vintage_id, release_at_ms FROM demographic_vintages WHERE namespace=? AND series_id=? "
                    "ORDER BY sequence DESC LIMIT 1", [demographic_namespace, series_id]).fetchone()
                if vintage is None:
                    continue
                periods = [r[0] for r in self.conn.execute(
                    "SELECT period FROM demographic_observations WHERE namespace=? AND vintage_id=?",
                    [demographic_namespace, vintage[0]]).fetchall()]
                out.append({"kind": "demographic-series", "provider_id": "economics.demographics",
                            "namespace": demographic_namespace, "id": series_id, "provider": provider,
                            "series_code": series_code, "indicator": name, "vintage_id": vintage[0],
                            "release_at": iso(vintage[1]), "geography_code": code, "periods": periods})
            return out

        return self._link_targets(namespace, "denominator", provider_table="demographic_series", finder=finder,
                                  principal_id=principal_id, scopes=scopes,
                                  required_scope="knowledge:demographics:read")

    def link_labour(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                    labour_namespace: str = "global") -> dict[str, Any]:
        """Link each series to the Labour series held for the same place (pinned latest vintage)."""

        def finder(scheme: str, code: str) -> list[dict[str, Any]]:
            out = []
            for series_id, provider, native_key, area, indicator in self.conn.execute(
                    "SELECT series_id, provider, native_key, area_json, indicator_json FROM labour_series WHERE "
                    "namespace=? ORDER BY series_id", [labour_namespace]).fetchall():
                area = load(area, {})
                if area.get("scheme") != scheme or str(area.get("code")) != code:
                    continue
                vintage = self.conn.execute(
                    "SELECT vintage_id, release_at_ms FROM labour_vintages WHERE namespace=? AND series_id=? "
                    "ORDER BY sequence DESC LIMIT 1", [labour_namespace, series_id]).fetchone()
                if vintage is None:
                    continue
                periods = [str(r[0])[:4] for r in self.conn.execute(
                    "SELECT period FROM labour_observations WHERE namespace=? AND vintage_id=?",
                    [labour_namespace, vintage[0]]).fetchall()]
                out.append({"kind": "labour-series", "provider_id": "economics.labour", "namespace": labour_namespace,
                            "id": series_id, "provider": provider, "native_key": native_key,
                            "concept": load(indicator, {}).get("concept"), "vintage_id": vintage[0],
                            "release_at": iso(vintage[1]), "periods": sorted(set(periods))})
            return out

        return self._link_targets(namespace, "labour", provider_table="labour_series", finder=finder,
                                  principal_id=principal_id, scopes=scopes, required_scope="knowledge:labour:read")

    # ------------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, series_id, vintage_id, kind, basis, reference_json, target_json, state, evidence_json, "
            "created_by FROM income_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise IncomeError("not_found", "no such link")
        return {"contract": CONTRACT, "link_id": row[0], "series_id": row[1], "vintage_id": row[2], "kind": row[3],
                "basis": row[4], "reference": load(row[5], {}), "target": load(row[6], None), "state": row[7],
                "evidence": load(row[8], {}), "created_by": row[9]}

    def links(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None,
              kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "income_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM income_links WHERE namespace=? AND (? IS NULL OR series_id=?) AND (? IS NULL OR "
            "kind=?) ORDER BY series_id, kind, link_id", [namespace, series_id, series_id, kind, kind]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


__all__ = ["BASES", "CONTRACT", "KINDS", "NO_DERIVATION", "IncomeLinks"]
