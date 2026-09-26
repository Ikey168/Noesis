"""LEI company identities and ownership assertions (GLEIF) linked to existing company records.

Projected from GLEIF parts (``noesis-lei-part-v1``). The store keeps:

* **LEI record revisions** (keyed by payload; GLEIF ``lastUpdateDate`` kept as
  the provider revision) with legal name, other/previous names, legal and
  headquarters addresses as distinct places, jurisdiction, legal form, entity
  and registration status, successor, and the registration authority plus
  registered-as number (the official-register pointer).
* **Parent assertions** (direct/ultimate) with relationship status, periods
  and the observation that reported them. A later observation reporting a
  different parent does not delete the earlier assertion; ``parents_as_of``
  answers from what was observed by a date.
* **Reporting exceptions** (e.g. natural persons, non-consolidating) as their
  own records; "none reported" is kept distinct from an exception.
* **Identity links** from an LEI to existing records (OpenCorporates documents
  from the regional provider, market issuers) are reviewable candidates on
  jurisdiction + normalized registry number; similarly named entities are
  never merged.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

READ_SCOPE = "knowledge:companies:read"
WRITE_SCOPE = "knowledge:companies:write"
REVIEW_SCOPE = "knowledge:companies:review"
ENTITY_CONTRACT = "noesis-lei-entity-v1"
LINK_CONTRACT = "noesis-company-identity-link-v1"
DEFAULT_NAMESPACE = "global"
STALE_REGISTRATION = frozenset({"LAPSED", "RETIRED", "ANNULLED", "DUPLICATE", "MERGED", "TRANSFERRED"})

_DDL = """
CREATE TABLE IF NOT EXISTS lei_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, lei TEXT NOT NULL, provider_revision TEXT,
  raw_sha256 TEXT NOT NULL, attributes_json TEXT NOT NULL, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS lei_current (
  namespace TEXT NOT NULL, lei TEXT NOT NULL, revision_id TEXT NOT NULL, PRIMARY KEY(namespace, lei)
);
CREATE TABLE IF NOT EXISTS lei_parent_assertions (
  assertion_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, child_lei TEXT NOT NULL, level TEXT NOT NULL,
  parent_lei TEXT, relationship_status TEXT, periods_json TEXT NOT NULL, registration_json TEXT NOT NULL,
  run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS lei_reporting (
  namespace TEXT NOT NULL, lei TEXT NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, reason TEXT,
  detail_json TEXT NOT NULL, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, lei, level, kind, run_id)
);
CREATE TABLE IF NOT EXISTS company_identity_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, lei TEXT NOT NULL, external_kind TEXT NOT NULL,
  external_id TEXT NOT NULL, state TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, reviewed_by TEXT
);
"""


class LeiError(ValueError):
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
        raise LeiError("unauthorized", f"{required} and namespace access are required")


def registry_key(value: Any) -> str:
    return re.sub(r"[\s.\-/]", "", str(value or "")).upper()


class LeiStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def observe_page(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str,
                     page_receipt: Mapping[str, Any]) -> dict[str, int]:
        observed = self.now()
        counts = {"revisions": 0, "parents": 0, "reporting": 0}
        self.conn.execute("BEGIN")
        try:
            part = page_receipt.get("part")
            if not records and page_receipt.get("outcome") == "none_reported" and part and part != "record":
                level = part.split("-")[0]
                kind = "relationship" if part.endswith("relationship") else "exception"
                self.conn.execute("INSERT OR IGNORE INTO lei_reporting VALUES (?,?,?,?,?,?,?,?)",
                                  [namespace, page_receipt["lei"], level, f"no_{kind}_reported", None, "{}", run_id,
                                   observed])
            for item in records:
                record = dict(item.get("lei_record") or {})
                lei, part, attributes = record["lei"], record["part"], dict(record["attributes"])
                if part == "record":
                    revision_id = "lei-revision:" + _digest([namespace, lei, record["raw_sha256"]])[:24]
                    inserted = self.conn.execute(
                        "INSERT OR IGNORE INTO lei_revisions VALUES (?,?,?,?,?,?,?,?) RETURNING revision_id",
                        [revision_id, namespace, lei, dict(attributes.get("registration") or {}).get("lastUpdateDate"),
                         record["raw_sha256"], _canonical(attributes), run_id, observed]).fetchall()
                    counts["revisions"] += len(inserted)
                    current = self.conn.execute(
                        "SELECT r.provider_revision FROM lei_current c JOIN lei_revisions r USING(revision_id) "
                        "WHERE c.namespace=? AND c.lei=?", [namespace, lei]).fetchone()
                    new_revision = dict(attributes.get("registration") or {}).get("lastUpdateDate") or ""
                    if current is None or new_revision >= (current[0] or ""):
                        self.conn.execute("INSERT OR REPLACE INTO lei_current VALUES (?,?,?)",
                                          [namespace, lei, revision_id])
                elif part.endswith("relationship"):
                    relationship = dict(attributes.get("relationship") or {})
                    parent = dict(relationship.get("endNode") or {}).get("id")
                    level = part.split("-")[0]
                    assertion_id = "lei-parent:" + _digest([namespace, lei, level, parent, record["raw_sha256"]])[:24]
                    inserted = self.conn.execute(
                        "INSERT OR IGNORE INTO lei_parent_assertions VALUES (?,?,?,?,?,?,?,?,?,?) RETURNING assertion_id",
                        [assertion_id, namespace, lei, level, parent, relationship.get("status"),
                         _canonical(relationship.get("periods") or []), _canonical(attributes.get("registration") or {}),
                         run_id, observed]).fetchall()
                    counts["parents"] += len(inserted)
                else:
                    level = part.split("-")[0]
                    self.conn.execute(
                        "INSERT OR IGNORE INTO lei_reporting VALUES (?,?,?,?,?,?,?,?)",
                        [namespace, lei, level, "reporting_exception", attributes.get("reason"),
                         _canonical(attributes), run_id, observed])
                    counts["reporting"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def entity(self, namespace: str, lei: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        row = self.conn.execute(
            "SELECT r.revision_id, r.attributes_json, r.provider_revision FROM lei_current c "
            "JOIN lei_revisions r USING(revision_id) WHERE c.namespace=? AND c.lei=?", [namespace, lei]).fetchone()
        if row is None:
            raise LeiError("not_found", "LEI is not visible in this namespace")
        attributes = _load(row[1], {})
        entity, registration = dict(attributes.get("entity") or {}), dict(attributes.get("registration") or {})
        revisions = self.conn.execute(
            "SELECT revision_id, provider_revision, observed_at_ms FROM lei_revisions WHERE namespace=? AND lei=? "
            "ORDER BY observed_at_ms, revision_id", [namespace, lei]).fetchall()
        status = registration.get("status")
        return {
            "contract": ENTITY_CONTRACT, "lei": lei, "revision_id": row[0], "provider_revision": row[2],
            "legal_name": entity.get("legalName"),
            "other_names": entity.get("otherNames") or [],
            "places": {"legal_address": entity.get("legalAddress"), "headquarters": entity.get("headquartersAddress"),
                       "registration_jurisdiction": entity.get("jurisdiction")},
            "official_register": {"authority_id": dict(entity.get("registeredAt") or {}).get("id"),
                                  "registered_as": entity.get("registeredAs"),
                                  "note": "registration authority pointer from the LEI record, not a register extract"},
            "legal_form": entity.get("legalForm"), "entity_status": entity.get("status"),
            "successor": entity.get("successorEntity"), "expiration": entity.get("expiration"),
            "registration": {"status": status, "last_update": registration.get("lastUpdateDate"),
                             "next_renewal": registration.get("nextRenewalDate"),
                             "stale": status in STALE_REGISTRATION},
            "parents": self.parents(namespace, lei),
            "reporting": self.reporting(namespace, lei),
            "identity_links": self.links(namespace, lei=lei),
            "revisions": [dict(zip(("revision_id", "provider_revision", "observed_at_ms"), r)) for r in revisions],
            "notice": "GLEIF reference data; relationships are as reported to GLEIF, not audited ownership.",
        }

    def parents(self, namespace: str, lei: str, *, as_of_ms: int | None = None) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT level, parent_lei, relationship_status, periods_json, run_id, observed_at_ms "
            "FROM lei_parent_assertions WHERE namespace=? AND child_lei=? AND (? IS NULL OR observed_at_ms<=?) "
            "ORDER BY level, observed_at_ms", [namespace, lei, as_of_ms, as_of_ms]).fetchall()
        result: dict[str, Any] = {}
        for level in ("direct", "ultimate"):
            assertions = [dict(zip(("parent_lei", "relationship_status", "periods", "run_id", "observed_at_ms"),
                                   (r[1], r[2], _load(r[3], []), r[4], r[5]))) for r in rows if r[0] == level]
            latest = assertions[-1] if assertions else None
            result[level] = {"latest": latest, "history": assertions,
                             "conflicting_parents": len({a["parent_lei"] for a in assertions}) > 1}
        return result

    def reporting(self, namespace: str, lei: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT level, kind, reason, run_id FROM lei_reporting WHERE namespace=? AND lei=? ORDER BY level, run_id",
            [namespace, lei]).fetchall()
        return [dict(zip(("level", "kind", "reason", "run_id"), r)) for r in rows]

    def parents_as_of(self, namespace: str, lei: str, as_of_ms: int, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        return {"lei": lei, "as_of_ms": as_of_ms, "parents": self.parents(namespace, lei, as_of_ms=as_of_ms),
                "notice": "What had been observed from GLEIF by this time; not the legal ownership on that date."}

    # ---------------------------------------------------------- identity links

    def propose_registry_links(self, namespace: str, *, scopes, principal_id: str) -> dict[str, Any]:
        """Candidates from existing OpenCorporates records on jurisdiction + normalized registry number."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        try:
            documents = self.conn.execute(
                "SELECT source_id, metadata FROM documents WHERE source_id LIKE 'opencorporates:%'").fetchall()
        except Exception:  # noqa: BLE001 - no document store yet
            documents = []
        companies = []
        for source_id, metadata in documents:
            fields = dict(_load(dict(_load(metadata, {})).get("provider_record_json"), {}).get("fields") or {})
            if fields.get("registry_number"):
                companies.append((source_id, str(fields.get("jurisdiction") or "").split("_")[0].upper(),
                                  registry_key(fields["registry_number"]), fields))
        created = []
        for (lei,) in self.conn.execute("SELECT lei FROM lei_current WHERE namespace=? ORDER BY lei",
                                        [namespace]).fetchall():
            entity = self.entity(namespace, lei, scopes={READ_SCOPE, f"namespace:{namespace}:read"})
            jurisdiction = str(entity["places"]["registration_jurisdiction"] or "").split("-")[0].upper()
            number = registry_key(entity["official_register"]["registered_as"])
            for source_id, company_jurisdiction, company_number, fields in companies:
                if number and number == company_number and jurisdiction == company_jurisdiction:
                    created.append(self._link(namespace, lei, "opencorporates", source_id, "candidate",
                                              "registry-number-match",
                                              {"jurisdiction": jurisdiction, "registered_as": entity["official_register"]
                                               ["registered_as"], "opencorporates_number": fields["registry_number"],
                                               "note": "OpenCorporates is enrichment, not the official register"},
                                              principal_id))
        return {"candidates": [link for link in self.links(namespace) if link["link_id"] in set(created)]}

    def _link(self, namespace, lei, kind, external_id, state, basis, evidence, principal_id) -> str:
        link_id = "company-link:" + _digest([namespace, lei, kind, external_id])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO company_identity_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [link_id, namespace, lei, kind, external_id, state, basis, _canonical(evidence), principal_id,
             self.now(), None])
        return link_id

    def link_identity(self, namespace: str, lei: str, external_kind: str, external_id: str, evidence: str, *,
                      scopes, principal_id: str) -> dict[str, Any]:
        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if external_kind not in {"opencorporates", "market-issuer", "registry"} or not str(evidence or "").strip():
            raise LeiError("invalid_link", "external_kind is opencorporates, market-issuer or registry, with evidence")
        link_id = self._link(namespace, lei, external_kind, external_id, "candidate", "proposed",
                             {"evidence": evidence.strip()}, principal_id)
        return next(link for link in self.links(namespace) if link["link_id"] == link_id)

    def review_link(self, namespace: str, link_id: str, decision: str, reason: str, *, scopes,
                    principal_id: str) -> dict[str, Any]:
        _authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accepted", "rejected"} or not str(reason or "").strip():
            raise LeiError("invalid_decision", "accept or reject with a reason")
        updated = self.conn.execute(
            "UPDATE company_identity_links SET state=?, reviewed_by=? WHERE namespace=? AND link_id=? RETURNING link_id",
            ["linked" if decision == "accepted" else "rejected", principal_id, namespace, link_id]).fetchall()
        if not updated:
            raise LeiError("not_found", "link is not visible in this namespace")
        return next(link for link in self.links(namespace) if link["link_id"] == link_id)

    def links(self, namespace: str, *, lei: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT link_id, lei, external_kind, external_id, state, basis, evidence_json, principal_id, reviewed_by "
            "FROM company_identity_links WHERE namespace=? AND (? IS NULL OR lei=?) ORDER BY lei, external_kind, "
            "external_id", [namespace, lei, lei]).fetchall()
        return [{"contract": LINK_CONTRACT, **dict(zip(("link_id", "lei", "external_kind", "external_id", "state",
                                                        "basis"), r[:6])),
                 "evidence": _load(r[6], {}), "principal_id": r[7], "reviewed_by": r[8]} for r in rows]


class LeiProjector:
    def __init__(self, conn: Any) -> None:
        self.store = LeiStore(conn)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        namespace = str(dict(source.get("lei") or {}).get("namespace") or DEFAULT_NAMESPACE)
        return self.store.observe_page(namespace, records, run_id=run_id, page_receipt=page_receipt)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}
