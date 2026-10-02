"""Political ads linked to elections, campaign-finance records, lobbying registers and ownership (#2580, SP08).

Every link points at a specific ad *revision* and at the target's revision,
and records its basis. Links rest only on published identifiers, declared
selections and accepted, unreverted SP07 identity decisions
(:mod:`src.kb.platform_transparency_identity`):

* **election** - an ad acquired under a selection that declares an elections
  id (Meta: the unit's ``election_id``), or whose advertiser's published
  election label (Google ``Elections``) the selection maps to an elections id,
  links to that election record with the ad's delivery period and the
  election date side by side (basis ``declared-selection`` or
  ``published-election-label``);
* **campaign-finance** - an advertiser or funding entity accepted as a
  campaign-finance committee or regulated entity links its ads to that
  registration's revision, listing the filing versions on record for it;
* **lobbying** - an advertiser or funding entity accepted as a lobbying
  registrant or client links its ads to the register revision in force, with
  the dossier links :class:`src.kb.lobbying_links.LobbyingDossierLinks` holds;
* **ownership** - an advertiser or funding entity accepted as a Corporate
  Ownership legal entity links its ads to that record's revision.

Absent providers and accepted targets that are not on record are reported,
never dropped. A link states a shared identifier, a declared selection or a
reviewed match; nothing infers coordination, influence or an undisclosed
funder from co-occurring links.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.platform_transparency_identity import (
    PlatformTransparencyIdentity,
    funding_key,
)
from src.kb.platform_transparency_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-platform-transparency-link-v1"
LINK_KINDS = ("election", "campaign-finance", "lobbying", "ownership")
NOTICE = ("a link states a published identifier, a declared selection or a reviewed identity match; it is not "
          "evidence of coordination, influence or an undisclosed funder")
_DDL = """
CREATE TABLE IF NOT EXISTS platform_transparency_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, link_kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  record_key TEXT NOT NULL, record_revision_id TEXT NOT NULL, target_key TEXT NOT NULL, target_namespace TEXT NOT NULL,
  target_revision TEXT, basis_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""
_COLUMNS = ("link_id", "link_kind", "subject_key", "record_key", "record_revision_id", "target_key",
            "target_namespace", "target_revision", "basis_json", "created_by", "created_at_ms")


def _link_view(row) -> dict[str, Any]:
    view = dict(zip(_COLUMNS, row))
    view["basis"] = json.loads(view.pop("basis_json"))
    return {"contract": CONTRACT, **view, "notice": NOTICE}


def _period(fields: Mapping[str, Any]) -> dict[str, Any]:
    return {"delivery_start": fields.get("delivery_start"), "delivery_stop": fields.get("delivery_stop")}


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
        ads = self.store.records(namespace, scopes=scopes, kinds=["ad"])
        if subject["kind"] == "advertiser":
            return [a for a in ads if a["advertiser_key"] == subject["record_key"]]
        return [a for a in ads if any(funding_key(b) == subject["record_key"]
                                      for b in a["record"]["fields"].get("funding_entity_as_declared") or [])]

    def _accepted_targets(self, namespace: str, scopes: set[str], prefixes: tuple[str, ...] | None,
                          exclude: tuple[str, ...] = ()) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        out = []
        for subject in self.identity.subjects(namespace, scopes=scopes):
            for match in self.identity.accepted(namespace, subject["record_key"], scopes=scopes):
                key = match["record_key"]
                if prefixes and not key.startswith(prefixes):
                    continue
                if exclude and key.startswith(exclude):
                    continue
                out.append((subject, match))
        return out

    def link_elections(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                       elections_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        elections_namespace = elections_namespace or namespace
        if not table_exists(self.conn, "election_elections"):
            return {"status": "elections_unavailable", "links": [], "missing_targets": [], "notice": NOTICE,
                    "reason": "the Political elections feature's result store is not present"}
        from src.kb.elections import ElectionError, ElectionStore

        elections = ElectionStore(self.conn, initialize=False)
        advertisers = {r["record_key"]: r for r in self.store.records(namespace, scopes=scopes, kinds=["advertiser"])}
        links, created, missing = [], 0, []
        for ad in self.store.records(namespace, scopes=scopes, kinds=["ad"]):
            fields = ad["record"]["fields"]
            declared = []
            if fields.get("election_id_declared"):
                declared.append((fields["election_id_declared"], {"basis": "declared-selection",
                                                                   "selection_key": ad["selection_key"]}))
            advertiser = advertisers.get(ad["advertiser_key"])
            labels = (advertiser or {}).get("record", {}).get("fields", {}).get("elections_as_published") or []
            for election_id in fields.get("election_ids_declared") or []:
                declared.append((election_id, {"basis": "published-election-label", "labels_as_published": labels,
                                               "advertiser_revision_id": (advertiser or {}).get("revision_id")}))
            for election_id, basis in declared:
                try:
                    election = elections.election(elections_namespace, election_id)
                except ElectionError:
                    missing.append({"record_key": ad["record_key"], "target_key": election_id,
                                    "reason": "election not on record in the elections namespace"})
                    continue
                view, new = self._insert(namespace, "election", ad["advertiser_key"] or ad["record_key"], ad,
                                         election_id, elections_namespace, election.get("first_release_id"),
                                         {**basis, "election_name": election.get("name"),
                                          "election_date": election.get("election_date"),
                                          "ad_period": _period(fields)}, principal_id)
                links.append(view)
                created += new
        return {"status": "linked" if links else "none_on_record", "linked": created, "links": links,
                "missing_targets": missing, "notice": NOTICE}

    def link_campaign_finance(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                              campaign_finance_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        target_namespace = campaign_finance_namespace or namespace
        if not table_exists(self.conn, "campaign_finance_records"):
            return {"status": "campaign_finance_unavailable", "links": [], "missing_targets": [], "notice": NOTICE,
                    "reason": "the Political campaign-finance store is not present"}
        from src.kb.campaign_finance_records import CampaignFinanceStore

        store = CampaignFinanceStore(self.conn, initialize=False)
        links, created, missing = [], 0, []
        for subject, match in self._accepted_targets(namespace, scopes, ("campaign-finance:",)):
            found = store.records(target_namespace, scopes=scopes, record_keys=[match["record_key"]])
            if not found:
                missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                "reason": "the accepted campaign-finance record is not in the namespace"})
                continue
            target = found[0]
            filings = [{"filing_key": f["record_key"], "revision_id": f["revision_id"],
                        "coverage_start": f["record"]["fields"].get("coverage_start_date"),
                        "coverage_end": f["record"]["fields"].get("coverage_end_date"),
                        "receipt_date": f["record"]["fields"].get("receipt_date")}
                       for f in store.records(target_namespace, scopes=scopes, kinds=["filing"],
                                              committee_key=match["record_key"])]
            basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                     "basis": match["basis"], "registration_revision_id": target["revision_id"],
                     "filings_on_record": filings}
            for ad in self._ads_of(namespace, subject, scopes):
                view, new = self._insert(namespace, "campaign-finance", subject["record_key"], ad,
                                         match["record_key"], target_namespace, target["revision_id"],
                                         {**basis, "ad_period": _period(ad["record"]["fields"])}, principal_id)
                links.append(view)
                created += new
        return {"status": "linked" if links else "none_on_record", "linked": created, "links": links,
                "missing_targets": missing, "notice": NOTICE}

    def link_lobbying(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                      lobbying_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        lobbying_namespace = lobbying_namespace or namespace
        if not table_exists(self.conn, "lobbying_revisions"):
            return {"status": "lobbying_unavailable", "links": [], "missing_targets": [], "notice": NOTICE,
                    "reason": "the Political lobbying feature's register store is not present"}
        from src.kb.lobbying import LobbyingStore

        register = LobbyingStore(self.conn, initialize=False)
        links, created, missing = [], 0, []
        for subject, match in self._accepted_targets(namespace, scopes, ("lobbying:",)):
            registrant = match["record_key"].split(":client:")[0]
            entry = self.conn.execute("SELECT entry_id, register FROM lobbying_entries WHERE namespace=? AND "
                                      "record_key=?", [lobbying_namespace, registrant]).fetchone()
            revision = register.in_force(lobbying_namespace, entry[0]) if entry else None
            if revision is None:
                missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                "reason": "no register entry in force in the lobbying namespace"})
                continue
            basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                     "basis": match["basis"], "register": entry[1], "register_entry": registrant,
                     "register_revision_id": revision["revision_id"],
                     "dossier_links": self._dossier_links(lobbying_namespace, revision["revision_id"], scopes)}
            for ad in self._ads_of(namespace, subject, scopes):
                view, new = self._insert(namespace, "lobbying", subject["record_key"], ad, match["record_key"],
                                         lobbying_namespace, revision["revision_id"], basis, principal_id)
                links.append(view)
                created += new
        return {"status": "linked" if links else "none_on_record", "linked": created, "links": links,
                "missing_targets": missing, "notice": NOTICE}

    def _dossier_links(self, namespace: str, revision_id: str, scopes: set[str]) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "lobbying_dossier_links"):
            return []
        from src.kb.lobbying_links import LobbyingDossierLinks

        try:
            found = LobbyingDossierLinks(self.conn, initialize=False).links_for_revision(namespace, revision_id,
                                                                                         scopes=scopes)
        except Exception:  # noqa: BLE001 - dossier links are optional context
            return []
        return [{"link_id": link["link_id"], "dossier_id": link.get("dossier_id"), "state": link.get("state")}
                for link in found]

    def link_ownership(self, namespace: str, ownership_namespace: str, *, principal_id: str,
                       scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "ownership_records"):
            return {"status": "ownership_unavailable", "links": [], "missing_targets": [], "notice": NOTICE,
                    "reason": "the Corporate Ownership store is not present"}
        from src.kb.ownership_store import OwnershipStore

        owned = OwnershipStore(self.conn, initialize=False)
        links, created, missing = [], 0, []
        for subject, match in self._accepted_targets(namespace, scopes, None,
                                                     exclude=("lobbying:", "elections:", "campaign-finance:",
                                                              "platform-transparency:")):
            view = owned.by_key(ownership_namespace, match["record_key"], principal_id=principal_id, scopes=scopes)
            if view is None:
                missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                "reason": "the accepted ownership record is not in the ownership namespace"})
                continue
            body = view["record"]
            basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                     "basis": match["basis"], "ownership_record_id": view["record_id"],
                     "ownership_revision": view["revision"], "name": body.get("name"),
                     "jurisdiction": body.get("jurisdiction"),
                     "relation": f"{subject['kind']} accepted as this legal entity"}
            for ad in self._ads_of(namespace, subject, scopes):
                link, new = self._insert(namespace, "ownership", subject["record_key"], ad, match["record_key"],
                                         ownership_namespace, f"{view['record_id']}@{view['revision']}", basis,
                                         principal_id)
                links.append(link)
                created += new
        return {"status": "linked" if links else "none_on_record", "linked": created, "links": links,
                "missing_targets": missing, "notice": NOTICE}

    def link_all(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                 elections_namespace: str | None = None, campaign_finance_namespace: str | None = None,
                 lobbying_namespace: str | None = None, ownership_namespace: str | None = None) -> dict[str, Any]:
        """Every link kind; each reports its own status, and an absent provider degrades only its own kind."""
        scopes = set(scopes)
        out = {
            "election": self.link_elections(namespace, principal_id=principal_id, scopes=scopes,
                                            elections_namespace=elections_namespace),
            "campaign-finance": self.link_campaign_finance(namespace, principal_id=principal_id, scopes=scopes,
                                                           campaign_finance_namespace=campaign_finance_namespace),
            "lobbying": self.link_lobbying(namespace, principal_id=principal_id, scopes=scopes,
                                           lobbying_namespace=lobbying_namespace),
        }
        out["ownership"] = (self.link_ownership(namespace, ownership_namespace, principal_id=principal_id,
                                                scopes=scopes) if ownership_namespace else
                            {"status": "ownership_unavailable", "links": [], "missing_targets": [],
                             "reason": "no ownership namespace given", "notice": NOTICE})
        return {"kinds": {k: {key: v for key, v in r.items() if key != "links"} | {"linked_total": len(r["links"])}
                          for k, r in out.items()},
                "unavailable": sorted(k for k, r in out.items() if r["status"].endswith("_unavailable")),
                "notice": NOTICE}

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
