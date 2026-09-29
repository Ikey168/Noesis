"""Trade flows linked to sanctions measures and corporate ownership records by citation (#2210, TF07).

**Sanctions.** A sanctions measure's product scope is the sourced correlation table held by
:class:`src.kb.sanctions_trade.SanctionsTrade` (a control code and the CN8/HS6 codes a published table lists, each
table revisioned and always a *lookup aid*). A trade series is linked to a measure row only when its own product
code is cited by the row, and - when the row or table cites areas - when its reporter or partner is one of them:

* ``same-code`` - the series states the cited code in the cited scheme;
* ``cn8-within-cited-hs6`` - a CN8 series code lies within the cited HS6 code (the CN structure);
* ``hs6-contains-cited-cn8`` - an HS6 series code contains the cited CN8 code (the flow is broader than the
  measure; flagged non-exact);
* ``concordance`` - the measure states its HS edition and the series is in another one: the TF05 concordance row
  used is cited and non-exact mappings are flagged.

Every link points at one observation vintage and records the classification vintage on both sides, the concordance
used and the matching basis. A measure row that matches no acquired flow is reported, never dropped, and a missing
Sanctions provider is reported as ``provider_absent``.

**Ownership.** A link to a corporate ownership record needs an explicit citation (the source and the locator that
names the company in connection with the flow); nothing is inferred from trade values. A cited record that is not
held is stored and reported as ``target_missing``; without the Ownership store the link is ``provider_absent``.

No link states that goods were controlled, that trade evaded a measure or that anyone complied or not.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.trade_flows import (
    READ_SCOPE,
    WRITE_SCOPE,
    TradeError,
    TradeFlowStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-trade-link-v1"
LEGAL_READ = "knowledge:legal:read"
OWNERSHIP_READ = "knowledge:ownership:read"
LOOKUP_AID = (
    "A measure's correlation table is a lookup aid: a link says that the flow's product code is cited by the "
    "measure, never that the goods were controlled, that trade evaded the measure or that anyone complied or not."
)
_DDL = """
CREATE TABLE IF NOT EXISTS trade_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, kind TEXT NOT NULL, series_id TEXT NOT NULL,
  vintage_id TEXT NOT NULL, target_json TEXT NOT NULL, basis_json TEXT NOT NULL, citation_json TEXT NOT NULL,
  target_status TEXT NOT NULL, state TEXT NOT NULL, history_json TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""
CN_SCHEME = {"CN8": "CN", "HS6": "HS"}


def _areas(row: Mapping[str, Any], source: Mapping[str, Any]) -> list[str]:
    cited = row.get("partner_areas") or row.get("areas") or source.get("areas") or []
    return [str(a) for a in cited]


class TradeLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = TradeFlowStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ storage

    def _put(self, namespace, kind, vintage, target, basis, citation, target_status, principal_id):
        link_id = "tf-link:" + digest([namespace, kind, vintage["vintage_id"], target, basis, citation])[:24]
        if self.conn.execute(
            "SELECT 1 FROM trade_links WHERE namespace=? AND link_id=?", [namespace, link_id]
        ).fetchone():
            return link_id, False
        now = self.now()
        self.conn.execute(
            "INSERT INTO trade_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                link_id,
                kind,
                vintage["series_id"],
                vintage["vintage_id"],
                canonical(target),
                canonical(basis),
                canonical(citation),
                target_status,
                "active",
                canonical([{"state": "active", "by": principal_id, "at_ms": now}]),
                principal_id,
                now,
            ],
        )
        return link_id, True

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, kind, series_id, vintage_id, target_json, basis_json, citation_json, target_status, state, "
            "history_json, created_by, created_at_ms FROM trade_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise TradeError("not_found", "link is not visible in this namespace")
        vintage = self.store.vintage(namespace, row[3])
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            "link_id": row[0],
            "kind": row[1],
            "series_id": row[2],
            "vintage_id": row[3],
            "target": json.loads(row[4]),
            "basis": json.loads(row[5]),
            "citation": json.loads(row[6]),
            "target_status": row[7],
            "state": row[8],
            "history": json.loads(row[9]),
            "created_by": row[10],
            "created_at_ms": row[11],
            "vintage_source_revision": vintage["source_revision"],
            "notice": LOOKUP_AID if row[1] == "sanctions" else "linked by explicit citation only",
        }

    def links(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        series_id: str | None = None,
        vintage_id: str | None = None,
        kind: str | None = None,
        active_only: bool = True,
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "trade_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM trade_links WHERE namespace=? AND (? IS NULL OR series_id=?) AND "
            "(? IS NULL OR vintage_id=?) AND (? IS NULL OR kind=?) ORDER BY kind, series_id, created_at_ms, link_id",
            [namespace, series_id, series_id, vintage_id, vintage_id, kind, kind],
        ).fetchall()
        out = [self.link(namespace, r[0]) for r in rows]
        return [link for link in out if not active_only or link["state"] == "active"]

    def withdraw(self, namespace, link_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if not str(reason or "").strip():
            raise TradeError("invalid_decision", "a withdrawal needs a reason")
        link = self.link(namespace, link_id)
        if link["state"] != "active":
            raise TradeError("invalid_state", "only an active link can be withdrawn")
        history = link["history"] + [{"state": "withdrawn", "by": principal_id, "reason": reason, "at_ms": self.now()}]
        self.conn.execute(
            "UPDATE trade_links SET state='withdrawn', history_json=? WHERE namespace=? AND link_id=?",
            [canonical(history), namespace, link_id],
        )
        return self.link(namespace, link_id)

    # ------------------------------------------------------------------ sanctions

    def measures(self, sanctions_namespace: str) -> list[dict[str, Any]]:
        """Every row of the latest revision of each sourced correlation table (the measures' product scope)."""
        rows = self.conn.execute(
            "SELECT t.table_id, t.revision, t.table_json FROM sanctions_trade_correlations t WHERE t.namespace=? AND "
            "t.revision=(SELECT max(x.revision) FROM sanctions_trade_correlations x WHERE x.namespace=t.namespace "
            "AND x.table_id=t.table_id) ORDER BY t.table_id",
            [sanctions_namespace],
        ).fetchall()
        out = []
        for table_id, revision, body in rows:
            table = json.loads(body)
            for row in table["rows"]:
                out.append(
                    {
                        "table_id": table_id,
                        "table_revision": revision,
                        "title": table.get("title"),
                        "source": table.get("source") or {},
                        "control_code": row["control_code"],
                        "product_code": str(row["product_code"]),
                        "product_scheme": row["product_scheme"],
                        "classification_vintage": row.get("classification_vintage")
                        or (table.get("source") or {}).get("classification_vintage"),
                        "areas": _areas(row, table.get("source") or {}),
                        "status": "lookup-aid",
                    }
                )
        return out

    def _product_basis(self, namespace: str, measure: Mapping[str, Any], series: Mapping[str, Any]):
        from src.kb.trade_identity import TradeIdentity

        code, scheme = measure["product_code"], CN_SCHEME[measure["product_scheme"]]
        series_code, series_scheme = series["product"]["code"], series["classification"]["scheme"]
        series_vintage = series["classification"]["vintage"]
        stated = measure["classification_vintage"]
        base = {
            "measure_classification": {"scheme": measure["product_scheme"], "vintage": stated or "not stated"},
            "flow_classification": dict(series["classification"]),
        }
        if scheme == series_scheme and (stated is None or stated == series_vintage):
            if series_code == code:
                return {**base, "method": "same-code", "exact": True, "concordance": None}
            return None
        if scheme == "HS" and series_scheme == "CN" and series_code[:6] == code:
            return {**base, "method": "cn8-within-cited-hs6", "exact": True, "concordance": None,
                    "note": "the CN8 code subdivides the cited HS6 code"}
        if scheme == "CN" and series_scheme == "HS" and code[:6] == series_code:
            return {**base, "method": "hs6-contains-cited-cn8", "exact": False, "concordance": None,
                    "note": "the flow's HS6 code is broader than the cited CN8 code"}
        if scheme == series_scheme == "HS" and stated and stated != series_vintage:
            resolved = TradeIdentity(self.conn, initialize=False).resolve_product(
                namespace, code, {"scheme": "HS", "vintage": stated}, {"scheme": "HS", "vintage": series_vintage}
            )
            for target in resolved["targets"]:
                if target["code"] == series_code:
                    return {**base, "method": "concordance", "exact": target["exact"],
                            "mapping_type": target["mapping_type"], "concordance": target.get("concordance")}
        return None

    def _area_basis(self, namespace: str, measure: Mapping[str, Any], series: Mapping[str, Any]):
        from src.kb.trade_identity import TradeIdentity

        if not measure["areas"]:
            return {"method": "none-cited", "note": "the measure cites no area; the link rests on the product only"}
        identity = TradeIdentity(self.conn, initialize=False)
        for side in ("partner", "reporter"):
            code = series[side]["code"]
            equivalent = (
                identity.equivalent_codes(namespace, code)
                if table_exists(self.conn, "trade_identity_assertions")
                else {"codes": [code], "basis": []}
            )
            cited = sorted(set(measure["areas"]) & set(equivalent["codes"]))
            if cited:
                return {"method": "cited-area", "side": side, "cited": cited,
                        "identity": [b["assertion_id"] for b in equivalent["basis"]]}
        return None

    def link_sanctions(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        sanctions_namespace: str = "global",
    ) -> dict[str, Any]:
        """Link every observation vintage whose product (and cited area) a measure cites; idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, LEGAL_READ)
        if not table_exists(self.conn, "sanctions_trade_correlations"):
            return {
                "status": "provider_absent",
                "provider": "legal.sanctions",
                "detail": "no sanctions correlation tables are held; nothing was linked or inferred",
                "links": [],
                "unmatched_measures": [],
            }
        measures = self.measures(sanctions_namespace)
        series_all = self.store.find_series(namespace)
        created, linked, unmatched = [], [], []
        for measure in measures:
            hits = 0
            for series in series_all:
                product = self._product_basis(namespace, measure, series)
                if product is None:
                    continue
                area = self._area_basis(namespace, measure, series)
                if area is None:
                    continue
                target = {k: measure[k] for k in ("table_id", "table_revision", "control_code", "product_code",
                                                  "product_scheme")} | {"sanctions_namespace": sanctions_namespace}
                citation = {"source": measure["source"], "title": measure["title"], "status": "lookup-aid"}
                for vintage in self.store.vintage_rows(namespace, series["series_id"]):
                    link_id, new = self._put(namespace, "sanctions", vintage, target,
                                             {"product": product, "area": area}, citation, "resolved", principal_id)
                    hits += 1
                    linked.append(link_id)
                    if new:
                        created.append(link_id)
            if not hits:
                unmatched.append({**measure, "reason": "no acquired flow states a product code the measure cites"})
        return {
            "status": "linked" if linked else "no_matching_flows",
            "created": created,
            "links": [self.link(namespace, link_id) for link_id in dict.fromkeys(linked)],
            "unmatched_measures": unmatched,
            "notice": LOOKUP_AID,
        }

    def sanctioned_series(self, namespace: str, *, control_code: str | None = None,
                          table_id: str | None = None) -> dict[str, dict[str, Any]]:
        """Series ids with active sanctions links (optionally for one control code or table), with their links."""
        out: dict[str, dict[str, Any]] = {}
        for link in self.links(namespace, scopes={"operator"}, kind="sanctions"):
            target = link["target"]
            if control_code and str(target["control_code"]).upper() != str(control_code).upper():
                continue
            if table_id and target["table_id"] != table_id:
                continue
            out.setdefault(link["series_id"], {"links": []})["links"].append(link)
        return out

    # ------------------------------------------------------------------ ownership

    def link_ownership(
        self,
        namespace: str,
        *,
        series_id: str,
        vintage_id: str,
        ownership_namespace: str,
        record_id: str,
        citation: Mapping[str, Any],
        statement: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Link an observation vintage to an ownership record the cited source names; nothing is inferred."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, OWNERSHIP_READ)
        citation = dict(citation or {})
        if not str(citation.get("source") or "").strip() or not str(citation.get("locator") or "").strip():
            raise TradeError(
                "citation_required",
                "an ownership link needs the citing source and the locator that names the company; links are never "
                "inferred from trade values",
            )
        if not str(statement or "").strip():
            raise TradeError("citation_required", "state what the cited source says")
        vintage = self.store.vintage(namespace, vintage_id)
        if vintage["series_id"] != series_id:
            raise TradeError("not_found", "vintage does not belong to this series")
        if not table_exists(self.conn, "ownership_records"):
            status = "provider_absent"
        elif self.conn.execute(
            "SELECT 1 FROM ownership_records WHERE namespace=? AND record_id=?", [ownership_namespace, record_id]
        ).fetchone():
            status = "resolved"
        else:
            status = "target_missing"
        target = {"ownership_namespace": ownership_namespace, "record_id": record_id}
        link_id, created = self._put(namespace, "ownership", vintage, target,
                                     {"method": "explicit-citation", "statement": statement.strip()},
                                     citation, status, principal_id)
        return {**self.link(namespace, link_id), "created": created}


__all__ = ["LOOKUP_AID", "TradeLinks"]
