"""Treaty records linked to other packs by citation, shared identifier or accepted match only (#2581, TR07).

* **Legislation** (Legal works, ``legal.core``): an agreement's own CELEX and
  the CELEX numbers of the EU acts CELLAR states point at it (``citations``,
  explicit CDM triples) are looked up exactly in ``legal_works``
  (:meth:`src.kb.legal.LegalStore.lookup`). Basis ``shared-identifier`` for the
  agreement's own CELEX, ``citation`` for a cited act.
* **Sanctions** (``legal.sanctions``): a sanctions legal basis whose CELEX
  equals a treaty's CELEX, or whose citation text contains one of the treaty's
  exact identifiers (``CETS No. 999``, the UNTC ``mtdsg_no``, the UNTS
  registration number with ``UNTS``), basis ``citation``.
* **Trade flows** (``economics.trade``): a participant's published ISO 3166-1
  alpha-3 code equal to a trade series reporter's published ISO code (basis
  ``shared-identifier``), or a participant reaching a geospatial place carrying
  that code through an accepted TR06 match (basis ``accepted-match``, naming the
  decision). A link says the reporter publishes trade under the same code; it
  never says a trade flow is governed by, or complies with, the treaty.

Every link names the treaty record revision it was made from and the target's
revision where the target has one. A missing provider or a missing target is
reported (``provider-missing`` / ``target-missing``), never dropped. No
implementation, obligation or compliance relationship is inferred.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.treaties_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    TreatiesStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

TARGET_KINDS = ("legal-work", "sanctions-legal-basis", "trade-reporter")
BASES = ("citation", "shared-identifier", "accepted-match")
STATUSES = ("resolved", "target-missing", "provider-missing")
NOTICE = "links record a citation, a shared published identifier or an accepted match; no implementation, " \
         "obligation or compliance relationship is inferred"
_DDL = """
CREATE TABLE IF NOT EXISTS treaty_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, treaty_key TEXT NOT NULL, treaty_revision_id TEXT NOT NULL,
  source_id TEXT NOT NULL, subject_key TEXT NOT NULL, target_kind TEXT NOT NULL, target_key TEXT NOT NULL,
  target_revision TEXT, target_namespace TEXT, basis TEXT NOT NULL, status TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def treaty_identifiers(record: Mapping[str, Any]) -> dict[str, list[str]]:
    """Exact identifiers a treaty record publishes, as tokens another record may cite."""
    fields = record["fields"]
    ids = fields.get("identifiers") or {}
    out: dict[str, list[str]] = {"celex": [], "tokens": []}
    if ids.get("celex"):
        out["celex"].append(ids["celex"])
    if ids.get("untc_mtdsg"):
        out["tokens"].append(ids["untc_mtdsg"])
    if ids.get("unts_registration"):
        out["tokens"].append(f"UNTS {ids['unts_registration']}")
    for ref in fields.get("cross_references") or []:
        if ref["scheme"] == "cets":
            out["tokens"].append(f"CETS No. {ref['value']}")
        if ref["scheme"] == "celex" and ref["value"] not in out["celex"]:
            out["celex"].append(ref["value"])
    return out


def _mentions(text: str, token: str) -> bool:
    return bool(re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w-])", text or ""))


class TreatiesLinks:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = TreatiesStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _put(self, namespace, row, subject, kind, target, revision, target_namespace, basis, status, evidence,
             principal_id) -> dict[str, Any]:
        link_id = "treaty-link:" + digest([namespace, row["revision_id"], subject, kind, target, basis])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO treaty_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, link_id, row["treaty_key"], row["revision_id"], row["source_id"], subject, kind, target,
             revision, target_namespace, basis, status, canonical(evidence), principal_id, self.now()])
        return self.link(namespace, link_id)

    def link_all(self, namespace: str, *, principal_id: str, scopes: Iterable[str], legal_namespace: str | None = None,
                 sanctions_namespace: str | None = None, trade_namespace: str | None = None) -> dict[str, Any]:
        """Create links for every current treaty record; idempotent per treaty revision and target."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        treaties = self.store.records(namespace, scopes=scopes, kinds=["treaty"])
        made, unavailable = [], []
        legal_ns = legal_namespace or namespace
        if not table_exists(self.conn, "legal_works"):
            unavailable.append({"provider": "legal.core", "reason": "no Legal works store (Legislation links "
                                                                   "degrade to target-missing records)"})
        sanctions_ns = sanctions_namespace or namespace
        if not table_exists(self.conn, "sanctions_legal_bases"):
            unavailable.append({"provider": "legal.sanctions", "reason": "the sanctions feature's store is absent"})
        trade_ns = trade_namespace or namespace
        if not table_exists(self.conn, "trade_series"):
            unavailable.append({"provider": "economics.trade", "reason": "no trade series store"})
        for row in treaties:
            ids = treaty_identifiers(row["record"])
            made += self._legislation(namespace, row, ids, legal_ns, principal_id, scopes)
            made += self._sanctions(namespace, row, ids, sanctions_ns, principal_id)
        made += self._trade(namespace, trade_ns, principal_id, scopes)
        return {"links": made, "unavailable": unavailable, "notice": NOTICE,
                "counts": {s: sum(1 for m in made if m["status"] == s) for s in STATUSES}}

    # ------------------------------------------------------------------ Legislation

    def _legislation(self, namespace, row, ids, legal_ns, principal_id, scopes) -> list[dict[str, Any]]:
        wanted = [(celex, "shared-identifier", {"identifier": celex, "as_published": "the agreement's own CELEX"})
                  for celex in ids["celex"]]
        for citation in row["record"]["fields"].get("citations") or []:
            wanted.append((citation["celex"], "citation", {"identifier": citation["celex"], "citation": citation}))
        out = []
        for celex, basis, evidence in wanted:
            if not table_exists(self.conn, "legal_works"):
                out.append(self._put(namespace, row, row["record_key"], "legal-work", f"celex:{celex}", None,
                                     legal_ns, basis, "provider-missing", evidence, principal_id))
                continue
            from src.kb.legal import LegalStore

            found = LegalStore(self.conn).lookup(legal_ns, scopes={READ_SCOPE, f"namespace:{legal_ns}:read"}
                                                 | set(scopes), identifier=celex)
            works = found.get("works") or []
            if not works:
                out.append(self._put(namespace, row, row["record_key"], "legal-work", f"celex:{celex}", None,
                                     legal_ns, basis, "target-missing", evidence, principal_id))
                continue
            work = works[0]
            revision = None
            if table_exists(self.conn, "legal_versions"):
                latest = self.conn.execute("SELECT version_id FROM legal_versions WHERE work_id=? ORDER BY "
                                           "version_id DESC LIMIT 1", [work["work_id"]]).fetchone()
                revision = latest[0] if latest else None
            out.append(self._put(namespace, row, row["record_key"], "legal-work", work["work_id"], revision, legal_ns,
                                 basis, "resolved", {**evidence, "match": "exact identifier lookup",
                                                     "work_title": work.get("title")}, principal_id))
        return out

    # ------------------------------------------------------------------ Sanctions

    def _sanctions(self, namespace, row, ids, sanctions_ns, principal_id) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "sanctions_legal_bases"):
            return []
        out = []
        rows = self.conn.execute("SELECT basis_id, list_id, citation, celex, first_snapshot_id FROM "
                                 "sanctions_legal_bases WHERE namespace=? ORDER BY basis_id",
                                 [sanctions_ns]).fetchall()
        for basis_id, list_id, citation, celex, snapshot in rows:
            hit = None
            if celex and celex in ids["celex"]:
                hit = {"identifier": celex, "field": "celex"}
            else:
                token = next((t for t in ids["tokens"] + ids["celex"] if _mentions(citation, t)), None)
                if token:
                    hit = {"identifier": token, "field": "citation text", "citation_as_published": citation}
            if hit:
                out.append(self._put(namespace, row, row["record_key"], "sanctions-legal-basis", basis_id, snapshot,
                                     sanctions_ns, "citation", "resolved", {**hit, "list_id": list_id},
                                     principal_id))
        return out

    # ------------------------------------------------------------------ Trade flows

    def _reporters(self, trade_ns: str) -> dict[str, list[tuple[str, str, str, str]]]:
        if not table_exists(self.conn, "trade_series"):
            return {}
        out: dict[str, list[tuple[str, str, str, str]]] = {}
        for series_id, scheme, code, reporter_json, release in self.conn.execute(
                "SELECT series_id, reporter_scheme, reporter_code, reporter_json, first_release_id FROM trade_series "
                "WHERE namespace=? ORDER BY series_id", [trade_ns]).fetchall():
            iso3 = (json.loads(reporter_json or "{}") or {}).get("iso3")
            if iso3:
                out.setdefault(str(iso3).upper(), []).append((series_id, scheme, code, release))
        return out

    def _trade(self, namespace, trade_ns, principal_id, scopes) -> list[dict[str, Any]]:
        reporters = self._reporters(trade_ns)
        if not reporters:
            return []
        from src.kb.treaties_identity import TreatiesIdentity

        identity = TreatiesIdentity(self.conn, initialize=False)
        out = []
        for row in self.store.records(namespace, scopes=scopes, kinds=["participant"]):
            fields = row["record"]["fields"]
            routes = []
            for code in fields.get("codes") or []:
                if code["scheme"] == "iso3166-1-alpha3":
                    routes.append((code["value"].upper(), "shared-identifier", {"participant_code": code}))
            for link in self._accepted_places(identity, namespace, row["record_key"], scopes):
                routes.append((link["iso3"], "accepted-match", {"candidate_id": link["candidate_id"],
                                                                "decision_id": link["decision_id"],
                                                                "place": link["record_key"]}))
            for iso3, basis, evidence in routes:
                for series_id, scheme, code, release in reporters.get(iso3, [])[:1]:
                    series = [s[0] for s in reporters[iso3]][:20]
                    out.append(self._put(namespace, row, row["record_key"], "trade-reporter",
                                         f"trade-reporter:{scheme}:{code}", release, trade_ns, basis, "resolved",
                                         {**evidence, "iso3": iso3, "series": series,
                                          "meaning": "the reporter publishes trade under the same ISO code"},
                                         principal_id))
        return out

    def _accepted_places(self, identity, namespace, participant, scopes) -> list[dict[str, Any]]:
        try:
            accepted = identity.accepted(namespace, participant, scopes=scopes)
        except Exception:  # noqa: BLE001 - identity is optional; no accepted match means no route
            return []
        out = []
        for link in accepted:
            if not link["record_key"].startswith("geospatial:place:"):
                continue
            row = self.conn.execute("SELECT source_ids_json FROM geospatial_place_revisions WHERE place_id=? "
                                    "ORDER BY revision DESC LIMIT 1", [link["record_key"].split(":", 2)[2]]
                                    ).fetchone() if table_exists(self.conn, "geospatial_place_revisions") else None
            iso3 = (json.loads(row[0]) if row else {}).get("iso3166-1-alpha3")
            if iso3:
                out.append({**link, "iso3": str(iso3).upper()})
        return out

    # ------------------------------------------------------------------ reads

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, treaty_key, treaty_revision_id, source_id, subject_key, target_kind, target_key, "
            "target_revision, target_namespace, basis, status, evidence_json, created_by, created_at_ms FROM "
            "treaty_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        keys = ("link_id", "treaty_key", "treaty_revision_id", "source_id", "subject_key", "target_kind", "target_key",
                "target_revision", "target_namespace", "basis", "status", "evidence", "created_by", "created_at_ms")
        out = dict(zip(keys, row))
        out["evidence"] = json.loads(out["evidence"])
        return out

    def links(self, namespace: str, *, scopes: Iterable[str], treaty_key: str | None = None,
              subject_key: str | None = None, target_kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "treaty_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM treaty_links WHERE namespace=? AND (? IS NULL OR treaty_key=?) AND "
            "(? IS NULL OR subject_key=?) AND (? IS NULL OR target_kind=?) ORDER BY target_kind, treaty_key, "
            "target_key, created_at_ms", [namespace, treaty_key, treaty_key, subject_key, subject_key, target_kind,
                                          target_kind]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]
