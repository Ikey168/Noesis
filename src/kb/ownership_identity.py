"""Reviewable, reversible identity reconciliation across ownership sources (O08).

Candidate matches between entity records from different sources are
*proposed* with their basis, evidence and a confidence; nothing is merged.
Accepting or rejecting a candidate is an entity identity decision recorded in
the shared owner :class:`src.kb.entity_history.EntityHistoryStore` (``match``
or ``non-match``); reverting appends an ``undo`` decision there. Records are
never rewritten or deleted: both sides stay intact and auditable whatever the
decision, and graph queries only *group* records through accepted, unreverted
matches.

Bases, strongest first:

* ``exact-identifier`` - both records carry the same identifier and it is the
  primary identity of at least one side (an LEI on a GLEIF record and on a
  BODS statement; a Companies House number on a CH profile and a BODS entity);
* ``cross-referenced-identifier`` - one record's secondary reference points
  at the other's primary identity (a GLEIF registration-authority number
  pointing at a Companies House company; a CIK mapped to an LEI by the market
  instrument master; an OpenCorporates record linked in ``src.kb.lei``);
* ``name-jurisdiction`` - equal normalized names in the same country, with no
  contradicting identifier; always low confidence.
* ``unqualified-identifier`` - the same identifier value on two records
  where the issuing country is missing or not comparable on one side; a
  reviewer must establish that both refer to the same issuer.
* ``similar-name`` - a name that another owner (e.g. a sanctions list) states
  resembles this record's name, with no corroborating attribute. It is shown
  as a candidate but can never be accepted: a similar name alone is not an
  identity.

Other record owners (the Legal sanctions, Political lobbying and elections, Economics public-finance,
Funding development-finance and Market BaFin-notices features, and the Materials pack, whose
``structure-similarity`` basis is a phase-level match of two material records) put their own candidates into this same state machine through :meth:`OwnershipIdentityService.offer`;
review, rejection and revert are shared.

Successors and predecessors (mergers, re-registrations) are corporate events,
never identity matches; they are not proposed here.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.ownership_records import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.ownership_store import OwnershipError, OwnershipStore, authorize, canonical_entity_id

CONTRACT = "noesis-ownership-identity-candidate-v1"
CONFIDENCE = {"exact-identifier": 0.95, "cross-referenced-identifier": 0.8,
              "unqualified-identifier": 0.5, "structure-similarity": 0.45, "name-jurisdiction": 0.35,
              "similar-name": 0.1}
NEVER_ACCEPTED = frozenset({"similar-name"})
# Record keys owned by other bundles that share this state machine through ``offer``.
FOREIGN_KEY_PREFIXES = ("sanctions:", "lobbying:", "elections:", "public-finance:", "devfin:", "funding-funder:",
                        "bafin:", "engineering-safety:", "materials:", "sports:", "legislation:", "courts:",
                        "infrastructure:", "space-registration:", "insurance:",
                        "competition:", "movements:", "campaign-finance:", "treaties:",
                        "enforcement:", "extractives:")
PRIMARY_SCHEME = {"gleif": "lei", "companies-house": "gb-coh", "sec-edgar": "sec-cik"}
STATES = ("proposed", "accepted", "rejected", "reverted")
_DDL = """
CREATE TABLE IF NOT EXISTS ownership_identity_candidates (
  namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
  left_entity TEXT NOT NULL, right_entity TEXT NOT NULL, basis TEXT NOT NULL, confidence DOUBLE NOT NULL,
  evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL, PRIMARY KEY(namespace, candidate_id)
);
"""
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}


def _norm(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum()).lstrip("0") or "0"


def _country(value: Any) -> str:
    return str(value or "").split("-")[0].upper()


class OwnershipIdentityService:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = OwnershipStore(conn, now=self.now, initialize=initialize)
        self.history = EntityHistoryStore(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return bool(self.conn.execute("SELECT 1 FROM information_schema.tables "
                                      "WHERE table_name='ownership_identity_candidates'").fetchone())

    # ------------------------------------------------------------ proposals

    def _entities(self, namespace: str, principal_id: str, scopes: set[str]) -> list[dict[str, Any]]:
        return [v for v in self.store.records(namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",))
                if not v.get("redacted")]

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                market: Mapping[str, Any] | None = None, lei_namespace: str | None = None) -> dict[str, Any]:
        """Propose candidates; re-proposing never changes a reviewed candidate."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        entities = self._entities(namespace, principal_id, scopes)
        pairs: dict[tuple[str, str], dict[str, Any]] = {}

        def offer(left: Mapping[str, Any], right_key: str, basis: str, evidence: Mapping[str, Any]) -> None:
            left_key = left["record"]["record_key"]
            if left_key == right_key:
                return
            a, b = sorted((left_key, right_key))
            current = pairs.get((a, b))
            if current is None or CONFIDENCE[basis] > CONFIDENCE[current["basis"]]:
                pairs[(a, b)] = {"basis": basis, "evidence": [dict(evidence)]}
            elif current["basis"] == basis:
                current["evidence"].append(dict(evidence))

        index: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for entity in entities:
            for identifier in entity["record"].get("identifiers") or []:
                index.setdefault((identifier["scheme"], _norm(identifier["value"])), []).append((entity, identifier))
        def primary(entity: Mapping[str, Any], scheme: str) -> bool:
            # An identifier is a record's own identity when it is the provider's
            # primary scheme; BODS entity statements declare their identifiers
            # as the statement's identity. Anything else is a cross-reference
            # (e.g. the registration-authority number on an LEI record).
            provider = entity["record"]["source"]["provider"]
            return provider == "open-ownership" or PRIMARY_SCHEME.get(provider) == scheme

        for (scheme, value), holders in sorted(index.items()):
            for i, (left, left_id) in enumerate(holders):
                for right, right_id in holders[i + 1:]:
                    both = primary(left, scheme) and primary(right, scheme)
                    basis = "exact-identifier" if both else "cross-referenced-identifier"
                    offer(left, right["record"]["record_key"], basis, {
                        "scheme": scheme, "value": left_id["value"],
                        "left": {"record_key": left["record"]["record_key"], "provider": left["record"]["source"]["provider"],
                                 "revision": left["revision"]},
                        "right": {"record_key": right["record"]["record_key"], "provider": right["record"]["source"]["provider"],
                                  "revision": right["revision"]}})
        if market:
            self._market_cross_references(entities, market, offer)
        if lei_namespace:
            self._opencorporates_links(entities, lei_namespace, offer)
        from src.kb.entities import normalize_surface

        by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entity in entities:
            body = entity["record"]
            country = _country(body.get("jurisdiction"))
            if country:
                by_name.setdefault((normalize_surface(body["name"]), country), []).append(entity)
        for (name, country), group in sorted(by_name.items()):
            for i, left in enumerate(group):
                for right in group[i + 1:]:
                    if self._contradicts(left["record"], right["record"]):
                        continue
                    offer(left, right["record"]["record_key"], "name-jurisdiction", {
                        "normalized_name": name, "country": country,
                        "names": [left["record"]["name"], right["record"]["name"]],
                        "note": "equal normalized names in one country are a weak signal, never an identity"})
        created = []
        by_key = {e["record"]["record_key"]: e for e in entities}
        for (a, b), proposal in sorted(pairs.items()):
            candidate_id = "own-idc:" + digest([namespace, a, b])[:24]
            exists = self.conn.execute("SELECT state FROM ownership_identity_candidates WHERE namespace=? AND candidate_id=?",
                                       [namespace, candidate_id]).fetchone()
            if exists:
                continue
            left_entity = canonical_entity_id(a) if a in by_key else self._external_entity(a)
            right_entity = canonical_entity_id(b) if b in by_key else self._external_entity(b)
            self.conn.execute(
                "INSERT INTO ownership_identity_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, candidate_id, a, b, left_entity, right_entity, proposal["basis"],
                 CONFIDENCE[proposal["basis"]], canonical(proposal["evidence"]), "proposed", None, principal_id,
                 self.now(), canonical([{"state": "proposed", "by": principal_id, "at_ms": self.now()}])])
            created.append(candidate_id)
        return {"proposed": created, "candidates": self.candidates(namespace, scopes=scopes)}

    def offer(self, namespace: str, *, left_key: str, right_key: str, left_entity: str, right_entity: str,
              basis: str, evidence: list[Mapping[str, Any]], principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Add or refresh one externally proposed candidate for a record pair.

        * no candidate yet: a new ``proposed`` candidate;
        * a pending (``proposed``) candidate with a weaker basis: upgraded to the
          stronger basis and evidence;
        * a ``rejected`` or ``reverted`` candidate: proposed again when the basis
          or the evidence is new, so a reviewer sees it afresh;
        * an ``accepted`` candidate, or nothing new: left unchanged.

        The candidate keeps its id, and every change is appended to its history
        with the basis and evidence it replaced, so the audit trail is complete.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if basis not in CONFIDENCE or left_key == right_key or not evidence:
            raise OwnershipError("invalid_candidate", "a candidate needs two records, a known basis and evidence")
        (a, a_entity), (b, b_entity) = sorted(((left_key, left_entity), (right_key, right_entity)))
        candidate_id = "own-idc:" + digest([namespace, a, b])[:24]
        evidence = [dict(e) for e in evidence]
        exists = self.conn.execute(
            "SELECT state, basis, evidence_json, history_json FROM ownership_identity_candidates "
            "WHERE namespace=? AND candidate_id=?", [namespace, candidate_id]).fetchone()
        if not exists:
            self.conn.execute(
                "INSERT INTO ownership_identity_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, candidate_id, a, b, a_entity, b_entity, basis, CONFIDENCE[basis],
                 canonical(evidence), "proposed", None, principal_id, self.now(),
                 canonical([{"state": "proposed", "by": principal_id, "at_ms": self.now()}])])
            return {"candidate_id": candidate_id, "created": True, "change": "created"}
        state, old_basis, old_evidence, history = exists[0], exists[1], json.loads(exists[2]), json.loads(exists[3])
        stronger = CONFIDENCE[basis] > CONFIDENCE[old_basis]
        new_evidence = digest(evidence) != digest(old_evidence) or basis != old_basis
        if state == "proposed" and stronger:
            change = "upgraded"
        elif state in {"rejected", "reverted"} and new_evidence:
            change = "reproposed"
        else:
            return {"candidate_id": candidate_id, "created": False, "change": None}
        history.append({"state": "proposed", "by": principal_id, "at_ms": self.now(), "change": change,
                        "previous_state": state, "previous_basis": old_basis, "previous_evidence": old_evidence})
        self.conn.execute(
            "UPDATE ownership_identity_candidates SET state='proposed', decision_id=NULL, basis=?, confidence=?, "
            "evidence_json=?, history_json=? WHERE namespace=? AND candidate_id=?",
            [basis, CONFIDENCE[basis], canonical(evidence), canonical(history), namespace, candidate_id])
        return {"candidate_id": candidate_id, "created": False, "change": change}

    @staticmethod
    def _external_entity(key: str) -> str:
        return canonical_entity_id(key)

    @staticmethod
    def _contradicts(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        ours = {(i["scheme"], _norm(i["value"])) for i in left.get("identifiers") or []}
        theirs = {(i["scheme"], _norm(i["value"])) for i in right.get("identifiers") or []}
        schemes = {s for s, _ in ours} & {s for s, _ in theirs}
        return any({v for s, v in ours if s == scheme} != {v for s, v in theirs if s == scheme} for scheme in schemes)

    def _market_cross_references(self, entities, market, offer) -> None:
        """CIK -> issuer -> LEI through the market instrument master (reused, not duplicated)."""
        from src.domains.market.instruments import MarketInstrumentError, MarketInstrumentStore

        instruments = MarketInstrumentStore(self.conn, initialize=False)
        lei_entities = {}
        for entity in entities:
            for identifier in entity["record"].get("identifiers") or []:
                if identifier["scheme"] == "lei":
                    lei_entities.setdefault(_norm(identifier["value"]), []).append(entity)
        for entity in entities:
            for identifier in entity["record"].get("identifiers") or []:
                if identifier["scheme"] != "sec-cik":
                    continue
                try:
                    resolved = instruments.resolve_identifier(
                        market["namespace"], "cik", identifier["value"], object_type="issuer",
                        as_of_ms=int(market["as_of_ms"]), acquired_by_ms=int(market["acquired_by_ms"]),
                        principal_id=market["principal_id"], scopes=set(market["scopes"]))
                except MarketInstrumentError:
                    continue
                if resolved["status"] != "resolved":
                    continue
                issuer = resolved["candidates"][0]["issuer"]
                for alias in issuer.get("identifiers") or []:
                    if str(alias.get("scheme", "")).lower() != "lei":
                        continue
                    for target in lei_entities.get(_norm(alias.get("value")), []):
                        offer(entity, target["record"]["record_key"], "cross-referenced-identifier", {
                            "via": "market instrument master", "issuer_id": issuer.get("issuer_id"),
                            "cik": identifier["value"], "lei": alias.get("value"),
                            "source_ref_id": alias.get("source_ref_id")})

    def _opencorporates_links(self, entities, lei_namespace, offer) -> None:
        from src.kb.lei import LeiStore

        links = LeiStore(self.conn).links(lei_namespace)
        for entity in entities:
            if entity["record"]["source"]["provider"] != "gleif":
                continue
            lei = entity["record"]["source"]["provider_record_id"]
            for link in links:
                if link["lei"] == lei and link["external_kind"] == "opencorporates" and link["state"] != "rejected":
                    offer(entity, f"opencorporates:{link['external_id']}", "cross-referenced-identifier", {
                        "via": "src.kb.lei company_identity_links", "link_id": link["link_id"],
                        "link_state": link["state"],
                        "note": "OpenCorporates is an aggregator, not an authoritative substitute for the register"})

    # ------------------------------------------------------------- reviews

    def _row(self, namespace: str, candidate_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT candidate_id, left_key, right_key, left_entity, right_entity, basis, confidence, evidence_json, state, "
            "decision_id, created_by, created_at_ms, history_json FROM ownership_identity_candidates "
            "WHERE namespace=? AND candidate_id=?", [namespace, candidate_id]).fetchone()
        if row is None:
            raise OwnershipError("not_found", "identity candidate is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace,
                **dict(zip(("candidate_id", "left_key", "right_key", "left_entity", "right_entity", "basis",
                            "confidence"), row[:7])),
                "evidence": json.loads(row[7]), "state": row[8], "decision_id": row[9], "created_by": row[10],
                "created_at_ms": row[11], "history": json.loads(row[12]),
                "notice": "a candidate is a reviewable proposal; records are never merged"}

    def candidates(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                   record_key: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT candidate_id FROM ownership_identity_candidates WHERE namespace=? AND (? IS NULL OR state=?) "
            "AND (? IS NULL OR left_key=? OR right_key=?) ORDER BY candidate_id",
            [namespace, state, state, record_key, record_key, record_key]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def _register(self, namespace: str, candidate: Mapping[str, Any], principal_id: str) -> None:
        for side in ("left", "right"):
            self.history.register_entity(namespace, candidate[f"{side}_entity"], [candidate[f"{side}_key"]],
                                         principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)

    def _transition(self, namespace, candidate, state, decision_id, principal_id, reason) -> dict[str, Any]:
        history = candidate["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                           "decision_id": decision_id}]
        self.conn.execute("UPDATE ownership_identity_candidates SET state=?, decision_id=?, history_json=? "
                          "WHERE namespace=? AND candidate_id=?",
                          [state, decision_id, canonical(history), namespace, candidate["candidate_id"]])
        return self._row(namespace, candidate["candidate_id"])

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept (``match``) or reject (``non-match``) as an entity identity decision."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise OwnershipError("invalid_decision", "accept or reject with a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] != "proposed":
            raise OwnershipError("invalid_state", f"candidate is {candidate['state']}; propose again to re-review")
        if decision == "accept" and candidate["basis"] in NEVER_ACCEPTED:
            raise OwnershipError("insufficient_evidence", "a similar name alone never produces an accepted match; "
                                                          "reject it or propose identifier or attribute evidence")
        self._register(namespace, candidate, principal_id)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match",
            [candidate["left_entity"], candidate["right_entity"]],
            {"candidate_id": candidate_id, "basis": candidate["basis"], "confidence": candidate["confidence"],
             "evidence": candidate["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "ownership.core", "records": [candidate["left_key"], candidate["right_key"]]},
             "policy": {"merge": False, "note": "identity decision only; records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"ownership-identity:{namespace}:{candidate_id}")
        return self._transition(namespace, candidate, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Undo an accepted or rejected decision; the candidate becomes ``reverted`` and records stay intact."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise OwnershipError("invalid_decision", "a revert needs a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] not in {"accepted", "rejected"}:
            raise OwnershipError("invalid_state", "only an accepted or rejected candidate can be reverted")
        undo = self.history.undo(namespace, candidate["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, candidate, "reverted", undo["decision_id"], principal_id, reason.strip())

    def clusters(self, namespace: str, *, accepted: Iterable[str] | None = None) -> dict[str, str]:
        """record_key -> cluster representative over accepted matches (or the pinned accepted set)."""
        if not self._ready():
            return {}
        if accepted is None:
            rows = self.conn.execute("SELECT left_key, right_key FROM ownership_identity_candidates "
                                     "WHERE namespace=? AND state='accepted' ORDER BY candidate_id", [namespace]).fetchall()
        else:
            ids = sorted(set(accepted))
            rows = [] if not ids else self.conn.execute(
                "SELECT left_key, right_key FROM ownership_identity_candidates WHERE namespace=? AND candidate_id IN ("
                + ",".join("?" * len(ids)) + ")", [namespace, *ids]).fetchall()
        parent: dict[str, str] = {}

        def find(key: str) -> str:
            parent.setdefault(key, key)
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        for left, right in rows:
            if left.startswith(FOREIGN_KEY_PREFIXES) or right.startswith(FOREIGN_KEY_PREFIXES):
                continue  # another owner's link (e.g. a sanctions designation) never regroups ownership entities
            a, b = find(left), find(right)
            if a != b:
                parent[max(a, b)] = min(a, b)
        return {key: find(key) for key in list(parent)}

    def accepted_ids(self, namespace: str) -> list[str]:
        if not self._ready():
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT candidate_id FROM ownership_identity_candidates WHERE namespace=? AND state='accepted' "
            "ORDER BY candidate_id", [namespace]).fetchall()]
