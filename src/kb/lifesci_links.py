"""Life-science records to Chemicals, Biodiversity, Clinical medicines and literature by citation (#2652, LS08 #2691).

Links are records of an explicit basis, each pointing from one record
*revision* to one target *revision*, and only ever read other packs through
their own stores - as the Chemicals links do (:mod:`src.kb.substances_links`):

* **compounds -> Chemicals substances** - only through an *accepted* LS07
  ``inchikey`` match (:class:`src.kb.lifesci_identity.LifeSciIdentity`);
* **taxa -> Biodiversity occurrences** - only through an *accepted* LS07 taxon
  match to a GBIF taxon; the occurrences GBIF publishes for that taxon key are
  linked at their current revision (:class:`src.kb.biodiversity_store.BiodiversityStore`);
* **targets and compounds -> medicines** - a Clinical medicines regulatory
  record (:mod:`src.kb.clinical_medicines`) whose published identifiers name
  the ChEMBL target or compound ID (``shared-identifier``);
* **entries -> literature** - UniProt citations, a PDB primary citation and
  ChEMBL documents link to Science paper documents whose metadata states the
  same DOI or PubMed ID (``citation``); each ChEMBL activity links to the
  document record it cites. Anything else stays an ``unresolved`` citation.

A provider or target that is not installed, not readable with the caller's
scopes or not on record is *reported* in ``missing``, never silently dropped.
No drug-target, disease or efficacy claim is inferred from a link.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from typing import Any

from src.kb.lifesci_records import (
    LINK_CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
)
from src.kb.lifesci_store import LifeSciStore, table_exists

BASES = ("accepted-match", "shared-identifier", "citation")
CLINICAL_READ = "knowledge:clinical:read"
BIODIVERSITY_READ = "knowledge:environment:read"
MEDICINE_ID_KINDS = {"chembl", "chembl-id", "chembl-compound", "chembl-target", "chembl_id"}
_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_DDL = """
CREATE TABLE IF NOT EXISTS lifesci_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_id TEXT NOT NULL, target_revision TEXT, relation TEXT NOT NULL,
  basis TEXT NOT NULL, detail_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def normalize_doi(value: Any) -> str | None:
    match = _DOI.search(str(value or ""))
    return match.group(0).rstrip(".,;").lower() if match else None


class LifeSciLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.lifesci_identity import LifeSciIdentity

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = LifeSciStore(conn, initialize=initialize, now=self.now)
        self.identity = LifeSciIdentity(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "lifesci_links")

    def _insert(self, namespace, record_id, revision_id, target_kind, target_id, target_revision, relation, basis,
                detail, principal_id) -> bool:
        link_id = "lifesci-link:" + digest([namespace, revision_id, target_kind, target_id, target_revision])[:24]
        inserted = self.conn.execute(
            "INSERT INTO lifesci_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING link_id",
            [namespace, link_id, record_id, revision_id, target_kind, target_id, target_revision, relation, basis,
             canonical(detail), principal_id, self.now()]).fetchall()
        return bool(inserted)

    def _current(self, namespace: str, record_type: str):
        for record in self.store.records(namespace, record_type=record_type):
            revision = self.store.current(namespace, record["record_id"])
            if revision is not None:
                yield record, revision

    # ------------------------------------------------------------------ targets in other packs

    def _chemicals(self, namespace, principal_id, missing) -> int:
        from src.kb.substances_store import SubstanceStore

        count = 0
        matches = [m for m in self.identity.accepted(namespace) if m["method"] == "inchikey"]
        if not table_exists(self.conn, "substance_identifiers"):
            missing.append({"target": "chemicals.substances", "reason": "Chemicals is not installed; compounds keep "
                                                                        "their InChIKey without a substance link"})
            return 0
        rows = SubstanceStore(self.conn, initialize=False).identifiers
        for match in matches:
            compound = next(k for k in (match["left_key"], match["right_key"]) if k.startswith("chembl-compound:"))
            substance = next(k for k in (match["left_key"], match["right_key"]) if k.startswith("chemicals:"))
            record = self.store.find(namespace, "compound", compound.split(":", 1)[1])
            revision = self.store.current(namespace, record["record_id"])
            subject = substance.split(":", 1)[1]
            identifiers = [r for r in rows(namespace, subject) if r["scheme"] == "inchikey"]
            if not identifiers:
                missing.append({"target": substance, "reason": "substance no longer on record"})
                continue
            count += self._insert(namespace, record["record_id"], revision["revision_id"], "chemicals-substance",
                                  subject, identifiers[-1]["revision_id"], "same-substance-as-reviewed",
                                  "accepted-match", {"match_id": match["match_id"], "method": match["method"],
                                                     "reviewer": match["reviewer"],
                                                     "inchikey": match["evidence"].get("inchikey")}, principal_id)
        return count

    def _biodiversity(self, namespace, scopes, principal_id, missing) -> int:
        from src.kb.biodiversity_store import BiodiversityStore

        matches = [m for m in self.identity.accepted(namespace) if m["method"] in {"taxon-cross-reference",
                                                                                     "scientific-name"}]
        if not table_exists(self.conn, "biodiversity_revisions"):
            missing.append({"target": "environment.biodiversity",
                            "reason": "Biodiversity is not installed; taxa keep their lineage without occurrences"})
            return 0
        if BIODIVERSITY_READ not in scopes and "operator" not in scopes:
            missing.append({"target": "environment.biodiversity", "reason": f"{BIODIVERSITY_READ} is required"})
            return 0
        store = BiodiversityStore(self.conn, initialize=False)
        count = 0
        for match in matches:
            taxon = next(k for k in (match["left_key"], match["right_key"]) if k.startswith("ncbitaxon:"))
            other = next(k for k in (match["left_key"], match["right_key"]) if k.startswith("biodiversity:"))
            record = self.store.find(namespace, "taxon", taxon.split(":", 1)[1])
            revision = self.store.current(namespace, record["record_id"])
            provider, _, key = other.split(":", 1)[1].partition(":")
            occurrences = []
            if provider == "gbif":
                for occ in store.records(namespace, record_type="occurrence"):
                    current = store.current(namespace, occ["record_id"])
                    published = current["statement"]["as_published"]
                    if current["event"] == "published" and key in {published.get("taxon_key"),
                                                                    published.get("accepted_taxon_key")}:
                        occurrences.append((occ, current))
            if not occurrences:
                missing.append({"target": other, "reason": "no occurrence on record for the matched taxon"})
            for occ, current in occurrences:
                count += self._insert(namespace, record["record_id"], revision["revision_id"],
                                      "biodiversity-occurrence", occ["record_id"], current["revision_id"],
                                      "occurrence-of-reviewed-taxon", "accepted-match",
                                      {"match_id": match["match_id"], "method": match["method"],
                                       "reviewer": match["reviewer"], "gbif_taxon_key": key,
                                       "gbif_id": current["statement"]["as_published"]["gbif_id"]}, principal_id)
        return count

    def _medicines(self, namespace, scopes, principal_id, missing) -> int:
        from src.kb.clinical_medicines import CONTRACT as MEDICINES_CONTRACT

        if not table_exists(self.conn, "clinical_records"):
            missing.append({"target": "clinical.medicines", "reason": "Clinical Evidence medicines are not installed"})
            return 0
        if CLINICAL_READ not in scopes and "operator" not in scopes:
            missing.append({"target": "clinical.medicines", "reason": f"{CLINICAL_READ} is required"})
            return 0
        from src.kb.clinical_records import ClinicalRecordStore

        clinical = ClinicalRecordStore(self.conn, initialize=False)
        named: dict[str, list[dict[str, Any]]] = {}
        for row in clinical.find(namespace, scopes={"operator"}, limit=10000):
            record = row["record"] or {}
            if record.get("contract") != MEDICINES_CONTRACT:
                continue
            for item in record.get("identifiers") or []:
                if str(item.get("kind") or "").casefold() in MEDICINE_ID_KINDS:
                    named.setdefault(str(item.get("value")), []).append(
                        {"record_id": row["record_id"], "revision": row["revision"], "kind": item["kind"],
                         "locator": item.get("locator"), "authority": record.get("authority"),
                         "native_id": record.get("native_id"), "name": record.get("name")})
        count = 0
        for record_type in ("target", "compound"):
            for record, revision in self._current(namespace, record_type):
                for medicine in named.get(record["record_key"], []):
                    count += self._insert(namespace, record["record_id"], revision["revision_id"], "clinical-medicine",
                                          medicine["record_id"], str(medicine["revision"]),
                                          "named-by-regulatory-record", "shared-identifier",
                                          {**medicine, "identifier": record["record_key"],
                                           "notice": "the regulatory record names this ChEMBL ID; no drug-target "
                                                     "or efficacy claim is made"}, principal_id)
        return count

    # ------------------------------------------------------------------ literature

    def _papers(self) -> dict[str, dict[str, Any]]:
        tables = {r[0] for r in self.conn.execute("SELECT table_name FROM information_schema.tables WHERE table_name "
                                                  "IN ('documents', 'document_revision_records')").fetchall()}
        if len(tables) < 2:
            return {}
        index = {}
        for document_id, title, metadata in self.conn.execute(
                "SELECT document_id, title, metadata FROM documents WHERE source_type='paper' ORDER BY document_id"
        ).fetchall():
            revision = self.conn.execute(
                "SELECT revision_id FROM document_revision_records WHERE document_id=? AND committed_watermark IS "
                "NOT NULL ORDER BY revision DESC LIMIT 1", [document_id]).fetchone()
            meta = json.loads(metadata) if isinstance(metadata, str) and metadata else dict(metadata or {})
            item = {"document_id": document_id, "revision_id": revision[0] if revision else None, "title": title}
            if normalize_doi(meta.get("doi")):
                index[f"doi:{normalize_doi(meta.get('doi'))}"] = item
            for key in ("pmid", "pubmed_id"):
                if meta.get(key):
                    index[f"pmid:{meta[key]}"] = item
        return index

    @staticmethod
    def _citations(record_type: str, published: dict[str, Any]) -> list[dict[str, Any]]:
        if record_type == "protein":
            items = published.get("citations") or []
        elif record_type == "structure":
            items = [published["primary_citation"]] if published.get("primary_citation") else []
        elif record_type == "document":
            items = [published]
        else:
            items = []
        return [{"doi": normalize_doi(c.get("doi")), "pubmed_id": c.get("pubmed_id"), "title": c.get("title")}
                for c in items]

    def _literature(self, namespace, principal_id, missing) -> tuple[int, int]:
        papers = self._papers()
        if not papers:
            missing.append({"target": "science.literature", "reason": "no paper with a DOI or PubMed ID is held; "
                                                                      "citations stay unresolved"})
        resolved = unresolved = 0
        for record_type in ("protein", "structure", "document"):
            for record, revision in self._current(namespace, record_type):
                for ref in self._citations(record_type, revision["statement"]["as_published"]):
                    paper = (papers.get(f"doi:{ref['doi']}") if ref["doi"] else None) or (
                        papers.get(f"pmid:{ref['pubmed_id']}") if ref["pubmed_id"] else None)
                    if paper:
                        resolved += 1
                    else:
                        unresolved += 1
                    target = paper["document_id"] if paper else (ref["doi"] or ref["pubmed_id"] or
                                                                 digest(ref["title"])[:16])
                    self._insert(namespace, record["record_id"], revision["revision_id"],
                                 "publication" if paper else "unresolved-citation", target,
                                 (paper or {}).get("revision_id"), "cites" if paper else "unresolved",
                                 "citation", {"doi": ref["doi"], "pubmed_id": ref["pubmed_id"], "title": ref["title"],
                                              "resolved_by": ("exact DOI or PubMed ID stated by the source and by the "
                                                              "paper's metadata") if paper else
                                              "no held paper states this identifier"}, principal_id)
        documents = {r["record_key"]: (r, rev) for r, rev in self._current(namespace, "document")}
        for record, revision in self._current(namespace, "activity"):
            cited = revision["statement"]["as_published"]["document_chembl_id"]
            held = documents.get(cited)
            self._insert(namespace, record["record_id"], revision["revision_id"],
                         "chembl-document" if held else "unresolved-citation",
                         held[0]["record_id"] if held else cited, held[1]["revision_id"] if held else None,
                         "cites" if held else "unresolved", "citation",
                         {"document_chembl_id": cited, "resolved_by": "the activity's published document_chembl_id"},
                         principal_id)
        return resolved, unresolved

    # ------------------------------------------------------------------ entry point

    def link(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Create every link the published identifiers, accepted matches and citations support; report the rest."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        missing: list[dict[str, Any]] = []
        created = {"chemicals": self._chemicals(namespace, principal_id, missing),
                   "biodiversity": self._biodiversity(namespace, scopes, principal_id, missing),
                   "medicines": self._medicines(namespace, scopes, principal_id, missing)}
        resolved, unresolved = self._literature(namespace, principal_id, missing)
        return {"contract": LINK_CONTRACT, "namespace": namespace, "created": created,
                "citations": {"resolved": resolved, "unresolved": unresolved}, "missing": missing,
                "links": self.links(namespace, scopes=scopes),
                "policy": "identifier, accepted-match and citation links only; no inferred drug-target or disease "
                          "claim"}

    def links(self, namespace: str, *, scopes: Iterable[str], record_id: str | None = None,
              target_kind: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT link_id, record_id, revision_id, target_kind, target_id, target_revision, relation, basis, "
            "detail_json, created_by FROM lifesci_links WHERE namespace=? AND (? IS NULL OR record_id=?) AND "
            "(? IS NULL OR target_kind=?) ORDER BY record_id, target_kind, target_id, link_id",
            [namespace, record_id, record_id, target_kind, target_kind]).fetchall()
        return [{"contract": LINK_CONTRACT, **dict(zip(("link_id", "record_id", "revision_id", "target_kind",
                                                        "target_id", "target_revision", "relation", "basis"), r[:8])),
                 "detail": json.loads(r[8]), "created_by": r[9]} for r in rows]

    def generation(self, namespace: str) -> int:
        if not self._ready():
            return 0
        return int(self.conn.execute("SELECT count(*) FROM lifesci_links WHERE namespace=?",
                                     [namespace]).fetchone()[0])


__all__ = ["BASES", "LifeSciLinks", "normalize_doi"]
