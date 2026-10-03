"""Internet infrastructure records linked to OSINT and Vulnerabilities by citation or stated identifier (#2743, II08).

Two kinds of link, each recording its **basis** and pinning **specific revisions** on both sides:

* ``osint`` - a record about a declared domain (the RDAP domain registration, crt.sh certificates of that exact domain)
  and an existing OSINT source identity (:mod:`src.kb.source_identity`, read-only) whose stated domain is that domain
  (a current ``domain`` alias decision or ``domain`` native id, as
  :func:`src.ingestion.osint_observations.source_domains` reads them). Basis ``stated-domain``; the target pins the
  source identity's current revision. This provider never writes source identities.
* ``vulnerabilities`` - only where a record's own content states a CVE id (``cve_ids``), to the
  ``technology.vulnerabilities`` identity of that CVE. None of the first-coverage sources states one, so first
  coverage records none and says so.

No link is ever inferred from shared addresses, shared hosting, shared certificates or similar names. When the OSINT
source-identity store or the Vulnerabilities store is not held the link is recorded ``provider_absent``; a held store
without the target is ``target_not_held`` - reported, never dropped.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.internet_infrastructure_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    InfrastructureRecordError,
    authorize,
    canonical,
    digest,
    iso,
    load,
    table_exists,
)
from src.kb.internet_infrastructure_store import InternetInfrastructureStore

CONTRACT = "noesis-internet-infrastructure-link-v1"
KINDS = ("osint", "vulnerabilities")
BASES = ("stated-domain", "stated-cve-id")
STATES = ("linked", "provider_absent", "target_not_held")
SOURCE_IDENTITY_READ = "knowledge:source-identity:read"
VULNERABILITIES_READ = "knowledge:vulnerabilities:read"
NO_INFERENCE = "A link rests on a stated domain or a stated CVE id only; nothing is inferred from shared addresses."
_CVE = re.compile(r"^CVE-\d{4}-\d{4,}$")
_DDL = """
CREATE TABLE IF NOT EXISTS ii_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, object_id TEXT NOT NULL, revision_id TEXT, kind TEXT NOT NULL,
  basis TEXT NOT NULL, reference_json TEXT NOT NULL, target_json TEXT, state TEXT NOT NULL, evidence_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def _require(scopes: Iterable[str], scope: str) -> None:
    scopes = set(scopes or ())
    if "operator" not in scopes and scope not in scopes:
        raise InfrastructureRecordError("unauthorized", f"{scope} is required to read the linked provider")


class InfrastructureLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = InternetInfrastructureStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _current(self, namespace: str, object_id: str) -> dict[str, Any] | None:
        rows = self.store.revisions(namespace, object_id)
        return rows[-1] if rows and rows[-1]["state"] == "published" else None

    def _put(self, namespace, obj, revision, kind, basis, reference, target, state, evidence, principal_id):
        link_id = "ii-link:" + digest([namespace, obj["object_id"], revision and revision["revision_id"], kind,
                                       reference, target, state])[:24]
        if not self.conn.execute("SELECT 1 FROM ii_links WHERE namespace=? AND link_id=?",
                                 [namespace, link_id]).fetchone():
            self.conn.execute("INSERT INTO ii_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, link_id, obj["object_id"], revision and revision["revision_id"], kind,
                               basis, canonical(reference), None if target is None else canonical(target), state,
                               canonical({**evidence, "note": NO_INFERENCE}), principal_id, self.now()])
        return self.link(namespace, link_id)

    def link_osint(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                   osint_namespace: str | None = None) -> dict[str, Any]:
        """Link records about a declared domain to OSINT source identities stating that domain (read-only)."""
        from src.ingestion import osint_observations as obs

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        _require(scopes, SOURCE_IDENTITY_READ)
        target_ns = osint_namespace or namespace
        held = table_exists(self.conn, "source_identity_current") and table_exists(self.conn,
                                                                                   "source_identity_revisions")
        owners = obs.source_domains(self.conn, target_ns) if held else {}
        links = []
        for obj in self.store.objects(namespace):
            if obj["resource"]["kind"] != "domain" or obj["object_kind"] not in {"domain", "certificate"}:
                continue
            revision = self._current(namespace, obj["object_id"])
            if revision is None:
                continue
            domain = obj["resource"]["value"]
            reference = {"stated_domain": domain, "provider": obj["provider"], "object_kind": obj["object_kind"],
                         "native_id": obj["native_id"]}
            if not held:
                links.append(self._put(namespace, obj, revision, "osint", "stated-domain", reference,
                                       {"provider": "osint.core", "namespace": target_ns}, "provider_absent",
                                       {"reason": "the OSINT source-identity store is not held"}, principal_id))
                continue
            found = owners.get(domain) or []
            if not found:
                links.append(self._put(namespace, obj, revision, "osint", "stated-domain", reference,
                                       {"namespace": target_ns, "domain": domain}, "target_not_held",
                                       {"reason": "no OSINT source identity states this domain"}, principal_id))
                continue
            for source_id in found:
                row = self.conn.execute(
                    "SELECT c.revision_id FROM source_identity_current c JOIN source_identity_revisions r "
                    "USING(revision_id) WHERE r.namespace=? AND c.source_id=?", [target_ns, source_id]).fetchone()
                links.append(self._put(namespace, obj, revision, "osint", "stated-domain", reference,
                                       {"provider": "osint.core", "namespace": target_ns, "source_id": source_id,
                                        "source_identity_revision_id": row[0] if row else None, "domain": domain},
                                       "linked", {"basis": "the source identity states the same domain",
                                                  "ambiguous": len(found) > 1}, principal_id))
        return {"contract": CONTRACT, "kind": "osint", "links": links,
                "note": "this provider writes no source identities; " + NO_INFERENCE}

    def link_vulnerabilities(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                             vulnerabilities_namespace: str | None = None) -> dict[str, Any]:
        """Link records to technology.vulnerabilities only where a record states a CVE id."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        _require(scopes, VULNERABILITIES_READ)
        target_ns = vulnerabilities_namespace or namespace
        held = table_exists(self.conn, "vuln_identities")
        links, stated = [], 0
        for obj in self.store.objects(namespace):
            revision = self._current(namespace, obj["object_id"])
            if revision is None:
                continue
            for cve in sorted({str(c) for c in revision["content"].get("cve_ids") or [] if _CVE.match(str(c))}):
                stated += 1
                reference = {"stated_cve_id": cve, "provider": obj["provider"], "native_id": obj["native_id"]}
                if not held:
                    state, target = "provider_absent", {"provider": "technology.vulnerabilities"}
                else:
                    row = self.conn.execute("SELECT first_series_id FROM vuln_identities WHERE namespace=? AND "
                                            "cve_id=?", [target_ns, cve]).fetchone()
                    state = "linked" if row else "target_not_held"
                    target = {"provider": "technology.vulnerabilities", "namespace": target_ns, "cve_id": cve,
                              "series_id": row[0] if row else None}
                links.append(self._put(namespace, obj, revision, "vulnerabilities", "stated-cve-id", reference,
                                       target, state, {"basis": "the record states the CVE id"}, principal_id))
        return {"contract": CONTRACT, "kind": "vulnerabilities", "links": links, "stated_cve_ids": stated,
                "provider_state": "held" if held else "provider_absent",
                "note": ("no record states a CVE id (none of the first-coverage sources does); no link is inferred"
                         if not stated else NO_INFERENCE)}

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, object_id, revision_id, kind, basis, reference_json, target_json, state, evidence_json, "
            "created_by, created_at_ms FROM ii_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if not row:
            raise InfrastructureRecordError("link_not_found", "no such link")
        return {"contract": CONTRACT, "link_id": row[0], "object_id": row[1], "revision_id": row[2], "kind": row[3],
                "basis": row[4], "reference": load(row[5], {}), "target": load(row[6], None), "state": row[7],
                "evidence": load(row[8], {}), "created_by": row[9], "created_at": iso(row[10])}

    def links(self, namespace: str, *, scopes: Iterable[str], object_id: str | None = None,
              kind: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "ii_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM ii_links WHERE namespace=? AND (? IS NULL OR object_id=?) AND (? IS NULL OR kind=?) "
            "AND (? IS NULL OR state=?) ORDER BY created_at_ms, link_id",
            [namespace, object_id, object_id, kind, kind, state, state]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]

    def links_for(self, namespace: str, object_ids: Iterable[str]) -> list[dict[str, Any]]:
        wanted = set(object_ids)
        return [lk for lk in self.links(namespace, scopes={"operator"}) if lk["object_id"] in wanted]


def summarise(links: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for link in links:
        out[link["state"]] = out.get(link["state"], 0) + 1
    return out


__all__ = ["BASES", "CONTRACT", "KINDS", "STATES", "InfrastructureLinks", "summarise"]
