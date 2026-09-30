"""Life-science records linked to other packs by citation, shared identifier or accepted match (#2652, LS08 #2691).

Links are records of an explicit basis, never an inference:

* **Chemicals** - a ChEMBL compound links to a Chemicals substance only through an *accepted* LS07 InChIKey match
  (``accepted-match``); the link points at the substance's current revision;
* **Clinical** (medicines) - a compound links to a medicinal product whose regulatory record states the compound's
  ChEMBL ID or InChIKey as an identifier (``shared-identifier``); a ChEMBL target or UniProt protein links to a
  medicines record whose text cites the target's ChEMBL ID or UniProt accession exactly (``citation``). No
  drug-target, indication or disease claim is made;
* **Biodiversity** - an NCBI taxon links to Biodiversity occurrences of a taxon identity it has an *accepted* match
  with (``accepted-match``);
* **Literature** - an entry links to a scholarly document in the document store whose DOI (metadata or DOI URL)
  equals a DOI the entry cites (``citation``).

Every link names the life-science record revision it was made from and the target record revision. A missing
provider store is reported as ``provider_absent`` and a cited target that is not held is kept as a
``target_missing`` link with its cited identifier - never dropped. Links reuse the reading patterns of
:mod:`src.kb.substances_links` and only read other packs through their own stores or tables.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any

from src.kb.lifesci_records import (
    INACTIVE,
    LINK_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    LifeSciError,
    authorize,
    canonical,
    digest,
    table_exists,
)
from src.kb.lifesci_store import LifeSciStore

OWNERS = ("chemicals", "clinical", "biodiversity", "literature")
BASES = ("accepted-match", "shared-identifier", "citation")
_DDL = """
CREATE TABLE IF NOT EXISTS lifesci_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  owner TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL, target_revision TEXT,
  basis TEXT NOT NULL, basis_json TEXT NOT NULL, status TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""
NOTICE = ("links rest on a citation, a shared published identifier or an accepted identity match only; no "
          "drug-target, indication, disease or biological claim is inferred")


def _token(text: str, value: str) -> bool:
    return bool(value) and re.search(rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", text) is not None


class LifeSciLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.store = LifeSciStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "lifesci_links")

    def _put(self, namespace, record, owner, target_kind, target_id, target_revision, basis, detail, status,
             principal_id) -> dict[str, Any]:
        revision_id = record["revision"]["revision_id"]
        link_id = "lifesci-link:" + digest([namespace, revision_id, owner, target_kind, target_id,
                                            target_revision, basis])[:24]
        self.conn.execute("INSERT OR IGNORE INTO lifesci_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, link_id, record["record_id"], revision_id, owner, target_kind, target_id,
                           target_revision, basis, canonical(detail), status, principal_id, self.now()])
        return self.link(namespace, link_id)

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, record_id, revision_id, owner, target_kind, target_id, target_revision, basis, basis_json, "
            "status, created_by FROM lifesci_links WHERE namespace=? AND link_id=?", [namespace, link_id]).fetchone()
        if row is None:
            raise LifeSciError("not_found", "link is not visible in this namespace")
        value = dict(zip(("link_id", "record_id", "revision_id", "owner", "target_kind", "target_id",
                          "target_revision", "basis", "basis_detail", "status", "created_by"), row))
        value["basis_detail"] = json.loads(value["basis_detail"])
        return {"contract": LINK_CONTRACT, **value}

    def _records(self, namespace: str, record_type: str) -> list[dict[str, Any]]:
        out = []
        for head in self.store.records(namespace, record_type=record_type):
            revision = self.store.in_force(namespace, head["record_id"])
            if revision is None or revision["status"] in INACTIVE:
                continue
            out.append({**head, "revision": revision,
                        "statement": self.store.statement(namespace, revision["revision_id"])})
        return out

    # ------------------------------------------------------------------ owners

    def _chemicals(self, namespace, principal_id) -> dict[str, Any]:
        if not table_exists(self.conn, "substance_records"):
            return {"owner": "chemicals", "status": "provider_absent", "linked": []}
        from src.kb.lifesci_identity import LifeSciIdentity
        from src.kb.substances_store import SubstanceStore

        identity = LifeSciIdentity(self.conn, initialize=False) if table_exists(self.conn, "lifesci_matches") else None
        substances = SubstanceStore(self.conn, initialize=False)
        linked = []
        for record in self._records(namespace, "compound"):
            for match in identity.accepted(namespace, record["record_id"], right_kind="substance") if identity else []:
                held = substances.records(namespace, subject_keys=[match["right_id"]])
                revisions = [rev for head in held for rev in substances.revisions(namespace, head["record_id"])]
                latest = max(revisions, key=lambda r: r["seq"]) if revisions else None
                linked.append(self._put(
                    namespace, record, "chemicals", "substance", match["right_id"],
                    latest["revision_id"] if latest else None, "accepted-match",
                    {"match_id": match["match_id"], "method": match["method"], "decision_id": match["decision_id"]},
                    "resolved" if latest else "target_missing", principal_id))
        return {"owner": "chemicals", "status": "linked", "linked": linked}

    def _clinical(self, namespace, scopes, principal_id) -> dict[str, Any]:
        if not table_exists(self.conn, "clinical_records"):
            return {"owner": "clinical", "status": "provider_absent", "linked": []}
        from src.kb.clinical_records import ClinicalRecordError, ClinicalRecordStore

        try:
            held = ClinicalRecordStore(self.conn, initialize=False).find(
                namespace, scopes=scopes, kinds=["medicinal-product", "label-revision"])
        except ClinicalRecordError as exc:
            return {"owner": "clinical", "status": exc.code, "linked": []}
        linked = []
        for record in self._records(namespace, "compound"):
            wanted = {record["native_id"].upper()}
            key = dict(record["statement"]["attributes"].get("structures") or {}).get("standard_inchi_key")
            if key:
                wanted.add(key.upper())
            for item in held:
                if item["record_kind"] != "medicinal-product":
                    continue
                for identifier in item["record"].get("identifiers") or []:
                    if str(identifier.get("value") or "").strip().upper() in wanted:
                        linked.append(self._put(
                            namespace, record, "clinical", item["record_kind"], item["record_id"],
                            str(item["revision"]), "shared-identifier",
                            {"identifier": identifier, "provider": item["provider"], "native_id": item["native_id"]},
                            "resolved", principal_id))
        for record_type in ("target", "protein"):
            for record in self._records(namespace, record_type):
                ids = [record["native_id"]]
                if record_type == "target":
                    ids += [c["accession"] for c in record["statement"]["attributes"].get("components") or []
                            if c.get("accession")]
                for item in held:
                    text = canonical(item["record"])
                    cited = sorted({i for i in ids if _token(text, i)})
                    if cited:
                        linked.append(self._put(
                            namespace, record, "clinical", item["record_kind"], item["record_id"],
                            str(item["revision"]), "citation",
                            {"cited_identifiers": cited, "provider": item["provider"], "native_id": item["native_id"],
                             "note": "the regulatory record cites the identifier; no drug-target claim is made"},
                            "resolved", principal_id))
        return {"owner": "clinical", "status": "linked", "linked": linked}

    def _biodiversity(self, namespace, principal_id) -> dict[str, Any]:
        if not table_exists(self.conn, "biodiversity_records"):
            return {"owner": "biodiversity", "status": "provider_absent", "linked": []}
        from src.kb.biodiversity_store import BiodiversityStore
        from src.kb.lifesci_identity import LifeSciIdentity

        identity = LifeSciIdentity(self.conn, initialize=False) if table_exists(self.conn, "lifesci_matches") else None
        store = BiodiversityStore(self.conn, initialize=False)
        occurrences = []
        for head in store.records(namespace, record_type="occurrence"):
            current = store.current(namespace, head["record_id"])
            if current is not None:
                occurrences.append((head, current))
        linked = []
        for record in self._records(namespace, "taxon"):
            for match in identity.accepted(namespace, record["record_id"], right_kind="biodiversity-taxon") \
                    if identity else []:
                key = match["right_id"].partition(":")[2]
                found = [(h, c) for h, c in occurrences
                         if key in {str(c["statement"]["as_published"].get("taxon_key")),
                                    str(c["statement"]["as_published"].get("accepted_taxon_key"))}]
                for head, current in found:
                    linked.append(self._put(
                        namespace, record, "biodiversity", "occurrence", head["record_id"], current["revision_id"],
                        "accepted-match", {"match_id": match["match_id"], "taxon_identity": match["right_id"],
                                           "decision_id": match["decision_id"]}, "resolved", principal_id))
                if not found:
                    linked.append(self._put(
                        namespace, record, "biodiversity", "taxon-identity", match["right_id"], None, "accepted-match",
                        {"match_id": match["match_id"], "note": "no occurrence of this taxon identity is held"},
                        "target_missing", principal_id))
        return {"owner": "biodiversity", "status": "linked", "linked": linked}

    def _literature(self, namespace, principal_id) -> dict[str, Any]:
        linked = []
        documents = table_exists(self.conn, "documents")
        records = [r for t in ("protein", "structure", "document") for r in self._records(namespace, t)]
        for record in records:
            for citation in record["statement"]["citations"]:
                if citation["kind"] != "doi":
                    continue
                doi = citation["id"].lower()
                row = self.conn.execute(
                    "SELECT document_id, content_hash FROM documents WHERE lower(coalesce(json_extract_string("
                    "metadata, '$.doi'), '')) = ? OR lower(coalesce(canonical_url, url, '')) IN (?, ?) "
                    "ORDER BY document_id LIMIT 1",
                    [doi, f"https://doi.org/{doi}", f"http://doi.org/{doi}"]).fetchone() if documents else None
                linked.append(self._put(
                    namespace, record, "literature", "scholarly-document" if row else "doi",
                    row[0] if row else doi, row[1] if row else None, "citation",
                    {"cited": {"kind": "doi", "id": doi, "title": citation.get("title")}},
                    "resolved" if row else "target_missing", principal_id))
        return {"owner": "literature", "status": "linked" if documents else "provider_absent", "linked": linked}

    # ------------------------------------------------------------------ entry points

    def link_all(self, namespace: str, *, scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Create every link the held records support; report absent providers. Idempotent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        self.conn.execute("BEGIN")
        try:
            reports = [self._chemicals(namespace, principal_id), self._clinical(namespace, scopes, principal_id),
                       self._biodiversity(namespace, principal_id), self._literature(namespace, principal_id)]
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"contract": LINK_CONTRACT, "namespace": namespace,
                "owners": {r["owner"]: {"status": r["status"], "links": len(r["linked"]),
                                        "target_missing": sum(1 for x in r["linked"] if x["status"] ==
                                                              "target_missing")} for r in reports},
                "linked": [x for r in reports for x in r["linked"]], "notice": NOTICE}

    def links(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None,
              owner: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM lifesci_links WHERE namespace=? AND (? IS NULL OR record_id=?) AND "
            "(? IS NULL OR owner=?) ORDER BY owner, target_id, link_id",
            [namespace, record_id, record_id, owner, owner]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


def owner_status(conn: Any) -> dict[str, str]:
    """Which linked packs are present in this deployment (links degrade gracefully when absent)."""
    tables = {"chemicals": "substance_records", "clinical": "clinical_records",
              "biodiversity": "biodiversity_records", "literature": "documents"}
    return {owner: "present" if table_exists(conn, table) else "provider_absent" for owner, table in tables.items()}


__all__ = ["BASES", "OWNERS", "LifeSciLinks", "owner_status"]
