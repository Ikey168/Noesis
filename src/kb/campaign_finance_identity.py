"""Committees, candidates, parties and organisational donors matched through reviewable identity (#2209, CF07).

Every subject stays the record its regulator published - an FEC committee or
candidate id, a Commission regulated entity id, or an organisational donor as a
filing named it - and links to other owners' records are *proposed* into the
shared reviewable state machine
(:class:`src.kb.ownership_identity.OwnershipIdentityService`, the one
:mod:`src.kb.elections_identity` and :mod:`src.kb.lobbying_identity` use),
whose accepted and reverted decisions are
:class:`src.kb.entity_history.EntityHistoryStore` decisions. Nothing is merged
and nothing is accepted automatically.

Methods (the shared basis, with the method named in the evidence):

* ``official-id`` (``exact-identifier``) - a donor line item naming an FEC
  committee id that an acquired committee registration carries;
* ``company-number`` (``exact-identifier``) - a Commission donor's Companies
  House number equal to a Corporate Ownership record's or a lobbying
  registrant's ``gb-coh`` identifier;
* ``name+address`` (``name-jurisdiction``) - equal normalised names in one
  country, with the city and state (or postcode) the filing published as
  evidence; used for FEC organisations and connected organisations against
  Corporate Ownership records and US lobbying registrants and clients;
* ``name+office+cycle`` (``name-jurisdiction``) - an FEC candidate against an
  elections-feature candidate in a US election whose year the candidate's
  registration lists, and a Commission party against the election's party list.

**Individual donors and payees are never subjects** (CF01): they carry no name
to match, are never proposed, never expanded and stay visible only as minimised,
unmatched line items. Organisational donors without an accepted decision stay
visible as ``unmatched``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.campaign_finance_sources import slug
from src.kb.campaign_finance_records import (
    READ_SCOPE,
    CampaignFinanceError,
    CampaignFinanceStore,
    authorize,
    table_exists,
)

SUBJECT_KINDS = ("candidate", "committee", "connected-organisation", "regulated-entity", "donor-committee",
                 "donor-organisation")
FEC_OFFICE_ELECTIONS = {"P": "us-president", "S": "us-senate", "H": "us-house"}
INDIVIDUAL_NOTICE = ("individual donors and payees are natural persons: never matched, linked or expanded (CF01); "
                     "they stay minimised, unmatched line items")


def _norm(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def fec_person_name(value: Any) -> str | None:
    """``LAST, FIRST`` as the FEC publishes candidate names, read as ``FIRST LAST`` for comparison only."""
    text = " ".join(str(value or "").split())
    if "," in text:
        last, first = text.split(",", 1)
        text = f"{first.strip()} {last.strip()}"
    return text or None


def donor_key(record: Mapping[str, Any]) -> str | None:
    """The subject key of an item's organisational donor; ``None`` for a natural person (never a subject)."""
    counterparty = (record.get("fields") or {}).get("counterparty") or {}
    if record.get("record_kind") != "contribution" or counterparty.get("kind") != "organisation":
        return None
    if record.get("provider") == "openfec":
        if counterparty.get("committee_id"):
            return f"campaign-finance:fec:donor-committee:{counterparty['committee_id']}"
        return f"campaign-finance:fec:donor:{slug(counterparty.get('name'))}:{slug(counterparty.get('state'))}"
    return f"campaign-finance:ukec:donor:{counterparty.get('donor_id') or slug(counterparty.get('name'))}"


def entity(record_key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(record_key)


def _country(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text[:2] if len(text) >= 2 and text[:2].isalpha() else None


class CampaignFinanceIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = CampaignFinanceStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Candidates, committees, connected organisations, regulated entities and organisational donors as published.

        Each subject cites the record revisions it was read from. Natural persons are never subjects.
        """
        scopes = set(scopes)
        out: dict[str, dict[str, Any]] = {}

        def add(key: str, kind: str, name: Any, country: str | None, row: Mapping[str, Any], **extra: Any) -> None:
            subject = out.setdefault(key, {"record_key": key, "kind": kind, "entity_id": entity(key), "names": set(),
                                           "country": country, "identifiers": {}, "addresses": set(), "cited": [],
                                           **extra})
            if name:
                subject["names"].add(" ".join(str(name).split()))
            subject["cited"].append({"record_key": row["record_key"], "source_id": row["source_id"],
                                     "revision_id": row["revision_id"]})

        for row in self.store.records(namespace, scopes=scopes):
            record = row["record"]
            fields = record["fields"]
            kind = row["record_kind"]
            if kind == "candidate":
                add(row["record_key"], "candidate", fec_person_name(fields.get("name")), "US", row,
                    office=fields.get("office"), election_years=fields.get("election_years") or [],
                    fec_id=fields.get("candidate_id"), name_as_published=fields.get("name"))
            elif kind == "committee":
                add(row["record_key"], "committee", fields.get("name"), "US", row, fec_id=fields.get("committee_id"),
                    committee_type=fields.get("committee_type"), candidate_ids=fields.get("candidate_ids") or [])
                if fields.get("affiliated_committee_name"):
                    key = f"campaign-finance:fec:connected-org:{fields['committee_id']}"
                    add(key, "connected-organisation", fields["affiliated_committee_name"], "US", row,
                        stated_by=row["record_key"], relation="connected organisation as stated on the committee's "
                                                              "registration (Form 1)")
                    out[key]["addresses"].add(f"state {fields.get('state')}")
            elif kind == "regulated-entity":
                add(row["record_key"], "regulated-entity", fields.get("name"), "GB", row,
                    regulated_entity_type=fields.get("regulated_entity_type"),
                    ec_id=fields.get("regulated_entity_id"))
            elif kind in {"contribution", "expenditure", "independent-expenditure"}:
                counterparty = fields.get("counterparty") or {}
                if counterparty.get("kind") != "organisation" or kind != "contribution":
                    continue  # only donors; natural persons are never subjects
                key = donor_key(record)
                if record["provider"] == "openfec":
                    if counterparty.get("committee_id"):
                        add(key, "donor-committee", counterparty.get("name"), "US", row,
                            fec_id=counterparty["committee_id"])
                    else:
                        add(key, "donor-organisation", counterparty.get("name"), "US", row)
                        out[key]["addresses"].add(", ".join(p for p in (counterparty.get("city"),
                                                                         counterparty.get("state")) if p))
                else:
                    add(key, "donor-organisation", counterparty.get("name"), "GB", row,
                        status=counterparty.get("status"))
                    if counterparty.get("company_registration_number"):
                        out[key]["identifiers"]["gb-coh"] = counterparty["company_registration_number"]
                    if counterparty.get("postcode"):
                        out[key]["addresses"].add(counterparty["postcode"])
        subjects = []
        for subject in out.values():
            subject["names"] = sorted(subject["names"])
            subject["addresses"] = sorted(a for a in subject["addresses"] if a)
            seen, cited = set(), []
            for c in subject["cited"]:
                if c["revision_id"] not in seen:
                    seen.add(c["revision_id"])
                    cited.append(c)
            subject["cited"] = cited[:20]
            subjects.append(subject)
        return sorted(subjects, key=lambda s: s["record_key"])

    # ------------------------------------------------------------------ targets (other owners, all optional)

    def _election_subjects(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "election_candidates"):
            return []
        from src.kb.elections_identity import ElectionIdentity

        try:
            return ElectionIdentity(self.conn, initialize=False).subjects(namespace)
        except Exception:  # noqa: BLE001 - the elections feature is optional; absent means no targets
            return []

    def _ownership_entities(self, namespace: str, principal_id: str, scopes: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_records"):
            return []
        from src.kb.ownership_store import OwnershipStore

        return [e for e in OwnershipStore(self.conn, initialize=False).records(
            namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",)) if not e.get("redacted")]

    def lobbying_subjects(self, namespace: str) -> list[dict[str, Any]]:
        """Registrants and declared clients as the in-force register revision states them (read-only)."""
        if not table_exists(self.conn, "lobbying_entries"):
            return []
        from src.kb.lobbying import LobbyingStore, client_key

        store = LobbyingStore(self.conn, initialize=False)
        out = []
        for entry_id, register, record_key in self.conn.execute(
                "SELECT entry_id, register, record_key FROM lobbying_entries WHERE namespace=? AND "
                "entry_kind='registrant' ORDER BY record_key", [namespace]).fetchall():
            revision = store.in_force(namespace, entry_id)
            statement = (revision or {}).get("statement")
            if not statement:
                continue
            side = {"register": register, "entry_id": entry_id, "revision_id": revision["revision_id"],
                    "record_key": record_key}
            identifiers = {i.get("scheme"): i.get("value") for i in statement.get("identifiers") or []
                           if isinstance(i, Mapping)}
            out.append({"kind": "registrant", "record_key": record_key, "name": statement.get("name"),
                        "country": _country(statement.get("country")), "identifiers": identifiers, "side": side})
            for client in statement.get("clients") or []:
                key = client_key(register, statement.get("native_id") or record_key.rsplit(":", 1)[-1],
                                 client.get("name"))
                out.append({"kind": "client", "record_key": key, "name": client.get("name"),
                            "country": _country(statement.get("country")), "identifiers": {},
                            "side": {**side, "client": client.get("name")}})
        return out

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                ownership_namespace: str | None = None, lobbying_namespace: str | None = None,
                elections_namespace: str | None = None) -> dict[str, Any]:
        """Offer reviewable candidates; idempotent, never an automatic merge, never an individual."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subjects = self.subjects(namespace, scopes=scopes)
        offered, unavailable = [], []
        committees = {s["fec_id"]: s for s in subjects if s["kind"] == "committee"}
        for subject in subjects:
            if subject["kind"] == "donor-committee" and subject["fec_id"] in committees:
                target = committees[subject["fec_id"]]
                offered.append(self._offer(namespace, subject, target["record_key"], target["entity_id"],
                                           "exact-identifier", "official-id", {
                                               "scheme": "fec-committee-id", "value": subject["fec_id"],
                                               "right": {"record_key": target["record_key"],
                                                         "cited": target["cited"][:1]}}, principal_id, scopes))
        # elections: candidates by name, office and cycle; parties by name in one country
        elections = self._election_subjects(elections_namespace or namespace)
        if not elections:
            unavailable.append({"provider": "political.elections", "reason": "no election records in the namespace"})
        for subject in subjects:
            names = {_norm(n) for n in subject["names"]}
            for target in elections:
                country = str(target.get("jurisdiction") or "").split("-")[0].upper()
                if _norm(target.get("label")) not in names:
                    continue
                if subject["kind"] == "candidate" and target["kind"] == "candidate" and country == "US":
                    year = int(str(target.get("election_date") or "0")[:4] or 0)
                    election_kind = str(target.get("election_id") or "").split(":")[0]
                    office = FEC_OFFICE_ELECTIONS.get(str(subject.get("office") or ""))
                    if year not in subject["election_years"] or (office and office not in election_kind):
                        continue  # the published office or election years contradict the pairing
                    offered.append(self._offer(namespace, subject, target["record_key"], target["entity_id"],
                                               "name-jurisdiction", "name+office+cycle", {
                                                   "value": target["label"], "country": "US", "office": office,
                                                   "election_id": target.get("election_id"),
                                                   "election_years_as_published": subject["election_years"],
                                                   "right": target.get("side"),
                                                   "note": "equal names with a consistent office and election year "
                                                           "are a weak signal; a reviewer decides"},
                                               principal_id, scopes))
                if subject["kind"] == "regulated-entity" and target["kind"] == "list" and country == "GB":
                    offered.append(self._offer(namespace, subject, target["record_key"], target["entity_id"],
                                               "name-jurisdiction", "name+jurisdiction", {
                                                   "value": target["label"], "country": "GB",
                                                   "election_id": target.get("election_id"),
                                                   "right": target.get("side"),
                                                   "note": "a regulated party and a party list with an equal name; "
                                                           "a reviewer decides"}, principal_id, scopes))
        # Corporate Ownership: company numbers, then name + address in one country
        organisations = [s for s in subjects if s["kind"] in {"donor-organisation", "connected-organisation"}]
        if ownership_namespace:
            try:
                owned = self._ownership_entities(ownership_namespace, principal_id, scopes)
            except Exception as exc:  # noqa: BLE001 - ownership access is optional; report it
                owned = []
                unavailable.append({"provider": "ownership.core", "reason": getattr(exc, "code", "unavailable")})
            if not owned:
                unavailable.append({"provider": "ownership.core", "reason": "no legal entities in the namespace"})
            for subject in organisations:
                for view in owned:
                    body = view["record"]
                    right = {"record_key": body["record_key"], "record_id": view["record_id"],
                             "revision": view["revision"], "ownership_namespace": ownership_namespace}
                    numbers = {str(i.get("value")).lstrip("0") for i in body.get("identifiers") or []
                               if i.get("scheme") in {"gb-coh", "company_number"}}
                    number = str(subject["identifiers"].get("gb-coh") or "").lstrip("0")
                    if number and number in numbers:
                        offered.append(self._offer(namespace, subject, body["record_key"], entity(body["record_key"]),
                                                   "exact-identifier", "company-number", {
                                                       "scheme": "gb-coh", "value": subject["identifiers"]["gb-coh"],
                                                       "right": right}, principal_id, scopes))
                    elif (_country(body.get("jurisdiction")) == subject["country"] and subject["names"]
                          and _norm(body.get("name")) in {_norm(n) for n in subject["names"]}):
                        offered.append(self._offer(namespace, subject, body["record_key"], entity(body["record_key"]),
                                                   "name-jurisdiction", "name+address", {
                                                       "value": body.get("name"), "country": subject["country"],
                                                       "addresses_as_published": subject["addresses"],
                                                       "right": right,
                                                       "note": "equal normalised names in one country with the "
                                                               "published address are a weak signal; a reviewer "
                                                               "decides"}, principal_id, scopes))
        else:
            unavailable.append({"provider": "ownership.core", "reason": "no ownership namespace given"})
        # Lobbying registrants and clients
        lobbying = self.lobbying_subjects(lobbying_namespace or namespace)
        if not lobbying:
            unavailable.append({"provider": "political.lobbying", "reason": "no register entries in the namespace"})
        for subject in organisations:
            for target in lobbying:
                number = str(subject["identifiers"].get("gb-coh") or "").lstrip("0")
                theirs = str(target["identifiers"].get("gb-coh") or "").lstrip("0")
                if number and number == theirs:
                    method, basis = "company-number", "exact-identifier"
                elif (target["country"] == subject["country"] and target["name"]
                      and _norm(target["name"]) in {_norm(n) for n in subject["names"]}):
                    method, basis = "name+address", "name-jurisdiction"
                else:
                    continue
                offered.append(self._offer(namespace, subject, target["record_key"], entity(target["record_key"]),
                                           basis, method, {"value": target["name"], "country": target["country"],
                                                           "right": target["side"], "lobbying_kind": target["kind"],
                                                           "addresses_as_published": subject["addresses"]},
                                           principal_id, scopes))
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes), "unavailable": unavailable,
                "individuals": INDIVIDUAL_NOTICE}

    def _offer(self, namespace, subject, right_key, right_entity, basis, method, evidence, principal_id, scopes):
        return self.service.offer(
            namespace, left_key=subject["record_key"], right_key=right_key, left_entity=subject["entity_id"],
            right_entity=right_entity, basis=basis,
            evidence=[{**evidence, "method": method,
                       "left": {"record_key": subject["record_key"], "kind": subject["kind"],
                                "names_as_published": subject["names"], "cited": subject["cited"][:5]}}],
            principal_id=principal_id, scopes=scopes)

    # ------------------------------------------------------------------ review and reads

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        evidence = candidate["evidence"][0] if candidate["evidence"] else {}
        return {
            "candidate_id": candidate["candidate_id"], "state": candidate["state"],
            "review_state": {"accepted": "reviewed-match", "rejected": "reviewed-non-match",
                             "reverted": "reverted", "proposed": "unreviewed-candidate"}[candidate["state"]],
            "basis": candidate["basis"], "method": evidence.get("method"), "confidence": candidate["confidence"],
            "records": [candidate["left_key"], candidate["right_key"]],
            "entities": [candidate["left_entity"], candidate["right_entity"]],
            "decision_id": candidate["decision_id"],
            "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
            "reason": last.get("reason"), "evidence": candidate["evidence"], "history": candidate["history"],
            "notice": "a reviewable identity decision; records are never merged",
        }

    def candidates(self, namespace: str, *, scopes: Iterable[str], record_key: str | None = None
                   ) -> list[dict[str, Any]]:
        rows = [c for c in self.service.candidates(namespace, scopes=scopes, record_key=record_key)
                if c["left_key"].startswith("campaign-finance:") or c["right_key"].startswith("campaign-finance:")]
        return [self.view(c) for c in rows]

    def review(self, namespace: str, candidate_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        self._own(namespace, candidate_id, scopes)
        return self.view(self.service.review(namespace, candidate_id, decision, reason, principal_id=principal_id,
                                             scopes=scopes))

    def revert(self, namespace: str, candidate_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        self._own(namespace, candidate_id, scopes)
        return self.view(self.service.revert(namespace, candidate_id, reason, principal_id=principal_id,
                                             scopes=scopes))

    def _own(self, namespace: str, candidate_id: str, scopes: Iterable[str]) -> None:
        if not any(c["candidate_id"] == candidate_id for c in self.candidates(namespace, scopes=scopes)):
            raise CampaignFinanceError("not_found", "no campaign-finance identity candidate with that id")

    def accepted(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Accepted, unreverted links of one subject: the other record, method and decision."""
        out = []
        for view in self.candidates(namespace, scopes=scopes, record_key=record_key):
            if view["state"] != "accepted":
                continue
            other = view["records"][1] if view["records"][0] == record_key else view["records"][0]
            out.append({"candidate_id": view["candidate_id"], "record_key": other, "basis": view["basis"],
                        "method": view["method"], "confidence": view["confidence"],
                        "decision_id": view["decision_id"], "reviewer": view["reviewer"]})
        return out

    def identity(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Accepted links and open candidates for one subject; without an accepted link it is unmatched."""
        try:
            views = self.candidates(namespace, scopes=scopes, record_key=record_key)
        except Exception as exc:  # noqa: BLE001 - identity access is optional for an answer
            return {"state": "unmatched", "links": [], "candidates": [], "unavailable": [getattr(exc, "code", "x")]}
        links = [v for v in views if v["state"] == "accepted"]
        return {"state": "matched" if links else "unmatched",
                "links": [{"candidate_id": v["candidate_id"], "records": v["records"], "method": v["method"],
                           "confidence": v["confidence"], "reviewer": v["reviewer"]} for v in links],
                "candidates": [v["candidate_id"] for v in views if v["state"] == "proposed"]}

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Organisational subjects without an accepted link, and the minimised individual items never matched."""
        scopes = set(scopes)
        subjects = [{"record_key": s["record_key"], "kind": s["kind"], "names": s["names"], "state": "unmatched"}
                    for s in self.subjects(namespace, scopes=scopes)
                    if self.identity(namespace, s["record_key"], scopes=scopes)["state"] == "unmatched"]
        individual = sum(1 for r in self.store.records(namespace, scopes=scopes, kinds=["contribution", "expenditure",
                                                                                          "independent-expenditure"])
                         if r["individual"])
        return {"unmatched": subjects, "individual_items_never_matched": individual, "notice": INDIVIDUAL_NOTICE}
