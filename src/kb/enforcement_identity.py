"""Respondents matched to ownership entities through reviewable identity; authorities as source identities (EN07).

Subjects are the **organisational** respondents of the current action
revisions (name and role as published). Natural persons are never subjects:
they are not stored by name (EN01), so there is nothing to offer. Each subject
is *proposed* against the legal entities of an ownership namespace - which
include the GLEIF-projected LEI records and the SEC EDGAR and Companies House
registrations, each registered as a ``canonical_entities`` row - into the
shared reviewable state machine
(:class:`src.kb.ownership_identity.OwnershipIdentityService`, whose accepted
and reverted decisions are :class:`src.kb.entity_history.EntityHistoryStore`
decisions with reviewer and time). Nothing is accepted automatically and
nothing is merged:

* ``exact-identifier`` first - a published identifier the respondent carries
  (SEC CIK, FCA FRN, LEI, company number) equal to one an ownership record
  carries, under ``SCHEME_ALIASES``;
* ``name-jurisdiction`` - equal normalized names whose countries do not
  contradict; **low evidence**, never auto-accepted.

A respondent without an accepted match stays as published and is reported as
**unmatched**; candidates spanning several ownership clusters are
**conflicts**. Candidate keys start with ``enforcement:`` (in
``FOREIGN_KEY_PREFIXES``), so these links never regroup ownership entities.

Authorities are **source identities**: each ``authority`` record is keyed by
the regulator's code and cited by every action; they are listed, never
matched against companies.
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
SCHEME_ALIASES = {"gb-coh": {"gb-coh", "ra:RA000585"}, "lei": {"lei"}, "sec-cik": {"sec-cik"},
                  "gb-fca-frn": {"gb-fca-frn"}, "nl-kvk": {"nl-kvk", "ra:RA000463"}}


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
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)
        self.store = EnforcementStore(conn, initialize=initialize, now=self.now)

    # -------------------------------------------------------------- subjects

    def authorities(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """The regulators as source identities (never matched against companies)."""
        authorize(namespace, scopes, READ_SCOPE)
        return [{"key": v["record"]["record_key"], "authority": v["record"]["authority"],
                 "name_as_published": v["record"]["name_as_published"],
                 "jurisdiction": v["record"].get("jurisdiction"), "revision_id": v["revision_id"],
                 "identity": "source identity: the publishing regulator"}
                for v in self.store.views(namespace, ("authority",))]

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Organisational respondents of the current revisions, as published."""
        authorize(namespace, scopes, READ_SCOPE)
        out = []
        for view in self.store.views(namespace, ("respondent",)):
            body = view["record"]
            out.append({"key": body["record_key"], "action_key": body["action_key"], "authority": body["authority"],
                        "name_as_published": body["name_as_published"], "role_as_published": body["role_as_published"],
                        "country": body.get("country"), "identifiers": body.get("identifiers") or [],
                        "revision_id": view["revision_id"]})
        return sorted(out, key=lambda s: s["key"])

    # ------------------------------------------------------------- proposals

    def propose(self, namespace: str, *, ownership_namespace: str, principal_id: str, scopes: Iterable[str]
                ) -> dict[str, Any]:
        """Offer identifier candidates first and name candidates as low evidence; idempotent, never accepts."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        entities = self.service._entities(ownership_namespace, principal_id, scopes) \
            if table_exists(self.conn, "ownership_records") else []
        offered = []
        for subject in self.subjects(namespace, scopes=scopes):
            left = {"record_key": subject["key"], "name_as_published": subject["name_as_published"],
                    "role_as_published": subject["role_as_published"], "revision_id": subject["revision_id"],
                    "ownership_namespace": ownership_namespace}
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
                                                                    "right": right, "confidence_basis":
                                                                    "published identifier"}, principal_id, scopes))
                    continue
                if _name(body.get("name")) != _name(subject["name_as_published"]):
                    continue
                theirs = str(body.get("jurisdiction") or "").split("-")[0].upper() or None
                ours = subject.get("country")
                if ours and theirs and ours != theirs:
                    continue  # a contradicting country is not a candidate
                offered.append(self._offer(namespace, subject, body["record_key"], right_entity, "name-jurisdiction", {
                    "normalized_name": _name(body.get("name")), "country": ours or theirs,
                    "country_basis": ("stated on both" if ours and theirs else
                                      "not published for the respondent" if not ours else
                                      "not stated by the register"),
                    "left": left, "right": right, "low_evidence": True,
                    "note": "a name is low evidence and never accepted automatically; a reviewer decides"},
                    principal_id, scopes))
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes),
                "unmatched": self.unmatched(namespace, scopes=scopes),
                "authorities": self.authorities(namespace, scopes=scopes)}

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
                "low_evidence": candidate["basis"] in NAME_BASES, "evidence": candidate["evidence"],
                "subject_key": subject, "ownership_key": other, "ownership_entity": other_entity,
                "decision_id": candidate["decision_id"],
                "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
                "reviewed_at_ms": last.get("at_ms") if candidate["state"] != "proposed" else None,
                "reason": last.get("reason"), "history": candidate["history"],
                "notice": "a reviewable identity decision; enforcement records are never merged or rewritten"}

    def candidates(self, namespace: str, *, scopes: Iterable[str], subject_key: str | None = None
                   ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_identity_candidates"):
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
        """Respondents with no accepted match: kept as published, with their pending candidate count."""
        views = self.candidates(namespace, scopes=scopes)
        out = []
        for subject in self.subjects(namespace, scopes=scopes):
            mine = [v for v in views if v["subject_key"] == subject["key"]]
            if any(v["state"] == "accepted" for v in mine):
                continue
            out.append({"key": subject["key"], "action_key": subject["action_key"],
                        "authority": subject["authority"], "name_as_published": subject["name_as_published"],
                        "role_as_published": subject["role_as_published"],
                        "pending_candidates": sum(v["state"] == "proposed" for v in mine), "status": "unmatched"})
        return out

    def conflicts(self, namespace: str, *, ownership_namespace: str, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Respondents whose open or accepted candidates point at more than one ownership cluster."""
        clusters = self.service.clusters(ownership_namespace)
        grouped: dict[str, dict[str, list[str]]] = {}
        for view in self.candidates(namespace, scopes=scopes):
            if view["state"] not in {"proposed", "accepted"}:
                continue
            cluster = clusters.get(view["ownership_key"], view["ownership_key"])
            grouped.setdefault(view["subject_key"], {}).setdefault(cluster, []).append(view["candidate_id"])
        return [{"subject_key": key, "clusters": [{"cluster": c, "candidates": ids} for c, ids in sorted(groups.items())],
                 "status": "conflict", "note": "one respondent, several ownership entities: a reviewer decides"}
                for key, groups in sorted(grouped.items()) if len(groups) > 1]

    def accepted_links(self, namespace: str, ownership_keys: Iterable[str], *, scopes: Iterable[str]
                       ) -> list[dict[str, Any]]:
        """Accepted, unreverted matches whose ownership side is one of the given record keys or entity ids."""
        wanted = set(ownership_keys)
        return [{"candidate_id": v["candidate_id"], "subject_key": v["subject_key"],
                 "ownership_key": v["ownership_key"], "method": v["method"], "low_evidence": v["low_evidence"],
                 "reviewer": v["reviewer"], "reviewed_at_ms": v["reviewed_at_ms"], "decision_id": v["decision_id"]}
                for v in self.candidates(namespace, scopes=scopes)
                if v["state"] == "accepted" and ({v["ownership_key"], v["ownership_entity"]} & wanted)]
