"""Reviewable identity across UniProt, NCBI, RCSB PDB, ChEMBL, Chemicals and Biodiversity (#2652, LS07 #2686).

Every record's native key (UniProt accession, Gene ID, Tax ID, PDB ID, ChEMBL
ID) is an identity *subject*, registered as its own ``canonical_entities`` row
under an identifier-based id, exactly as the Biodiversity and Chemicals
identities do (:mod:`src.kb.biodiversity_identity`,
:mod:`src.kb.substances_identity`). Subjects are connected only by **match
proposals** that carry a method, evidence and confidence; nothing is merged and
nothing is accepted automatically. Published identifiers come first:

* ``published-cross-reference`` - one source publishes the other's native key
  (a UniProt cross-reference to PDB, GeneID or ChEMBL; a PDB entity's UniProt
  mapping; an NCBI gene's Swiss-Prot accession; a ChEMBL target component
  accession). Confidence ``high`` when both sides assert it, ``medium`` when one
  does; the evidence names which source asserts it and the revision.
* ``inchikey`` - a ChEMBL compound and a Chemicals substance
  (:class:`src.kb.substances_store.SubstanceStore`) publish the same standard
  InChIKey (compared with :func:`src.kb.substances_records.identifier_key`, the
  Chemicals identity's own key) - confidence ``medium``: salts, mixtures and
  stereo-isomers are for the reviewer.
* ``taxon-cross-reference`` - a Biodiversity taxon identity
  (:mod:`src.kb.biodiversity_store`) publishes the NCBI Tax ID - ``medium``.
* ``scientific-name`` - only when no identifier connects an NCBI taxon and a
  Biodiversity taxon, an exact scientific-name equality is offered as a
  ``low``-confidence, lower-evidence proposal. Names never connect proteins,
  genes, structures, targets or compounds.

Accepting or rejecting records the reviewer, reason and time and an entity
identity decision in :class:`src.kb.entity_history.EntityHistoryStore`;
reverting undoes it. Subjects with no proposal stay listed as unmatched.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.lifesci_records import (
    IDENTITY_CONTRACT,
    PROVIDER_ID,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    LifeSciError,
    authorize,
    canonical,
    digest,
)
from src.kb.lifesci_store import LifeSciStore, table_exists

METHODS = ("published-cross-reference", "inchikey", "taxon-cross-reference", "scientific-name")
STRENGTH = {"published-cross-reference": 4, "inchikey": 3, "taxon-cross-reference": 3, "scientific-name": 1}
EVIDENCE_CLASS = {"published-cross-reference": "published-identifier", "inchikey": "published-identifier",
                  "taxon-cross-reference": "published-identifier", "scientific-name": "lower-evidence"}
IDENTITY_TYPES = ("protein", "gene", "taxon", "structure", "target", "compound")
# UniProt cross-reference databases that name another covered source's native key.
XREF_TARGETS = {"pdb": "pdb", "geneid": "ncbigene", "chembl": "chembl-target"}
TAXON_SCHEMES = {"ncbi-taxon", "ncbi", "ncbi-taxonomy", "ncbitaxon", "ncbi taxonomy"}
CHEMICALS_READ = "knowledge:substances:read"
BIODIVERSITY_READ = "knowledge:environment:read"
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS lifesci_identity_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
  left_entity TEXT NOT NULL, right_entity TEXT NOT NULL, method TEXT NOT NULL, confidence TEXT NOT NULL,
  evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL, PRIMARY KEY(namespace, match_id)
);
"""


def entity_id(subject_key: str) -> str:
    return "ent-lifesci-" + re.sub(r"[^a-z0-9]+", "-", subject_key.casefold()).strip("-")


def family(subject_key: str) -> str:
    return subject_key.split(":", 1)[0]


class LifeSciIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = LifeSciStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "lifesci_identity_matches")

    # ------------------------------------------------------------------ subjects

    def profiles(self, namespace: str) -> dict[str, dict[str, Any]]:
        """Current (not obsoleted) revision of every identity-bearing record, with the keys it publishes."""
        result: dict[str, dict[str, Any]] = {}
        for record in self.store.records(namespace):
            if record["record_type"] not in IDENTITY_TYPES:
                continue
            revision = self.store.current(namespace, record["record_id"])
            if revision is None or revision["event"] != "published":
                continue
            published = revision["statement"]["as_published"]
            asserts: set[str] = set()
            kind = record["record_type"]
            if kind == "protein":
                for ref in published.get("cross_references") or []:
                    prefix = XREF_TARGETS.get(str(ref["database"]).casefold())
                    if prefix:
                        asserts.add(f"{prefix}:{ref['id']}")
            elif kind == "structure":
                asserts |= {f"uniprot:{a}" for e in published.get("entities") or [] for a in e["uniprot_accessions"]}
            elif kind == "gene":
                asserts |= {f"uniprot:{r['id']}" for r in published.get("cross_references") or []
                            if str(r["database"]).casefold().startswith("uniprotkb")}
            elif kind == "target":
                asserts |= {f"uniprot:{c['accession']}" for c in published.get("components") or []
                            if c.get("accession")}
            result[record["subject_key"]] = {
                "subject_key": record["subject_key"], "record_type": kind, "provider": record["provider"],
                "name": record["subject_name"], "asserts": asserts, "revision_id": revision["revision_id"],
                "release": revision["release"], "inchikey": published.get("standard_inchikey"),
                "scientific_name": published.get("scientific_name"), "tax_id": published.get("tax_id")}
        return result

    def _chemicals(self, namespace: str, scopes: set[str]) -> tuple[dict[str, list[dict]], str | None]:
        """InChIKey -> Chemicals substance subjects, or the reason Chemicals cannot be read."""
        from src.kb.substances_store import SubstanceStore

        if not table_exists(self.conn, "substance_identifiers"):
            return {}, "Chemicals (chemicals.substances) is not installed or holds no substance"
        if CHEMICALS_READ not in scopes and "operator" not in scopes:
            return {}, f"{CHEMICALS_READ} is required to read Chemicals substances"
        index: dict[str, list[dict]] = {}
        for row in SubstanceStore(self.conn, initialize=False).identifiers(namespace):
            if row["scheme"] == "inchikey" and row["value_key"]:
                index.setdefault(row["value_key"], []).append(row)
        return index, None

    def _biodiversity(self, namespace: str, scopes: set[str]) -> tuple[list[dict], str | None]:
        from src.kb.biodiversity_store import BiodiversityStore

        if not table_exists(self.conn, "biodiversity_revisions"):
            return [], "Biodiversity (environment.biodiversity) is not installed or holds no taxon"
        if BIODIVERSITY_READ not in scopes and "operator" not in scopes:
            return [], f"{BIODIVERSITY_READ} is required to read Biodiversity taxa"
        store = BiodiversityStore(self.conn, initialize=False)
        taxa = []
        for record in store.records(namespace, record_type="taxon_identity"):
            revision = store.current(namespace, record["record_id"])
            published = revision["statement"]["as_published"]
            taxa.append({"subject_key": f"biodiversity:{record['subject_key']}", "name": published["scientific_name"],
                         "revision_id": revision["revision_id"], "status": published["status"],
                         "tax_ids": {str(r["value"]) for r in published.get("cross_references") or []
                                     if str(r.get("scheme") or "").casefold() in TAXON_SCHEMES}})
        return taxa, None

    # ------------------------------------------------------------------ proposals

    @staticmethod
    def _side(profile: Mapping[str, Any]) -> dict[str, Any]:
        return {k: profile.get(k) for k in ("subject_key", "name", "revision_id", "release")}

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Offer every published-identifier candidate (then name candidates for taxa) for review; accept nothing."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        profiles = self.profiles(namespace)
        offers: dict[tuple[str, str], tuple[str, str, dict[str, Any]]] = {}
        for key, profile in sorted(profiles.items()):
            for other in sorted(profile["asserts"]):
                if other not in profiles:
                    continue
                pair = tuple(sorted((key, other)))
                asserted = sorted({key if other in profile["asserts"] else None,
                                   other if key in profiles[other]["asserts"] else None} - {None})
                evidence = {"asserted_by": [{"subject_key": s, "source": profiles[s]["provider"],
                                             "revision_id": profiles[s]["revision_id"]} for s in asserted],
                            "left": self._side(profiles[pair[0]]), "right": self._side(profiles[pair[1]])}
                offers[pair] = ("published-cross-reference", "high" if len(asserted) == 2 else "medium", evidence)
        skipped = []
        chemicals, reason = self._chemicals(namespace, scopes)
        if reason:
            skipped.append({"target": "chemicals.substances", "reason": reason})
        for key, profile in sorted(profiles.items()):
            if profile["record_type"] != "compound" or not profile["inchikey"]:
                continue
            from src.kb.substances_records import identifier_key

            for row in chemicals.get(identifier_key("inchikey", profile["inchikey"]) or "", []):
                other = f"chemicals:{row['subject_key']}"
                offers[tuple(sorted((key, other)))] = ("inchikey", "medium", {
                    "inchikey": profile["inchikey"], "left": self._side(profile),
                    "right": {"subject_key": other, "revision_id": row["revision_id"], "provider": row["provider"]},
                    "note": "equal standard InChIKeys; salts, mixtures and stereo-isomers are for the reviewer"})
        taxa, reason = self._biodiversity(namespace, scopes)
        if reason:
            skipped.append({"target": "environment.biodiversity", "reason": reason})
        for key, profile in sorted(profiles.items()):
            if profile["record_type"] != "taxon":
                continue
            for taxon in taxa:
                pair = tuple(sorted((key, taxon["subject_key"])))
                right = {k: taxon[k] for k in ("subject_key", "name", "revision_id", "status")}
                if profile["tax_id"] in taxon["tax_ids"]:
                    offers[pair] = ("taxon-cross-reference", "medium", {
                        "tax_id": profile["tax_id"], "asserted_by": [{"subject_key": taxon["subject_key"],
                                                                      "source": "biodiversity"}],
                        "left": self._side(profile), "right": right})
                elif profile["scientific_name"] and profile["scientific_name"] == taxon["name"] and not any(
                        taxon["tax_ids"]):
                    offers[pair] = ("scientific-name", "low", {
                        "scientific_name": profile["scientific_name"], "left": self._side(profile), "right": right,
                        "note": "exact name only; no published identifier connects these taxa"})
        changes = [self._offer(namespace, a, b, method, confidence, evidence, principal_id=principal_id)
                   for (a, b), (method, confidence, evidence) in sorted(offers.items())]
        paired = {k for pair in offers for k in pair}
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace,
                "proposed": sorted(c["match_id"] for c in changes if c["change"]),
                "unmatched": sorted(set(profiles) - paired), "skipped_targets": skipped,
                "matches": self.matches(namespace, scopes=scopes),
                "policy": "published identifiers first; every proposal needs a reviewer; nothing is merged"}

    def _offer(self, namespace, a, b, method, confidence, evidence, *, principal_id) -> dict[str, Any]:
        from src.kb.entities import register_canonical_entity

        match_id = "lifesci-idm:" + digest([namespace, a, b])[:24]
        row = self.conn.execute("SELECT state, method, evidence_json FROM lifesci_identity_matches WHERE namespace=? "
                                "AND match_id=?", [namespace, match_id]).fetchone()
        if row is not None:
            if row[0] == "proposed" and (STRENGTH[method] > STRENGTH[row[1]] or json.loads(row[2]) != evidence):
                self.conn.execute("UPDATE lifesci_identity_matches SET method=?, confidence=?, evidence_json=? WHERE "
                                  "namespace=? AND match_id=?",
                                  [method, confidence, canonical(evidence), namespace, match_id])
                return {"match_id": match_id, "change": "updated"}
            return {"match_id": match_id, "change": None}
        names = {evidence["left"]["subject_key"]: evidence["left"].get("name"),
                 evidence["right"]["subject_key"]: evidence["right"].get("name")}
        entities = [register_canonical_entity(self.conn, entity_id(k), names.get(k) or k, "life_science_record")
                    for k in (a, b)]
        now = self.now()
        self.conn.execute(
            "INSERT INTO lifesci_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, match_id, a, b, entities[0], entities[1], method, confidence, canonical(evidence), "proposed",
             None, principal_id, now,
             canonical([{"state": "proposed", "by": principal_id, "at_ms": now, "method": method}])])
        return {"match_id": match_id, "change": "created"}

    # ------------------------------------------------------------------ reviews

    def _row(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, left_key, right_key, left_entity, right_entity, method, confidence, evidence_json, "
            "state, decision_id, created_by, created_at_ms, history_json FROM lifesci_identity_matches WHERE "
            "namespace=? AND match_id=?", [namespace, match_id]).fetchone() if self._ready() else None
        if row is None:
            raise LifeSciError("not_found", "life-sciences identity match is not visible in this namespace")
        history = json.loads(row[12])
        reviewed = [h for h in history if h["state"] in {"accepted", "rejected", "reverted"}]
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace,
                **dict(zip(("match_id", "left_key", "right_key", "left_entity", "right_entity", "method",
                            "confidence"), row[:7])),
                "evidence_class": EVIDENCE_CLASS[row[5]], "evidence": json.loads(row[7]), "state": row[8],
                "decision_id": row[9], "created_by": row[10], "created_at_ms": row[11], "history": history,
                "reviewer": reviewed[-1]["by"] if reviewed and row[8] != "proposed" else None,
                "reviewed_at_ms": reviewed[-1]["at_ms"] if reviewed and row[8] != "proposed" else None,
                "notice": "a match connects records of different sources; it never merges them"}

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                subject_key: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM lifesci_identity_matches WHERE namespace=? AND (? IS NULL OR state=?) AND "
            "(? IS NULL OR left_key=? OR right_key=?) ORDER BY match_id",
            [namespace, state, state, subject_key, subject_key, subject_key]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise LifeSciError("invalid_decision", "accept or reject with a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"proposed", "reverted"}:
            raise LifeSciError("invalid_state", f"match is {match['state']}; revert it before re-reviewing")
        for side in ("left", "right"):
            self.history.register_entity(namespace, match[f"{side}_entity"], [match[f"{side}_key"]],
                                         principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [match["left_entity"], match["right_entity"]],
            {"match_id": match_id, "method": match["method"], "confidence": match["confidence"],
             "evidence": match["evidence"], "reason": reason.strip(),
             "provenance": {"producer": PROVIDER_ID, "records": [match["left_key"], match["right_key"]]},
             "policy": {"merge": False, "note": "identity decision only; records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"lifesci-identity:{namespace}:{match_id}:{len(match['history'])}")
        return self._transition(namespace, match, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise LifeSciError("invalid_decision", "a revert needs a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"accepted", "rejected"}:
            raise LifeSciError("invalid_state", "only an accepted or rejected match can be reverted")
        undo = self.history.undo(namespace, match["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, match, "reverted", undo["decision_id"], principal_id, reason.strip())

    def _transition(self, namespace, match, state, decision_id, principal_id, reason):
        history = match["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                        "decision_id": decision_id}]
        self.conn.execute("UPDATE lifesci_identity_matches SET state=?, decision_id=?, history_json=? WHERE "
                          "namespace=? AND match_id=?",
                          [state, decision_id, canonical(history), namespace, match["match_id"]])
        return self._row(namespace, match["match_id"])

    # ------------------------------------------------------------------ resolution

    def accepted(self, namespace: str) -> list[dict[str, Any]]:
        if not self._ready():
            return []
        rows = self.conn.execute("SELECT match_id FROM lifesci_identity_matches WHERE namespace=? AND "
                                 "state='accepted' ORDER BY match_id", [namespace]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def accepted_for(self, namespace: str, subject_key: str) -> list[dict[str, Any]]:
        return [m for m in self.accepted(namespace) if subject_key in {m["left_key"], m["right_key"]}]

    def generation(self, namespace: str) -> int:
        if not self._ready():
            return 0
        rows = self.conn.execute("SELECT history_json FROM lifesci_identity_matches WHERE namespace=?",
                                 [namespace]).fetchall()
        return sum(len(json.loads(r[0])) - 1 for r in rows)  # reviews, rejections and reverts only


__all__ = ["EVIDENCE_CLASS", "METHODS", "LifeSciIdentity", "entity_id", "family"]
