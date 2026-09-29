"""Bill dossiers linked to lobbying disclosures and enacted Legal works by citation (#2208, LT08).

**Lobbying.** Links reuse :class:`src.kb.lobbying_links.LobbyingDossierLinks`
unchanged: a disclosure is *linked* to a US bill dossier when an LDA issue
description names the bill's number (the ``us-bill`` reference scheme, in the
Congress of the filing year, stated as such); a disclosure that only shares
words with the bill's title is a discovery *candidate* a reviewer accepts or
rejects, and a reviewer may propose one. UK registers (the consultant-lobbyist
register) name no bills, so UK links exist only as reviewed assertions. Every
link cites the register revision and the dossier revision it was made
against. References to bills without a dossier in the namespace are reported
as missing targets, never dropped. Nothing infers influence from a link.

**Enactment.** A published citation - the public-law number congress.gov (or
BILLSTATUS, as its own assertion) states for the bill, or the legislation.gov.uk
Act citation a UK Bills API publication links to - is resolved through
:meth:`src.kb.legal.LegalStore.lookup` by exact identifier. One work: linked
(``enacted_as``, through :meth:`LegalStore.link_dossier` when the Legal and
dossier namespaces agree) and recorded here against the specific record and
dossier revisions with the matching basis. Several works: ``ambiguous``, kept
for review. None, or no Legal store: ``missing_target`` / ``legal_unavailable``,
reported. Nothing is inferred from a bill's title or text.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.legislation import (
    READ_SCOPE,
    WRITE_SCOPE,
    LegislationDossiers,
    LegislationError,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-legislation-link-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS legislation_enactment_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, dossier_namespace TEXT NOT NULL, dossier_id TEXT NOT NULL,
  dossier_revision BIGINT NOT NULL, bill_key TEXT NOT NULL, record_key TEXT NOT NULL, source_id TEXT NOT NULL,
  record_revision_id TEXT NOT NULL, citation TEXT NOT NULL, citation_basis TEXT NOT NULL, status TEXT NOT NULL,
  legal_namespace TEXT, work_id TEXT, legal_link_id TEXT, evidence_json TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""
_UK_ACT = re.compile(r"legislation\.gov\.uk/(ukpga|asp|anaw|asc|nia|ukla)/(\d{4})/(\d+)")


def enactment_citations(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Published enactment citations a record states, with the identifier forms a Legal work may carry."""
    fields = record.get("fields") or {}
    out = []
    if record["record_kind"] in {"us-bill", "us-bill-status"}:
        for law in fields.get("laws") or []:
            number = law.get("number")
            if not number:
                continue
            out.append({"citation": law.get("citation") or f"Pub. L. {number}",
                        "forms": [law.get("citation"), f"Pub. L. {number}", f"Public Law {number}", f"PL {number}",
                                  f"P.L. {number}"],
                        "basis": f"{record['provider']} laws field ({law.get('type')})"})
    elif record["record_kind"] == "uk-publication" and "act" in str(fields.get("publication_type") or "").lower():
        for link in fields.get("links") or []:
            match = _UK_ACT.search(str(link.get("url") or ""))
            if match:
                kind, year, chapter = match.groups()
                out.append({"citation": f"{kind}/{year}/{chapter}",
                            "forms": [f"{kind}/{year}/{chapter}", f"http://www.legislation.gov.uk/{kind}/{year}/{chapter}",
                                      f"https://www.legislation.gov.uk/{kind}/{year}/{chapter}",
                                      f"{year} c. {chapter}"],
                            "basis": "UK Bills API publication link to legislation.gov.uk"})
    for item in out:
        item["forms"] = [f for f in dict.fromkeys(item["forms"]) if f]
    return out


class LegislationLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.dossiers = LegislationDossiers(conn, now=now, initialize=initialize)
        self.store = self.dossiers.store
        self.now = self.dossiers.now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _dossier(self, namespace, bill_key, dossier_namespace, principal_id, scopes) -> dict[str, Any]:
        found = self.dossiers.find(namespace, dossier_namespace, bill_key, principal_id)
        if found is None:
            raise LegislationError("dossier_not_found", "build the bill's dossier first")
        return self.dossiers.dossier(namespace, dossier_namespace, found["dossier_id"], principal_id=principal_id,
                                     scopes=scopes)

    @staticmethod
    def _document_scopes(dossier: Mapping[str, Any]) -> set[str]:
        return {f"document:{s['citation']['document_id']}:read"
                for s in dossier["stages"] + dossier["review_candidates"]}

    # ------------------------------------------------------------------ lobbying

    def link_lobbying(self, namespace: str, bill_key: str, dossier_namespace: str, lobbying_namespace: str, *,
                      principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        dossier = self._dossier(namespace, bill_key, dossier_namespace, principal_id, scopes)
        if not table_exists(self.conn, "lobbying_revisions"):
            return {"status": "lobbying_unavailable", "dossier_id": dossier["dossier_id"],
                    "reason": "the Political lobbying feature's register store is not present"}
        from src.kb.lobbying import LobbyingError
        from src.kb.lobbying_links import LobbyingDossierLinks

        try:
            result = LobbyingDossierLinks(self.conn, now=self.now).link_dossier(
                lobbying_namespace, dossier_namespace, dossier["dossier_id"], principal_id=principal_id,
                scopes=scopes | self._document_scopes(dossier))
            missing = self.missing_lobbying_targets(namespace, lobbying_namespace, dossier_namespace,
                                                    principal_id=principal_id, scopes=scopes)
        except LobbyingError as exc:
            raise LegislationError(exc.code, str(exc)) from exc
        return {"status": "linked" if result["links"] else "none_on_record", "dossier_id": dossier["dossier_id"],
                "dossier_revision": dossier["revision"], "linked": result["linked"],
                "candidates": result["candidates"], "links": result["links"], "missing_targets": missing,
                "notice": "a disclosure naming a bill is not evidence of influence"}

    def missing_lobbying_targets(self, namespace: str, lobbying_namespace: str, dossier_namespace: str, *,
                                 principal_id: str, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Bill references in disclosures in force whose bill has no dossier here: reported, never dropped."""
        from src.kb.lobbying import READ_SCOPE as LOBBYING_READ
        from src.kb.lobbying import LobbyingStore
        from src.kb.lobbying import authorize as lobbying_authorize

        lobbying_authorize(lobbying_namespace, set(scopes), LOBBYING_READ)
        known = {r[0] for r in self.conn.execute(
            "SELECT bill_key FROM legislation_dossier_index WHERE namespace=? AND dossier_namespace=? AND owner=?",
            [namespace, dossier_namespace, principal_id]).fetchall()}
        store = LobbyingStore(self.conn, initialize=False)
        out = []
        for (entry_id,) in self.conn.execute(
                "SELECT DISTINCT entry_id FROM lobbying_revisions WHERE namespace=? ORDER BY entry_id",
                [lobbying_namespace]).fetchall():
            revision = store.in_force(lobbying_namespace, entry_id)
            if revision is None or revision["statement"] is None:
                continue
            for interest in store.interests(revision):
                for ref in interest["references"]:
                    if ref.get("scheme") != "us-bill" or ref["key"] in known:
                        continue
                    out.append({"status": "missing_target", "reference": ref["key"],
                                "as_written": ref.get("as_written"), "register": revision["register"],
                                "native_id": revision["native_id"], "revision_id": revision["revision_id"],
                                "interest_key": interest["interest_key"],
                                "note": "the disclosure names a bill with no dossier in this namespace"})
        return out

    # ------------------------------------------------------------------ enactment

    def link_enactment(self, namespace: str, bill_key: str, dossier_namespace: str, *, principal_id: str,
                       scopes: Iterable[str], legal_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        dossier = self._dossier(namespace, bill_key, dossier_namespace, principal_id, scopes)
        legal_namespace = legal_namespace or dossier_namespace
        found = self.store.records_for_bill(namespace, bill_key, scopes=scopes)["linked"]
        results = []
        for row in found:
            for cited in enactment_citations(row["record"] or {}):
                results.append(self._enactment(namespace, dossier_namespace, dossier, bill_key, row, cited,
                                               legal_namespace, principal_id, scopes))
        return {"dossier_id": dossier["dossier_id"], "dossier_revision": dossier["revision"],
                "status": "none_on_record" if not results else "reported", "links": results,
                "notice": "an enactment link follows a published citation; legal effect is a Legal review step"}

    def _enactment(self, namespace, dossier_namespace, dossier, bill_key, row, cited, legal_namespace,
                   principal_id, scopes) -> dict[str, Any]:
        status, works, legal_link_id, lookup_status = "legal_unavailable", [], None, None
        if table_exists(self.conn, "legal_works"):
            from src.kb.legal import LegalError, LegalStore

            legal = LegalStore(self.conn, initialize=False, now=self.now)
            try:
                for form in cited["forms"]:
                    answer = legal.lookup(legal_namespace, scopes=scopes, identifier=form)
                    lookup_status = answer["status"]
                    if answer["works"]:
                        works = answer["works"]
                        break
            except LegalError as exc:
                raise LegislationError(exc.code, str(exc)) from exc
            unique = {w["work_id"] for w in works}
            status = "linked" if len(unique) == 1 else "ambiguous" if unique else "missing_target"
            if status == "linked" and legal_namespace == dossier_namespace:
                try:
                    legal_link_id = legal.link_dossier(
                        legal_namespace, dossier["dossier_id"], works[0]["work_id"], "enacted_as",
                        f"{cited['citation']} ({cited['basis']}; {row['record_key']} revision {row['revision_id']})",
                        scopes=scopes, principal_id=principal_id)["link_id"]
                except LegalError as exc:
                    raise LegislationError(exc.code, str(exc)) from exc
        link_id = "legislation-enactment:" + digest([namespace, dossier_namespace, dossier["dossier_id"],
                                                     dossier["revision"], row["record_key"], row["source_id"],
                                                     cited["citation"]])[:24]
        evidence = {"forms_tried": cited["forms"], "lookup_status": lookup_status,
                    "works": [{"work_id": w["work_id"], "jurisdiction": w["jurisdiction"],
                               "matched_by": w.get("matched_by")} for w in works]}
        prior = self.conn.execute("SELECT status, evidence_json FROM legislation_enactment_links WHERE namespace=? "
                                  "AND link_id=?", [namespace, link_id]).fetchone()
        if prior and prior[0] == status:
            return self.enactment_link(namespace, link_id)
        if prior:
            # A later run resolved differently (a Legal provider appeared, a target was added): keep the trail.
            evidence["previous"] = [*json.loads(prior[1]).get("previous", []),
                                    {"status": prior[0], "evidence": {k: v for k, v in json.loads(prior[1]).items()
                                                                     if k != "previous"}}]
            self.conn.execute("DELETE FROM legislation_enactment_links WHERE namespace=? AND link_id=?",
                              [namespace, link_id])
        self.conn.execute(
            "INSERT INTO legislation_enactment_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, link_id, dossier_namespace, dossier["dossier_id"], dossier["revision"], bill_key,
             row["record_key"], row["source_id"], row["revision_id"], cited["citation"], cited["basis"], status,
             legal_namespace, works[0]["work_id"] if status == "linked" else None, legal_link_id, canonical(evidence),
             principal_id, self.now()])
        return self.enactment_link(namespace, link_id)

    def enactment_link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, dossier_namespace, dossier_id, dossier_revision, bill_key, record_key, source_id, "
            "record_revision_id, citation, citation_basis, status, legal_namespace, work_id, legal_link_id, "
            "evidence_json, created_by, created_at_ms FROM legislation_enactment_links WHERE namespace=? AND link_id=?",
            [namespace, link_id]).fetchone()
        if row is None:
            raise LegislationError("not_found", "no such enactment link")
        view = dict(zip(("link_id", "dossier_namespace", "dossier_id", "dossier_revision", "bill_key", "record_key",
                         "source_id", "record_revision_id", "citation", "citation_basis", "status",
                         "legal_namespace", "work_id", "legal_link_id"), row[:14]))
        view.update(evidence=json.loads(row[14]), created_by=row[15], created_at_ms=row[16],
                    relation="enacted_as" if view["status"] == "linked" else None, contract=CONTRACT)
        return view

    def enactment_links(self, namespace: str, dossier_namespace: str, dossier_id: str, *,
                        dossier_revision: int | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "legislation_enactment_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM legislation_enactment_links WHERE namespace=? AND dossier_namespace=? AND "
            "dossier_id=? AND (? IS NULL OR dossier_revision<=?) ORDER BY dossier_revision, record_key, citation",
            [namespace, dossier_namespace, dossier_id, dossier_revision, dossier_revision]).fetchall()
        return [self.enactment_link(namespace, r[0]) for r in rows]


def lobbying_link_for(links: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The linked or reviewed lobbying links only (candidates stay out of answers' link lists)."""
    return [dict(link) for link in links if link.get("state") in {"linked", "accepted"}]
