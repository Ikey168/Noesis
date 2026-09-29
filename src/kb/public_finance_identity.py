"""Reviewable, reversible identity for payment beneficiaries (#1909, B05).

A beneficiary stays the source string (and the identifiers) an FTS row states.
It is offered as an identity *candidate* to

* Corporate Ownership legal entities (``ownership.identity``): a stated VAT
  number equal to an ownership record's VAT identifier, compared only within
  the issuing country (the VAT prefix or the stated country), or an equal
  normalised name in the same country (weak, ``name-jurisdiction``);
* ``canonical_entities`` (``platform.entity-identity``): a name that resolves
  to a canonical alias, which is a ``similar-name`` candidate and can never be
  accepted on its own;
* Funding award records (``funding.opportunities``): a grant reference the
  row states equal to the award's provider id (``cross-referenced-identifier``),
  or the beneficiary name inside an award title (``similar-name``).

Every candidate carries the payment revision and the evidence fields it rests
on and enters the existing reviewable state machine,
:class:`src.kb.ownership_identity.OwnershipIdentityService`: decisions are
recorded in ``entity_identity_decisions``, accepting links records without
rewriting them, and reverting restores the unmatched state. An unmatched
beneficiary stays a source string, and beneficiary links never regroup
ownership entities.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.public_finance import (
    READ_SCOPE,
    PublicFinanceError,
    PublicFinanceStore,
    authorize,
    entity_id,
    normalize_identifier,
    normalize_name,
    require_scope,
    table_exists,
)

FUNDING_READ = "knowledge:funding:read"
KEY_PREFIX = "public-finance:"


def _country(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text[:2] if len(text) >= 2 and text[:2].isalpha() else None


def vat_key(value: Any, country: Any = None) -> tuple[str | None, str] | None:
    """(issuing country, number) of a VAT identifier, normalised the same way on both sides of a match.

    The two-letter prefix is the issuing country; a number without a prefix takes the stated country. A prefix
    that contradicts the stated country leaves the issuer unknown rather than picking one.
    """
    text = normalize_identifier(value)
    if not text:
        return None
    stated = _country(country)
    prefix = text[:2] if text[:2].isalpha() else None
    number = text[2:] if prefix else text
    if (
        prefix
        and stated
        and prefix != stated
        and not (prefix == "EL" and stated == "GR")
    ):
        return None, number
    return (prefix or stated), number


class PublicFinanceIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.store = PublicFinanceStore(conn, initialize=initialize, now=now)
        self.service = OwnershipIdentityService(conn, now=now, initialize=initialize)

    def subjects(self, namespace: str) -> list[dict[str, Any]]:
        out = []
        for beneficiary in self.store.beneficiaries(namespace):
            latest = self.store.payment(namespace, beneficiary["payments"][-1])
            statement = latest["statement"]
            out.append(
                {
                    **beneficiary,
                    "record_key": beneficiary["beneficiary_key"],
                    "grant_references": sorted(
                        {
                            p["statement"].get("grant_reference")
                            for p in (
                                self.store.payment(namespace, pid)
                                for pid in beneficiary["payments"]
                            )
                            if p["statement"].get("grant_reference")
                        }
                    ),
                    "programmes": sorted(
                        {
                            p["programme"]
                            for p in (
                                self.store.payment(namespace, pid)
                                for pid in beneficiary["payments"]
                            )
                            if p["programme"]
                        }
                    ),
                    "side": {
                        "payment_id": latest["payment_id"],
                        "release_id": latest["release_id"],
                        "published_on": latest["published_on"],
                        "beneficiary": statement.get("beneficiary"),
                    },
                }
            )
        return out

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
        return self.service.offer(
            namespace,
            left_key=subject["record_key"],
            right_key=right_key,
            left_entity=subject["entity_id"],
            right_entity=right_entity,
            basis=basis,
            evidence=[evidence],
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
        """Offer beneficiaries to ownership, funding and canonical entities; nothing is linked until reviewed.

        Idempotent; a stronger basis upgrades a pending weaker candidate and new evidence re-proposes a rejected
        or reverted one (shared state-machine rules). Payment records are never edited.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subjects = self.subjects(namespace)
        offered = []
        if ownership_namespace:
            offered += self._ownership(
                namespace, ownership_namespace, subjects, principal_id, scopes
            )
        if funding_namespace:
            require_scope(scopes, FUNDING_READ)
            offered += self._funding(
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
        for entity in entities:
            body = entity["record"]
            jurisdiction = _country(body.get("jurisdiction"))
            right = {
                "record_key": body["record_key"],
                "record_id": entity["record_id"],
                "revision": entity["revision"],
                "ownership_namespace": ownership_namespace,
            }
            right_entity = body.get("canonical_entity_id") or entity_id(
                body["record_key"]
            )
            owned = [
                vat_key(i.get("value"), i.get("country") or jurisdiction)
                for i in body.get("identifiers") or []
                if str(i.get("scheme") or "").casefold() == "vat"
            ]
            for subject in subjects:
                matched = False
                for ident in subject["identifiers"]:
                    if ident.get("scheme") != "vat":
                        continue
                    ours = vat_key(
                        ident.get("value"), ident.get("country") or subject["country"]
                    )
                    for theirs in owned:
                        if ours is None or theirs is None or ours[1] != theirs[1]:
                            continue
                        if ours[0] is None or theirs[0] is None:
                            basis, issuers = "unqualified-identifier", "unknown"
                        elif ours[0] != theirs[0]:
                            continue  # the same digits from two issuing countries are two identifiers
                        else:
                            basis, issuers = "cross-referenced-identifier", "same"
                        matched = True
                        offered.append(
                            self._offer(
                                namespace,
                                subject,
                                body["record_key"],
                                right_entity,
                                basis,
                                {
                                    "kind": "vat",
                                    "value": ident["value"],
                                    "issuers": issuers,
                                    "left": subject["side"],
                                    "right": right,
                                    "fields": ["beneficiary_identifiers"],
                                },
                                principal_id,
                                scopes,
                            )
                        )
                country = _country(subject["country"])
                if (
                    not matched
                    and country
                    and jurisdiction == country
                    and normalize_name(subject["name"])
                    == normalize_name(body.get("name"))
                ):
                    offered.append(
                        self._offer(
                            namespace,
                            subject,
                            body["record_key"],
                            right_entity,
                            "name-jurisdiction",
                            {
                                "kind": "name",
                                "value": subject["name"],
                                "country": country,
                                "left": subject["side"],
                                "right": right,
                                "fields": ["beneficiary", "beneficiary_country"],
                                "note": "equal normalized names in one country are a weak signal, never an identity",
                            },
                            principal_id,
                            scopes,
                        )
                    )
        return offered

    def _funding(self, namespace, funding_namespace, subjects, principal_id, scopes):
        from src.kb.funding_opportunities import FundingOpportunityStore

        if not table_exists(self.conn, "funding_opportunities"):
            return []
        awards = FundingOpportunityStore(self.conn, initialize=False).list(
            funding_namespace, scopes=scopes, kinds=["award"]
        )
        offered = []
        for award in awards:
            record = award["record"]
            key = f"funding:{award['opportunity_id']}"
            right = {
                "record_key": key,
                "opportunity_id": award["opportunity_id"],
                "revision": award.get("revision") or award.get("current_revision"),
                "funding_namespace": funding_namespace,
                "provider": record["provider"],
                "provider_id": record["provider_id"],
                "title": record["title"],
            }
            for subject in subjects:
                references = {
                    normalize_identifier(r) for r in subject["grant_references"]
                }
                if normalize_identifier(record["provider_id"]) in references:
                    basis, evidence = (
                        "cross-referenced-identifier",
                        {
                            "kind": "grant-reference",
                            "value": record["provider_id"],
                            "fields": ["grant_reference"],
                        },
                    )
                elif subject["name"] and normalize_name(
                    subject["name"]
                ) in normalize_name(record["title"]):
                    basis, evidence = (
                        "similar-name",
                        {
                            "kind": "name",
                            "value": subject["name"],
                            "fields": ["beneficiary"],
                            "note": "the award title names the beneficiary; a name alone is never an identity",
                        },
                    )
                else:
                    continue
                offered.append(
                    self._offer(
                        namespace,
                        subject,
                        key,
                        entity_id(key),
                        basis,
                        {**evidence, "left": subject["side"], "right": right},
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
                        "left": subject["side"],
                        "right": {
                            "record_key": key,
                            "canonical_id": found["canonical_id"],
                            "preferred_name": found["preferred_name"],
                        },
                        "fields": ["beneficiary"],
                        "note": "a name that resolves to a canonical entity alias; a name alone is never an identity",
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
        self, namespace: str, beneficiary_key: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """A beneficiary's reviewed links; without an accepted match it stays the source string."""
        if not beneficiary_key.startswith(KEY_PREFIX):
            raise PublicFinanceError(
                "invalid_request", "not a public-finance beneficiary key"
            )
        candidates = self.candidates(
            namespace, scopes=scopes, record_key=beneficiary_key
        )
        accepted = [c for c in candidates if c["state"] == "accepted"]
        return {
            "beneficiary_key": beneficiary_key,
            "state": "matched" if accepted else "unmatched",
            "links": [
                {
                    "target": c["right_key"]
                    if c["left_key"] == beneficiary_key
                    else c["left_key"],
                    "basis": c["basis"],
                    "candidate_id": c["candidate_id"],
                    "decision_id": c["decision_id"],
                    "reviewer": c["reviewer"],
                }
                for c in accepted
            ],
            "pending": [
                c["candidate_id"] for c in candidates if c["state"] == "proposed"
            ],
            "note": "identity links are reviewed decisions; the payment keeps the beneficiary as published",
        }
