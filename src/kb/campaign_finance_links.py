"""Filings and line items linked to election contests, lobbying records and corporate ownership (#2209, CF08).

Every link points at a specific record revision (a filing version, a line item
or a committee registration) and at the filing revision it was reported in,
and records its matching basis. Links rest only on accepted, unreverted CF07
identity decisions (:mod:`src.kb.campaign_finance_identity`) plus the
identifiers the records publish:

* **contests** - an FEC candidate accepted as the same person as an elections
  candidate record (name, office and a published election year) links the
  filing versions of the committees whose registration lists that candidate id,
  and the independent expenditures naming that candidate id (support/oppose
  verbatim), to the contest(s) of that record; a Commission party accepted as
  the same party as an election's party list links its spending returns to the
  contests the party stood in, only when the return's election name as
  published equals the election's published name;
* **lobbying** - an organisational donor accepted as a lobbying registrant or
  client links its contributions to the register revision in force, together
  with the dossier links :class:`src.kb.lobbying_links.LobbyingDossierLinks`
  holds for that revision;
* **ownership** - an organisational donor or a connected organisation accepted
  as a Corporate Ownership record links its contributions (or the committee
  registration stating the connection) to that record's revision.

Accepted links whose target is not on record, and providers that are absent,
are reported, never dropped. A link states a shared identifier or a reviewed
match; nothing infers influence, a quid pro quo or an undisclosed funder from
co-occurring links.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.campaign_finance_identity import CampaignFinanceIdentity, _norm, donor_key
from src.kb.campaign_finance_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-campaign-finance-link-v1"
LINK_KINDS = ("contest", "lobbying", "ownership")
NOTICE = ("a link states a shared identifier or a reviewed identity match; it is not evidence of influence, a quid "
          "pro quo or an undisclosed funder")
_DDL = """
CREATE TABLE IF NOT EXISTS campaign_finance_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, link_kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  record_key TEXT NOT NULL, record_revision_id TEXT NOT NULL, filing_key TEXT, filing_revision_id TEXT,
  target_key TEXT NOT NULL, target_namespace TEXT NOT NULL, target_revision TEXT, basis_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""
_COLUMNS = ("link_id", "link_kind", "subject_key", "record_key", "record_revision_id", "filing_key",
            "filing_revision_id", "target_key", "target_namespace", "target_revision", "basis_json", "created_by",
            "created_at_ms")
ITEM_KINDS = ["contribution", "expenditure", "independent-expenditure"]


def _link_view(row) -> dict[str, Any]:
    view = dict(zip(_COLUMNS, row))
    view["basis"] = json.loads(view.pop("basis_json"))
    return {"contract": CONTRACT, **view, "notice": NOTICE}


class CampaignFinanceLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.identity = CampaignFinanceIdentity(conn, now=now, initialize=initialize)
        self.store = self.identity.store
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "campaign_finance_links")

    # ------------------------------------------------------------------ writes

    def _insert(self, namespace: str, kind: str, subject_key: str, row: Mapping[str, Any], target_key: str,
                target_namespace: str, target_revision: str | None, basis: Mapping[str, Any], principal_id: str
                ) -> tuple[dict[str, Any], bool]:
        record = row["record"]
        if row["record_kind"] == "filing":
            filing_key, filing_revision = row["record_key"], row["revision_id"]
        else:
            filing_key, filing_revision = record.get("filing_key"), row.get("filing_revision_id")
        link_id = "cf-link:" + digest([namespace, kind, row["revision_id"], target_key, target_namespace,
                                       target_revision])[:24]
        created = not self.conn.execute("SELECT 1 FROM campaign_finance_links WHERE namespace=? AND link_id=?",
                                        [namespace, link_id]).fetchone()
        if created:
            self.conn.execute("INSERT INTO campaign_finance_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, link_id, kind, subject_key, row["record_key"], row["revision_id"],
                               filing_key, filing_revision, target_key, target_namespace, target_revision,
                               canonical(basis), principal_id, self.now()])
        found = self.conn.execute("SELECT " + ", ".join(_COLUMNS) + " FROM campaign_finance_links WHERE "
                                  "namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        return _link_view(found), created

    def _items_by_donor(self, namespace: str, scopes: set[str]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.records(namespace, scopes=scopes, kinds=["contribution"]):
            key = donor_key(row["record"])
            if key:
                out.setdefault(key, []).append(row)
        return out

    def _subject_records(self, namespace: str, subject: Mapping[str, Any], items: Mapping[str, list],
                         scopes: set[str]) -> list[dict[str, Any]]:
        """The records a subject's link attaches to: its contributions, or the registration stating a connection."""
        if subject["kind"] == "connected-organisation":
            return self.store.records(namespace, scopes=scopes, kinds=["committee"],
                                      record_keys=[subject["stated_by"]])
        return list(items.get(subject["record_key"]) or [])

    def link_contests(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                      elections_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        elections_namespace = elections_namespace or namespace
        if not table_exists(self.conn, "election_contests"):
            return {"status": "elections_unavailable", "links": [], "missing_targets": [],
                    "reason": "the Political elections feature's result store is not present", "notice": NOTICE}
        from src.kb.elections import ElectionError, ElectionStore

        elections = ElectionStore(self.conn, initialize=False)
        by_key = {c["record_key"]: c for c in elections.candidates(elections_namespace)}
        links, created, missing, unmatched = [], 0, [], []
        committees = self.store.records(namespace, scopes=scopes, kinds=["committee"])
        filings = self.store.records(namespace, scopes=scopes, kinds=["filing"])
        spending = self.store.records(namespace, scopes=scopes, kinds=["independent-expenditure"])
        for subject in self.identity.subjects(namespace, scopes=scopes):
            if subject["kind"] not in {"candidate", "regulated-entity"}:
                continue
            accepted = [a for a in self.identity.accepted(namespace, subject["record_key"], scopes=scopes)
                        if a["record_key"].startswith("elections:")]
            if not accepted:
                unmatched.append(subject["record_key"])
                continue
            for match in accepted:
                target = by_key.get(match["record_key"])
                if target is None:
                    missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                    "reason": "the accepted election record is not in the elections namespace"})
                    continue
                try:
                    election = elections.election(elections_namespace, target["election_id"])
                except ElectionError:
                    missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                    "reason": "election not on record"})
                    continue
                contests = self._contests_for(elections, elections_namespace, target)
                basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                         "election_id": target["election_id"], "election_record": match["record_key"]}
                if subject["kind"] == "candidate":
                    fec_id = subject["fec_id"]
                    own = {c["record_key"]: c for c in committees
                           if fec_id in (c["record"]["fields"].get("candidate_ids") or [])}
                    rows = [(f, {"via_committee": f["committee_key"],
                                 "designation_as_published": own[f["committee_key"]]["record"]["fields"].get(
                                     "designation"),
                                 "candidate_id_on_registration": fec_id})
                            for f in filings if f["committee_key"] in own]
                    rows += [(ie, {"candidate_id_as_published": ie["record"]["fields"].get("candidate_id"),
                                   "support_oppose_as_published": ie["record"]["fields"].get(
                                       "support_oppose_indicator"),
                                   "spender": ie["committee_key"]})
                             for ie in spending if ie["record"]["fields"].get("candidate_id") == fec_id]
                    basis.update(office_as_published=subject.get("office"),
                                 election_years_as_published=subject.get("election_years"))
                else:
                    rows = []
                    for filing in filings:
                        fields = filing["record"]["fields"]
                        if filing["committee_key"] != subject["record_key"] or fields.get("return_kind") != "spending":
                            continue
                        if not election.get("name") or _norm(fields.get("election_name")) != _norm(election["name"]):
                            missing.append({"subject_key": subject["record_key"], "record_key": filing["record_key"],
                                            "target_key": target["election_id"],
                                            "reason": "the return's election name as published does not equal the "
                                                      "election's published name (or the election states none)"})
                            continue
                        rows.append((filing, {"election_name_as_published": fields.get("election_name"),
                                              "party_list": match["record_key"]}))
                for contest in contests:
                    for row, extra in rows:
                        view, new = self._insert(namespace, "contest", subject["record_key"], row,
                                                 contest["contest_id"], elections_namespace,
                                                 contest["first_release_id"],
                                                 {**basis, **extra, "contest_unit": [contest["unit_scheme"],
                                                                                     contest["unit_native_id"]]},
                                                 principal_id)
                        links.append(view)
                        created += new
        return {"status": "linked" if links else "none_on_record", "linked": created, "links": links,
                "missing_targets": missing, "unmatched_subjects": unmatched, "notice": NOTICE}

    @staticmethod
    def _contests_for(elections, namespace: str, target: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The contests an election record belongs to: its own unit, or every unit a party list's party stood in."""
        prefix = f"elections:{target['election_id']}:"
        contests = elections.contests(namespace, election_id=target["election_id"])
        if target["kind"] == "list":
            units = set()
            for candidate in elections.candidates(namespace, election_id=target["election_id"]):
                if candidate["kind"] == "list" or _norm(candidate.get("party")) != _norm(target["label"]):
                    continue
                parts = candidate["record_key"][len(prefix):].split(":")
                units.add((parts[0], ":".join(parts[1:-2])))
        else:
            parts = target["record_key"][len(prefix):].split(":")
            units = {(parts[0], ":".join(parts[1:-2]))}
        return [c for c in contests if (c["unit_scheme"], c["unit_native_id"]) in units]

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
        items = self._items_by_donor(namespace, scopes)
        links, created, missing = [], 0, []
        for subject in self.identity.subjects(namespace, scopes=scopes):
            for match in self.identity.accepted(namespace, subject["record_key"], scopes=scopes):
                if not match["record_key"].startswith("lobbying:"):
                    continue
                registrant = match["record_key"].split(":client:")[0]
                entry = self.conn.execute("SELECT entry_id, register FROM lobbying_entries WHERE namespace=? AND "
                                          "record_key=?", [lobbying_namespace, registrant]).fetchone()
                revision = register.in_force(lobbying_namespace, entry[0]) if entry else None
                if revision is None:
                    missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                    "reason": "no register entry in force in the lobbying namespace"})
                    continue
                dossier_links = self._dossier_links(lobbying_namespace, revision["revision_id"], scopes)
                basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                         "register": entry[1], "register_entry": registrant,
                         "register_revision_id": revision["revision_id"], "register_effective_on":
                         revision.get("effective_on"), "dossier_links": dossier_links}
                for row in self._subject_records(namespace, subject, items, scopes):
                    view, new = self._insert(namespace, "lobbying", subject["record_key"], row, match["record_key"],
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
            return {"status": "ownership_unavailable", "links": [], "missing_targets": [],
                    "reason": "the Corporate Ownership store is not present", "notice": NOTICE}
        from src.kb.ownership_store import OwnershipStore

        owned = OwnershipStore(self.conn, initialize=False)
        items = self._items_by_donor(namespace, scopes)
        links, created, missing = [], 0, []
        for subject in self.identity.subjects(namespace, scopes=scopes):
            if subject["kind"] not in {"donor-organisation", "connected-organisation"}:
                continue
            for match in self.identity.accepted(namespace, subject["record_key"], scopes=scopes):
                if match["record_key"].startswith(("lobbying:", "elections:", "campaign-finance:")):
                    continue
                view = owned.by_key(ownership_namespace, match["record_key"], principal_id=principal_id,
                                    scopes=scopes)
                if view is None:
                    missing.append({"subject_key": subject["record_key"], "target_key": match["record_key"],
                                    "reason": "the accepted ownership record is not in the ownership namespace"})
                    continue
                body = view["record"]
                basis = {"identity_candidate_id": match["candidate_id"], "method": match["method"],
                         "ownership_record_id": view["record_id"], "ownership_revision": view["revision"],
                         "name": body.get("name"), "jurisdiction": body.get("jurisdiction"),
                         "relation": subject.get("relation") or "donor accepted as this legal entity"}
                for row in self._subject_records(namespace, subject, items, scopes):
                    link, new = self._insert(namespace, "ownership", subject["record_key"], row, match["record_key"],
                                             ownership_namespace, f"{view['record_id']}@{view['revision']}", basis,
                                             principal_id)
                    links.append(link)
                    created += new
        present = bool(owned.records(ownership_namespace, principal_id=principal_id, scopes=scopes,
                                     kinds=("legal_entity",)))
        status = "linked" if links else ("none_on_record" if present else "ownership_unavailable")
        return {"status": status, "linked": created, "links": links, "missing_targets": missing, "notice": NOTICE,
                **({} if present else {"reason": "no Corporate Ownership records in the ownership namespace"})}

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
              record_key: str | None = None, target_key: str | None = None, subject_key: str | None = None
              ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_COLUMNS) + " FROM campaign_finance_links WHERE namespace=? AND (? IS NULL OR "
            "link_kind=?) AND (? IS NULL OR record_key=?) AND (? IS NULL OR target_key=?) AND (? IS NULL OR "
            "subject_key=?) ORDER BY link_kind, target_key, record_key, link_id",
            [namespace, kind, kind, record_key, record_key, target_key, target_key, subject_key, subject_key]
        ).fetchall()
        return [_link_view(r) for r in rows]
