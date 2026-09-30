"""Claimants, claims and publishers matched through reviewable identity (#2659, FC06).

Every fact-check stays the record its publisher published. Links to other
owners' records are *proposed* as candidates with a method, evidence and a
confidence, and a reviewer accepts, rejects or later reverts them; accepted and
rejected decisions are :class:`src.kb.entity_history.EntityHistoryStore`
``match`` / ``non-match`` decisions and a revert is an ``undo`` there. Nothing is
merged and nothing is accepted automatically.

Three kinds of match, published identifiers before names:

* **claimant -> canonical entity** (``canonical_entities``):
  ``published-identifier`` when the claimant carries a Wikidata id and a
  canonical entity is registered under that identifier
  (``ent-wikidata-q...``, :func:`src.kb.entities.register_canonical_entity`);
  otherwise ``name-as-published`` when the claimant's name as published resolves
  through the entity alias table (a weak signal);
* **fact-check claim -> argument claim** (``argument_claims``):
  ``shared-appearance-url`` when the argument claim's document is an appearance
  the fact-check cites (URLs compared under ``wa-canon-v1``), and
  ``quoted-text-overlap`` when the quoted claim and the extracted claim share
  enough tokens (:mod:`src.kb.claim_links` tokenisation). Either is only a
  candidate: *no automatic claim matching without review*;
* **publisher -> source identity** (:mod:`src.kb.source_identity`):
  ``published-domain`` when the publisher's website domain resolves to a source
  identity through a domain alias decision.

Records without an accepted match stay visible as ``unmatched``. Claimant
accounts and social-platform appearances are never matched (FC01).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.fact_checks_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    FactCheckError,
    FactCheckStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-fact-check-identity-candidate-v1"
MATCH_KINDS = ("claimant", "claim", "publisher")
CONFIDENCE = {"published-identifier": 0.95, "published-domain": 0.8, "shared-appearance-url": 0.6,
              "quoted-text-overlap": 0.3, "name-as-published": 0.3}
TEXT_OVERLAP = 0.6
STATES = ("proposed", "accepted", "rejected", "reverted")
NOTICE = "a reviewable identity decision; records are never merged and nothing is accepted automatically"
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS fact_check_identity_candidates (
  namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, match_kind TEXT NOT NULL, left_key TEXT NOT NULL,
  right_key TEXT NOT NULL, method TEXT NOT NULL, confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, decision_id TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  history_json TEXT NOT NULL, PRIMARY KEY(namespace, candidate_id)
);
"""
_COLUMNS = ("candidate_id", "match_kind", "left_key", "right_key", "method", "confidence", "evidence_json", "state",
            "decision_id", "created_by", "created_at_ms", "history_json")


def claimant_key(claimant: Mapping[str, Any] | None) -> str | None:
    """A claimant subject: by its first published identifier, else by its normalised name as published."""
    from src.kb.entities import normalize_surface

    if not claimant:
        return None
    identifiers = sorted((i["scheme"], i["value"]) for i in claimant.get("identifiers") or [])
    if identifiers:
        return f"fact-check:claimant:{identifiers[0][0]}:{identifiers[0][1]}"
    name = normalize_surface(str(claimant.get("name_as_published") or ""))
    return f"fact-check:claimant:name:{name.replace(' ', '-')}" if name else None


def subject_entity(match_kind: str, key: str) -> str:
    """The entity-history id of one side of a candidate (a fact-check subject or its target)."""
    if key.startswith(("ent-", "source-identity:")):
        return key
    if match_kind == "claim" and not key.startswith("fact-check:"):
        return f"argument-claim:{key}"
    return f"fact-check-{match_kind}:{digest(key)[:24]}"


def _view(row) -> dict[str, Any]:
    import json

    value = dict(zip(_COLUMNS, row))
    value["evidence"] = json.loads(value.pop("evidence_json"))
    value["history"] = json.loads(value.pop("history_json"))
    last = value["history"][-1]
    return {"contract": CONTRACT, **value,
            "review_state": {"accepted": "reviewed-match", "rejected": "reviewed-non-match",
                             "reverted": "reverted", "proposed": "unreviewed-candidate"}[value["state"]],
            "reviewer": last.get("by") if value["state"] != "proposed" else None, "reason": last.get("reason"),
            "notice": NOTICE}


class FactCheckIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = FactCheckStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "fact_check_identity_candidates")

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Claimants, claims and publishers as published, each citing the revisions it was read from."""
        claimants: dict[str, dict[str, Any]] = {}
        claims: dict[str, dict[str, Any]] = {}
        publishers: dict[str, dict[str, Any]] = {}
        for view in self.store.records(namespace, scopes=scopes, include_absent=False):
            record, cite = view["record"], {"record_key": view["record_key"], "source_id": view["source_id"],
                                            "revision_id": view["revision_id"]}
            fields = record["fields"]
            if view["record_kind"] == "publisher":
                entry = publishers.setdefault(view["record_key"], {"subject_key": view["record_key"],
                                                                   "domain": fields["domain"], "names": set(),
                                                                   "cited": []})
                entry["names"].add(fields["name_as_published"])
                entry["cited"].append(cite)
                continue
            publisher = fields["publisher"]
            entry = publishers.setdefault(view["publisher_key"], {"subject_key": view["publisher_key"],
                                                                  "domain": publisher["domain"], "names": set(),
                                                                  "cited": []})
            if publisher.get("name_as_published"):
                entry["names"].add(publisher["name_as_published"])
            entry["cited"].append(cite)
            claim = fields["claims"][0]
            subject = claims.setdefault(view["record_key"], {"subject_key": view["record_key"],
                                                             "claim_text": claim["claim_text"], "appearances": set(),
                                                             "cited": []})
            subject["appearances"].update(a["url_canonical"] for a in claim.get("appearances") or []
                                          if a.get("url_canonical"))
            if claim.get("first_appearance") and claim["first_appearance"].get("url_canonical"):
                subject["appearances"].add(claim["first_appearance"]["url_canonical"])
            subject["cited"].append(cite)
            person = claim.get("claimant")
            key = claimant_key(person)
            if key:
                entry = claimants.setdefault(key, {"subject_key": key, "names": set(), "identifiers": {},
                                                   "types": set(), "fact_checks": set(), "cited": []})
                if person.get("name_as_published"):
                    entry["names"].add(person["name_as_published"])
                if person.get("type_as_published"):
                    entry["types"].add(person["type_as_published"])
                for identifier in person.get("identifiers") or []:
                    entry["identifiers"][identifier["scheme"]] = identifier["value"]
                entry["fact_checks"].add(view["record_key"])
                entry["cited"].append(cite)

        def done(items: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
            out = []
            for item in items.values():
                out.append({k: sorted(v) if isinstance(v, set) else v for k, v in item.items()})
            return sorted(out, key=lambda s: s["subject_key"])

        return {"claimant": done(claimants), "claim": done(claims), "publisher": done(publishers)}

    # ------------------------------------------------------------------ proposals

    def _offer(self, namespace: str, kind: str, left: str, right: str, method: str, evidence: Mapping[str, Any],
               principal_id: str) -> dict[str, Any]:
        candidate_id = "fc-idc:" + digest([namespace, kind, left, right])[:24]
        evidence = [dict(evidence, method=method)]
        now = self.now()
        row = self.conn.execute("SELECT state, method, evidence_json, history_json FROM "
                                "fact_check_identity_candidates WHERE namespace=? AND candidate_id=?",
                                [namespace, candidate_id]).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO fact_check_identity_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, candidate_id, kind, left, right, method, CONFIDENCE[method],
                               canonical(evidence), "proposed", None, principal_id, now,
                               canonical([{"state": "proposed", "by": principal_id, "at_ms": now}])])
            return {"candidate_id": candidate_id, "change": "created"}
        import json

        state, old_method, history = row[0], row[1], json.loads(row[3])
        stronger = CONFIDENCE[method] > CONFIDENCE[old_method]
        fresh = digest(evidence) != digest(json.loads(row[2]))
        if (state == "proposed" and stronger) or (state in {"rejected", "reverted"} and fresh):
            history.append({"state": "proposed", "by": principal_id, "at_ms": now, "previous_state": state,
                            "previous_method": old_method})
            self.conn.execute("UPDATE fact_check_identity_candidates SET state='proposed', decision_id=NULL, "
                              "method=?, confidence=?, evidence_json=?, history_json=? WHERE namespace=? AND "
                              "candidate_id=?", [method, CONFIDENCE[method], canonical(evidence), canonical(history),
                                                 namespace, candidate_id])
            return {"candidate_id": candidate_id, "change": "reproposed"}
        return {"candidate_id": candidate_id, "change": None}

    def _claimant_targets(self) -> list[dict[str, Any]] | None:
        if not table_exists(self.conn, "canonical_entities"):
            return None
        rows = self.conn.execute("SELECT canonical_id, preferred_name, entity_type FROM canonical_entities").fetchall()
        return [{"canonical_id": r[0], "preferred_name": r[1], "entity_type": r[2]} for r in rows]

    def _argument_claims(self) -> list[dict[str, Any]] | None:
        if not table_exists(self.conn, "argument_claims"):
            return None
        from src.kb.web_archive_identity import canonical_key

        documents = {}
        if table_exists(self.conn, "documents"):
            documents = {r[0]: r[1] for r in self.conn.execute("SELECT document_id, url FROM documents").fetchall()}
        out = []
        for claim_id, text, document_id in self.conn.execute(
                "SELECT claim_id, claim_text, document_id FROM argument_claims ORDER BY claim_id").fetchall():
            url = documents.get(document_id)
            out.append({"claim_id": claim_id, "claim_text": text, "document_id": document_id,
                        "document_url_canonical": canonical_key(url) if url else None})
        return out

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                source_identity_namespace: str | None = None) -> dict[str, Any]:
        """Offer reviewable candidates; idempotent, never an automatic acceptance; absent owners are reported."""
        from src.kb.claim_links import _jaccard, _tokens
        from src.kb.entities import normalize_surface, resolve

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        subjects = self.subjects(namespace, scopes=scopes)
        offered, unavailable = [], []
        entities = self._claimant_targets()
        if entities is None:
            unavailable.append({"target": "canonical_entities", "reason": "no canonical entity store"})
        else:
            by_id = {e["canonical_id"]: e for e in entities}
            for subject in subjects["claimant"]:
                matched = False
                for scheme, value in sorted(subject["identifiers"].items()):
                    target = f"ent-{scheme}-{value.lower()}"
                    if target in by_id:
                        matched = True
                        offered.append(self._offer(namespace, "claimant", subject["subject_key"], target,
                                                   "published-identifier", {
                                                       "scheme": scheme, "value": value,
                                                       "names_as_published": subject["names"],
                                                       "cited": subject["cited"][:5],
                                                       "right": by_id[target]}, principal_id))
                if matched:
                    continue
                for name in subject["names"]:
                    hits = {h["canonical_id"]: {**h, "via": "entity alias"} for h in [resolve(self.conn, name)] if h}
                    for entity in entities:
                        if normalize_surface(entity["preferred_name"] or "") == normalize_surface(name):
                            hits.setdefault(entity["canonical_id"], {**entity, "via": "preferred name"})
                    for target, hit in sorted(hits.items()):
                        offered.append(self._offer(namespace, "claimant", subject["subject_key"], target,
                                                   "name-as-published", {
                                                       "value": name, "via": hit["via"],
                                                       "cited": subject["cited"][:5], "right": hit,
                                                       "note": "a name alone is a weak signal; a reviewer decides"},
                                                   principal_id))
        claims = self._argument_claims()
        if claims is None:
            unavailable.append({"target": "argument_claims", "reason": "no argument claim store"})
        else:
            for subject in subjects["claim"]:
                quoted = _tokens(subject["claim_text"])
                for claim in claims:
                    overlap = _jaccard(quoted, _tokens(claim["claim_text"] or ""))
                    shared = claim["document_url_canonical"] in set(subject["appearances"])
                    if not shared and overlap < TEXT_OVERLAP:
                        continue
                    method = "shared-appearance-url" if shared else "quoted-text-overlap"
                    offered.append(self._offer(namespace, "claim", subject["subject_key"], claim["claim_id"], method, {
                        "quoted_claim": subject["claim_text"], "argument_claim": claim["claim_text"],
                        "token_overlap": round(overlap, 3), "document_id": claim["document_id"],
                        "shared_appearance": claim["document_url_canonical"] if shared else None,
                        "url_rules": "wa-canon-v1", "cited": subject["cited"][:5],
                        "note": "a candidate only: no automatic claim matching without review"}, principal_id))
        if not table_exists(self.conn, "source_alias_decisions"):
            unavailable.append({"target": "source_identities", "reason": "no source identity store"})
        else:
            from src.kb.source_identity import READ_SCOPE as SOURCE_READ
            from src.kb.source_identity import SourceIdentityStore

            store = SourceIdentityStore(self.conn, initialize=False)
            for subject in subjects["publisher"]:
                resolved = store.resolve_alias(source_identity_namespace or namespace, "domain", subject["domain"],
                                               scopes={SOURCE_READ})
                for match in resolved["matches"]:
                    offered.append(self._offer(namespace, "publisher", subject["subject_key"], match["source_id"],
                                               "published-domain", {
                                                   "domain": subject["domain"], "names_as_published": subject["names"],
                                                   "alias_decision_id": match["decision_id"],
                                                   "ambiguous": resolved["ambiguous"], "cited": subject["cited"][:5]},
                                               principal_id))
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["change"]}),
                "candidates": self.candidates(namespace, scopes=scopes), "unavailable": unavailable,
                "unmatched": self.unmatched(namespace, scopes=scopes), "notice": NOTICE}

    # ------------------------------------------------------------------ review

    def _row(self, namespace: str, candidate_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT " + ", ".join(_COLUMNS) + " FROM fact_check_identity_candidates WHERE "
                                "namespace=? AND candidate_id=?", [namespace, candidate_id]).fetchone() \
            if self.ready() else None
        if row is None:
            raise FactCheckError("not_found", "no fact-check identity candidate with that id")
        return _view(row)

    def _transition(self, namespace: str, candidate: Mapping[str, Any], state: str, decision_id: str,
                    principal_id: str, reason: str) -> dict[str, Any]:
        history = candidate["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                           "decision_id": decision_id}]
        self.conn.execute("UPDATE fact_check_identity_candidates SET state=?, decision_id=?, history_json=? WHERE "
                          "namespace=? AND candidate_id=?",
                          [state, decision_id, canonical(history), namespace, candidate["candidate_id"]])
        return self._row(namespace, candidate["candidate_id"])

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept (``match``) or reject (``non-match``) a candidate as an entity identity decision."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise FactCheckError("invalid_decision", "accept or reject with a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] != "proposed":
            raise FactCheckError("invalid_state", f"candidate is {candidate['state']}; propose again to re-review")
        kind = candidate["match_kind"]
        sides = [subject_entity(kind, candidate["left_key"]), subject_entity(kind, candidate["right_key"])]
        for entity, key in zip(sides, (candidate["left_key"], candidate["right_key"])):
            self.history.register_entity(namespace, entity, [key], principal_id=principal_id,
                                         scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", sides,
            {"candidate_id": candidate_id, "match_kind": kind, "method": candidate["method"],
             "confidence": candidate["confidence"], "evidence": candidate["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "news.fact-checks", "records": [candidate["left_key"],
                                                                        candidate["right_key"]]},
             "policy": {"merge": False, "note": "identity decision only; records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"fact-check-identity:{namespace}:{candidate_id}:{len(candidate['history'])}")
        return self._transition(namespace, candidate, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Undo an accepted or rejected decision; the candidate becomes ``reverted`` and records stay intact."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise FactCheckError("invalid_decision", "a revert needs a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] not in {"accepted", "rejected"}:
            raise FactCheckError("invalid_state", "only an accepted or rejected candidate can be reverted")
        undo = self.history.undo(namespace, candidate["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, candidate, "reverted", undo["decision_id"], principal_id, reason.strip())

    # ------------------------------------------------------------------ reads

    def candidates(self, namespace: str, *, scopes: Iterable[str], match_kind: str | None = None,
                   key: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM fact_check_identity_candidates WHERE namespace=? AND "
            "(? IS NULL OR match_kind=?) AND (? IS NULL OR left_key=? OR right_key=?) AND (? IS NULL OR state=?) "
            "ORDER BY match_kind, left_key, right_key",
            [namespace, match_kind, match_kind, key, key, key, state, state]).fetchall()
        return [_view(r) for r in rows]

    def accepted(self, namespace: str, match_kind: str, *, scopes: Iterable[str], key: str | None = None
                 ) -> list[dict[str, Any]]:
        """Accepted, unreverted matches of one kind (optionally touching one key)."""
        return self.candidates(namespace, scopes=scopes, match_kind=match_kind, key=key, state="accepted")

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Subjects without an accepted match stay visible as unmatched."""
        subjects = self.subjects(namespace, scopes=scopes)
        accepted = {(c["match_kind"], c["left_key"]) for c in self.candidates(namespace, scopes=scopes,
                                                                             state="accepted")}
        out: dict[str, list[dict[str, Any]]] = {}
        for kind in MATCH_KINDS:
            out[kind] = [{"subject_key": s["subject_key"], "state": "unmatched",
                          **({"names": s["names"]} if "names" in s else {"claim_text": s["claim_text"]})}
                         for s in subjects[kind] if (kind, s["subject_key"]) not in accepted]
        return out
