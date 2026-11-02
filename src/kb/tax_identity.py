"""Public CbCR references matched to ownership entities through reviewable identity (#2889).

A public CbCR reference (a link-only record of a company's income tax information report under Directive (EU)
2021/2101) reaches a legal entity of an ownership namespace **only** through a reviewed identity decision; nothing is
merged and nothing is accepted automatically. Candidates go through the shared reviewable state machine
(:class:`src.kb.ownership_identity.OwnershipIdentityService`, whose accepted and reverted decisions are
:class:`src.kb.entity_history.EntityHistoryStore` decisions):

* ``exact-identifier`` when the report publishes an identifier (an LEI, a register number) an ownership record
  carries, with the declared scheme equivalences (a KvK number is the GLEIF ``RA000463`` registration number and a
  Companies House number the ``RA000585`` one);
* otherwise ``name-jurisdiction`` on equal normalized names - **low evidence**, never accepted automatically.

Reference keys start with ``tax-cbcr:`` (in the ownership ``FOREIGN_KEY_PREFIXES``), so these links never regroup
ownership entities. Without ownership records every reference is reported unmatched with ``ownership_absent``, and a
reference without an accepted candidate stays visible as ``unmatched``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.tax_records import (
    MATCH_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    TaxError,
    authorize,
    table_exists,
)
from src.kb.tax_store import REFERENCE_PREFIX, TaxStore

NAME_BASES = frozenset({"name-jurisdiction", "similar-name"})
SCHEME_ALIASES = {"nl-kvk": {"nl-kvk", "ra:RA000463"}, "gb-coh": {"gb-coh", "ra:RA000585"}, "lei": {"lei"}}
NOTICE = "a reviewable identity decision; public CbCR references and ownership records are never merged"


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


class TaxIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = TaxStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        return self.store.company_reports(namespace)

    def propose_company_matches(self, namespace: str, *, ownership_namespace: str, principal_id: str,
                                scopes: Iterable[str]) -> dict[str, Any]:
        """Identifier candidates first, name candidates as low evidence; idempotent, never accepts."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        entities = []
        if table_exists(self.conn, "ownership_records"):
            authorize(ownership_namespace, scopes, "knowledge:ownership:read")
            entities = self.service._entities(ownership_namespace, principal_id, scopes)
        if not entities:
            return {"status": "ownership_absent", "proposed": [], "candidates": [],
                    "unmatched": self.unmatched(namespace, scopes=scopes),
                    "note": "no ownership records are held; every report reference stays unmatched"}
        offered = []
        for subject in self.subjects(namespace, scopes=scopes):
            left = {"reference_key": subject["reference_key"], "revision_id": subject["revision_id"],
                    "company_name_as_published": subject["company_name_as_published"],
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
                if _name(body.get("name")) != _name(subject["company_name_as_published"]):
                    continue
                offered.append(self._offer(namespace, subject, body["record_key"], right_entity, "name-jurisdiction", {
                    "normalized_name": _name(body.get("name")), "left": left, "right": right, "low_evidence": True,
                    "note": "a name is low evidence and never accepted automatically; a reviewer decides"},
                    principal_id, scopes))
        return {"status": "proposed",
                "proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes),
                "unmatched": self.unmatched(namespace, scopes=scopes)}

    def _offer(self, namespace, subject, right_key, right_entity, basis, evidence, principal_id, scopes):
        return self.service.offer(namespace, left_key=subject["reference_key"], right_key=right_key,
                                  left_entity=entity_for(subject["reference_key"]), right_entity=right_entity,
                                  basis=basis, evidence=[{**evidence, "method": basis}], principal_id=principal_id,
                                  scopes=scopes)

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        left, right = candidate["left_key"], candidate["right_key"]
        subject, other = (left, right) if left.startswith(REFERENCE_PREFIX) else (right, left)
        other_entity = candidate["right_entity"] if other == right else candidate["left_entity"]
        reviewed = candidate["state"] != "proposed"
        return {"contract": MATCH_CONTRACT, "kind": "company_report", "candidate_id": candidate["candidate_id"],
                "state": candidate["state"], "method": candidate["basis"], "confidence": candidate["confidence"],
                "low_evidence": candidate["basis"] in NAME_BASES, "reference_key": subject, "ownership_key": other,
                "ownership_entity": other_entity, "decision_id": candidate["decision_id"],
                "reviewer": last.get("by") if reviewed else None,
                "reviewed_at_ms": last.get("at_ms") if reviewed else None, "reason": last.get("reason"),
                "evidence": candidate["evidence"], "history": candidate["history"], "notice": NOTICE}

    def candidates(self, namespace: str, *, scopes: Iterable[str], reference_key: str | None = None
                   ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return []
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=reference_key)
                if c["left_key"].startswith(REFERENCE_PREFIX) or c["right_key"].startswith(REFERENCE_PREFIX)]
        return [self.view(c) for c in rows]

    def _own(self, namespace, candidate_id, scopes):
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise TaxError("not_found", "no public CbCR company candidate with that id")

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        self._own(namespace, candidate_id, scopes)
        try:
            return self.view(self.service.review(namespace, candidate_id, decision, reason,
                                                 principal_id=principal_id, scopes=scopes))
        except OwnershipError as exc:
            raise TaxError(exc.code, str(exc)) from exc

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        self._own(namespace, candidate_id, scopes)
        try:
            return self.view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                                 scopes=scopes))
        except OwnershipError as exc:
            raise TaxError(exc.code, str(exc)) from exc

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        views = self.candidates(namespace, scopes=scopes)
        out = []
        for subject in self.subjects(namespace, scopes=scopes):
            mine = [v for v in views if v["reference_key"] == subject["reference_key"]]
            if any(v["state"] == "accepted" for v in mine):
                continue
            out.append({"reference_key": subject["reference_key"],
                        "company_name_as_published": subject["company_name_as_published"],
                        "pending_candidates": sum(v["state"] == "proposed" for v in mine), "status": "unmatched"})
        return out

    def accepted_links(self, namespace: str, ownership_keys: Iterable[str], *, scopes: Iterable[str]
                       ) -> list[dict[str, Any]]:
        """Accepted, unreverted matches whose ownership side is one of the given record keys or entities."""
        wanted = set(ownership_keys)
        return [{"candidate_id": v["candidate_id"], "reference_key": v["reference_key"],
                 "ownership_key": v["ownership_key"], "ownership_entity": v["ownership_entity"],
                 "method": v["method"], "low_evidence": v["low_evidence"], "confidence": v["confidence"],
                 "reviewer": v["reviewer"], "reviewed_at_ms": v["reviewed_at_ms"], "decision_id": v["decision_id"]}
                for v in self.candidates(namespace, scopes=scopes)
                if v["state"] == "accepted" and ({v["ownership_key"], v["ownership_entity"]} & wanted)]


__all__ = ["NOTICE", "TaxIdentity", "entity_for"]
