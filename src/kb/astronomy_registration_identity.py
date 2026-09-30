"""Registrations, re-entries, operators and registering States through reviewable identity (#2224, SO07, SO08).

**Objects (SO07).** Registration, DISCOS and re-entry records name a space
object by the COSPAR and NORAD identifiers they state. They are linked to the
``orbital_object`` records of GCAT and CelesTrak SATCAT
(:class:`src.kb.astronomy_store.AstronomyStore`, the Astronomy pack's object
identities from #2156) as follows, and no record is ever merged or edited:

* every identifier both sides state agrees -> an *exact link*
  (``astronomy_registration_object_links``) with the matched identifiers and
  both revisions as evidence;
* one identifier agrees and another differs -> a reviewable
  ``registration-identifier-conflict`` candidate in the Astronomy identity
  state machine (:class:`src.kb.astronomy_identity.AstronomyIdentity`, whose
  decisions are :class:`src.kb.entity_history.EntityHistoryStore` decisions);
  both values stay visible;
* a registration naming an object without any designator -> a
  ``registration-name-only`` candidate against catalogue objects of that
  name, never auto-linked; with no such object it stays unmatched.

**Parties (SO08).** Registering States, intergovernmental registrants and
operators/owners are matched to ``canonical_entities`` (and Corporate
Ownership legal entities where the operator publishes an LEI) through the
shared reviewable state machine :class:`src.kb.ownership_identity.OwnershipIdentityService`,
with ``space-registration:`` record keys (one of its
``FOREIGN_KEY_PREFIXES``, so these links never regroup ownership entities):

* ``exact-identifier`` - an operator's published LEI equals an ownership
  legal entity's LEI;
* ``name-jurisdiction`` - equal normalised names with a country entity (a
  State registrant) or an organisation entity (an intergovernmental registrant
  or an operator); a reviewer decides;
* ``similar-name`` - names sharing their first two words; shown, never
  acceptable.

Parties come only from what a registration or operator assertion states; no
military, intelligence or other attribution is created.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.astronomy_records import READ_SCOPE, WRITE_SCOPE, AstronomyError, authorize, canonical, digest
from src.kb.astronomy_records import object_name_key
from src.kb.astronomy_registration import RegistrationStore, party_key, table_exists

LINK_CONTRACT = "noesis-astronomy-registration-object-link-v1"
PARTY_PREFIX = "space-registration:"
COUNTRY_TYPES = ("country", "gpe", "nation", "state")
ORGANISATION_TYPES = ("organization", "organisation", "org")
_DDL = """
CREATE TABLE IF NOT EXISTS astronomy_registration_object_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, object_record_id TEXT NOT NULL,
  matched_on_json TEXT NOT NULL, evidence_json TEXT NOT NULL, state TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""
_IDS = ("cospar", "norad")


def registration_key(record: Mapping[str, Any]) -> str:
    return f"astronomy:registration:{record['source']['provider']}:{record['source']['source_record_id']}"


def object_key(record: Mapping[str, Any]) -> str:
    return f"astronomy:object:{record['source']['provider']}:{record['source']['source_record_id']}"


def _similar(a: str, b: str) -> bool:
    ta, tb = a.split(), b.split()
    return a != b and len(ta) >= 2 and len(tb) >= 2 and ta[:2] == tb[:2]


class RegistrationIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.astronomy_identity import AstronomyIdentity
        from src.kb.astronomy_store import AstronomyStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = RegistrationStore(conn, initialize=initialize, now=self.now)
        self.objects = AstronomyStore(conn, initialize=initialize, now=self.now)
        self.astronomy = AstronomyIdentity(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # -------------------------------------------------------------- objects (SO07)

    def _catalogue(self, namespace: str) -> list[dict[str, Any]]:
        if not self.objects.ready():
            return []
        return self.objects.visible(namespace, kinds=["orbital_object"])["records"]

    def match_objects(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Link exact identifier matches with evidence; offer conflicts and name-only entries for review."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        catalogue = self._catalogue(namespace)
        linked, offered, unmatched = [], [], []
        for view in self.store.visible(namespace)["records"]:
            record = view["record"]
            stated = {k: record[k] for k in _IDS if record.get(k)}
            if stated:
                for obj in catalogue:
                    other = obj["record"]
                    both = [k for k in _IDS if k in stated and other.get(k)]
                    agree = [k for k in both if stated[k] == other[k]]
                    differ = [k for k in both if stated[k] != other[k]]
                    if not agree:
                        continue
                    if not differ:
                        linked.append(self._link(namespace, view, obj, agree, principal_id))
                    else:
                        offered.append(
                            self.astronomy.offer(
                                namespace,
                                left_key=registration_key(record),
                                right_key=object_key(other),
                                basis="registration-identifier-conflict",
                                evidence={
                                    "registration": {"provider": record["source"]["provider"],
                                                     "revision_id": view["revision_id"], **stated},
                                    "catalogue": {"provider": other["source"]["provider"],
                                                  "revision_id": obj["revision_id"],
                                                  **{k: other[k] for k in _IDS if other.get(k)}},
                                    "agree": agree,
                                    "differ": differ,
                                    "note": "the sources state these identifiers differently; both stay as stated",
                                },
                                principal_id=principal_id,
                            )
                        )
            elif record.get("object_name") and record["kind"] == "registration_entry":
                name = object_name_key(record["object_name"])
                same = [o for o in catalogue if o["record"].get("name") and object_name_key(o["record"]["name"]) == name]
                for obj in same:
                    offered.append(
                        self.astronomy.offer(
                            namespace,
                            left_key=registration_key(record),
                            right_key=object_key(obj["record"]),
                            basis="registration-name-only",
                            evidence={
                                "registered_name": record["object_name"],
                                "catalogue_name": obj["record"]["name"],
                                "document": record.get("un_document"),
                                "note": "registered by name only; a name is never an automatic link",
                            },
                            principal_id=principal_id,
                        )
                    )
                if not same:
                    unmatched.append({"record_id": view["record_id"], "object_name": record["object_name"],
                                      "state": "unmatched", "reason": "no catalogue object states this name"})
        return {
            "linked": sorted({x for x in linked if x}),
            "proposed": sorted({o["candidate_id"] for o in offered if o["change"]}),
            "candidates": [c for c in self.astronomy.candidates(namespace, scopes=scopes)
                           if c["basis"].startswith("registration-")],
            "unmatched": unmatched,
            "policy": "exact identifiers link with evidence; conflicts and names are reviewable; records never merge",
        }

    def _link(self, namespace, view, obj, agree, principal_id) -> str | None:
        link_id = "astro-reg-link:" + digest([namespace, view["record_id"], obj["record_id"]])[:24]
        if self.conn.execute(
            "SELECT 1 FROM astronomy_registration_object_links WHERE namespace=? AND link_id=?", [namespace, link_id]
        ).fetchone():
            return None
        record, other = view["record"], obj["record"]
        self.conn.execute(
            "INSERT INTO astronomy_registration_object_links VALUES (?,?,?,?,?,?,?,?,?)",
            [
                namespace, link_id, view["record_id"], obj["record_id"],
                canonical({k: record[k] for k in agree}),
                canonical({
                    "registration": {"provider": record["source"]["provider"],
                                     "source_record_id": record["source"]["source_record_id"],
                                     "revision_id": view["revision_id"]},
                    "catalogue": {"provider": other["source"]["provider"],
                                  "source_record_id": other["source"]["source_record_id"],
                                  "revision_id": obj["revision_id"]},
                }),
                "linked", principal_id, self.now(),
            ],
        )
        return link_id

    def object_links(self, namespace: str, record_ids: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "astronomy_registration_object_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, record_id, object_record_id, matched_on_json, evidence_json, state "
            "FROM astronomy_registration_object_links WHERE namespace=? ORDER BY link_id",
            [namespace],
        ).fetchall()
        wanted = None if record_ids is None else set(record_ids)
        return [
            {"contract": LINK_CONTRACT, "link_id": r[0], "record_id": r[1], "object_record_id": r[2],
             "basis": "exact-identifier", "matched_on": json.loads(r[3]), "evidence": json.loads(r[4]),
             "state": r[5]}
            for r in rows
            if wanted is None or r[1] in wanted or r[2] in wanted
        ]

    def accepted_object_keys(self, namespace: str) -> list[tuple[str, str]]:
        return [
            (a, b) for a, b in self.astronomy.accepted_pairs(namespace)
            if a.startswith("astronomy:registration:") or b.startswith("astronomy:registration:")
        ]

    # -------------------------------------------------------------- parties (SO08)

    def parties(self, namespace: str) -> list[dict[str, Any]]:
        """Every registering State, intergovernmental registrant and operator/owner the records state."""
        found: dict[str, dict[str, Any]] = {}

        def add(kind: str, name: str, record_id: str, **extra: Any) -> None:
            key = f"{PARTY_PREFIX}{kind}:{party_key(name)}"
            party = found.setdefault(key, {"party_key": key, "kind": kind, "names": set(), "records": set(),
                                           "roles": set(), "identifiers": set()})
            party["names"].add(name)
            party["records"].add(record_id)
            if extra.get("role"):
                party["roles"].add(extra["role"])
            if extra.get("identifier"):
                party["identifiers"].add((extra["identifier"]["scheme"], extra["identifier"]["value"]))

        for view in self.store.visible(namespace)["records"]:
            record = view["record"]
            if record["kind"] == "registration_entry":
                igo = record.get("registrant_kind") == "intergovernmental_organisation"
                if record.get("registering_state"):
                    add("organisation" if igo else "state", record["registering_state"], view["record_id"],
                        role="registrant")
                for side in ("from", "to"):
                    value = (record.get("supervision") or {}).get(side)
                    if value:
                        add("state", value, view["record_id"], role=f"supervising State ({side})")
            elif record["kind"] == "operator_assertion":
                add("operator", record["operator_name"], view["record_id"], role=record["role"],
                    identifier=record.get("identifier"))
        out = []
        for party in found.values():
            out.append({**party, "names": sorted(party["names"]), "records": sorted(party["records"]),
                        "roles": sorted(party["roles"]),
                        "identifiers": [{"scheme": s, "value": v} for s, v in sorted(party["identifiers"])]})
        return sorted(out, key=lambda p: p["party_key"])

    def _canonical(self, types: tuple[str, ...]) -> list[tuple[str, str]]:
        if not table_exists(self.conn, "canonical_entities"):
            return []
        return self.conn.execute(
            "SELECT canonical_id, preferred_name FROM canonical_entities WHERE lower(coalesce(entity_type, '')) IN ("
            + ",".join("?" * len(types)) + ") ORDER BY canonical_id",
            list(types),
        ).fetchall()

    def match_parties(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        ownership_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Offer reviewable party candidates; idempotent and never an automatic merge."""
        from src.kb.ownership_identity import OwnershipIdentityService
        from src.kb.ownership_store import canonical_entity_id

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        service = OwnershipIdentityService(self.conn, now=self.now)
        countries, organisations = self._canonical(COUNTRY_TYPES), self._canonical(ORGANISATION_TYPES)
        legal_entities = []
        if ownership_namespace:
            from src.kb.ownership_store import OwnershipStore

            legal_entities = [
                v for v in OwnershipStore(self.conn, initialize=False).records(
                    ownership_namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",))
                if not v.get("redacted")
            ]
        offered = []

        def offer(party, right_key, right_entity, basis, right):
            offered.append(service.offer(
                namespace, left_key=party["party_key"], right_key=right_key,
                left_entity=canonical_entity_id(party["party_key"]), right_entity=right_entity, basis=basis,
                evidence=[{"method": basis, "left": {"party_key": party["party_key"], "kind": party["kind"],
                                                     "names_as_published": party["names"], "roles": party["roles"],
                                                     "identifiers": party["identifiers"],
                                                     "records": party["records"]},
                           "right": right,
                           "policy": "attribution only as published; a reviewer decides"}],
                principal_id=principal_id, scopes=scopes))

        for party in self.parties(namespace):
            names = {party_key(n) for n in party["names"]}
            pool = countries if party["kind"] == "state" else organisations
            for canonical_id, preferred in pool:
                other = party_key(preferred)
                basis = ("name-jurisdiction" if other in names
                         else "similar-name" if any(_similar(n, other) for n in names) else None)
                if basis:
                    offer(party, f"canonical:{canonical_id}", canonical_id, basis,
                          {"canonical_id": canonical_id, "preferred_name": preferred,
                           "entity_kind": "country" if party["kind"] == "state" else "organisation"})
            if party["kind"] == "operator":
                published = {(i["scheme"].lower(), i["value"].upper()) for i in party["identifiers"]}
                for view in legal_entities:
                    entity = view["record"]
                    theirs = {(str(i.get("scheme", "")).lower(), str(i.get("value", "")).upper())
                              for i in entity.get("identifiers") or []}
                    if published & theirs:
                        offer(party, entity["record_key"], canonical_entity_id(entity["record_key"]),
                              "exact-identifier",
                              {"record_key": entity["record_key"], "name": entity.get("name"),
                               "identifiers": sorted(f"{s}:{v}" for s, v in published & theirs),
                               "revision": view.get("revision")})
        return {
            "proposed": sorted({o["candidate_id"] for o in offered if o.get("change")}),
            "candidates": self.party_candidates(namespace, scopes=scopes),
            "notice": "parties are what registrations and operator assertions state; no other attribution is made",
        }

    def party_candidates(self, namespace: str, *, scopes: Iterable[str], party: str | None = None
                         ) -> list[dict[str, Any]]:
        from src.kb.ownership_identity import OwnershipIdentityService

        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return []
        rows = OwnershipIdentityService(self.conn, initialize=False).candidates(
            namespace, scopes=set(scopes) | {"knowledge:ownership:read"}, record_key=party)
        return [c for c in rows if c["left_key"].startswith(PARTY_PREFIX) or c["right_key"].startswith(PARTY_PREFIX)]

    def _own(self, namespace: str, candidate_id: str, scopes: Iterable[str]) -> None:
        if not any(c["candidate_id"] == candidate_id for c in self.party_candidates(namespace, scopes=scopes)):
            raise AstronomyError("not_found", "no space-registration party candidate with that id")

    def review_party(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
                     scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_identity import OwnershipIdentityService

        self._own(namespace, candidate_id, scopes)
        return OwnershipIdentityService(self.conn, now=self.now).review(
            namespace, candidate_id, decision, reason, principal_id=principal_id, scopes=scopes)

    def revert_party(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
                     scopes: Iterable[str]) -> dict[str, Any]:
        from src.kb.ownership_identity import OwnershipIdentityService

        self._own(namespace, candidate_id, scopes)
        return OwnershipIdentityService(self.conn, now=self.now).revert(
            namespace, candidate_id, reason, principal_id=principal_id, scopes=scopes)

    def party_identity(self, namespace: str, kind: str, name: str) -> dict[str, Any]:
        """Accepted links and open candidates for one stated party (read-only; no scope beyond the caller's)."""
        key = f"{PARTY_PREFIX}{kind}:{party_key(name)}"
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return {"party_key": key, "state": "unmatched", "links": [], "candidates": []}
        rows = self.conn.execute(
            "SELECT candidate_id, left_key, right_key, basis, state FROM ownership_identity_candidates "
            "WHERE namespace=? AND (left_key=? OR right_key=?) ORDER BY candidate_id",
            [namespace, key, key],
        ).fetchall()
        links = [{"candidate_id": r[0], "entity": r[2] if r[1] == key else r[1], "basis": r[3]}
                 for r in rows if r[4] == "accepted"]
        return {"party_key": key, "state": "matched" if links else "unmatched", "links": links,
                "candidates": [r[0] for r in rows if r[4] == "proposed"]}
