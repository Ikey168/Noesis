"""Reviewable taxon identity across Catalogue of Life, GBIF and IUCN (#2220, BD06 #2520).

Every provider taxon key (CoL ID, GBIF ``taxonKey``, IUCN SIS id) is an
identity *subject* registered as its own ``canonical_entities`` row under an
identifier-based id (no alias rows), exactly as the Climate and Environment
operator identities and the Fisheries vessel identities do
(:mod:`src.kb.environment_identity`, :mod:`src.kb.fisheries_identity`).
Subjects of different providers are connected only by **reviewed match
proposals**; nothing is merged and nothing is accepted automatically:

* ``cross-reference`` - one provider publishes the other's native key
  (e.g. a GBIF backbone identifier naming a CoL ID) - deterministic;
* ``name-authorship`` - exact scientific name plus equal authorship -
  deterministic;
* ``name-only`` - equal name, authorship missing or different -
  **lower-evidence**;
* ``synonym`` - one side's name is the accepted name a checklist states for
  the other side's synonym - **lower-evidence**.

Split and lumped concepts surface as **conflicts** on the proposals (one taxon
proposed against several taxa of another provider, or one provider treating
the name as accepted where another treats it as a synonym); they are shown to
the reviewer and never resolved automatically. Accepting or rejecting records
the reviewer and time, an entity identity decision in
:class:`src.kb.entity_history.EntityHistoryStore`, and - on acceptance - the
checklist version of both sides. Unmatched provider taxa stay queryable by
their native key.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.biodiversity_records import (
    IDENTITY_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    BiodiversityError,
    authorize,
    authorship_key,
    canonical,
    digest,
    name_key,
)
from src.kb.biodiversity_store import BiodiversityStore, table_exists

BASES = ("cross-reference", "name-authorship", "name-only", "synonym")
STRENGTH = {"cross-reference": 4, "name-authorship": 3, "name-only": 2, "synonym": 1}
EVIDENCE_CLASS = {"cross-reference": "deterministic", "name-authorship": "deterministic",
                  "name-only": "lower-evidence", "synonym": "lower-evidence"}
SCHEME_PROVIDER = {"col-id": "col", "gbif-taxon-key": "gbif", "iucn-sis-id": "iucn"}
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS biodiversity_identity_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
  left_entity TEXT NOT NULL, right_entity TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL,
  conflicts_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, checklists_json TEXT,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL,
  PRIMARY KEY(namespace, match_id)
);
"""


def entity_id(subject_key: str) -> str:
    return "ent-taxon-" + re.sub(r"[^a-z0-9]+", "-", subject_key.casefold()).strip("-")


def provider_of(subject_key: str) -> str:
    return subject_key.split(":", 1)[0]


class BiodiversityIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = BiodiversityStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "biodiversity_identity_matches")

    # ------------------------------------------------------------------ profiles

    def profiles(self, namespace: str) -> dict[str, dict[str, Any]]:
        """Latest published identity per provider taxon key (plus the synonym's accepted name a checklist states)."""
        result: dict[str, dict[str, Any]] = {}
        for record in self.store.records(namespace, record_type="taxon_identity"):
            revision = self.store.current(namespace, record["record_id"])
            published = revision["statement"]["as_published"]
            result[record["subject_key"]] = {
                "subject_key": record["subject_key"], "provider": record["provider"],
                "native_key": published["native_key"], "scientific_name": published["scientific_name"],
                "authorship": published.get("authorship"), "name": name_key(published["scientific_name"]),
                "author": authorship_key(published.get("authorship")), "status": published["status"],
                "status_class": published["status_class"], "accepted_name": published.get("accepted_name"),
                "refs": {(SCHEME_PROVIDER.get(r["scheme"]), str(r["value"]))
                         for r in published.get("cross_references") or []},
                "checklist": published.get("checklist"), "revision_id": revision["revision_id"]}
        return result

    @staticmethod
    def _compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
        if (right["provider"], right["native_key"]) in left["refs"] or \
                (left["provider"], left["native_key"]) in right["refs"]:
            return "cross-reference", {"published_by": left["subject_key"] if (right["provider"], right["native_key"])
                                       in left["refs"] else right["subject_key"]}
        if left["name"] and left["name"] == right["name"]:
            if left["author"] and left["author"] == right["author"]:
                return "name-authorship", {"scientific_name": left["name"], "authorship": left["authorship"]}
            return "name-only", {"scientific_name": left["name"], "authorship": [left["authorship"],
                                                                                 right["authorship"]]}
        for synonym, other in ((left, right), (right, left)):
            if synonym["status_class"] == "synonym" and name_key(synonym["accepted_name"]) == other["name"] \
                    and other["name"]:
                return "synonym", {"synonym": synonym["subject_key"], "accepted_name": synonym["accepted_name"]}
        return None, {}

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Offer every candidate pair across providers for review; flag split/lump conflicts; accept nothing."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        profiles = self.profiles(namespace)
        keys = sorted(profiles)
        pairs: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if provider_of(a) == provider_of(b):
                    continue
                basis, detail = self._compare(profiles[a], profiles[b])
                if basis:
                    pairs[(a, b)] = (basis, detail)
        conflicts = self._conflicts(profiles, pairs)
        offered = []
        for (a, b), (basis, detail) in sorted(pairs.items()):
            evidence = {**detail, "method": basis, "evidence_class": EVIDENCE_CLASS[basis],
                        "left": {k: profiles[a][k] for k in ("subject_key", "scientific_name", "authorship", "status",
                                                             "revision_id")},
                        "right": {k: profiles[b][k] for k in ("subject_key", "scientific_name", "authorship",
                                                              "status", "revision_id")},
                        "note": "provider taxa stay separate; a reviewed match only connects them"}
            mine = [c for c in conflicts if {a, b} & set(c["subjects"])]
            offered.append(self._offer(namespace, a, b, basis, evidence, mine, profiles, principal_id=principal_id))
        return {"contract": IDENTITY_CONTRACT, "proposed": sorted(o["match_id"] for o in offered if o["change"]),
                "conflicts": conflicts, "unmatched": sorted(set(keys) - {k for pair in pairs for k in pair}),
                "matches": self.matches(namespace, scopes=scopes),
                "policy": "every proposal needs a reviewer; conflicts are never resolved automatically"}

    @staticmethod
    def _conflicts(profiles, pairs) -> list[dict[str, Any]]:
        conflicts = []
        by_subject: dict[tuple[str, str], set[str]] = {}
        for a, b in pairs:
            by_subject.setdefault((a, provider_of(b)), set()).add(b)
            by_subject.setdefault((b, provider_of(a)), set()).add(a)
        for (subject, provider), others in sorted(by_subject.items()):
            if len(others) > 1:
                conflicts.append({"kind": "split-or-lump", "subjects": sorted({subject, *others}),
                                  "reason": f"{subject} is proposed against {len(others)} {provider} taxa; one concept "
                                            "may correspond to several (split/lumped) - resolved only by a reviewer"})
        for (a, b), (basis, _) in sorted(pairs.items()):
            classes = {profiles[a]["status_class"], profiles[b]["status_class"]}
            if basis in {"name-authorship", "name-only", "cross-reference"} and classes == {"accepted", "synonym"}:
                conflicts.append({"kind": "status-differs", "subjects": [a, b],
                                  "reason": "one provider treats the name as accepted, the other as a synonym "
                                            "(possible split/lump); resolved only by a reviewer"})
        return conflicts

    def _offer(self, namespace, a, b, basis, evidence, conflicts, profiles, *, principal_id) -> dict[str, Any]:
        from src.kb.entities import register_canonical_entity

        match_id = "biodiversity-idm:" + digest([namespace, a, b])[:24]
        row = self.conn.execute("SELECT state, basis, conflicts_json FROM biodiversity_identity_matches WHERE "
                                "namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if row is not None:
            if row[0] == "proposed" and (STRENGTH[basis] > STRENGTH[row[1]] or json.loads(row[2]) != conflicts):
                self.conn.execute("UPDATE biodiversity_identity_matches SET basis=?, evidence_json=?, conflicts_json=? "
                                  "WHERE namespace=? AND match_id=?",
                                  [basis, canonical(evidence), canonical(conflicts), namespace, match_id])
                return {"match_id": match_id, "change": "updated"}
            return {"match_id": match_id, "change": None}
        entities = [register_canonical_entity(self.conn, entity_id(k), profiles[k]["scientific_name"], "taxon")
                    for k in (a, b)]
        now = self.now()
        self.conn.execute(
            "INSERT INTO biodiversity_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, match_id, a, b, entities[0], entities[1], basis, canonical(evidence), canonical(conflicts),
             "proposed", None, None, principal_id, now,
             canonical([{"state": "proposed", "by": principal_id, "at_ms": now, "basis": basis}])])
        return {"match_id": match_id, "change": "created"}

    # ------------------------------------------------------------------ reviews

    def _row(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, left_key, right_key, left_entity, right_entity, basis, evidence_json, conflicts_json, "
            "state, decision_id, checklists_json, created_by, created_at_ms, history_json FROM "
            "biodiversity_identity_matches WHERE namespace=? AND match_id=?",
            [namespace, match_id]).fetchone() if self._ready() else None
        if row is None:
            raise BiodiversityError("not_found", "taxon identity match is not visible in this namespace")
        history = json.loads(row[13])
        reviewed = [h for h in history if h["state"] in {"accepted", "rejected", "reverted"}]
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace,
                **dict(zip(("match_id", "left_key", "right_key", "left_entity", "right_entity", "basis"), row[:6])),
                "evidence_class": EVIDENCE_CLASS[row[5]], "evidence": json.loads(row[6]),
                "conflicts": json.loads(row[7]), "state": row[8], "decision_id": row[9],
                "checklists": json.loads(row[10]) if row[10] else None, "created_by": row[11],
                "created_at_ms": row[12], "history": history,
                "reviewer": reviewed[-1]["by"] if reviewed and row[8] != "proposed" else None,
                "reviewed_at_ms": reviewed[-1]["at_ms"] if reviewed and row[8] != "proposed" else None,
                "notice": "a match connects provider taxa; it never merges taxonomic concepts"}

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                subject_key: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM biodiversity_identity_matches WHERE namespace=? AND (? IS NULL OR state=?) AND "
            "(? IS NULL OR left_key=? OR right_key=?) ORDER BY match_id",
            [namespace, state, state, subject_key, subject_key, subject_key]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise BiodiversityError("invalid_decision", "accept or reject with a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"proposed", "reverted"}:
            raise BiodiversityError("invalid_state", f"match is {match['state']}; revert it before re-reviewing")
        profiles = self.profiles(namespace)
        checklists = None
        if decision == "accept":
            checklists = {side: {"subject_key": match[f"{side}_key"],
                                 "checklist": (profiles.get(match[f"{side}_key"]) or {}).get("checklist")
                                 or "no checklist version published by this provider",
                                 "revision_id": (profiles.get(match[f"{side}_key"]) or {}).get("revision_id")}
                          for side in ("left", "right")}
        for side in ("left", "right"):
            self.history.register_entity(namespace, match[f"{side}_entity"], [match[f"{side}_key"]],
                                         principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [match["left_entity"], match["right_entity"]],
            {"match_id": match_id, "basis": match["basis"], "evidence": match["evidence"], "reason": reason.strip(),
             "conflicts": match["conflicts"], "checklists": checklists,
             "provenance": {"producer": "environment.biodiversity", "records": [match["left_key"], match["right_key"]]},
             "policy": {"merge": False, "note": "identity decision only; provider taxa stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"biodiversity-identity:{namespace}:{match_id}:{len(match['history'])}")
        state = "accepted" if decision == "accept" else "rejected"
        return self._transition(namespace, match, state, recorded["decision_id"], principal_id, reason.strip(),
                                checklists)

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise BiodiversityError("invalid_decision", "a revert needs a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"accepted", "rejected"}:
            raise BiodiversityError("invalid_state", "only an accepted or rejected match can be reverted")
        undo = self.history.undo(namespace, match["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, match, "reverted", undo["decision_id"], principal_id, reason.strip(), None)

    def _transition(self, namespace, match, state, decision_id, principal_id, reason, checklists):
        history = match["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                        "decision_id": decision_id}]
        self.conn.execute("UPDATE biodiversity_identity_matches SET state=?, decision_id=?, checklists_json=?, "
                          "history_json=? WHERE namespace=? AND match_id=?",
                          [state, decision_id, canonical(checklists) if checklists else None, canonical(history),
                           namespace, match["match_id"]])
        return self._row(namespace, match["match_id"])

    # ------------------------------------------------------------------ resolution

    def members(self, namespace: str, subject_key: str) -> list[str]:
        """The subject plus every subject an accepted match connects to it (transitively)."""
        parent: dict[str, str] = {}

        def find(key: str) -> str:
            parent.setdefault(key, key)
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        if self._ready():
            for left, right in self.conn.execute("SELECT left_key, right_key FROM biodiversity_identity_matches WHERE "
                                                 "namespace=? AND state='accepted'", [namespace]).fetchall():
                a, b = find(left), find(right)
                if a != b:
                    parent[max(a, b)] = min(a, b)
        root = find(subject_key)
        return sorted({k for k in list(parent) if find(k) == root} | {subject_key})

    def accepted_between(self, namespace: str, members: Iterable[str]) -> list[dict[str, Any]]:
        members = set(members)
        if not self._ready():
            return []
        rows = self.conn.execute("SELECT match_id FROM biodiversity_identity_matches WHERE namespace=? AND "
                                 "state='accepted' ORDER BY match_id", [namespace]).fetchall()
        return [m for m in (self._row(namespace, r[0]) for r in rows)
                if m["left_key"] in members and m["right_key"] in members]

    def find(self, namespace: str, query: str) -> tuple[str, list[str]]:
        """(interpreted_as, subject keys) for ``provider:key``, a bare native key or an exact scientific name."""
        text = str(query or "").strip()
        if not text:
            raise BiodiversityError("invalid_request", "give a scientific name, a native taxon key or provider:key")
        subjects = {r["subject_key"]: r for r in self.store.records(namespace)
                    if r["record_type"] in {"taxon", "taxon_identity"}}
        if text in subjects:
            return "subject_key", [text]
        by_key = sorted(k for k, r in subjects.items() if r["record_key"] == text)
        if by_key:
            return "native_key", by_key
        wanted = name_key(re.sub(r"\s*\(?[A-Z][^()]*,\s*\d{4}\)?\s*$", "", text))
        return "scientific_name", sorted(k for k, r in subjects.items() if name_key(r["subject_name"]) == wanted)


__all__ = ["BASES", "EVIDENCE_CLASS", "BiodiversityIdentity", "entity_id", "provider_of"]
