"""Organisations and project participants matched through reviewable identity (#2579, RE07 #2614).

Two kinds of match are proposed, and nothing is ever accepted or merged automatically:

* **ROR organisation <-> CORDIS participant** (PIC), kept in ``rentity_identity_matches``. CORDIS publishes no ROR,
  GRID, ISNI or Wikidata identifier for its participants, so no published identifier ties the two registries; the
  bases are ``website-domain-country`` (the participant's published website host is a ROR domain or website host of
  a ROR record in the same country) and ``name-country`` (equal normalised names in the same country; low evidence).
* **ROR organisation or CORDIS participant <-> ownership legal entity** (LEI records from GLEIF, BODS and register
  entities), offered into the shared state machine :class:`src.kb.ownership_identity.OwnershipIdentityService` with
  keys starting ``research-entities:`` (in ``FOREIGN_KEY_PREFIXES``, so they never regroup ownership entities). Published
  identifiers come first: ``exact-identifier`` when a ROR external identifier (ISNI, Wikidata, GRID, FundRef) or a
  participant's VAT number equals one the ownership record carries; ``name-jurisdiction`` only when no identifier is
  shared (low evidence).

Every match carries its method, evidence and confidence and moves through proposed -> accepted or rejected ->
reverted; accept and reject are entity identity decisions in :class:`src.kb.entity_history.EntityHistoryStore`
(``match`` / ``non-match``, ``policy.merge`` false), revert appends an ``undo``. Only accepted matches are used by
queries and links; organisations and participants without one stay visible as **unmatched**.

Researchers are never subjects of identity matching or entity merges (RE01 minimisation decision): an ORCID iD is its
own identity, and researchers reach papers only through the works they assert in ORCID (:mod:`src.kb.research_entities_links`).
There is no name-based author matching anywhere in this feature.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from src.kb.research_entities_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    ResearchEntitiesError,
    ResearchEntitiesStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-research-entity-match-v1"
KEY_PREFIX = "research-entities:"
CONFIDENCE = {"website-domain-country": 0.6, "name-country": 0.35}
STATES = ("proposed", "accepted", "rejected", "reverted")
VAT_SCHEMES = frozenset({"vat", "eu-vat", "vat-number"})
ROR_SCHEMES = {"isni": {"isni"}, "wikidata": {"wikidata", "wikidata-qid"}, "grid": {"grid"},
               "fundref": {"fundref", "crossref-funder-id"}}
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS rentity_identity_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, ror_id TEXT NOT NULL, pic TEXT NOT NULL, basis TEXT NOT NULL,
  confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT,
  history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, match_id)
);
"""


def _name(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def _value(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum()).lstrip("0") or "0"


def _host(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    host = (urlsplit(raw if "//" in raw else "https://" + raw).hostname or "").casefold()
    return host.removeprefix("www.") or None


def ror_key(ror_id: str) -> str:
    return f"{KEY_PREFIX}ror:{ror_id.rsplit('/', 1)[-1]}"


def pic_key(pic: str) -> str:
    return f"{KEY_PREFIX}pic:{pic}"


def _entity(key: str) -> str:
    return "ent-rentity-" + re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-")


class ResearchEntitiesIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ResearchEntitiesStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "rentity_identity_matches")

    # ------------------------------------------------------------------ subjects

    def _current(self, namespace: str, kind: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        out = []
        for record in self.store.records(namespace, kind=kind):
            revision, _ = self.store.revision_as_of(namespace, record["record_id"], None)
            if revision is None:
                continue
            out.append((revision, self.store.statement(namespace, revision["revision_id"])))
        return out

    def organisations(self, namespace: str) -> list[dict[str, Any]]:
        """Held ROR organisations in their current revision (removed ones excluded)."""
        out = []
        for revision, statement in self._current(namespace, "organisation"):
            body = statement["body"]
            if not body:
                continue
            hosts = {h for h in ([_host(d) for d in body.get("domains") or []]
                                 + [_host(link.get("value")) for link in body.get("links") or []]) if h}
            out.append({"key": ror_key(body["ror_id"]), "ror_id": body["ror_id"], "status": statement["status"],
                        "display_name": body["display_name"], "names": sorted({n["value"] for n in body["names"]}),
                        "countries": sorted({str(loc.get("country_code") or "").upper()
                                             for loc in body.get("locations") or [] if loc.get("country_code")}),
                        "hosts": sorted(hosts), "external_ids": body.get("external_ids") or [],
                        "revision_id": revision["revision_id"]})
        return out

    def participants(self, namespace: str) -> list[dict[str, Any]]:
        """CORDIS participants (by PIC) across the current project revisions, with names as published."""
        grouped: dict[str, dict[str, Any]] = {}
        for revision, statement in self._current(namespace, "project"):
            for participant in statement["body"].get("participants") or []:
                item = grouped.setdefault(participant["pic"], {
                    "key": pic_key(participant["pic"]), "pic": participant["pic"], "names": set(), "countries": set(),
                    "hosts": set(), "vat_numbers": set(), "projects": [], "revision_ids": set()})
                item["names"].add(participant.get("name") or "")
                if participant.get("country"):
                    item["countries"].add(str(participant["country"]).upper())
                if _host(participant.get("website")):
                    item["hosts"].add(_host(participant["website"]))
                if participant.get("vat_number"):
                    item["vat_numbers"].add(participant["vat_number"])
                item["projects"].append(statement["native_id"])
                item["revision_ids"].add(revision["revision_id"])
        return [{**v, "names": sorted(n for n in v["names"] if n), "countries": sorted(v["countries"]),
                 "hosts": sorted(v["hosts"]), "vat_numbers": sorted(v["vat_numbers"]),
                 "projects": sorted(set(v["projects"])), "revision_ids": sorted(v["revision_ids"])}
                for _, v in sorted(grouped.items())]

    # ------------------------------------------------------------------ proposals

    def _evaluate(self, organisation: Mapping[str, Any], participant: Mapping[str, Any]) -> dict[str, Any] | None:
        shared_country = sorted(set(organisation["countries"]) & set(participant["countries"]))
        if not shared_country:
            return None
        hosts = sorted(set(organisation["hosts"]) & set(participant["hosts"]))
        if hosts:
            return {"basis": "website-domain-country", "evidence": {
                "hosts": hosts, "country": shared_country, "participant_names": participant["names"],
                "ror_names": organisation["names"],
                "note": "the participant's published website is a ROR website or domain in the same country"}}
        names = {_name(n) for n in organisation["names"]} & {_name(n) for n in participant["names"]}
        if names:
            return {"basis": "name-country", "evidence": {
                "normalized_names": sorted(names), "country": shared_country,
                "participant_names": participant["names"], "ror_names": organisation["names"], "low_evidence": True,
                "note": "equal normalised names in one country are low evidence; a reviewer decides"}}
        return None

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                ownership_namespace: str | None = None) -> dict[str, Any]:
        """Propose ROR <-> participant candidates and, with an ownership namespace, ownership candidates;
        idempotent, never accepts. Published identifiers are tried before names."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.conn.execute(_DDL)
        organisations, participants = self.organisations(namespace), self.participants(namespace)
        created = []
        for participant in participants:
            for organisation in organisations:
                found = self._evaluate(organisation, participant)
                if found is None:
                    continue
                match_id = "rentity-match:" + digest([namespace, organisation["ror_id"], participant["pic"]])[:24]
                if self.conn.execute("SELECT 1 FROM rentity_identity_matches WHERE namespace=? AND match_id=?",
                                     [namespace, match_id]).fetchone():
                    continue
                evidence = {**found["evidence"], "method": found["basis"],
                            "ror_revision_id": organisation["revision_id"],
                            "participant_revision_ids": participant["revision_ids"]}
                now = self.now()
                self.conn.execute(
                    "INSERT INTO rentity_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [namespace, match_id, organisation["ror_id"], participant["pic"], found["basis"],
                     CONFIDENCE[found["basis"]], canonical(evidence), "proposed", None,
                     canonical([{"state": "proposed", "by": principal_id, "at_ms": now}]), principal_id, now])
                created.append(match_id)
        if ownership_namespace is not None:
            created += self._offer_ownership(namespace, ownership_namespace, organisations, participants,
                                             principal_id=principal_id, scopes=scopes)
        return {"proposed": created, "matches": self.matches(namespace, scopes={"operator"}),
                "unmatched": self.unmatched(namespace, scopes={"operator"})}

    def _offer_ownership(self, namespace, ownership_namespace, organisations, participants, *, principal_id, scopes):
        from src.kb.ownership_identity import OwnershipIdentityService
        from src.kb.ownership_store import OwnershipError

        if not table_exists(self.conn, "ownership_records"):
            return []
        service = OwnershipIdentityService(self.conn, now=self.now)
        try:
            entities = service._entities(ownership_namespace, principal_id, scopes)
        except OwnershipError as exc:
            raise ResearchEntitiesError(exc.code, str(exc)) from exc
        subjects = []
        for organisation in organisations:
            identifiers = {(scheme, _value(v)) for e in organisation["external_ids"]
                           for scheme in ROR_SCHEMES.get(e["type"], {e["type"]}) for v in e["all"]}
            subjects.append((organisation["key"], organisation["names"], organisation["countries"], identifiers,
                             {"ror_id": organisation["ror_id"], "revision_id": organisation["revision_id"]}))
        for participant in participants:
            identifiers = {(scheme, _value(v)) for v in participant["vat_numbers"] for scheme in VAT_SCHEMES}
            subjects.append((participant["key"], participant["names"], participant["countries"], identifiers,
                             {"pic": participant["pic"], "revision_ids": participant["revision_ids"]}))
        created = []
        for key, names, countries, identifiers, left in subjects:
            for entity in entities:
                body = entity["record"]
                right = {"record_key": body["record_key"], "provider": body["source"]["provider"],
                         "revision": entity["revision"], "revision_id": entity["revision_id"],
                         "ownership_namespace": ownership_namespace}
                right_entity = body.get("canonical_entity_id") or _entity(body["record_key"])
                shared = [i for i in body.get("identifiers") or []
                          if (str(i["scheme"]).casefold(), _value(i["value"])) in identifiers]
                if shared:
                    basis, evidence = "exact-identifier", {"identifiers": shared, "left": left, "right": right,
                                                          "method": "exact-identifier"}
                else:
                    jurisdiction = str(body.get("jurisdiction") or "").split("-")[0].upper()
                    if _name(body.get("name")) not in {_name(n) for n in names} or jurisdiction not in countries:
                        continue
                    basis, evidence = "name-jurisdiction", {
                        "normalized_name": _name(body.get("name")), "country": jurisdiction, "left": left,
                        "right": right, "method": "name-jurisdiction", "low_evidence": True,
                        "note": "a name is low evidence and never accepted automatically; a reviewer decides"}
                offered = service.offer(namespace, left_key=key, right_key=body["record_key"],
                                        left_entity=_entity(key), right_entity=right_entity, basis=basis,
                                        evidence=[evidence], principal_id=principal_id, scopes=scopes)
                if offered["created"] or offered.get("change"):
                    created.append(offered["candidate_id"])
        return created

    # ------------------------------------------------------------------ views

    def _internal(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, ror_id, pic, basis, confidence, evidence_json, state, decision_id, history_json, "
            "created_by, created_at_ms FROM rentity_identity_matches WHERE namespace=? AND match_id=?",
            [namespace, match_id]).fetchone()
        if row is None:
            raise ResearchEntitiesError("not_found", "identity match is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "match_id": row[0], "kind": "ror-participant",
                "left": {"key": ror_key(row[1]), "ror_id": row[1]}, "right": {"key": pic_key(row[2]), "pic": row[2]},
                "method": row[3], "confidence": row[4], "evidence": json.loads(row[5]), "state": row[6],
                "decision_id": row[7], "history": json.loads(row[8]), "created_by": row[9],
                "created_at_ms": row[10], "low_evidence": row[3] == "name-country",
                "usable": row[6] == "accepted",
                "notice": "a reviewable identity proposal; organisation and participant records are never merged"}

    @staticmethod
    def _ownership_view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        left, right = candidate["left_key"], candidate["right_key"]
        subject, other = (left, right) if left.startswith(KEY_PREFIX) else (right, left)
        ours = subject.split(":", 2)
        return {"contract": CONTRACT, "namespace": candidate["namespace"], "match_id": candidate["candidate_id"],
                "kind": "ownership",
                "left": {"key": subject, **({"ror_id": "https://ror.org/" + ours[2]} if ours[1] == "ror" else
                                            {"pic": ours[2]})},
                "right": {"key": other, "ownership_entity": candidate["right_entity"] if other == right
                          else candidate["left_entity"]},
                "method": candidate["basis"], "confidence": candidate["confidence"],
                "evidence": candidate["evidence"], "state": candidate["state"],
                "decision_id": candidate["decision_id"], "history": candidate["history"],
                "created_by": candidate["created_by"], "created_at_ms": candidate["created_at_ms"],
                "low_evidence": candidate["basis"] in {"name-jurisdiction", "similar-name"},
                "usable": candidate["state"] == "accepted",
                "notice": "a reviewable identity proposal in the shared ownership state machine; nothing is merged"}

    def _ownership_candidates(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return []
        from src.kb.ownership_identity import OwnershipIdentityService

        service = OwnershipIdentityService(self.conn, initialize=False, now=self.now)
        return [self._ownership_view(c) for c in service.candidates(namespace, scopes={"operator"})
                if c["left_key"].startswith(KEY_PREFIX) or c["right_key"].startswith(KEY_PREFIX)]

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None, ror: str | None = None,
                pic: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        out = []
        if self._ready():
            rows = self.conn.execute("SELECT match_id FROM rentity_identity_matches WHERE namespace=? "
                                     "ORDER BY ror_id, pic, match_id", [namespace]).fetchall()
            out += [self._internal(namespace, r[0]) for r in rows]
        out += self._ownership_candidates(namespace)
        return [m for m in out if (state is None or m["state"] == state)
                and (ror is None or m["left"].get("ror_id") == ror)
                and (pic is None or m["left"].get("pic") == pic or m["right"].get("pic") == pic)
                and (kind is None or m["kind"] == kind)]

    def match(self, namespace: str, match_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        found = [m for m in self.matches(namespace, scopes=scopes) if m["match_id"] == match_id]
        if not found:
            raise ResearchEntitiesError("not_found", "identity match is not visible in this namespace")
        return found[0]

    # ------------------------------------------------------------------ review

    def _transition(self, namespace, item, state, decision_id, principal_id, reason):
        history = item["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                      "decision_id": decision_id}]
        self.conn.execute("UPDATE rentity_identity_matches SET state=?, decision_id=?, history_json=? WHERE "
                          "namespace=? AND match_id=?",
                          [state, decision_id, canonical(history), namespace, item["match_id"]])
        return self._internal(namespace, item["match_id"])

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept or reject a proposed match with a reason, as an entity identity decision (never a merge)."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise ResearchEntitiesError("invalid_decision", "accept or reject with a reason")
        item = self.match(namespace, match_id, scopes={"operator"})
        if item["kind"] == "ownership":
            from src.kb.ownership_identity import OwnershipIdentityService
            from src.kb.ownership_store import OwnershipError

            try:
                reviewed = OwnershipIdentityService(self.conn, now=self.now).review(
                    namespace, match_id, decision, reason, principal_id=principal_id, scopes=scopes)
            except OwnershipError as exc:
                raise ResearchEntitiesError(exc.code, str(exc)) from exc
            return self._ownership_view(reviewed)
        if item["state"] not in {"proposed", "reverted"}:
            raise ResearchEntitiesError("invalid_state", f"match is {item['state']}; only a proposal is reviewed")
        left, right = _entity(item["left"]["key"]), _entity(item["right"]["key"])
        self.history.register_entity(namespace, left, [item["left"]["ror_id"]], principal_id=principal_id,
                                     scopes=_ENTITY_HISTORY_SCOPES)
        self.history.register_entity(namespace, right, [item["right"]["key"]], principal_id=principal_id,
                                     scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [left, right],
            {"match_id": match_id, "basis": item["method"], "confidence": item["confidence"],
             "evidence": item["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "science.research-entities", "records": [item["left"]["key"],
                                                                                  item["right"]["key"]]},
             "policy": {"merge": False, "note": "identity decision only; organisation and participant stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"research-entities-identity:{namespace}:{match_id}:{len(item['history'])}")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Undo an accepted or rejected match; it is no longer used until reviewed again."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise ResearchEntitiesError("invalid_decision", "a revert needs a reason")
        item = self.match(namespace, match_id, scopes={"operator"})
        if item["kind"] == "ownership":
            from src.kb.ownership_identity import OwnershipIdentityService
            from src.kb.ownership_store import OwnershipError

            try:
                reverted = OwnershipIdentityService(self.conn, now=self.now).revert(
                    namespace, match_id, reason, principal_id=principal_id, scopes=scopes)
            except OwnershipError as exc:
                raise ResearchEntitiesError(exc.code, str(exc)) from exc
            return self._ownership_view(reverted)
        if item["state"] not in {"accepted", "rejected"}:
            raise ResearchEntitiesError("invalid_state", "only an accepted or rejected match can be reverted")
        undo = self.history.undo(namespace, item["decision_id"], reviewer_id=principal_id, principal_id=principal_id,
                                 scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, item, "reverted", undo["decision_id"], principal_id, reason.strip())

    @staticmethod
    def refuse_researcher_match() -> None:
        """Researchers are excluded from identity matching and entity merges (RE01)."""
        raise ResearchEntitiesError("researcher_identity_excluded", "researchers are never matched or merged: an ORCID "
                                    "iD is its own identity and works link only through ORCID assertions")

    # ------------------------------------------------------------------ use by queries and links

    def accepted_pics(self, namespace: str, ror: str) -> list[dict[str, Any]]:
        """Participants an accepted match ties to a ROR ID, with the match used."""
        return [{"pic": m["right"]["pic"], "match_id": m["match_id"], "method": m["method"],
                 "decision_id": m["decision_id"]}
                for m in self.matches(namespace, scopes={"operator"}, ror=ror, kind="ror-participant", state="accepted")]

    def accepted_ownership(self, namespace: str, key: str) -> list[dict[str, Any]]:
        return [{"ownership_key": m["right"]["key"], "ownership_entity": m["right"]["ownership_entity"],
                 "match_id": m["match_id"], "method": m["method"], "decision_id": m["decision_id"],
                 "evidence": m["evidence"]}
                for m in self.matches(namespace, scopes={"operator"}, kind="ownership", state="accepted")
                if m["left"]["key"] == key]

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
        """Organisations and participants without an accepted match, kept as published with pending counts."""
        views = self.matches(namespace, scopes=scopes)
        accepted = {k for m in views if m["state"] == "accepted" for k in (m["left"]["key"], m["right"]["key"])}
        pending: dict[str, int] = {}
        for m in views:
            if m["state"] == "proposed":
                for k in (m["left"]["key"], m["right"]["key"]):
                    pending[k] = pending.get(k, 0) + 1
        organisations = [{"ror_id": o["ror_id"], "display_name": o["display_name"], "status": "unmatched",
                          "pending_candidates": pending.get(o["key"], 0)}
                         for o in self.organisations(namespace) if o["key"] not in accepted]
        participants = [{"pic": p["pic"], "names": p["names"], "projects": p["projects"], "status": "unmatched",
                         "pending_candidates": pending.get(p["key"], 0)}
                        for p in self.participants(namespace) if p["key"] not in accepted]
        return {"organisations": organisations, "participants": participants}


__all__ = ["CONFIDENCE", "KEY_PREFIX", "ResearchEntitiesIdentity", "pic_key", "ror_key"]
