"""Respondents and authorities through reviewable identity (#2651, EN07).

Authorities are **source identities**: each action names the regulator that
published it (``us-sec``, ``uk-fca``, ``us-epa``, or the EU supervisory
authority the EDPB register names as lead, ``eu-sa-xx``) and
:func:`authority_identity` describes it; nothing is inferred about an
authority.

Organisation respondents (natural persons are **never** subjects, EN01) are
*proposed* against the legal entities of an ownership namespace into the
shared reviewable state machine
(:class:`src.kb.ownership_identity.OwnershipIdentityService`, whose accepted
and reverted decisions are :class:`src.kb.entity_history.EntityHistoryStore`
decisions with reviewer and time recorded); nothing is accepted automatically
and nothing is merged:

* ``exact-identifier`` first - an identifier the regulator published for the
  respondent (a CIK stated in an SEC release, an LEI or company number) equal
  to one an ownership record carries (``SCHEME_ALIASES``); an FCA Firm
  Reference Number or an EPA FRS id is kept as published but no ownership
  source carries one, so it never matches deterministically;
* ``name-jurisdiction`` - equal normalized names; **low evidence**, marked as
  such and never auto-accepted.

A respondent without an accepted match stays as published and is reported as
**unmatched**. Candidate keys start with ``enforcement:`` (in
``FOREIGN_KEY_PREFIXES``), so these links never regroup ownership entities.
When the ownership store is absent the proposal answers
``ownership_unavailable`` and every respondent stays unmatched.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.enforcement import (
    READ_SCOPE,
    EnforcementError,
    EnforcementStore,
    authorize,
    table_exists,
)

NAME_BASES = frozenset({"name-jurisdiction", "similar-name"})
SCHEME_ALIASES = {"sec-cik": {"sec-cik", "cik"}, "lei": {"lei"}, "gb-coh": {"gb-coh", "ra:RA000585"},
                  "nl-kvk": {"nl-kvk", "ra:RA000463"}}
AUTHORITIES = {
    "us-sec": {"name": "U.S. Securities and Exchange Commission", "jurisdiction": "US",
               "provider": "us-sec", "site": "https://www.sec.gov"},
    "uk-fca": {"name": "Financial Conduct Authority", "jurisdiction": "GB", "provider": "uk-fca",
               "site": "https://www.fca.org.uk"},
    "us-epa": {"name": "U.S. Environmental Protection Agency", "jurisdiction": "US", "provider": "us-epa-echo",
               "site": "https://echo.epa.gov"},
}


def authority_identity(authority: str) -> dict[str, Any]:
    """The source identity of an authority as the acquired records state it; never a resolved entity."""
    if authority in AUTHORITIES:
        return {"authority": authority, **AUTHORITIES[authority], "basis": "source identity (the publisher)"}
    if authority.startswith("eu-sa-"):
        code = authority.rsplit("-", 1)[-1].upper()
        return {"authority": authority, "name": f"supervisory authority of {code} (as the EDPB register codes it)",
                "jurisdiction": code, "provider": "edpb", "site": "https://www.edpb.europa.eu",
                "basis": "the lead or concerned authority code published in the Article 60 register"}
    raise EnforcementError("invalid_authority", "unknown enforcement authority")


def _scheme_set(scheme: str) -> set[str]:
    for group in SCHEME_ALIASES.values():
        if scheme in group:
            return group
    return {scheme}


def _value(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum()).lstrip("0") or "0"


def _name(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def entity_for(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


class EnforcementIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.initialize = initialize
        self.store = EnforcementStore(conn, initialize=initialize, now=self.now)
        self._service = None

    @property
    def service(self):
        if self._service is None:
            from src.kb.ownership_identity import OwnershipIdentityService

            self._service = OwnershipIdentityService(self.conn, now=self.now, initialize=self.initialize)
        return self._service

    # -------------------------------------------------------------- subjects

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Organisation respondents of the current revisions, as published; natural persons are never subjects."""
        authorize(namespace, scopes, READ_SCOPE)
        out = []
        for view in self.store.views(namespace, ("respondent",)):
            body = view["record"]
            if body["party_type"] != "organisation":
                continue
            out.append({"key": body["record_key"], "action_key": body["action_key"], "authority": body["authority"],
                        "name_as_published": body["name_as_published"],
                        "role_as_published": body["role_as_published"], "country": body.get("country"),
                        "identifiers": body.get("identifiers") or [], "revision_id": view["revision_id"]})
        return sorted(out, key=lambda s: s["key"])

    # ------------------------------------------------------------- proposals

    def propose(self, namespace: str, *, ownership_namespace: str, principal_id: str, scopes: Iterable[str]
                ) -> dict[str, Any]:
        """Offer identifier candidates first and name candidates as low evidence; idempotent, never accepts."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "ownership_records"):
            return {"status": "ownership_unavailable", "proposed": [], "candidates": [],
                    "unmatched": self.unmatched(namespace, scopes=scopes),
                    "note": "the Corporate Ownership store is not installed; respondents stay as published"}
        entities = [e for e in self.service._entities(ownership_namespace, principal_id, scopes)
                    if e["record"].get("kind") != "person"]
        offered = []
        for subject in self.subjects(namespace, scopes=scopes):
            left = {"record_key": subject["key"], "name_as_published": subject["name_as_published"],
                    "role_as_published": subject["role_as_published"], "revision_id": subject["revision_id"],
                    "authority": subject["authority"], "ownership_namespace": ownership_namespace}
            published = {(scheme, _value(i["value"])) for i in subject["identifiers"]
                         for scheme in _scheme_set(i["scheme"])}
            for entity in entities:
                body = entity["record"]
                right = {"record_key": body["record_key"], "provider": body["source"]["provider"],
                         "revision": entity["revision"]}
                right_entity = body.get("canonical_entity_id") or entity_for(body["record_key"])
                shared = [i for i in body.get("identifiers") or [] if (i["scheme"], _value(i["value"])) in published]
                if shared:
                    offered.append(self._offer(namespace, subject, body["record_key"], right_entity,
                                               "exact-identifier", {"identifiers": shared, "left": left,
                                                                    "right": right}, principal_id, scopes))
                    continue
                if not body.get("name") or _name(body.get("name")) != _name(subject["name_as_published"]):
                    continue
                offered.append(self._offer(namespace, subject, body["record_key"], right_entity, "name-jurisdiction", {
                    "normalized_name": _name(body.get("name")), "jurisdiction": body.get("jurisdiction"),
                    "left": left, "right": right, "low_evidence": True,
                    "note": "a name is low evidence and never accepted automatically; a reviewer decides"},
                    principal_id, scopes))
        return {"status": "proposed",
                "proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes),
                "unmatched": self.unmatched(namespace, scopes=scopes)}

    def _offer(self, namespace, subject, right_key, right_entity, basis, evidence, principal_id, scopes):
        return self.service.offer(namespace, left_key=subject["key"], right_key=right_key,
                                  left_entity=entity_for(subject["key"]), right_entity=right_entity, basis=basis,
                                  evidence=[{**evidence, "method": basis}], principal_id=principal_id, scopes=scopes)

    # --------------------------------------------------------------- reviews

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        left, right = candidate["left_key"], candidate["right_key"]
        subject, other = (left, right) if left.startswith("enforcement:") else (right, left)
        other_entity = candidate["right_entity"] if other == right else candidate["left_entity"]
        return {"candidate_id": candidate["candidate_id"], "state": candidate["state"],
                "method": candidate["basis"], "confidence": candidate["confidence"],
                "low_evidence": candidate["basis"] in NAME_BASES, "subject_key": subject,
                "ownership_key": other, "ownership_entity": other_entity, "decision_id": candidate["decision_id"],
                "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
                "reviewed_at_ms": last.get("at_ms") if candidate["state"] != "proposed" else None,
                "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
                "notice": "a reviewable identity decision; enforcement records are never merged or rewritten"}

    def candidates(self, namespace: str, *, scopes: Iterable[str], subject_key: str | None = None
                   ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_identity_candidates"):
            authorize(namespace, scopes, READ_SCOPE)
            return []
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=subject_key)
                if c["left_key"].startswith("enforcement:") or c["right_key"].startswith("enforcement:")]
        return [self.view(c) for c in rows]

    def _own(self, namespace, candidate_id, scopes):
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise EnforcementError("not_found", "no enforcement identity candidate with that id")

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        self._own(namespace, candidate_id, scopes)
        try:
            return self.view(self.service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                                                 scopes=scopes))
        except OwnershipError as exc:
            raise EnforcementError(exc.code, str(exc)) from exc

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        self._own(namespace, candidate_id, scopes)
        try:
            return self.view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                                 scopes=scopes))
        except OwnershipError as exc:
            raise EnforcementError(exc.code, str(exc)) from exc

    # ------------------------------------------------------------ reporting

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Organisation respondents with no accepted match: kept as published, with their pending candidate count."""
        views = self.candidates(namespace, scopes=scopes)
        out = []
        for subject in self.subjects(namespace, scopes=scopes):
            mine = [v for v in views if v["subject_key"] == subject["key"]]
            if any(v["state"] == "accepted" for v in mine):
                continue
            out.append({"key": subject["key"], "action_key": subject["action_key"],
                        "authority": subject["authority"], "name_as_published": subject["name_as_published"],
                        "identifiers": subject["identifiers"],
                        "pending_candidates": sum(v["state"] == "proposed" for v in mine), "status": "unmatched"})
        return out

    def accepted_links(self, namespace: str, ownership_keys: Iterable[str], *, scopes: Iterable[str]
                       ) -> list[dict[str, Any]]:
        """Accepted, unreverted matches whose ownership side is one of the given record keys or entity ids."""
        wanted = set(ownership_keys)
        return [{"candidate_id": v["candidate_id"], "subject_key": v["subject_key"],
                 "ownership_key": v["ownership_key"], "method": v["method"], "low_evidence": v["low_evidence"],
                 "reviewer": v["reviewer"], "reviewed_at_ms": v["reviewed_at_ms"], "decision_id": v["decision_id"]}
                for v in self.candidates(namespace, scopes=scopes)
                if v["state"] == "accepted" and ({v["ownership_key"], v["ownership_entity"]} & wanted)]

    def all_accepted(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        return [v for v in self.candidates(namespace, scopes=scopes) if v["state"] == "accepted"]
