"""Reviewable identity for places, crises, organisations and conflict actors (HR07, #2260).

Records keep places, organisations, crises and actors exactly as their
sources published them. This module only *proposes* match assertions, each
with its method, evidence and confidence:

* **places** - a published place reference (``<scheme>:<code or name>``) to a
  ``geospatial`` place: p-codes and ISO3 codes by exact code against admin
  places imported from a COD-AB boundary file (the boundary vintage is
  recorded on the place and on every assertion); names by normalised name
  (lower confidence);
* **crises** - a ReliefWeb disaster to an HDX dataset whose tags carry the
  disaster's GLIDE number (``shared-identifier:glide``), or to another
  disaster sharing its GLIDE; linking them as one crisis is always an
  assertion, never a merge;
* **organisations** - ReliefWeb publishers and HDX organisations to
  ``canonical_entities`` (``platform.entity-identity``) by alias, and to each
  other by identical name;
* **actors** - coder-published actor labels to ``canonical_entities`` by
  alias; the label stays the coder's.

Every assertion is ``proposed`` until a *different* principal with
``knowledge:humanitarian:review`` accepts or rejects it with a reason; the
decision is recorded as an entity identity decision in
:class:`src.kb.entity_history.EntityHistoryStore` (``match``/``non-match``) and
can be reverted (``undo``). Nothing is auto-merged, no record is rewritten,
and subjects without an accepted assertion stay visible as unmatched. No new
entity or spatial store is created: places and geometries live in
:class:`src.kb.geospatial.GeospatialStore`.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.humanitarian_records import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE, HumanitarianError, canonical, digest
from src.kb.humanitarian_store import HumanitarianStore, authorize, table_exists

CONTRACT = "noesis-humanitarian-identity-assertion-v1"
GEO_READ = {"knowledge:geospatial:read"}
GEO_WRITE = {"knowledge:geospatial:read", "knowledge:geospatial:write"}
CONFIDENCE = {"exact-pcode": 0.95, "exact-iso3": 0.95, "shared-identifier:glide": 0.9, "entity-alias": 0.6,
              "normalised-name": 0.5, "same-name-across-sources": 0.4}
TARGET_KINDS = ("geospatial-place", "crisis", "canonical-entity", "organisation-ref")
_ENTITY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                  "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_SUFFIXES = re.compile(r"\b(state|province|governorate|region|locality|district|wilayat)\b")
_DDL = """
CREATE TABLE IF NOT EXISTS humanitarian_identity_assertions(
 namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_key TEXT NOT NULL,
 subject_json TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, target_json TEXT NOT NULL,
 method TEXT NOT NULL, confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL, boundary_vintage TEXT,
 state TEXT NOT NULL, decision_id TEXT, proposed_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 history_json TEXT NOT NULL, PRIMARY KEY(namespace, assertion_id));
"""


def fold(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode().casefold()
    text = _SUFFIXES.sub(" ", text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def place_ref_key(place: Mapping[str, Any]) -> str:
    code = place.get("code")
    scheme = place.get("scheme")
    if code and scheme in {"iso3", "cod-ab-pcode", "hdx-group"}:
        return f"place-ref:{'iso3' if scheme == 'hdx-group' else scheme}:{str(code).upper()}"
    if code and scheme == "gw":
        return f"place-ref:gw:{code}"
    return f"place-ref:name:{place.get('level') or 'unknown'}:{fold(place.get('name'))}"


def _entity_id(key: str) -> str:
    return "ent-hum-" + re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:280]


class HumanitarianIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore
        from src.kb.geospatial import GeospatialStore

        self.conn = conn
        self.store = HumanitarianStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.geo = GeospatialStore(conn, initialize=initialize, now=now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ boundaries

    def import_admin_boundaries(self, namespace: str, collection: Mapping[str, Any], *, vintage: str,
                                source_url: str, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Register a published COD-AB boundary file as geospatial admin places (with its vintage)."""

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        features = sorted(collection.get("features") or [], key=lambda f: int(f["properties"].get("admin_level", 9)))
        by_code: dict[str, str] = {}
        registered = []
        for feature in features:
            props = dict(feature.get("properties") or {})
            code, level = str(props.get("pcode") or ""), int(props.get("admin_level", -1))
            if not code or level < 0 or not props.get("name"):
                raise HumanitarianError("invalid_boundary", "each boundary feature needs pcode, admin_level and name")
            source_ids = {"cod-ab-pcode": code, "boundary_vintage": vintage}
            if props.get("iso3"):
                source_ids["iso3"] = str(props["iso3"]).upper()
            parent = by_code.get(str(props.get("parent_pcode") or ""))
            place = self.geo.register_place(
                namespace, str(props["name"]), "country" if level == 0 else f"admin{level}",
                names=[{"value": str(props["name"]), "language": "en", "kind": "canonical"}], source_ids=source_ids,
                parent_ids=[parent] if parent else [], principal_id=principal_id, scopes=GEO_WRITE,
                place_key=f"cod-ab:{code}:{vintage}", geometry=feature.get("geometry"),
                provenance={"boundary_vintage": vintage, "source_url": source_url, "producer": "humanitarian.core"})
            by_code[code] = place["place_id"]
            registered.append({"pcode": code, "place_id": place["place_id"], "admin_level": level})
        return {"vintage": vintage, "source_url": source_url, "places": registered}

    def admin_places(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        places = []
        for place_id, source_ids, name, place_type, parents in self.conn.execute(
                "SELECT r.place_id, r.source_ids_json, r.canonical_name, r.place_type, r.parent_ids_json "
                "FROM geospatial_place_revisions r JOIN geospatial_place_current c ON c.revision_id=r.revision_id "
                "WHERE r.namespace=? ORDER BY r.place_id", [namespace]).fetchall():
            ids = json.loads(source_ids)
            if "cod-ab-pcode" in ids:
                places.append({"place_id": place_id, "pcode": ids["cod-ab-pcode"], "iso3": ids.get("iso3"),
                               "boundary_vintage": ids.get("boundary_vintage"), "name": name, "place_type": place_type,
                               "parent_ids": json.loads(parents)})
        return places

    # ------------------------------------------------------------ proposals

    def _offer(self, namespace, subject_kind, subject_key, subject, target_kind, target_id, target, method, evidence,
               principal_id, boundary_vintage=None):
        assertion_id = "hum-idm:" + digest([namespace, subject_key, target_kind, target_id])[:24]
        row = self.conn.execute("SELECT state FROM humanitarian_identity_assertions WHERE namespace=? AND assertion_id=?",
                                [namespace, assertion_id]).fetchone()
        if row is not None:
            return None  # a reviewed or pending assertion is never re-proposed silently
        now = self.now()
        self.conn.execute(
            "INSERT INTO humanitarian_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, subject_kind, subject_key, canonical(subject), target_kind, target_id,
             canonical(target), method, CONFIDENCE[method], canonical(evidence), boundary_vintage, "proposed", None,
             principal_id, now, canonical([{"state": "proposed", "by": principal_id, "at_ms": now}])])
        return assertion_id

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Place references, organisations and actors named by current revisions, with the records citing them."""

        scopes = set(scopes)
        subjects: dict[str, dict[str, Any]] = {}

        def add(kind, key, value, revision):
            item = subjects.setdefault(key, {"kind": kind, "key": key, "value": value, "records": set()})
            item["records"].add((revision["record_key"], revision["revision_id"]))

        for revision in self.store.current(namespace, scopes=scopes):
            content = revision["content"]
            for place in content.get("places") or []:
                add("place", place_ref_key(place), {k: place.get(k) for k in ("name", "code", "scheme", "level")}, revision)
            for publisher in content.get("publishers") or []:
                add("organisation", f"org-ref:reliefweb:{fold(publisher.get('name'))}", dict(publisher), revision)
            organization = content.get("organization") or {}
            if organization.get("title") or organization.get("name"):
                add("organisation", f"org-ref:hdx:{fold(organization.get('title') or organization.get('name'))}",
                    dict(organization), revision)
            for actor in content.get("actors") or []:
                add("actor", f"actor-ref:{content['coding_source'].split('-')[0]}:{fold(actor['label'])}",
                    {"label": actor["label"], "coder_id": actor.get("coder_id"), "coding_source": content["coding_source"]},
                    revision)
        return {k: {**v, "records": [{"record_key": r, "revision_id": i} for r, i in sorted(v["records"])]}
                for k, v in sorted(subjects.items())}

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Offer candidate assertions from stated evidence only; nothing is accepted or merged."""

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        subjects = self.subjects(namespace, scopes=scopes)
        created = []
        admin = self.admin_places(namespace)
        for key, subject in subjects.items():
            if subject["kind"] == "place":
                created += self._place_candidates(namespace, key, subject, admin, principal_id)
            elif subject["kind"] in {"organisation", "actor"}:
                created += self._entity_candidates(namespace, key, subject, principal_id)
        created += self._organisation_pairs(namespace, subjects, principal_id)
        created += self._crisis_candidates(namespace, scopes, principal_id)
        return {"contract": CONTRACT, "proposed": sorted(a for a in created if a),
                "unmatched": self.unmatched(namespace, scopes=scopes),
                "policy": "candidates only; a different principal reviews each assertion; nothing is merged"}

    def _place_candidates(self, namespace, key, subject, admin, principal_id):
        value = subject["value"]
        evidence = {"published": value, "records": subject["records"]}
        created = []
        for place in admin:
            method = None
            if value.get("scheme") == "cod-ab-pcode" and str(value.get("code") or "").upper() == place["pcode"].upper():
                method = "exact-pcode"
            elif value.get("scheme") in {"iso3", "hdx-group"} and place.get("iso3") and \
                    str(value.get("code") or "").upper() == place["iso3"]:
                method = "exact-iso3"
            elif value.get("name") and fold(value["name"]) == fold(place["name"]) and \
                    (value.get("level") in {None, place["place_type"]} or place["place_type"] == "country"):
                method = "normalised-name"
            if method:
                created.append(self._offer(
                    namespace, "place", key, value, "geospatial-place", place["place_id"],
                    {"place_id": place["place_id"], "pcode": place["pcode"], "name": place["name"],
                     "place_type": place["place_type"]},
                    method, {**evidence, "boundary_vintage": place["boundary_vintage"]}, principal_id,
                    boundary_vintage=place["boundary_vintage"]))
        return created

    def _entity_candidates(self, namespace, key, subject, principal_id):
        if not table_exists(self.conn, "entity_aliases"):
            return []
        from src.kb.entities import resolve

        label = subject["value"].get("label") or subject["value"].get("title") or subject["value"].get("name")
        found = resolve(self.conn, str(label or ""))
        if not found:
            return []
        return [self._offer(namespace, subject["kind"], key, subject["value"], "canonical-entity", found["canonical_id"],
                            {"canonical_id": found["canonical_id"], "preferred_name": found["preferred_name"],
                             "entity_type": found["entity_type"]},
                            "entity-alias", {"published": label, "alias_method": found["method"],
                                             "alias_score": found["score"], "records": subject["records"]},
                            principal_id)]

    def _organisation_pairs(self, namespace, subjects, principal_id):
        created = []
        orgs = [s for s in subjects.values() if s["kind"] == "organisation"]
        for left in orgs:
            for right in orgs:
                if left["key"] < right["key"] and left["key"].split(":")[1] != right["key"].split(":")[1] and \
                        left["key"].split(":", 2)[2] == right["key"].split(":", 2)[2]:
                    created.append(self._offer(namespace, "organisation", left["key"], left["value"], "organisation-ref",
                                               right["key"], right["value"], "same-name-across-sources",
                                               {"left_records": left["records"], "right_records": right["records"]},
                                               principal_id))
        return created

    def _crisis_candidates(self, namespace, scopes, principal_id):
        created = []
        crises = self.store.current(namespace, scopes=scopes, record_type="crisis")
        datasets = self.store.current(namespace, scopes=scopes, record_type="dataset")
        for crisis in crises:
            glide = str(crisis["content"].get("glide") or "").lower()
            if not glide:
                continue
            for dataset in datasets:
                tags = {str(t).lower() for t in dataset["content"].get("tags") or []}
                if glide in tags:
                    created.append(self._offer(
                        namespace, "crisis", dataset["record_key"],
                        {"title": dataset["content"]["title"], "tags": sorted(tags)}, "crisis", crisis["record_key"],
                        {"name": crisis["content"]["name"], "glide": crisis["content"]["glide"]},
                        "shared-identifier:glide",
                        {"glide": crisis["content"]["glide"], "dataset_revision": dataset["revision_id"],
                         "crisis_revision": crisis["revision_id"]}, principal_id))
            for other in crises:
                if other["record_key"] > crisis["record_key"] and str(other["content"].get("glide") or "").lower() == glide:
                    created.append(self._offer(
                        namespace, "crisis", crisis["record_key"], {"name": crisis["content"]["name"]}, "crisis",
                        other["record_key"], {"name": other["content"]["name"]}, "shared-identifier:glide",
                        {"glide": crisis["content"]["glide"]}, principal_id))
        return created

    # ------------------------------------------------------------ reviews

    def _row(self, namespace: str, assertion_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT assertion_id, subject_kind, subject_key, subject_json, target_kind, target_id, target_json, method, "
            "confidence, evidence_json, boundary_vintage, state, decision_id, proposed_by, created_at_ms, history_json "
            "FROM humanitarian_identity_assertions WHERE namespace=? AND assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise HumanitarianError("not_found", "identity assertion is not visible in this namespace")
        history = json.loads(row[15])
        return {"contract": CONTRACT, "namespace": namespace, "assertion_id": row[0], "subject_kind": row[1],
                "subject_key": row[2], "subject": json.loads(row[3]), "target_kind": row[4], "target_id": row[5],
                "target": json.loads(row[6]), "method": row[7], "confidence": row[8], "evidence": json.loads(row[9]),
                "boundary_vintage": row[10], "state": row[11], "decision_id": row[12], "proposed_by": row[13],
                "created_at_ms": row[14], "history": history,
                "reviewer": history[-1].get("by") if row[11] != "proposed" else None,
                "notice": "an assertion links records by review; the records keep what the source published"}

    def _transition(self, namespace, assertion, state, decision_id, principal_id, reason):
        history = assertion["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                           "decision_id": decision_id}]
        self.conn.execute("UPDATE humanitarian_identity_assertions SET state=?, decision_id=?, history_json=? "
                          "WHERE namespace=? AND assertion_id=?",
                          [state, decision_id, canonical(history), namespace, assertion["assertion_id"]])
        return self._row(namespace, assertion["assertion_id"])

    def review(self, namespace: str, assertion_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept (``match``) or reject (``non-match``) as an entity identity decision; never self-review."""

        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise HumanitarianError("invalid_decision", "accept or reject with a reason")
        assertion = self._row(namespace, assertion_id)
        if assertion["state"] != "proposed":
            raise HumanitarianError("invalid_state", f"assertion is {assertion['state']}")
        if assertion["proposed_by"] == principal_id:
            raise HumanitarianError("self_review", "the proposer cannot review their own assertion")
        left, right = _entity_id(assertion["subject_key"]), _entity_id(f"{assertion['target_kind']}:{assertion['target_id']}")
        for entity, alias in ((left, assertion["subject_key"]), (right, assertion["target_id"])):
            self.history.register_entity(namespace, entity, [alias], principal_id=principal_id, scopes=_ENTITY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [left, right],
            {"assertion_id": assertion_id, "method": assertion["method"], "confidence": assertion["confidence"],
             "evidence": assertion["evidence"], "boundary_vintage": assertion["boundary_vintage"], "reason": reason.strip(),
             "provenance": {"producer": "humanitarian.core"},
             "policy": {"merge": False, "note": "identity assertion only; records stay as published"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_SCOPES,
            event_key=f"humanitarian-identity:{namespace}:{assertion_id}:{len(assertion['history'])}")
        return self._transition(namespace, assertion, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, assertion_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise HumanitarianError("invalid_decision", "a revert needs a reason")
        assertion = self._row(namespace, assertion_id)
        if assertion["state"] not in {"accepted", "rejected"}:
            raise HumanitarianError("invalid_state", "only an accepted or rejected assertion can be reverted")
        undo = self.history.undo(namespace, assertion["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_SCOPES)
        return self._transition(namespace, assertion, "reverted", undo["decision_id"], principal_id, reason.strip())

    # ------------------------------------------------------------ reads

    def assertions(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                   subject_key: str | None = None, target_kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "humanitarian_identity_assertions"):
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM humanitarian_identity_assertions WHERE namespace=? AND (? IS NULL OR state=?) "
            "AND (? IS NULL OR subject_key=? OR target_id=?) AND (? IS NULL OR target_kind=?) ORDER BY assertion_id",
            [namespace, state, state, subject_key, subject_key, subject_key, target_kind, target_kind]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def accepted_place_links(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """place-ref key -> accepted geospatial places (with the boundary vintage of each assertion)."""

        links: dict[str, list[dict[str, Any]]] = {}
        for a in self.assertions(namespace, scopes=scopes, state="accepted", target_kind="geospatial-place"):
            links.setdefault(a["subject_key"], []).append({"place_id": a["target_id"], "assertion_id": a["assertion_id"],
                                                           "method": a["method"], "boundary_vintage": a["boundary_vintage"],
                                                           "decision_id": a["decision_id"]})
        return links

    def place_refs_within(self, namespace: str, place_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Place refs linked (by accepted assertions) to the place or to places whose parent chain reaches it."""

        admin = {p["place_id"]: p for p in self.admin_places(namespace)}
        covered = {place_id}
        changed = True
        while changed:
            changed = False
            for p in admin.values():
                if p["place_id"] not in covered and set(p["parent_ids"]) & covered:
                    covered.add(p["place_id"])
                    changed = True
        refs, vintages = {}, set()
        for ref, links in self.accepted_place_links(namespace, scopes=scopes).items():
            for link in links:
                if link["place_id"] in covered:
                    refs[ref] = link
                    vintages.add(link["boundary_vintage"])
        return {"place_id": place_id, "places": sorted(covered), "refs": refs,
                "boundary_vintages": sorted(v for v in vintages if v)}

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Subjects without an accepted assertion stay visible (pending candidates are listed)."""

        accepted, pending = set(), {}
        for a in self.assertions(namespace, scopes=scopes):
            if a["state"] == "accepted":
                accepted |= {a["subject_key"], a["target_id"]}
            elif a["state"] == "proposed":
                pending.setdefault(a["subject_key"], []).append(a["assertion_id"])
        return [{"subject_key": key, "kind": s["kind"], "published": s["value"], "state": "unmatched",
                 "pending_assertions": sorted(pending.get(key, []))}
                for key, s in self.subjects(namespace, scopes=scopes).items() if key not in accepted]
