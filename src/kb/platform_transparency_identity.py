"""Advertisers and funding entities matched through reviewable identity (#2580, SP07).

Every subject stays the record its platform published - a Google advertiser
id, a Meta page, or a funding entity as a Meta ad's ``bylines`` declared it -
and links to other owners' records are *proposed* into the shared reviewable
state machine (:class:`src.kb.ownership_identity.OwnershipIdentityService`, the
one :mod:`src.kb.elections_identity` and :mod:`src.kb.campaign_finance_identity`
use), whose accepted and reverted decisions are
:class:`src.kb.entity_history.EntityHistoryStore` decisions. Nothing is merged
and nothing is accepted automatically. Platforms themselves are source
identities (the ``platform`` of each record and its source id), never matched.

Methods, published identifiers before names:

* ``published-id`` (``exact-identifier``) - an FEC committee id Google publishes
  in the advertiser's ``Public_IDs_List`` equal to an acquired campaign-finance
  committee registration;
* ``name+country`` (``name-jurisdiction``) - equal normalised names in one
  country (the advertiser's published region or the declared reached country)
  against campaign-finance committees and regulated entities, lobbying
  registrants and clients, and Corporate Ownership legal entities; a weak
  signal that a reviewer decides.

**Individuals are excluded from matching (SP01).** Match targets are
organisation records only: campaign-finance *candidates* and elections
candidates (natural persons) are never targets, so an advertiser or page whose
name matches only a person stays unmatched. Subjects without an accepted
decision stay visible as ``unmatched``.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.platform_transparency_sources import slug
from src.kb.platform_transparency_records import (
    READ_SCOPE,
    PlatformTransparencyError,
    PlatformTransparencyStore,
    authorize,
    table_exists,
)

SUBJECT_KINDS = ("advertiser", "funding-entity")
PERSON_NOTICE = ("match targets are organisation records only (committees, regulated entities, lobbying registrants "
                 "and clients, legal entities); natural-person records are never targets (SP01), and a platform is a "
                 "source identity, never matched")
_PAID_FOR = re.compile(r"^\s*(paid for by|funded by|sponsored by)\s+", re.IGNORECASE)


def _norm(value: Any) -> str:
    from src.kb.entities import normalize_surface

    return normalize_surface(str(value or ""))


def funding_name(value: Any) -> str:
    """A declared byline read without its 'Paid for by' lead-in, for comparison only (stored as declared)."""
    return _PAID_FOR.sub("", " ".join(str(value or "").split()))


def funding_key(name: Any) -> str:
    return f"platform-transparency:meta:funding-entity:{slug(funding_name(name))}"


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

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Advertisers and declared funding entities as published, each citing the record revisions read."""
        scopes = set(scopes)
        out: dict[str, dict[str, Any]] = {}

        def add(key: str, kind: str, names: Iterable[Any], country: str | None, row: Mapping[str, Any],
                **extra: Any) -> dict[str, Any]:
            subject = out.setdefault(key, {"record_key": key, "kind": kind, "entity_id": entity(key), "names": set(),
                                           "countries": set(), "fec_ids": set(), "platform": row["platform"],
                                           "cited": [], **extra})
            subject["names"].update(" ".join(str(n).split()) for n in names if n)
            if country:
                subject["countries"].add(country)
            subject["cited"].append({"record_key": row["record_key"], "source_id": row["source_id"],
                                     "revision_id": row["revision_id"]})
            return subject

        for row in self.store.records(namespace, scopes=scopes, kinds=["advertiser", "ad"]):
            fields = row["record"]["fields"]
            if row["record_kind"] == "advertiser":
                countries = fields.get("country_selection") or [fields.get("regions_as_published")]
                subject = add(row["record_key"], "advertiser", fields.get("names_as_published") or [],
                              _country(countries[0] if countries else None), row,
                              advertiser_id=fields.get("advertiser_id"))
                subject["fec_ids"].update(fields.get("fec_committee_ids_as_published") or [])
            else:
                countries = fields.get("reached_countries_selected") or []
                for byline in fields.get("funding_entity_as_declared") or []:
                    subject = add(funding_key(byline), "funding-entity", [funding_name(byline)],
                                  countries[0] if len(countries) == 1 else None, row,
                                  declared_as=set(), advertisers=set())
                    subject["declared_as"].add(byline)
                    subject["advertisers"].add(row["record"].get("advertiser_key"))
        subjects = []
        for subject in out.values():
            for key in ("names", "countries", "fec_ids", "declared_as", "advertisers"):
                if key in subject:
                    subject[key] = sorted(v for v in subject[key] if v)
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

        store = CampaignFinanceStore(self.conn, initialize=False)
        out = []
        # candidates are natural persons: never targets
        for row in store.records(namespace, scopes=scopes, kinds=["committee", "regulated-entity"]):
            fields = row["record"]["fields"]
            out.append({"record_key": row["record_key"], "name": fields.get("name"),
                        "country": "US" if row["record_kind"] == "committee" else "GB",
                        "fec_id": fields.get("committee_id"), "owner": "political.campaign-finance",
                        "side": {"record_key": row["record_key"], "source_id": row["source_id"],
                                 "revision_id": row["revision_id"], "namespace": namespace}})
        return out

    def _lobbying_targets(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "lobbying_entries"):
            return []
        from src.kb.campaign_finance_identity import CampaignFinanceIdentity

        try:
            found = CampaignFinanceIdentity(self.conn, initialize=False).lobbying_subjects(namespace)
        except Exception:  # noqa: BLE001 - the lobbying feature is optional; absent means no targets
            return []
        return [{"record_key": t["record_key"], "name": t["name"], "country": t["country"], "fec_id": None,
                 "owner": "political.lobbying", "lobbying_kind": t["kind"], "side": t["side"]} for t in found]

    def _ownership_targets(self, namespace: str, principal_id: str, scopes: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ownership_records"):
            return []
        from src.kb.ownership_store import OwnershipStore

        out = []
        for view in OwnershipStore(self.conn, initialize=False).records(
                namespace, principal_id=principal_id, scopes=scopes, kinds=("legal_entity",)):
            if view.get("redacted"):
                continue
            body = view["record"]
            out.append({"record_key": body["record_key"], "name": body.get("name"),
                        "country": _country(body.get("jurisdiction")), "fec_id": None, "owner": "ownership.core",
                        "side": {"record_key": body["record_key"], "record_id": view["record_id"],
                                 "revision": view["revision"], "ownership_namespace": namespace}})
        return out

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                ownership_namespace: str | None = None, lobbying_namespace: str | None = None,
                campaign_finance_namespace: str | None = None) -> dict[str, Any]:
        """Offer reviewable candidates; idempotent, never an automatic merge, never a natural-person target."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subjects = self.subjects(namespace, scopes=scopes)
        offered, unavailable = [], []
        targets: list[dict[str, Any]] = []
        try:
            committees = self._campaign_finance_targets(campaign_finance_namespace or namespace, scopes)
        except Exception as exc:  # noqa: BLE001 - campaign-finance access is optional; report it
            committees = []
            unavailable.append({"provider": "political.campaign-finance", "reason": getattr(exc, "code", "error")})
        if not committees:
            unavailable.append({"provider": "political.campaign-finance",
                                "reason": "no committee or regulated-entity records in the namespace"})
        targets += committees
        lobbying = self._lobbying_targets(lobbying_namespace or namespace)
        if not lobbying:
            unavailable.append({"provider": "political.lobbying", "reason": "no register entries in the namespace"})
        targets += lobbying
        if ownership_namespace:
            try:
                owned = self._ownership_targets(ownership_namespace, principal_id, scopes)
            except Exception as exc:  # noqa: BLE001 - ownership access is optional; report it
                owned = []
                unavailable.append({"provider": "ownership.core", "reason": getattr(exc, "code", "unavailable")})
            if not owned:
                unavailable.append({"provider": "ownership.core", "reason": "no legal entities in the namespace"})
            targets += owned
        else:
            unavailable.append({"provider": "ownership.core", "reason": "no ownership namespace given"})
        for subject in subjects:
            exact = set()
            for target in targets:
                if target.get("fec_id") and target["fec_id"] in subject["fec_ids"]:
                    exact.add(target["record_key"])
                    offered.append(self._offer(namespace, subject, target, "exact-identifier", "published-id", {
                        "scheme": "fec-committee-id", "value": target["fec_id"],
                        "published_by": "Google Public_IDs_List", "right": target["side"]}, principal_id, scopes))
            names = {_norm(n) for n in subject["names"]}
            for target in targets:
                if target["record_key"] in exact or not target.get("name") or _norm(target["name"]) not in names:
                    continue
                if not target.get("country") or target["country"] not in subject["countries"]:
                    continue  # names in different (or unstated) countries are never paired
                offered.append(self._offer(namespace, subject, target, "name-jurisdiction", "name+country", {
                    "value": target["name"], "country": target["country"], "right": target["side"],
                    "target_kind": target.get("lobbying_kind") or target["owner"],
                    "note": "equal normalised names in one country are a weak signal; a reviewer decides"},
                    principal_id, scopes))
        return {"proposed": sorted({o["candidate_id"] for o in offered if o["created"] or o.get("change")}),
                "candidates": self.candidates(namespace, scopes=scopes), "unavailable": unavailable,
                "individuals": PERSON_NOTICE}

    def _offer(self, namespace, subject, target, basis, method, evidence, principal_id, scopes):
        return self.service.offer(
            namespace, left_key=subject["record_key"], right_key=target["record_key"],
            left_entity=subject["entity_id"], right_entity=entity(target["record_key"]), basis=basis,
            evidence=[{**evidence, "method": method, "target_owner": target["owner"],
                       "left": {"record_key": subject["record_key"], "kind": subject["kind"],
                                "platform": subject["platform"], "names_as_published": subject["names"],
                                "cited": subject["cited"][:5]}}],
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

    def subjects_for(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Platform-transparency subjects accepted as the same as another owner's record (e.g. a committee)."""
        out = []
        for view in self.candidates(namespace, scopes=scopes, record_key=record_key):
            if view["state"] != "accepted":
                continue
            ours = next((r for r in view["records"] if r.startswith("platform-transparency:")), None)
            if ours and ours != record_key:
                out.append({"record_key": ours, "candidate_id": view["candidate_id"], "method": view["method"],
                            "basis": view["basis"], "decision_id": view["decision_id"]})
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
                           "basis": v["basis"], "confidence": v["confidence"], "reviewer": v["reviewer"],
                           "decision_id": v["decision_id"]} for v in links],
                "candidates": [v["candidate_id"] for v in views if v["state"] == "proposed"]}

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Advertisers and funding entities without an accepted link, visible as published."""
        scopes = set(scopes)
        return {"unmatched": [{"record_key": s["record_key"], "kind": s["kind"], "platform": s["platform"],
                               "names": s["names"], "state": "unmatched"}
                              for s in self.subjects(namespace, scopes=scopes)
                              if self.identity(namespace, s["record_key"], scopes=scopes)["state"] == "unmatched"],
                "notice": PERSON_NOTICE}
