"""Advertisers and funding entities matched through reviewable identity; platforms as source identities (#2580, SP07).

Every subject stays the record its platform published - a Meta page (the
advertiser as declared) with the ``bylines`` disclaimer (the funding entity as
declared), or a Google verified advertiser with the identifiers Google
publishes - and links to other owners' records are *proposed* into the shared
reviewable state machine (:class:`src.kb.ownership_identity.OwnershipIdentityService`,
the one :mod:`src.kb.elections_identity`, :mod:`src.kb.ownership_identity` and
:mod:`src.kb.campaign_finance_identity` use), whose accepted and reverted
decisions are :class:`src.kb.entity_history.EntityHistoryStore` decisions.
Nothing is merged and nothing is accepted automatically.

Methods, published identifiers before names:

* ``published-id`` (``exact-identifier``) - a Google advertiser whose
  ``public_ids_list`` names an FEC committee id that an acquired
  campaign-finance committee registration carries;
* ``name+jurisdiction`` (``name-jurisdiction``) - equal normalised names in
  one country, against campaign-finance committees and Electoral Commission
  regulated entities, election party lists, lobbying registrants and clients,
  and Corporate Ownership legal entities. Always a weak signal for a reviewer.

**Individuals are excluded from matching (SP01).** Only organisational targets
are ever proposed: election *candidate* records (natural persons), individual
donors and any person record are never targets, whatever the name. Platforms
are source identities (:class:`src.kb.source_identity.SourceIdentityStore`),
registered by :meth:`PlatformTransparencyIdentity.register_platforms`, never
identity candidates. Subjects without an accepted decision stay visible as
``unmatched``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.platform_transparency_sources import meta_funder_key
from src.kb.platform_transparency_records import (
    READ_SCOPE,
    PlatformTransparencyError,
    PlatformTransparencyStore,
    authorize,
    table_exists,
)

SUBJECT_KINDS = ("advertiser", "funding-entity")
INDIVIDUAL_NOTICE = ("matching proposes organisational targets only (committees, regulated entities, party lists, "
                     "lobbying registrants and clients, legal entities); election candidates and other natural "
                     "persons are never matched (SP01)")
_FEC_COMMITTEE = re.compile(r"^C\d{8}$")
PLATFORM_NAMES = {"meta": "Meta (Facebook, Instagram)", "google": "Google"}


def _norm(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def entity(record_key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(record_key)


def _country(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text[:2] if len(text) >= 2 and text[:2].isalpha() else None


class PlatformTransparencyIdentity:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = PlatformTransparencyStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(conn, now=self.now, initialize=initialize)

    # ------------------------------------------------------------------ platforms as source identities

    def register_platforms(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Each platform on record becomes (or already is) a source identity; idempotent."""
        from src.kb.source_identity import SourceIdentityStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        identities = SourceIdentityStore(self.conn)
        platforms: dict[str, str] = {}
        for row in self.store.records(namespace, scopes=scopes):
            published = row["record"]["fields"].get("platform_name") if row["record_kind"] == \
                "statement-of-reasons" else None
            name = published or PLATFORM_NAMES.get(row["platform"])
            if name or row["platform"] not in platforms:
                platforms[row["platform"]] = name or row["platform"]
        out = []
        for platform, name in sorted(platforms.items()):
            registered = identities.register(namespace, "organization", name, principal_id=principal_id,
                                             scopes=scopes, native_ids={"platform-transparency": platform},
                                             producer={"name": "osint.platform-transparency", "version": "1.0.0"})
            out.append({"platform": platform, "display_name": name, "source_id": registered["source_id"],
                        "idempotent": bool(registered.get("idempotent"))})
        return out

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Advertisers and funding entities as the platforms published them, each citing the revisions it was read
        from."""
        scopes = set(scopes)
        out: dict[str, dict[str, Any]] = {}

        def add(key: str, kind: str, platform: str, name: Any, country: str | None, row: Mapping[str, Any],
                **extra: Any) -> dict[str, Any]:
            subject = out.setdefault(key, {"record_key": key, "kind": kind, "platform": platform,
                                           "entity_id": entity(key), "names": set(), "country": country,
                                           "identifiers": {}, "cited": [], **extra})
            if name:
                subject["names"].add(" ".join(str(name).split()))
            subject["cited"].append({"record_key": row["record_key"], "source_id": row["source_id"],
                                     "revision_id": row["revision_id"]})
            return subject

        for row in self.store.records(namespace, scopes=scopes, kinds=["advertiser", "ad"]):
            fields = row["record"]["fields"]
            if row["provider"] == "meta-ad-library":
                countries = fields.get("countries") or fields.get("ad_reached_countries_requested") or []
                country = countries[0] if len(countries) == 1 else None
                if row["record_kind"] == "advertiser":
                    for name in fields.get("names_as_declared") or []:
                        add(row["record_key"], "advertiser", "meta", name, country, row, page_id=fields["page_id"])
                else:
                    add(row["advertiser_key"], "advertiser", "meta", fields.get("advertiser_as_declared"), country,
                        row, page_id=fields["page_id"])
                    if fields.get("funding_entity_as_declared"):
                        add(meta_funder_key(fields["funding_entity_as_declared"], country), "funding-entity", "meta",
                            fields["funding_entity_as_declared"], country, row,
                            relation="bylines (paid for by) as declared on the advertiser's ads")
            elif row["record_kind"] == "advertiser":
                regions = fields.get("regions") or []
                subject = add(row["record_key"], "advertiser", "google", fields.get("advertiser_name"),
                              regions[0] if len(regions) == 1 else None, row, google_id=fields["advertiser_id"])
                for value in fields.get("public_ids") or []:
                    if _FEC_COMMITTEE.fullmatch(value.upper()):
                        subject["identifiers"]["fec-committee-id"] = value.upper()
                    else:
                        subject["identifiers"].setdefault("published-id", value)
            else:
                add(row["advertiser_key"], "advertiser", "google", fields.get("advertiser_as_declared"),
                    (fields.get("regions") or [None])[0] if len(fields.get("regions") or []) == 1 else None, row,
                    google_id=fields["advertiser_id"])
        subjects = []
        for subject in out.values():
            subject["names"] = sorted(subject["names"])
            seen, cited = set(), []
            for c in subject["cited"]:
                if c["revision_id"] not in seen:
                    seen.add(c["revision_id"])
                    cited.append(c)
            subject["cited"] = cited[:20]
            subjects.append(subject)
        return sorted(subjects, key=lambda s: s["record_key"])

    # ------------------------------------------------------------------ targets (other owners, all optional)

    def _campaign_finance_targets(self, namespace: str, scopes: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "campaign_finance_records"):
            return []
        from src.kb.campaign_finance_records import CampaignFinanceStore

        try:
            rows = CampaignFinanceStore(self.conn, initialize=False).records(
                namespace, scopes=scopes | {"knowledge:political:campaign-finance:read"},
                kinds=["committee", "regulated-entity"])
        except Exception:  # noqa: BLE001 - the campaign-finance feature is optional; absent means no targets
            return []
        out = []
        for row in rows:
            fields = row["record"]["fields"]
            out.append({"kind": row["record_kind"], "record_key": row["record_key"], "name": fields.get("name"),
                        "country": row["jurisdiction"], "fec_id": fields.get("committee_id"),
                        "side": {"record_key": row["record_key"], "revision_id": row["revision_id"],
                                 "source_id": row["source_id"], "campaign_finance_namespace": namespace}})
        return out

    def _election_party_lists(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "election_candidates"):
            return []
        from src.kb.elections_identity import ElectionIdentity

        try:
            subjects = ElectionIdentity(self.conn, initialize=False).subjects(namespace)
        except Exception:  # noqa: BLE001 - the elections feature is optional
            return []
        # party lists only: candidate records are natural persons and never targets (SP01)
        return [s for s in subjects if s["kind"] == "list"]

    def _lobbying_targets(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "lobbying_entries"):
            return []
        from src.kb.campaign_finance_identity import CampaignFinanceIdentity

        try:
            return CampaignFinanceIdentity(self.conn, initialize=False).lobbying_subjects(namespace)
        except Exception:  # noqa: BLE001 - the lobbying feature is optional
            return []

    def _ownership_entities(self, namespace: str, principal_id: str, scopes: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_records"):
            return []
        from src.kb.ownership_store import OwnershipStore

        return [e for e in OwnershipStore(self.conn, initialize=False).records(
            namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",)) if not e.get("redacted")]

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                ownership_namespace: str | None = None, campaign_finance_namespace: str | None = None,
                lobbying_namespace: str | None = None, elections_namespace: str | None = None) -> dict[str, Any]:
        """Offer reviewable candidates; idempotent, never an automatic merge, never a natural person."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subjects = self.subjects(namespace, scopes=scopes)
        offered, unavailable = [], []

        def names(subject: Mapping[str, Any]) -> set[str]:
            return {_norm(n) for n in subject["names"]}

        committees = self._campaign_finance_targets(campaign_finance_namespace or namespace, scopes)
        if not committees:
            unavailable.append({"provider": "political.campaign-finance",
                                "reason": "no committee or regulated-entity records in the namespace"})
        for subject in subjects:
            fec_id = subject["identifiers"].get("fec-committee-id")
            for target in committees:
                if fec_id and target["fec_id"] and fec_id == str(target["fec_id"]).upper():
                    offered.append(self._offer(namespace, subject, target["record_key"], "exact-identifier",
                                               "published-id", {"scheme": "fec-committee-id", "value": fec_id,
                                                                "right": target["side"],
                                                                "note": "the id Google publishes for the verified "
                                                                        "advertiser equals the committee's FEC id"},
                                               principal_id, scopes))
                elif (target["name"] and subject["country"] == target["country"]
                      and _norm(target["name"]) in names(subject) and not fec_id):
                    offered.append(self._offer(namespace, subject, target["record_key"], "name-jurisdiction",
                                               "name+jurisdiction", {"value": target["name"],
                                                                     "country": subject["country"],
                                                                     "right": target["side"],
                                                                     "note": "equal normalised names in one country "
                                                                             "are a weak signal; a reviewer decides"},
                                               principal_id, scopes))
        lists = self._election_party_lists(elections_namespace or namespace)
        if not lists:
            unavailable.append({"provider": "political.elections", "reason": "no party lists in the namespace"})
        for subject in subjects:
            for target in lists:
                country = str(target.get("jurisdiction") or "").split("-")[0].upper()
                if country == subject["country"] and _norm(target.get("label")) in names(subject):
                    offered.append(self._offer(namespace, subject, target["record_key"], "name-jurisdiction",
                                               "name+jurisdiction", {"value": target["label"], "country": country,
                                                                     "election_id": target.get("election_id"),
                                                                     "right": target.get("side"),
                                                                     "note": "an advertiser and a party list with an "
                                                                             "equal name; a reviewer decides"},
                                               principal_id, scopes))
        lobbying = self._lobbying_targets(lobbying_namespace or namespace)
        if not lobbying:
            unavailable.append({"provider": "political.lobbying", "reason": "no register entries in the namespace"})
        for subject in subjects:
            for target in lobbying:
                if target["country"] == subject["country"] and target["name"] and _norm(target["name"]) in \
                        names(subject):
                    offered.append(self._offer(namespace, subject, target["record_key"], "name-jurisdiction",
                                               "name+jurisdiction", {"value": target["name"],
                                                                     "country": target["country"],
                                                                     "right": target["side"],
                                                                     "lobbying_kind": target["kind"]},
                                               principal_id, scopes))
        if ownership_namespace:
            try:
                owned = self._ownership_entities(ownership_namespace, principal_id, scopes)
            except Exception as exc:  # noqa: BLE001 - ownership access is optional; report it
                owned = []
                unavailable.append({"provider": "ownership.core", "reason": getattr(exc, "code", "unavailable")})
            if not owned:
                unavailable.append({"provider": "ownership.core", "reason": "no legal entities in the namespace"})
            for subject in subjects:
                for view in owned:
                    body = view["record"]
                    if _country(body.get("jurisdiction")) == subject["country"] and _norm(body.get("name")) in \
                            names(subject):
                        offered.append(self._offer(namespace, subject, body["record_key"], "name-jurisdiction",
                                                   "name+jurisdiction", {
                                                       "value": body.get("name"), "country": subject["country"],
                                                       "right": {"record_key": body["record_key"],
                                                                 "record_id": view["record_id"],
                                                                 "revision": view["revision"],
                                                                 "ownership_namespace": ownership_namespace},
                                                       "note": "equal normalised names in one country; a reviewer "
                                                               "decides"}, principal_id, scopes))
        else:
            unavailable.append({"provider": "ownership.core", "reason": "no ownership namespace given"})
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes), "unavailable": unavailable,
                "individuals": INDIVIDUAL_NOTICE}

    def _offer(self, namespace, subject, right_key, basis, method, evidence, principal_id, scopes):
        return self.service.offer(
            namespace, left_key=subject["record_key"], right_key=right_key, left_entity=subject["entity_id"],
            right_entity=entity(right_key), basis=basis,
            evidence=[{**evidence, "method": method,
                       "left": {"record_key": subject["record_key"], "kind": subject["kind"],
                                "platform": subject["platform"], "names_as_published": subject["names"],
                                "identifiers_as_published": subject["identifiers"], "cited": subject["cited"][:5]}}],
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
                if c["left_key"].startswith("platform-transparency:")
                or c["right_key"].startswith("platform-transparency:")]
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
            raise PlatformTransparencyError("not_found", "no platform-transparency identity candidate with that id")

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
                           "basis": v["basis"], "confidence": v["confidence"], "reviewer": v["reviewer"]}
                          for v in links],
                "candidates": [v["candidate_id"] for v in views if v["state"] == "proposed"]}

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Advertisers and funding entities without an accepted link, visible as unmatched."""
        scopes = set(scopes)
        subjects = [{"record_key": s["record_key"], "kind": s["kind"], "platform": s["platform"], "names": s["names"],
                     "state": "unmatched"}
                    for s in self.subjects(namespace, scopes=scopes)
                    if self.identity(namespace, s["record_key"], scopes=scopes)["state"] == "unmatched"]
        return {"unmatched": subjects, "notice": INDIVIDUAL_NOTICE}

