"""Social protection series linked to Demographics and Public finance by citation or accepted match (#2741, SS07).

Follows :mod:`src.kb.income_distribution_links`. Two kinds of link, each recording its **basis** and pinning
**specific record revisions** on both sides (the social protection vintage in force when the link was made, and the
target's vintage):

* ``denominator`` - the population denominator series in ``economics.demographics`` (``demographic_series``; Eurostat
  ``demo_pjan`` or the World Bank total population) for the same place;
* ``cofog`` - COFOG social-protection expenditure (``gov_10a_exp``, ``cofog99`` ``GF10``) in
  ``economics.public-finance`` (``public_finance_gfs_vintages``) for the same place. COFOG is general-government
  expenditure on an ESA 2010 basis: a third, distinct concept, linked by place and citation, never merged with or
  compared as the same thing as an ESSPROS or SOCX figure.

A link's basis is ``shared-identifier`` (the same published geography code) or ``accepted-match`` (an accepted SS06
place match whose place carries the code; the assertion is cited). Every link lists the reference years the two
pinned vintages share; **no ratio, share or per-capita figure is computed** from linked series. When the Demographics
or Public finance provider is not installed the link is ``provider_absent``; a place with no held target is
``target_not_held`` - reported, never dropped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.social_protection_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    SocialProtectionError,
    authorize,
    canonical,
    digest,
    iso,
    load,
    table_exists,
)
from src.kb.social_protection_store import SocialProtectionStore

CONTRACT = "noesis-social-protection-link-v1"
KINDS = ("denominator", "cofog")
BASES = ("shared-identifier", "accepted-match")
PROVIDERS = {"denominator": "economics.demographics", "cofog": "economics.public-finance"}
NO_DERIVATION = ("A link records what is related and on which basis; no value is combined and no ratio, share or "
                 "per-capita figure is computed.")
COFOG_NOTE = ("COFOG social protection (GF10) is general-government expenditure on an ESA 2010 basis, a concept "
              "distinct from ESSPROS and SOCX expenditure; shown beside them, never combined")
DENOMINATOR_CODES = ("demo_pjan", "SP.POP.TOTL")
COFOG_DATASET, COFOG_CODE = "gov_10a_exp", "GF10"
# The Geospatial source-identifier keys under which a place carries each area scheme's code.
SCHEME_KEYS = {"iso3166-1-alpha3": "iso3166-1-alpha3", "eurostat-geo": "iso3166-1-alpha2"}
_DDL = """
CREATE TABLE IF NOT EXISTS social_protection_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT, kind TEXT NOT NULL,
  basis TEXT, reference_json TEXT NOT NULL, target_json TEXT, state TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


class SocialProtectionLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = SocialProtectionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _current(self, namespace: str, series_id: str) -> dict[str, Any] | None:
        published = [v for v in self.store.vintage_rows(namespace, series_id) if v["status"] == "published"]
        return published[-1] if published else None

    def _put(self, namespace, series, vintage, kind, basis, reference, target, state, evidence, principal_id):
        link_id = "sp-link:" + digest([namespace, series["series_id"], vintage and vintage["vintage_id"], kind,
                                       reference, target, state])[:24]
        if self.conn.execute("SELECT 1 FROM social_protection_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return self.link(namespace, link_id), False
        self.conn.execute("INSERT INTO social_protection_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, series["series_id"], vintage and vintage["vintage_id"], kind, basis,
                           canonical(reference), None if target is None else canonical(target), state,
                           canonical({**evidence, "note": NO_DERIVATION}), principal_id, self.now()])
        return self.link(namespace, link_id), True

    def _codes(self, namespace: str, series: Mapping[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
        """(scheme, code, basis evidence): the series' own published code, then the codes of its accepted place."""
        from src.kb.social_protection_identity import (
            EUROSTAT_ISO2,
            SocialProtectionIdentity,
        )

        area = series["area"]
        codes = [(area["scheme"], str(area["code"]), {"basis": "shared-identifier"})]
        accepted = SocialProtectionIdentity(self.conn, initialize=False).place_for_area(
            namespace, area["scheme"], area["code"]) if table_exists(
            self.conn, "social_protection_identity_assertions") else None
        if accepted and table_exists(self.conn, "geospatial_place_revisions"):
            row = self.conn.execute(
                "SELECT r.source_ids_json FROM geospatial_place_current c JOIN geospatial_place_revisions r ON "
                "r.revision_id=c.revision_id WHERE c.place_id=?", [accepted["place_id"]]).fetchone()
            reverse = {v: k for k, v in EUROSTAT_ISO2.items()}
            for key, value in sorted(json.loads(row[0] or "{}").items()) if row else []:
                for scheme, wanted in SCHEME_KEYS.items():
                    if key == wanted:
                        code = reverse.get(str(value), str(value)) if scheme == "eurostat-geo" else str(value)
                        codes.append((scheme, code, {"basis": "accepted-match", "assertion_id": accepted["assertion_id"],
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
            raise SocialProtectionError("unauthorized", f"{required_scope} is required to read the linked provider")
        for series in self.store.find_series(namespace):
            vintage = self._current(namespace, series["series_id"])
            if vintage is None:
                continue
            if not held:
                link, new = self._put(namespace, series, vintage, kind, None, {"provider_table": provider_table},
                                      None, "provider_absent",
                                      {"reason": f"{PROVIDERS[kind]} is not composed ({provider_table} not held)"},
                                      principal_id)
                if new:
                    out["missing"].append(link["link_id"])
                continue
            found = False
            for scheme, code, basis in self._codes(namespace, series):
                for target in finder(scheme, code):
                    found = True
                    shared = sorted(self._periods(namespace, vintage["vintage_id"]) & set(target.pop("periods")))
                    extra = {"concept_note": COFOG_NOTE} if kind == "cofog" else {}
                    link, new = self._put(namespace, series, vintage, kind, basis["basis"],
                                          {"scheme": scheme, "code": code}, target, "linked",
                                          {**basis, "shared_reference_years": shared, **extra}, principal_id)
                    if new:
                        out["linked"].append(link["link_id"])
            if not found:
                link, new = self._put(namespace, series, vintage, kind, None,
                                      {"scheme": series["area"]["scheme"], "code": series["area"]["code"]}, None,
                                      "target_not_held", {"reason": f"no {PROVIDERS[kind]} series is held for this "
                                                                    "place"}, principal_id)
                if new:
                    out["missing"].append(link["link_id"])
        return out

    def link_demographics(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                          demographic_namespace: str = "global",
                          series_codes: Iterable[str] = DENOMINATOR_CODES) -> dict[str, Any]:
        """Link each series to the population denominator held for its place (pinned latest vintage)."""
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
                periods = [str(r[0])[:4] for r in self.conn.execute(
                    "SELECT period FROM demographic_observations WHERE namespace=? AND vintage_id=?",
                    [demographic_namespace, vintage[0]]).fetchall()]
                out.append({"kind": "demographic-series", "provider_id": PROVIDERS["denominator"],
                            "namespace": demographic_namespace, "id": series_id, "provider": provider,
                            "series_code": series_code, "indicator": name, "vintage_id": vintage[0],
                            "release_at": iso(vintage[1]), "geography_code": code, "periods": sorted(set(periods))})
            return out

        return self._link_targets(namespace, "denominator", provider_table="demographic_series", finder=finder,
                                  principal_id=principal_id, scopes=scopes,
                                  required_scope="knowledge:demographics:read")

    def link_public_finance(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                            public_finance_namespace: str = "global") -> dict[str, Any]:
        """Link each series to COFOG social-protection expenditure (gov_10a_exp, GF10) held for its place."""

        def finder(scheme: str, code: str) -> list[dict[str, Any]]:
            if scheme != "eurostat-geo":
                return []
            rows = self.conn.execute(
                "SELECT series_id, as_of, vintage_id, snapshot_id, dimensions_json, published_on FROM "
                "public_finance_gfs_vintages WHERE namespace=? AND dataset=? ORDER BY series_id, as_of",
                [public_finance_namespace, COFOG_DATASET]).fetchall()
            latest: dict[str, tuple] = {}
            for row in rows:
                dims = load(row[4], {})
                if dims.get("cofog99") == COFOG_CODE and str(dims.get("geo")) == code:
                    latest[row[0]] = row
            out = []
            for series_id, as_of, vintage_id, snapshot_id, dims, published in latest.values():
                periods = [r[0] for r in self.conn.execute(
                    "SELECT period FROM dataset_observations WHERE series_id=? AND as_of=?",
                    [series_id, as_of]).fetchall()] if table_exists(self.conn, "dataset_observations") else []
                out.append({"kind": "cofog-series", "provider_id": PROVIDERS["cofog"],
                            "namespace": public_finance_namespace, "id": series_id, "dataset": COFOG_DATASET,
                            "cofog": {"scheme": "cofog", "code": COFOG_CODE, "label": "Social protection"},
                            "dimensions": load(dims, {}), "vintage_id": vintage_id, "snapshot_id": snapshot_id,
                            "published_on": published, "accounting_basis": "esa2010",
                            "periods": sorted({str(p)[:4] for p in periods})})
            return out

        return self._link_targets(namespace, "cofog", provider_table="public_finance_gfs_vintages", finder=finder,
                                  principal_id=principal_id, scopes=scopes,
                                  required_scope="knowledge:public-finance:read")

    # ------------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, series_id, vintage_id, kind, basis, reference_json, target_json, state, evidence_json, "
            "created_by FROM social_protection_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise SocialProtectionError("not_found", "no such link")
        return {"contract": CONTRACT, "link_id": row[0], "series_id": row[1], "vintage_id": row[2], "kind": row[3],
                "provider_id": PROVIDERS[row[3]], "basis": row[4], "reference": load(row[5], {}),
                "target": load(row[6], None), "state": row[7], "evidence": load(row[8], {}), "created_by": row[9]}

    def links(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None,
              kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "social_protection_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM social_protection_links WHERE namespace=? AND (? IS NULL OR series_id=?) AND (? IS NULL "
            "OR kind=?) ORDER BY series_id, kind, link_id", [namespace, series_id, series_id, kind, kind]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


__all__ = ["BASES", "COFOG_NOTE", "CONTRACT", "KINDS", "NO_DERIVATION", "SocialProtectionLinks"]
