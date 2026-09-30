"""Hazard events and advisories linked to other records by citation only (NH09, #2347).

A link is created only on one of three bases, and records which:

* ``citation`` — the other record explicitly cites the hazard record's
  published identifier (a news or OSINT document quoting a USGS event ID, a
  GLIDE number or an NHC storm ID); the quote is kept as evidence.
* ``shared_identifier`` — both records publish the same identifier (GLIDE,
  storm ID, event ID), e.g. a ``climate-environment`` record carrying a GLIDE.
* ``accepted_correspondence`` — an NH08 correspondence a reviewer accepted.

Every link points at specific revisions on both sides (the hazard record
revision, and the target's revision id or content hash). Targets whose
provider is not shipped in this deployment — weather warnings (#2163) and
Humanitarian Response reports (#2206) — are reported as ``unavailable``, never
silently skipped. Co-occurrence in time or space is not a basis: no link
asserts that one event caused, worsened or was attributed to anything.
"""

from __future__ import annotations

import importlib.util
import json

from src.kb.hazards_records import READ_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.hazards_store import HazardStore, HazardStoreError, authorize

CONTRACT = "noesis-hazard-link-v1"
BASES = ("citation", "shared_identifier", "accepted_correspondence")
NO_CAUSATION = "a citation or shared identifier only; no causal, attribution or impact claim is made"
# Optional record owners: (module candidates, table, issue). Absent owners are reported unavailable.
OPTIONAL_PROVIDERS = {
    "weather": (("src.kb.weather_warnings", "src.kb.weather_records", "src.kb.weather"), "weather_warnings", "#2163"),
    "humanitarian": (("src.kb.humanitarian_reports", "src.kb.humanitarian_records", "src.kb.humanitarian"),
                     "humanitarian_reports", "#2206"),
}
_DDL = """
CREATE TABLE IF NOT EXISTS hazard_links(
 link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_id TEXT NOT NULL, record_revision_id TEXT NOT NULL,
 target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL, target_id TEXT NOT NULL, target_revision TEXT NOT NULL,
 basis TEXT NOT NULL, evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def identifier_tokens(content):
    """Published identifiers specific enough to be cited verbatim (never shortened or normalised)."""

    ids = dict(content.get("identifiers") or {})
    tokens = {content["native_id"]} if content["provider"] in {"usgs", "emsc", "nhc"} else set()
    tokens |= set(ids.get("ids") or [])
    for key in ("glide", "storm_id", "unid", "notification_id"):
        if ids.get(key):
            tokens.add(str(ids[key]))
    if content.get("storm_id"):
        tokens.add(content["storm_id"])
    return sorted(t for t in tokens if len(t) >= 8)


def linked_providers(conn):
    """Which target owners exist here; a missing one is reported, not skipped."""

    state = {
        "climate-environment": {"status": "available" if _table(conn, "environment_record_revisions")
                                else "unavailable", "reason": None if _table(conn, "environment_record_revisions")
                                else "no environment records in this deployment"},
        "documents (news, osint)": {"status": "available" if _table(conn, "documents") else "unavailable",
                                    "reason": None if _table(conn, "documents") else "document store not initialised"},
        "natural-hazards (accepted correspondences)": {"status": "available", "reason": None},
    }
    for name, (modules, table, issue) in OPTIONAL_PROVIDERS.items():
        shipped = any(importlib.util.find_spec(m) is not None for m in modules)
        state[name] = {"status": "available" if shipped and _table(conn, table) else "unavailable",
                       "reason": None if shipped and _table(conn, table) else
                       f"{name} provider ({issue}) is not shipped or has no records in this deployment"}
    return state


class HazardLinks:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.store = HazardStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def providers(self):
        """Which target owners exist here; a missing one is reported, not skipped."""

        return linked_providers(self.conn)

    # ------------------------------------------------------------------ writes

    def link(self, namespace, record_id, *, target, basis, evidence, principal_id, scopes, record_revision_id=None):
        """Record one link on an explicit basis between specific revisions."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if basis not in BASES:
            raise HazardStoreError("invalid_basis", f"a link's basis is one of {BASES}; co-occurrence is not a basis")
        if not isinstance(evidence, dict) or not evidence:
            raise HazardStoreError("evidence_required", "a link records the citation, identifier or correspondence")
        kind = str(target.get("kind") or "")
        target_ns = str(target.get("namespace") or namespace)
        target_id, revision = str(target.get("id") or ""), str(target.get("revision") or "")
        if not target_id or not revision:
            raise HazardStoreError("revision_required", "links point at a specific target revision")
        providers = self.providers()
        if kind in OPTIONAL_PROVIDERS and providers[kind]["status"] != "available":
            raise HazardStoreError("provider_unavailable", providers[kind]["reason"])
        if not self._target_exists(kind, target_ns, target_id, revision):
            raise HazardStoreError("target_not_found", f"{kind} {target_id} revision {revision} is not on record")
        record = self.store.record(namespace, record_id, scopes=scopes)
        pinned = record_revision_id or record["revision_id"]
        if not self.conn.execute("SELECT 1 FROM hazard_record_revisions WHERE record_id=? AND revision_id=?",
                                 [record_id, pinned]).fetchone():
            raise HazardStoreError("revision_required", "the hazard revision is not on record")
        link_id = "hazard-link:" + digest([namespace, pinned, kind, target_ns, target_id, revision, basis])[:24]
        self.conn.execute("INSERT INTO hazard_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [link_id, namespace, record_id, pinned, kind, target_ns, target_id, revision, basis,
                           canonical(evidence), principal_id, self.now()])
        return self._link(link_id)

    def _target_exists(self, kind, namespace, target_id, revision):
        if kind == "environment-record":
            return _table(self.conn, "environment_record_revisions") and bool(self.conn.execute(
                "SELECT 1 FROM environment_record_revisions WHERE record_id=? AND revision_id=?", [target_id, revision]).fetchone())
        if kind == "document":
            return _table(self.conn, "documents") and bool(self.conn.execute(
                "SELECT 1 FROM documents WHERE document_id=? AND content_hash=?", [target_id, revision]).fetchone())
        if kind == "hazard-record":
            return bool(self.conn.execute("SELECT 1 FROM hazard_record_revisions WHERE record_id=? AND revision_id=? "
                                          "AND namespace=?", [target_id, revision, namespace]).fetchone())
        if kind in OPTIONAL_PROVIDERS:
            return True  # the owner validated availability; its own reader resolves the revision
        raise HazardStoreError("invalid_target", f"unknown link target kind {kind!r}")

    def discover(self, namespace, record_id, *, principal_id, scopes):
        """Create links from explicit citations, shared identifiers and accepted correspondences only."""

        from src.kb.hazards_identity import HazardIdentity

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        record = self.store.record(namespace, record_id, scopes=scopes)
        tokens = identifier_tokens(record["content"])
        created = []
        providers = self.providers()
        if providers["documents (news, osint)"]["status"] == "available":
            for token in tokens:
                for document_id, content_hash, source_type, title, content in self.conn.execute(
                        "SELECT document_id, content_hash, source_type, title, content FROM documents WHERE "
                        "position(? IN coalesce(content, '')) > 0 OR position(? IN coalesce(title, '')) > 0 "
                        "ORDER BY document_id LIMIT 50", [token, token]).fetchall():
                    text = content or title or ""
                    at = text.find(token)
                    quote = text[max(0, at - 80): at + len(token) + 80] if at >= 0 else title
                    created.append(self.link(namespace, record_id, principal_id=principal_id, scopes=scopes,
                                             target={"kind": "document", "id": document_id, "revision": content_hash},
                                             basis="citation", evidence={"cited_identifier": token, "quote": quote,
                                                                         "source_type": source_type}))
        if providers["climate-environment"]["status"] == "available":
            for token in tokens:
                for env_id, revision_id, content in self.conn.execute(
                        "SELECT c.record_id, c.revision_id, r.content_json FROM environment_record_current c "
                        "JOIN environment_record_revisions r USING(revision_id) WHERE position(? IN r.content_json) > 0 "
                        "ORDER BY c.record_id LIMIT 50", [token]).fetchall():
                    published = json.loads(content).get("identifiers") or {}
                    if token not in json.dumps(published):
                        continue  # the token must be a published identifier, not free text
                    created.append(self.link(namespace, record_id, principal_id=principal_id, scopes=scopes,
                                             target={"kind": "environment-record", "id": env_id, "revision": revision_id,
                                                     "namespace": namespace},
                                             basis="shared_identifier", evidence={"identifier": token,
                                                                                  "target_identifiers": published}))
        identity = HazardIdentity(self.conn, initialize=False, now=self.now)
        for cid, other in identity.accepted_for(namespace, record_id):
            view = self.store.record(namespace, other, scopes=scopes)
            created.append(self.link(namespace, record_id, principal_id=principal_id, scopes=scopes,
                                     target={"kind": "hazard-record", "id": other, "revision": view["revision_id"]},
                                     basis="accepted_correspondence", evidence={"correspondence_id": cid}))
        unavailable = {k: v for k, v in providers.items() if v["status"] != "available"}
        return {"contract": CONTRACT, "record_id": record_id, "record_revision_id": record["revision_id"],
                "identifiers": tokens, "links": sorted({link["link_id"]: link for link in created}.values(),
                                                       key=lambda item: item["link_id"]),
                "unavailable_providers": unavailable, "claims": NO_CAUSATION}

    # ------------------------------------------------------------------- reads

    def _link(self, link_id):
        row = self.conn.execute("SELECT namespace, record_id, record_revision_id, target_kind, target_namespace, target_id, "
                                "target_revision, basis, evidence_json, created_by, created_at_ms FROM hazard_links "
                                "WHERE link_id=?", [link_id]).fetchone()
        return {"contract": CONTRACT, "link_id": link_id, "namespace": row[0], "record_id": row[1],
                "record_revision_id": row[2],
                "target": {"kind": row[3], "namespace": row[4], "id": row[5], "revision": row[6]},
                "basis": row[7], "evidence": json.loads(row[8]), "created_by": row[9], "created_at_ms": int(row[10]),
                "claims": NO_CAUSATION}

    def links(self, namespace, *, scopes, record_id=None):
        authorize(namespace, scopes, READ_SCOPE)
        if not _table(self.conn, "hazard_links"):
            return []
        return [self._link(r[0]) for r in self.conn.execute(
            "SELECT link_id FROM hazard_links WHERE namespace=? AND (? IS NULL OR record_id=?) ORDER BY link_id",
            [namespace, record_id, record_id]).fetchall()]
