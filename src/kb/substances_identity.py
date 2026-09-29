"""Reviewable substance identity across CAS, EC, InChIKey and DTXSID (#2212, CH07 #2300).

Each provider record of a substance (a PubChem CID, an ECHA substance or CLP
group entry, a CompTox DTXSID) is an identity *subject*, registered as its own
``canonical_entities`` row under an identifier-based id (no alias rows, so two
substances sharing a name never converge there). Subjects from different
providers are offered as **candidates** with a basis and the evidence that
connects them:

* ``exact-identifier`` - both publish the same well-formed CAS, EC, DTXSID or
  index number (check digits verified);
* ``inchikey`` - both publish the same standard InChIKey;
* ``synonym`` - a name or synonym one publishes equals, as an exact
  case-folded string, a name the other publishes, and no structural
  identifier connects them;
* ``reviewed-manual`` - a reviewer's own proposal with stated evidence, the
  only way a group entry, mixture, salt or isomer is ever connected to another
  substance.

Nothing is merged or linked automatically. Accepting or rejecting a candidate
is an entity identity decision (``match`` / ``non-match``) in
:class:`src.kb.entity_history.EntityHistoryStore`; reverting appends an
``undo`` there and returns the candidate to ``reverted``. Queries group
subjects only through accepted, unreverted candidates and always return the
match (basis, evidence, decision, reviewer) that connected them. Automatic
proposals never pair a composite subject (group, mixture, multi-component,
salt, isomer) with another kind of subject; such pairs are reported as
withheld. Subjects with no accepted match are reported as unmatched.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.substances_records import (
    COMPOSITE_KINDS,
    IDENTITY_CONTRACT,
    NAME_SCHEMES,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    SubstanceError,
    authorize,
    canonical,
    classify_query,
    digest,
)
from src.kb.substances_store import SubstanceStore, table_exists

BASES = ("exact-identifier", "inchikey", "synonym", "reviewed-manual")
STRENGTH = {"exact-identifier": 3, "inchikey": 3, "reviewed-manual": 2, "synonym": 1}
MATCH_SCHEMES = ("cas", "ec", "dtxsid", "index")
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS substance_identity_candidates (
  namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
  left_entity TEXT NOT NULL, right_entity TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL,
  origin TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  history_json TEXT NOT NULL, PRIMARY KEY(namespace, candidate_id)
);
"""


def provider_family(subject_key: str) -> str:
    return subject_key.split(":", 1)[0]


def entity_id(subject_key: str) -> str:
    """The identifier-based canonical entity id of one provider subject."""
    return "ent-substance-" + re.sub(r"[^a-z0-9]+", "-", subject_key.casefold()).strip("-")


class SubstanceIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = SubstanceStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "substance_identity_candidates")

    # ------------------------------------------------------------------ subjects

    def _profiles(self, namespace: str) -> dict[str, dict[str, Any]]:
        profiles: dict[str, dict[str, Any]] = {}
        for subject in self.store.subjects(namespace):
            profiles[subject["subject_key"]] = {**subject, "structural": {}, "names": {}}
        for row in self.store.identifiers(namespace):
            profile = profiles.get(row["subject_key"])
            if profile is None or row["value_key"] is None:
                continue
            bucket = "names" if row["scheme"] in NAME_SCHEMES else "structural"
            profile[bucket].setdefault(row["scheme"], {})[row["value_key"]] = {
                "value": row["value"], "revision_id": row["revision_id"], "conflict": row["conflict"]}
        return profiles

    def _register(self, subject: Mapping[str, Any]) -> str:
        from src.kb.entities import register_canonical_entity

        return register_canonical_entity(self.conn, entity_id(subject["subject_key"]),
                                         subject.get("name") or subject["subject_key"], "chemical_substance")

    # ------------------------------------------------------------------ proposals

    def _offer(self, namespace, left, right, basis, evidence, *, origin, principal_id):
        (a, b) = sorted((left["subject_key"], right["subject_key"]))
        candidate_id = "substance-idc:" + digest([namespace, a, b])[:24]
        row = self.conn.execute("SELECT state, basis, evidence_json, history_json FROM substance_identity_candidates "
                                "WHERE namespace=? AND candidate_id=?", [namespace, candidate_id]).fetchone()
        now = self.now()
        if row is None:
            left_entity, right_entity = self._register(left), self._register(right)
            entities = {left["subject_key"]: left_entity, right["subject_key"]: right_entity}
            self.conn.execute(
                "INSERT INTO substance_identity_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, candidate_id, a, b, entities[a], entities[b], basis, canonical(evidence), origin,
                 "proposed", None, principal_id, now,
                 canonical([{"state": "proposed", "by": principal_id, "at_ms": now, "basis": basis}])])
            return {"candidate_id": candidate_id, "change": "created"}
        state, old_basis, old_evidence, history = row[0], row[1], json.loads(row[2]), json.loads(row[3])
        changed = digest(evidence) != digest(old_evidence) or basis != old_basis
        if state == "proposed" and STRENGTH[basis] > STRENGTH[old_basis]:
            change = "upgraded"
        elif state in {"rejected", "reverted"} and changed:
            change = "reproposed"
        else:
            return {"candidate_id": candidate_id, "change": None}
        history.append({"state": "proposed", "by": principal_id, "at_ms": now, "change": change,
                        "previous_state": state, "previous_basis": old_basis})
        self.conn.execute("UPDATE substance_identity_candidates SET state='proposed', decision_id=NULL, basis=?, "
                          "evidence_json=?, origin=?, history_json=? WHERE namespace=? AND candidate_id=?",
                          [basis, canonical(evidence), origin, canonical(history), namespace, candidate_id])
        return {"candidate_id": candidate_id, "change": change}

    @staticmethod
    def _shared(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
        shared = []
        for scheme in (*MATCH_SCHEMES, "inchikey"):
            for key in sorted(set(left["structural"].get(scheme, {})) & set(right["structural"].get(scheme, {}))):
                lv, rv = left["structural"][scheme][key], right["structural"][scheme][key]
                shared.append({"scheme": scheme, "value": key, "left_revision": lv["revision_id"],
                               "right_revision": rv["revision_id"],
                               "conflicting_depositor_identifier": bool(lv["conflict"] or rv["conflict"])})
        if any(s["scheme"] in MATCH_SCHEMES for s in shared):
            return "exact-identifier", shared
        if shared:
            return "inchikey", shared
        names = []
        left_names = {k: v for scheme in left["names"].values() for k, v in scheme.items()}
        right_names = {k: v for scheme in right["names"].values() for k, v in scheme.items()}
        for key in sorted(set(left_names) & set(right_names)):
            names.append({"scheme": "name", "value": key, "left_published": left_names[key]["value"],
                          "right_published": right_names[key]["value"],
                          "left_revision": left_names[key]["revision_id"],
                          "right_revision": right_names[key]["revision_id"]})
        return ("synonym", names) if names else (None, [])

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Offer candidates between subjects of different providers; nothing is accepted or merged."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        profiles = self._profiles(namespace)
        keys = sorted(profiles)
        offered, withheld = [], []
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                left, right = profiles[a], profiles[b]
                if provider_family(a) == provider_family(b):
                    continue  # one provider's records are never matched to each other here
                basis, shared = self._shared(left, right)
                if basis is None:
                    continue
                composite = [s["subject_key"] for s in (left, right) if s["kind"] in COMPOSITE_KINDS]
                if composite and left["kind"] != right["kind"]:
                    withheld.append({"subjects": [a, b], "basis": basis, "composite": composite,
                                     "reason": "a group, mixture, salt or isomer entry is never proposed against "
                                               "another kind of substance; only an explicit reviewed proposal "
                                               "connects them"})
                    continue
                evidence = {"shared": shared, "left": {"subject_key": a, "kind": left["kind"], "name": left["name"]},
                            "right": {"subject_key": b, "kind": right["kind"], "name": right["name"]},
                            "method": basis,
                            "note": "a candidate is a reviewable proposal; the records stay separate"}
                offered.append(self._offer(namespace, left, right, basis, evidence, origin="automatic",
                                           principal_id=principal_id))
        return {"proposed": sorted(o["candidate_id"] for o in offered if o["change"]),
                "withheld": withheld, "candidates": self.candidates(namespace, scopes=scopes)}

    def propose_manual(self, namespace: str, left_key: str, right_key: str, evidence: str, *, principal_id: str,
                       scopes: Iterable[str]) -> dict[str, Any]:
        """A reviewer's explicit proposal (e.g. a CLP group entry to one member); still needs a review."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        profiles = self._profiles(namespace)
        if left_key not in profiles or right_key not in profiles or left_key == right_key:
            raise SubstanceError("not_found", "both subjects must be visible substance records")
        if not str(evidence or "").strip():
            raise SubstanceError("invalid_candidate", "a manual proposal states its evidence")
        left, right = profiles[left_key], profiles[right_key]
        record = {"shared": [], "stated": evidence.strip(), "method": "reviewed-manual",
                  "left": {"subject_key": left_key, "kind": left["kind"], "name": left["name"]},
                  "right": {"subject_key": right_key, "kind": right["kind"], "name": right["name"]},
                  "note": "an explicit proposal; composite entries are connected only this way"}
        result = self._offer(namespace, left, right, "reviewed-manual", record, origin="manual",
                             principal_id=principal_id)
        return self.candidate(namespace, result["candidate_id"], scopes=scopes)

    # ------------------------------------------------------------------ reviews

    def _row(self, namespace: str, candidate_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT candidate_id, left_key, right_key, left_entity, right_entity, basis, evidence_json, origin, state, "
            "decision_id, created_by, created_at_ms, history_json FROM substance_identity_candidates "
            "WHERE namespace=? AND candidate_id=?", [namespace, candidate_id]).fetchone() if self._ready() else None
        if row is None:
            raise SubstanceError("not_found", "identity candidate is not visible in this namespace")
        history = json.loads(row[12])
        reviewed = [h for h in history if h["state"] in {"accepted", "rejected", "reverted"}]
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace,
                **dict(zip(("candidate_id", "left_key", "right_key", "left_entity", "right_entity", "basis"), row[:6])),
                "evidence": json.loads(row[6]), "origin": row[7], "state": row[8], "decision_id": row[9],
                "created_by": row[10], "created_at_ms": row[11], "history": history,
                "reviewer": reviewed[-1]["by"] if reviewed and row[8] != "proposed" else None,
                "notice": "a candidate is a reviewable proposal; records are never merged"}

    def candidate(self, namespace: str, candidate_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        return self._row(namespace, candidate_id)

    def candidates(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                   subject_key: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT candidate_id FROM substance_identity_candidates WHERE namespace=? AND (? IS NULL OR state=?) AND "
            "(? IS NULL OR left_key=? OR right_key=?) ORDER BY candidate_id",
            [namespace, state, state, subject_key, subject_key, subject_key]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def _transition(self, namespace, candidate, state, decision_id, principal_id, reason):
        history = candidate["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                           "decision_id": decision_id}]
        self.conn.execute("UPDATE substance_identity_candidates SET state=?, decision_id=?, history_json=? "
                          "WHERE namespace=? AND candidate_id=?",
                          [state, decision_id, canonical(history), namespace, candidate["candidate_id"]])
        return self._row(namespace, candidate["candidate_id"])

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept (``match``) or reject (``non-match``) as an entity identity decision; never a merge."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise SubstanceError("invalid_decision", "accept or reject with a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] != "proposed":
            raise SubstanceError("invalid_state", f"candidate is {candidate['state']}; propose again to re-review")
        for side in ("left", "right"):
            self.history.register_entity(namespace, candidate[f"{side}_entity"], [candidate[f"{side}_key"]],
                                         principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match",
            [candidate["left_entity"], candidate["right_entity"]],
            {"candidate_id": candidate_id, "basis": candidate["basis"], "evidence": candidate["evidence"],
             "reason": reason.strip(),
             "provenance": {"producer": "chemicals.substances", "records": [candidate["left_key"],
                                                                            candidate["right_key"]]},
             "policy": {"merge": False, "note": "identity decision only; provider records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"substance-identity:{namespace}:{candidate_id}")
        return self._transition(namespace, candidate, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise SubstanceError("invalid_decision", "a revert needs a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] not in {"accepted", "rejected"}:
            raise SubstanceError("invalid_state", "only an accepted or rejected candidate can be reverted")
        undo = self.history.undo(namespace, candidate["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, candidate, "reverted", undo["decision_id"], principal_id, reason.strip())

    # ------------------------------------------------------------------ resolution

    def accepted(self, namespace: str) -> list[dict[str, Any]]:
        if not self._ready():
            return []
        rows = self.conn.execute("SELECT candidate_id FROM substance_identity_candidates WHERE namespace=? AND "
                                 "state='accepted' ORDER BY candidate_id", [namespace]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def clusters(self, namespace: str) -> dict[str, str]:
        """subject_key -> representative over accepted, unreverted candidates."""
        parent: dict[str, str] = {}

        def find(key: str) -> str:
            parent.setdefault(key, key)
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        for subject in self.store.subjects(namespace):
            find(subject["subject_key"])
        for candidate in self.accepted(namespace):
            a, b = find(candidate["left_key"]), find(candidate["right_key"])
            if a != b:
                parent[max(a, b)] = min(a, b)
        return {key: find(key) for key in list(parent)}

    def members(self, namespace: str, subject_key: str) -> list[str]:
        clusters = self.clusters(namespace)
        root = clusters.get(subject_key, subject_key)
        return sorted(k for k, v in clusters.items() if v == root) or [subject_key]

    def substance(self, namespace: str, subject_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """One reviewable substance: its member records, the accepted matches joining them and open candidates."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        members = self.members(namespace, subject_key)
        subjects = {s["subject_key"]: s for s in self.store.subjects(namespace)}
        matches = [c for c in self.accepted(namespace) if c["left_key"] in members and c["right_key"] in members]
        pending = [c for c in self.candidates(namespace, scopes=scopes, state="proposed")
                   if c["left_key"] in members or c["right_key"] in members]
        identifiers: dict[str, set[str]] = {}
        for member in members:
            for row in self.store.identifiers(namespace, member):
                if row["scheme"] not in NAME_SCHEMES:
                    identifiers.setdefault(row["scheme"], set()).add(row["value"])
        root = min(members)
        return {
            "substance_id": entity_id(root), "members": [
                {"subject_key": m, "provider_family": provider_family(m), "kind": subjects.get(m, {}).get("kind"),
                 "name": subjects.get(m, {}).get("name"), "providers": subjects.get(m, {}).get("providers", []),
                 "entity_id": entity_id(m)} for m in members],
            "identifiers": {k: sorted(v) for k, v in sorted(identifiers.items())},
            "matches": [{"candidate_id": c["candidate_id"], "records": [c["left_key"], c["right_key"]],
                         "basis": c["basis"], "evidence": c["evidence"], "decision_id": c["decision_id"],
                         "reviewer": c["reviewer"]} for c in matches],
            "pending_candidates": [{"candidate_id": c["candidate_id"], "records": [c["left_key"], c["right_key"]],
                                    "basis": c["basis"]} for c in pending],
            "state": "matched" if matches else "unmatched",
            "notice": "records join only through accepted, reviewable identity decisions; each keeps its source",
        }

    def resolve(self, namespace: str, query: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """A name, CAS, EC, index number, InChIKey or DTXSID to the reviewable substances that publish it."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        parsed = classify_query(query)
        if parsed is None:
            raise SubstanceError("invalid_request", "give a substance name, CAS, EC, InChIKey or DTXSID")
        scheme, key = parsed
        entry = self.store.find_subjects(namespace, scheme, key)
        clusters = self.clusters(namespace) if entry else {}
        roots = sorted({clusters.get(k, k) for k in entry})
        substances = []
        for root in roots:
            found = self.substance(namespace, root, scopes=scopes)
            found["entry_records"] = [k for k in entry if clusters.get(k, k) == root]
            substances.append(found)
        status = "resolved" if len(substances) == 1 else "ambiguous" if substances else "not_found"
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace, "query": query,
                "interpreted_as": scheme, "key": key, "status": status, "substances": substances,
                "coverage_notice": "only the bounded, acquired substance set is searched; not found is not a "
                                   "statement about the substance",
                "matching": "exact identifiers (check digits verified) or exact published names; never similarity"}

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Subjects no accepted match connects, with their identifiers and open candidates."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        clusters = self.clusters(namespace)
        sizes: dict[str, int] = {}
        for root in clusters.values():
            sizes[root] = sizes.get(root, 0) + 1
        pending = self.candidates(namespace, scopes=scopes, state="proposed")
        items = []
        for subject in self.store.subjects(namespace):
            key = subject["subject_key"]
            if sizes.get(clusters.get(key, key), 1) > 1:
                continue
            items.append({"subject_key": key, "kind": subject["kind"], "name": subject["name"],
                          "identifiers": sorted({f"{r['scheme']}:{r['value']}" for r in self.store.identifiers(
                              namespace, key) if r["scheme"] not in NAME_SCHEMES}),
                          "pending_candidates": sorted(c["candidate_id"] for c in pending
                                                       if key in (c["left_key"], c["right_key"]))})
        return {"namespace": namespace, "unmatched": items, "count": len(items)}


__all__ = ["BASES", "SubstanceIdentity", "entity_id", "provider_family"]
