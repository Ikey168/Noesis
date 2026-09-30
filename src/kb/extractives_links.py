"""Extractives records linked to other packs by citation, shared identifier or accepted match only (#2653, EX07).

Every link records its **basis** - ``citation`` (a citing source and locator), ``shared-identifier`` (an
identifier both records publish) or ``accepted-match`` (an accepted EX06 identity decision) - and points at a
specific revision on both sides:

* **public finance**: a payment line whose report states a budget reference (``scheme`` and ``code``, the code
  written as the public-finance line key writes it, e.g. ``0802/12101``) links to the Economics public-finance
  budget line with exactly that scheme and key (``shared-identifier``); the line's first release is the target
  revision;
* **trade flows**: a commodity with an accepted HS mapping (or an HS code the EITI summary states) links to the
  Economics trade series of that HS code (a series of a more detailed code under the stated heading is labelled
  ``narrower``); the series' latest vintage is the target revision;
* **energy**: a hydrocarbon series links to an Energy series only when its source document names that series
  (provider, dataset and native id) - ``shared-identifier``; nothing is linked by commodity name;
* **infrastructure**: a reported project links to the infrastructure asset of its accepted EX06 match
  (``accepted-match``) at the matched asset revision; project ownership is never inferred;
* **explicit citation**: any other record can be linked with a citing source and a locator.

A missing provider store is reported as ``provider_absent`` and a missing target as ``target_not_held``; both are
kept as unresolved links, never dropped. Nothing is linked by name similarity, co-location or correlation.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.extractives_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    ExtractivesError,
    authorize,
    canonical,
    digest,
    table_exists,
)
from src.kb.extractives_store import ExtractivesStore

CONTRACT = "noesis-extractives-link-v1"
BASES = ("citation", "shared-identifier", "accepted-match")
REFUSED_BASES = ("name similarity", "similar name", "co-location", "colocation", "correlation")
NO_INFERENCE = "A link records a published identifier, a citation or an accepted match; nothing is inferred."
_DDL = """
CREATE TABLE IF NOT EXISTS ex_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
  source_revision TEXT NOT NULL, relation TEXT NOT NULL, target_owner TEXT NOT NULL, target_json TEXT,
  basis TEXT NOT NULL, detail_json TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, history_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


class ExtractivesLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = ExtractivesStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _put(self, namespace, source_kind, source_id, source_revision, relation, owner, target, basis, detail,
             state, reason, principal_id):
        if basis not in BASES:
            raise ExtractivesError("invalid_link", f"a link basis is one of {BASES}")
        link_id = "ex-link:" + digest([namespace, source_kind, source_id, source_revision, relation, owner, target,
                                       state])[:24]
        if self.conn.execute("SELECT 1 FROM ex_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone():
            return link_id, False
        now = self.now()
        self.conn.execute(
            "INSERT INTO ex_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, link_id, source_kind, source_id, source_revision, relation, owner,
             None if target is None else canonical(target), basis, canonical({**detail, "note": NO_INFERENCE}),
             state, reason, canonical([{"state": state, "by": principal_id, "at_ms": now}]), principal_id, now])
        return link_id, True

    @staticmethod
    def _collect(result: dict[str, list[str]], link_id: str, new: bool, state: str) -> None:
        if new:
            result["linked" if state == "linked" else "unresolved"].append(link_id)

    def _current_reports(self, namespace: str) -> list[dict[str, Any]]:
        return [self.store.report_as_of(namespace, key)[0] for key in self.store.report_keys(namespace)]

    # ------------------------------------------------------------------ public finance

    def link_public_finance(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                            finance_namespace: str = "global") -> dict[str, Any]:
        """Payment lines whose report states a budget reference -> the budget line with that scheme and code."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        held = table_exists(self.conn, "public_finance_lines")
        result: dict[str, list[str]] = {"linked": [], "unresolved": []}
        for report in self._current_reports(namespace):
            for line in self.store.payments(namespace, report["report_id"]):
                ref = line.get("budget_reference")
                if not ref:
                    continue
                rows = self.conn.execute(
                    "SELECT line_id, line_key, first_release_id, provider FROM public_finance_lines WHERE "
                    "namespace=? AND scheme=? AND line_key=? ORDER BY line_id",
                    [finance_namespace, str(ref.get("scheme")), f"{ref.get('scheme')}:{ref.get('code')}"]
                ).fetchall() if held else []
                detail = {"stated_reference": ref, "stated_in": report["report_id"], "line": line["line_key"]}
                if len(rows) == 1:
                    target = {"kind": "budget-line", "namespace": finance_namespace, "id": rows[0][0],
                              "revision": rows[0][2], "provider": rows[0][3]}
                    state, reason = "linked", None
                else:
                    target, state = None, "unresolved"
                    reason = ("provider_absent: the Economics public-finance store is not held" if not held else
                              "several budget lines carry this code" if rows else
                              "target_not_held: no budget line carries this scheme and code")
                link_id, new = self._put(namespace, "payment", f"{report['report_id']}#{line['line_key']}",
                                         report["report_id"], "revenue_recorded_as", "economics.public-finance",
                                         target, "shared-identifier", detail, state, reason, principal_id)
                self._collect(result, link_id, new, state)
        return result

    # ------------------------------------------------------------------ trade

    def _hs_codes(self, namespace: str) -> list[dict[str, Any]]:
        from src.kb.extractives_identity import ExtractivesIdentity

        identity = ExtractivesIdentity(self.conn, initialize=False, now=self.now)
        out = []
        for assertion in identity.assertions(namespace, scopes={"operator"}, kind="commodity", state="accepted"):
            for mapped in assertion["target"]["codes"]:
                out.append({"commodity_key": assertion["subject"]["commodity_key"], "code": mapped["code"],
                            "relation": mapped["relation"], "assertion_id": assertion["assertion_id"],
                            "method": assertion["method"]})
        return out

    def link_trade(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                   trade_namespace: str = "global") -> dict[str, Any]:
        """Accepted commodity-to-HS mappings -> Economics trade series of that HS code (or narrower codes)."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        held = table_exists(self.conn, "trade_series")
        result: dict[str, list[str]] = {"linked": [], "unresolved": []}
        for code in self._hs_codes(namespace):
            rows = self.conn.execute(
                "SELECT series_id, product_code, classification_vintage FROM trade_series WHERE namespace=? AND "
                "classification_scheme='HS' AND (product_code=? OR product_code LIKE ?) ORDER BY series_id",
                [trade_namespace, code["code"], code["code"] + "%"]).fetchall() if held else []
            detail = {"hs_code": code["code"], "mapping": {k: code[k] for k in ("assertion_id", "method",
                                                                                 "relation")}}
            if not rows:
                reason = ("provider_absent: the Economics trade store is not held" if not held else
                          "target_not_held: no trade series of this HS code is held")
                link_id, new = self._put(namespace, "commodity", code["commodity_key"], code["assertion_id"],
                                         "traded_as", "economics.trade", None, "accepted-match", detail,
                                         "unresolved", reason, principal_id)
                self._collect(result, link_id, new, "unresolved")
                continue
            for series_id, product, vintage_scheme in rows:
                vintage = self.conn.execute(
                    "SELECT vintage_id FROM trade_vintages WHERE namespace=? AND series_id=? ORDER BY release_at_ms "
                    "DESC, sequence DESC LIMIT 1", [trade_namespace, series_id]).fetchone() \
                    if table_exists(self.conn, "trade_vintages") else None
                target = {"kind": "trade-series", "namespace": trade_namespace, "id": series_id,
                          "revision": vintage[0] if vintage else None, "product_code": product,
                          "classification_vintage": vintage_scheme,
                          "code_relation": "exact" if product == code["code"] else "narrower"}
                link_id, new = self._put(namespace, "commodity", code["commodity_key"], code["assertion_id"],
                                         "traded_as", "economics.trade", target, "accepted-match", detail, "linked",
                                         None, principal_id)
                self._collect(result, link_id, new, "linked")
        return result

    # ------------------------------------------------------------------ energy

    def link_energy(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                    energy_namespace: str = "global") -> dict[str, Any]:
        """Hydrocarbon series -> the Energy series their source document names (provider, dataset, native id)."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        held = table_exists(self.conn, "energy_series")
        result: dict[str, list[str]] = {"linked": [], "unresolved": []}
        for series in self.store.find_series(namespace):
            if not series["commodity"].get("hydrocarbon"):
                continue
            named = [r for r in series["references"] if r.get("kind") == "energy-series"]
            if not named:
                link_id, new = self._put(namespace, "series", series["series_id"], series["current_vintage_id"],
                                         "balance_series", "energy.core", None, "shared-identifier",
                                         {"reason_detail": "the source names no Energy series"}, "unresolved",
                                         "no published identifier names an Energy series; nothing is matched by "
                                         "commodity name", principal_id)
                self._collect(result, link_id, new, "unresolved")
                continue
            for ref in named:
                row = self.conn.execute(
                    "SELECT series_id FROM energy_series WHERE namespace=? AND provider=? AND dataset=? AND "
                    "native_id=?", [energy_namespace, str(ref.get("provider")), str(ref.get("dataset")),
                                    str(ref.get("native_id"))]).fetchone() if held else None
                if row is None:
                    reason = ("provider_absent: the Energy store is not held" if not held else
                              "target_not_held: the named Energy series is not held")
                    link_id, new = self._put(namespace, "series", series["series_id"], series["current_vintage_id"],
                                             "balance_series", "energy.core", None, "shared-identifier",
                                             {"named": ref}, "unresolved", reason, principal_id)
                    self._collect(result, link_id, new, "unresolved")
                    continue
                vintage = self.conn.execute(
                    "SELECT vintage_id FROM energy_vintages WHERE series_id=? ORDER BY sequence DESC LIMIT 1",
                    [row[0]]).fetchone() if table_exists(self.conn, "energy_vintages") else None
                target = {"kind": "energy-series", "namespace": energy_namespace, "id": row[0],
                          "revision": vintage[0] if vintage else None, **{k: ref.get(k) for k in (
                              "provider", "dataset", "native_id")}}
                link_id, new = self._put(namespace, "series", series["series_id"], series["current_vintage_id"],
                                         "balance_series", "energy.core", target, "shared-identifier",
                                         {"named": ref}, "linked", None, principal_id)
                self._collect(result, link_id, new, "linked")
        return result

    # ------------------------------------------------------------------ infrastructure

    def link_infrastructure(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Projects with an accepted EX06 match -> the matched infrastructure asset revision."""
        from src.kb.extractives_identity import ExtractivesIdentity

        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        identity = ExtractivesIdentity(self.conn, initialize=False, now=self.now)
        result: dict[str, list[str]] = {"linked": [], "unresolved": []}
        held = table_exists(self.conn, "infra_assets")
        for assertion in identity.assertions(namespace, scopes={"operator"}, kind="project"):
            if assertion["state"] == "accepted":
                target = {"kind": "infrastructure-asset", "namespace": assertion["target"]["infra_namespace"],
                          "id": assertion["target"]["asset_id"], "revision": assertion["target"]["revision_id"],
                          "asset_class": assertion["target"]["asset_class"]}
                link_id, new = self._put(namespace, "project", assertion["subject"]["project_key"],
                                         assertion["assertion_id"], "located_at_asset", "geospatial.infrastructure",
                                         target, "accepted-match", {"method": assertion["method"],
                                                                    "reviewed": assertion["history"][-1]},
                                         "linked", None, principal_id)
                self._collect(result, link_id, new, "linked")
            elif assertion["state"] in {"unmatched", "proposed"}:
                reason = ("provider_absent: no infrastructure store is held" if not held else
                          "awaiting review" if assertion["state"] == "proposed" else assertion["reason"])
                link_id, new = self._put(namespace, "project", assertion["subject"]["project_key"],
                                         assertion["assertion_id"], "located_at_asset", "geospatial.infrastructure",
                                         None, "accepted-match", {"state": assertion["state"]}, "unresolved", reason,
                                         principal_id)
                self._collect(result, link_id, new, "unresolved")
        return result

    # ------------------------------------------------------------------ explicit citation

    def link_by_citation(self, namespace: str, *, source_kind: str, source_id: str, source_revision: str,
                         relation: str, target: Mapping[str, Any], citation: Mapping[str, Any], principal_id: str,
                         scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        citation = dict(citation or {})
        if not str(citation.get("source") or "").strip() or not str(citation.get("locator") or "").strip():
            raise ExtractivesError("citation_required", "a link names the citing source and a locator")
        if any(word in str(citation.get("basis") or "").casefold() for word in REFUSED_BASES):
            raise ExtractivesError("inferred_link_refused", "links are never inferred from names, co-location or "
                                                            "correlation")
        target = dict(target or {})
        if not target.get("owner") or not target.get("id") or not target.get("revision"):
            raise ExtractivesError("invalid_link", "a cited target names its owner, id and revision")
        link_id, _ = self._put(namespace, source_kind, source_id, source_revision, relation, str(target["owner"]),
                               target, "citation", {"citation": citation}, "linked", None, principal_id)
        return self.link(namespace, link_id)

    # ------------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, source_kind, source_id, source_revision, relation, target_owner, target_json, basis, "
            "detail_json, state, reason, history_json, created_by, created_at_ms FROM ex_links WHERE namespace=? AND "
            "link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "link is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "link_id": row[0], "source_kind": row[1],
                "source_id": row[2], "source_revision": row[3], "relation": row[4], "target_owner": row[5],
                "target": None if row[6] is None else json.loads(row[6]), "basis": row[7],
                "detail": json.loads(row[8]), "state": row[9], "reason": row[10], "history": json.loads(row[11]),
                "created_by": row[12], "created_at_ms": row[13]}

    def links(self, namespace: str, *, scopes: Iterable[str], source_id: str | None = None,
              target_owner: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "ex_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM ex_links WHERE namespace=? AND (? IS NULL OR source_id=?) AND "
            "(? IS NULL OR target_owner=?) AND (? IS NULL OR state=?) ORDER BY source_kind, source_id, link_id",
            [namespace, source_id, source_id, target_owner, target_owner, state, state]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


__all__ = ["BASES", "CONTRACT", "NO_INFERENCE", "ExtractivesLinks"]
