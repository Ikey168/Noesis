"""Reporter and partner areas and product codes through reviewable identity (#2210, TF06).

**Areas.** Every reporter and partner code a trade series states (UN M49 numeric codes from Comtrade, Eurostat
GEO codes from Comext) is offered to the Geospatial places the platform already holds
(:class:`src.kb.geospatial.GeospatialStore`; no new spatial or entity store):

* ``published-code`` - a place whose source identifiers carry the same code in the same scheme (``m49``,
  ``eurostat-geo``);
* ``stated-iso3`` - the ISO 3166-1 alpha-3 code the Comtrade record itself states (``reporterISO`` /
  ``partnerISO``) carried by a place;
* ``iso-alpha2-equivalent`` - a Eurostat GEO code read as ISO 3166-1 alpha-2 (``EL`` is ``GR``, ``UK`` is ``GB``);
* ``gazetteer-name`` - the published label equal to a built-in gazetteer place name (weak; shown as such).

A code whose candidates name more than one place stays ``unmatched`` with the reason; so does a code with none.
Special areas - world and regional aggregates, "areas not elsewhere specified", bunkers and free zones, customs
unions and former countries (the USSR, Czechoslovakia, the former Federal Republic of Germany) - are
``special-area`` records: kept distinct, never proposed to a place and never merged with a successor. Names in
``canonical_entities`` that equal a label are listed as context only.

**Products.** A product code in one classification vintage resolves to another vintage through the concordance
records of TF05 (:meth:`src.kb.trade_flows.TradeFlowStore.map_code`), citing the concordance revision; 1:n, n:1
and n:n rows are flagged ``exact: false``. A CN8 code lies within the HS6 code of its first six digits (the
Combined Nomenclature subdivides the HS), recorded with method ``cn-structure`` and the HS edition the CN year is
based on when the bundled table states it. A code with no concordance stays ``unmatched``.

Every match is an assertion that is ``proposed``, then ``accepted`` or ``rejected`` by a reviewer and can be
``reverted``; queries use accepted area matches only. Nothing re-allocates a flow between areas.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.trade_flows import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    TradeError,
    TradeFlowStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-trade-identity-v1"
GEO_READ = "knowledge:geospatial:read"
# Special areas by scheme (verify each code against the current M49 / Comtrade partner list and the Eurostat GEO
# code list before a live run). They are never matched to a place.
SPECIAL_AREAS: dict[str, dict[str, tuple[str, str]]] = {
    "m49": {
        "0": ("aggregate", "World"),
        "97": ("customs-union", "European Union (as a Comtrade reporter or partner)"),
        "490": ("not-elsewhere-specified", "Other Asia, not elsewhere specified"),
        "527": ("not-elsewhere-specified", "Oceania, not elsewhere specified"),
        "568": ("not-elsewhere-specified", "Other Europe, not elsewhere specified"),
        "577": ("not-elsewhere-specified", "Other Africa, not elsewhere specified"),
        "636": ("not-elsewhere-specified", "Rest of America, not elsewhere specified"),
        "837": ("bunkers-and-stores", "Bunkers"),
        "838": ("free-zones", "Free zones"),
        "839": ("not-elsewhere-specified", "Special categories"),
        "899": ("not-elsewhere-specified", "Areas, not elsewhere specified"),
        "200": ("former-country", "Czechoslovakia (former)"),
        "278": ("former-country", "German Democratic Republic (former)"),
        "280": ("former-country", "Federal Republic of Germany (until 1990)"),
        "532": ("former-country", "Netherlands Antilles (former)"),
        "720": ("former-country", "Democratic Yemen (former)"),
        "810": ("former-country", "USSR (former)"),
        "890": ("former-country", "Yugoslavia, Socialist Federal Republic (former)"),
        "891": ("former-country", "Serbia and Montenegro (former)"),
    },
    "eurostat-geo": {
        "EU27_2020": ("customs-union", "European Union (27 member states from 2020)"),
        "EU28": ("customs-union", "European Union (28 member states, 2013-2020)"),
        "EXT_EU27_2020": ("aggregate", "Extra-EU27 (from 2020)"),
        "INT_EU27_2020": ("aggregate", "Intra-EU27 (from 2020)"),
        "QP": ("not-elsewhere-specified", "High seas"),
        "QQ": ("bunkers-and-stores", "Stores and provisions"),
        "QR": ("bunkers-and-stores", "Stores and provisions within intra-EU trade"),
        "QS": ("bunkers-and-stores", "Stores and provisions within extra-EU trade"),
        "QU": ("not-elsewhere-specified", "Countries and territories not specified"),
        "QV": ("not-elsewhere-specified", "Countries not specified within intra-EU trade"),
        "QW": ("not-elsewhere-specified", "Countries not specified for commercial or military reasons"),
        "QX": ("not-elsewhere-specified", "Countries not specified for commercial or military reasons (intra-EU)"),
        "QY": ("not-elsewhere-specified", "Countries not specified (extra-EU)"),
        "QZ": ("not-elsewhere-specified", "Countries not specified (intra-EU)"),
    },
}
PLACE_KEYS = {"m49": "m49", "eurostat-geo": "eurostat-geo"}
EUROSTAT_ISO2 = {"EL": "GR", "UK": "GB"}
METHOD_STRENGTH = {
    "published-code": "strong",
    "stated-iso3": "strong",
    "iso-alpha2-equivalent": "medium",
    "gazetteer-name": "weak",
}
# The HS edition each CN year is based on (verify against the CN regulations); other years are not stated here.
CN_HS_EDITIONS = {**{str(y): "HS2017" for y in range(2017, 2022)}, **{str(y): "HS2022" for y in range(2022, 2027)}}
_DDL = """
CREATE TABLE IF NOT EXISTS trade_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, method TEXT, evidence_json TEXT NOT NULL, state TEXT NOT NULL,
  reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
"""


def special_area(scheme: str, code: str) -> dict[str, str] | None:
    found = SPECIAL_AREAS.get(scheme, {}).get(str(code))
    return None if found is None else {"kind": found[0], "label": found[1]}


class TradeIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = TradeFlowStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ helpers

    def _areas(self, namespace: str) -> list[dict[str, Any]]:
        """Every distinct area a series states (reporter or partner), with its labels and stated ISO3."""
        if not table_exists(self.conn, "trade_series"):
            return []
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for reporter, partner in self.conn.execute(
            "SELECT reporter_json, partner_json FROM trade_series WHERE namespace=? ORDER BY series_id", [namespace]
        ).fetchall():
            for area in (json.loads(reporter), json.loads(partner)):
                entry = found.setdefault((area["scheme"], str(area["code"])), {"scheme": area["scheme"],
                                                                              "code": str(area["code"]),
                                                                              "labels": set(), "iso3": set()})
                if area.get("label"):
                    entry["labels"].add(area["label"])
                if area.get("iso3"):
                    entry["iso3"].add(area["iso3"])
        return [
            {**v, "labels": sorted(v["labels"]), "iso3": sorted(v["iso3"])}
            for _, v in sorted(found.items())
        ]

    def _places(self, geo_namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, r.revision_id, r.canonical_name, r.place_type, r.source_ids_json FROM "
            "geospatial_places p JOIN geospatial_place_current c ON c.place_id=p.place_id JOIN "
            "geospatial_place_revisions r ON r.revision_id=c.revision_id WHERE p.namespace IN (?, 'global') "
            "ORDER BY p.place_id",
            [geo_namespace],
        ).fetchall()
        return [
            {"place_id": r[0], "revision_id": r[1], "name": r[2], "place_type": r[3],
             "source_ids": json.loads(r[4] or "{}")}
            for r in rows
        ]

    def _canonical_names(self, labels: list[str]) -> list[dict[str, Any]]:
        if not labels or not table_exists(self.conn, "canonical_entities"):
            return []
        wanted = {label.casefold() for label in labels}
        return [
            {"canonical_id": r[0], "preferred_name": r[1], "entity_type": r[2],
             "note": "equal name only; context, never an accepted match on its own"}
            for r in self.conn.execute(
                "SELECT canonical_id, preferred_name, entity_type FROM canonical_entities ORDER BY canonical_id"
            ).fetchall()
            if str(r[1]).casefold() in wanted
        ]

    def _candidates(self, area: Mapping[str, Any], places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scheme, code = area["scheme"], area["code"]
        out = []
        for place in places:
            ids = {str(k): str(v) for k, v in place["source_ids"].items()}
            method, evidence = None, None
            if ids.get(PLACE_KEYS[scheme]) == code:
                method, evidence = "published-code", {"source_id_key": PLACE_KEYS[scheme], "value": code}
            elif area["iso3"] and ids.get("iso3166-1-alpha3") in area["iso3"]:
                method = "stated-iso3"
                evidence = {"source_id_key": "iso3166-1-alpha3", "value": ids["iso3166-1-alpha3"],
                            "stated_by": "the provider record (reporterISO / partnerISO)"}
            elif scheme == "eurostat-geo" and ids.get("iso3166-1-alpha2") == EUROSTAT_ISO2.get(code, code):
                method = "iso-alpha2-equivalent"
                evidence = {"source_id_key": "iso3166-1-alpha2", "value": ids["iso3166-1-alpha2"],
                            "rule": "Eurostat GEO code read as ISO 3166-1 alpha-2 (EL=GR, UK=GB)"}
            elif ids.get("builtin") and ids["builtin"].casefold() in {label.casefold() for label in area["labels"]}:
                method, evidence = "gazetteer-name", {"source_id_key": "builtin", "value": ids["builtin"]}
            if method:
                out.append({"place_id": place["place_id"], "place_revision_id": place["revision_id"],
                            "place_name": place["name"], "place_type": place["place_type"], "method": method,
                            "strength": METHOD_STRENGTH[method], "evidence": evidence})
        # The strongest method decides; weaker candidates for other places are kept as evidence only.
        order = list(METHOD_STRENGTH)
        out.sort(key=lambda c: (order.index(c["method"]), c["place_id"]))
        return out

    def _latest(self, namespace: str, kind: str, subject_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT assertion_id FROM trade_identity_assertions WHERE namespace=? AND kind=? AND subject_key=? "
            "ORDER BY created_at_ms DESC, assertion_id DESC LIMIT 1",
            [namespace, kind, subject_key],
        ).fetchone()
        return None if row is None else self.assertion(namespace, row[0], scopes={"operator"})

    def _record(self, namespace, kind, subject, target, method, evidence, state, reason, principal_id):
        """Record an evaluation unless the latest one already says the same (a reverted decision is not re-proposed
        on unchanged evidence); a changed outcome is a new assertion, earlier ones stay as history."""
        subject_key = canonical(subject)
        latest = self._latest(namespace, kind, subject_key)
        if (
            latest is not None
            and latest["target"] == target
            and latest["method"] == method
            and latest["state"] in {state, "reverted"}
            and (latest["state"] == "reverted" or latest["reason"] == reason)
        ):
            return latest["assertion_id"], False
        number = 1 + int(
            self.conn.execute(
                "SELECT count(*) FROM trade_identity_assertions WHERE namespace=? AND kind=? AND subject_key=?",
                [namespace, kind, subject_key],
            ).fetchone()[0]
        )
        assertion_id = "tf-identity:" + digest([namespace, kind, subject, number, target, method, state])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO trade_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject), None if target is None else canonical(target),
             method, canonical(evidence), state, reason,
             canonical([{"state": state, "by": principal_id, "at_ms": now, "reason": reason}]), principal_id, now],
        )
        return assertion_id, True

    # ------------------------------------------------------------------ areas

    def propose_areas(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str], geo_namespace: str = "global"
    ) -> dict[str, Any]:
        """Offer every stated area code to Geospatial places; idempotent. Special areas stay distinct."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        places = self._places(geo_namespace)
        created, out = [], []
        for area in self._areas(namespace):
            subject = {"scheme": area["scheme"], "code": area["code"]}
            special = special_area(area["scheme"], area["code"])
            latest = self._latest(namespace, "area", canonical(subject))
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue  # a reviewed decision stands until it is reverted
            if special:
                target, method, state, reason = None, "special-area-table", "special-area", special["kind"]
                evidence = {"special_area": special, "labels": area["labels"],
                            "handling": "kept distinct; never matched to a place or merged with a successor"}
            else:
                candidates = self._candidates(area, places)
                best = [c for c in candidates if c["method"] == candidates[0]["method"]] if candidates else []
                evidence = {"labels": area["labels"], "stated_iso3": area["iso3"], "candidates": candidates,
                            "canonical_entities": self._canonical_names(area["labels"])}
                if len({c["place_id"] for c in best}) == 1:
                    chosen = best[0]
                    target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
                    method, state, reason = chosen["method"], "proposed", None
                    evidence["strength"] = chosen["strength"]
                else:
                    target, method, state = None, None, "unmatched"
                    reason = "no place carries this code" if not best else "more than one place carries this code"
            assertion_id, new = self._record(namespace, "area", subject, target, method, evidence, state, reason,
                                             principal_id)
            if new:
                created.append(assertion_id)
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    # ------------------------------------------------------------------ products

    def propose_products(
        self,
        namespace: str,
        target: Mapping[str, str],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Resolve every stated product code to the target classification vintage through the concordances (or the
        CN structure); non-exact mappings are flagged; unmatched codes stay visible."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        target = {"scheme": str(target["scheme"]), "vintage": str(target["vintage"])}
        rows = self.conn.execute(
            "SELECT DISTINCT product_code, classification_scheme, classification_vintage FROM trade_series WHERE "
            "namespace=? ORDER BY 2, 3, 1",
            [namespace],
        ).fetchall() if table_exists(self.conn, "trade_series") else []
        created, out = [], []
        for code, scheme, vintage in rows:
            source = {"scheme": scheme, "vintage": vintage}
            if source == target:
                continue
            subject = {"code": code, **source, "target": target}
            latest = self._latest(namespace, "product", canonical(subject))
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue
            resolved = self.resolve_product(namespace, code, source, target)
            if resolved["targets"]:
                state, reason = "proposed", None
                mapping = {"targets": resolved["targets"]}
                method = resolved["method"]
            else:
                state, reason, mapping, method = "unmatched", resolved["reason"], None, None
            assertion_id, new = self._record(namespace, "product", subject, mapping, method,
                                             {"exact": resolved["exact"]}, state, reason, principal_id)
            if new:
                created.append(assertion_id)
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    def resolve_product(
        self, namespace: str, code: str, source: Mapping[str, str], target: Mapping[str, str],
        *, as_of_ms: int | None = None,
    ) -> dict[str, Any]:
        """One code's targets in another classification vintage, each citing its basis; nothing stored."""
        source, target = dict(source), dict(target)
        if source == target:
            return {"code": code, "targets": [{"code": code, "exact": True, "mapping_type": "1:1",
                                               "basis": "same classification vintage"}],
                    "exact": True, "method": "identity", "reason": None}
        if source["scheme"] == "CN" and target["scheme"] == "HS" and len(str(code)) == 8:
            edition = CN_HS_EDITIONS.get(source["vintage"][2:6])
            hs6 = str(code)[:6]
            entry = {
                "code": hs6,
                "mapping_type": "n:1",
                "exact": False,
                "basis": "cn-structure: a CN8 code subdivides the HS6 code of its first six digits",
                "cn_hs_edition": edition or "not stated for this CN year (verify against the CN regulation)",
                "citation": "Council Regulation (EEC) No 2658/87, Annex I (Combined Nomenclature)",
            }
            if edition is not None and edition != target["vintage"]:
                # Through the HS edition the CN year is based on, then the concordance to the requested edition.
                chained = self.store.map_code(namespace, hs6, {"scheme": "HS", "vintage": edition}, target,
                                              as_of_ms=as_of_ms)
                targets = [{**t, "via": entry, "exact": False} for t in chained["targets"]]
                return {"code": code, "targets": targets, "exact": False, "method": "cn-structure+concordance",
                        "reason": None if targets else "no concordance from the CN year's HS edition"}
            return {"code": code, "targets": [entry], "exact": False, "method": "cn-structure", "reason": None}
        mapped = self.store.map_code(namespace, code, source, target, as_of_ms=as_of_ms)
        targets = [{**t, "basis": "concordance"} for t in mapped["targets"]]
        return {
            "code": code,
            "targets": targets,
            "exact": bool(targets) and all(t["exact"] for t in targets),
            "method": "concordance" if targets else None,
            "reason": None if targets else "no concordance between these classification vintages",
        }

    # ------------------------------------------------------------------ review

    def assertion(self, namespace: str, assertion_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_json, target_json, method, evidence_json, state, reason, history_json, "
            "created_by, created_at_ms FROM trade_identity_assertions WHERE namespace=? AND assertion_id=?",
            [namespace, assertion_id],
        ).fetchone()
        if row is None:
            raise TradeError("not_found", "identity assertion is not visible in this namespace")
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            "assertion_id": row[0],
            "kind": row[1],
            "subject": json.loads(row[2]),
            "target": None if row[3] is None else json.loads(row[3]),
            "method": row[4],
            "evidence": json.loads(row[5]),
            "state": row[6],
            "reason": row[7],
            "history": json.loads(row[8]),
            "created_by": row[9],
            "created_at_ms": row[10],
        }

    def assertions(
        self, namespace: str, *, scopes: Iterable[str], kind: str | None = None, state: str | None = None
    ) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "trade_identity_assertions"):
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM trade_identity_assertions WHERE namespace=? AND (? IS NULL OR kind=?) "
            "ORDER BY kind, subject_key, created_at_ms, assertion_id",
            [namespace, kind, kind],
        ).fetchall()
        # The latest assertion per subject is the one in force; earlier ones stay as history.
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for (assertion_id,) in rows:
            item = self.assertion(namespace, assertion_id, scopes={"operator"})
            latest[(item["kind"], canonical(item["subject"]))] = item
        return [a for a in latest.values() if state is None or a["state"] == state]

    def _transition(self, namespace, item, state, principal_id, reason):
        history = item["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        self.conn.execute(
            "UPDATE trade_identity_assertions SET state=?, history_json=? WHERE namespace=? AND assertion_id=?",
            [state, canonical(history), namespace, item["assertion_id"]],
        )
        return self.assertion(namespace, item["assertion_id"], scopes={"operator"})

    def review(self, namespace, assertion_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise TradeError("invalid_decision", "accept or reject with a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] != "proposed":
            raise TradeError("invalid_state", f"assertion is {item['state']}; only a proposed match is reviewed")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace, assertion_id, reason, *, principal_id, scopes):
        """Withdraw a reviewed decision; the subject is unmatched again until a new proposal is reviewed."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise TradeError("invalid_decision", "a revert needs a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected"}:
            raise TradeError("invalid_state", "only an accepted or rejected match can be reverted")
        return self._transition(namespace, item, "reverted", principal_id, reason.strip())

    # ------------------------------------------------------------------ use by queries

    def accepted_place(self, namespace: str, scheme: str, code: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "trade_identity_assertions"):
            return None
        latest = self._latest(namespace, "area", canonical({"scheme": scheme, "code": str(code)}))
        return latest if latest and latest["state"] == "accepted" else None

    def equivalent_codes(self, namespace: str, code: str) -> dict[str, Any]:
        """The area codes that denote the same place as ``code`` through accepted matches (else the code itself)."""
        code = str(code)
        accepted = self.assertions(namespace, scopes={"operator"}, kind="area", state="accepted") if table_exists(
            self.conn, "trade_identity_assertions") else []
        places = {a["target"]["place_id"] for a in accepted if a["subject"]["code"] == code}
        codes = {code} | {a["subject"]["code"] for a in accepted if a["target"]["place_id"] in places}
        return {
            "code": code,
            "codes": sorted(codes),
            "place_ids": sorted(places),
            "basis": [
                {"assertion_id": a["assertion_id"], "subject": a["subject"], "method": a["method"],
                 "place_id": a["target"]["place_id"]}
                for a in accepted
                if a["target"]["place_id"] in places
            ],
            "special_area": special_area("m49", code) or special_area("eurostat-geo", code),
        }


__all__ = ["CN_HS_EDITIONS", "SPECIAL_AREAS", "TradeIdentity", "special_area"]
