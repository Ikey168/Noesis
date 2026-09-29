"""Link agri-food series to trade flows, climate, weather and RASFF notices by citation (#2213, AF08 #2356).

Links are records of an explicit citation, stored with the citing text and a
locator, and only ever read other packs through their own capabilities:

* **Trade flows** (Economics, filed alongside) - read through a generic
  citing-record reader (:meth:`AgrifoodLinks.link_trade_flows`); when no trade
  provider is composed the answer is ``provider_unavailable``. A trade record
  links when it cites a commodity code that is an acquired code
  (``shared-code``) or that an **accepted** equivalent crosswalk (AF07) maps to
  one (``reviewed-crosswalk``), **and** a place code of the same geospatial
  place. A rejected, reverted, pending or broader/narrower mapping never links;
  those records are reported as skipped with the reason.
* **Climate & Environment and Weather** - only a record that explicitly cites
  both a place code of an agri-food series and one of its reference periods
  (``cited-place-and-period``); read from :class:`src.kb.environment_store.
  EnvironmentStore` by default, or through an injected reader.
* **RASFF notices** (``products.safety``) - a notice revision whose product
  identification or hazard text names a commodity by one of the names its
  publisher's label states, as a whole word (``explicit-commodity-mention``),
  read through :class:`src.kb.product_safety.ProductSafetyStore`.

No causal or correlational link (for example weather to yield) is ever created,
and a link joins records; it never changes or combines any figure.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.agrifood_identity import AgrifoodIdentity, label_names
from src.kb.agrifood_records import (
    LINK_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    AgrifoodError,
    authorize,
    canonical,
    digest,
    label_key,
    require,
)
from src.kb.agrifood_store import AgrifoodStore, table_exists

PRODUCTS_READ = "knowledge:products:read"
ENVIRONMENT_READ = "knowledge:environment:read"
OWNERS = ("economics-trade", "climate-environment", "weather", "products-rasff")
BASES = ("shared-code", "reviewed-crosswalk", "cited-place-and-period", "explicit-commodity-mention")
MIN_NAME_LENGTH = 4
POLICY = "citation only: no causal or correlational link is created, and no figure is changed or combined"
_DDL = """
CREATE TABLE IF NOT EXISTS agrifood_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, owner TEXT NOT NULL, target_kind TEXT NOT NULL,
  target_namespace TEXT NOT NULL, target_id TEXT NOT NULL, target_revision TEXT, commodity_scheme TEXT,
  commodity_code TEXT, place_scheme TEXT, place_code TEXT, period TEXT, basis TEXT NOT NULL, matched TEXT NOT NULL,
  crosswalk_id TEXT, citing_text TEXT NOT NULL, locator_json TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def _codes(values: Any) -> list[tuple[str, str]]:
    out = []
    for item in values or []:
        if isinstance(item, Mapping) and item.get("scheme") and item.get("code"):
            out.append((str(item["scheme"]), str(item["code"])))
    return out


def _mentions(text: str, name: str) -> bool:
    return bool(re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text, flags=re.IGNORECASE))


class AgrifoodLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = AgrifoodStore(conn, initialize=initialize, now=self.now)
        self.identity = AgrifoodIdentity(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _insert(self, namespace, owner, target_kind, target_namespace, target_id, target_revision, commodity, place,
                period, basis, matched, crosswalk_id, citing_text, locator, principal_id) -> dict[str, Any] | None:
        link_id = "agrifood-link:" + digest([namespace, owner, target_id, target_revision, commodity, place, period,
                                             basis, matched])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO agrifood_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING link_id",
            [namespace, link_id, owner, target_kind, target_namespace, target_id, target_revision,
             commodity[0] if commodity else None, commodity[1] if commodity else None, place[0] if place else None,
             place[1] if place else None, period, basis, matched, crosswalk_id, str(citing_text), canonical(locator),
             principal_id, self.now()]).fetchall()
        return {"link_id": link_id, "owner": owner, "target_id": target_id, "basis": basis, "matched": matched,
                "commodity": commodity, "place": place, "period": period} if inserted else None

    # ------------------------------------------------------------------ shared lookups

    def _place_codes(self, series: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], set[tuple[str, str]]]:
        """Every code of the geospatial place behind each series place code (the code itself at least)."""
        out: dict[tuple[str, str], set[tuple[str, str]]] = {}
        for item in series:
            key = (item["place_scheme"], item["place_code"])
            if key in out:
                continue
            codes = {key}
            for place in self.identity._places_with("global", *key):
                codes |= {(s, str(c)) for s, c in place["codes"].items()}
            out[key] = codes
        return out

    def _commodity_routes(self, namespace: str, cited: tuple[str, str]) -> list[tuple[tuple[str, str], str, Any]]:
        """Acquired codes a cited commodity code reaches: itself (shared) or accepted equivalent crosswalks."""
        acquired = {(c["scheme"], c["code"]) for c in self.store.commodities(namespace)}
        routes = []
        for item in self.identity.expand(namespace, *cited):
            key = (item["scheme"], item["code"])
            if key not in acquired:
                continue
            if item["match"] == "query":
                routes.append((key, "shared-code", None))
            elif item["match"] == "equivalent":
                routes.append((key, "reviewed-crosswalk", item["via"][-1]["crosswalk_id"]))
        return routes

    def _mapping_states(self, namespace: str, cited: tuple[str, str]) -> list[dict[str, Any]]:
        return [{"crosswalk_id": c["crosswalk_id"], "state": c["state"], "kind": c["kind"]}
                for c in self.identity.crosswalks(namespace, scopes={"operator"}, code=f"{cited[0]}:{cited[1]}")]

    # ------------------------------------------------------------------ citing records (trade, climate, weather)

    def link_citing_records(self, namespace: str, owner: str, records: Sequence[Mapping[str, Any]], *,
                            scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Records from another pack's reader: ``{record_id, revision_id?, namespace?, kind, text?, cites}``."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if owner not in {"economics-trade", "climate-environment", "weather"}:
            raise AgrifoodError("invalid_request", "citing records come from economics-trade, climate-environment "
                                                   "or weather")
        series = self.store.series_list(namespace)
        place_codes = self._place_codes(series)
        created, skipped = [], []
        for record in records:
            cites = dict(record.get("cites") or {})
            cited_places = set(_codes(cites.get("places")))
            target = (str(record.get("namespace") or namespace), str(record["record_id"]), record.get("revision_id"))
            kind = str(record.get("kind") or f"{owner}-record")
            text = str(record.get("text") or record.get("title") or "")
            matched_places = {key for key, codes in place_codes.items() if codes & cited_places}
            if not matched_places:
                skipped.append({"record_id": target[1], "reason": "cites no place code of an agri-food series"})
                continue
            if owner == "economics-trade":
                routes = []
                for cited in _codes(cites.get("commodities")):
                    found = self._commodity_routes(namespace, cited)
                    if not found:
                        skipped.append({"record_id": target[1], "cited": f"{cited[0]}:{cited[1]}",
                                        "reason": "no acquired code or accepted equivalent crosswalk reaches it",
                                        "mappings": self._mapping_states(namespace, cited)})
                    routes += [(cited, *r) for r in found]
                for cited, commodity, basis, crosswalk_id in routes:
                    for place in sorted(matched_places):
                        if not any((s["commodity_scheme"], s["commodity_code"]) == commodity
                                   and (s["place_scheme"], s["place_code"]) == place for s in series):
                            continue
                        link = self._insert(namespace, owner, kind, *target, commodity, place, None, basis,
                                            f"{cited[0]}:{cited[1]}", crosswalk_id, text,
                                            {"cites": cites, "record_kind": kind}, principal_id)
                        created += [link] if link else []
                continue
            periods = {str(p) for p in cites.get("periods") or []}
            if not periods:
                skipped.append({"record_id": target[1], "reason": "cites no reference period explicitly"})
                continue
            hit = False
            for place in sorted(matched_places):
                known = {v["period_key"] for s in series if (s["place_scheme"], s["place_code"]) == place
                         for g in self.store.vintages(namespace, s["series_id"])
                         for v in self.store.values(namespace, g["vintage_id"])}
                for period in sorted(periods & known):
                    hit = True
                    link = self._insert(namespace, owner, kind, *target, None, place, period,
                                        "cited-place-and-period", f"{place[0]}:{place[1]}@{period}", None, text,
                                        {"cites": cites, "record_kind": kind}, principal_id)
                    created += [link] if link else []
            if not hit:
                skipped.append({"record_id": target[1], "reason": "the cited period is not a period of the place's "
                                                                  "series"})
        return {"namespace": namespace, "owner": owner, "linked": created, "skipped": skipped,
                "records_read": len(records), "status": "read", "policy": POLICY}

    def link_trade_flows(self, namespace: str, reader: Callable[[], Sequence[Mapping[str, Any]]] | None, *,
                         scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Economics trade-flow records through the trade capability's reader; unavailable when none is composed."""
        if reader is None:
            return {"namespace": namespace, "owner": "economics-trade", "linked": [], "records_read": 0,
                    "status": "provider_unavailable", "note": "no Economics trade-flow provider is composed in this "
                                                              "deployment"}
        return self.link_citing_records(namespace, "economics-trade", list(reader()), scopes=scopes,
                                        principal_id=principal_id)

    def link_environment(self, namespace: str, owner: str = "climate-environment",
                         reader: Callable[[], Sequence[Mapping[str, Any]]] | None = None, *, scopes: Iterable[str],
                         principal_id: str, environment_namespace: str | None = None) -> dict[str, Any]:
        """Climate & Environment or Weather records that cite a place and a period; environment store by default."""
        scopes = set(scopes)
        if reader is None:
            if not table_exists(self.conn, "environment_records"):
                return {"namespace": namespace, "owner": owner, "linked": [], "records_read": 0,
                        "status": "provider_unavailable", "note": "no Climate & Environment records are stored"}
            require(scopes, ENVIRONMENT_READ)
            reader = self._environment_reader(environment_namespace or namespace, scopes)
        return self.link_citing_records(namespace, owner, list(reader()), scopes=scopes, principal_id=principal_id)

    def _environment_reader(self, environment_namespace: str, scopes: set[str]):
        from src.kb.environment_store import EnvironmentStore

        def read() -> list[dict[str, Any]]:
            store = EnvironmentStore(self.conn, initialize=False)
            env_scopes = scopes | ({f"namespace:{environment_namespace}:read"} if "operator" in scopes else set())
            out = []
            for record in store.records(environment_namespace, scopes=env_scopes):
                content = record["content"]
                cites = dict(content.get("cites") or {})  # only what a record states itself
                out.append({"record_id": record["record_id"], "revision_id": record["revision_id"],
                            "namespace": environment_namespace, "kind": f"environment-{record['record_type']}",
                            "text": content.get("title") or "", "cites": cites})
            return out

        return read

    # ------------------------------------------------------------------ RASFF

    def _names(self, namespace: str) -> dict[tuple[str, str], list[str]]:
        names: dict[tuple[str, str], list[str]] = {}
        for item in self.store.commodities(namespace):
            for name in label_names(item["label"]):
                if len(name) >= MIN_NAME_LENGTH and name not in names.setdefault((item["scheme"], item["code"]), []):
                    names[(item["scheme"], item["code"])].append(name)
        return names

    def link_rasff(self, namespace: str, *, scopes: Iterable[str], principal_id: str,
                   products_namespace: str | None = None) -> dict[str, Any]:
        """RASFF notice revisions whose product or hazard text names a commodity explicitly."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require(scopes, PRODUCTS_READ)
        products_namespace = products_namespace or namespace
        if not table_exists(self.conn, "product_safety_revisions"):
            return {"namespace": namespace, "owner": "products-rasff", "linked": [], "notices_read": 0,
                    "status": "provider_unavailable", "note": "no product safety notices are stored"}
        from src.kb.product_safety import ProductSafetyStore

        notices = ProductSafetyStore(self.conn, initialize=False)
        names = self._names(namespace)
        created, read, unmatched = [], 0, []
        rows = self.conn.execute("SELECT notice_id, notice_number FROM product_safety_notices WHERE namespace=? AND "
                                 "provider='rasff' ORDER BY notice_id", [products_namespace]).fetchall()
        for notice_id, number in rows:
            hit = False
            for revision in notices.revisions(products_namespace, notice_id):
                read += 1
                parts = notices.parts(products_namespace, revision["revision_id"])
                fields = [({"part": "identification", "kind": i["kind"], **_load(i.get("locator"))}, i["value"])
                          for i in parts["identifications"] if i.get("value") and i["kind"] in {"name", "description"}]
                fields += [({"part": "hazard", "hazard_id": h["hazard_id"], **_load(h.get("locator"))},
                            h["description"]) for h in parts["hazards"] if h.get("description")]
                for locator, text in fields:
                    for commodity, published in sorted(names.items()):
                        for name in published:
                            if not _mentions(str(text), name):
                                continue
                            hit = True
                            link = self._insert(namespace, "products-rasff", "product-safety-notice",
                                                products_namespace, notice_id, revision["revision_id"], commodity,
                                                None, None, "explicit-commodity-mention", f"name:{label_key(name)}",
                                                None, text, {**locator, "notice_number": number}, principal_id)
                            created += [link] if link else []
                            break
            if not hit:
                unmatched.append(number)
        return {"namespace": namespace, "owner": "products-rasff", "linked": created, "notices_read": read,
                "unmatched_notices": unmatched, "status": "read",
                "policy": "an explicit, whole-word mention of a name the commodity's publisher label states; " + POLICY}

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], commodities: Iterable[tuple[str, str]] = (),
              places: Iterable[tuple[str, str]] = (), owner: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "agrifood_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, owner, target_kind, target_namespace, target_id, target_revision, commodity_scheme, "
            "commodity_code, place_scheme, place_code, period, basis, matched, crosswalk_id, citing_text, locator_json, "
            "created_by, created_at_ms FROM agrifood_links WHERE namespace=? AND (? IS NULL OR owner=?) "
            "ORDER BY owner, target_id, link_id", [namespace, owner, owner]).fetchall()
        wanted_c, wanted_p = set(commodities), set(places)
        out = []
        for r in rows:
            commodity = (r[6], r[7]) if r[6] else None
            place = (r[8], r[9]) if r[8] else None
            if wanted_c and commodity is not None and commodity not in wanted_c:
                continue
            if wanted_c and commodity is None and not (wanted_p and place in wanted_p):
                continue
            if wanted_p and place is not None and place not in wanted_p:
                continue
            out.append({"contract": LINK_CONTRACT, "link_id": r[0], "owner": r[1], "target_kind": r[2],
                        "target_namespace": r[3], "target_id": r[4], "target_revision": r[5],
                        "commodity": None if commodity is None else {"scheme": commodity[0], "code": commodity[1]},
                        "place": None if place is None else {"scheme": place[0], "code": place[1]},
                        "period": r[10], "basis": r[11], "matched": r[12], "crosswalk_id": r[13],
                        "citing_text": r[14], "locator": json.loads(r[15]), "created_by": r[16],
                        "created_at_ms": int(r[17]), "claim": "citation only; no causal or correlational claim"})
        return out

    def generation(self, namespace: str) -> int:
        if not table_exists(self.conn, "agrifood_links"):
            return 0
        return int(self.conn.execute("SELECT count(*) FROM agrifood_links WHERE namespace=?",
                                     [namespace]).fetchone()[0])


def _load(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        loaded = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


__all__ = ["BASES", "OWNERS", "POLICY", "AgrifoodLinks"]
