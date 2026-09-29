"""Issuers, notifiers, holders, short sellers and managers through reviewable identity (#2106, BF07).

* **Issuers** resolve deterministically: by ISIN to a ``market.core`` security and
  its issuer as of the notice date (:class:`MarketInstrumentStore`), and by LEI to
  a ``market.lei`` record when the notice states one. A failure stays
  ``unresolved`` with its reason; nothing is guessed.
* **Parties** named in notices (notifiers, chain members, short sellers, managers,
  entity strings in warnings and measures, authorised entities) are *source
  strings*. They become candidates in the shared reviewable state machine
  (:class:`src.kb.ownership_identity.OwnershipIdentityService`) with their
  evidence (LEI, BaFin ID, name + seat country), and stay ``proposed`` until a
  reviewer decides. Accepting or reverting is an entity identity decision; no
  notice or ownership record is ever rewritten.
* **Natural persons** (managers, and notifiers or holders the source marks as
  natural persons) are keyed within their issuer. No candidate is ever proposed
  automatically for a person, and a person candidate across issuers exists only
  when a reviewer proposes it with evidence, and even then it stays ``proposed``
  until reviewed.

One equivalence (:func:`src.domains.market.bafin_notices.party_key`) is used by
candidate generation, lookups, queries and monitors.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.domains.market.bafin_notices import (
    READ_SCOPE,
    BafinError,
    BafinNoticeStore,
    authorize,
    digest,
    end_of_day_ms,
    fold,
    issuer_key,
    normalize_bafin_id,
    normalize_isin,
    normalize_lei,
    party_key,
)

PREFIX = "bafin:"
COUNTRIES = {
    "deutschland": "DE",
    "germany": "DE",
    "de": "DE",
    "luxemburg": "LU",
    "luxembourg": "LU",
    "lu": "LU",
    "frankreich": "FR",
    "france": "FR",
    "fr": "FR",
    "vereinigtes koenigreich": "GB",
    "united kingdom": "GB",
    "grossbritannien": "GB",
    "gb": "GB",
    "uk": "GB",
    "vereinigte staaten": "US",
    "usa": "US",
    "us": "US",
    "niederlande": "NL",
    "netherlands": "NL",
    "nl": "NL",
    "schweiz": "CH",
    "switzerland": "CH",
    "ch": "CH",
    "irland": "IE",
    "ireland": "IE",
    "ie": "IE",
    "oesterreich": "AT",
    "austria": "AT",
    "at": "AT",
}


def country_code(value: Any) -> str | None:
    text = fold(value).strip()
    if not text:
        return None
    return COUNTRIES.get(text) or (
        text.upper() if len(text) == 2 and text.isalpha() else None
    )


def organisation_key(name: Any) -> str:
    return f"{PREFIX}party:{party_key(name)}"


def person_key(issuer: str, name: Any) -> str:
    """A natural person is keyed within the issuer context only."""
    return (
        f"{PREFIX}person:"
        + digest([issuer, party_key(name, kind="natural_person")])[:20]
    )


def named_key(name: Any) -> str:
    return f"{PREFIX}named:{party_key(name)}"


def authorised_key(bafin_id: Any) -> str:
    return f"{PREFIX}authorised:{normalize_bafin_id(bafin_id)}"


def _entity_id(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def resolve_issuer(
    conn: Any,
    notice_issuer: Mapping[str, Any],
    *,
    on: str | None,
    market_namespace: str | None,
    principal_id: str,
    scopes: Iterable[str],
    acquired_by_ms: int | None = None,
    lei_namespace: str | None = None,
) -> dict[str, Any]:
    """An issuer by ISIN (as of ``on``, a notice date) and, when stated, by LEI; failures stay unresolved."""
    scopes = set(scopes)
    isin = (
        normalize_isin(notice_issuer.get("isin")) if notice_issuer.get("isin") else None
    )
    result: dict[str, Any] = {"isin": isin, "name": notice_issuer.get("name"), "on": on}
    if market_namespace is None:
        result["instrument"] = {
            "status": "not_requested",
            "reason": "no market namespace was given",
        }
    elif not isin:
        result["instrument"] = {
            "status": "unresolved",
            "reason": "the notice states no valid ISIN",
        }
    elif not conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name="
        "'market_instrument_object_revisions'"
    ).fetchone():
        result["instrument"] = {
            "status": "unresolved",
            "reason": "the market instrument master has not been acquired",
        }
    else:
        from src.domains.market.instruments import (
            MarketInstrumentError,
            MarketInstrumentStore,
        )

        at = end_of_day_ms(on) if on else None
        acquired = acquired_by_ms if acquired_by_ms is not None else at
        try:
            if at is None or acquired is None:
                raise MarketInstrumentError(
                    "invalid_request", "a notice date is needed to resolve as of"
                )
            resolved = MarketInstrumentStore(conn, initialize=False).resolve_identifier(
                market_namespace,
                "isin",
                isin,
                object_type="security",
                as_of_ms=min(at, acquired),
                acquired_by_ms=acquired,
                principal_id=principal_id,
                scopes=scopes,
            )
        except MarketInstrumentError as exc:
            result["instrument"] = {"status": "unresolved", "reason": exc.code}
        else:
            candidates = resolved.get("candidates") or []
            if resolved.get("status") == "resolved" and len(candidates) == 1:
                security, issuer = candidates[0]["security"], candidates[0]["issuer"]
                result["instrument"] = {
                    "status": "resolved",
                    "basis": "isin",
                    "security_id": security.get("security_id"),
                    "issuer_id": issuer.get("issuer_id"),
                    "issuer_name": issuer.get("display_name"),
                    "revision_ids": [
                        security.get("revision_id"),
                        issuer.get("revision_id"),
                    ],
                }
            else:
                result["instrument"] = {
                    "status": "unresolved",
                    "reason": "ambiguous"
                    if len(candidates) > 1
                    else "no instrument for the ISIN as of the notice date",
                }
    lei = normalize_lei(notice_issuer.get("lei")) if notice_issuer.get("lei") else None
    if lei and lei_namespace:
        from src.kb.lei import LeiError, LeiStore

        if not conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='lei_current'"
        ).fetchone():
            result["lei"] = {
                "status": "unresolved",
                "lei": lei,
                "reason": "LEI records have not been acquired",
            }
        else:
            try:
                entity = LeiStore(conn, initialize=False).entity(
                    lei_namespace, lei, scopes=scopes
                )
                result["lei"] = {
                    "status": "resolved",
                    "lei": lei,
                    "legal_name": entity["legal_name"],
                    "revision_id": entity["revision_id"],
                }
            except LeiError as exc:
                result["lei"] = {"status": "unresolved", "lei": lei, "reason": exc.code}
    elif lei:
        result["lei"] = {
            "status": "stated",
            "lei": lei,
            "note": "no LEI namespace given; the LEI is as stated",
        }
    return result


class BafinIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.store = BafinNoticeStore(conn, initialize=False, now=now)
        self.service = OwnershipIdentityService(conn, now=now, initialize=initialize)

    # -------------------------------------------------------------- parties

    def parties(self, namespace: str) -> dict[str, dict[str, Any]]:
        """Every party the current notices name, keyed by its record key, with the evidence each states."""
        views = self.store.visible(namespace)["notices"]
        parties: dict[str, dict[str, Any]] = {}

        def add(
            key: str,
            kind: str,
            name: str | None,
            *,
            notice: Mapping[str, Any],
            role: str,
            **evidence: Any,
        ) -> None:
            entry = parties.setdefault(
                key,
                {
                    "record_key": key,
                    "kind": kind,
                    "names": set(),
                    "roles": set(),
                    "issuers": set(),
                    "leis": set(),
                    "countries": set(),
                    "bafin_ids": set(),
                    "revisions": set(),
                },
            )
            if name:
                entry["names"].add(name)
            entry["roles"].add(role)
            issuer = notice.get("issuer") or {}
            if issuer:
                entry["issuers"].add(issuer_key(issuer))
            for field, target in (
                ("lei", "leis"),
                ("country", "countries"),
                ("bafin_id", "bafin_ids"),
            ):
                value = evidence.get(field)
                if value:
                    entry[target].add(value)
            entry["revisions"].add(evidence["revision_id"])

        for view in views:
            notice, revision_id = view["notice"], view["revision_id"]
            kind = notice["kind"]
            issuer = (
                issuer_key(notice.get("issuer") or {}) if notice.get("issuer") else None
            )
            if kind == "voting_rights_notification":
                notifier = notice["notifier"]
                natural = notifier.get("kind") == "natural_person"
                key = (
                    person_key(issuer, notifier["name"])
                    if natural
                    else organisation_key(notifier["name"])
                )
                add(
                    key,
                    "natural_person" if natural else "organisation",
                    notifier["name"],
                    notice=notice,
                    role="notifier",
                    country=country_code(notifier.get("country")),
                    revision_id=revision_id,
                )
                for member in notice.get("chain") or []:
                    same = party_key(member["name"]) == party_key(notifier["name"])
                    member_key = key if same else organisation_key(member["name"])
                    add(
                        member_key,
                        "natural_person" if same and natural else "organisation",
                        member["name"],
                        notice=notice,
                        role="chain member",
                        revision_id=revision_id,
                    )
            elif kind == "net_short_position":
                holder = notice["holder"]
                natural = holder.get("kind") == "natural_person"
                key = (
                    person_key(issuer, holder["name"])
                    if natural
                    else organisation_key(holder["name"])
                )
                add(
                    key,
                    "natural_person" if natural else "organisation",
                    holder["name"],
                    notice=notice,
                    role="short seller",
                    revision_id=revision_id,
                )
            elif kind == "managers_transaction":
                person = notice["person"]
                if person.get("withdrawn") or not person.get("name"):
                    continue  # withdrawn person data is never a candidate
                natural = person.get("kind") != "legal_person"
                key = (
                    person_key(issuer, person["name"])
                    if natural
                    else organisation_key(person["name"])
                )
                add(
                    key,
                    "natural_person" if natural else "organisation",
                    person["name"],
                    notice=notice,
                    role="manager" if natural else "closely associated legal person",
                    revision_id=revision_id,
                )
            elif kind in {"bafin_warning", "bafin_measure"}:
                for name in notice.get("named_entities") or []:
                    add(
                        named_key(name),
                        "named_string",
                        name,
                        notice=notice,
                        role="named in a warning"
                        if kind == "bafin_warning"
                        else "named in a measure",
                        revision_id=revision_id,
                    )
            elif kind == "authorised_entity":
                add(
                    authorised_key(notice["bafin_id"]),
                    "authorised_entity",
                    notice["name"],
                    notice=notice,
                    role="authorised entity",
                    lei=notice.get("lei"),
                    country=country_code(notice.get("country")),
                    bafin_id=notice["bafin_id"],
                    revision_id=revision_id,
                )
        return {
            key: {k: sorted(v) if isinstance(v, set) else v for k, v in entry.items()}
            for key, entry in sorted(parties.items())
        }

    # -------------------------------------------------------------- proposals

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        ownership_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Offer identifier and name + country candidates; persons are never proposed automatically. Idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        parties = self.parties(namespace)
        offered = []

        def offer(
            left: Mapping[str, Any],
            right_key: str,
            right_entity: str,
            basis: str,
            evidence: Mapping[str, Any],
        ) -> None:
            offered.append(
                self.service.offer(
                    namespace,
                    left_key=left["record_key"],
                    right_key=right_key,
                    left_entity=_entity_id(left["record_key"]),
                    right_entity=right_entity,
                    basis=basis,
                    evidence=[
                        {
                            **evidence,
                            "left": {
                                "record_key": left["record_key"],
                                "names": left["names"],
                                "revisions": left["revisions"],
                            },
                        }
                    ],
                    principal_id=principal_id,
                    scopes=scopes,
                )
            )

        organisations = [
            p
            for p in parties.values()
            if p["kind"] in {"organisation", "authorised_entity"}
        ]
        # Among BaFin parties: an organisation named in notices and an authorised entity.
        for party in organisations:
            if party["kind"] != "organisation":
                continue
            for other in organisations:
                if other["kind"] != "authorised_entity":
                    continue
                shared_lei = sorted(set(party["leis"]) & set(other["leis"]))
                same_name = {party_key(n) for n in party["names"]} & {
                    party_key(n) for n in other["names"]
                }
                countries = set(party["countries"]) & set(other["countries"])
                if shared_lei:
                    offer(
                        party,
                        other["record_key"],
                        _entity_id(other["record_key"]),
                        "exact-identifier",
                        {"kind": "lei", "value": shared_lei[0], "fields": ["lei"]},
                    )
                elif same_name and countries:
                    offer(
                        party,
                        other["record_key"],
                        _entity_id(other["record_key"]),
                        "name-jurisdiction",
                        {
                            "kind": "name-country",
                            "names": sorted(same_name),
                            "country": sorted(countries)[0],
                            "fields": ["name", "country"],
                            "note": "equal names in one country are a weak signal, never an identity",
                        },
                    )
                elif same_name:
                    offer(
                        party,
                        other["record_key"],
                        _entity_id(other["record_key"]),
                        "similar-name",
                        {
                            "kind": "name",
                            "names": sorted(same_name),
                            "fields": ["name"],
                            "note": "a name alone never becomes an accepted match",
                        },
                    )
        # A string a warning or measure names is only ever a similar-name proposal on its own.
        for named in (p for p in parties.values() if p["kind"] == "named_string"):
            for other in organisations:
                if {party_key(n) for n in named["names"]} & {
                    party_key(n) for n in other["names"]
                }:
                    offer(
                        named,
                        other["record_key"],
                        _entity_id(other["record_key"]),
                        "similar-name",
                        {
                            "kind": "name",
                            "names": named["names"],
                            "fields": ["name"],
                            "note": "a warning names a string; it is never attributed without a reviewed match",
                        },
                    )
        if ownership_namespace:
            offered += self._ownership(
                namespace, ownership_namespace, parties, principal_id, scopes
            )
        return {
            "proposed": sorted(
                {o["candidate_id"] for o in offered if o["created"] or o.get("change")}
            ),
            "candidates": self.candidates(namespace, scopes=scopes),
            "parties": len(parties),
            "notice": "candidates stay proposed until reviewed; natural persons are never proposed automatically",
        }

    def _ownership(
        self, namespace, ownership_namespace, parties, principal_id, scopes
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
            and not str(e["record"]["record_key"]).startswith("bafin-issuer:")
        ]
        offered = []
        for party in parties.values():
            if party["kind"] not in {
                "organisation",
                "authorised_entity",
                "named_string",
            }:
                continue
            names = {party_key(n) for n in party["names"]}
            for entity in entities:
                body = entity["record"]
                leis = {
                    normalize_lei(i["value"])
                    for i in body.get("identifiers") or []
                    if i.get("scheme") == "lei"
                }
                bafin_ids = {
                    normalize_bafin_id(i["value"])
                    for i in body.get("identifiers") or []
                    if i.get("scheme") == "bafin-id"
                }
                country = country_code(
                    str(body.get("jurisdiction") or "").split("-")[0]
                )
                target = {
                    "record_key": body["record_key"],
                    "record_id": entity["record_id"],
                    "revision": entity["revision"],
                    "ownership_namespace": ownership_namespace,
                }
                shared_lei = sorted(set(party["leis"]) & leis)
                shared_id = sorted(set(party["bafin_ids"]) & bafin_ids)
                same_name = party_key(body.get("name")) in names
                if party["kind"] != "named_string" and (shared_lei or shared_id):
                    basis, evidence = (
                        "exact-identifier",
                        {
                            "kind": "lei" if shared_lei else "bafin-id",
                            "value": (shared_lei or shared_id)[0],
                            "fields": ["identifiers"],
                        },
                    )
                elif (
                    party["kind"] != "named_string"
                    and same_name
                    and country
                    and country in party["countries"]
                ):
                    basis, evidence = (
                        "name-jurisdiction",
                        {
                            "kind": "name-country",
                            "name": body.get("name"),
                            "country": country,
                            "fields": ["name", "jurisdiction"],
                        },
                    )
                elif same_name:
                    basis, evidence = (
                        "similar-name",
                        {"kind": "name", "name": body.get("name"), "fields": ["name"]},
                    )
                else:
                    continue
                offered.append(
                    self.service.offer(
                        namespace,
                        left_key=party["record_key"],
                        right_key=body["record_key"],
                        left_entity=_entity_id(party["record_key"]),
                        right_entity=body.get("canonical_entity_id")
                        or body["record_key"],
                        basis=basis,
                        evidence=[
                            {
                                **evidence,
                                "left": {
                                    "record_key": party["record_key"],
                                    "names": party["names"],
                                    "revisions": party["revisions"],
                                },
                                "right": target,
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
        party: str,
        *,
        target_key: str,
        target_entity: str | None,
        evidence: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A reviewer-supplied candidate for one party, resting on what a notice states.

        ``evidence`` names the attribute (``kind``: ``lei``, ``bafin-id`` or
        ``name``) and where the reviewer saw it on the target
        (``target_source``). A person candidate whose target is another
        issuer's person is flagged ``cross_issuer`` and, like every candidate,
        stays ``proposed`` until reviewed.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        parties = self.parties(namespace)
        if party not in parties:
            raise BafinError(
                "not_found",
                "the party is not named by any current notice in this namespace",
            )
        if not str(evidence.get("target_source") or "").strip():
            raise BafinError(
                "invalid_request",
                "cite where the target states the attribute (target_source)",
            )
        entry = parties[party]
        kind = str(evidence.get("kind") or "")
        value = str(evidence.get("value") or "")
        if kind == "lei" and normalize_lei(value) in entry["leis"]:
            basis = "cross-referenced-identifier"
        elif kind == "bafin-id" and normalize_bafin_id(value) in entry["bafin_ids"]:
            basis = "cross-referenced-identifier"
        elif kind == "name" and party_key(
            value,
            kind="natural_person" if entry["kind"] == "natural_person" else "unknown",
        ) in {
            party_key(
                n,
                kind="natural_person"
                if entry["kind"] == "natural_person"
                else "unknown",
            )
            for n in entry["names"]
        }:
            basis = (
                "name-jurisdiction" if evidence.get("attributes") else "similar-name"
            )
        else:
            raise BafinError(
                "not_stated",
                "the notices do not state this attribute for the party; candidates rest "
                "only on what a notice states",
            )
        target_party = parties.get(target_key)
        cross_issuer = bool(
            entry["kind"] == "natural_person"
            and target_party
            and set(target_party["issuers"]) != set(entry["issuers"])
        )
        return self.service.offer(
            namespace,
            left_key=party,
            right_key=target_key,
            left_entity=_entity_id(party),
            right_entity=target_entity or _entity_id(target_key),
            basis=basis,
            evidence=[
                {
                    "kind": kind,
                    "value": value,
                    "left": {"record_key": party, "revisions": entry["revisions"]},
                    "right": {
                        "record_key": target_key,
                        "target_source": evidence["target_source"],
                    },
                    "attributes": list(evidence.get("attributes") or []),
                    "cross_issuer": cross_issuer,
                    "fields": [kind],
                    "proposed_by": "reviewer",
                }
            ],
            principal_id=principal_id,
            scopes=scopes,
        )

    # -------------------------------------------------------------- reads

    def candidates(
        self, namespace: str, *, scopes: Iterable[str], party: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            c
            for c in self.service.candidates(namespace, scopes=scopes, record_key=party)
            if c["left_key"].startswith(PREFIX) or c["right_key"].startswith(PREFIX)
        ]
        return [
            {
                "candidate_id": c["candidate_id"],
                "state": c["state"],
                "basis": c["basis"],
                "confidence": c["confidence"],
                "records": [c["left_key"], c["right_key"]],
                "entities": [c["left_entity"], c["right_entity"]],
                "decision_id": c["decision_id"],
                "evidence": c["evidence"],
                "history": c["history"],
                "notice": "a reviewable proposal; notices and ownership records are never merged or edited",
            }
            for c in rows
        ]

    def accepted_target(self, namespace: str, party: str) -> dict[str, Any] | None:
        """The one non-BaFin record an accepted, unreverted candidate links a party to (else None)."""
        if not self.service._ready():
            return None
        rows = self.conn.execute(
            "SELECT candidate_id, left_key, right_key FROM ownership_identity_candidates WHERE namespace=? "
            "AND state='accepted' AND (left_key=? OR right_key=?) ORDER BY candidate_id",
            [namespace, party, party],
        ).fetchall()
        targets = {}
        for candidate_id, left, right in rows:
            other = right if left == party else left
            if not other.startswith(PREFIX):
                targets.setdefault(other, candidate_id)
        if len(targets) != 1:
            return None
        key, candidate_id = next(iter(targets.items()))
        return {"record_key": key, "candidate_id": candidate_id}

    def accepted_links(self, namespace: str) -> dict[str, list[str]]:
        """party -> the other record keys of its accepted candidates (both directions)."""
        if not self.service._ready():
            return {}
        rows = self.conn.execute(
            "SELECT left_key, right_key FROM ownership_identity_candidates WHERE namespace=? AND state='accepted'",
            [namespace],
        ).fetchall()
        links: dict[str, set[str]] = {}
        for left, right in rows:
            links.setdefault(left, set()).add(right)
            links.setdefault(right, set()).add(left)
        return {k: sorted(v) for k, v in links.items()}
