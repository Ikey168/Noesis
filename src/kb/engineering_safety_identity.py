"""Reviewable matches from published safety subjects to Products models and canonical entities (ES10, #2071).

A subject is what a record's *current* revision names, keyed per domain by
:func:`src.kb.engineering_safety_records.subject_key` (type-certificate model
designation, vehicle make/model/year, part number + manufacturer, PHMSA
operator ID, facility name + address, organisation name). Candidate
generation is deterministic and records the evidence it used:

* ``products.identities`` (when the Products pack's store exists): an
  aircraft, engine, vehicle or component subject whose normalised designation
  equals a Products model designation. A stated make or holder must equal the
  Products brand (``designation+brand``); without a stated make the basis is
  the weaker ``designation-only``. A different brand is no candidate at all.
  Siblings, sister types and similar models are never proposed;
* ``canonical_entities``: an organisation, operator or facility name that the
  canonical alias table resolves (``canonical-alias``);
* Corporate Ownership (optional ``ownership_namespace``): organisation and
  operator names equal to an ownership legal entity's name are offered into
  the shared :class:`src.kb.ownership_identity.OwnershipIdentityService` state
  machine (``name-jurisdiction`` when the countries agree, else the
  never-acceptable ``similar-name``), under ``engineering-safety:`` record keys.

Candidates stay ``proposed`` until a reviewer accepts or rejects them. A
review applies to the proposal it was made against: when a later source
revision strengthens the basis of a pending candidate it is upgraded in place;
when a rejected or reverted candidate gains new evidence, or a detached
candidate is named again, it is proposed afresh and needs a new review. A
revert never reactivates an earlier acceptance. Accepting a canonical-entity
candidate records an entity identity ``match`` decision (``src.kb.entity_history``);
records are never merged. Unmatched subjects stay queryable by source string.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.engineering_safety_records import (
    MATCH_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    EngineeringSafetyError,
    authorize,
    canonical,
    designation_key,
    digest,
    load,
    name_key,
    require,
    subject_key,
)
from src.kb.engineering_safety_store import EngineeringSafetyStore, table_exists

METHOD = "engineering-safety-subject-v1"
BASIS_STRENGTH = {"designation+brand": 3, "canonical-alias": 2, "designation-only": 1}
PRODUCT_KINDS = frozenset({"aircraft_model", "aircraft", "engine_model", "vehicle", "component"})
ENTITY_KINDS = frozenset({"organisation", "pipeline_operator", "facility"})
DECISIONS = frozenset({"accepted", "rejected"})
OWNERSHIP_PREFIX = "engineering-safety:"
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_WRITE = "knowledge:ownership:write"
_DDL = """
CREATE TABLE IF NOT EXISTS es_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, subject_key TEXT NOT NULL, subject_kind TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_id TEXT NOT NULL, target_label TEXT, method TEXT NOT NULL, basis TEXT NOT NULL,
  candidate_state TEXT NOT NULL, evidence_json TEXT NOT NULL, proposal_seq INTEGER NOT NULL,
  history_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, match_id)
);
CREATE TABLE IF NOT EXISTS es_match_reviews (
  namespace TEXT NOT NULL, review_id TEXT NOT NULL, match_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  proposal_seq INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL, decision_id TEXT,
  principal_id TEXT NOT NULL, reviewed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, review_id)
);
"""


def ownership_key(key: str) -> str:
    return OWNERSHIP_PREFIX + key


class SubjectIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = EngineeringSafetyStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "es_matches")

    # ------------------------------------------------------------ subjects

    def current_subjects(self, namespace: str) -> dict[str, dict[str, Any]]:
        """Distinct subject keys named by current revisions, with every naming record and source string."""
        rows = self.conn.execute(
            "SELECT s.subject_key, s.kind, s.source_string, s.fields_json, s.record_id, s.revision_id, "
            "r.provider, r.native_id, r.record_kind FROM es_subjects s JOIN es_current c ON c.namespace=s.namespace "
            "AND c.revision_id=s.revision_id JOIN es_records r ON r.namespace=s.namespace AND r.record_id=s.record_id "
            "WHERE s.namespace=? ORDER BY s.subject_key, r.provider, r.native_id, s.ordinal", [namespace]).fetchall()
        subjects: dict[str, dict[str, Any]] = {}
        for key, kind, source_string, fields, record_id, revision_id, provider, native, record_kind in rows:
            if not key:
                continue
            entry = subjects.setdefault(key, {"subject_key": key, "kind": kind, "source_strings": [], "fields": [],
                                              "records": []})
            if source_string not in entry["source_strings"]:
                entry["source_strings"].append(source_string)
            fields = load(fields, {})
            if fields not in entry["fields"]:
                entry["fields"].append(fields)
            record = {"record_id": record_id, "revision_id": revision_id, "provider": provider, "native_id": native,
                      "record_kind": record_kind}
            if record not in entry["records"]:
                entry["records"].append(record)
        return subjects

    def _models(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "product_identities"):
            return []
        return [{"model_id": r[0], "provider": r[1], "brand": r[2], "designation": r[3]} for r in self.conn.execute(
            "SELECT identity_id, provider, brand, designation FROM product_identities WHERE namespace=? "
            "AND level='model' ORDER BY identity_id", [namespace]).fetchall()]

    @staticmethod
    def _product_proposal(subject: Mapping[str, Any], model: Mapping[str, Any]) -> tuple[str, dict] | None:
        target = designation_key(model.get("designation"))
        for fields in subject["fields"]:
            designation = fields.get("model") or fields.get("part_number")
            if not designation or designation_key(designation) != target or not target:
                continue
            maker = fields.get("make") or fields.get("manufacturer") or fields.get("type_certificate_holder")
            compared = {"designation": [designation, model.get("designation")]}
            if maker:
                if name_key(maker) != name_key(model.get("brand")):
                    return None  # a stated maker that differs is no candidate at all
                compared["brand"] = [maker, model.get("brand")]
                return "designation+brand", compared
            return "designation-only", compared
        return None

    # ------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, scopes: Iterable[str], principal_id: str,
                products_namespace: str | None = None, ownership_namespace: str | None = None) -> dict[str, Any]:
        """Deterministic candidates for the current subjects; idempotent; nothing is accepted automatically."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        if ownership_namespace:
            require(scopes, OWNERSHIP_READ, OWNERSHIP_WRITE)
        products_namespace = products_namespace or namespace
        subjects = self.current_subjects(namespace)
        models = self._models(products_namespace)
        resolver = None
        if table_exists(self.conn, "entity_aliases") and table_exists(self.conn, "canonical_entities"):
            from src.kb.entities import resolve as resolver
        now = self.now()
        produced: set[str] = set()
        changes = []
        for key, subject in sorted(subjects.items()):
            records = sorted(subject["records"], key=lambda r: (r["provider"], r["native_id"]))
            if subject["kind"] in PRODUCT_KINDS:
                for model in models:
                    proposal = self._product_proposal(subject, model)
                    if proposal is None:
                        continue
                    basis, compared = proposal
                    changes.append(self._upsert(
                        namespace, key, subject["kind"], "product-model", model["model_id"],
                        f"{model.get('brand') or ''} {model.get('designation') or ''}".strip(), basis,
                        {"compared": compared, "products_namespace": products_namespace,
                         "source_strings": subject["source_strings"]}, records, now, produced, principal_id))
            if subject["kind"] in ENTITY_KINDS and resolver is not None:
                for source_string in subject["source_strings"]:
                    match = resolver(self.conn, source_string)
                    if not match:
                        continue
                    changes.append(self._upsert(
                        namespace, key, subject["kind"], "canonical-entity", match["canonical_id"],
                        match["preferred_name"], "canonical-alias",
                        {"compared": {"name": [source_string, match["preferred_name"]]},
                         "alias_method": match["method"], "source_strings": subject["source_strings"]},
                        records, now, produced, principal_id))
        # A candidate the current revisions no longer name stays on record, detached.
        for (match_id,) in self.conn.execute(
                "SELECT match_id FROM es_matches WHERE namespace=? AND candidate_state<>'not_named_in_current_revision' "
                "ORDER BY match_id", [namespace]).fetchall():
            if match_id not in produced:
                self._transition(namespace, match_id, "not_named_in_current_revision", now, principal_id,
                                 {"change": "detached"})
                changes.append({"match_id": match_id, "change": "detached"})
        offered = []
        if ownership_namespace:
            offered = self._offer_ownership(namespace, ownership_namespace, subjects, scopes, principal_id)
        return {
            "contract": MATCH_CONTRACT,
            "namespace": namespace,
            "method": METHOD,
            "changes": [c for c in changes if c.get("change")],
            "candidates": self.candidates(namespace, scopes=scopes),
            "ownership_candidates": offered,
            "policy": "candidates only; a reviewer accepts each; no sister-type, similar-model or topic inference",
        }

    def _upsert(self, namespace, key, kind, target_kind, target_id, label, basis, evidence, records, now, produced,
                principal_id) -> dict[str, Any]:
        match_id = "es-match:" + digest([namespace, key, target_kind, target_id])[:24]
        produced.add(match_id)
        evidence = {**evidence, "subject_key": key, "records": records}
        existing = self.conn.execute(
            "SELECT basis, candidate_state, evidence_json, proposal_seq, history_json FROM es_matches "
            "WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO es_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, match_id, key, kind, target_kind, target_id, label, METHOD, basis, "proposed",
                 canonical(evidence), 1, canonical([{"change": "proposed", "basis": basis, "at_ms": now,
                                                     "by": principal_id}]), now, now])
            self.store.bump(namespace)
            return {"match_id": match_id, "change": "proposed"}
        old_basis, state, old_evidence, proposal_seq, history = existing
        history = load(history, [])
        review = self._review_state(namespace, match_id, proposal_seq)
        comparison_changed = digest({k: v for k, v in load(old_evidence, {}).items() if k != "records"}) != \
            digest({k: v for k, v in evidence.items() if k != "records"}) or old_basis != basis
        if state == "not_named_in_current_revision":
            change = "renamed_again"  # named again after being detached: a fresh proposal
        elif review in {"rejected", "reverted"} and comparison_changed:
            change = "reproposed"
        elif review == "unreviewed" and BASIS_STRENGTH[basis] > BASIS_STRENGTH[old_basis]:
            change = "upgraded"
        elif canonical(load(old_evidence, {})) != canonical(evidence) and not comparison_changed:
            # Only the list of naming records changed: the identity claim is the same, reviews stand.
            self.conn.execute("UPDATE es_matches SET evidence_json=?, updated_at_ms=? WHERE namespace=? AND match_id=?",
                              [canonical(evidence), now, namespace, match_id])
            return {"match_id": match_id, "change": None}
        else:
            return {"match_id": match_id, "change": None}
        new_seq = proposal_seq if change == "upgraded" else proposal_seq + 1
        history.append({"change": change, "basis": basis, "previous_basis": old_basis, "previous_state": state,
                        "previous_review": review, "at_ms": now, "by": principal_id})
        self.conn.execute(
            "UPDATE es_matches SET basis=?, candidate_state='proposed', evidence_json=?, proposal_seq=?, "
            "history_json=?, updated_at_ms=? WHERE namespace=? AND match_id=?",
            [basis, canonical(evidence), new_seq, canonical(history), now, namespace, match_id])
        self.store.bump(namespace)
        return {"match_id": match_id, "change": change}

    def _transition(self, namespace, match_id, state, now, principal_id, entry) -> None:
        history = load(self.conn.execute("SELECT history_json FROM es_matches WHERE namespace=? AND match_id=?",
                                         [namespace, match_id]).fetchone()[0], [])
        history.append({**entry, "state": state, "at_ms": now, "by": principal_id})
        self.conn.execute("UPDATE es_matches SET candidate_state=?, history_json=?, updated_at_ms=? "
                          "WHERE namespace=? AND match_id=?", [state, canonical(history), now, namespace, match_id])
        self.store.bump(namespace)

    def _offer_ownership(self, namespace, ownership_namespace, subjects, scopes, principal_id) -> list[dict]:
        if not table_exists(self.conn, "ownership_records"):
            return []
        from src.kb.ownership_identity import OwnershipIdentityService
        from src.kb.ownership_store import OwnershipStore

        entities = [e for e in OwnershipStore(self.conn, initialize=False).records(
            ownership_namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",))
            if not e.get("redacted")]
        service = OwnershipIdentityService(self.conn, now=self.now)
        offered = []
        for key, subject in sorted(subjects.items()):
            if subject["kind"] not in {"organisation", "pipeline_operator"}:
                continue
            names = {name_key(s) for s in subject["source_strings"]}
            for entity in entities:
                body = entity["record"]
                if name_key(body.get("name")) not in names:
                    continue
                country = str(body.get("jurisdiction") or "").split("-")[0].upper()
                basis = "name-jurisdiction" if country == "US" and any(
                    r["provider"] in {"phmsa", "ntsb", "csb", "nhtsa-odi", "faa-ad"} for r in subject["records"]) \
                    else "similar-name"
                offered.append(service.offer(
                    namespace, left_key=ownership_key(key), right_key=body["record_key"],
                    left_entity=ownership_key(key), right_entity=body.get("canonical_entity_id") or body["record_key"],
                    basis=basis, evidence=[{"kind": "name", "name": body.get("name"), "country": country or None,
                                            "left": {"subject_key": key, "source_strings": subject["source_strings"],
                                                     "records": subject["records"]},
                                            "right": {"record_key": body["record_key"],
                                                      "ownership_namespace": ownership_namespace,
                                                      "revision": entity.get("revision")}}],
                    principal_id=principal_id, scopes=scopes))
        return offered

    # ------------------------------------------------------------ reviews

    def _review_state(self, namespace: str, match_id: str, proposal_seq: int) -> str:
        if not table_exists(self.conn, "es_match_reviews"):
            return "unreviewed"
        row = self.conn.execute(
            "SELECT decision FROM es_match_reviews WHERE namespace=? AND match_id=? AND proposal_seq=? "
            "ORDER BY sequence DESC LIMIT 1", [namespace, match_id, proposal_seq]).fetchone()
        return row[0] if row else "unreviewed"

    def match(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT subject_key, subject_kind, target_kind, target_id, target_label, method, basis, candidate_state, "
            "evidence_json, proposal_seq, history_json FROM es_matches WHERE namespace=? AND match_id=?",
            [namespace, match_id]).fetchone()
        if row is None:
            raise EngineeringSafetyError("not_found", "subject match is not visible in this namespace")
        reviews = [dict(zip(("sequence", "proposal_seq", "decision", "reason", "decision_id", "principal_id",
                             "reviewed_at_ms"), r)) for r in self.conn.execute(
            "SELECT sequence, proposal_seq, decision, reason, decision_id, principal_id, reviewed_at_ms FROM "
            "es_match_reviews WHERE namespace=? AND match_id=? ORDER BY sequence", [namespace, match_id]).fetchall()]
        review_state = self._review_state(namespace, match_id, row[9])
        attached = review_state == "accepted" and row[7] == "proposed"
        return {
            "contract": MATCH_CONTRACT, "match_id": match_id, "subject_key": row[0], "subject_kind": row[1],
            "target_kind": row[2], "target_id": row[3], "target_label": row[4], "method": row[5], "basis": row[6],
            "candidate_state": row[7], "evidence": load(row[8], {}), "proposal_seq": row[9],
            "history": load(row[10], []), "review_state": review_state, "review_history": reviews,
            "attached": attached,
            **({"needs_re_review": True} if review_state == "unreviewed" and any(
                r["proposal_seq"] < row[9] for r in reviews) else {}),
        }

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, scopes: Iterable[str],
               principal_id: str) -> dict[str, Any]:
        """Accept or reject the current proposal; appended, never overwriting an earlier review."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in DECISIONS or not str(reason or "").strip():
            raise EngineeringSafetyError("invalid_decision", "decide accepted or rejected with a reason")
        current = self.match(namespace, match_id)
        if current["candidate_state"] != "proposed":
            raise EngineeringSafetyError("not_named", "the current revisions no longer name this subject")
        if current["review_state"] in DECISIONS:
            raise EngineeringSafetyError("already_reviewed", f"this proposal is already {current['review_state']}; "
                                                             "revert it first")
        decision_id = None
        if current["target_kind"] == "canonical-entity":
            from src.kb.entity_history import EntityHistoryStore

            history = EntityHistoryStore(self.conn, now=self.now)
            left = ownership_key(current["subject_key"])
            for entity in (left, current["target_id"]):
                history.register_entity(namespace, entity, [], principal_id=principal_id,
                                        scopes=_ENTITY_HISTORY_SCOPES)
            decision_id = history.decide(
                namespace, "match" if decision == "accepted" else "non-match", [left, current["target_id"]],
                {"match_id": match_id, "basis": current["basis"], "evidence": current["evidence"],
                 "reason": reason.strip(), "provenance": {"producer": "engineering-safety.identity"},
                 "policy": {"merge": False, "note": "identity decision only; records stay separate"}},
                reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
                event_key=f"engineering-safety:{namespace}:{match_id}:{current['proposal_seq']}")["decision_id"]
        self._append_review(namespace, match_id, current["proposal_seq"], decision, reason.strip(), decision_id,
                            principal_id)
        return self.match(namespace, match_id)

    def revert(self, namespace: str, match_id: str, reason: str, *, scopes: Iterable[str],
               principal_id: str) -> dict[str, Any]:
        """Undo the current proposal's decision; the candidate is ``reverted`` and nothing earlier comes back."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise EngineeringSafetyError("invalid_decision", "a revert needs a reason")
        current = self.match(namespace, match_id)
        if current["review_state"] not in DECISIONS:
            raise EngineeringSafetyError("invalid_state", "only an accepted or rejected proposal can be reverted")
        last = current["review_history"][-1]
        decision_id = None
        if last.get("decision_id"):
            from src.kb.entity_history import EntityHistoryStore

            decision_id = EntityHistoryStore(self.conn, now=self.now).undo(
                namespace, last["decision_id"], reviewer_id=principal_id, principal_id=principal_id,
                scopes=_ENTITY_HISTORY_SCOPES)["decision_id"]
        self._append_review(namespace, match_id, current["proposal_seq"], "reverted", reason.strip(), decision_id,
                            principal_id)
        return self.match(namespace, match_id)

    def _append_review(self, namespace, match_id, proposal_seq, decision, reason, decision_id, principal_id):
        sequence = int(self.conn.execute("SELECT coalesce(max(sequence), 0) FROM es_match_reviews WHERE namespace=? "
                                         "AND match_id=?", [namespace, match_id]).fetchone()[0]) + 1
        self.conn.execute("INSERT INTO es_match_reviews VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [namespace, "es-match-review:" + digest([namespace, match_id, sequence])[:24], match_id,
                           sequence, proposal_seq, decision, reason, decision_id, principal_id, self.now()])
        self.store.bump(namespace)

    # ------------------------------------------------------------ queries

    def candidates(self, namespace: str, *, scopes: Iterable[str], subject_key: str | None = None,
                   target_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM es_matches WHERE namespace=? AND (? IS NULL OR subject_key=?) AND "
            "(? IS NULL OR target_id=?) ORDER BY subject_key, target_id",
            [namespace, subject_key, subject_key, target_id, target_id]).fetchall()
        return [self.match(namespace, r[0]) for r in rows]

    def attached_keys(self, namespace: str, target_id: str) -> list[dict[str, Any]]:
        """Subject keys an accepted, still-named match connects to a Products model or canonical entity."""
        if not self.ready():
            return []
        result = []
        for (match_id,) in self.conn.execute("SELECT match_id FROM es_matches WHERE namespace=? AND target_id=? "
                                             "ORDER BY match_id", [namespace, target_id]).fetchall():
            match = self.match(namespace, match_id)
            if match["attached"]:
                result.append(match)
        return result


def query_keys(identity: SubjectIdentity, namespace: str, subject: Mapping[str, Any]
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The one equivalence definition shared by lookups, dossiers and monitors.

    Returns (key patterns, connecting matches). A pattern is ``{"key": ..., "prefix": bool}``; a vehicle asked
    without a model year matches every year of that make and model. A Products model id or entity id reaches
    the subject keys that accepted, still-named matches connect to it; nothing else is equivalent.
    """
    subject = {k: v for k, v in dict(subject or {}).items() if v not in (None, "")}
    if subject.get("product_model_id") or subject.get("entity_id"):
        target = str(subject.get("product_model_id") or subject.get("entity_id"))
        matches = identity.attached_keys(namespace, target)
        return [{"key": m["subject_key"], "prefix": False} for m in matches], matches
    kind = str(subject.get("kind") or "")
    if kind in {"aircraft", "aircraft_model"}:
        kind = "aircraft_model"
    key = subject_key(kind, subject) if kind else None
    if key is None:
        raise EngineeringSafetyError(
            "invalid_request", "name a subject: kind with its identifying fields (aircraft_model/engine_model model, "
            "vehicle make+model[+model_year], component part_number+manufacturer, pipeline_operator operator_id, "
            "facility name[+address], organisation name), a product_model_id or an entity_id")
    prefix = kind == "vehicle" and not subject.get("model_year")
    return [{"key": key, "prefix": prefix}], []


def key_matches(key: str | None, patterns: Iterable[Mapping[str, Any]]) -> bool:
    if not key:
        return False
    for pattern in patterns:
        if key == pattern["key"] or (pattern["prefix"] and key.startswith(pattern["key"] + ":")):
            return True
    return False
