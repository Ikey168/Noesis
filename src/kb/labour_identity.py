"""Places, sectors and occupations across labour sources through reviewable identity (#2219, LB07).

**Places.** Every area code a labour series states - ISO 3166-1 alpha-3 (ILOSTAT, OECD), Eurostat GEO codes
(countries and NUTS regions), ISO alpha-2 (BLS national surveys), BLS LAUS area codes and US FIPS state codes - is
offered to the Geospatial places the platform already holds (:class:`src.kb.geospatial.GeospatialStore`; no new
place store), matching by the published code only:

* ``published-code`` - a place whose source identifiers carry the same code under the scheme's key;
* ``iso-alpha2-equivalent`` - a two-letter Eurostat GEO code read as ISO 3166-1 alpha-2 (``EL`` is ``GR``);
* ``fips-from-laus-area`` - the FIPS state code positions 3-4 of a LAUS state area code carry (BLS LAUS area
  code layout).

Each proposal cites the place revision and the identifier key it rests on. A code whose candidates name more than
one place is ``ambiguous`` - a review candidate a reviewer resolves by choosing one of the cited candidates; a code
with none stays ``unmatched``. The NUTS hierarchy of :func:`src.kb.demographics_places.ancestry` tells a query which
regions lie within a requested country; nothing is aggregated or apportioned.

**Sectors and occupations.** A native code maps to another classification only through a published concordance
an operator imports with its citation (URL, publisher, publication date and file digest): NACE Rev.2 to ISIC Rev.4,
NAICS to ISIC Rev.4, SOC 2018 to ISCO-08 and so on. Each mapping keeps the relation the table states (``exact``,
``partial`` or ``one-to-many``) and the classification version of both sides; a code in the requested
classification and version maps to itself (``exact``, method ``same-classification``). Codes without a concordance
stay ``unmatched`` and remain queryable by their native code.

Every mapping is an assertion that is ``proposed`` (or ``ambiguous``), then ``accepted`` or ``rejected`` by a
reviewer - reviewer and time are recorded - and can be ``reverted``; queries use accepted mappings only. Names in
``canonical_entities`` equal to a place label are listed as context, never as a match.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.labour_statistics import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    LabourError,
    LabourStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-labour-identity-v1"
GEO_READ = "knowledge:geospatial:read"
KINDS = ("area", "sector", "occupation")
RELATIONS = ("exact", "partial", "one-to-many")
# The place source-identifier key carrying each area scheme's codes.
PLACE_KEYS = {
    "iso3166-1-alpha3": "iso3166-1-alpha3",
    "iso3166-1-alpha2": "iso3166-1-alpha2",
    "eurostat-geo": "nuts",
    "us-fips-state": "us-fips-state",
    "bls-laus-area": "bls-laus-area",
    "bls-oews-area": "bls-oews-area",
}
EUROSTAT_ISO2 = {"EL": "GR", "UK": "GB"}
_DDL = """
CREATE TABLE IF NOT EXISTS labour_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, method TEXT, relation TEXT, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
CREATE TABLE IF NOT EXISTS labour_concordances (
  namespace TEXT NOT NULL, concordance_id TEXT NOT NULL, label TEXT NOT NULL, source_json TEXT NOT NULL,
  target_json TEXT NOT NULL, citation_json TEXT NOT NULL, rows_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  recorded_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, concordance_id)
);
"""


def _classification(value: Mapping[str, Any]) -> dict[str, str]:
    value = dict(value or {})
    if not value.get("scheme") or not value.get("version"):
        raise LabourError("invalid_mapping", "a classification states its scheme and version")
    return {"scheme": str(value["scheme"]), "version": str(value["version"])}


class LabourIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = LabourStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ subjects

    def _areas(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "labour_series"):
            return []
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for (area,) in self.conn.execute(
            "SELECT area_json FROM labour_series WHERE namespace=? ORDER BY series_id", [namespace]
        ).fetchall():
            area = json.loads(area)
            entry = found.setdefault((area["scheme"], str(area["code"])), {**area, "labels": set()})
            if area.get("label"):
                entry["labels"].add(area["label"])
        return [{**v, "labels": sorted(v["labels"])} for _, v in sorted(found.items())]

    def _codes(self, namespace: str, kind: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "labour_series"):
            return []
        column = "sector_json" if kind == "sector" else "occupation_json"
        found = {}
        for (value,) in self.conn.execute(
            f"SELECT {column} FROM labour_series WHERE namespace=? AND {column} IS NOT NULL ORDER BY series_id",
            [namespace],
        ).fetchall():
            code = json.loads(value)
            found[(code["scheme"], code["version"], code["code"])] = {
                k: code.get(k) for k in ("scheme", "version", "code", "native", "label")
            }
        return [found[k] for k in sorted(found)]

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
        return [{"place_id": r[0], "revision_id": r[1], "name": r[2], "place_type": r[3],
                 "source_ids": {str(k): str(v) for k, v in json.loads(r[4] or "{}").items()}} for r in rows]

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

    @staticmethod
    def _candidates(area: Mapping[str, Any], places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scheme, code = area["scheme"], str(area["code"])
        rules: list[tuple[str, str, str]] = []  # (method, source-id key, value)
        if scheme in PLACE_KEYS:
            rules.append(("published-code", PLACE_KEYS[scheme], code))
        if scheme == "eurostat-geo":
            rules.append(("published-code", "eurostat-geo", code))
            if len(code) == 2:
                rules.append(("iso-alpha2-equivalent", "iso3166-1-alpha2", EUROSTAT_ISO2.get(code, code)))
        if scheme == "bls-laus-area" and code.startswith("ST"):
            rules.append(("fips-from-laus-area", "us-fips-state", code[2:4]))
        out = []
        for place in places:
            for method, key, value in rules:
                if place["source_ids"].get(key) == value:
                    out.append({"place_id": place["place_id"], "place_revision_id": place["revision_id"],
                                "place_name": place["name"], "place_type": place["place_type"], "method": method,
                                "evidence": {"source_id_key": key, "value": value,
                                             "rule": {"published-code": "the place carries the published code",
                                                      "iso-alpha2-equivalent": "Eurostat GEO country code read as "
                                                      "ISO 3166-1 alpha-2 (EL=GR, UK=GB)",
                                                      "fips-from-laus-area": "LAUS state area code ST+FIPS state "
                                                      "(BLS LAUS area-code layout)"}[method]}})
                    break
        return out

    def _latest(self, namespace: str, kind: str, subject_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT assertion_id FROM labour_identity_assertions WHERE namespace=? AND kind=? AND subject_key=? "
            "ORDER BY created_at_ms DESC, assertion_id DESC LIMIT 1",
            [namespace, kind, subject_key],
        ).fetchone()
        return None if row is None else self.assertion(namespace, row[0], scopes={"operator"})

    def _record(self, namespace, kind, subject, target, method, relation, evidence, state, reason, principal_id):
        subject_key = canonical(subject)
        latest = self._latest(namespace, kind, subject_key)
        if (latest is not None and latest["target"] == target and latest["method"] == method
                and latest["state"] in {state, "reverted"}):
            return latest["assertion_id"], False
        number = 1 + int(self.conn.execute(
            "SELECT count(*) FROM labour_identity_assertions WHERE namespace=? AND kind=? AND subject_key=?",
            [namespace, kind, subject_key]).fetchone()[0])
        assertion_id = "lb-identity:" + digest([namespace, kind, subject, number, target, method, state])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO labour_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject),
             None if target is None else canonical(target), method, relation, canonical(evidence), state, reason,
             canonical([{"state": state, "by": principal_id, "at_ms": now, "reason": reason}]), principal_id, now],
        )
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
                        "canonical_entities": self._canonical_names(area["labels"])}
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

    # ------------------------------------------------------------------ classifications

    def import_concordance(self, namespace: str, table: Mapping[str, Any], *, principal_id: str,
                           scopes: Iterable[str]) -> dict[str, Any]:
        """Record a published concordance with its citation; each row states its relation as published."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        table = dict(table)
        citation = dict(table.get("citation") or {})
        if not all(citation.get(k) for k in ("url", "publisher", "published_on", "file_sha256")):
            raise LabourError("invalid_table", "a concordance cites its URL, publisher, publication date and digest")
        source, target = _classification(table.get("source")), _classification(table.get("target"))
        rows = []
        for row in table.get("rows") or []:
            row = dict(row)
            if not row.get("source_code") or not row.get("target_code") or row.get("relation") not in RELATIONS:
                raise LabourError("invalid_table", f"each row names both codes and a relation in {RELATIONS}")
            rows.append({k: str(row[k]) for k in ("source_code", "target_code", "relation")}
                        | ({"note": str(row["note"])} if row.get("note") else {}))
        if not rows or not str(table.get("label") or "").strip():
            raise LabourError("invalid_table", "a concordance has a label and at least one row")
        rows.sort(key=lambda r: (r["source_code"], r["target_code"]))
        content_hash = digest([source, target, rows, citation])
        concordance_id = "lb-concordance:" + content_hash[:24]
        if not self.conn.execute("SELECT 1 FROM labour_concordances WHERE namespace=? AND concordance_id=?",
                                 [namespace, concordance_id]).fetchone():
            self.conn.execute(
                "INSERT INTO labour_concordances VALUES (?,?,?,?,?,?,?,?,?,?)",
                [namespace, concordance_id, str(table["label"]).strip(), canonical(source), canonical(target),
                 canonical(citation), canonical(rows), content_hash, principal_id, self.now()],
            )
        return self.concordance(namespace, concordance_id)

    def concordance(self, namespace: str, concordance_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT concordance_id, label, source_json, target_json, citation_json, rows_json, recorded_by FROM "
            "labour_concordances WHERE namespace=? AND concordance_id=?", [namespace, concordance_id]).fetchone()
        if row is None:
            raise LabourError("not_found", "concordance is not visible in this namespace")
        return {"concordance_id": row[0], "label": row[1], "source": json.loads(row[2]),
                "target": json.loads(row[3]), "citation": json.loads(row[4]), "rows": json.loads(row[5]),
                "recorded_by": row[6]}

    def concordances(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "labour_concordances"):
            return []
        return [self.concordance(namespace, r[0]) for r in self.conn.execute(
            "SELECT concordance_id FROM labour_concordances WHERE namespace=? ORDER BY label, concordance_id",
            [namespace]).fetchall()]

    def resolve_code(self, namespace: str, code: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
        """One native code's codes in the target classification, each citing the concordance row; nothing stored."""
        source = _classification(code)
        target = _classification(target)
        native = str(code["code"])
        if source == target:
            return {"targets": [{"code": native, "relation": "exact", "basis": "same classification and version"}],
                    "method": "same-classification", "relation": "exact"}
        found = []
        for table in self.concordances(namespace):
            forward = table["source"] == source and table["target"] == target
            backward = table["source"] == target and table["target"] == source
            if not forward and not backward:
                continue
            for row in table["rows"]:
                if (row["source_code"] if forward else row["target_code"]) != native:
                    continue
                relation = row["relation"]
                if backward and relation == "one-to-many":
                    relation = "partial"  # read from the many side, one code covers part of the other
                found.append({"code": row["target_code"] if forward else row["source_code"], "relation": relation,
                              "direction": "forward" if forward else "reverse",
                              "concordance": {"concordance_id": table["concordance_id"], "label": table["label"],
                                              "citation": table["citation"]}})
        if not found:
            return {"targets": [], "method": None, "relation": None,
                    "reason": "no published concordance between these classifications"}
        relations = {f["relation"] for f in found}
        relation = "one-to-many" if len(found) > 1 else found[0]["relation"]
        if len(found) == 1 and relations == {"exact"}:
            relation = "exact"
        return {"targets": found, "method": "published-concordance", "relation": relation}

    def propose_classifications(self, namespace: str, kind: str, target: Mapping[str, Any], *, principal_id: str,
                                scopes: Iterable[str]) -> dict[str, Any]:
        """Map every stated sector or occupation code to the target classification through published concordances."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if kind not in ("sector", "occupation"):
            raise LabourError("invalid_mapping", "kind is sector or occupation")
        target = _classification(target)
        created, out = [], []
        for code in self._codes(namespace, kind):
            subject = {"scheme": code["scheme"], "version": code["version"], "code": code["code"], "target": target}
            latest = self._latest(namespace, kind, canonical(subject))
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue
            resolved = self.resolve_code(namespace, code, target)
            if resolved["targets"]:
                mapped = {"classification": target, "codes": resolved["targets"]}
                state, reason = "proposed", None
            else:
                mapped, state, reason = None, "unmatched", resolved["reason"]
            assertion_id, new = self._record(namespace, kind, subject, mapped, resolved["method"],
                                             resolved["relation"], {"native": code}, state, reason, principal_id)
            if new:
                created.append(assertion_id)
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    # ------------------------------------------------------------------ review

    def assertion(self, namespace: str, assertion_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_json, target_json, method, relation, evidence_json, state, reason, "
            "history_json, created_by, created_at_ms FROM labour_identity_assertions WHERE namespace=? AND "
            "assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise LabourError("not_found", "identity assertion is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "assertion_id": row[0], "kind": row[1],
                "subject": json.loads(row[2]), "target": None if row[3] is None else json.loads(row[3]),
                "method": row[4], "relation": row[5], "evidence": json.loads(row[6]), "state": row[7],
                "reason": row[8], "history": json.loads(row[9]), "created_by": row[10], "created_at_ms": row[11]}

    def assertions(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "labour_identity_assertions"):
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM labour_identity_assertions WHERE namespace=? AND (? IS NULL OR kind=?) "
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
                "UPDATE labour_identity_assertions SET state=?, history_json=?, target_json=?, method=?, relation=? "
                "WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), canonical(target["target"]), target["method"], "exact", namespace,
                 item["assertion_id"]])
        else:
            self.conn.execute(
                "UPDATE labour_identity_assertions SET state=?, history_json=? WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), namespace, item["assertion_id"]])
        return self.assertion(namespace, item["assertion_id"], scopes={"operator"})

    def review(self, namespace, assertion_id, decision, reason, *, principal_id, scopes, place_id=None):
        """Accept or reject a proposal; an ambiguous place mapping is accepted by choosing a cited candidate."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise LabourError("invalid_decision", "accept or reject with a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] == "ambiguous":
            if decision == "reject":
                return self._transition(namespace, item, "rejected", principal_id, reason.strip())
            chosen = next((c for c in item["evidence"]["candidates"] if c["place_id"] == place_id), None)
            if chosen is None:
                raise LabourError("invalid_decision", "choose one of the cited candidate places")
            target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
            return self._transition(namespace, item, "accepted", principal_id, reason.strip(),
                                    target={"target": target, "method": chosen["method"]})
        if item["state"] != "proposed":
            raise LabourError("invalid_state", f"assertion is {item['state']}; only a proposed mapping is reviewed")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace, assertion_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise LabourError("invalid_decision", "a revert needs a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected"}:
            raise LabourError("invalid_state", "only an accepted or rejected mapping can be reverted")
        return self._transition(namespace, item, "reverted", principal_id, reason.strip())

    # ------------------------------------------------------------------ use by queries

    def area_codes_for_place(self, namespace: str, place_id: str) -> list[dict[str, Any]]:
        """The area codes accepted as this place, each with the assertion it rests on."""
        return [
            {"scheme": a["subject"]["scheme"], "code": a["subject"]["code"], "assertion_id": a["assertion_id"],
             "method": a["method"], "reviewed": a["history"][-1]}
            for a in self.assertions(namespace, scopes={"operator"}, kind="area", state="accepted")
            if a["target"]["place_id"] == place_id
        ]

    def place_for_area(self, namespace: str, scheme: str, code: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "labour_identity_assertions"):
            return None
        latest = self._latest(namespace, "area", canonical({"scheme": scheme, "code": str(code)}))
        return latest

    def codes_for(self, namespace: str, kind: str, target: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Native codes accepted as mapping to the target code (``target`` = scheme, version, code)."""
        classification = _classification(target)
        wanted = str(target["code"])
        out = []
        for a in self.assertions(namespace, scopes={"operator"}, kind=kind, state="accepted"):
            if a["subject"]["target"] != classification:
                continue
            for mapped in a["target"]["codes"]:
                if mapped["code"] == wanted:
                    out.append({"scheme": a["subject"]["scheme"], "version": a["subject"]["version"],
                                "code": a["subject"]["code"], "relation": mapped["relation"],
                                "assertion_id": a["assertion_id"], "concordance": mapped.get("concordance"),
                                "reviewed": a["history"][-1]})
        return out


__all__ = ["CONTRACT", "LabourIdentity", "PLACE_KEYS", "RELATIONS"]
