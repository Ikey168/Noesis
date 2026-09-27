"""Reviewable, reversible matches between register records and corporate/entity identity (#1911, T06).

Registrants and declared clients are proposed as identity candidates from what
a register states - a declared LEI, national company number, EU Transparency
Register number, Lobbyregister number or website - against other register
records, Corporate Ownership records (``ownership.identity``), GLEIF records
in the LEI store (``market.legal-entities``) and canonical entities
(``platform.entity-identity``). Every candidate carries the register revision
and the evidence fields it rests on and enters the *existing* reviewable state
machine, :class:`src.kb.ownership_identity.OwnershipIdentityService`
(propose/review/revert, decisions in ``entity_identity_decisions``). No new
matcher, entity store or merge exists: accepting links records without
rewriting them, reverting restores the unmatched state, and the same
organisation in the EU, German and UK registers stays three registrant records
with three native identifiers.

Identifier comparison respects the issuer: a company number is only comparable
within the register country that issues it; a TR or Lobbyregister number is
issued by that register. A name alone is a ``similar-name`` candidate that the
state machine refuses to accept. Where a registrant is also a news or media
source, the match to OSINT source identity is left to that store's alias
review flow (:meth:`LobbyingIdentity.source_identity_candidates`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import urlsplit

from src.kb.lobbying import (
    READ_SCOPE,
    LobbyingError,
    LobbyingStore,
    authorize,
    client_key,
    entry_entity_id,
    normalize_name,
)

# Scheme -> the register whose own identifier it is (its "primary" holder) and the country that issues it.
SCHEMES = {
    "lei": {"primary": None, "country": None},
    "eu-tr": {"primary": "eu-tr", "country": None},
    "de-lobbyregister": {"primary": "de-lobbyregister", "country": None},
    "gb-coh": {"primary": None, "country": "GB"},
    "de-register": {"primary": None, "country": "DE"},
    "website": {"primary": None, "country": None},
}
# Ownership identifier schemes a register-stated identifier may equal.
OWNERSHIP_SCHEMES = {"lei": {"lei"}, "gb-coh": {"gb-coh", "company_number"}}


def _norm(scheme: str, value: Any) -> str:
    text = str(value or "").strip()
    if scheme == "website":
        host = (
            urlsplit(text if "://" in text else f"https://{text}").hostname or ""
        ).lower()
        return host.removeprefix("www.")
    return "".join(ch for ch in text.upper() if ch.isalnum()).lstrip("0") or "0"


def _country(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text[:2] if len(text) >= 2 and text[:2].isalpha() else None


def issuers(scheme: str, left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    """``same``, ``different`` or ``unknown`` issuer for two stated identifiers (``global`` when issuer-free)."""
    implied = SCHEMES.get(scheme, {}).get("country")
    if implied is None:
        return "global"
    a = _country(left.get("country")) or implied
    b = _country(right.get("country")) or implied
    return "same" if a == b == implied else "different"


class LobbyingIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.store = LobbyingStore(conn, initialize=initialize, now=now)
        self.service = OwnershipIdentityService(conn, now=now, initialize=initialize)

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str) -> list[dict[str, Any]]:
        """Registrants and declared clients as their latest stated revision by the register's own dates.

        A deregistered entry keeps its last statement; an older export observed later never displaces a newer one.
        """
        rows = self.conn.execute(
            "SELECT entry_id, revision_id FROM (SELECT e.entry_id, r.revision_id, e.register, e.native_id, "
            "row_number() OVER (PARTITION BY e.entry_id ORDER BY r.effective_on DESC, r.revision_no DESC) AS rank "
            "FROM lobbying_entries e JOIN lobbying_revisions r ON r.namespace=e.namespace AND r.entry_id=e.entry_id "
            "WHERE e.namespace=? AND e.entry_kind='registrant' AND r.statement_json IS NOT NULL) "
            "WHERE rank=1 ORDER BY register, native_id",
            [namespace],
        ).fetchall()
        subjects = []
        for entry_id, revision_id in rows:
            revision = self.store.revision(namespace, revision_id)
            statement = revision["statement"]
            side = {
                "register": revision["register"],
                "native_id": revision["native_id"],
                "revision_id": revision_id,
                "export_id": revision["export_id"],
            }
            subjects.append(
                {
                    "kind": "registrant",
                    "entry_id": entry_id,
                    "record_key": revision["record_key"],
                    "entity_id": entry_entity_id(revision["record_key"]),
                    "register": revision["register"],
                    "name": statement.get("name"),
                    "country": statement.get("country"),
                    "address": statement.get("address"),
                    "identifiers": [
                        i
                        for i in statement.get("identifiers") or []
                        if i.get("scheme") in SCHEMES
                    ],
                    "side": side,
                }
            )
            for client in statement.get("clients") or []:
                key = client_key(
                    revision["register"], revision["native_id"], client.get("name")
                )
                subjects.append(
                    {
                        "kind": "client",
                        "entry_id": entry_id,
                        "record_key": key,
                        "entity_id": entry_entity_id(key),
                        "register": revision["register"],
                        "name": client.get("name"),
                        "country": None,
                        "address": None,
                        "identifiers": [
                            i
                            for i in client.get("native_ids") or []
                            if i.get("scheme") in SCHEMES
                        ],
                        "side": {**side, "client": client.get("name")},
                    }
                )
        return subjects

    @staticmethod
    def _primary(subject: Mapping[str, Any], scheme: str) -> bool:
        return (
            subject["kind"] == "registrant"
            and SCHEMES[scheme]["primary"] == subject["register"]
        )

    # ------------------------------------------------------------------ proposals

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        ownership_namespace: str | None = None,
        lei_namespace: str | None = None,
        canonical: bool = True,
    ) -> dict[str, Any]:
        """Propose identifier-based candidates between register records and to ownership, LEI and entity records.

        Idempotent; a stronger basis upgrades a pending weaker candidate and new evidence re-proposes a rejected
        or reverted one (shared state-machine rules). Register records are never edited.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subjects = self.subjects(namespace)
        offered = []
        index: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for subject in subjects:
            for ident in subject["identifiers"]:
                index.setdefault(
                    (ident["scheme"], _norm(ident["scheme"], ident["value"])), []
                ).append((subject, ident))
        for (scheme, _value), holders in sorted(index.items()):
            for i, (left, left_id) in enumerate(holders):
                for right, right_id in holders[i + 1 :]:
                    if left["record_key"] == right["record_key"]:
                        continue
                    relation = issuers(scheme, left_id, right_id)
                    if relation == "different":
                        continue  # the same digits from two issuing countries are two identifiers
                    if scheme == "website":
                        basis = "unqualified-identifier"  # a shared website needs a reviewer to establish identity
                    elif self._primary(left, scheme) or self._primary(right, scheme):
                        basis = "exact-identifier"
                    else:
                        basis = "cross-referenced-identifier"
                    offered.append(
                        self._offer(
                            namespace,
                            left,
                            right["record_key"],
                            right["entity_id"],
                            basis,
                            {
                                "kind": scheme,
                                "value": left_id["value"],
                                "left": left["side"],
                                "right": right["side"],
                                "issuers": relation,
                                "fields": ["identifiers"],
                                "note": "both register records state this identifier; the records stay separate",
                            },
                            principal_id,
                            scopes,
                        )
                    )
        if ownership_namespace:
            offered += self._ownership(
                namespace, ownership_namespace, subjects, principal_id, scopes
            )
        if lei_namespace:
            offered += self._lei(
                namespace, lei_namespace, subjects, principal_id, scopes
            )
        if canonical:
            offered += self._canonical(namespace, subjects, principal_id, scopes)
        return {
            "proposed": sorted(
                {o["candidate_id"] for o in offered if o["created"] or o.get("change")}
            ),
            "candidates": self.candidates(namespace, scopes=scopes),
        }

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

    def _ownership(
        self, namespace, ownership_namespace, subjects, principal_id, scopes
    ):
        from src.kb.entities import normalize_surface
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
            right = {
                "record_key": body["record_key"],
                "record_id": entity["record_id"],
                "revision": entity["revision"],
                "ownership_namespace": ownership_namespace,
            }
            right_entity = body.get("canonical_entity_id") or body["record_key"]
            owned = {
                (i.get("scheme"), _norm("lei", i.get("value")))
                for i in body.get("identifiers") or []
            }
            jurisdiction = _country(body.get("jurisdiction"))
            for subject in subjects:
                matched = False
                for ident in subject["identifiers"]:
                    schemes = OWNERSHIP_SCHEMES.get(ident["scheme"])
                    if not schemes:
                        continue
                    value = _norm("lei", ident["value"])
                    hit = next((s for s in schemes if (s, value) in owned), None)
                    if hit is None:
                        continue
                    implied = SCHEMES[ident["scheme"]]["country"]
                    stated = _country(ident.get("country"))
                    if implied and (
                        (stated and stated != implied)
                        or (jurisdiction and jurisdiction != implied)
                    ):
                        continue  # a company number is only comparable within its issuing register's country
                    matched = True
                    offered.append(
                        self._offer(
                            namespace,
                            subject,
                            body["record_key"],
                            right_entity,
                            "exact-identifier",
                            {
                                "kind": ident["scheme"],
                                "value": ident["value"],
                                "left": subject["side"],
                                "right": {**right, "scheme": hit},
                                "fields": ["identifiers"],
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
                    and subject["name"]
                    and normalize_surface(subject["name"])
                    == normalize_surface(body.get("name") or "")
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
                                "fields": ["name", "country"],
                                "note": "equal normalized names in one country are a weak "
                                "signal, never an identity",
                            },
                            principal_id,
                            scopes,
                        )
                    )
        return offered

    def _lei(self, namespace, lei_namespace, subjects, principal_id, scopes):
        from src.kb.lei import READ_SCOPE as LEI_READ

        if "operator" not in scopes and (
            LEI_READ not in scopes
            or not {
                f"namespace:{lei_namespace}:read",
                f"namespace:{lei_namespace}:write",
            }
            & scopes
        ):
            raise LobbyingError(
                "unauthorized", f"{LEI_READ} and LEI namespace access are required"
            )
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='lei_current'"
        ).fetchone():
            return []
        offered = []
        for subject in subjects:
            for ident in subject["identifiers"]:
                if ident["scheme"] != "lei":
                    continue
                lei = "".join(ch for ch in str(ident["value"]).upper() if ch.isalnum())
                row = self.conn.execute(
                    "SELECT revision_id FROM lei_current WHERE namespace=? AND lei=?",
                    [lei_namespace, lei],
                ).fetchone()
                if row is None:
                    continue
                key = f"gleif:lei:{lei}"
                offered.append(
                    self._offer(
                        namespace,
                        subject,
                        key,
                        entry_entity_id(key),
                        "exact-identifier",
                        {
                            "kind": "lei",
                            "value": ident["value"],
                            "left": subject["side"],
                            "right": {
                                "record_key": key,
                                "lei_namespace": lei_namespace,
                                "lei_revision_id": row[0],
                            },
                            "fields": ["identifiers"],
                        },
                        principal_id,
                        scopes,
                    )
                )
        return offered

    def _canonical(self, namespace, subjects, principal_id, scopes):
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='entity_aliases'"
        ).fetchone():
            return []
        from src.kb.entities import resolve

        offered = []
        for subject in subjects:
            if not subject["name"]:
                continue
            found = resolve(self.conn, subject["name"])
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
                        "fields": ["name"],
                        "note": "a name that resolves to a canonical entity alias; a name alone is never an identity",
                    },
                    principal_id,
                    scopes,
                )
            )
        return offered

    def propose_link(
        self,
        namespace: str,
        record_key: str,
        *,
        target_key: str,
        target_entity: str,
        evidence: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A reviewer-supplied candidate resting on what the register states (identifier, or name plus attributes).

        ``evidence`` names the stated attribute (``kind``/``value``) and where the reviewer saw it on the target
        (``target_source``). An identifier yields ``cross-referenced-identifier``, a name corroborated by the
        stated country or address ``name-jurisdiction``, and a name alone ``similar-name`` (never acceptable).
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subject = next(
            (s for s in self.subjects(namespace) if s["record_key"] == record_key), None
        )
        if subject is None:
            raise LobbyingError(
                "not_found", "register record is not visible in this namespace"
            )
        if not str(evidence.get("target_source") or "").strip():
            raise LobbyingError(
                "invalid_request",
                "cite where the target states the attribute (target_source)",
            )
        kind, value = str(evidence.get("kind") or ""), evidence.get("value")
        if kind in SCHEMES:
            stated = [
                i
                for i in subject["identifiers"]
                if i["scheme"] == kind and _norm(kind, i["value"]) == _norm(kind, value)
            ]
            if not stated:
                raise LobbyingError(
                    "not_stated", "the register revision does not state this identifier"
                )
            basis, corroborated = "cross-referenced-identifier", []
        elif kind == "name":
            if normalize_name(value) != normalize_name(subject["name"]):
                raise LobbyingError(
                    "not_stated", "the register revision does not state this name"
                )
            corroborated = []
            for attribute in evidence.get("attributes") or []:
                if (
                    attribute.get("kind") == "country"
                    and _country(attribute.get("value")) == _country(subject["country"])
                    and subject["country"]
                ):
                    corroborated.append(dict(attribute))
                if (
                    attribute.get("kind") == "address"
                    and subject["address"]
                    and normalize_name(attribute.get("value"))
                    == normalize_name((subject["address"] or {}).get("text"))
                ):
                    corroborated.append(dict(attribute))
            basis = "name-jurisdiction" if corroborated else "similar-name"
        else:
            raise LobbyingError(
                "invalid_request",
                "evidence names an identifier scheme or the stated name",
            )
        return self._offer(
            namespace,
            subject,
            target_key,
            target_entity,
            basis,
            {
                "kind": kind,
                "value": value,
                "left": subject["side"],
                "right": {
                    "record_key": target_key,
                    "entity_id": target_entity,
                    "target_source": evidence["target_source"],
                },
                "corroborating_attributes": corroborated,
                "fields": [kind] + sorted({a["kind"] for a in corroborated}),
            },
            principal_id,
            scopes,
        )

    # ------------------------------------------------------------------ reads

    def candidates(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        record_key: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = [
            c
            for c in self.service.candidates(
                namespace, scopes=scopes, record_key=record_key
            )
            if c["left_key"].startswith("lobbying:")
            or c["right_key"].startswith("lobbying:")
        ]
        return [self.view(c) for c in rows]

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        return {
            "candidate_id": candidate["candidate_id"],
            "state": candidate["state"],
            "review_state": {
                "accepted": "reviewed-match",
                "rejected": "reviewed-non-match",
                "reverted": "reverted",
                "proposed": "unreviewed-candidate",
            }[candidate["state"]],
            "basis": candidate["basis"],
            "confidence": candidate["confidence"],
            "records": [candidate["left_key"], candidate["right_key"]],
            "entities": [candidate["left_entity"], candidate["right_entity"]],
            "decision_id": candidate["decision_id"],
            "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
            "reason": last.get("reason"),
            "reviewed_at_ms": last.get("at_ms")
            if candidate["state"] != "proposed"
            else None,
            "evidence": candidate["evidence"],
            "register_revisions": sorted(
                {
                    side["revision_id"]
                    for e in candidate["evidence"]
                    for side in (e.get("left"), e.get("right"))
                    if isinstance(side, Mapping) and side.get("revision_id")
                }
            ),
            "history": candidate["history"],
            "notice": "a reviewable assertion; register records are never merged or edited",
        }

    def identity(
        self, namespace: str, record_key: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Accepted links and open candidates for one register record; without an accepted link it is unmatched."""
        try:
            views = self.candidates(namespace, scopes=scopes, record_key=record_key)
        except Exception as exc:  # noqa: BLE001 - identity access is optional for an answer
            return {
                "state": "unmatched",
                "links": [],
                "candidates": [],
                "unavailable": [str(getattr(exc, "code", "unavailable"))],
            }
        links = [v for v in views if v["state"] == "accepted"]
        return {
            "state": "matched" if links else "unmatched",
            "links": links,
            "candidates": [v for v in views if v["state"] == "proposed"],
        }

    def source_identity_candidates(
        self,
        namespace: str,
        entry_id: str,
        *,
        source_namespace: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Candidate OSINT source identities for a registrant with a declared website, for that store's alias review.

        Nothing is written here: a reviewer links the registrant key as an ``identifier`` alias of the source
        through ``decide_source_alias`` (and splits it to revert). Linked aliases are returned as ``links``.
        """
        from src.kb.source_identity import READ_SCOPE as SOURCE_READ
        from src.kb.source_identity import SourceIdentityStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if SOURCE_READ not in scopes and "operator" not in scopes:
            raise LobbyingError("unauthorized", f"{SOURCE_READ} is required")
        entry = self.store.entry(namespace, entry_id)
        subject = next(
            (
                s
                for s in self.subjects(namespace)
                if s["record_key"] == entry["record_key"]
            ),
            None,
        )
        if subject is None:
            return {"record_key": entry["record_key"], "candidates": [], "links": []}
        sources = SourceIdentityStore(self.conn, initialize=False)
        candidates = []
        for ident in subject["identifiers"]:
            if ident["scheme"] != "website":
                continue
            resolved = sources.resolve_alias(
                source_namespace, "domain", ident["value"], scopes={SOURCE_READ}
            )
            for match in resolved["matches"]:
                candidates.append(
                    {
                        "source_id": match["source_id"],
                        "basis": "declared website equals a linked source domain",
                        "evidence": {
                            "website": ident["value"],
                            "register_revision": subject["side"]["revision_id"],
                            "domain_decision_id": match["decision_id"],
                        },
                        "review_operation": {
                            "tool": "decide_source_alias",
                            "namespace": source_namespace,
                            "source_id": match["source_id"],
                            "alias_type": "identifier",
                            "value": entry["record_key"],
                            "action": "link",
                        },
                        "state": "candidate",
                    }
                )
        linked = sources.resolve_alias(
            source_namespace, "identifier", entry["record_key"], scopes={SOURCE_READ}
        )
        return {
            "record_key": entry["record_key"],
            "candidates": candidates,
            "links": linked["matches"],
        }
