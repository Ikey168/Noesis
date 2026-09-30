"""Treaty participants and treaties across sources through reviewable identity (#2581, TR06).

**Participants.** A participant is kept per source as published
(``treaties:participant:<source>:<name>``); nothing is merged by name. Each is
offered to the :mod:`src.kb.geospatial` places the platform already holds and
to ``canonical_entities``, published identifiers first:

* ``published-code`` - an ISO 3166-1 code (or a CELLAR country authority code,
  which is ISO 3166-1 alpha-3 based) the source itself publishes for the
  participant, carried by exactly one place in its ``source_ids``
  (``iso3166-1-alpha2`` / ``iso3166-1-alpha3``);
* ``exact-name-coded-place`` - the participant's name as published equals the
  name of exactly one place that carries an ISO 3166-1 code; the code is the
  evidence, the reviewer decides;
* ``name-only`` - a canonical entity whose preferred name equals the name as
  published; shown as context and never acceptable.

**Treaties.** The same treaty in UNTC, CELLAR and the Council of Europe is
matched only through published cross-references: ``shared-identifier`` (both
records publish the same identifier in the same scheme, e.g. a UNTS
registration number) and ``published-cross-reference`` (one record's
published title or reference cites the other's identifier, e.g. "CETS No.
990" in a CELLAR agreement title). Titles are never compared.

Every candidate carries its method, evidence and confidence and is
``proposed``; a reviewer ``accepts`` or ``rejects`` it with a reason and may
``revert`` a decision. Accepted and rejected decisions (and reverts) are
recorded as :class:`src.kb.entity_history.EntityHistoryStore` ``match`` /
``non-match`` / ``undo`` decisions; records are never merged. A participant or
treaty without a candidate stays visible as ``unmatched``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.treaties_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    TreatiesError,
    authorize,
    canonical,
    digest,
    load,
    table_exists,
)

CONTRACT = "noesis-treaty-identity-candidate-v1"
CONFIDENCE = {"published-code": 0.95, "shared-identifier": 0.95, "published-cross-reference": 0.9,
              "exact-name-coded-place": 0.5, "name-only": 0.1}
NEVER_ACCEPTED = frozenset({"name-only"})
PLACE_CODE_KEYS = ("iso3166-1-alpha2", "iso3166-1-alpha3")
CODE_SCHEMES = {"iso3166-1-alpha2": "iso3166-1-alpha2", "iso3166-1-alpha3": "iso3166-1-alpha3",
                "op-country": "iso3166-1-alpha3"}
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS treaty_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  target_key TEXT, target_json TEXT, method TEXT, confidence DOUBLE, evidence_json TEXT NOT NULL, state TEXT NOT NULL,
  decision_id TEXT, reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
"""


def entity_for(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


class TreatiesIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.entity_history import EntityHistoryStore
        from src.kb.treaties_store import TreatyStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = TreatyStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ inputs

    def _places(self, geo_namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, r.revision_id, r.canonical_name, r.names_json, r.source_ids_json FROM "
            "geospatial_places p JOIN geospatial_place_current c ON c.place_id=p.place_id JOIN "
            "geospatial_place_revisions r ON r.revision_id=c.revision_id WHERE p.namespace IN (?, 'global') "
            "ORDER BY p.place_id", [geo_namespace]).fetchall()
        out = []
        for place_id, revision_id, name, names, ids in rows:
            labels = {str(name).casefold()} | {str(n.get("value")).casefold() for n in load(names, [])
                                                if isinstance(n, Mapping) and n.get("value")}
            out.append({"place_id": place_id, "place_revision_id": revision_id, "place_name": name,
                        "labels": labels, "source_ids": {str(k): str(v) for k, v in load(ids, {}).items()}})
        return out

    def _canonical(self, name: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "canonical_entities"):
            return []
        return [{"canonical_id": r[0], "preferred_name": r[1], "entity_type": r[2]} for r in self.conn.execute(
            "SELECT canonical_id, preferred_name, entity_type FROM canonical_entities WHERE lower(preferred_name)="
            "lower(?) ORDER BY canonical_id", [name]).fetchall()]

    # ------------------------------------------------------------------ assertions

    def _offer(self, namespace: str, kind: str, subject: str, target_key: str | None, target: Mapping[str, Any] | None,
               method: str | None, evidence: Mapping[str, Any], principal_id: str, *, state: str = "proposed",
               reason: str | None = None) -> tuple[str, bool]:
        assertion_id = "treaty-idc:" + digest([namespace, kind, subject, target_key])[:24]
        row = self.conn.execute("SELECT state, method, evidence_json, history_json FROM treaty_identity_assertions "
                                "WHERE namespace=? AND assertion_id=?", [namespace, assertion_id]).fetchone()
        now = self.now()
        if row is None:
            self.conn.execute(
                "INSERT INTO treaty_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, assertion_id, kind, subject, target_key, None if target is None else canonical(target),
                 method, CONFIDENCE.get(method or ""), canonical(evidence), state, None, reason,
                 canonical([{"state": state, "by": principal_id, "at_ms": now, "reason": reason}]), principal_id, now])
            return assertion_id, True
        old_state, old_method, old_evidence, history = row[0], row[1], load(row[2], {}), load(row[3], [])
        changed = old_method != method or digest(old_evidence) != digest(dict(evidence))
        if old_state in {"accepted", "rejected"} or not changed:
            return assertion_id, False  # a reviewed decision stands until reverted; nothing new to propose
        history.append({"state": state, "by": principal_id, "at_ms": now, "change": "reproposed",
                        "previous_state": old_state, "previous_method": old_method, "previous_evidence": old_evidence})
        self.conn.execute("UPDATE treaty_identity_assertions SET state=?, method=?, confidence=?, evidence_json=?, "
                          "target_json=?, decision_id=NULL, reason=?, history_json=? WHERE namespace=? AND "
                          "assertion_id=?", [state, method, CONFIDENCE.get(method or ""), canonical(evidence),
                                             None if target is None else canonical(target), reason,
                                             canonical(history), namespace, assertion_id])
        return assertion_id, True

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                geo_namespace: str = "global") -> dict[str, Any]:
        """Offer every participant and treaty; idempotent. Reviewed decisions stand until reverted."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        places = self._places(geo_namespace) if "knowledge:geospatial:read" in scopes else []
        created: list[str] = []
        for participant in self.store.participants(namespace):
            key, name = participant["participant_key"], participant["name_as_published"]
            left = {"participant_key": key, "name_as_published": name, "kind": participant["kind"],
                    "provider": participant["provider"], "published_codes": participant["published_codes"],
                    "treaties": participant["treaties"]}
            offered = False
            codes = {(CODE_SCHEMES[c["scheme"]], str(c["value"]).upper()) for c in participant["published_codes"]
                     if c.get("scheme") in CODE_SCHEMES}
            by_code = [p for p in places if any(p["source_ids"].get(s, "").upper() == v for s, v in codes)]
            by_name = [p for p in places if name.casefold() in p["labels"]
                       and any(p["source_ids"].get(k) for k in PLACE_CODE_KEYS)]
            for method, matches in (("published-code", by_code), ("exact-name-coded-place", by_name)):
                if len({p["place_id"] for p in matches}) != 1:
                    continue
                place = matches[0]
                target = {"place_id": place["place_id"], "place_revision_id": place["place_revision_id"],
                          "place_name": place["place_name"],
                          "codes": {k: place["source_ids"][k] for k in PLACE_CODE_KEYS if place["source_ids"].get(k)}}
                evidence = {"method": method, "participant": left, "place": target,
                            "codes_compared": sorted(f"{s}:{v}" for s, v in codes) if method == "published-code"
                            else None,
                            "note": "a published code identifies the place" if method == "published-code" else
                            "the name as published equals a place carrying an ISO 3166-1 code; a reviewer decides"}
                assertion_id, new = self._offer(namespace, "participant-place", key, f"place:{place['place_id']}",
                                                target, method, evidence, principal_id)
                created += [assertion_id] if new else []
                offered = True
                break
            for entity in self._canonical(name):
                assertion_id, new = self._offer(
                    namespace, "participant-entity", key, f"canonical:{entity['canonical_id']}", entity, "name-only",
                    {"method": "name-only", "participant": left, "entity": entity,
                     "note": "an equal name alone is never an identity"}, principal_id)
                created += [assertion_id] if new else []
            if not offered:
                reason = "no place carries a published code or the exact name with an ISO 3166-1 code" if places \
                    else "no geospatial places are available (or knowledge:geospatial:read is missing)"
                assertion_id, new = self._offer(namespace, "participant-place", key, None, None, None,
                                                {"participant": left}, principal_id, state="unmatched", reason=reason)
                created += [assertion_id] if new else []
        created += self._propose_treaties(namespace, principal_id)
        return {"created": sorted(set(created)), "candidates": self.candidates(namespace, scopes=scopes),
                "unmatched": self.unmatched(namespace, scopes=scopes),
                "notice": "published identifiers first; nothing is merged or accepted automatically"}

    def _treaties(self, namespace: str) -> list[dict[str, Any]]:
        out = []
        for key in self.store.treaty_keys(namespace):
            row = self.store.as_of(namespace, key)
            if row is None:
                continue
            fields = self.store.record(row)["fields"]
            out.append({"treaty_key": key, "provider": row["provider"], "revision_id": row["revision_id"],
                        "title_as_published": row["title"],
                        "identifiers": [(i["scheme"], str(i["value"])) for i in fields.get("identifiers") or []],
                        "cross_references": [c for c in fields.get("cross_references") or []]})
        return out

    def _propose_treaties(self, namespace: str, principal_id: str) -> list[str]:
        treaties = self._treaties(namespace)
        created, matched = [], set()
        for i, left in enumerate(treaties):
            for right in treaties[i + 1:]:
                if left["provider"] == right["provider"]:
                    continue
                shared = sorted(set(left["identifiers"]) & set(right["identifiers"]))
                cited = [c for a, b in ((left, right), (right, left)) for c in a["cross_references"]
                         if (c["scheme"], str(c["value"])) in set(b["identifiers"])]
                if not shared and not cited:
                    continue
                method = "shared-identifier" if shared else "published-cross-reference"
                a, b = sorted((left, right), key=lambda t: t["treaty_key"])
                evidence = {"method": method, "shared_identifiers": [f"{s}:{v}" for s, v in shared],
                            "cross_references": cited,
                            "records": [{k: t[k] for k in ("treaty_key", "provider", "revision_id",
                                                           "title_as_published")} for t in (a, b)],
                            "note": "matched through published identifiers only; titles are never compared"}
                assertion_id, new = self._offer(namespace, "treaty", a["treaty_key"], b["treaty_key"],
                                                {"treaty_key": b["treaty_key"], "revision_id": b["revision_id"]},
                                                method, evidence, principal_id)
                created += [assertion_id] if new else []
                matched |= {a["treaty_key"], b["treaty_key"]}
        for treaty in treaties:
            if treaty["treaty_key"] not in matched and not self.conn.execute(
                    "SELECT 1 FROM treaty_identity_assertions WHERE namespace=? AND kind='treaty' AND "
                    "(subject_key=? OR target_key=?) AND target_key IS NOT NULL",
                    [namespace, treaty["treaty_key"], treaty["treaty_key"]]).fetchone():
                assertion_id, new = self._offer(namespace, "treaty", treaty["treaty_key"], None, None, None,
                                                {"record": treaty}, principal_id, state="unmatched",
                                                reason="no other acquired source publishes a shared identifier or a "
                                                       "cross-reference")
                created += [assertion_id] if new else []
        return created

    # ------------------------------------------------------------------ reads

    def assertion(self, namespace: str, assertion_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_key, target_key, target_json, method, confidence, evidence_json, state, "
            "decision_id, reason, history_json, created_by, created_at_ms FROM treaty_identity_assertions WHERE "
            "namespace=? AND assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise TreatiesError("not_found", "no treaty identity candidate with that id")
        history = load(row[11], [])
        reviewed = [h for h in history if h["state"] in {"accepted", "rejected", "reverted"}]
        return {"contract": CONTRACT, "namespace": namespace, "candidate_id": row[0], "kind": row[1],
                "subject": row[2], "target": row[3], "target_detail": load(row[4], None), "method": row[5],
                "confidence": row[6], "evidence": load(row[7], {}), "state": row[8], "decision_id": row[9],
                "reason": row[10], "history": history, "reviewer": reviewed[-1]["by"] if reviewed else None,
                "created_by": row[12], "created_at_ms": row[13],
                "notice": "a reviewable identity assertion; records are never merged"}

    def candidates(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   subject: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "treaty_identity_assertions"):
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM treaty_identity_assertions WHERE namespace=? AND target_key IS NOT NULL AND "
            "(? IS NULL OR kind=?) AND (? IS NULL OR subject_key=? OR target_key=?) AND (? IS NULL OR state=?) "
            "ORDER BY kind, subject_key, assertion_id",
            [namespace, kind, kind, subject, subject, subject, state, state]).fetchall()
        return [self.assertion(namespace, r[0]) for r in rows]

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Participants and treaties with no usable candidate: kept visible, never hidden."""
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "treaty_identity_assertions"):
            return []
        out = []
        for kind, subject, reason, evidence in self.conn.execute(
                "SELECT kind, subject_key, reason, evidence_json FROM treaty_identity_assertions a WHERE namespace=? "
                "AND state='unmatched' AND NOT EXISTS (SELECT 1 FROM treaty_identity_assertions b WHERE "
                "b.namespace=a.namespace AND b.kind=a.kind AND (b.subject_key=a.subject_key OR "
                "b.target_key=a.subject_key) AND b.target_key IS NOT NULL AND b.state IN ('proposed','accepted')) "
                "ORDER BY kind, subject_key", [namespace]).fetchall():
            out.append({"kind": kind, "subject": subject, "status": "unmatched", "reason": reason,
                        "as_published": load(evidence, {}).get("participant") or load(evidence, {}).get("record")})
        return out

    # ------------------------------------------------------------------ reviews

    def _entities(self, candidate: Mapping[str, Any]) -> list[str]:
        return [entity_for(candidate["subject"]), entity_for(candidate["target"])]

    def _transition(self, namespace, candidate, state, decision_id, principal_id, reason) -> dict[str, Any]:
        history = candidate["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                           "decision_id": decision_id}]
        self.conn.execute("UPDATE treaty_identity_assertions SET state=?, decision_id=?, reason=?, history_json=? "
                          "WHERE namespace=? AND assertion_id=?", [state, decision_id, reason, canonical(history),
                                                                   namespace, candidate["candidate_id"]])
        return self.assertion(namespace, candidate["candidate_id"])

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept (``match``) or reject (``non-match``) a candidate as an entity identity decision."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise TreatiesError("invalid_decision", "accept or reject with a reason")
        candidate = self.assertion(namespace, candidate_id)
        if candidate["state"] != "proposed":
            raise TreatiesError("invalid_state", f"candidate is {candidate['state']}; propose again to re-review")
        if decision == "accept" and candidate["method"] in NEVER_ACCEPTED:
            raise TreatiesError("insufficient_evidence", "an equal name alone never produces an accepted match")
        entities = self._entities(candidate)
        for entity, alias in zip(entities, (candidate["subject"], candidate["target"])):
            self.history.register_entity(namespace, entity, [alias], principal_id=principal_id,
                                         scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", entities,
            {"candidate_id": candidate_id, "basis": candidate["method"], "confidence": candidate["confidence"],
             "evidence": candidate["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "legal.treaties", "records": [candidate["subject"], candidate["target"]]},
             "policy": {"merge": False, "note": "identity decision only; records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"treaties-identity:{namespace}:{candidate_id}")
        return self._transition(namespace, candidate, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise TreatiesError("invalid_decision", "a revert needs a reason")
        candidate = self.assertion(namespace, candidate_id)
        if candidate["state"] not in {"accepted", "rejected"}:
            raise TreatiesError("invalid_state", "only an accepted or rejected candidate can be reverted")
        undo = self.history.undo(namespace, candidate["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, candidate, "reverted", undo["decision_id"], principal_id, reason.strip())

    # ------------------------------------------------------------------ accepted matches (used by queries and links)

    def _accepted(self, namespace: str, kind: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "treaty_identity_assertions"):
            return []
        return [self.assertion(namespace, r[0]) for r in self.conn.execute(
            "SELECT assertion_id FROM treaty_identity_assertions WHERE namespace=? AND kind=? AND state='accepted' "
            "ORDER BY assertion_id", [namespace, kind]).fetchall()]

    def accepted_place(self, namespace: str, participant: str) -> dict[str, Any] | None:
        for item in self._accepted(namespace, "participant-place"):
            if item["subject"] == participant:
                return {**item["target_detail"], "candidate_id": item["candidate_id"], "method": item["method"],
                        "reviewer": item["reviewer"], "decision_id": item["decision_id"]}
        return None

    def participants_for_place(self, namespace: str, place: str) -> list[dict[str, Any]]:
        """Participant keys linked to a place (place id or ISO 3166-1 code) by an accepted, unreverted match."""
        wanted = str(place).removeprefix("iso3166:").upper()
        out = []
        for item in self._accepted(namespace, "participant-place"):
            detail = item["target_detail"] or {}
            codes = {str(v).upper() for v in dict(detail.get("codes") or {}).values()}
            if place == detail.get("place_id") or wanted in codes:
                out.append({"participant_key": item["subject"], "candidate_id": item["candidate_id"],
                            "method": item["method"], "reviewer": item["reviewer"], "place": detail})
        return out

    def related_treaties(self, namespace: str, treaty: str) -> list[dict[str, Any]]:
        """Treaty records of other sources linked to ``treaty`` by an accepted, unreverted match."""
        out = []
        for item in self._accepted(namespace, "treaty"):
            if treaty in {item["subject"], item["target"]}:
                other = item["target"] if item["subject"] == treaty else item["subject"]
                out.append({"treaty_key": other, "candidate_id": item["candidate_id"], "method": item["method"],
                            "reviewer": item["reviewer"]})
        return out


__all__ = ["CONFIDENCE", "CONTRACT", "NEVER_ACCEPTED", "TreatiesIdentity", "entity_for"]

