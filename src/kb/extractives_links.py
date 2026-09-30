"""Extractives records linked to other packs by citation, shared identifier or accepted match only (#2653, EX07).

Every link records its **basis** - ``explicit-citation`` (quoted text and locator), ``shared-identifier`` (a code
both records publish) or ``accepted-match`` (an EX06 match a reviewer accepted, named by id) - and points at
**specific record revisions** on both sides where the target store has them. A requested link whose target store
is not held is recorded as ``provider_absent`` and one whose target is not found as ``target_not_found`` (or
``no_target_on_record`` for an automatic join); missing providers and targets are reported, never dropped.

* **Public finance** - an EITI payment or revenue stream to a public-finance budget line
  (:class:`src.kb.public_finance.PublicFinanceStore`) by explicit citation only (e.g. the budget reference the
  report states for a revenue stream).
* **Trade flows** - a commodity series to trade-flow series (:class:`src.kb.trade_flows.TradeFlowStore`) whose
  product code lies within an HS heading the commodity maps to through an **accepted** published-concordance match,
  for the same country: the ISO alpha-2 code both publish (Eurostat GEO reporters, except the divergent EL/GR and
  UK/GB) or the M49 code the operator declared for the country (Comtrade reporters).
* **Energy** - a hydrocarbon series whose source document publishes a SIEC product code to Energy balance series
  (:class:`src.kb.energy_store.EnergyStore`) of the same SIEC code and country (shared identifiers), or by explicit
  citation.
* **Infrastructure** - a project to an infrastructure asset through an accepted EX06 project match (shared
  identifier or published coordinates). No ownership of a project is inferred.

Values of linked records are never combined: units, statistics and currencies are listed side by side.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.extractives_records import (
    LINK_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    ExtractivesError,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    load,
    require_scope,
    table_exists,
)
from src.kb.extractives_store import ExtractivesStore

TRADE_READ = "knowledge:trade:read"
ENERGY_READ = "knowledge:energy:read"
PUBLIC_FINANCE_READ = "knowledge:economic:public-finance:read"
INFRASTRUCTURE_READ = "knowledge:infrastructure:read"
BASES = ("explicit-citation", "shared-identifier", "accepted-match")
STATUSES = ("linked", "provider_absent", "target_not_found")
DIVERGENT_COUNTRY_CODES = frozenset({"GR", "EL", "GB", "UK"})
POLICY = ("links rest on an explicit citation, a shared published identifier or an accepted match only; linked "
          "values are listed side by side and never combined, converted or reconciled; no project ownership is "
          "inferred")
_DDL = """
CREATE TABLE IF NOT EXISTS extractives_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  subject_revision_id TEXT, target_kind TEXT NOT NULL, target_namespace TEXT, target_id TEXT NOT NULL,
  target_revision_id TEXT, basis TEXT NOT NULL, status TEXT NOT NULL, match_id TEXT, shared_json TEXT,
  citation_json TEXT, side_by_side_json TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


class ExtractivesLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.extractives_identity import ExtractivesIdentity

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ExtractivesStore(conn, initialize=initialize, now=self.now)
        self.identity = ExtractivesIdentity(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _record(self, namespace, *, subject_kind, subject_id, subject_revision_id, target_kind, target_namespace,
                target_id, target_revision_id, basis, status, principal_id, match_id=None, shared=None,
                citation=None, side_by_side=None) -> dict[str, Any]:
        if basis not in BASES or status not in STATUSES:
            raise ExtractivesError("invalid_link", "unknown link basis or status")
        link_id = "extractives-link:" + digest([namespace, subject_kind, subject_id, subject_revision_id, target_kind,
                                                target_namespace, target_id, basis])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO extractives_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING link_id",
            [namespace, link_id, subject_kind, subject_id, subject_revision_id, target_kind, target_namespace,
             target_id, target_revision_id, basis, status, match_id, canonical(shared) if shared else None,
             canonical(citation) if citation else None, canonical(side_by_side) if side_by_side else None,
             principal_id, self.now()]).fetchall()
        return {**self._link(namespace, link_id), "created": bool(inserted)}

    def _link(self, namespace: str, link_id: str) -> dict[str, Any]:
        r = self.conn.execute(
            "SELECT link_id, subject_kind, subject_id, subject_revision_id, target_kind, target_namespace, target_id, "
            "target_revision_id, basis, status, match_id, shared_json, citation_json, side_by_side_json, created_by, "
            "created_at_ms FROM extractives_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        return {"contract": LINK_CONTRACT, "link_id": r[0],
                "subject": {"kind": r[1], "id": r[2], "revision_id": r[3]},
                "target": {"kind": r[4], "namespace": r[5], "id": r[6], "revision_id": r[7]}, "basis": r[8],
                "status": r[9], "match_id": r[10], "shared": load(r[11]), "citation": load(r[12]),
                "side_by_side": load(r[13]), "created_by": r[14], "created_at": iso_from_ms(r[15])}

    @staticmethod
    def _citation(citation: Mapping[str, Any] | None) -> dict[str, Any]:
        citation = dict(citation or {})
        if not str(citation.get("text") or "").strip() or not citation.get("locator"):
            raise ExtractivesError("invalid_citation", "an explicit citation quotes its text and gives a locator")
        return citation

    def _eiti_subject(self, namespace: str, record_key: str) -> dict[str, Any]:
        view = self.store.record(namespace, record_key)
        if view is None or view["record_type"] not in {"company_payment", "revenue_stream", "project"}:
            raise ExtractivesError("not_found", "no current EITI payment, revenue stream or project with that key")
        return view

    def _series_subject(self, namespace: str, series_id: str) -> tuple[dict[str, Any], str | None]:
        series = self.store.series(namespace, series_id)
        return series, series["current_vintage_id"]

    # -------------------------------------------------------------- public finance

    def link_public_finance(self, namespace: str, record_key: str, line_id: str, citation: Mapping[str, Any], *,
                            principal_id: str, scopes: Iterable[str],
                            public_finance_namespace: str | None = None) -> dict[str, Any]:
        """An EITI payment or revenue stream to a budget line, by explicit citation only."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        citation = self._citation(citation)
        subject = self._eiti_subject(namespace, record_key)
        target_ns = public_finance_namespace or namespace
        common = {"subject_kind": subject["record_type"], "subject_id": record_key,
                  "subject_revision_id": subject["revision_id"], "target_kind": "public-finance-line",
                  "target_namespace": target_ns, "target_id": line_id, "basis": "explicit-citation",
                  "principal_id": principal_id, "citation": citation}
        if not table_exists(self.conn, "public_finance_lines"):
            return self._record(namespace, **common, target_revision_id=None, status="provider_absent")
        require_scope(scopes, PUBLIC_FINANCE_READ)
        from src.kb.public_finance import PublicFinanceError, PublicFinanceStore

        try:
            line = PublicFinanceStore(self.conn, initialize=False).line(target_ns, line_id)
        except PublicFinanceError:
            return self._record(namespace, **common, target_revision_id=None, status="target_not_found")
        return self._record(namespace, **common, target_revision_id=line["first_release_id"], status="linked",
                            side_by_side={"extractives": {"amounts": {k: subject["record"].get(k) for k in (
                                "government_reported", "company_reported")}},
                                "public_finance": {"scheme": line["scheme"], "codes": line["codes"],
                                                   "side": line["side"]},
                                "combined": False, "note": "amounts listed side by side; never reconciled"})

    # -------------------------------------------------------------- trade

    def link_trade_flows(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                         trade_namespace: str | None = None) -> dict[str, Any]:
        """Commodity series to trade series within an accepted HS heading for the same published country code."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "trade_series"):
            return {"namespace": namespace, "status": "provider_absent", "linked": [], "unlinked": [],
                    "note": "no trade-flow records are held; nothing is joined", "policy": POLICY}
        require_scope(scopes, TRADE_READ)
        from src.kb.trade_flows import TradeFlowStore

        trade = TradeFlowStore(self.conn, initialize=False)
        trade_namespace = trade_namespace or namespace
        linked, unlinked = [], []
        for series in self.store.find_series(namespace):
            country = series["country"]
            headings = self.identity.accepted_hs_codes(namespace, series["commodity"]["code"])
            if not headings or country.get("aggregate"):
                unlinked.append({"series_id": series["series_id"], "status": "no_target_on_record",
                                 "reason": "no accepted HS match for the commodity" if not headings
                                 else "an aggregate row has no country code"})
                continue
            keys = []
            if country.get("iso2") and country["iso2"] not in DIVERGENT_COUNTRY_CODES:
                keys.append(("eurostat-geo", country["iso2"]))
            if country.get("m49"):
                keys.append(("m49", str(int(country["m49"]))))
            found = []
            for heading in headings:
                for scheme, code in keys:
                    for flow in trade.find_series(trade_namespace, reporter_codes=[code]):
                        if flow["reporter"].get("scheme") != scheme or \
                                not str(flow["product"]["code"]).startswith(heading["hs_code"]):
                            continue
                        vintages = trade.vintage_rows(trade_namespace, flow["series_id"])
                        link = self._record(
                            namespace, subject_kind="commodity-series", subject_id=series["series_id"],
                            subject_revision_id=series["current_vintage_id"], target_kind="trade-series",
                            target_namespace=trade_namespace, target_id=flow["series_id"],
                            target_revision_id=vintages[-1]["vintage_id"] if vintages else None,
                            basis="accepted-match", status="linked", principal_id=principal_id,
                            match_id=heading["match_id"],
                            shared={"country": {"scheme": scheme, "code": code,
                                                "extractives_basis": country.get("code_basis")},
                                    "hs_heading": heading["hs_code"], "product_code": flow["product"]["code"],
                                    "classification": flow["classification"]},
                            side_by_side={"extractives": {"statistic": series["statistic"], "unit": series["unit"]},
                                          "trade": {"flow": flow["flow"], "unit": flow["unit"],
                                                    "frequency": flow["frequency"]},
                                          "combined": False,
                                          "note": "quantities and trade values listed side by side; never combined"})
                        found.append(link["link_id"])
            (linked if found else unlinked).append({"series_id": series["series_id"], "links": found,
                                                    **({} if found else {"status": "no_target_on_record"})})
        return {"namespace": namespace, "status": "linked" if linked else "no_target_on_record",
                "linked": linked, "unlinked": unlinked, "policy": POLICY}

    # -------------------------------------------------------------- energy

    def link_energy(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                    energy_namespace: str = "energy") -> dict[str, Any]:
        """Hydrocarbon series to Energy balance series of the same published SIEC code and country code."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        hydrocarbons = [s for s in self.store.find_series(namespace) if s["commodity"].get("hydrocarbon")]
        if not table_exists(self.conn, "energy_series"):
            return {"namespace": namespace, "status": "provider_absent", "linked": [],
                    "unlinked": [{"series_id": s["series_id"], "status": "provider_absent"} for s in hydrocarbons],
                    "note": "no Energy records are held; nothing is joined", "policy": POLICY}
        require_scope(scopes, ENERGY_READ)
        from src.kb.energy_store import EnergyStore

        energy = EnergyStore(self.conn, initialize=False)
        linked, unlinked = [], []
        for series in hydrocarbons:
            siec, iso2 = series["commodity"].get("siec"), series["country"].get("iso2")
            found = []
            if siec and iso2 and iso2 not in DIVERGENT_COUNTRY_CODES:
                for target in energy.series(energy_namespace, scopes=scopes, record_type="energy_balance",
                                            subject_codes=[iso2]):
                    if target["facets"].get("siec") != siec or target["subject"]["scheme"] != "eurostat-geo":
                        continue
                    vintage = energy.select_vintage(energy_namespace, target["series_id"], scopes=scopes)
                    link = self._record(
                        namespace, subject_kind="commodity-series", subject_id=series["series_id"],
                        subject_revision_id=series["current_vintage_id"], target_kind="energy-series",
                        target_namespace=energy_namespace, target_id=target["series_id"],
                        target_revision_id=vintage["vintage_id"] if vintage else None, basis="shared-identifier",
                        status="linked", principal_id=principal_id,
                        shared={"siec": siec, "country": {"scheme": "iso2/eurostat-geo", "code": iso2}},
                        side_by_side={"extractives": {"statistic": series["statistic"], "unit": series["unit"]},
                                      "energy": {"nrg_bal": target["facets"].get("nrg_bal"), "unit": target["unit"]},
                                      "combined": False, "note": "listed side by side; never combined"})
                    found.append(link["link_id"])
            (linked if found else unlinked).append({"series_id": series["series_id"], "links": found,
                                                    **({} if found else {"status": "no_target_on_record",
                                                                         "siec": siec, "country": iso2})})
        return {"namespace": namespace, "status": "linked" if linked else "no_target_on_record", "linked": linked,
                "unlinked": unlinked, "policy": POLICY}

    def link_energy_by_citation(self, namespace: str, series_id: str, energy_series_id: str,
                                citation: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                                energy_namespace: str = "energy") -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        citation = self._citation(citation)
        series, vintage_id = self._series_subject(namespace, series_id)
        common = {"subject_kind": "commodity-series", "subject_id": series_id, "subject_revision_id": vintage_id,
                  "target_kind": "energy-series", "target_namespace": energy_namespace, "target_id": energy_series_id,
                  "basis": "explicit-citation", "principal_id": principal_id, "citation": citation}
        if not table_exists(self.conn, "energy_series"):
            return self._record(namespace, **common, target_revision_id=None, status="provider_absent")
        require_scope(scopes, ENERGY_READ)
        from src.kb.energy_store import EnergyStore

        energy = EnergyStore(self.conn, initialize=False)
        found = energy.series(energy_namespace, scopes=scopes, series_ids=[energy_series_id])
        if not found:
            return self._record(namespace, **common, target_revision_id=None, status="target_not_found")
        vintage = energy.select_vintage(energy_namespace, energy_series_id, scopes=scopes)
        return self._record(namespace, **common, target_revision_id=vintage["vintage_id"] if vintage else None,
                            status="linked",
                            side_by_side={"extractives": {"statistic": series["statistic"], "unit": series["unit"]},
                                          "energy": {"unit": found[0]["unit"], "facets": found[0]["facets"]},
                                          "combined": False, "note": "listed side by side; never combined"})

    # -------------------------------------------------------------- infrastructure

    def link_infrastructure(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Projects to infrastructure assets through accepted EX06 project matches only."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        projects = self.store.records(namespace, record_types=("project",))
        if not table_exists(self.conn, "infra_assets"):
            return {"namespace": namespace, "status": "provider_absent", "linked": [],
                    "unlinked": [{"record_key": p["record_key"], "status": "provider_absent"} for p in projects],
                    "policy": POLICY}
        linked, unlinked = [], []
        for view in projects:
            accepted = self.identity.matches(namespace, kind="project", subject_key=view["record_key"],
                                             state="accepted")
            found = []
            for match in accepted:
                link = self._record(
                    namespace, subject_kind="project", subject_id=view["record_key"],
                    subject_revision_id=view["revision_id"], target_kind="infrastructure-asset",
                    target_namespace=match["target"]["namespace"], target_id=match["target"]["id"],
                    target_revision_id=match["target"]["revision_id"], basis="accepted-match", status="linked",
                    principal_id=principal_id, match_id=match["match_id"],
                    shared={"method": match["method"], "evidence": match["evidence"]},
                    side_by_side={"note": "the project as reported and the asset as its publisher states it; no "
                                          "ownership of the project is inferred", "combined": False})
                found.append(link["link_id"])
            (linked if found else unlinked).append({"record_key": view["record_key"], "links": found,
                                                    **({} if found else {"status": "no_target_on_record"})})
        return {"namespace": namespace, "status": "linked" if linked else "no_target_on_record", "linked": linked,
                "unlinked": unlinked, "policy": POLICY}

    # -------------------------------------------------------------- reads

    def links(self, namespace: str, *, subject_id: str | None = None, target_kind: str | None = None,
              status: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "extractives_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM extractives_links WHERE namespace=? AND (? IS NULL OR subject_id=?) AND "
            "(? IS NULL OR target_kind=?) AND (? IS NULL OR status=?) ORDER BY subject_id, target_kind, target_id",
            [namespace, subject_id, subject_id, target_kind, target_kind, status, status]).fetchall()
        return [self._link(namespace, r[0]) for r in rows]


def read_links(conn: Any, namespace: str, scopes) -> ExtractivesLinks:
    authorize(namespace, scopes, READ_SCOPE)
    return ExtractivesLinks(conn, initialize=False)


__all__ = ["BASES", "POLICY", "STATUSES", "ExtractivesLinks", "read_links"]
