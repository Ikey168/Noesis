"""Organisations and CORDIS participants matched through reviewable identity (#2579, RE07).

Subjects are the ROR organisations of the current records and the CORDIS participants (by PIC) of the current
project revisions. Each is *proposed* into the shared reviewable state machine
(:class:`src.kb.ownership_identity.OwnershipIdentityService`, whose accepted and reverted decisions are
:class:`src.kb.entity_history.EntityHistoryStore` decisions with reviewer and time recorded); nothing is accepted or
merged automatically, and no entity store is added. Published identifiers are used before names:

* **ROR organisation -> Corporate Ownership legal entity**: ``exact-identifier`` when an identifier ROR publishes
  (ROR id, ISNI, Wikidata, GRID, FundRef) equals one the ownership record carries; otherwise ``name-jurisdiction``
  (equal normalised name, countries not contradicting) - **low evidence**, never auto-accepted;
* **CORDIS participant -> Corporate Ownership legal entity**: ``exact-identifier`` when the VAT number CORDIS
  publishes equals the entity's VAT identifier; otherwise ``name-jurisdiction`` (low evidence);
* **CORDIS participant -> ROR organisation**: ``unqualified-identifier`` (method ``website-domain``) when the
  participant's published organisation URL and a ROR website share their host; otherwise ``name-jurisdiction``.

**Researchers are never subjects** (RE01): they are not proposed, merged or disambiguated by name; a researcher
reaches papers and organisations only through what their public ORCID record asserts (RE08 links). A subject without
an accepted match stays visible as **unmatched**; a subject whose open or accepted candidates point at more than one
target is reported as a **conflict**. Candidate keys start with ``research-entities:`` (in ``FOREIGN_KEY_PREFIXES``), so
these links never regroup ownership entities.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from src.kb.research_entities_records import (
    READ_SCOPE,
    ResearchEntityError,
    ResearchEntityStore,
    authorize,
    table_exists,
)

NAME_BASES = frozenset({"name-jurisdiction", "similar-name"})
ROR_SCHEMES = {"isni", "wikidata", "grid", "fundref", "ror"}
VAT_SCHEMES = {"vat", "eu-vat"}
PREFIX = "research-entities:"


def _value(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum()).lstrip("0") or "0"


def _name(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def host(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parts = urlsplit(raw if "://" in raw else "https://" + raw)
    name = (parts.hostname or "").casefold()
    return name.removeprefix("www.") or None


def entity_for(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


class ResearchEntityIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)
        self.store = ResearchEntityStore(conn, initialize=initialize, now=self.now)

    # -------------------------------------------------------------- subjects

    def organisations(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        out = []
        for view in self.store.records(namespace, scopes=scopes, kinds=["organisation"]):
            fields = view["record"]["fields"]
            identifiers = [{"scheme": "ror", "value": fields["ror_id"].rsplit("/", 1)[-1]}]
            identifiers += [{"scheme": str(e["type"]).lower(), "value": v} for e in fields["external_ids"]
                            for v in e["all"] if str(e["type"]).lower() in ROR_SCHEMES]
            countries = sorted({loc["country_code"] for loc in fields["locations"] if loc.get("country_code")})
            out.append({"key": view["record_key"], "subject": "organisation", "ror_id": fields["ror_id"],
                        "name_as_published": fields["display_name"],
                        "names": sorted({n["value"] for n in fields["names"] if n.get("value")}),
                        "country": countries[0] if len(countries) == 1 else None, "status": fields["status"],
                        "identifiers": identifiers,
                        "hosts": sorted({h for h in (host(link.get("value")) for link in fields["links"]
                                                     if link.get("type") == "website") if h}),
                        "revision_id": view["revision_id"]})
        return out

    def participants(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """CORDIS participants of the current project revisions, one per PIC, as published (latest row wins for the
        name; every project and revision is listed)."""
        grouped: dict[str, dict[str, Any]] = {}
        for view in self.store.records(namespace, scopes=scopes, kinds=["project"]):
            for participant in view["record"]["fields"]["participants"]:
                pic = participant.get("pic")
                if not pic:
                    continue
                entry = grouped.setdefault(pic, {
                    "key": f"{PREFIX}cordis-participant:{pic}", "subject": "cordis_participant", "pic": pic,
                    "name_as_published": participant.get("name"), "country": participant.get("country"),
                    "vat_number": participant.get("vat_number"), "hosts": set(), "projects": [],
                    "revision_ids": []})
                entry["projects"].append(view["record_key"])
                entry["revision_ids"].append(view["revision_id"])
                if host(participant.get("organization_url")):
                    entry["hosts"].add(host(participant.get("organization_url")))
        out = []
        for entry in grouped.values():
            entry["hosts"] = sorted(entry["hosts"])
            entry["projects"] = sorted(set(entry["projects"]))
            entry["revision_ids"] = sorted(set(entry["revision_ids"]))
            entry["revision_id"] = entry["revision_ids"][0]
            out.append(entry)
        return sorted(out, key=lambda e: e["key"])

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        return self.organisations(namespace, scopes=scopes) + self.participants(namespace, scopes=scopes)

    # ------------------------------------------------------------- proposals

    def _offer(self, namespace, subject, right_key, right_entity, basis, evidence, principal_id, scopes):
        return self.service.offer(namespace, left_key=subject["key"], right_key=right_key,
                                  left_entity=entity_for(subject["key"]), right_entity=right_entity, basis=basis,
                                  evidence=[evidence], principal_id=principal_id, scopes=scopes)

    @staticmethod
    def _countries_agree(ours: Any, theirs: Any) -> bool:
        ours = str(ours or "").split("-")[0].upper()
        theirs = str(theirs or "").split("-")[0].upper()
        return not (ours and theirs and ours != theirs)

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                ownership_namespace: str | None = None) -> dict[str, Any]:
        """Offer identifier candidates first and name candidates as low evidence; idempotent, never accepts."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        organisations = self.organisations(namespace, scopes=scopes)
        participants = self.participants(namespace, scopes=scopes)
        offered = []
        # CORDIS participant -> ROR organisation
        for participant in participants:
            for organisation in organisations:
                shared_hosts = sorted(set(participant["hosts"]) & set(organisation["hosts"]))
                left = {"record_key": participant["key"], "pic": participant["pic"],
                        "name_as_published": participant["name_as_published"],
                        "project_revisions": participant["revision_ids"]}
                right = {"record_key": organisation["key"], "ror_id": organisation["ror_id"],
                         "revision_id": organisation["revision_id"], "status": organisation["status"]}
                if shared_hosts:
                    offered.append(self._offer(namespace, participant, organisation["key"],
                                               entity_for(organisation["key"]), "unqualified-identifier",
                                               {"method": "website-domain", "hosts": shared_hosts, "left": left,
                                                "right": right,
                                                "note": "the participant's published organisation URL and the ROR "
                                                        "website share their host"}, principal_id, scopes))
                elif _name(participant["name_as_published"]) in {_name(n) for n in organisation["names"]} and \
                        self._countries_agree(participant["country"], organisation["country"]):
                    offered.append(self._offer(namespace, participant, organisation["key"],
                                               entity_for(organisation["key"]), "name-jurisdiction",
                                               {"method": "name-country", "left": left, "right": right,
                                                "low_evidence": True,
                                                "note": "a name is low evidence and never accepted automatically"},
                                               principal_id, scopes))
        ownership = {"namespace": ownership_namespace, "status": "not_requested"}
        if ownership_namespace:
            authorize(ownership_namespace, scopes, "knowledge:ownership:read")
            entities = (self.service._entities(ownership_namespace, principal_id, scopes)
                        if table_exists(self.conn, "ownership_records") else [])
            if not entities:
                ownership["status"] = "provider_absent"
                ownership["note"] = "no Corporate Ownership legal entities in that namespace; nothing was proposed"
            else:
                ownership["status"] = "proposed"
                offered += self._ownership(namespace, organisations, participants, entities, ownership_namespace,
                                           principal_id, scopes)
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes),
                "unmatched": self.unmatched(namespace, scopes=scopes), "ownership": ownership,
                "researchers": "never proposed: researchers are not matched, merged or disambiguated by name (RE01)"}

    def _ownership(self, namespace, organisations, participants, entities, ownership_namespace, principal_id,
                   scopes) -> list[dict[str, Any]]:
        offered = []
        for subject in organisations + participants:
            if subject["subject"] == "organisation":
                published = {(i["scheme"], _value(i["value"])) for i in subject["identifiers"]}
                schemes = ROR_SCHEMES
            else:
                published = {(s, _value(subject["vat_number"])) for s in VAT_SCHEMES} if subject["vat_number"] \
                    else set()
                schemes = VAT_SCHEMES
            left = {"record_key": subject["key"], "name_as_published": subject["name_as_published"],
                    "revision_id": subject["revision_id"], "ownership_namespace": ownership_namespace}
            for entity in entities:
                body = entity["record"]
                right = {"record_key": body["record_key"], "provider": body["source"]["provider"],
                         "revision": entity["revision"]}
                right_entity = body.get("canonical_entity_id") or entity_for(body["record_key"])
                shared = [i for i in body.get("identifiers") or [] if str(i["scheme"]).lower() in schemes
                          and (str(i["scheme"]).lower(), _value(i["value"])) in published]
                if shared:
                    offered.append(self._offer(namespace, subject, body["record_key"], right_entity,
                                               "exact-identifier", {"method": "published-identifier",
                                                                    "identifiers": shared, "left": left,
                                                                    "right": right}, principal_id, scopes))
                    continue
                names = {_name(n) for n in subject.get("names") or [subject["name_as_published"]]}
                if _name(body.get("name")) not in names:
                    continue
                if not self._countries_agree(subject.get("country"), body.get("jurisdiction")):
                    continue  # a contradicting country is not a candidate
                offered.append(self._offer(namespace, subject, body["record_key"], right_entity, "name-jurisdiction",
                                           {"method": "name-country", "normalized_name": _name(body.get("name")),
                                            "left": left, "right": right, "low_evidence": True,
                                            "note": "a name is low evidence and never accepted automatically"},
                                           principal_id, scopes))
        return offered

    # --------------------------------------------------------------- reviews

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        left, right = candidate["left_key"], candidate["right_key"]
        # The subject is the CORDIS participant when both sides are research entities, else our side.
        if left.startswith(PREFIX) and right.startswith(PREFIX):
            subject, other = (left, right) if ":cordis-participant:" in left else (right, left)
        else:
            subject, other = (left, right) if left.startswith(PREFIX) else (right, left)
        target_kind = "ror_organisation" if other.startswith(f"{PREFIX}ror:") else "ownership_entity"
        other_entity = candidate["right_entity"] if other == right else candidate["left_entity"]
        return {"candidate_id": candidate["candidate_id"], "state": candidate["state"],
                "method": (candidate["evidence"][0] or {}).get("method") if candidate["evidence"] else None,
                "basis": candidate["basis"], "confidence": candidate["confidence"],
                "low_evidence": candidate["basis"] in NAME_BASES, "subject_key": subject, "target_key": other,
                "target_kind": target_kind, "target_entity": other_entity, "decision_id": candidate["decision_id"],
                "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
                "reviewed_at_ms": last.get("at_ms") if candidate["state"] != "proposed" else None,
                "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
                "notice": "a reviewable identity decision; registry records are never merged or rewritten"}

    def candidates(self, namespace: str, *, scopes: Iterable[str], subject_key: str | None = None
                   ) -> list[dict[str, Any]]:
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=subject_key)
                if c["left_key"].startswith(PREFIX) or c["right_key"].startswith(PREFIX)]
        return [self.view(c) for c in rows]

    def _own(self, namespace, candidate_id, scopes):
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise ResearchEntityError("not_found", "no research-entities identity candidate with that id")

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        self._own(namespace, candidate_id, scopes)
        try:
            return self.view(self.service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                                                 scopes=scopes))
        except OwnershipError as exc:
            raise ResearchEntityError(exc.code, str(exc)) from exc

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_store import OwnershipError

        self._own(namespace, candidate_id, scopes)
        try:
            return self.view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                                 scopes=scopes))
        except OwnershipError as exc:
            raise ResearchEntityError(exc.code, str(exc)) from exc

    # ------------------------------------------------------------ reporting

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Subjects with no accepted match: kept as published, with their pending candidate count."""
        views = self.candidates(namespace, scopes=scopes)
        out = []
        for subject in self.subjects(namespace, scopes=scopes):
            mine = [v for v in views if subject["key"] in {v["subject_key"], v["target_key"]}]
            if any(v["state"] == "accepted" for v in mine):
                continue
            out.append({"key": subject["key"], "subject": subject["subject"],
                        "name_as_published": subject["name_as_published"],
                        "pending_candidates": sum(v["state"] == "proposed" for v in mine), "status": "unmatched"})
        return out

    def conflicts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Subjects whose open or accepted candidates point at more than one target of the same kind."""
        grouped: dict[tuple[str, str], dict[str, list[str]]] = {}
        for view in self.candidates(namespace, scopes=scopes):
            if view["state"] not in {"proposed", "accepted"}:
                continue
            grouped.setdefault((view["subject_key"], view["target_kind"]), {}).setdefault(
                view["target_key"], []).append(view["candidate_id"])
        return [{"subject_key": key, "target_kind": kind,
                 "targets": [{"target": t, "candidates": ids} for t, ids in sorted(groups.items())],
                 "status": "conflict", "note": "one subject, several targets: a reviewer decides; nothing is picked"}
                for (key, kind), groups in sorted(grouped.items()) if len(groups) > 1]

    def accepted(self, namespace: str, *, scopes: Iterable[str], key: str | None = None) -> list[dict[str, Any]]:
        """Accepted, unreverted matches touching ``key`` (either side), or all of them."""
        return [v for v in self.candidates(namespace, scopes=scopes)
                if v["state"] == "accepted" and (key is None or key in {v["subject_key"], v["target_key"]})]
