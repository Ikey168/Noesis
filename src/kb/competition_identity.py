"""Case parties and aid beneficiaries matched to ownership entities through reviewable identity (#2217, CS07).

Subjects are the parties of the current case revisions (name and role as
published) and the beneficiaries of state-aid awards. Each is *proposed*
against the legal entities of an ownership namespace into the shared
reviewable state machine (:class:`src.kb.ownership_identity.OwnershipIdentityService`,
whose accepted and reverted decisions are :class:`src.kb.entity_history.EntityHistoryStore`
decisions, with reviewer and time recorded); nothing is accepted
automatically and no entity store is added:

* ``exact-identifier`` first - a published identifier (a TAM national id such
  as a KvK number, an LEI or a company number a party carries) equal to one an
  ownership record carries, under the declared scheme equivalences
  (``SCHEME_ALIASES``: a KvK number is also the GLEIF ``RA000463``
  registration-authority number);
* ``name-jurisdiction`` - equal normalized names whose countries do not
  contradict; **low evidence**, marked as such, never auto-accepted, and the
  evidence states whether the party's country was published.

A subject without an accepted match stays as published and is reported as
**unmatched**; a subject whose candidates point at entities in more than one
ownership cluster (the accepted ownership identity decisions of that
namespace) is reported as a **conflict**. Candidate keys start with
``competition:`` (in ``FOREIGN_KEY_PREFIXES``), so these links never regroup
ownership entities.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.competition import READ_SCOPE, CompetitionError, CompetitionStore, authorize, table_exists

NAME_BASES = frozenset({"name-jurisdiction", "similar-name"})
SCHEME_ALIASES = {"nl-kvk": {"nl-kvk", "ra:RA000463"}, "gb-coh": {"gb-coh", "ra:RA000585"}, "lei": {"lei"},
                  "sec-cik": {"sec-cik"}}


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


class CompetitionIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)
        self.store = CompetitionStore(conn, initialize=initialize, now=self.now)

    # -------------------------------------------------------------- subjects

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Case parties and award beneficiaries of the current revisions, as published."""
        authorize(namespace, scopes, READ_SCOPE)
        out = []
        for view in self.store.views(namespace, ("case_party", "state_aid_award")):
            body = view["record"]
            if body["kind"] == "case_party":
                out.append({"key": body["record_key"], "subject": "case_party", "case_key": body["case_key"],
                            "authority": body["authority"], "name_as_published": body["name_as_published"],
                            "role_as_published": body["role_as_published"], "country": body.get("country"),
                            "identifiers": body.get("identifiers") or [], "revision_id": view["revision_id"]})
            else:
                out.append({"key": body["record_key"], "subject": "award_beneficiary", "award_key": body["record_key"],
                            "sa_number": body.get("sa_number"), "authority": "ec",
                            "name_as_published": body["beneficiary_name_as_published"],
                            "role_as_published": "beneficiary", "country": body["member_state"],
                            "country_basis": "granting member state",
                            "identifiers": body.get("beneficiary_identifiers") or [],
                            "revision_id": view["revision_id"]})
        return sorted(out, key=lambda s: s["key"])

    # ------------------------------------------------------------- proposals

    def propose(self, namespace: str, *, ownership_namespace: str, principal_id: str, scopes: Iterable[str]
                ) -> dict[str, Any]:
        """Offer identifier candidates first and name candidates as low evidence; idempotent, never accepts."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        authorize(ownership_namespace, scopes, READ_SCOPE)
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
                                                                    "right": right}, principal_id, scopes))
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
                                      "not published for the party" if not ours else "not stated by the register"),
                    "left": left, "right": right, "low_evidence": True,
                    "note": "a name is low evidence and never accepted automatically; a reviewer decides"},
                    principal_id, scopes))
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
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
        subject, other = (left, right) if left.startswith("competition:") else (right, left)
        other_entity = candidate["right_entity"] if other == right else candidate["left_entity"]
        return {"candidate_id": candidate["candidate_id"], "state": candidate["state"],
                "method": candidate["basis"], "confidence": candidate["confidence"],
                "low_evidence": candidate["basis"] in NAME_BASES, "subject_key": subject,
                "ownership_key": other, "ownership_entity": other_entity, "decision_id": candidate["decision_id"],
                "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
                "reviewed_at_ms": last.get("at_ms") if candidate["state"] != "proposed" else None,
                "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
                "notice": "a reviewable identity decision; case and award records are never merged or rewritten"}

    def candidates(self, namespace: str, *, scopes: Iterable[str], subject_key: str | None = None
                   ) -> list[dict[str, Any]]:
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=subject_key)
                if c["left_key"].startswith("competition:") or c["right_key"].startswith("competition:")]
        return [self.view(c) for c in rows]

    def _own(self, namespace, candidate_id, scopes):
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise CompetitionError("not_found", "no competition identity candidate with that id")

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        self._own(namespace, candidate_id, scopes)
        try:
            return self.view(self.service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                                                 scopes=scopes))
        except OwnershipError as exc:
            raise CompetitionError(exc.code, str(exc)) from exc

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        self._own(namespace, candidate_id, scopes)
        try:
            return self.view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                                 scopes=scopes))
        except OwnershipError as exc:
            raise CompetitionError(exc.code, str(exc)) from exc

    # ------------------------------------------------------------ reporting

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Subjects with no accepted match: kept as published, with their pending candidate count."""
        views = self.candidates(namespace, scopes=scopes)
        out = []
        for subject in self.subjects(namespace, scopes=scopes):
            mine = [v for v in views if v["subject_key"] == subject["key"]]
            if any(v["state"] == "accepted" for v in mine):
                continue
            out.append({"key": subject["key"], "subject": subject["subject"],
                        "name_as_published": subject["name_as_published"],
                        "role_as_published": subject["role_as_published"],
                        "case_key": subject.get("case_key"), "award_key": subject.get("award_key"),
                        "pending_candidates": sum(v["state"] == "proposed" for v in mine),
                        "status": "unmatched"})
        return out

    def conflicts(self, namespace: str, *, ownership_namespace: str, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Subjects whose open or accepted candidates point at more than one ownership cluster."""
        clusters = self.service.clusters(ownership_namespace)
        grouped: dict[str, dict[str, list[str]]] = {}
        for view in self.candidates(namespace, scopes=scopes):
            if view["state"] not in {"proposed", "accepted"}:
                continue
            cluster = clusters.get(view["ownership_key"], view["ownership_key"])
            grouped.setdefault(view["subject_key"], {}).setdefault(cluster, []).append(view["candidate_id"])
        return [{"subject_key": key, "clusters": [{"cluster": c, "candidates": ids} for c, ids in sorted(groups.items())],
                 "status": "conflict",
                 "note": "one party, several ownership entities: a reviewer decides; nothing is picked"}
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
