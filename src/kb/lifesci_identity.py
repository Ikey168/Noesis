"""Reviewable cross-source identity for life-science records (#2652, LS07 #2686).

Identity uses published identifiers first and never merges anything:

* **published-xref** - a record's published cross-reference names another acquired record by its own accession
  (UniProt to PDB, GeneID and ChEMBL targets; a PDB entity to UniProt; a ChEMBL target component to UniProt). When
  both sides assert each other the evidence lists both assertions and the confidence is higher. A cross-reference to
  a record that was not acquired stays an *unresolved* cross-reference, never a match;
* **inchikey** - a ChEMBL compound and a Chemicals substance (:mod:`src.kb.substances_identity` subjects) that
  publish the same standard InChIKey, offered as a reviewable assertion;
* **published-xref** to Biodiversity - a Biodiversity taxon identity whose published cross-references carry the NCBI
  Tax ID; **scientific-name** - an exact scientific name and rank match to a Biodiversity taxon identity, offered only
  when no identifier connects them, at low confidence;
* **reviewed-manual** - a reviewer's own proposal with stated evidence.

Every match carries method, evidence and confidence and moves through ``proposed`` -> ``accepted`` / ``rejected``
-> ``reverted``; accepting or rejecting is an entity-history ``match`` / ``non-match`` decision
(:class:`src.kb.entity_history.EntityHistoryStore`), reverting appends an ``undo``. Nothing is accepted
automatically, and records without an accepted match stay visible as unmatched.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

from src.kb.lifesci_records import (
    _ENTITY_HISTORY_SCOPES,
    INACTIVE,
    MATCH_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    XREF_TARGETS,
    LifeSciError,
    authorize,
    canonical,
    digest,
    table_exists,
)
from src.kb.lifesci_store import LifeSciStore, iso_from_ms

METHODS = ("published-xref", "inchikey", "scientific-name", "reviewed-manual")
CONFIDENCE = {"mutual-xref": 0.95, "published-xref": 0.9, "inchikey": 0.9, "reviewed-manual": 0.6,
              "scientific-name": 0.3}
RIGHT_KINDS = ("lifesci-record", "substance", "biodiversity-taxon")
STATES = ("proposed", "accepted", "rejected", "reverted")
DECISIONS = ("accepted", "rejected")
_DDL = """
CREATE TABLE IF NOT EXISTS lifesci_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, left_id TEXT NOT NULL, right_kind TEXT NOT NULL,
  right_id TEXT NOT NULL, method TEXT NOT NULL, confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, decision_id TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, match_id)
);
"""
NOTICE = ("an identity match only: nothing is merged; each source's records, revisions and licences stay separate; "
          "only accepted matches are used by links and answers")


class LifeSciIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.store = LifeSciStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "lifesci_matches")

    # ------------------------------------------------------------------ profiles

    def profiles(self, namespace: str) -> dict[str, dict[str, Any]]:
        """Current revision per record with its statement and cross-references."""
        out = {}
        for head in self.store.records(namespace):
            revision = self.store.in_force(namespace, head["record_id"])
            if revision is None:
                continue
            out[head["record_id"]] = {**head, "revision": revision,
                                      "statement": self.store.statement(namespace, revision["revision_id"]),
                                      "xrefs": self.store.xrefs(namespace, revision["revision_id"])}
        return out

    # ------------------------------------------------------------------ rows

    def _row(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, left_id, right_kind, right_id, method, confidence, evidence_json, state, decision_id, "
            "history_json, created_by FROM lifesci_matches WHERE namespace=? AND match_id=?",
            [namespace, match_id]).fetchone()
        if row is None:
            raise LifeSciError("not_found", "match is not visible in this namespace")
        value = dict(zip(("match_id", "left_id", "right_kind", "right_id", "method", "confidence", "evidence",
                          "state", "decision_id", "history", "created_by"), row))
        value["evidence"], value["history"] = json.loads(value["evidence"]), json.loads(value["history"])
        return {"contract": MATCH_CONTRACT, **value, "notice": NOTICE}

    def _upsert(self, namespace, left, right_kind, right, method, confidence, evidence, principal_id) -> str:
        a, b = (left, right) if right_kind != "lifesci-record" else tuple(sorted([left, right]))
        match_id = "lifesci-match:" + digest([namespace, a, right_kind, b])[:24]
        now = self.now()
        existing = self.conn.execute("SELECT state, method, confidence FROM lifesci_matches WHERE namespace=? AND "
                                     "match_id=?", [namespace, match_id]).fetchone()
        if existing is None:
            self.conn.execute("INSERT INTO lifesci_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, match_id, a, right_kind, b, method, confidence, canonical(evidence),
                               "proposed", None, "[]", principal_id, now, now])
        elif existing[0] == "proposed" and confidence >= existing[2]:
            # An unreviewed proposal follows the current evidence; reviews stand until reverted.
            self.conn.execute("UPDATE lifesci_matches SET method=?, confidence=?, evidence_json=?, updated_at_ms=? "
                              "WHERE namespace=? AND match_id=?",
                              [method, confidence, canonical(evidence), now, namespace, match_id])
        return match_id

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Offer matches from published identifiers (and, for taxa without one, exact scientific names)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        profiles = self.profiles(namespace)
        by_key = {(p["source"], p["record_type"], p["native_id"].upper()): rid for rid, p in profiles.items()}
        produced: set[str] = set()
        unresolved: list[dict[str, Any]] = []
        assertions: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for record_id, profile in profiles.items():
            if profile["revision"]["status"] in INACTIVE:
                continue
            for xref in profile["xrefs"]:
                # An organism, parent, or an activity's target/compound/document is a reference, not an identity.
                if xref["database"] == "NCBI Taxonomy" or profile["record_type"] in {"activity", "taxon"}:
                    continue
                target = XREF_TARGETS.get(xref["database"])
                if target is None:
                    continue
                other = by_key.get((target[0], target[1], xref["id"].upper()))
                if other is None:
                    unresolved.append({"record_id": record_id, "database": xref["database"], "id": xref["id"],
                                       "asserted_by": profile["source"],
                                       "revision_id": profile["revision"]["revision_id"]})
                    continue
                if other == record_id:
                    continue
                pair = tuple(sorted([record_id, other]))
                assertions.setdefault(pair, []).append({
                    "kind": "published-xref", "asserted_by": profile["source"], "record_id": record_id,
                    "revision_id": profile["revision"]["revision_id"], "release": profile["revision"]["release_label"],
                    "database": xref["database"], "id": xref["id"], "relation": xref.get("relation"),
                    "properties": xref.get("properties") or {}})
        self.conn.execute("BEGIN")
        try:
            for (left, right), evidence in sorted(assertions.items()):
                mutual = len({e["record_id"] for e in evidence}) == 2
                produced.add(self._upsert(namespace, left, "lifesci-record", right, "published-xref",
                                          CONFIDENCE["mutual-xref" if mutual else "published-xref"], evidence,
                                          principal_id))
            produced |= self._propose_substances(namespace, profiles, scopes, principal_id)
            produced |= self._propose_taxa(namespace, profiles, principal_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"contract": MATCH_CONTRACT, "namespace": namespace,
                "matches": [self._row(namespace, m) for m in sorted(produced)],
                "unresolved_xrefs": unresolved,
                "notice": "published identifiers first; every match is proposed for review and nothing is merged"}

    def _propose_substances(self, namespace, profiles, scopes, principal_id) -> set[str]:
        if not table_exists(self.conn, "substance_identifiers"):
            return set()
        from src.kb.substances_records import identifier_key
        from src.kb.substances_store import SubstanceStore

        substances = SubstanceStore(self.conn, initialize=False)
        produced = set()
        for record_id, profile in profiles.items():
            if profile["record_type"] != "compound":
                continue
            published = dict(profile["statement"]["attributes"].get("structures") or {}).get("standard_inchi_key")
            key = identifier_key("inchikey", published) if published else None
            if key is None:
                continue
            for subject in substances.find_subjects(namespace, "inchikey", key):
                rows = [i for i in substances.identifiers(namespace, subject) if i["scheme"] == "inchikey"
                        and i["value_key"] == key]
                evidence = [{"kind": "inchikey", "value": key,
                             "compound": {"record_id": record_id, "revision_id": profile["revision"]["revision_id"],
                                          "asserted_by": "chembl"},
                             "substance": {"subject_key": subject,
                                           "revision_ids": sorted({r["revision_id"] for r in rows}),
                                           "providers": sorted({r["provider"] for r in rows})}}]
                produced.add(self._upsert(namespace, record_id, "substance", subject, "inchikey",
                                          CONFIDENCE["inchikey"], evidence, principal_id))
        del scopes
        return produced

    def _propose_taxa(self, namespace, profiles, principal_id) -> set[str]:
        if not table_exists(self.conn, "biodiversity_records"):
            return set()
        from src.kb.biodiversity_store import BiodiversityStore

        store = BiodiversityStore(self.conn, initialize=False)
        identities = []
        for head in store.records(namespace, record_type="taxon_identity"):
            current = store.current(namespace, head["record_id"])
            if current is not None:
                identities.append((head, current))
        produced = set()
        for record_id, profile in profiles.items():
            if profile["record_type"] != "taxon" or profile["revision"]["status"] in INACTIVE:
                continue
            attributes = profile["statement"]["attributes"]
            tax_id = str(attributes.get("tax_id"))
            for head, current in identities:
                published = current["statement"]["as_published"]
                refs = [r for r in published.get("cross_references") or []
                        if "ncbi" in str(r.get("scheme")).casefold() and str(r.get("value")) == tax_id]
                right = f"{head['provider']}:{head['record_key']}"
                if refs:
                    evidence = [{"kind": "published-xref", "asserted_by": head["provider"],
                                 "biodiversity_record_id": head["record_id"],
                                 "biodiversity_revision_id": current["revision_id"], "scheme": refs[0]["scheme"],
                                 "value": refs[0]["value"],
                                 "taxon": {"record_id": record_id, "revision_id": profile["revision"]["revision_id"]}}]
                    produced.add(self._upsert(namespace, record_id, "biodiversity-taxon", right, "published-xref",
                                              CONFIDENCE["published-xref"], evidence, principal_id))
                elif (str(published.get("scientific_name") or "").strip() == str(attributes.get("scientific_name"))
                      and str(published.get("rank") or "").casefold() == str(attributes.get("rank")).casefold()):
                    evidence = [{"kind": "scientific-name", "name": attributes.get("scientific_name"),
                                 "rank": attributes.get("rank"), "biodiversity_record_id": head["record_id"],
                                 "biodiversity_revision_id": current["revision_id"],
                                 "taxon": {"record_id": record_id, "revision_id": profile["revision"]["revision_id"]},
                                 "note": "no identifier connects them; a name match is a low-confidence candidate"}]
                    produced.add(self._upsert(namespace, record_id, "biodiversity-taxon", right, "scientific-name",
                                              CONFIDENCE["scientific-name"], evidence, principal_id))
        return produced

    def propose_manual(self, namespace: str, left_id: str, right_kind: str, right_id: str, evidence: str, *,
                       scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if right_kind not in RIGHT_KINDS or not str(evidence or "").strip():
            raise LifeSciError("invalid_request", f"a manual proposal names one of {RIGHT_KINDS} and its evidence")
        self.store.record(namespace, left_id)
        if right_kind == "lifesci-record":
            self.store.record(namespace, right_id)
        match_id = self._upsert(namespace, left_id, right_kind, right_id, "reviewed-manual",
                                CONFIDENCE["reviewed-manual"], [{"kind": "reviewed-manual", "text": evidence.strip(),
                                                                  "by": principal_id}], principal_id)
        return self._row(namespace, match_id)

    # ------------------------------------------------------------------ review

    def matches(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None,
                state: str | None = None, right_kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM lifesci_matches WHERE namespace=? AND (? IS NULL OR left_id=? OR right_id=?) "
            "AND (? IS NULL OR state=?) AND (? IS NULL OR right_kind=?) ORDER BY match_id",
            [namespace, record_id, record_id, record_id, state, state, right_kind, right_kind]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, scopes: Iterable[str],
               principal_id: str) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in DECISIONS or not str(reason or "").strip():
            raise LifeSciError("invalid_decision", "decide accepted or rejected with a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"proposed", "reverted"}:
            raise LifeSciError("invalid_state", "only a proposed or reverted match can be reviewed; revert first")
        left, right = f"lifesci-record:{match['left_id']}", f"{match['right_kind']}:{match['right_id']}"
        for entity in (left, right):
            self.history.register_entity(namespace, entity, [entity], principal_id=principal_id,
                                         scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accepted" else "non-match", [left, right],
            {"match_id": match_id, "method": match["method"], "confidence": match["confidence"],
             "evidence": match["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "science.life-sciences"},
             "policy": {"merge": False, "note": "identity decision only; source records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"lifesci-identity:{namespace}:{match_id}:{len(match['history'])}")
        return self._transition(namespace, match, decision, recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, match_id: str, reason: str, *, scopes: Iterable[str],
               principal_id: str) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise LifeSciError("invalid_decision", "a revert needs a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in DECISIONS or not match["decision_id"]:
            raise LifeSciError("invalid_state", "only an accepted or rejected match can be reverted")
        undo = self.history.undo(namespace, match["decision_id"], reviewer_id=principal_id, principal_id=principal_id,
                                 scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, match, "reverted", undo["decision_id"], principal_id, reason.strip())

    def _transition(self, namespace, match, state, decision_id, principal_id, reason):
        history = match["history"] + [{"state": state, "by": principal_id, "reason": reason,
                                       "at": iso_from_ms(self.now()), "decision_id": decision_id}]
        self.conn.execute("UPDATE lifesci_matches SET state=?, decision_id=?, history_json=?, updated_at_ms=? "
                          "WHERE namespace=? AND match_id=?",
                          [state, decision_id, canonical(history), self.now(), namespace, match["match_id"]])
        return self._row(namespace, match["match_id"])

    # ------------------------------------------------------------------ reads

    def accepted(self, namespace: str, record_id: str, *, right_kind: str | None = None) -> list[dict[str, Any]]:
        """Accepted matches touching a record (the only matches links and answers use)."""
        if not self.ready():
            return []
        return [m for m in self.matches(namespace, scopes={"operator"}, record_id=record_id, state="accepted",
                                        right_kind=right_kind)]

    def unmatched(self, namespace: str, *, scopes: Iterable[str], record_type: str | None = None) -> dict[str, Any]:
        """Records with no accepted match, as published (never hidden)."""
        authorize(namespace, scopes, READ_SCOPE)
        accepted = set()
        if self.ready():
            for left, right_kind, right in self.conn.execute(
                    "SELECT left_id, right_kind, right_id FROM lifesci_matches WHERE namespace=? AND state='accepted'",
                    [namespace]).fetchall():
                accepted.add(left)
                if right_kind == "lifesci-record":
                    accepted.add(right)
        records = [r for r in self.store.records(namespace, record_type=record_type) if r["record_id"] not in accepted]
        return {"namespace": namespace, "unmatched": records, "count": len(records),
                "note": "unmatched records stay addressable by their own accession; nothing is matched by name"}


__all__ = ["CONFIDENCE", "METHODS", "STATES", "LifeSciIdentity"]
