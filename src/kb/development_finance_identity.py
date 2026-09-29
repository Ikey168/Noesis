"""Reviewable identity for participating organisations and the open-call cross-reference (#1932, D07).

An organisation stays the source string (and the reference) a publisher
reports. Each organisation *as one publisher reports it* is an identity
subject (``devfin:org:<publisher>:<reference or name>``), so two publishers
naming the same organisation are two subjects and reporting organisations are
never collapsed. Subjects are offered as candidates, from stated evidence only,
to

* Corporate Ownership legal entities (``ownership.identity``): an IATI
  organisation identifier whose registration-agency prefix names a register
  the ownership record carries (``GB-COH-`` Companies House number, ``XI-LEI-``
  LEI), compared after the same normalisation on both sides
  (``cross-referenced-identifier``);
* development-finance publishers: an organisation reference equal to a
  publisher's reporting-org reference (``cross-referenced-identifier``);
* ``canonical_entities`` (``platform.entity-identity``): a name that resolves
  to a canonical alias (``similar-name``, which can never be accepted);
* funders of open calls in ``funding.opportunities``: a funder id equal to the
  organisation reference (``cross-referenced-identifier``) or a funder name
  inside the organisation name (``similar-name``).

Every candidate enters the shared reviewable state machine,
:class:`src.kb.ownership_identity.OwnershipIdentityService`; decisions are
recorded as entity identity decisions, reverting restores the unmatched state,
and nothing is linked automatically. A subject with several pending candidates
to different records and no accepted one is *ambiguous* and stays unmatched.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.development_finance_sources import identifier_key
from src.kb.development_finance import (
    READ_SCOPE,
    DevelopmentFinanceError,
    DevelopmentFinanceStore,
    authorize,
    name_key,
    publisher_id,
    require_scope,
    table_exists,
)

KEY_PREFIX = "devfin:"
FUNDER_PREFIX = "funding-funder:"
FUNDING_READ = "knowledge:funding:read"
OWNERSHIP_READ = "knowledge:ownership:read"
# IATI organisation-identifier prefixes (org-id.guide registration agencies) -> ownership identifier schemes.
REGISTER_PREFIXES = {"GB-COH-": "gb-coh", "XI-LEI-": "lei"}
CALL_NOTE = (
    "an open call is a cross-reference to the funder's opportunity record: never an activity, a transaction or "
    "a commitment, and no eligibility is inferred"
)


def subject_key(publisher: str, ref: Any, name: Any) -> str:
    if ref:
        return f"{KEY_PREFIX}org:{publisher}:ref:{identifier_key(ref)}"
    return f"{KEY_PREFIX}org:{publisher}:name:{name_key(name).replace(' ', '-') or 'unnamed'}"


def publisher_key(publisher: str) -> str:
    return f"{KEY_PREFIX}publisher:{publisher}"


def funder_key(funder_id: Any) -> str:
    return FUNDER_PREFIX + identifier_key(funder_id)


def register_number(ref: Any) -> tuple[str, str] | None:
    """(ownership scheme, normalised number) of an IATI organisation identifier with a known register prefix."""
    key = identifier_key(ref)
    for prefix, scheme in REGISTER_PREFIXES.items():
        if key.startswith(prefix):
            return scheme, number_key(key[len(prefix) :])
    return None


def number_key(value: Any) -> str:
    """A register number compared the same way on both sides: alphanumerics, upper case."""
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def _entity(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


class DevelopmentFinanceIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.store = DevelopmentFinanceStore(conn, initialize=initialize, now=now)
        self.service = OwnershipIdentityService(conn, now=now, initialize=initialize)

    # ------------------------------------------------------------------ subjects

    def subjects(
        self, namespace: str, *, as_of: int | None = None
    ) -> list[dict[str, Any]]:
        """Every organisation each publisher's current revisions name (reporting, participating, parties)."""
        subjects: dict[str, dict[str, Any]] = {}

        def add(publisher, org, role, revision):
            if not org or not (org.get("ref") or org.get("name")):
                return
            key = subject_key(publisher, org.get("ref"), org.get("name"))
            item = subjects.setdefault(
                key,
                {
                    "record_key": key,
                    "entity_id": _entity(key),
                    "publisher_id": publisher,
                    "ref": org.get("ref"),
                    "name": org.get("name"),
                    "roles": set(),
                    "activities": set(),
                    "revisions": set(),
                },
            )
            item["roles"].add(role)
            item["activities"].add(revision["activity_key"])
            item["revisions"].add(revision["revision_id"])
            if not item["name"] and org.get("name"):
                item["name"] = org["name"]

        for revision in self.store.current(namespace, as_of=as_of).values():
            activity, publisher = revision["activity"], revision["publisher_id"]
            add(publisher, activity.get("reporting_org"), "reporting", revision)
            for org in activity.get("participating_orgs") or []:
                add(
                    publisher,
                    org,
                    f"participating:{org.get('role') or 'unstated'}",
                    revision,
                )
            for tx in revision["transactions"]:
                add(publisher, tx.get("provider_org"), "transaction-provider", revision)
                add(publisher, tx.get("receiver_org"), "transaction-receiver", revision)
        return [
            {
                **s,
                "roles": sorted(s["roles"]),
                "activities": sorted(s["activities"]),
                "revisions": sorted(s["revisions"]),
            }
            for _, s in sorted(subjects.items())
        ]

    def _offer(
        self,
        namespace,
        subject,
        right_key,
        right_entity,
        basis,
        evidence,
        principal_id,
        scopes,
    ):
        side = {
            "publisher_id": subject["publisher_id"],
            "ref": subject["ref"],
            "name": subject["name"],
            "revisions": subject["revisions"],
        }
        return self.service.offer(
            namespace,
            left_key=subject["record_key"],
            right_key=right_key,
            left_entity=subject["entity_id"],
            right_entity=right_entity,
            basis=basis,
            evidence=[{**evidence, "left": side}],
            principal_id=principal_id,
            scopes=scopes,
        )

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        ownership_namespace: str | None = None,
        funding_namespace: str | None = None,
        canonical: bool = True,
    ) -> dict[str, Any]:
        """Offer subjects to ownership records, publishers, canonical entities and funders; nothing is linked."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        subjects = self.subjects(namespace)
        offered = self._publishers(namespace, subjects, principal_id, scopes)
        if ownership_namespace:
            require_scope(scopes, OWNERSHIP_READ)
            offered += self._ownership(
                namespace, ownership_namespace, subjects, principal_id, scopes
            )
        if funding_namespace:
            require_scope(scopes, FUNDING_READ)
            offered += self._funders(
                namespace, funding_namespace, subjects, principal_id, scopes
            )
        if canonical:
            offered += self._canonical(namespace, subjects, principal_id, scopes)
        return {
            "proposed": sorted(
                {o["candidate_id"] for o in offered if o["created"] or o.get("change")}
            ),
            "candidates": self.candidates(namespace, scopes=scopes),
        }

    def _publishers(self, namespace, subjects, principal_id, scopes):
        offered = []
        publishers = {
            identifier_key(p["publisher_ref"]): p
            for p in self.store.publishers(namespace)
            if p["provider"] == "iati" and p["publisher_ref"]
        }
        for subject in subjects:
            publisher = (
                publishers.get(identifier_key(subject["ref"]))
                if subject["ref"]
                else None
            )
            if publisher is None:
                continue
            key = publisher_key(publisher["publisher_id"])
            offered.append(
                self._offer(
                    namespace,
                    subject,
                    key,
                    _entity(key),
                    "cross-referenced-identifier",
                    {
                        "kind": "iati-organisation-identifier",
                        "value": subject["ref"],
                        "right": {
                            "record_key": key,
                            "publisher_id": publisher["publisher_id"],
                            "publisher_ref": publisher["publisher_ref"],
                        },
                        "fields": ["participating-org/@ref", "reporting-org/@ref"],
                        "note": "links a reported organisation to a publisher record; publishers are never merged",
                    },
                    principal_id,
                    scopes,
                )
            )
        return offered

    def _ownership(
        self, namespace, ownership_namespace, subjects, principal_id, scopes
    ):
        from src.kb.ownership_store import OwnershipStore

        if not table_exists(self.conn, "ownership_records"):
            return []
        entities = [
            e
            for e in OwnershipStore(self.conn, initialize=False).records(
                ownership_namespace,
                principal_id=principal_id,
                scopes=scopes,
                kinds=("legal_entity",),
            )
            if not e.get("redacted")
        ]
        offered = []
        for subject in subjects:
            stated = register_number(subject["ref"])
            if stated is None:
                continue
            for entity in entities:
                body = entity["record"]
                for identifier in body.get("identifiers") or []:
                    if (
                        str(identifier.get("scheme") or "").casefold() != stated[0]
                        or number_key(identifier.get("value")) != stated[1]
                    ):
                        continue
                    right_entity = body.get("canonical_entity_id") or _entity(
                        body["record_key"]
                    )
                    offered.append(
                        self._offer(
                            namespace,
                            subject,
                            body["record_key"],
                            right_entity,
                            "cross-referenced-identifier",
                            {
                                "kind": stated[0],
                                "value": subject["ref"],
                                "fields": ["participating-org/@ref"],
                                "right": {
                                    "record_key": body["record_key"],
                                    "record_id": entity["record_id"],
                                    "revision": entity["revision"],
                                    "ownership_namespace": ownership_namespace,
                                },
                            },
                            principal_id,
                            scopes,
                        )
                    )
        return offered

    def _funders(self, namespace, funding_namespace, subjects, principal_id, scopes):
        from src.kb.funding_opportunities import FundingOpportunityStore

        if not table_exists(self.conn, "funding_opportunities"):
            return []
        funders: dict[str, dict[str, Any]] = {}
        for opportunity in FundingOpportunityStore(self.conn, initialize=False).list(
            funding_namespace, scopes=scopes
        ):
            funder = dict(opportunity["record"].get("funder") or {})
            if funder.get("id"):
                funders.setdefault(identifier_key(funder["id"]), funder)
        offered = []
        for subject in subjects:
            for fkey, funder in sorted(funders.items()):
                if subject["ref"] and identifier_key(subject["ref"]) == fkey:
                    basis, evidence = (
                        "cross-referenced-identifier",
                        {
                            "kind": "funder-id",
                            "value": funder["id"],
                            "fields": ["participating-org/@ref"],
                        },
                    )
                elif (
                    funder.get("name")
                    and subject["name"]
                    and name_key(funder["name"]) == name_key(subject["name"])
                ):
                    basis, evidence = (
                        "similar-name",
                        {
                            "kind": "name",
                            "value": subject["name"],
                            "fields": ["participating-org/narrative"],
                            "note": "an equal funder name is never an identity on its own",
                        },
                    )
                else:
                    continue
                key = funder_key(funder["id"])
                offered.append(
                    self._offer(
                        namespace,
                        subject,
                        key,
                        _entity(key),
                        basis,
                        {
                            **evidence,
                            "right": {
                                "record_key": key,
                                "funder": funder,
                                "funding_namespace": funding_namespace,
                            },
                        },
                        principal_id,
                        scopes,
                    )
                )
        return offered

    def _canonical(self, namespace, subjects, principal_id, scopes):
        if not table_exists(self.conn, "entity_aliases"):
            return []
        from src.kb.entities import resolve

        offered = []
        for subject in subjects:
            found = resolve(self.conn, subject["name"] or "")
            if not found:
                continue
            key = f"canonical:{found['canonical_id']}"
            offered.append(
                self._offer(
                    namespace,
                    subject,
                    key,
                    found["canonical_id"],
                    "similar-name",
                    {
                        "kind": "name",
                        "value": subject["name"],
                        "fields": ["narrative"],
                        "right": {
                            "record_key": key,
                            "canonical_id": found["canonical_id"],
                            "preferred_name": found["preferred_name"],
                        },
                        "note": "a name that resolves to a canonical alias; a name alone is never an identity",
                    },
                    principal_id,
                    scopes,
                )
            )
        return offered

    # ------------------------------------------------------------------ reads

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        state = candidate["state"]
        last = candidate["history"][-1]
        return {
            **candidate,
            "records": [candidate["left_key"], candidate["right_key"]],
            "review_state": {
                "proposed": "unreviewed",
                "accepted": "reviewed-match",
                "rejected": "reviewed-non-match",
                "reverted": "reverted",
            }[state],
            "reviewer": last.get("by")
            if state in {"accepted", "rejected", "reverted"}
            else None,
        }

    def candidates(
        self, namespace: str, *, scopes: Iterable[str], record_key: str | None = None
    ) -> list[dict]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        return [
            self.view(c)
            for c in self.service.candidates(
                namespace, scopes=scopes, record_key=record_key
            )
            if c["left_key"].startswith(KEY_PREFIX)
            or c["right_key"].startswith(KEY_PREFIX)
        ]

    def identity(
        self, namespace: str, key: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """A subject's reviewed links; without an accepted match it stays the source string."""
        if not key.startswith(KEY_PREFIX + "org:"):
            raise DevelopmentFinanceError(
                "invalid_request", "not a development-finance organisation key"
            )
        candidates = self.candidates(namespace, scopes=scopes, record_key=key)
        accepted = [c for c in candidates if c["state"] == "accepted"]
        pending = [c for c in candidates if c["state"] == "proposed"]
        targets = {
            c["right_key"] if c["left_key"] == key else c["left_key"] for c in pending
        }
        state = (
            "matched" if accepted else "ambiguous" if len(targets) > 1 else "unmatched"
        )
        return {
            "record_key": key,
            "state": state,
            "links": [
                {
                    "target": c["right_key"] if c["left_key"] == key else c["left_key"],
                    "basis": c["basis"],
                    "candidate_id": c["candidate_id"],
                    "decision_id": c["decision_id"],
                    "reviewer": c["reviewer"],
                }
                for c in accepted
            ],
            "pending": sorted(c["candidate_id"] for c in pending),
            "note": "identity links are reviewed decisions; the activity keeps the organisation as reported",
        }

    def linked_subjects(
        self, namespace: str, target: str, *, scopes: Iterable[str]
    ) -> list[str]:
        """Subjects joined to ``target`` (an ownership, canonical, publisher or funder key) by accepted decisions.

        A subject key itself resolves to itself plus every subject that shares an accepted target with it, so a
        funder query reaches activities across publishers without merging any publisher record.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        accepted = [
            c
            for c in self.candidates(namespace, scopes=scopes)
            if c["state"] == "accepted"
        ]
        targets = {target}
        if target.startswith(KEY_PREFIX + "org:"):
            targets |= {
                c["right_key"] if c["left_key"] == target else c["left_key"]
                for c in accepted
                if target in (c["left_key"], c["right_key"])
            }
        subjects = {target} if target.startswith(KEY_PREFIX + "org:") else set()
        for candidate in accepted:
            left, right = candidate["left_key"], candidate["right_key"]
            if right in targets and left.startswith(KEY_PREFIX + "org:"):
                subjects.add(left)
            if left in targets and right.startswith(KEY_PREFIX + "org:"):
                subjects.add(right)
        return sorted(subjects)

    def open_calls(
        self, namespace: str, key: str, *, funding_namespace: str, scopes: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Open and forthcoming calls of the funders a subject is matched to, with their revisions."""
        from src.kb.funding_opportunities import FundingOpportunityStore

        scopes = set(scopes)
        require_scope(scopes, FUNDING_READ)
        funders = {
            link["target"]
            for link in self.identity(namespace, key, scopes=scopes)["links"]
            if link["target"].startswith(FUNDER_PREFIX)
        }
        if not funders or not table_exists(self.conn, "funding_opportunities"):
            return []
        calls = []
        for opportunity in FundingOpportunityStore(self.conn, initialize=False).list(
            funding_namespace, scopes=scopes
        ):
            record = opportunity["record"]
            funder = dict(record.get("funder") or {})
            if not funder.get("id") or funder_key(funder["id"]) not in funders:
                continue
            if (opportunity.get("status") or {}).get("state") in {
                "closed",
                "not_an_opportunity",
            }:
                continue  # award history, directory entries and closed calls are not open calls
            calls.append(
                {
                    "opportunity_id": opportunity["opportunity_id"],
                    "revision": opportunity["current_revision"],
                    "provider": record["provider"],
                    "record_kind": record["record_kind"],
                    "title": record["title"],
                    "status": opportunity["status"],
                    "funder": funder,
                    "funding_namespace": funding_namespace,
                    "note": CALL_NOTE,
                }
            )
        return calls


def publisher_subject(publisher_ref: str) -> str:
    return publisher_key(publisher_id("iati", publisher_ref))
