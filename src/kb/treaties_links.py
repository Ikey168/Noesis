"""Treaty records linked to other packs by citation, shared identifier or accepted match (#2581, TR07).

Every link records its **basis** and points at **specific record revisions**
on both sides (the treaty revision, and the target's work version, list
snapshot or identity assertion):

* ``eu-act`` (Legal ``legislation-regulation``, :mod:`src.kb.legal`) - an EU act
  CELLAR links to an agreement (``eu_acts`` of the agreement record) resolved
  to a Legal work by exact CELEX through :meth:`src.kb.legal.LegalStore.lookup`;
  basis ``citation``. The act's role (signing, concluding, implementing) is
  never inferred: the CELLAR relation is quoted.
* ``legal-work`` - the agreement itself acquired as a Legal work (same CELEX
  or ELI); basis ``shared-identifier``.
* ``sanctions-legal-basis`` (Legal ``sanctions``, :mod:`src.kb.sanctions`) - a
  list's legal-basis citation that states the treaty's CELEX or ELI, or cites
  its CETS/ETS, UNTS or MTDSG number exactly; basis ``citation``. A basis
  citing a treaty number with no treaty record here is a ``missing_target``.
* ``participant-trade-reporter`` (Economics ``trade``, :mod:`src.kb.trade_identity`)
  - a participant whose accepted TR06 place match is the place an accepted
  trade identity assertion maps a reporter/partner area code to; basis
  ``accepted-match``. Nothing says the treaty governs that trade.

A missing target or an absent provider is reported (``missing_target``,
``provider_unavailable``), never dropped. Links are recomputed idempotently;
a link made against an earlier revision stays as history.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.treaties_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    TreatiesError,
    authorize,
    canonical,
    digest,
    load,
    table_exists,
)

CONTRACT = "noesis-treaty-link-v1"
LINK_KINDS = ("eu-act", "legal-work", "sanctions-legal-basis", "participant-trade-reporter")
_DDL = """
CREATE TABLE IF NOT EXISTS treaty_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, link_kind TEXT NOT NULL, treaty_key TEXT NOT NULL,
  treaty_revision_id TEXT NOT NULL, subject_key TEXT, target_pack TEXT NOT NULL, target_key TEXT NOT NULL,
  target_revision TEXT, basis TEXT NOT NULL, status TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


class TreatiesLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.kb.treaties_identity import TreatiesIdentity

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.identity = TreatiesIdentity(conn, now=self.now, initialize=initialize)
        self.store = self.identity.store
        if initialize:
            conn.execute(_DDL)

    def _record(self, namespace: str, kind: str, treaty_row: Mapping[str, Any], subject: str | None, target_pack: str,
                target_key: str, target_revision: str | None, basis: str, status: str, evidence: Mapping[str, Any],
                principal_id: str) -> dict[str, Any]:
        link_id = "treaty-link:" + digest([namespace, kind, treaty_row["revision_id"], subject, target_key,
                                           target_revision, status])[:24]
        if not self.conn.execute("SELECT 1 FROM treaty_links WHERE namespace=? AND link_id=?",
                                 [namespace, link_id]).fetchone():
            self.conn.execute("INSERT INTO treaty_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, link_id, kind, treaty_row["record_key"], treaty_row["revision_id"], subject,
                               target_pack, target_key, target_revision, basis, status, canonical(dict(evidence)),
                               principal_id, self.now()])
        return self.link(namespace, link_id)

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, link_kind, treaty_key, treaty_revision_id, subject_key, target_pack, target_key, "
            "target_revision, basis, status, evidence_json, created_by, created_at_ms FROM treaty_links WHERE "
            "namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise TreatiesError("not_found", "no such treaty link")
        return {"contract": CONTRACT, **dict(zip(("link_id", "link_kind", "treaty_key", "treaty_revision_id",
                                                  "subject_key", "target_pack", "target_key", "target_revision",
                                                  "basis", "status"), row[:10])),
                "evidence": load(row[10], {}), "created_by": row[11], "created_at_ms": row[12]}

    # ------------------------------------------------------------------ build

    def link_all(self, namespace: str, *, principal_id: str, scopes: Iterable[str], legal_namespace: str | None = None,
                 sanctions_namespace: str | None = None, trade_namespace: str | None = None) -> dict[str, Any]:
        """Link every current treaty revision; idempotent. Absent providers and missing targets are reported."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        out: list[dict[str, Any]] = []
        unavailable: dict[str, str] = {}
        treaties = [r for r in (self.store.as_of(namespace, k) for k in self.store.treaty_keys(namespace)) if r]
        out += self._legal(namespace, treaties, legal_namespace or namespace, principal_id, scopes, unavailable)
        out += self._sanctions(namespace, treaties, sanctions_namespace or namespace, principal_id, unavailable)
        out += self._trade(namespace, treaties, trade_namespace or namespace, principal_id, scopes, unavailable)
        return {"links": out, "linked": sum(1 for x in out if x["status"] == "linked"),
                "missing_targets": [x for x in out if x["status"] == "missing_target"],
                "provider_unavailable": [{"pack": pack, "reason": reason} for pack, reason in sorted(
                    unavailable.items())],
                "notice": "links follow citations, shared identifiers and accepted matches only; no implementation "
                          "or trade relationship is inferred"}

    def _legal(self, namespace, treaties, legal_namespace, principal_id, scopes, unavailable) -> list[dict[str, Any]]:
        cellar = [t for t in treaties if t["provider"] == "cellar"]
        if not cellar:
            return []
        if not table_exists(self.conn, "legal_works"):
            unavailable["legal.core"] = "the Legal work store is not present; EU act citations stay unresolved"
            return []
        from src.kb.legal import READ_SCOPE as LEGAL_READ
        from src.kb.legal import LegalStore

        legal = LegalStore(self.conn, initialize=False, now=self.now)
        legal_scopes = {LEGAL_READ, f"namespace:{legal_namespace}:read"}
        del scopes
        out = []
        for treaty in cellar:
            fields = self.store.record(treaty)["fields"]
            targets = [("legal-work", ident["value"], "shared-identifier",
                        {"identifier": ident, "note": "the agreement itself acquired as a Legal work"})
                       for ident in fields.get("identifiers") or [] if ident["scheme"] in {"celex", "eli"}]
            targets += [("eu-act", act["celex"], "citation",
                         {"cellar_relation": act["relation"], "direction": act["direction"],
                          "document_date": act["document_date"], "url": act["url"],
                          "note": "CELLAR states the relation; the act's role is not inferred"})
                        for act in fields.get("eu_acts") or []]
            for kind, identifier, basis, evidence in targets:
                found = legal.lookup(legal_namespace, scopes=legal_scopes, identifier=identifier)
                works = {w["work_id"] for w in found["works"]}
                if len(works) == 1:
                    work_id = next(iter(works))
                    versions = [v["version_id"] for v in legal.versions(legal_namespace, work_id)]
                    out.append(self._record(namespace, kind, treaty, None, "legal.core", work_id,
                                            versions[-1] if versions else None, basis, "linked",
                                            {**evidence, "identifier": identifier, "legal_namespace": legal_namespace,
                                             "version_ids": versions}, principal_id))
                elif kind == "eu-act":
                    out.append(self._record(namespace, kind, treaty, None, "legal.core", f"celex:{identifier}", None,
                                            basis, "ambiguous" if works else "missing_target",
                                            {**evidence, "identifier": identifier, "lookup_status": found["status"],
                                             "note": "the cited act is not (uniquely) acquired in the Legal store"},
                                            principal_id))
        return out

    def _sanctions(self, namespace, treaties, sanctions_namespace, principal_id, unavailable) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "sanctions_legal_bases"):
            unavailable["legal.sanctions"] = "the sanctions store is not present; no legal-basis citation was read"
            return []
        from src.ingestion.treaties_sources import citations_in

        by_identifier: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
        for treaty in treaties:
            for ident in self.store.record(treaty)["fields"].get("identifiers") or []:
                by_identifier.setdefault((ident["scheme"], str(ident["value"]).lstrip("0")), []).append(treaty)
        out = []
        rows = self.conn.execute(
            "SELECT basis_id, list_id, citation, celex, eli, first_snapshot_id FROM sanctions_legal_bases WHERE "
            "namespace=? ORDER BY basis_id", [sanctions_namespace]).fetchall()
        for basis_id, list_id, citation, celex, eli, snapshot in rows:
            cited = [("celex", celex, celex), ("eli", eli, eli)] + [
                (c["scheme"], c["value"], c["as_written"]) for c in citations_in(citation)]
            for scheme, value, written in cited:
                if not value:
                    continue
                evidence = {"list_id": list_id, "basis_id": basis_id, "citation_as_published": citation,
                            "cited": {"scheme": scheme, "value": value, "as_written": written},
                            "sanctions_namespace": sanctions_namespace}
                matches = by_identifier.get((scheme, str(value).lstrip("0")), [])
                for treaty in matches:
                    out.append(self._record(namespace, "sanctions-legal-basis", treaty, None, "legal.sanctions",
                                            basis_id, snapshot, "citation", "linked", evidence, principal_id))
                if not matches and scheme in {"cets", "unts-registration", "untc-mtdsg"}:
                    out.append(self._record(
                        namespace, "sanctions-legal-basis", {"record_key": f"{scheme}:{value}",
                                                             "revision_id": "none"}, None, "legal.sanctions",
                        basis_id, snapshot, "citation", "missing_target",
                        {**evidence, "note": "the list cites a treaty that has no treaty record in this namespace"},
                        principal_id))
        return out

    def _trade(self, namespace, treaties, trade_namespace, principal_id, scopes, unavailable) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "trade_identity_assertions"):
            unavailable["economics.trade"] = "the Trade flows identity store is not present; no reporter was read"
            return []
        del scopes
        areas: dict[str, list[dict[str, Any]]] = {}
        for assertion_id, subject, target in self.conn.execute(
                "SELECT assertion_id, subject_json, target_json FROM trade_identity_assertions WHERE namespace=? AND "
                "kind='area' AND state='accepted' ORDER BY assertion_id", [trade_namespace]).fetchall():
            place = load(target, {}) or {}
            if place.get("place_id"):
                areas.setdefault(place["place_id"], []).append({"assertion_id": assertion_id, "area": load(subject, {}),
                                                                "place": place})
        out = []
        for treaty in treaties:
            participants = sorted({k for k in (self.conn.execute(
                "SELECT DISTINCT participant_key FROM treaty_action_revisions WHERE namespace=? AND treaty_key=?",
                [namespace, treaty["record_key"]]).fetchall() if table_exists(self.conn, "treaty_action_revisions")
                else [])})
            for (participant,) in participants:
                place = self.identity.accepted_place(namespace, participant)
                if not place:
                    continue
                for area in areas.get(place["place_id"], []):
                    subject = area["area"]
                    out.append(self._record(
                        namespace, "participant-trade-reporter", treaty, participant, "economics.trade",
                        f"trade-area:{subject.get('scheme')}:{subject.get('code')}", area["assertion_id"],
                        "accepted-match", "linked",
                        {"participant_place_match": place["candidate_id"], "trade_area_assertion": area["assertion_id"],
                         "place_id": place["place_id"], "trade_namespace": trade_namespace,
                         "note": "the participant and the trade area reach the same place through two accepted "
                                 "matches; nothing says the treaty governs that trade"}, principal_id))
        return out

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, *, scopes: Iterable[str], treaty_key: str | None = None,
              link_kind: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "treaty_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM treaty_links WHERE namespace=? AND (? IS NULL OR treaty_key=?) AND "
            "(? IS NULL OR link_kind=?) AND (? IS NULL OR status=?) ORDER BY treaty_key, link_kind, target_key, "
            "created_at_ms", [namespace, treaty_key, treaty_key, link_kind, link_kind, status, status]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


__all__ = ["CONTRACT", "LINK_KINDS", "TreatiesLinks"]
