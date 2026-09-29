"""Substances linked to regulations, product notices, materials and toxicology literature by citation (#2212, CH08 #2303).

Links are records of an explicit citation, stored with the citing text and a
locator, and only ever read other packs through their own stores:

* **legal acts** - a classification, restriction, authorisation or
  Candidate List revision names its legal act (CELEX/ELI) and entry; the act
  resolves to a ``legal.works`` record through :class:`src.kb.legal.LegalStore`
  by that exact identifier (``cited-act``). The regulation text behind a
  restriction is then read from the Legal owner by the entry locator;
* **product safety notices** (``products.safety``, #1916) - a notice revision
  whose hazard, identification or corrective-action text states a CAS, EC,
  InChIKey or DTXSID the substance publishes (``cited-identifier``) or one of
  the substance's own published names as an exact, whole-word string
  (``explicit-name-mention``), read through
  :class:`src.kb.product_safety.ProductSafetyStore`;
* **Materials** (#2060) and **Clinical toxicology literature** - only a
  stated CAS, EC, InChIKey or DTXSID, or a reviewed substance identity
  (``ent-substance-...``) that the record cites; never a name.

No link is inferred from name similarity, category or topic, and a link joins
a substance to other records only through that substance's own published
identifiers and names: a rejected identity candidate never carries a link to
another record. No exposure, causation or hazard claim is made by a link.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.substances_records import (
    LINK_CONTRACT,
    NAME_SCHEMES,
    READ_SCOPE,
    WRITE_SCOPE,
    SubstanceError,
    authorize,
    canonical,
    digest,
    identifier_key,
    name_key,
    require,
)
from src.kb.substances_store import SubstanceStore, table_exists

LEGAL_READ = "knowledge:legal:read"
PRODUCTS_READ = "knowledge:products:read"
LITERATURE_READ = "knowledge:read"
OWNERS = ("legal", "products", "materials", "clinical")
BASES = ("cited-act", "cited-identifier", "explicit-name-mention", "cited-reviewed-identity")
MIN_NAME_LENGTH = 5
LINKED_RECORD_TYPES = ("classification", "restriction", "authorisation", "candidate_listing")
_PATTERNS = {
    "cas": re.compile(r"(?<![\d-])(\d{2,7}-\d{2}-\d)(?![\d-])"),
    "ec": re.compile(r"(?<![\d-])(\d{3}-\d{3}-\d)(?![\d-])"),
    "inchikey": re.compile(r"(?<![A-Z])([A-Z]{14}-[A-Z]{10}-[A-Z])(?![A-Z])"),
    "dtxsid": re.compile(r"\b(DTXSID\d{7,12})\b"),
}
_ENTITY = re.compile(r"\b(ent-substance-[a-z0-9-]+)\b")
_DDL = """
CREATE TABLE IF NOT EXISTS substance_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_key TEXT NOT NULL, source_revision_id TEXT,
  owner TEXT NOT NULL, target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL, target_id TEXT NOT NULL,
  target_revision TEXT, basis TEXT NOT NULL, matched TEXT NOT NULL, citing_text TEXT NOT NULL, locator_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def cited_identifiers(text: Any) -> list[tuple[str, str]]:
    """(scheme, key) of every well-formed CAS, EC, InChIKey or DTXSID a text states (check digits verified)."""
    found = []
    for scheme, pattern in _PATTERNS.items():
        for match in pattern.finditer(str(text or "")):
            key = identifier_key(scheme, match.group(1))
            if key and (scheme, key) not in found:
                found.append((scheme, key))
    return found


def _mentions(text: str, name: str) -> bool:
    return bool(re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", text, flags=re.IGNORECASE))


class SubstanceLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = SubstanceStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _insert(self, namespace, subject_key, source_revision_id, owner, target_kind, target_namespace, target_id,
                target_revision, basis, matched, citing_text, locator, principal_id) -> dict[str, Any] | None:
        link_id = "substance-link:" + digest([namespace, subject_key, source_revision_id, owner, target_id,
                                              target_revision, basis, matched])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO substance_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING link_id",
            [namespace, link_id, subject_key, source_revision_id, owner, target_kind, target_namespace, target_id,
             target_revision, basis, matched, str(citing_text), canonical(locator), principal_id,
             self.now()]).fetchall()
        return {"link_id": link_id, "subject_key": subject_key, "owner": owner, "target_id": target_id,
                "basis": basis, "matched": matched} if inserted else None

    # ------------------------------------------------------------------ legal

    def link_legal(self, namespace: str, *, scopes: Iterable[str], principal_id: str,
                   legal_namespace: str | None = None) -> dict[str, Any]:
        """Resolve each cited act by its exact CELEX/ELI to a Legal work; unresolved citations are listed."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require(scopes, LEGAL_READ)
        self.store.require_ready()
        legal_namespace = legal_namespace or namespace
        if not table_exists(self.conn, "legal_works"):
            return {"namespace": namespace, "linked": [], "unresolved": [], "stores": {"legal": False},
                    "status": "legal store not available; nothing linked"}
        from src.kb.legal import LegalError, LegalStore

        legal = LegalStore(self.conn, initialize=False)
        legal_scopes = scopes | {LEGAL_READ, f"namespace:{legal_namespace}:read"} if "operator" in scopes else scopes
        created, unresolved = [], []
        for record_type in LINKED_RECORD_TYPES:
            for record in self.store.records(namespace, record_type=record_type):
                for revision in self.store.revisions(namespace, record["record_id"]):
                    act = revision["legal_act"]
                    if not act:
                        continue
                    work = None
                    for label in ("celex", "eli"):
                        if not act.get(label):
                            continue
                        try:
                            found = legal.lookup(legal_namespace, scopes=legal_scopes, identifier=act[label])
                        except LegalError:
                            continue
                        if found["status"] == "found":
                            work = (found["works"][0], label, act[label])
                            break
                    if work is None:
                        unresolved.append({"revision_id": revision["revision_id"], "act": act["title"],
                                           "celex": act.get("celex"), "reason": "no Legal work with that exact "
                                                                                "identifier is acquired"})
                        continue
                    versions = legal.versions(legal_namespace, work[0]["work_id"])
                    link = self._insert(
                        namespace, record["subject_key"], revision["revision_id"], "legal", "legal-work",
                        legal_namespace, work[0]["work_id"], versions[-1]["version_id"] if versions else None,
                        "cited-act", f"{work[1]}:{work[2]}",
                        " - ".join(x for x in (act["title"], act.get("entry")) if x),
                        {"entry": act.get("entry"), "locator": act.get("locator"), "record_type": record_type,
                         "statement_locator": revision["statement"]["source"]["locator"]}, principal_id)
                    if link:
                        created.append(link)
        return {"namespace": namespace, "linked": created, "unresolved": unresolved, "stores": {"legal": True},
                "policy": "exact CELEX/ELI only; never by title, topic or hazard similarity"}

    def regulation_text(self, namespace: str, link: Mapping[str, Any], *, scopes: Iterable[str]) -> dict[str, Any]:
        """The Legal owner's passages for a cited act at the cited entry (exact locator or entry substring)."""
        from src.kb.legal import LegalError, LegalStore

        scopes = set(scopes)
        require(scopes, LEGAL_READ)
        if not link.get("target_revision"):
            return {"status": "metadata_only", "passages": []}
        legal = LegalStore(self.conn, initialize=False)
        entry = (link.get("locator") or {}).get("entry") or ""
        cited = re.findall(r"(entry|index)\s+(?:No\s+)?([\w.-]+)", entry, flags=re.IGNORECASE)
        target_scopes = (scopes | {LEGAL_READ, f"namespace:{link['target_namespace']}:read"} if "operator" in scopes
                         else scopes)
        try:
            passages = legal.passages(link["target_namespace"], link["target_revision"], scopes=target_scopes,
                                      locator=f"{cited[-1][0].lower()} {cited[-1][1]}" if cited else None)
        except LegalError as exc:
            return {"status": getattr(exc, "code", "unavailable"), "passages": []}
        return {"status": passages["status"], "version_id": link["target_revision"], "work_id": passages["work_id"],
                "passages": passages["passages"], "notice": passages["notice"]}

    # ------------------------------------------------------------------ names and identifiers

    def _index(self, namespace: str) -> tuple[dict[tuple[str, str], list[str]], dict[str, list[tuple[str, str]]]]:
        identifiers: dict[tuple[str, str], list[str]] = {}
        names: dict[str, list[tuple[str, str]]] = {}
        for row in self.store.identifiers(namespace):
            if row["value_key"] is None:
                continue
            if row["scheme"] in NAME_SCHEMES:
                if len(row["value"]) >= MIN_NAME_LENGTH and not row["value"].replace("-", "").isdigit():
                    names.setdefault(row["subject_key"], []).append((row["value"], row["value_key"]))
            else:
                identifiers.setdefault((row["scheme"], row["value_key"]), []).append(row["subject_key"])
        return identifiers, names

    def _link_text(self, namespace, owner, target_kind, target_namespace, target_id, target_revision, fields,
                   identifiers, names, principal_id, *, allow_names: bool, allow_entities: bool) -> list[dict]:
        created = []
        known_entities = {}
        if allow_entities:
            from src.kb.substances_identity import entity_id

            known_entities = {entity_id(s["subject_key"]): s["subject_key"] for s in self.store.subjects(namespace)}
        for locator, text in fields:
            for scheme, key in cited_identifiers(text):
                for subject in sorted(set(identifiers.get((scheme, key), []))):
                    link = self._insert(namespace, subject, None, owner, target_kind, target_namespace, target_id,
                                        target_revision, "cited-identifier", f"{scheme}:{key}", text, locator,
                                        principal_id)
                    created += [link] if link else []
            if allow_entities:
                for entity in _ENTITY.findall(str(text)):
                    subject = known_entities.get(entity)
                    if subject:
                        link = self._insert(namespace, subject, None, owner, target_kind, target_namespace,
                                            target_id, target_revision, "cited-reviewed-identity", entity, text,
                                            locator, principal_id)
                        created += [link] if link else []
            if allow_names:
                for subject, published in sorted(names.items()):
                    seen = set()
                    for value, key in published:
                        if key in seen or not _mentions(str(text), value):
                            continue
                        seen.add(key)
                        link = self._insert(namespace, subject, None, owner, target_kind, target_namespace,
                                            target_id, target_revision, "explicit-name-mention", f"name:{key}", text,
                                            locator, principal_id)
                        created += [link] if link else []
        return created

    def link_product_notices(self, namespace: str, *, scopes: Iterable[str], principal_id: str,
                             products_namespace: str | None = None) -> dict[str, Any]:
        """Link notice revisions that state a substance's identifier or one of its own published names."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require(scopes, PRODUCTS_READ)
        self.store.require_ready()
        products_namespace = products_namespace or namespace
        if not table_exists(self.conn, "product_safety_revisions"):
            return {"namespace": namespace, "linked": [], "notices_read": 0, "stores": {"products.safety": False}}
        from src.kb.product_safety import ProductSafetyStore

        notices = ProductSafetyStore(self.conn, initialize=False)
        identifiers, names = self._index(namespace)
        created, read = [], 0
        for (notice_id,) in self.conn.execute(
                "SELECT notice_id FROM product_safety_notices WHERE namespace=? ORDER BY notice_id",
                [products_namespace]).fetchall():
            for revision in notices.revisions(products_namespace, notice_id):
                read += 1
                parts = notices.parts(products_namespace, revision["revision_id"])
                fields = [({"part": "hazard", "hazard_id": h["hazard_id"], **(h.get("locator") or {})},
                           h["description"]) for h in parts["hazards"] if h.get("description")]
                fields += [({"part": "corrective_action", "action_id": a["action_id"], **(a.get("locator") or {})},
                            a["text"]) for a in parts["corrective_actions"] if a.get("text")]
                fields += [({"part": "identification", "identification_id": i["identification_id"],
                             **(i.get("locator") or {})}, i["value"]) for i in parts["identifications"]
                           if i.get("value")]
                created += self._link_text(namespace, "products", "product-safety-notice", products_namespace,
                                           notice_id, revision["revision_id"], fields, identifiers, names,
                                           principal_id, allow_names=True, allow_entities=False)
        return {"namespace": namespace, "linked": created, "notices_read": read, "stores": {"products.safety": True},
                "policy": "a stated CAS/EC/InChIKey/DTXSID or one of the substance's own published names as a whole "
                          "word; never similarity, category or hazard type; no exposure or causation claim"}

    def link_citing_records(self, namespace: str, owner: str, records: Sequence[Mapping[str, Any]], *,
                            scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Materials or Clinical records: only a stated identifier or a reviewed substance identity is a link."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if owner not in {"materials", "clinical"}:
            raise SubstanceError("invalid_request", "citing records come from materials or clinical")
        identifiers, _ = self._index(namespace)
        created = []
        for record in records:
            fields = [({"field": key}, record[key]) for key in ("title", "text") if record.get(key)]
            fields += [({"field": f"identifiers.{scheme}"}, value)
                       for scheme, values in sorted(dict(record.get("identifiers") or {}).items())
                       for value in values]
            created += self._link_text(namespace, owner, str(record.get("kind") or f"{owner}-record"),
                                       str(record.get("namespace") or namespace), str(record["record_id"]),
                                       record.get("revision_id"), fields, identifiers, {}, principal_id,
                                       allow_names=False, allow_entities=True)
        return {"namespace": namespace, "owner": owner, "linked": created, "records_read": len(records),
                "policy": "explicit CAS/EC/InChIKey/DTXSID or reviewed substance identity only; never a name"}

    def link_literature(self, namespace: str, *, scopes: Iterable[str], principal_id: str,
                        limit: int = 5000) -> dict[str, Any]:
        """Toxicology literature (scholarly documents in the shared document store) that cites an identifier."""
        scopes = set(scopes)
        require(scopes, LITERATURE_READ)
        if not table_exists(self.conn, "documents"):
            return {"namespace": namespace, "owner": "clinical", "linked": [], "records_read": 0,
                    "stores": {"documents": False}}
        rows = self.conn.execute("SELECT document_id, title, content FROM documents WHERE source_type='paper' "
                                 "ORDER BY document_id LIMIT ?", [int(limit)]).fetchall()
        records = [{"record_id": r[0], "kind": "literature-document", "title": r[1], "text": r[2]} for r in rows]
        return self.link_citing_records(namespace, "clinical", records, scopes=scopes, principal_id=principal_id)

    def link_materials(self, namespace: str, reader: Callable[[], Sequence[Mapping[str, Any]]] | None, *,
                       scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Materials records read through the Materials capability's reader; unavailable when none is present."""
        if reader is None:
            return {"namespace": namespace, "owner": "materials", "linked": [], "records_read": 0,
                    "status": "provider_unavailable", "note": "no Materials provider is composed in this deployment"}
        return self.link_citing_records(namespace, "materials", list(reader()), scopes=scopes,
                                        principal_id=principal_id)

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, subject_keys: Iterable[str], *, scopes: Iterable[str],
              owner: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        keys = sorted(set(subject_keys))
        if not keys or not table_exists(self.conn, "substance_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, subject_key, source_revision_id, owner, target_kind, target_namespace, target_id, "
            "target_revision, basis, matched, citing_text, locator_json, created_by, created_at_ms FROM substance_links "
            "WHERE namespace=? AND (? IS NULL OR owner=?) AND subject_key IN (" + ",".join("?" * len(keys)) + ") "
            "ORDER BY owner, target_id, link_id", [namespace, owner, owner, *keys]).fetchall()
        import json

        return [{"contract": LINK_CONTRACT, **dict(zip(
            ("link_id", "subject_key", "source_revision_id", "owner", "target_kind", "target_namespace", "target_id",
             "target_revision", "basis", "matched", "citing_text"), r[:11])), "locator": json.loads(r[11]),
            "created_by": r[12], "created_at_ms": int(r[13])} for r in rows]

    def generation(self, namespace: str) -> int:
        if not table_exists(self.conn, "substance_links"):
            return 0
        return int(self.conn.execute("SELECT count(*) FROM substance_links WHERE namespace=?",
                                     [namespace]).fetchone()[0])


def name_mentioned(text: str, name: str) -> bool:
    """Public helper for tests and reviewers: the whole-word, case-insensitive rule used for name mentions."""
    return len(name) >= MIN_NAME_LENGTH and _mentions(text, name) and bool(name_key(name))


__all__ = ["BASES", "OWNERS", "SubstanceLinks", "cited_identifiers", "name_mentioned"]
