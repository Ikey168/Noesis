"""Reviewable, non-destructive matches between designations and corporate/entity identity (#1907, S07).

Candidates are proposed only from identifiers a list states (LEI, registration
number, IMO number, passport or national ID) or from name-and-attribute
evidence a reviewer supplies, and every candidate carries the list revision and
the evidence fields it rests on. They enter the *existing* reviewable state
machine - :class:`src.kb.ownership_identity.OwnershipIdentityService`
(propose/review/revert, decisions in ``entity_identity_decisions``) - so
accepting links records without rewriting either side and reverting restores
the prior state. List records are never edited by any transition.

Cross-list candidates (an EU entry and an OFAC entry stating the same IMO
number) are assertions in the same state machine. A similar name alone is
shown as a ``similar-name`` candidate that the state machine refuses to accept.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.sanctions import (
    READ_SCOPE,
    SanctionsError,
    SanctionsStore,
    authorize,
    designation_entity_id,
    normalize_identifier,
    record_key,
)

IDENTIFIER_KINDS = ("lei", "registration_number", "imo", "passport", "national_id")
# Ownership identifier schemes a list-stated identifier may equal, with the country the scheme implies.
OWNERSHIP_SCHEMES = {
    "lei": {"lei": None},
    "imo": {"imo": None},
    "registration_number": {"gb-coh": "GB", "company_number": "GB"},
}
ATTRIBUTE_KINDS = ("date_of_birth", "nationality", "address")
# Identifiers unique worldwide; every other kind is only unique within its issuing country.
GLOBAL_IDENTIFIER_KINDS = ("imo", "lei")


def _issuer(value: Any) -> tuple[str, str] | None:
    """An issuing country as (form, value): an ISO code ("code") or a normalized name ("name")."""
    from src.ingestion.connectors.dataset.normalize import normalize_geography

    text = " ".join(str(value or "").split())
    if not text:
        return None
    if len(text) in (2, 3) and text.isalpha():
        return "code", str(normalize_geography(text))
    return "name", normalize_identifier(text)


def issuer_comparison(
    kind: str, left: Mapping[str, Any], right: Mapping[str, Any]
) -> dict[str, Any]:
    """Whether two stated identifiers come from the same issuer.

    ``global`` for worldwide identifiers (IMO, LEI); ``same`` or ``different``
    when both sides state a comparable issuing country; ``unknown`` when a
    country is missing or stated in forms that cannot be compared (a code
    against a name) - such a candidate is downgraded, never exact.
    """
    countries = {"left": left.get("country"), "right": right.get("country")}
    if kind in GLOBAL_IDENTIFIER_KINDS:
        return {"relation": "global", **countries}
    a, b = _issuer(left.get("country")), _issuer(right.get("country"))
    if a is None or b is None or a[0] != b[0]:
        return {"relation": "unknown", **countries}
    return {"relation": "same" if a[1] == b[1] else "different", **countries}


class SanctionsIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.store = SanctionsStore(conn, initialize=initialize, now=now)
        self.service = OwnershipIdentityService(conn, now=now, initialize=initialize)

    def _current(self, namespace: str) -> list[dict[str, Any]]:
        """Latest stated revision of every designation (a delisted entry keeps its last statement)."""
        rows = self.conn.execute(
            "SELECT d.designation_id, d.list_id, d.list_entry_id, r.revision_id, r.statement_json, r.snapshot_id "
            "FROM sanctions_designations d JOIN sanctions_revisions r ON r.namespace=d.namespace AND "
            "r.designation_id=d.designation_id WHERE d.namespace=? AND r.statement_json IS NOT NULL AND "
            "r.revision_no=(SELECT max(x.revision_no) FROM sanctions_revisions x WHERE x.namespace=d.namespace AND "
            "x.designation_id=d.designation_id AND x.statement_json IS NOT NULL) ORDER BY d.designation_id",
            [namespace],
        ).fetchall()
        return [
            {
                "designation_id": r[0],
                "list_id": r[1],
                "list_entry_id": r[2],
                "revision_id": r[3],
                "statement": json.loads(r[4]),
                "snapshot_id": r[5],
                "record_key": record_key(r[1], r[2]),
                "entity_id": designation_entity_id(r[1], r[2]),
            }
            for r in rows
        ]

    @staticmethod
    def _side(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "record_key": item["record_key"],
            "list_id": item["list_id"],
            "revision_id": item["revision_id"],
            "snapshot_id": item["snapshot_id"],
        }

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        ownership_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Propose identifier-based candidates (cross-list and to Corporate Ownership records); idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        current = self._current(namespace)
        offered = []
        index: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for item in current:
            for identifier in item["statement"].get("identifiers") or []:
                if identifier["kind"] in IDENTIFIER_KINDS and normalize_identifier(
                    identifier["value"]
                ):
                    index.setdefault(
                        (identifier["kind"], normalize_identifier(identifier["value"])),
                        [],
                    ).append((item, identifier))
        for (kind, value), holders in sorted(index.items()):
            for i, (left, left_id) in enumerate(holders):
                for right, right_id in holders[i + 1 :]:
                    if left["list_id"] == right["list_id"]:
                        continue  # one list's own entries are its business; lists are only compared across
                    issuers = issuer_comparison(kind, left_id, right_id)
                    if issuers["relation"] == "different":
                        continue  # same digits from two issuers are two different documents
                    offered.append(
                        self.service.offer(
                            namespace,
                            left_key=left["record_key"],
                            right_key=right["record_key"],
                            left_entity=left["entity_id"],
                            right_entity=right["entity_id"],
                            basis="exact-identifier"
                            if issuers["relation"] in {"same", "global"}
                            else "unqualified-identifier",
                            evidence=[
                                {
                                    "kind": kind,
                                    "value": left_id["value"],
                                    "left": self._side(left),
                                    "right": self._side(right),
                                    "issuers": issuers,
                                    "fields": ["identifiers"],
                                    "note": "both lists state this identifier; the lists stay separate",
                                }
                            ],
                            principal_id=principal_id,
                            scopes=scopes,
                        )
                    )
        if ownership_namespace:
            offered += self._ownership(
                namespace, ownership_namespace, index, principal_id, scopes
            )
        return {
            "proposed": [
                o["candidate_id"] for o in offered if o["created"] or o.get("change")
            ],
            "candidates": self.candidates(namespace, scopes=scopes),
        }

    def _ownership(
        self, namespace, ownership_namespace, index, principal_id, scopes
    ) -> list[dict[str, Any]]:
        from src.kb.ownership_store import OwnershipStore

        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='ownership_records'"
        ).fetchone():
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
            for identifier in body.get("identifiers") or []:
                for kind, schemes in OWNERSHIP_SCHEMES.items():
                    if identifier.get("scheme") not in schemes:
                        continue
                    implied = schemes[identifier["scheme"]]
                    for item, stated in index.get(
                        (kind, normalize_identifier(identifier.get("value"))), []
                    ):
                        country = str(stated.get("country") or "").upper()[:2]
                        if implied and country and country != implied:
                            continue  # a registration number is only comparable within its register's country
                        offered.append(
                            self.service.offer(
                                namespace,
                                left_key=item["record_key"],
                                right_key=body["record_key"],
                                left_entity=item["entity_id"],
                                right_entity=body.get("canonical_entity_id")
                                or body["record_key"],
                                basis="exact-identifier",
                                evidence=[
                                    {
                                        "kind": kind,
                                        "value": stated["value"],
                                        "left": self._side(item),
                                        "right": {
                                            "record_key": body["record_key"],
                                            "record_id": entity["record_id"],
                                            "revision": entity["revision"],
                                            "ownership_namespace": ownership_namespace,
                                            "scheme": identifier["scheme"],
                                        },
                                        "fields": ["identifiers"],
                                    }
                                ],
                                principal_id=principal_id,
                                scopes=scopes,
                            )
                        )
        return offered

    def propose_link(
        self,
        namespace: str,
        designation_id: str,
        *,
        target_key: str,
        target_entity: str,
        evidence: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A reviewer-supplied candidate to any entity (e.g. a canonical entity), resting on what the list states.

        ``evidence`` names the attribute the list states (``kind`` and ``value``)
        and where the reviewer saw it on the target (``target_source``). An
        identifier yields a ``cross-referenced-identifier`` candidate, a name
        plus a date of birth, nationality or address a ``name-jurisdiction``
        candidate, and a name alone a ``similar-name`` candidate that can never
        be accepted.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        item = next(
            (
                c
                for c in self._current(namespace)
                if c["designation_id"] == designation_id
            ),
            None,
        )
        if item is None:
            raise SanctionsError(
                "not_found", "designation is not visible in this namespace"
            )
        if not str(evidence.get("target_source") or "").strip():
            raise SanctionsError(
                "invalid_request",
                "cite where the target states the attribute (target_source)",
            )
        statement = item["statement"]
        stated = self.store.aliases(namespace, item["revision_id"])
        kind, value = (
            str(evidence.get("kind") or ""),
            normalize_identifier(evidence.get("value")),
        )
        matched = [
            a
            for a in stated
            if a["alias_kind"] == kind and normalize_identifier(a["value"]) == value
        ]
        if not matched:
            raise SanctionsError(
                "not_stated",
                "the list revision does not state this attribute value; candidates "
                "rest only on what the list states",
            )
        names = [a for a in stated if a["alias_kind"] in {"name", "transliteration"}]
        attributes = [dict(a) for a in (evidence.get("attributes") or [])]
        corroborated = [
            a
            for a in attributes
            if a.get("kind") in ATTRIBUTE_KINDS
            and any(
                s["alias_kind"] == a["kind"]
                and normalize_identifier(s["value"])
                == normalize_identifier(a.get("value"))
                for s in stated
            )
        ]
        if kind in IDENTIFIER_KINDS:
            basis = "cross-referenced-identifier"
        elif kind in {"name", "transliteration"} and corroborated:
            basis = "name-jurisdiction"
        elif kind in {"name", "transliteration"}:
            basis = "similar-name"
        else:
            raise SanctionsError(
                "invalid_request", "evidence names an identifier or a stated name"
            )
        return self.service.offer(
            namespace,
            left_key=item["record_key"],
            right_key=target_key,
            left_entity=item["entity_id"],
            right_entity=target_entity,
            basis=basis,
            evidence=[
                {
                    "kind": kind,
                    "value": matched[0]["value"],
                    "left": self._side(item),
                    "right": {
                        "record_key": target_key,
                        "entity_id": target_entity,
                        "target_source": evidence["target_source"],
                    },
                    "corroborating_attributes": corroborated,
                    "stated_names": [n["value"] for n in names],
                    "party_kind": statement.get("party_kind"),
                    "fields": [kind] + sorted({a["kind"] for a in corroborated}),
                }
            ],
            principal_id=principal_id,
            scopes=scopes,
        )

    def candidates(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        designation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        key = None
        if designation_id is not None:
            key = self.store.designation(namespace, designation_id)["record_key"]
        rows = [
            c
            for c in self.service.candidates(namespace, scopes=scopes, record_key=key)
            if c["left_key"].startswith("sanctions:")
            or c["right_key"].startswith("sanctions:")
        ]
        return [self.view(c) for c in rows]

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        compared = [
            side
            for e in candidate["evidence"]
            for side in (e.get("left"), e.get("right"))
            if side
        ]
        return {
            "candidate_id": candidate["candidate_id"],
            "state": candidate["state"],
            "basis": candidate["basis"],
            "confidence": candidate["confidence"],
            "records": [candidate["left_key"], candidate["right_key"]],
            "entities": [candidate["left_entity"], candidate["right_entity"]],
            "decision_id": candidate["decision_id"],
            "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
            "reason": last.get("reason"),
            "evidence": candidate["evidence"],
            "source_revisions_compared": compared,
            "history": candidate["history"],
            "notice": "a reviewable assertion; list records are never merged or edited",
        }

    def links(
        self, namespace: str, designation_key: str, *, scopes: Iterable[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Accepted links and open candidates for one designation (reviewed states only become links)."""
        try:
            rows = self.service.candidates(
                namespace, scopes=scopes, record_key=designation_key
            )
        except Exception as exc:  # noqa: BLE001 - identity access is optional for an answer
            return {
                "links": [],
                "candidates": [],
                "unavailable": [str(getattr(exc, "code", "unavailable"))],
            }
        views = [self.view(r) for r in rows]
        return {
            "links": [v for v in views if v["state"] == "accepted"],
            "candidates": [v for v in views if v["state"] == "proposed"],
        }
