"""Patent publications for Research and Technical: families, citations, legal events and claims.

Projected from EPO OPS parts (``noesis-patent-part-v1``). The store keeps
provider assertions as assertions:

* **Publications** keyed by docdb number, with application, priorities,
  multilingual titles/abstracts, applicants/inventors as provider strings,
  classifications and immutable biblio revisions (changed bibliographic data
  is a new revision; earlier ones stay).
* **Families** are INPADOC membership *as asserted by OPS* for one
  publication at one observation; a later observation can differ.
* **Citations**: patent citations keep the cited docdb number; non-patent
  citations keep their text and any DOI found in it.
* **Legal events** keep code, description, date and locator per observation.
  A publication with no legal data is ``legal_status: not_available``, not
  "in force" or "lapsed". Validity, enforceability and freedom to operate
  are never inferred.
* **Links** to scholarly works, organizations and standards are either the
  patent's own sourced citations (a DOI in a non-patent citation) or
  reviewed assertions; candidates stay candidates.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

READ_SCOPE = "knowledge:patents:read"
WRITE_SCOPE = "knowledge:patents:write"
REVIEW_SCOPE = "knowledge:patents:review"
PUBLICATION_CONTRACT = "noesis-patent-publication-v1"
LINK_CONTRACT = "noesis-patent-link-v1"
DEFAULT_NAMESPACE = "global"
LINK_TARGETS = frozenset({"scholarly_work", "organization", "standard"})
NOTICE = ("Patent records are publication and legal-event data from the provider; they establish neither "
          "validity, enforceability nor freedom to operate.")

_DDL = """
CREATE TABLE IF NOT EXISTS patent_publications (
  publication_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, docdb TEXT NOT NULL, country TEXT, kind TEXT,
  created_run_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS patent_part_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, publication_id TEXT NOT NULL, part TEXT NOT NULL,
  raw_sha256 TEXT NOT NULL, data_json TEXT NOT NULL, document_id TEXT, first_run_id TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS patent_part_current (
  publication_id TEXT NOT NULL, part TEXT NOT NULL, namespace TEXT NOT NULL, revision_id TEXT, outcome TEXT NOT NULL,
  run_id TEXT NOT NULL, updated_at_ms BIGINT NOT NULL, PRIMARY KEY(publication_id, part)
);
CREATE TABLE IF NOT EXISTS patent_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, publication_id TEXT NOT NULL, target_kind TEXT NOT NULL,
  target_id TEXT NOT NULL, state TEXT NOT NULL, basis TEXT NOT NULL, evidence TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
"""


class PatentError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def _authorize(namespace: str, scopes, required: str, *, write: bool) -> None:
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & set(scopes):
        raise PatentError("unauthorized", f"{required} and namespace access are required")


class PatentStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _publication(self, namespace: str, docdb: str, run_id: str) -> str:
        publication_id = "patent-publication:" + _digest([namespace, docdb])[:24]
        country, _, rest = docdb.partition(".")
        self.conn.execute("INSERT OR IGNORE INTO patent_publications VALUES (?,?,?,?,?,?,?)",
                          [publication_id, namespace, docdb, country, rest.rpartition(".")[2] or None, run_id,
                           self.now()])
        return publication_id

    def observe_page(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str,
                     page_receipt: Mapping[str, Any], documents: Mapping[str, str] | None = None) -> dict[str, int]:
        observed = self.now()
        counts = {"revisions": 0}
        self.conn.execute("BEGIN")
        try:
            if not records and page_receipt.get("outcome") == "not_available":
                publication_id = self._publication(namespace, page_receipt["publication"], run_id)
                self.conn.execute(
                    "INSERT OR REPLACE INTO patent_part_current VALUES (?,?,?,?,?,?,?)",
                    [publication_id, page_receipt["part"], namespace, None, "not_available", run_id, observed])
            for item in records:
                record = dict(item.get("patent_record") or {})
                publication_id = self._publication(namespace, record["publication"], run_id)
                revision_id = "patent-revision:" + _digest([publication_id, record["part"], record["raw_sha256"]])[:24]
                inserted = self.conn.execute(
                    "INSERT OR IGNORE INTO patent_part_revisions VALUES (?,?,?,?,?,?,?,?,?) RETURNING revision_id",
                    [revision_id, namespace, publication_id, record["part"], record["raw_sha256"],
                     _canonical(record["data"]), (documents or {}).get(str(item.get("id"))), run_id,
                     observed]).fetchall()
                counts["revisions"] += len(inserted)
                self.conn.execute(
                    "INSERT OR REPLACE INTO patent_part_current VALUES (?,?,?,?,?,?,?)",
                    [publication_id, record["part"], namespace, revision_id, "returned", run_id, observed])
                if record["part"] == "biblio":
                    for citation in record["data"].get("citations") or []:
                        if citation.get("doi"):
                            self._link(namespace, publication_id, "scholarly_work", "doi:" + citation["doi"].lower(),
                                       "linked", "patent-npl-citation", citation["locator"], "system:patents")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def _link(self, namespace, publication_id, target_kind, target_id, state, basis, evidence, principal_id):
        link_id = "patent-link:" + _digest([namespace, publication_id, target_kind, target_id])[:24]
        self.conn.execute(
            "INSERT INTO patent_links VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT (link_id) DO UPDATE SET "
            "state=excluded.state, basis=excluded.basis, evidence=excluded.evidence, principal_id=excluded.principal_id",
            [link_id, namespace, publication_id, target_kind, target_id, state, basis, evidence, principal_id,
             self.now()])
        return link_id

    def _part(self, publication_id: str, part: str) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
        current = self.conn.execute(
            "SELECT outcome, revision_id FROM patent_part_current WHERE publication_id=? AND part=?",
            [publication_id, part]).fetchone()
        history = self.conn.execute(
            "SELECT revision_id, raw_sha256, first_run_id, observed_at_ms FROM patent_part_revisions "
            "WHERE publication_id=? AND part=? ORDER BY observed_at_ms, revision_id", [publication_id, part]).fetchall()
        revisions = [dict(zip(("revision_id", "raw_sha256", "first_run_id", "observed_at_ms"), r)) for r in history]
        if current is None:
            return "not_acquired", None, revisions
        if current[1] is None:
            return current[0], None, revisions
        data = self.conn.execute("SELECT data_json FROM patent_part_revisions WHERE revision_id=?",
                                 [current[1]]).fetchone()
        return current[0], {"revision_id": current[1], **_load(data[0], {})}, revisions

    def publication(self, namespace: str, docdb: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        row = self.conn.execute("SELECT publication_id FROM patent_publications WHERE namespace=? AND docdb=?",
                                [namespace, docdb]).fetchone()
        if row is None:
            raise PatentError("not_found", "publication is not visible in this namespace")
        publication_id = row[0]
        parts = {part: self._part(publication_id, part) for part in ("biblio", "family", "legal", "claims")}
        biblio = parts["biblio"][1] or {}
        legal_state, legal, _ = parts["legal"]
        family_state, family, _ = parts["family"]
        claims_state, claims, _ = parts["claims"]
        return {
            "contract": PUBLICATION_CONTRACT, "publication_id": publication_id, "docdb": docdb,
            "biblio": biblio, "biblio_revisions": parts["biblio"][2],
            "family": {"state": family_state, "asserted_by": "EPO OPS (INPADOC)",
                       "members": (family or {}).get("members") or [], "revision_id": (family or {}).get("revision_id")},
            "legal_status": {"state": legal_state if legal_state != "returned" or (legal or {}).get("events")
                             else "no_events_reported",
                             "events": (legal or {}).get("events") or [],
                             "revision_id": (legal or {}).get("revision_id")},
            "claims": {"state": claims_state, "languages": sorted({c["language"] for c in (claims or {}).get("claims") or []
                                                                    if c.get("language")}),
                       "items": (claims or {}).get("claims") or []},
            "links": self.links(namespace, publication_id=publication_id),
            "notice": NOTICE,
        }

    def family_members(self, namespace: str, docdb: str, *, scopes) -> dict[str, Any]:
        """All acquired publications that any acquired family assertion puts together with ``docdb``."""

        record = self.publication(namespace, docdb, scopes=scopes)
        members = {m["publication"]["docdb"] for m in record["family"]["members"]}
        rows = self.conn.execute("SELECT docdb FROM patent_publications WHERE namespace=?", [namespace]).fetchall()
        acquired = {r[0] for r in rows}
        return {"docdb": docdb, "family_state": record["family"]["state"],
                "asserted_members": sorted(members), "acquired_members": sorted(members & acquired),
                "not_acquired_members": sorted(members - acquired),
                "notice": "Family membership is the provider's assertion at the recorded observation."}

    def links(self, namespace: str, *, publication_id: str | None = None,
              target_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT link_id, publication_id, target_kind, target_id, state, basis, evidence, principal_id "
            "FROM patent_links WHERE namespace=? AND (? IS NULL OR publication_id=?) AND (? IS NULL OR target_id=?) "
            "ORDER BY target_kind, target_id",
            [namespace, publication_id, publication_id, target_id, target_id]).fetchall()
        return [{"contract": LINK_CONTRACT, **dict(zip(("link_id", "publication_id", "target_kind", "target_id",
                                                        "state", "basis", "evidence", "principal_id"), r))}
                for r in rows]

    def propose_link(self, namespace: str, docdb: str, target_kind: str, target_id: str, evidence: str, *, scopes,
                     principal_id: str) -> dict[str, Any]:
        """A reviewable candidate link to an organization, standard or scholarly work."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if target_kind not in LINK_TARGETS or not str(evidence or "").strip():
            raise PatentError("invalid_link", f"target_kind must be one of {sorted(LINK_TARGETS)} with evidence")
        publication_id = self.publication(namespace, docdb, scopes=scopes)["publication_id"]
        link_id = self._link(namespace, publication_id, target_kind, target_id, "candidate", "proposed",
                             evidence.strip(), principal_id)
        return next(link for link in self.links(namespace, publication_id=publication_id) if link["link_id"] == link_id)

    def review_link(self, namespace: str, link_id: str, decision: str, reason: str, *, scopes,
                    principal_id: str) -> dict[str, Any]:
        _authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accepted", "rejected"} or not str(reason or "").strip():
            raise PatentError("invalid_decision", "accept or reject with a reason")
        row = self.conn.execute("SELECT basis FROM patent_links WHERE namespace=? AND link_id=?",
                                [namespace, link_id]).fetchone()
        if row is None:
            raise PatentError("not_found", "link is not visible in this namespace")
        if row[0] == "patent-npl-citation":
            raise PatentError("sourced_link", "a link from the patent's own citation is source data, not a candidate")
        self.conn.execute(
            "UPDATE patent_links SET state=?, basis=?, evidence=evidence || ?, principal_id=? WHERE link_id=?",
            ["linked" if decision == "accepted" else "rejected",
             "reviewed-assertion" if decision == "accepted" else "reviewed-rejection",
             " | review: " + reason.strip(), principal_id, link_id])
        return next(link for link in self.links(namespace) if link["link_id"] == link_id)


class PatentProjector:
    def __init__(self, conn: Any) -> None:
        self.store = PatentStore(conn)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        namespace = str(dict(source.get("patent") or {}).get("namespace") or DEFAULT_NAMESPACE)
        document_ids = {str(dict(d.get("metadata") or {}).get("source_pack_record_id")): str(d["document_id"])
                        for d in documents}
        return self.store.observe_page(namespace, records, run_id=run_id, page_receipt=page_receipt,
                                       documents=document_ids)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}
