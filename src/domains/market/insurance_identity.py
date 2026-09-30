"""Insurers matched to LEI and ownership entities through reviewable identity (#2230, IN07).

* **Published identifiers match exactly.** An LEI or NAIC company code that a record
  publishes matches the LEI record (:class:`src.kb.lei.LeiStore`) or an ownership
  entity that carries the same identifier. This is deterministic: nothing is guessed
  and nothing needs review.
* **Names are candidates.** A name + jurisdiction or name-only match against an
  ownership entity becomes a candidate in the shared reviewable state machine
  (:class:`src.kb.ownership_identity.OwnershipIdentityService`). It stays
  ``proposed`` until a reviewer accepts it, and a similar name alone can never be
  accepted. Rejected and reverted candidates are never used.
* **Group and solo stay apart.** An insurer's key includes its reporting level, so a
  group report and a solo report of the same legal entity are separate parties. A
  group report is shown beside a subsidiary only through a recorded ownership link:
  the GLEIF parent relationship observed by the query's cutoff. It is labelled as
  the group's report and is never counted as the subsidiary's.
* **Unmatched insurers stay queryable** by their source key, for example
  ``insurance:insurer:name:<name>|<country>|<level>``.

No new entity store: candidates live in ``ownership_identity_candidates`` under the
insurance namespace, and records are never merged or edited.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.domains.market.insurance import (
    READ_SCOPE,
    InsuranceError,
    InsuranceStore,
    authorize,
    fold,
    lei_valid,
    normalize_lei,
    normalize_naic,
)

PREFIX = "insurance:"
IDENTITY_NOTICE = (
    "published LEI or NAIC codes match exactly; name matches are reviewable candidates and a similar name alone is "
    "never an identity; group and solo reporting stay distinct"
)


def insurer_key(insurer: Mapping[str, Any]) -> str:
    level = insurer.get("reporting_level") or "unknown"
    if insurer.get("lei"):
        return f"{PREFIX}insurer:lei:{normalize_lei(insurer['lei'])}:{level}"
    if insurer.get("naic_company_code"):
        return f"{PREFIX}insurer:naic:{normalize_naic(insurer['naic_company_code'])}:{level}"
    return f"{PREFIX}insurer:name:{fold(insurer.get('name'))}|{insurer.get('country') or ''}|{level}"


def _entity_id(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def _table(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


class InsuranceIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.store = InsuranceStore(conn, initialize=False, now=now)
        self.service = OwnershipIdentityService(conn, now=now, initialize=initialize)

    insurer_key = staticmethod(insurer_key)

    # -------------------------------------------------------------- parties

    def parties(self, namespace: str) -> dict[str, dict[str, Any]]:
        """Every insurer that current records name, keyed by its record key, with what each record states."""
        parties: dict[str, dict[str, Any]] = {}
        for view in self.store.visible(namespace):
            insurer = view["record"].get("insurer")
            if not insurer:
                continue
            key = insurer_key(insurer)
            entry = parties.setdefault(
                key,
                {
                    "record_key": key,
                    "names": set(),
                    "lei": insurer.get("lei"),
                    "naic_company_code": insurer.get("naic_company_code"),
                    "naic_group_code": insurer.get("naic_group_code"),
                    "country": insurer.get("country"),
                    "reporting_level": insurer.get("reporting_level"),
                    "providers": set(),
                    "revisions": [],
                },
            )
            entry["names"].add(insurer["name"])
            entry["providers"].add(view["record"]["source"]["provider"])
            entry["revisions"].append(view["revision_id"])
        for entry in parties.values():
            entry["names"] = sorted(entry["names"])
            entry["providers"] = sorted(entry["providers"])
            entry["revisions"] = sorted(entry["revisions"])
        return parties

    def _ownership_entities(self, ownership_namespace: str, principal_id: str, scopes: set[str]) -> list[dict]:
        if not _table(self.conn, "ownership_records"):
            return []
        from src.kb.ownership_store import OwnershipStore

        return [
            e
            for e in OwnershipStore(self.conn, initialize=False).records(
                ownership_namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",)
            )
            if not e.get("redacted")
        ]

    # -------------------------------------------------------------- exact matches

    def exact_matches(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        lei_namespace: str | None = None,
        ownership_namespace: str | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """party -> deterministic matches on a published LEI or NAIC code (no review needed, nothing guessed)."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        parties = self.parties(namespace)
        result: dict[str, list[dict[str, Any]]] = {key: [] for key in parties}
        if lei_namespace and _table(self.conn, "lei_current"):
            from src.kb.lei import LeiError, LeiStore

            leis = LeiStore(self.conn, initialize=False)
            for key, party in parties.items():
                if not party["lei"]:
                    continue
                try:
                    entity = leis.entity(lei_namespace, party["lei"], scopes=scopes)
                except LeiError as exc:
                    if exc.code == "unauthorized":
                        raise InsuranceError("unauthorized", "knowledge:companies:read and LEI namespace access") from exc
                    continue
                result[key].append(
                    {
                        "target": f"lei:{party['lei']}",
                        "kind": "lei-record",
                        "basis": "exact-identifier",
                        "identifier": {"scheme": "lei", "value": party["lei"]},
                        "legal_name": entity["legal_name"],
                        "revision_id": entity["revision_id"],
                    }
                )
        if ownership_namespace:
            for entity in self._ownership_entities(ownership_namespace, principal_id, scopes):
                body = entity["record"]
                identifiers = {(i.get("scheme"), str(i.get("value") or "")) for i in body.get("identifiers") or []}
                for key, party in parties.items():
                    shared = None
                    if party["lei"] and ("lei", party["lei"]) in {(s, normalize_lei(v)) for s, v in identifiers}:
                        shared = {"scheme": "lei", "value": party["lei"]}
                    elif party["naic_company_code"] and ("naic", party["naic_company_code"]) in {
                        (s, normalize_naic(v)) for s, v in identifiers
                    }:
                        shared = {"scheme": "naic", "value": party["naic_company_code"]}
                    if shared:
                        result[key].append(
                            {
                                "target": body["record_key"],
                                "kind": "ownership-entity",
                                "basis": "exact-identifier",
                                "identifier": shared,
                                "ownership_namespace": ownership_namespace,
                                "record_id": entity["record_id"],
                                "revision": entity["revision"],
                            }
                        )
        return result

    # -------------------------------------------------------------- candidates

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        ownership_namespace: str,
    ) -> dict[str, Any]:
        """Offer name + jurisdiction (and name-only) candidates for parties without an exact match. Idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        parties = self.parties(namespace)
        exact = self.exact_matches(
            namespace, principal_id=principal_id, scopes=scopes, ownership_namespace=ownership_namespace
        )
        offered = []
        for entity in self._ownership_entities(ownership_namespace, principal_id, scopes):
            body = entity["record"]
            country = str(body.get("jurisdiction") or "").split("-")[0].upper() or None
            for key, party in parties.items():
                if any(m["target"] == body["record_key"] for m in exact.get(key, [])):
                    continue  # already an exact, deterministic match
                if fold(body.get("name")) not in {fold(n) for n in party["names"]}:
                    continue
                stated_country = (party["country"] or "").split("-")[0].upper() or None
                if country and stated_country and country != stated_country:
                    continue
                basis = "name-jurisdiction" if country and stated_country else "similar-name"
                offered.append(
                    self.service.offer(
                        namespace,
                        left_key=key,
                        right_key=body["record_key"],
                        left_entity=_entity_id(key),
                        right_entity=body.get("canonical_entity_id") or body["record_key"],
                        basis=basis,
                        evidence=[
                            {
                                "kind": "name-country" if basis == "name-jurisdiction" else "name",
                                "names": party["names"],
                                "country": country,
                                "fields": ["name", "jurisdiction"] if basis == "name-jurisdiction" else ["name"],
                                "left": {"record_key": key, "revisions": party["revisions"],
                                         "reporting_level": party["reporting_level"]},
                                "right": {"record_key": body["record_key"], "record_id": entity["record_id"],
                                          "revision": entity["revision"], "ownership_namespace": ownership_namespace},
                                "note": "a name match is a candidate for review, never an identity by itself",
                            }
                        ],
                        principal_id=principal_id,
                        scopes=scopes,
                    )
                )
        return {
            "proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
            "candidates": self.candidates(namespace, scopes=scopes),
            "parties": len(parties),
            "notice": IDENTITY_NOTICE,
        }

    def candidates(self, namespace: str, *, scopes: Iterable[str], party: str | None = None) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": c["candidate_id"],
                "state": c["state"],
                "basis": c["basis"],
                "confidence": c["confidence"],
                "records": [c["left_key"], c["right_key"]],
                "decision_id": c["decision_id"],
                "evidence": c["evidence"],
                "history": c["history"],
            }
            for c in self.service.candidates(namespace, scopes=scopes, record_key=party)
            if c["left_key"].startswith(PREFIX) or c["right_key"].startswith(PREFIX)
        ]

    def _own(self, namespace: str, candidate_id: str, scopes: set[str]) -> None:
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise InsuranceError("not_found", "not an insurance identity candidate in this namespace")

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        self._own(namespace, candidate_id, scopes)
        return self.service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                                   scopes=scopes)

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        self._own(namespace, candidate_id, scopes)
        return self.service.revert(namespace, candidate_id, reason, principal_id=principal_id, scopes=scopes)

    def accepted(self, namespace: str) -> dict[str, list[str]]:
        """insurance party -> the other record keys of its accepted, unreverted candidates."""
        if not _table(self.conn, "ownership_identity_candidates"):
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

    # -------------------------------------------------------------- resolution

    def resolve(self, namespace: str, insurer: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """The insurance parties an insurer query names: exact on a published identifier, else an accepted match.

        ``insurer`` is an LEI, a five-digit NAIC company code, an insurance party key, or another record key
        (e.g. an ownership entity) that a reviewer accepted as the same insurer. A plain name resolves nothing:
        it lists the parties publishing that name so a reviewer can propose a match.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        text = str(insurer or "").strip()
        parties = self.parties(namespace) if self.store.ready() else {}
        accepted = self.accepted(namespace)
        keys: list[str] = []
        basis = None
        lei = normalize_lei(text)
        if lei_valid(lei):
            keys = [k for k, p in parties.items() if p["lei"] == lei]
            basis = "published-lei"
        elif re.fullmatch(r"\d{5}", text):
            keys = [k for k, p in parties.items() if p["naic_company_code"] == text]
            basis = "published-naic-code"
        elif text in parties:
            keys, basis = [text], "source-key"
        else:
            keys = sorted(k for k in accepted.get(text, []) if k.startswith(PREFIX) and k in parties)
            basis = "accepted-match" if keys else None
        # Accepted matches from a resolved party to other insurance parties join the answer, rejected ones never.
        for key in list(keys):
            for other in accepted.get(key, []):
                if other.startswith(PREFIX) and other in parties and other not in keys:
                    keys.append(other)
        named = sorted(k for k, p in parties.items() if fold(text) in {fold(n) for n in p["names"]})
        return {
            "query": text,
            "status": "resolved" if keys else "unmatched",
            "basis": basis,
            "record_keys": sorted(keys),
            "parties": [
                {field: value for field, value in parties[key].items() if field != "revisions"}
                for key in sorted(keys)
            ],
            "levels": sorted({parties[k]["reporting_level"] for k in keys}),
            "same_name_parties": [] if keys else named,
            "notice": IDENTITY_NOTICE,
        }

    def unmatched(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                  lei_namespace: str | None = None, ownership_namespace: str | None = None) -> list[str]:
        """Parties with no exact match and no accepted candidate: still queryable by their source key."""
        exact = self.exact_matches(namespace, principal_id=principal_id, scopes=scopes, lei_namespace=lei_namespace,
                                   ownership_namespace=ownership_namespace)
        accepted = self.accepted(namespace)
        return sorted(k for k, matches in exact.items() if not matches and not accepted.get(k))

    def group_links(
        self,
        namespace: str,
        resolved: Mapping[str, Any],
        *,
        scopes: Iterable[str],
        lei_namespace: str | None,
        as_of_ms: int | None = None,
    ) -> dict[str, Any]:
        """Group-level parties whose LEI is a recorded (GLEIF) parent of a resolved undertaking, as of a time."""
        out: dict[str, Any] = {"group_keys": set(), "links": {}}
        if not lei_namespace or not _table(self.conn, "lei_parent_assertions"):
            return out
        from src.kb.lei import LeiError, LeiStore

        leis = LeiStore(self.conn, initialize=False)
        parties = self.parties(namespace)
        groups = {p["lei"]: k for k, p in parties.items() if p["reporting_level"] == "group" and p["lei"]}
        for party in resolved.get("parties") or []:
            if not party.get("lei") or party.get("reporting_level") == "group":
                continue
            try:
                parents = leis.parents_as_of(
                    lei_namespace, party["lei"], as_of_ms if as_of_ms is not None else 2**62, scopes=set(scopes)
                )["parents"]
            except LeiError as exc:
                raise InsuranceError("unauthorized", "knowledge:companies:read and LEI namespace access") from exc
            for level in ("direct", "ultimate"):
                latest = parents[level]["latest"]
                if not latest or latest.get("relationship_status") not in {None, "ACTIVE"}:
                    continue
                key = groups.get(latest.get("parent_lei"))
                if key and key not in out["group_keys"]:
                    out["group_keys"].add(key)
                    out["links"][key] = {
                        "basis": "gleif-parent-relationship",
                        "level": level,
                        "child_lei": party["lei"],
                        "parent_lei": latest["parent_lei"],
                        "observed_at_ms": latest["observed_at_ms"],
                        "note": "a recorded ownership link from GLEIF Level 2; the group report remains the group's",
                    }
        return out
