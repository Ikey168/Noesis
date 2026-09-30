"""Sponsors, cosponsors and voting members matched to political identities through review (#2208, LT07).

A member is the identifier a source publishes - a bioguide ID (congress.gov,
BILLSTATUS), a Senate LIS member id (senate.gov) or a UK Parliament member id
(Bills, Votes and Hansard APIs) - kept as an *external identifier*
(``legislation:member:<scheme>:<id>``); nothing is merged by name. Links to
existing political identities are *proposed* into the shared reviewable state
machine (:class:`src.kb.ownership_identity.OwnershipIdentityService`, the one
:mod:`src.kb.elections_identity` uses), whose accepted and reverted decisions
are :class:`src.kb.entity_history.EntityHistoryStore` decisions:

* ``name-jurisdiction`` - an election candidate record
  (:meth:`src.kb.elections_identity.ElectionIdentity.subjects`) in the same
  country with an equal normalized name, or a Political pack ``person`` whose
  scoped alias resolves to exactly one person in that jurisdiction;
* ``similar-name`` - a canonical entity alias; shown, never acceptable.

Every proposal carries its method, evidence (the member's published names,
the records it appears in, and the candidate's side) and the shared confidence
for its basis. Party, state or constituency are never taken from the match:
they are what each record stated at the vote or sponsorship date. Members
without an accepted decision stay visible as ``unmatched``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.legislation import READ_SCOPE, LegislationError, LegislationStore, authorize, table_exists

MEMBER_RECORD_KINDS = ("us-bill", "us-bill-status", "us-roll-call", "uk-bill", "uk-division", "uk-debate-reference")


def _norm(value: Any) -> str:
    from src.kb.elections import normalize_name

    return normalize_name(value)


def member_entity(member_key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(member_key)


def display_name(person: Mapping[str, Any]) -> str | None:
    first, last = person.get("first_name"), person.get("last_name")
    if first and last:
        return f"{first} {last}"
    return person.get("name_as_published")


def _appearances(record: Mapping[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """(person, appearance) pairs for one record, with the source's party and place at that date."""
    fields = record.get("fields") or {}
    kind = record["record_kind"]
    out = []
    if kind in {"us-bill", "us-bill-status"}:
        for person in fields.get("sponsors") or []:
            out.append((person, {"role": "sponsor", "date": fields.get("introduced_date")}))
        for person in fields.get("cosponsors") or []:
            out.append((person, {"role": "cosponsor", "date": person.get("sponsorship_date"),
                                 "withdrawn_date": person.get("withdrawn_date")}))
    elif kind in {"us-roll-call", "uk-division"}:
        for person in fields.get("positions") or []:
            out.append((person, {"role": "voter", "date": fields.get("date"), "position": person.get("position")}))
    elif kind == "uk-bill":
        for person in fields.get("sponsors") or []:
            if person.get("member_key"):
                out.append((person, {"role": "sponsor", "date": fields.get("introduced_date"),
                                     "organisation": person.get("organisation")}))
    elif kind == "uk-debate-reference":
        for person in fields.get("contributions") or []:
            if person.get("member_key"):
                out.append(({**person, "name_as_published": person.get("attributed_to"), "scheme": "uk-parliament"},
                            {"role": "contributor", "date": fields.get("date")}))
    return out


class LegislationIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = LegislationStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)

    # ------------------------------------------------------------------ subjects

    def members(self, namespace: str, *, scopes: Iterable[str], bill_key: str | None = None
                ) -> list[dict[str, Any]]:
        """Every member the acquired records name, with each appearance's party and place as published."""
        scopes = set(scopes)
        rows = self.store.records(namespace, scopes=scopes, kinds=MEMBER_RECORD_KINDS)
        members: dict[str, dict[str, Any]] = {}
        for row in rows:
            record = row["record"] or {}
            if bill_key and bill_key not in {row["bill_key"], row["candidate_bill_key"]}:
                continue
            for person, appearance in _appearances(record):
                key = person.get("member_key")
                if not key:
                    continue
                member = members.setdefault(key, {
                    "member_key": key, "scheme": person.get("scheme"), "member_id": person.get("member_id"),
                    "jurisdiction": row["jurisdiction"], "entity_id": member_entity(key), "names": set(),
                    "appearances": []})
                name = display_name(person)
                if name:
                    member["names"].add(name)
                member["appearances"].append({
                    **appearance, "record_key": row["record_key"], "record_kind": row["record_kind"],
                    "source_id": row["source_id"], "bill_key": row["bill_key"] or row["candidate_bill_key"],
                    "party": person.get("party"), "state": person.get("state"), "district": person.get("district"),
                    "constituency": person.get("constituency"), "citation": row["citation"],
                    "as_of_basis": "as the record stated it at this date"})
        out = []
        for member in members.values():
            member["names"] = sorted(member["names"])
            member["appearances"].sort(key=lambda a: (a["date"] or "", a["record_key"], a["role"]))
            out.append(member)
        return sorted(out, key=lambda m: m["member_key"])

    # ------------------------------------------------------------------ proposals

    def _election_subjects(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "election_candidates"):
            return []
        from src.kb.elections_identity import ElectionIdentity

        try:
            subjects = ElectionIdentity(self.conn, initialize=False).subjects(namespace)
        except Exception:  # noqa: BLE001 - the elections feature is optional; absent means no candidates
            return []
        return [s for s in subjects if s["kind"] == "candidate"]

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                political_jurisdictions: Mapping[str, str] | None = None, canonical_names: bool = True,
                ) -> dict[str, Any]:
        """Offer reviewable candidates; idempotent, and never an automatic merge."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        members = self.members(namespace, scopes=scopes)
        offered = []
        candidates = self._election_subjects(namespace)
        for member in members:
            names = {_norm(n) for n in member["names"] if n}
            country = member["jurisdiction"]
            for subject in candidates:
                subject_country = str(subject.get("jurisdiction") or "").split("-")[0].upper()
                if subject_country != country or _norm(subject["label"]) not in names:
                    continue
                offered.append(self._offer(namespace, member, subject["record_key"], subject["entity_id"],
                                           "name-jurisdiction", {
                                               "kind": "election-candidate", "value": subject["label"],
                                               "country": country, "right": subject["side"],
                                               "election_id": subject.get("election_id"),
                                               "note": "equal names in one country are a weak signal; a reviewer "
                                                       "decides"}, principal_id, scopes))
            scope = (political_jurisdictions or {}).get(country)
            if scope and table_exists(self.conn, "political_aliases"):
                from src.domains.political.model import resolve_alias

                for name in member["names"]:
                    found = resolve_alias(self.conn, name, object_type="person", jurisdiction_id=scope)
                    if found["status"] != "resolved":
                        continue
                    key = f"political:{found['object']['object_id']}"
                    offered.append(self._offer(namespace, member, key, member_entity(key), "name-jurisdiction", {
                        "kind": "political-alias", "value": name, "right": {"record_key": key,
                                                                            "object_id": found["object"]["object_id"],
                                                                            "jurisdiction_id": scope},
                        "note": "a scoped alias of one Political pack person; a reviewer decides"},
                        principal_id, scopes))
            if canonical_names and table_exists(self.conn, "entity_aliases"):
                from src.kb.entities import resolve

                for name in member["names"]:
                    found = resolve(self.conn, name)
                    if not found:
                        continue
                    key = f"canonical:{found['canonical_id']}"
                    offered.append(self._offer(namespace, member, key, found["canonical_id"], "similar-name", {
                        "kind": "name", "value": name, "right": {"record_key": key,
                                                                "canonical_id": found["canonical_id"]},
                        "note": "a name alone is never an identity"}, principal_id, scopes))
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes)}

    def _offer(self, namespace, member, right_key, right_entity, basis, evidence, principal_id, scopes):
        return self.service.offer(
            namespace, left_key=member["member_key"], right_key=right_key, left_entity=member["entity_id"],
            right_entity=right_entity, basis=basis,
            evidence=[{**evidence, "method": basis,
                       "left": {"record_key": member["member_key"], "external_identifier": {
                           "scheme": member["scheme"], "value": member["member_id"]},
                           "names_as_published": member["names"],
                           "records": sorted({a["record_key"] for a in member["appearances"]})}}],
            principal_id=principal_id, scopes=scopes)

    # ------------------------------------------------------------------ review and reads

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        return {
            "candidate_id": candidate["candidate_id"], "state": candidate["state"],
            "review_state": {"accepted": "reviewed-match", "rejected": "reviewed-non-match",
                             "reverted": "reverted", "proposed": "unreviewed-candidate"}[candidate["state"]],
            "method": candidate["basis"], "confidence": candidate["confidence"],
            "records": [candidate["left_key"], candidate["right_key"]],
            "entities": [candidate["left_entity"], candidate["right_entity"]],
            "decision_id": candidate["decision_id"],
            "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
            "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
            "notice": "a reviewable identity decision; members are never merged by name",
        }

    def candidates(self, namespace: str, *, scopes: Iterable[str], member_key: str | None = None
                   ) -> list[dict[str, Any]]:
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=member_key)
                if c["left_key"].startswith("legislation:") or c["right_key"].startswith("legislation:")]
        return [self.view(c) for c in rows]

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        self._own(namespace, candidate_id, scopes)
        return self.view(self.service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                                             scopes=scopes))

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        self._own(namespace, candidate_id, scopes)
        return self.view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                             scopes=scopes))

    def _own(self, namespace: str, candidate_id: str, scopes: Iterable[str]) -> None:
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise LegislationError("not_found", "no legislation identity candidate with that id")

    def identity(self, namespace: str, member_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Accepted links and open candidates for one member; without an accepted link the member is unmatched."""
        try:
            views = self.candidates(namespace, scopes=scopes, member_key=member_key)
        except Exception as exc:  # noqa: BLE001 - identity access is optional for an answer
            return {"state": "unmatched", "links": [], "candidates": [], "unavailable": [getattr(exc, "code", "x")]}
        links = [v for v in views if v["state"] == "accepted"]
        return {"state": "matched" if links else "unmatched",
                "links": [{"candidate_id": v["candidate_id"], "records": v["records"], "method": v["method"],
                           "confidence": v["confidence"], "reviewer": v["reviewer"]} for v in links],
                "candidates": [v["candidate_id"] for v in views if v["state"] == "proposed"]}

    def unmatched(self, namespace: str, *, scopes: Iterable[str], bill_key: str | None = None
                  ) -> list[dict[str, Any]]:
        return [{"member_key": m["member_key"], "names": m["names"], "external_identifier": {
                    "scheme": m["scheme"], "value": m["member_id"]}, "state": "unmatched"}
                for m in self.members(namespace, scopes=scopes, bill_key=bill_key)
                if self.identity(namespace, m["member_key"], scopes=scopes)["state"] == "unmatched"]
