"""Places and classifications across business statistics sources through reviewable identity (#2738, IB06).

**Places.** Every area a series states - Eurostat GEO codes (``DE``) and US FIPS state codes (CBP ``06``) - is offered
to the Geospatial places the platform already holds (:class:`src.kb.geospatial.GeospatialStore`; no new place store),
matching by the published code only:

* ``published-code`` - a place whose source identifiers carry the same code under the scheme's key (``nuts``,
  ``eurostat-geo``, ``us-fips-state``);
* ``iso-alpha2-equivalent`` - a two-letter Eurostat GEO code read as ISO 3166-1 alpha-2 (``EL`` is ``GR``).

A code whose candidates name more than one place is ``ambiguous`` (a reviewer chooses one of the cited candidates); a
code with none stays ``unmatched``. Names are never a match.

**Classifications.** NACE Rev.2 and NAICS (each vintage) are never merged. A pair of stated codes becomes a
*candidate link* only through published concordances an operator imports with their citation (URL, publisher,
publication date and file digest) - this module's own imports and, read-only, the concordances the labour track's
LB07 import holds (``labour_concordances``) - either directly (2017 NAICS to 2022 NAICS) or through a shared pivot
classification both sides map to (NACE Rev.2 ``C`` and NAICS ``31-33`` both map to ISIC Rev.4 ``C``). Each candidate
keeps the relation the tables state (``exact``, ``partial`` or ``one-to-many``; a path through a pivot is ``exact``
only when both legs are exact) and cites every row it rests on. Series keep their own classification; a link never
merges, re-classifies or sums series.

Every assertion is ``proposed`` (or ``ambiguous``), then ``accepted`` or ``rejected`` by a reviewer - reviewer, reason
and time are recorded - and can be ``reverted``; queries use accepted assertions only. Unlinked codes stay queryable by
their native code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from itertools import combinations
from typing import Any

from src.kb.business_statistics_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    BusinessError,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)
from src.kb.business_statistics_store import BusinessStatisticsStore

CONTRACT = "noesis-business-identity-v1"
GEO_READ = "knowledge:geospatial:read"
KINDS = ("area", "classification")
RELATIONS = ("exact", "partial", "one-to-many")
STATES = ("proposed", "ambiguous", "unmatched", "accepted", "rejected", "reverted")
# The place source-identifier keys carrying each area scheme's codes.
PLACE_KEYS = {"eurostat-geo": ("nuts", "eurostat-geo"), "us-fips-state": ("us-fips-state",)}
EUROSTAT_ISO2 = {"EL": "GR", "UK": "GB"}
_DDL = """
CREATE TABLE IF NOT EXISTS business_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, method TEXT, relation TEXT, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
CREATE TABLE IF NOT EXISTS business_concordances (
  namespace TEXT NOT NULL, concordance_id TEXT NOT NULL, label TEXT NOT NULL, source_json TEXT NOT NULL,
  target_json TEXT NOT NULL, citation_json TEXT NOT NULL, rows_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  recorded_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, concordance_id)
);
"""


def _classification(value: Mapping[str, Any]) -> dict[str, str]:
    value = dict(value or {})
    if not value.get("scheme") or not value.get("version"):
        raise BusinessError("invalid_mapping", "a classification states its scheme and version")
    return {"scheme": str(value["scheme"]), "version": str(value["version"])}


def code_ref(value: Mapping[str, Any]) -> dict[str, str]:
    return {**_classification(value), "code": str(value["code"])}


class BusinessIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = BusinessStatisticsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "business_identity_assertions")

    # ------------------------------------------------------------------ subjects

    def _areas(self, namespace: str) -> list[dict[str, Any]]:
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for series in self.store.find_series(namespace):
            area = series["area"]
            entry = found.setdefault((area["scheme"], str(area["code"])), {**area, "labels": set()})
            if area.get("label"):
                entry["labels"].add(area["label"])
        return [{**v, "labels": sorted(v["labels"])} for _, v in sorted(found.items())]

    def stated_codes(self, namespace: str) -> list[dict[str, str]]:
        found = {}
        for series in self.store.find_series(namespace):
            ref = code_ref(series["classification"])
            found[canonical(ref)] = {**ref, **({"label": series["classification"]["label"]}
                                               if series["classification"].get("label") else {})}
        return [found[k] for k in sorted(found)]

    def _places(self, geo_namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, r.revision_id, r.canonical_name, r.place_type, r.source_ids_json FROM "
            "geospatial_places p JOIN geospatial_place_current c ON c.place_id=p.place_id JOIN "
            "geospatial_place_revisions r ON r.revision_id=c.revision_id WHERE p.namespace IN (?, 'global') "
            "ORDER BY p.place_id", [geo_namespace]).fetchall()
        return [{"place_id": r[0], "revision_id": r[1], "name": r[2], "place_type": r[3],
                 "source_ids": {str(k): str(v) for k, v in json.loads(r[4] or "{}").items()}} for r in rows]

    @staticmethod
    def _candidates(area: Mapping[str, Any], places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scheme, code = area["scheme"], str(area["code"])
        rules = [("published-code", key, code) for key in PLACE_KEYS.get(scheme, ())]
        if scheme == "eurostat-geo" and len(code) == 2:
            rules.append(("iso-alpha2-equivalent", "iso3166-1-alpha2", EUROSTAT_ISO2.get(code, code)))
        out = []
        for place in places:
            for method, key, value in rules:
                if place["source_ids"].get(key) == value:
                    out.append({"place_id": place["place_id"], "place_revision_id": place["revision_id"],
                                "place_name": place["name"], "place_type": place["place_type"], "method": method,
                                "evidence": {"source_id_key": key, "value": value,
                                             "rule": "the place carries the published code" if method ==
                                             "published-code" else "Eurostat GEO country code read as ISO 3166-1 "
                                                                   "alpha-2 (EL=GR, UK=GB)"}})
                    break
        return out

    def _latest(self, namespace: str, kind: str, subject_key: str) -> dict[str, Any] | None:
        if not self.ready():
            return None
        row = self.conn.execute(
            "SELECT assertion_id FROM business_identity_assertions WHERE namespace=? AND kind=? AND subject_key=? "
            "ORDER BY created_at_ms DESC, assertion_id DESC LIMIT 1", [namespace, kind, subject_key]).fetchone()
        return None if row is None else self.assertion(namespace, row[0], scopes={"operator"})

    def _record(self, namespace, kind, subject, target, method, relation, evidence, state, reason, principal_id):
        subject_key = canonical(subject)
        latest = self._latest(namespace, kind, subject_key)
        if latest is not None and latest["target"] == target and latest["method"] == method and \
                latest["state"] in {state, "reverted"}:
            return latest["assertion_id"], False
        number = 1 + int(self.conn.execute(
            "SELECT count(*) FROM business_identity_assertions WHERE namespace=? AND kind=? AND subject_key=?",
            [namespace, kind, subject_key]).fetchone()[0])
        assertion_id = "bs-identity:" + digest([namespace, kind, subject, number, target, method, state])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO business_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject),
             None if target is None else canonical(target), method, relation, canonical(evidence), state, reason,
             canonical([{"state": state, "by": principal_id, "at_ms": now, "reason": reason}]), principal_id, now])
        return assertion_id, True

    # ------------------------------------------------------------------ places

    def propose_places(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                       geo_namespace: str = "global") -> dict[str, Any]:
        """Offer every stated area code to Geospatial places by the published code; idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        places = self._places(geo_namespace)
        created, out = [], []
        for area in self._areas(namespace):
            subject = {"scheme": area["scheme"], "code": str(area["code"])}
            latest = self._latest(namespace, "area", canonical(subject))
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue
            candidates = self._candidates(area, places)
            evidence = {"labels": area["labels"], "candidates": candidates,
                        "note": "names are context only; matching rests on published codes"}
            distinct = {c["place_id"] for c in candidates}
            if len(distinct) == 1:
                chosen = candidates[0]
                target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
                method, state, reason = chosen["method"], "proposed", None
            elif distinct:
                target, method, state = None, None, "ambiguous"
                reason = "more than one place carries this code; a reviewer chooses one of the cited candidates"
            else:
                target, method, state, reason = None, None, "unmatched", "no place carries this code"
            assertion_id, new = self._record(namespace, "area", subject, target, method,
                                             "exact" if target else None, evidence, state, reason, principal_id)
            if new:
                created.append(assertion_id)
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    # ------------------------------------------------------------------ concordances

    def import_concordance(self, namespace: str, table: Mapping[str, Any], *, principal_id: str,
                           scopes: Iterable[str]) -> dict[str, Any]:
        """Record a published concordance with its citation; each row states its relation as published."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        table = dict(table)
        citation = dict(table.get("citation") or {})
        if not all(citation.get(k) for k in ("url", "publisher", "published_on", "file_sha256")):
            raise BusinessError("invalid_table", "a concordance cites its URL, publisher, publication date and digest")
        source, target = _classification(table.get("source")), _classification(table.get("target"))
        rows = []
        for row in table.get("rows") or []:
            row = dict(row)
            if not row.get("source_code") or not row.get("target_code") or row.get("relation") not in RELATIONS:
                raise BusinessError("invalid_table", f"each row names both codes and a relation in {RELATIONS}")
            rows.append({k: str(row[k]) for k in ("source_code", "target_code", "relation")}
                        | ({"note": str(row["note"])} if row.get("note") else {}))
        if not rows or not str(table.get("label") or "").strip():
            raise BusinessError("invalid_table", "a concordance has a label and at least one row")
        rows.sort(key=lambda r: (r["source_code"], r["target_code"]))
        content_hash = digest([source, target, rows, citation])
        concordance_id = "bs-concordance:" + content_hash[:24]
        if not self.conn.execute("SELECT 1 FROM business_concordances WHERE namespace=? AND concordance_id=?",
                                 [namespace, concordance_id]).fetchone():
            self.conn.execute("INSERT INTO business_concordances VALUES (?,?,?,?,?,?,?,?,?,?)",
                              [namespace, concordance_id, str(table["label"]).strip(), canonical(source),
                               canonical(target), canonical(citation), canonical(rows), content_hash, principal_id,
                               self.now()])
        return next(c for c in self.concordances(namespace) if c["concordance_id"] == concordance_id)

    def concordances(self, namespace: str) -> list[dict[str, Any]]:
        """This provider's imported concordances and, read-only, the labour track's LB07 imports."""
        out = []
        for table, origin in (("business_concordances", "economics.business"),
                              ("labour_concordances", "economics.labour (LB07 operator import)")):
            if not table_exists(self.conn, table):
                continue
            for row in self.conn.execute(
                    f"SELECT concordance_id, label, source_json, target_json, citation_json, rows_json, recorded_by "
                    f"FROM {table} WHERE namespace=? ORDER BY label, concordance_id", [namespace]).fetchall():
                out.append({"concordance_id": row[0], "label": row[1], "source": json.loads(row[2]),
                            "target": json.loads(row[3]), "citation": json.loads(row[4]), "rows": json.loads(row[5]),
                            "recorded_by": row[6], "held_by": origin})
        return out

    def _mappings(self, namespace: str, ref: Mapping[str, str]) -> list[dict[str, Any]]:
        """Every code a native code maps to through any held concordance, in either direction, each cited."""
        source = _classification(ref)
        out = []
        for table in self.concordances(namespace):
            forward, backward = table["source"] == source, table["target"] == source
            if not forward and not backward:
                continue
            for row in table["rows"]:
                if (row["source_code"] if forward else row["target_code"]) != ref["code"]:
                    continue
                relation = row["relation"]
                if backward and relation == "one-to-many":
                    relation = "partial"
                other = table["target"] if forward else table["source"]
                out.append({"code": {**other, "code": row["target_code"] if forward else row["source_code"]},
                            "relation": relation, "row": row,
                            "concordance": {"concordance_id": table["concordance_id"], "label": table["label"],
                                            "citation": table["citation"], "held_by": table["held_by"]}})
        return out

    def resolve_pair(self, namespace: str, left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
        """How two stated codes relate through published concordances (directly or through a shared pivot)."""
        left, right = code_ref(left), code_ref(right)
        direct = [m for m in self._mappings(namespace, left) if m["code"] == right]
        if direct:
            relation = direct[0]["relation"] if len(direct) == 1 else "one-to-many"
            return {"method": "published-concordance", "relation": relation, "paths": [
                {"via": None, "legs": [{k: m[k] for k in ("relation", "row", "concordance")}]} for m in direct]}
        paths = []
        right_maps = self._mappings(namespace, right)
        for leg in self._mappings(namespace, left):
            for other in right_maps:
                if leg["code"] == other["code"]:
                    paths.append({"via": leg["code"], "legs": [{k: m[k] for k in ("relation", "row", "concordance")}
                                                               for m in (leg, other)]})
        if not paths:
            return {"method": None, "relation": None, "paths": [],
                    "reason": "no published concordance connects these codes"}
        exact = all(leg["relation"] == "exact" for path in paths for leg in path["legs"]) and len(paths) == 1
        return {"method": "published-concordance-via-pivot", "relation": "exact" if exact else "partial",
                "paths": paths}

    def propose_classification_links(self, namespace: str, *, principal_id: str,
                                      scopes: Iterable[str]) -> dict[str, Any]:
        """Candidate links between stated codes of different classifications or vintages; nothing is merged."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        codes = self.stated_codes(namespace)
        created, out, linked = [], [], set()
        for left, right in combinations(codes, 2):
            if (left["scheme"], left["version"]) == (right["scheme"], right["version"]):
                continue
            a, b = code_ref(left), code_ref(right)
            resolved = self.resolve_pair(namespace, a, b)
            if not resolved["paths"]:
                continue
            subject = {"pair": sorted([a, b], key=canonical)}
            latest = self._latest(namespace, "classification", canonical(subject))
            linked |= {canonical(a), canonical(b)}
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue
            assertion_id, new = self._record(
                namespace, "classification", subject, {"pair": subject["pair"], "merge": False},
                resolved["method"], resolved["relation"], {"paths": resolved["paths"], "labels": {
                    canonical(code_ref(c)): c.get("label") for c in (left, right)}}, "proposed", None, principal_id)
            if new:
                created.append(assertion_id)
            out.append(assertion_id)
        unlinked = [c for c in codes if canonical(code_ref(c)) not in linked]
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out],
                "unlinked_codes": unlinked,
                "note": "candidate links only; every series keeps its own classification and nothing is merged"}

    # ------------------------------------------------------------------ review

    def assertion(self, namespace: str, assertion_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_json, target_json, method, relation, evidence_json, state, reason, "
            "history_json, created_by, created_at_ms FROM business_identity_assertions WHERE namespace=? AND "
            "assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise BusinessError("not_found", "identity assertion is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "assertion_id": row[0], "kind": row[1],
                "subject": json.loads(row[2]), "target": None if row[3] is None else json.loads(row[3]),
                "method": row[4], "relation": row[5], "evidence": json.loads(row[6]), "state": row[7],
                "reason": row[8], "history": json.loads(row[9]), "created_by": row[10], "created_at_ms": row[11]}

    def assertions(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM business_identity_assertions WHERE namespace=? AND (? IS NULL OR kind=?) "
            "ORDER BY kind, subject_key, created_at_ms, assertion_id", [namespace, kind, kind]).fetchall()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for (assertion_id,) in rows:
            item = self.assertion(namespace, assertion_id, scopes={"operator"})
            latest[(item["kind"], canonical(item["subject"]))] = item
        return [a for a in latest.values() if state is None or a["state"] == state]

    def _transition(self, namespace, item, state, principal_id, reason, target=None):
        history = item["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        if target is not None:
            self.conn.execute(
                "UPDATE business_identity_assertions SET state=?, history_json=?, target_json=?, method=?, "
                "relation=? WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), canonical(target["target"]), target["method"], "exact", namespace,
                 item["assertion_id"]])
        else:
            self.conn.execute(
                "UPDATE business_identity_assertions SET state=?, history_json=? WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), namespace, item["assertion_id"]])
        return self.assertion(namespace, item["assertion_id"], scopes={"operator"})

    def review(self, namespace, assertion_id, decision, reason, *, principal_id, scopes, place_id=None):
        """Accept or reject a proposal; an ambiguous place is accepted by choosing one of the cited candidates."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise BusinessError("invalid_decision", "accept or reject with a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] == "ambiguous":
            if decision == "reject":
                return self._transition(namespace, item, "rejected", principal_id, reason.strip())
            chosen = next((c for c in item["evidence"]["candidates"] if c["place_id"] == place_id), None)
            if chosen is None:
                raise BusinessError("invalid_decision", "choose one of the cited candidate places")
            target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
            return self._transition(namespace, item, "accepted", principal_id, reason.strip(),
                                    target={"target": target, "method": chosen["method"]})
        if item["state"] != "proposed":
            raise BusinessError("invalid_state", f"assertion is {item['state']}; only a proposal is reviewed")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace, assertion_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise BusinessError("invalid_decision", "a revert needs a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected"}:
            raise BusinessError("invalid_state", "only an accepted or rejected assertion can be reverted")
        return self._transition(namespace, item, "reverted", principal_id, reason.strip())

    # ------------------------------------------------------------------ use by queries and links

    def area_codes_for_place(self, namespace: str, place_id: str) -> list[dict[str, Any]]:
        """The area codes accepted as this place, each with the assertion it rests on."""
        return [{"scheme": a["subject"]["scheme"], "code": a["subject"]["code"], "assertion_id": a["assertion_id"],
                 "method": a["method"], "reviewed": a["history"][-1]}
                for a in self.assertions(namespace, scopes={"operator"}, kind="area", state="accepted")
                if a["target"]["place_id"] == place_id]

    def place_for_area(self, namespace: str, scheme: str, code: str) -> dict[str, Any] | None:
        """The accepted place of an area code (``None`` unless a reviewer accepted one)."""
        latest = self._latest(namespace, "area", canonical({"scheme": scheme, "code": str(code)}))
        if latest is None or latest["state"] != "accepted":
            return None
        return {**latest["target"], "assertion_id": latest["assertion_id"]}

    def linked_codes(self, namespace: str, ref: Mapping[str, Any], *, state: str = "accepted") -> list[dict[str, Any]]:
        """Codes of other classifications linked to ``ref`` by assertions in ``state`` (accepted by default)."""
        wanted = code_ref(ref)
        out = []
        for a in self.assertions(namespace, scopes={"operator"}, kind="classification", state=state):
            pair = a["subject"]["pair"]
            if wanted in pair:
                other = pair[1] if pair[0] == wanted else pair[0]
                out.append({**other, "relation": a["relation"], "method": a["method"],
                            "assertion_id": a["assertion_id"], "state": a["state"], "reviewed": a["history"][-1]})
        return out

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        return [a for a in self.assertions(namespace, scopes=scopes) if a["state"] in {"unmatched", "ambiguous",
                                                                                      "proposed", "rejected",
                                                                                      "reverted"}]


__all__ = ["CONTRACT", "KINDS", "PLACE_KEYS", "RELATIONS", "BusinessIdentity", "code_ref"]
