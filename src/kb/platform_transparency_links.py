"""Ads linked to elections, campaign-finance filings and lobbying records by citation and accepted matches (#2580,
SP08).

Every link points at a specific ad revision and at a specific target revision,
and records its basis. Links rest only on accepted, unreverted SP07 identity
decisions (:mod:`src.kb.platform_transparency_identity`) and on identifiers the
records publish:

* **election** - an advertiser or funding entity accepted as the same
  organisation as an election's party list links each of its ads to that
  election and the contests the party stood in; an advertiser accepted as a
  campaign-finance committee links its ads to the contests that committee's
  filings are linked to by the campaign-finance feature (the campaign-finance
  link is cited). Each link states the ad's delivery dates relative to the
  election day as published (``before``, ``spanning`` or ``after``), never an
  effect on the result;
* **campaign-finance** - an advertiser accepted as a committee or regulated
  entity (by the FEC id Google publishes, or by a reviewed name match) links
  each ad to the filing versions of that committee current when the link is
  made, each cited by its record revision;
* **lobbying** - an advertiser or funding entity accepted as a lobbying
  registrant or client links its ads to the register revision in force.

Accepted links whose target is not on record, and providers that are absent,
are reported, never dropped. A link states a shared identifier or a reviewed
match; nothing infers coordination, influence or a common operator from
co-occurring links.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.platform_transparency_sources import meta_funder_key
from src.kb.platform_transparency_identity import PlatformTransparencyIdentity, _norm
from src.kb.platform_transparency_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-platform-transparency-link-v1"
LINK_KINDS = ("election", "campaign-finance", "lobbying")
NOTICE = ("a link states a shared identifier or a reviewed identity match; it is not evidence of coordination, "
          "influence or a common operator")
CF_READ = "knowledge:political:campaign-finance:read"
_DDL = """
CREATE TABLE IF NOT EXISTS platform_transparency_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, link_kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  record_key TEXT NOT NULL, record_revision_id TEXT NOT NULL, target_key TEXT NOT NULL,
  target_namespace TEXT NOT NULL, target_revision TEXT, basis_json TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""
_COLUMNS = ("link_id", "link_kind", "subject_key", "record_key", "record_revision_id", "target_key",
            "target_namespace", "target_revision", "basis_json", "created_by", "created_at_ms")


def _link_view(row) -> dict[str, Any]:
    view = dict(zip(_COLUMNS, row))
    view["basis"] = json.loads(view.pop("basis_json"))
    return {"contract": CONTRACT, **view, "notice": NOTICE}


def delivery_relation(fields: Mapping[str, Any], election_day: str | None) -> str | None:
    """The ad's published delivery dates relative to an election day: before, spanning or after (or unknown)."""
    start = str(fields.get("ad_delivery_start_time") or fields.get("date_range_start") or "")[:10] or None
    stop = str(fields.get("ad_delivery_stop_time") or fields.get("date_range_end") or "")[:10] or None
    if not election_day or not start:
        return None
    if start > election_day:
        return "after"
    if stop and stop < election_day:
        return "before"
    return "spanning" if stop else "started-before-no-stop-published"


def ad_keys_of(store: Any, namespace: str, subject: Mapping[str, Any], scopes: set[str]) -> list[str]:
    """The ads a subject stands for: an advertiser's ads, or the ads whose bylines declare the funding entity."""
    if subject["kind"] == "advertiser":
        return [r["record_key"] for r in store.records(namespace, scopes=scopes, kinds=["ad"],
                                                        advertiser_key=subject["record_key"])]
    keys = []
    for row in store.records(namespace, scopes=scopes, kinds=["ad"], provider="meta-ad-library"):
        fields = row["record"]["fields"]
        countries = fields.get("ad_reached_countries_requested") or []
        country = countries[0] if len(countries) == 1 else None
        if fields.get("funding_entity_as_declared") and meta_funder_key(
                fields["funding_entity_as_declared"], country) == subject["record_key"]:
            keys.append(row["record_key"])
    return keys


class PlatformTransparencyLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.identity = PlatformTransparencyIdentity(conn, now=now, initialize=initialize)
        self.store = self.identity.store
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "platform_transparency_links")

    # ------------------------------------------------------------------ writes

    def _insert(self, namespace: str, kind: str, subject_key: str, row: Mapping[str, Any], target_key: str,
                target_namespace: str, target_revision: str | None, basis: Mapping[str, Any], principal_id: str
                ) -> tuple[dict[str, Any], bool]:
        link_id = "pt-link:" + digest([namespace, kind, subject_key, row["revision_id"], target_key, target_namespace,
                                       target_revision])[:24]
        created = not self.conn.execute("SELECT 1 FROM platform_transparency_links WHERE namespace=? AND link_id=?",
                                        [namespace, link_id]).fetchone()
        if created:
            self.conn.execute("INSERT INTO platform_transparency_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, link_id, kind, subject_key, row["record_key"], row["revision_id"],
                               target_key, target_namespace, target_revision, canonical(basis), principal_id,
                               self.now()])
        found = self.conn.execute("SELECT " + ", ".join(_COLUMNS) + " FROM platform_transparency_links WHERE "
                                  "namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        return _link_view(found), created

    def _ads_of(self, namespace: str, subject: Mapping[str, Any], scopes: set[str]) -> list[dict[str, Any]]:
        keys = ad_keys_of(self.store, namespace, subject, scopes)
        return self.store.records(namespace, scopes=scopes, kinds=["ad"], record_keys=keys) if keys else []

    def _accepted(self, namespace: str, scopes: set[str], prefixes: tuple[str, ...]):
        for subject in self.identity.subjects(namespace, scopes=scopes):
            matches = [a for a in self.identity.accepted(namespace, subject["record_key"], scopes=scopes)
                       if a["record_key"].startswith(prefixes)]
            yield subject, matches

    def link_campaign_finance(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                              campaign_finance_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        cf_namespace = campaign_finance_namespace or namespace
        if not table_exists(self.conn, "campaign_finance_revisions"):
            return {"status": "campaign_finance_unavailable", "links": [], "missing_targets": [],
                    "reason": "the Political campaign-finance feature's store is not present", "notice": NOTICE}
        from src.kb.campaign_finance_records import CampaignFinanceStore

        cf = CampaignFinanceStore(self.conn, initialize=False)
        cf_scopes = scopes | {CF_READ}
        links, created, missing, unmatched = [], 0, [], []
        for subject, matches in self._accepted(namespace, scopes, ("campaign-finance:",)):
            if not matches:
                unmatched.append(subject["record_key"])
                continue
            for match in matches:
                registration = cf.records(cf_namespace, scopes=cf_scopes, record_keys=[match["record_key"]])
                if not registration:
                    missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                    "reason": "the accepted committee record is not in the campaign-finance "
                                              "namespace"})
                    continue
                filings = cf.records(cf_namespace, scopes=cf_scopes, kinds=["filing"],
                                     committee_key=match["record_key"])
                targets = filings or registration
                for ad in self._ads_of(namespace, subject, scopes):
                    for target in targets:
                        basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                                 "basis": match["basis"], "committee_key": match["record_key"],
                                 "target_kind": target["record_kind"],
                                 "file_number": target["record"]["fields"].get("file_number"),
                                 "receipt_date": target["record"]["fields"].get("receipt_date"),
                                 "target_citation": {"record_key": target["record_key"],
                                                     "revision_id": target["revision_id"],
                                                     "source_id": target["source_id"],
                                                     "locator": target["record"].get("locator")}}
                        view, new = self._insert(namespace, "campaign-finance", subject["record_key"], ad,
                                                 target["record_key"], cf_namespace, target["revision_id"], basis,
                                                 principal_id)
                        links.append(view)
                        created += new
        return {"status": "linked" if links else "none_on_record", "linked": created, "links": links,
                "missing_targets": missing, "unmatched_subjects": unmatched, "notice": NOTICE}

    def link_elections(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                       elections_namespace: str | None = None, campaign_finance_namespace: str | None = None
                       ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        elections_namespace = elections_namespace or namespace
        if not table_exists(self.conn, "election_contests"):
            return {"status": "elections_unavailable", "links": [], "missing_targets": [],
                    "reason": "the Political elections feature's result store is not present", "notice": NOTICE}
        from src.kb.elections import ElectionError, ElectionStore

        elections = ElectionStore(self.conn, initialize=False)
        lists = {c["record_key"]: c for c in elections.candidates(elections_namespace) if c["kind"] == "list"}
        links, created, missing = [], 0, []
        for subject, matches in self._accepted(namespace, scopes, ("elections:", "campaign-finance:")):
            ads = None
            for match in matches:
                ads = ads if ads is not None else self._ads_of(namespace, subject, scopes)
                if match["record_key"].startswith("elections:"):
                    target = lists.get(match["record_key"])
                    if target is None:
                        missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                        "reason": "the accepted party list is not in the elections namespace"})
                        continue
                    try:
                        election = elections.election(elections_namespace, target["election_id"])
                    except ElectionError:
                        missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                        "reason": "election not on record"})
                        continue
                    contests = self._party_contests(elections, elections_namespace, target)
                    basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                             "via": "party list", "party_list": match["record_key"],
                             "election_id": target["election_id"], "election_name": election.get("name"),
                             "election_date": election.get("election_date"),
                             "contests": [c["contest_id"] for c in contests]}
                    for ad in ads:
                        view, new = self._insert(
                            namespace, "election", subject["record_key"], ad, target["election_id"],
                            elections_namespace, target["first_release_id"],
                            {**basis, "delivery_relative_to_election_day": delivery_relation(
                                ad["record"]["fields"], election.get("election_date"))}, principal_id)
                        links.append(view)
                        created += new
                else:
                    found = self._campaign_finance_contests(match["record_key"], campaign_finance_namespace
                                                            or namespace, scopes)
                    if found is None:
                        missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                        "reason": "no campaign-finance contest link for the accepted committee"})
                        continue
                    for contest_id, cf_links in sorted(found.items()):
                        try:
                            contest = elections.contest(elections_namespace, contest_id)
                            election = elections.election(elections_namespace, contest["election_id"])
                        except ElectionError:
                            missing.append({"subject_key": subject["record_key"], "target_key": contest_id,
                                            "reason": "contest not in the elections namespace"})
                            continue
                        basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                                 "via": "campaign-finance contest link", "committee_key": match["record_key"],
                                 "campaign_finance_links": cf_links, "election_id": contest["election_id"],
                                 "election_name": election.get("name"),
                                 "election_date": election.get("election_date"), "contests": [contest_id]}
                        for ad in ads:
                            view, new = self._insert(
                                namespace, "election", subject["record_key"], ad, contest["election_id"],
                                elections_namespace, election.get("first_release_id"),
                                {**basis, "delivery_relative_to_election_day": delivery_relation(
                                    ad["record"]["fields"], election.get("election_date"))}, principal_id)
                            links.append(view)
                            created += new
        return {"status": "linked" if links else "none_on_record", "linked": created, "links": links,
                "missing_targets": missing, "notice": NOTICE}

    @staticmethod
    def _party_contests(elections, namespace: str, target: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The contests a party list's party stood in (as the election's result files state)."""
        prefix = f"elections:{target['election_id']}:"
        units = set()
        for candidate in elections.candidates(namespace, election_id=target["election_id"]):
            if candidate["kind"] == "list" or _norm(candidate.get("party")) != _norm(target["label"]):
                continue
            parts = candidate["record_key"][len(prefix):].split(":")
            units.add((parts[0], ":".join(parts[1:-2])))
        return [c for c in elections.contests(namespace, election_id=target["election_id"])
                if (c["unit_scheme"], c["unit_native_id"]) in units]

    def _campaign_finance_contests(self, committee_key: str, cf_namespace: str, scopes: set[str]
                                   ) -> dict[str, list[str]] | None:
        if not table_exists(self.conn, "campaign_finance_links"):
            return None
        from src.kb.campaign_finance_links import CampaignFinanceLinks

        try:
            cf_links = CampaignFinanceLinks(self.conn, initialize=False).links(cf_namespace, scopes=scopes | {CF_READ},
                                                                               kind="contest")
        except Exception:  # noqa: BLE001 - the campaign-finance links are optional context
            return None
        found: dict[str, list[str]] = {}
        for link in cf_links:
            basis = link["basis"]
            if committee_key in (basis.get("via_committee"), basis.get("spender"), link["subject_key"]):
                found.setdefault(link["target_key"], []).append(link["link_id"])
        return found or None

    def link_lobbying(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                      lobbying_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        lobbying_namespace = lobbying_namespace or namespace
        if not table_exists(self.conn, "lobbying_revisions"):
            return {"status": "lobbying_unavailable", "links": [], "missing_targets": [],
                    "reason": "the Political lobbying feature's register store is not present", "notice": NOTICE}
        from src.kb.lobbying import LobbyingStore

        register = LobbyingStore(self.conn, initialize=False)
        links, created, missing = [], 0, []
        for subject, matches in self._accepted(namespace, scopes, ("lobbying:",)):
            for match in matches:
                registrant = match["record_key"].split(":client:")[0]
                entry = self.conn.execute("SELECT entry_id, register FROM lobbying_entries WHERE namespace=? AND "
                                          "record_key=?", [lobbying_namespace, registrant]).fetchone()
                revision = register.in_force(lobbying_namespace, entry[0]) if entry else None
                if revision is None:
                    missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                    "reason": "no register entry in force in the lobbying namespace"})
                    continue
                basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                         "register": entry[1], "register_entry": registrant,
                         "register_revision_id": revision["revision_id"],
                         "register_effective_on": revision.get("effective_on")}
                for ad in self._ads_of(namespace, subject, scopes):
                    view, new = self._insert(namespace, "lobbying", subject["record_key"], ad, match["record_key"],
                                             lobbying_namespace, revision["revision_id"], basis, principal_id)
                    links.append(view)
                    created += new
        return {"status": "linked" if links else "none_on_record", "linked": created, "links": links,
                "missing_targets": missing, "notice": NOTICE}

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
              record_key: str | None = None, target_key: str | None = None, subject_key: str | None = None
              ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM platform_transparency_links WHERE namespace=? AND (? IS NULL OR "
            "link_kind=?) AND (? IS NULL OR record_key=?) AND (? IS NULL OR target_key=?) AND (? IS NULL OR "
            "subject_key=?) ORDER BY link_kind, target_key, record_key, link_id",
            [namespace, kind, kind, record_key, record_key, target_key, target_key, subject_key, subject_key]
        ).fetchall()
        return [_link_view(r) for r in rows]
