"""Companies, commodities, countries and projects matched through reviewable identity (#2653, EX06).

**Companies.** Every reporting company of an EITI report revision is proposed against the legal entities of an
ownership namespace in the shared reviewable state machine (:class:`src.kb.ownership_identity.OwnershipIdentityService`,
whose accepted, rejected and reverted decisions are :class:`src.kb.entity_history.EntityHistoryStore` decisions):
``exact-identifier`` first (an LEI or register number the report states equal to one the ownership record
carries), then ``name-jurisdiction`` as low evidence (equal normalised names; the report country is where the
company pays, not its jurisdiction, so it never corroborates). Nothing is accepted automatically. Candidate keys
start with ``extractives:`` (in ``FOREIGN_KEY_PREFIXES``), so these links never regroup ownership entities. A
redacted natural-person entity is never proposed.

**Commodities.** An HS code the EITI summary states is a published identifier (``published-hs-code``); a USGS or
BGS commodity maps to HS only through a published concordance an operator imports with its citation (URL,
publisher, publication date, file digest), each row ``exact``, ``partial`` or ``one-to-many`` as published.
**Countries.** A country code the source publishes (``published-code``) is used before a name; a country name maps
to ISO 3166-1 alpha-3 only through an imported, cited code list. Aggregates (``World total``) stay unmatched.
**Projects.** A project maps to an infrastructure asset only through a published identifier both records carry or
through published coordinates of both records that coincide within the coarser published precision
(``published-coordinates``, low evidence); nothing is matched by name.

Every mapping is ``proposed`` (or ``unmatched``), then ``accepted`` or ``rejected`` by a reviewer and can be
``reverted``; queries use accepted mappings only, and unmatched subjects stay visible as unmatched.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal
from typing import Any

from src.kb.extractives_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    SUBJECT_PREFIX,
    WRITE_SCOPE,
    ExtractivesError,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)
from src.kb.extractives_store import ExtractivesStore

CONTRACT = "noesis-extractives-identity-v1"
KINDS = ("commodity", "country", "project")
RELATIONS = ("exact", "partial", "one-to-many")
INFRA_READ = "knowledge:infrastructure:read"
NAME_BASES = frozenset({"name-jurisdiction", "similar-name"})
SCHEME_ALIASES = {"lei": {"lei"}, "gb-coh": {"gb-coh", "ra:RA000585"}, "sec-cik": {"sec-cik"}}
AGGREGATE_WORDS = ("world", "total", "other countries", "rounded")
_DDL = """
CREATE TABLE IF NOT EXISTS ex_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, method TEXT, relation TEXT, confidence DOUBLE, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
CREATE TABLE IF NOT EXISTS ex_concordances (
  namespace TEXT NOT NULL, concordance_id TEXT NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL,
  target_json TEXT NOT NULL, citation_json TEXT NOT NULL, rows_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  recorded_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, concordance_id)
);
"""
CONFIDENCE = {"published-hs-code": 0.95, "published-concordance": 0.9, "published-code": 0.95,
              "published-code-list": 0.85, "published-identifier": 0.9, "published-coordinates": 0.4}


def _scheme_set(scheme: str) -> set[str]:
    for group in SCHEME_ALIASES.values():
        if scheme in group:
            return group
    return {scheme}


def _value(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum()).lstrip("0") or "0"


def _name(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def entity_for(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def aggregate(name: str) -> bool:
    return any(word in str(name).casefold() for word in AGGREGATE_WORDS)


def _precision_m(value: str) -> float:
    decimals = len(value.split(".", 1)[1]) if "." in value else 0
    return 0.5 * 10 ** (-decimals) * 111_320


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


class ExtractivesIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.store = ExtractivesStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ assertions

    def _latest(self, namespace: str, kind: str, subject_key: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "ex_identity_assertions"):
            return None
        row = self.conn.execute(
            "SELECT assertion_id FROM ex_identity_assertions WHERE namespace=? AND kind=? AND subject_key=? "
            "ORDER BY created_at_ms DESC, assertion_id DESC LIMIT 1", [namespace, kind, subject_key]).fetchone()
        return None if row is None else self.assertion(namespace, row[0], scopes={"operator"})

    def _record(self, namespace, kind, subject, target, method, relation, evidence, state, reason, principal_id):
        subject_key = canonical(subject)
        latest = self._latest(namespace, kind, subject_key)
        if latest is not None and latest["state"] in {"accepted", "rejected"}:
            return latest["assertion_id"], False
        if latest is not None and latest["target"] == target and latest["method"] == method \
                and latest["state"] in {state, "reverted"}:
            return latest["assertion_id"], False
        number = 1 + int(self.conn.execute(
            "SELECT count(*) FROM ex_identity_assertions WHERE namespace=? AND kind=? AND subject_key=?",
            [namespace, kind, subject_key]).fetchone()[0])
        assertion_id = "ex-identity:" + digest([namespace, kind, subject, number, target, method, state])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO ex_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject),
             None if target is None else canonical(target), method, relation, CONFIDENCE.get(method),
             canonical(evidence), state, reason,
             canonical([{"state": state, "by": principal_id, "at_ms": now, "reason": reason}]), principal_id, now])
        return assertion_id, True

    def assertion(self, namespace: str, assertion_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_json, target_json, method, relation, confidence, evidence_json, "
            "state, reason, history_json, created_by, created_at_ms FROM ex_identity_assertions WHERE namespace=? "
            "AND assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "identity assertion is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "assertion_id": row[0], "kind": row[1],
                "subject": json.loads(row[2]), "target": None if row[3] is None else json.loads(row[3]),
                "method": row[4], "relation": row[5], "confidence": row[6], "evidence": json.loads(row[7]),
                "state": row[8], "reason": row[9], "history": json.loads(row[10]), "created_by": row[11],
                "created_at_ms": row[12], "low_evidence": row[4] == "published-coordinates",
                "notice": "a reviewable mapping; records are never merged or rewritten"}

    def assertions(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "ex_identity_assertions"):
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM ex_identity_assertions WHERE namespace=? AND (? IS NULL OR kind=?) "
            "ORDER BY kind, subject_key, created_at_ms, assertion_id", [namespace, kind, kind]).fetchall()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for (assertion_id,) in rows:
            item = self.assertion(namespace, assertion_id, scopes={"operator"})
            latest[(item["kind"], canonical(item["subject"]))] = item
        return [a for a in latest.values() if state is None or a["state"] == state]

    def _transition(self, namespace, item, state, principal_id, reason):
        history = item["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        self.conn.execute("UPDATE ex_identity_assertions SET state=?, history_json=? WHERE namespace=? AND "
                          "assertion_id=?", [state, canonical(history), namespace, item["assertion_id"]])
        return self.assertion(namespace, item["assertion_id"], scopes={"operator"})

    def review(self, namespace: str, assertion_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept or reject a proposed commodity, country or project mapping, or a company candidate."""
        scopes = set(scopes)
        if assertion_id.startswith("own-idc:"):
            return self.review_company(namespace, assertion_id, decision, reason, principal_id=principal_id,
                                       scopes=scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise ExtractivesError("invalid_decision", "accept or reject with a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] != "proposed":
            raise ExtractivesError("invalid_state", f"assertion is {item['state']}; only a proposal is reviewed")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace: str, assertion_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        if assertion_id.startswith("own-idc:"):
            return self.revert_company(namespace, assertion_id, reason, principal_id=principal_id, scopes=scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise ExtractivesError("invalid_decision", "a revert needs a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected"}:
            raise ExtractivesError("invalid_state", "only an accepted or rejected mapping can be reverted")
        return self._transition(namespace, item, "reverted", principal_id, reason.strip())

    # ------------------------------------------------------------------ concordances

    def import_concordance(self, namespace: str, table: Mapping[str, Any], *, principal_id: str,
                           scopes: Iterable[str]) -> dict[str, Any]:
        """Record a published commodity-to-HS or country-name-to-code table with its citation."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        table = dict(table)
        kind = table.get("kind")
        if kind not in {"commodity", "country"}:
            raise ExtractivesError("invalid_table", "a concordance maps commodities or countries")
        citation = dict(table.get("citation") or {})
        if not all(citation.get(k) for k in ("url", "publisher", "published_on", "file_sha256")):
            raise ExtractivesError("invalid_table", "a concordance cites its URL, publisher, publication date and "
                                                    "file digest")
        target = dict(table.get("target") or {})
        if not target.get("scheme") or not target.get("version"):
            raise ExtractivesError("invalid_table", "a concordance states its target scheme and version")
        rows = []
        for row in table.get("rows") or []:
            row = dict(row)
            if not row.get("name") or not row.get("code") or row.get("relation", "exact") not in RELATIONS:
                raise ExtractivesError("invalid_table", f"each row names the published name, a code and a relation "
                                                        f"in {RELATIONS}")
            rows.append({"provider": row.get("provider"), "name": str(row["name"]), "form": row.get("form"),
                         "code": str(row["code"]), "relation": row.get("relation", "exact"),
                         **({"note": str(row["note"])} if row.get("note") else {})})
        if not rows or not str(table.get("label") or "").strip():
            raise ExtractivesError("invalid_table", "a concordance has a label and at least one row")
        rows.sort(key=lambda r: canonical(r))
        content_hash = digest([kind, target, rows, citation])
        concordance_id = "ex-concordance:" + content_hash[:24]
        if not self.conn.execute("SELECT 1 FROM ex_concordances WHERE namespace=? AND concordance_id=?",
                                 [namespace, concordance_id]).fetchone():
            self.conn.execute("INSERT INTO ex_concordances VALUES (?,?,?,?,?,?,?,?,?,?)",
                              [namespace, concordance_id, kind, str(table["label"]).strip(), canonical(target),
                               canonical(citation), canonical(rows), content_hash, principal_id, self.now()])
        return self.concordance(namespace, concordance_id)

    def concordance(self, namespace: str, concordance_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT concordance_id, kind, label, target_json, citation_json, rows_json, recorded_by FROM "
            "ex_concordances WHERE namespace=? AND concordance_id=?", [namespace, concordance_id]).fetchone()
        if row is None:
            raise ExtractivesError("not_found", "concordance is not visible in this namespace")
        return {"concordance_id": row[0], "kind": row[1], "label": row[2], "target": json.loads(row[3]),
                "citation": json.loads(row[4]), "rows": json.loads(row[5]), "recorded_by": row[6]}

    def concordances(self, namespace: str, kind: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ex_concordances"):
            return []
        return [self.concordance(namespace, r[0]) for r in self.conn.execute(
            "SELECT concordance_id FROM ex_concordances WHERE namespace=? AND (? IS NULL OR kind=?) "
            "ORDER BY label, concordance_id", [namespace, kind, kind]).fetchall()]

    # ------------------------------------------------------------------ commodities

    def _commodity_subjects(self, namespace: str) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for series in self.store.find_series(namespace):
            commodity = series["commodity"]
            found.setdefault(series["commodity_key"], {
                "commodity_key": series["commodity_key"], "provider": series["provider"],
                "name": commodity.get("name"), "form": commodity.get("form"), "code": commodity.get("code"),
                "hydrocarbon": bool(commodity.get("hydrocarbon")), "stated_hs": None})
        if table_exists(self.conn, "ex_reports"):
            for report_id, commodities in self.conn.execute(
                    "SELECT report_id, commodities_json FROM ex_reports WHERE namespace=? ORDER BY report_id",
                    [namespace]).fetchall():
                for commodity in json.loads(commodities):
                    key = SUBJECT_PREFIX + "commodity:" + digest(["eiti", commodity.get("name"), None])[:24]
                    found.setdefault(key, {"commodity_key": key, "provider": "eiti", "name": commodity.get("name"),
                                           "form": None, "code": None, "hydrocarbon": False,
                                           "stated_hs": commodity.get("hs_code"), "report_id": report_id})
        return [found[k] for k in sorted(found)]

    def propose_commodities(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Map every commodity to HS: a stated HS code first, else a cited concordance row; unmatched stays."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        tables = self.concordances(namespace, "commodity")
        created, out = [], []
        for subject in self._commodity_subjects(namespace):
            key = {k: subject[k] for k in ("commodity_key", "provider", "name", "form")}
            if subject["stated_hs"]:
                target = {"scheme": "HS", "version": "as stated by the report", "codes": [
                    {"code": subject["stated_hs"], "relation": "exact"}]}
                method, relation, state, reason = "published-hs-code", "exact", "proposed", None
                evidence = {"stated_in": subject.get("report_id"), "rule": "the EITI summary states the HS code"}
            else:
                rows = [(t, r) for t in tables for r in t["rows"]
                        if (r.get("provider") in (None, subject["provider"])) and r["name"] == subject["name"]
                        and (r.get("form") in (None, subject["form"]))]
                if rows:
                    codes = [{"code": r["code"], "relation": r["relation"],
                              "concordance": {"concordance_id": t["concordance_id"], "label": t["label"],
                                              "citation": t["citation"]}} for t, r in rows]
                    target = {"scheme": rows[0][0]["target"]["scheme"], "version": rows[0][0]["target"]["version"],
                              "codes": codes}
                    relation = "one-to-many" if len(codes) > 1 else codes[0]["relation"]
                    method, state, reason = "published-concordance", "proposed", None
                    evidence = {"rule": "a published concordance row names this commodity"}
                else:
                    target, method, relation, state = None, None, None, "unmatched"
                    reason, evidence = "no stated HS code and no published concordance row", {}
            assertion_id, new = self._record(namespace, "commodity", key, target, method, relation, evidence, state,
                                             reason, principal_id)
            created += [assertion_id] if new else []
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    def commodity_keys_for(self, namespace: str, commodity: Any) -> dict[str, Any]:
        """Commodity keys a request names: an HS code through accepted mappings (and the HS hierarchy), a name as
        published, or a commodity key."""
        if isinstance(commodity, str) and commodity.startswith(SUBJECT_PREFIX):
            return {"requested": commodity, "keys": [{"commodity_key": commodity, "basis": "requested key"}],
                    "unmatched": []}
        request = dict(commodity) if isinstance(commodity, Mapping) else {"name": str(commodity)}
        keys = []
        if request.get("hs_code"):
            code = str(request["hs_code"])
            for a in self.assertions(namespace, scopes={"operator"}, kind="commodity", state="accepted"):
                for mapped in a["target"]["codes"]:
                    if mapped["code"] == code or mapped["code"].startswith(code) or code.startswith(mapped["code"]):
                        keys.append({"commodity_key": a["subject"]["commodity_key"],
                                     "provider": a["subject"]["provider"], "name": a["subject"]["name"],
                                     "form": a["subject"]["form"], "hs_code": mapped["code"],
                                     "relation": mapped["relation"] if mapped["code"] == code else (
                                         "narrower" if mapped["code"].startswith(code) else "broader"),
                                     "assertion_id": a["assertion_id"], "method": a["method"],
                                     "basis": "accepted commodity mapping"})
            unmatched = [{"name": a["subject"]["name"], "provider": a["subject"]["provider"], "state": a["state"],
                          "reason": a["reason"]}
                         for a in self.assertions(namespace, scopes={"operator"}, kind="commodity")
                         if a["state"] != "accepted"]
            return {"requested": request, "keys": keys, "unmatched": unmatched}
        name = str(request.get("name") or "").casefold()
        for subject in self._commodity_subjects(namespace):
            if str(subject["name"] or "").casefold() == name and (
                    not request.get("provider") or request["provider"] == subject["provider"]):
                keys.append({"commodity_key": subject["commodity_key"], "provider": subject["provider"],
                             "name": subject["name"], "form": subject["form"], "basis": "name as published"})
        return {"requested": request, "keys": keys, "unmatched": []}

    # ------------------------------------------------------------------ countries

    def propose_countries(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Map every series country to ISO 3166-1 alpha-3: a published code first, else a cited code list."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        tables = self.concordances(namespace, "country")
        subjects: dict[str, dict[str, Any]] = {}
        for series in self.store.find_series(namespace):
            country = series["country"]
            subjects.setdefault(canonical([series["provider"], country.get("name")]), {
                "provider": series["provider"], "name": country.get("name"), "code": country.get("code"),
                "code_scheme": country.get("code_scheme")})
        created, out = [], []
        for _, subject in sorted(subjects.items()):
            key = {"provider": subject["provider"], "name": subject["name"]}
            if aggregate(subject["name"]):
                target, method, relation, state = None, None, None, "unmatched"
                reason, evidence = "an aggregate the source publishes, not a country", {}
            elif subject["code"] and subject.get("code_scheme") == "iso3166-1-alpha3":
                target = {"scheme": "iso3166-1-alpha3", "code": subject["code"]}
                method, relation, state, reason = "published-code", "exact", "proposed", None
                evidence = {"rule": "the source publishes the ISO code with the figure"}
            else:
                rows = [(t, r) for t in tables for r in t["rows"] if r["name"].casefold() == subject["name"].casefold()
                        and r.get("provider") in (None, subject["provider"])]
                codes = {r["code"] for _, r in rows}
                if len(codes) == 1:
                    t, r = rows[0]
                    target = {"scheme": t["target"]["scheme"], "code": r["code"]}
                    method, relation, state, reason = "published-code-list", r["relation"], "proposed", None
                    evidence = {"concordance": {"concordance_id": t["concordance_id"], "label": t["label"],
                                                "citation": t["citation"]}}
                else:
                    target, method, relation, state = None, None, None, "unmatched"
                    reason = ("several codes carry this name" if codes else "no published code list names it")
                    evidence = {"candidates": sorted(codes)}
            assertion_id, new = self._record(namespace, "country", key, target, method, relation, evidence, state,
                                             reason, principal_id)
            created += [assertion_id] if new else []
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    def country_names_for(self, namespace: str, country: Any) -> dict[str, Any]:
        """Native country names a request names: an ISO alpha-3 code through accepted mappings, or a name."""
        request = dict(country) if isinstance(country, Mapping) else (
            {"code": country} if isinstance(country, str) and len(country) == 3 and country.isupper()
            else {"name": str(country)})
        if request.get("name"):
            return {"requested": request, "names": [{"name": request["name"], "basis": "name as published"}],
                    "unmatched": []}
        code = str(request["code"]).upper()
        names = [{"name": a["subject"]["name"], "provider": a["subject"]["provider"], "method": a["method"],
                  "assertion_id": a["assertion_id"], "basis": "accepted country mapping"}
                 for a in self.assertions(namespace, scopes={"operator"}, kind="country", state="accepted")
                 if a["target"]["code"] == code]
        pending = [{"name": a["subject"]["name"], "provider": a["subject"]["provider"], "state": a["state"]}
                   for a in self.assertions(namespace, scopes={"operator"}, kind="country")
                   if a["state"] == "proposed" and (a["target"] or {}).get("code") == code]
        return {"requested": {"code": code}, "names": names, "unmatched": pending, "code": code}

    # ------------------------------------------------------------------ projects

    def _assets(self, infra_namespace: str, scopes: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "infra_assets"):
            return []
        from src.kb.infrastructure_assets import InfrastructureStore

        require_scope(scopes, INFRA_READ)
        infra = InfrastructureStore(self.conn, initialize=False)
        grant = {"operator"}
        out = []
        for asset in infra.assets(infra_namespace, scopes=grant):
            revision = infra.revision_as_of(infra_namespace, asset["asset_id"], scopes=grant)
            if revision is not None:
                out.append({**asset, "revision_id": revision["revision_id"], "record": revision["record"]})
        return out

    def propose_projects(self, namespace: str, *, infra_namespace: str, principal_id: str,
                         scopes: Iterable[str]) -> dict[str, Any]:
        """Offer every reported project to infrastructure assets by published identifier or coordinates."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        assets = self._assets(infra_namespace, scopes)
        created, out = [], []
        for project in self.store.projects(namespace):
            key = {"project_key": project["key"], "name_as_reported": project.get("name_as_reported")}
            published = {(i["scheme"], _value(i["value"])) for i in project.get("identifiers") or []}
            candidates = []
            for asset in assets:
                ids = {(i["scheme"], _value(i["value"])) for i in asset["record"].get("identifiers") or []}
                ids.add((f"{asset['provider']}:{asset['dataset']}", _value(asset["native_id"])))
                shared = sorted(published & ids)
                if shared:
                    candidates.append((asset, "published-identifier", {"shared_identifiers": [
                        {"scheme": s, "value": v} for s, v in shared]}))
                    continue
                point = project.get("coordinates")
                geometry = asset["record"].get("geometry") or {}
                if point and geometry.get("type") == "Point":
                    lon, lat = geometry["coordinates"][0], geometry["coordinates"][1]
                    tolerance = max(_precision_m(point["lon"]), _precision_m(point["lat"]),
                                    _precision_m(format(Decimal(str(lon)), "f")),
                                    _precision_m(format(Decimal(str(lat)), "f")))
                    distance = _distance_m((float(point["lon"]), float(point["lat"])), (float(lon), float(lat)))
                    if distance <= tolerance:
                        candidates.append((asset, "published-coordinates", {
                            "project_point": point, "asset_point": [lon, lat], "distance_m": round(distance, 1),
                            "tolerance_m": round(tolerance, 1), "low_evidence": True,
                            "note": "coordinates coincide within the coarser published precision; a reviewer "
                                    "decides; ownership of the project is never inferred"}))
            candidates.sort(key=lambda c: (c[1] != "published-identifier", c[0]["asset_id"]))
            if candidates:
                asset, method, detail = candidates[0]
                target = {"owner": "geospatial.infrastructure", "asset_id": asset["asset_id"],
                          "revision_id": asset["revision_id"], "provider": asset["provider"],
                          "dataset": asset["dataset"], "native_id": asset["native_id"],
                          "asset_class": asset["asset_class"], "infra_namespace": infra_namespace}
                evidence = {**detail, "other_candidates": [c[0]["asset_id"] for c in candidates[1:]]}
                state, reason = "proposed", None
            else:
                target, method, state, reason, evidence = None, None, "unmatched", (
                    "no infrastructure asset shares a published identifier or coordinates"
                    if assets else "no infrastructure asset is held"), {}
            assertion_id, new = self._record(namespace, "project", key, target, method,
                                             "exact" if target else None, evidence, state, reason, principal_id)
            created += [assertion_id] if new else []
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    # ------------------------------------------------------------------ companies

    def propose_companies(self, namespace: str, *, ownership_namespace: str, principal_id: str,
                          scopes: Iterable[str]) -> dict[str, Any]:
        """Offer identifier candidates first and name candidates as low evidence; idempotent, never accepts."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        from src.kb.ownership_records import READ_SCOPE as OWNERSHIP_READ

        authorize(ownership_namespace, scopes, OWNERSHIP_READ)
        entities = self.service._entities(ownership_namespace, principal_id, scopes) \
            if table_exists(self.conn, "ownership_records") else []
        offered = []
        for subject in self.store.companies(namespace):
            if subject.get("redacted"):
                continue  # a natural person is never proposed (EX01 minimisation)
            left = {"record_key": subject["key"], "name_as_reported": subject["name_as_reported"],
                    "report_ids": subject["report_ids"], "ownership_namespace": ownership_namespace}
            published = {(scheme, _value(i["value"])) for i in subject.get("identifiers") or []
                         for scheme in _scheme_set(i["scheme"])}
            for entity in entities:
                body = entity["record"]
                right = {"record_key": body["record_key"], "provider": body["source"]["provider"],
                         "revision": entity["revision"]}
                right_entity = body.get("canonical_entity_id") or entity_for(body["record_key"])
                shared = [i for i in body.get("identifiers") or [] if (i["scheme"], _value(i["value"])) in published]
                if shared:
                    offered.append(self._offer(namespace, subject, body["record_key"], right_entity,
                                               "exact-identifier", {"identifiers": shared, "left": left,
                                                                    "right": right}, principal_id, scopes))
                    continue
                if not subject.get("name_as_reported") or _name(body.get("name")) != _name(
                        subject["name_as_reported"]):
                    continue
                offered.append(self._offer(namespace, subject, body["record_key"], right_entity, "name-jurisdiction", {
                    "normalized_name": _name(body.get("name")), "country": None,
                    "country_basis": "the report states where the company pays, not its jurisdiction",
                    "left": left, "right": right, "low_evidence": True,
                    "note": "a name is low evidence and never accepted automatically; a reviewer decides"},
                    principal_id, scopes))
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.company_candidates(namespace, scopes=scopes),
                "unmatched": self.unmatched_companies(namespace, scopes=scopes)}

    def _offer(self, namespace, subject, right_key, right_entity, basis, evidence, principal_id, scopes):
        return self.service.offer(namespace, left_key=subject["key"], right_key=right_key,
                                  left_entity=entity_for(subject["key"]), right_entity=right_entity, basis=basis,
                                  evidence=[{**evidence, "method": basis}], principal_id=principal_id, scopes=scopes)

    @staticmethod
    def company_view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        left, right = candidate["left_key"], candidate["right_key"]
        subject, other = (left, right) if left.startswith(SUBJECT_PREFIX) else (right, left)
        other_entity = candidate["right_entity"] if other == right else candidate["left_entity"]
        return {"candidate_id": candidate["candidate_id"], "state": candidate["state"],
                "method": candidate["basis"], "confidence": candidate["confidence"],
                "low_evidence": candidate["basis"] in NAME_BASES, "subject_key": subject, "ownership_key": other,
                "ownership_entity": other_entity, "decision_id": candidate["decision_id"],
                "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
                "reviewed_at_ms": last.get("at_ms") if candidate["state"] != "proposed" else None,
                "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
                "notice": "a reviewable identity decision; payment records are never merged or rewritten"}

    def company_candidates(self, namespace: str, *, scopes: Iterable[str], subject_key: str | None = None
                           ) -> list[dict[str, Any]]:
        from src.kb.ownership_store import OwnershipError

        try:
            rows = self.service.candidates(namespace, scopes=scopes, record_key=subject_key)
        except OwnershipError as exc:
            raise ExtractivesError(exc.code, str(exc)) from exc
        return [self.company_view(c) for c in rows
                if c["left_key"].startswith(SUBJECT_PREFIX + "company:")
                or c["right_key"].startswith(SUBJECT_PREFIX + "company:")]

    def _own(self, namespace, candidate_id, scopes):
        if not any(c["candidate_id"] == candidate_id for c in self.company_candidates(namespace, scopes=scopes)):
            raise ExtractivesError("not_found", "no extractives company candidate with that id")

    def review_company(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
                       scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        self._own(namespace, candidate_id, {"operator"})
        try:
            return self.company_view(self.service.review(namespace, candidate_id, decision, reason,
                                                         principal_id=principal_id,
                                                         scopes=scopes))
        except OwnershipError as exc:
            raise ExtractivesError(exc.code, str(exc)) from exc

    def revert_company(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
                       scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        self._own(namespace, candidate_id, {"operator"})
        try:
            return self.company_view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                                         scopes=scopes))
        except OwnershipError as exc:
            raise ExtractivesError(exc.code, str(exc)) from exc

    def unmatched_companies(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Reporting companies with no accepted match: kept as reported, with their pending candidate count."""
        views = self.company_candidates(namespace, scopes=scopes)
        out = []
        for subject in self.store.companies(namespace):
            mine = [v for v in views if v["subject_key"] == subject["key"]]
            if any(v["state"] == "accepted" for v in mine):
                continue
            out.append({"key": subject["key"], "name_as_reported": subject["name_as_reported"],
                        "redacted": bool(subject.get("redacted")), "report_ids": subject["report_ids"],
                        "pending_candidates": sum(v["state"] == "proposed" for v in mine), "status": "unmatched"})
        return out

    def accepted_company_links(self, namespace: str, ownership_keys: Iterable[str], *, scopes: Iterable[str]
                               ) -> list[dict[str, Any]]:
        wanted = set(ownership_keys)
        return [{k: v[k] for k in ("candidate_id", "subject_key", "ownership_key", "method", "low_evidence",
                                   "reviewer", "reviewed_at_ms", "decision_id")}
                for v in self.company_candidates(namespace, scopes=scopes)
                if v["state"] == "accepted" and ({v["ownership_key"], v["ownership_entity"]} & wanted)]


__all__ = ["CONTRACT", "KINDS", "ExtractivesIdentity", "aggregate"]
