"""Standards catalogue editions and certification records for the Technical capability.

* **Standards** come from the ISO Open Data catalogue as immutable revisions
  per deliverable, and are mirrored into the existing Technical model as
  ``standard`` objects (``standard:iso:<id>``) with ``supersedes`` (replaced
  editions) and ``amends`` (amendments/corrigenda) relations. Withdrawn stays
  withdrawn; the protected text is never stored, only the catalogue link.
* **Certificates** enter through an explicit import path until a certification
  registry is validated. Certificates, conformity declarations and standards
  are distinct record types; a certificate's validity window and status are
  its own sourced facts, and two sources disagreeing on status are both kept.
  Certificates link to standards by reference and to Products models only by
  explicit identifier (reviewable), never inferred from a specification.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

READ_SCOPE = "knowledge:standards:read"
WRITE_SCOPE = "knowledge:standards:write"
REVIEW_SCOPE = "knowledge:standards:review"
STANDARD_CONTRACT = "noesis-standard-edition-v1"
CERTIFICATE_CONTRACT = "noesis-certificate-record-v1"
DEFAULT_NAMESPACE = "global"
CERTIFICATE_KINDS = frozenset({"certificate", "conformity-declaration"})
CERTIFICATE_STATUSES = frozenset({"valid", "suspended", "withdrawn", "expired", "unknown"})

_DDL = """
CREATE TABLE IF NOT EXISTS standard_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, native_id TEXT NOT NULL,
  reference TEXT NOT NULL, record_json TEXT NOT NULL, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS standard_current (
  namespace TEXT NOT NULL, provider TEXT NOT NULL, native_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  PRIMARY KEY(namespace, provider, native_id)
);
CREATE TABLE IF NOT EXISTS certificate_records (
  record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL,
  issuer TEXT NOT NULL, scheme TEXT, certificate_number TEXT NOT NULL, status TEXT NOT NULL,
  valid_from TEXT, valid_until TEXT, standards_json TEXT NOT NULL, products_json TEXT NOT NULL,
  locator TEXT NOT NULL, observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS certificate_product_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, certificate_number TEXT NOT NULL, issuer TEXT NOT NULL,
  model_id TEXT NOT NULL, state TEXT NOT NULL, basis TEXT NOT NULL, principal_id TEXT NOT NULL
);
"""


class StandardsError(ValueError):
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
        raise StandardsError("unauthorized", f"{required} and namespace access are required")


class StandardsStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def observe_page(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str) -> dict[str, int]:
        from src.domains.technical.model import record_object, record_relation

        observed = self.now()
        counts = {"revisions": 0}
        self.conn.execute("BEGIN")
        try:
            for item in records:
                record = dict(item.get("standard_record") or {})
                revision_id = "standard-revision:" + _digest([namespace, record["provider"], record["native_id"],
                                                             record["native_sha256"]])[:24]
                inserted = self.conn.execute(
                    "INSERT OR IGNORE INTO standard_revisions VALUES (?,?,?,?,?,?,?,?) RETURNING revision_id",
                    [revision_id, namespace, record["provider"], record["native_id"], record["reference"],
                     _canonical(record), run_id, observed]).fetchall()
                self.conn.execute("INSERT OR REPLACE INTO standard_current VALUES (?,?,?,?)",
                                  [namespace, record["provider"], record["native_id"], revision_id])
                counts["revisions"] += len(inserted)
                object_id = f"standard:iso:{record['native_id']}"
                record_object(
                    self.conn, object_type="standard", object_id=object_id, canonical_name=record["reference"],
                    version=str(record.get("edition") or "unknown"), immutable_id=record["reference"],
                    status=record["status"], published_at=record.get("publication_date"), observed_at=observed,
                    source_url=record["catalogue_url"],
                    metadata={"stage": record.get("stage"), "ics_codes": record.get("ics_codes"),
                              "deliverable_type": record.get("deliverable_type"),
                              "supplement_type": record.get("supplement_type"),
                              "content_access": record["content_access"]},
                    provenance={"standard_revision_id": revision_id, "provider": record["provider"]})
                for older in record.get("replaces") or []:
                    record_relation(self.conn, object_id, "supersedes", f"standard:iso:{older}", observed_at=observed,
                                    source_url=record["catalogue_url"])
                if record.get("supplement_type"):
                    base = record["reference"].split("/", 1)[0]
                    base_row = self.conn.execute(
                        "SELECT native_id FROM standard_revisions WHERE namespace=? AND reference=? LIMIT 1",
                        [namespace, base]).fetchone()
                    if base_row:
                        record_relation(self.conn, object_id, "amends", f"standard:iso:{base_row[0]}",
                                        observed_at=observed, source_url=record["catalogue_url"])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def standard(self, namespace: str, reference: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        rows = self.conn.execute(
            "SELECT r.native_id, r.record_json FROM standard_current c JOIN standard_revisions r USING(revision_id) "
            "WHERE c.namespace=? AND r.reference=?", [namespace, reference]).fetchall()
        if not rows:
            raise StandardsError("not_found", "standard edition is not in the acquired catalogue")
        native_id, record = rows[0][0], _load(rows[0][1], {})
        object_id = f"standard:iso:{native_id}"
        relations = self.conn.execute(
            "SELECT relation, object_id FROM technical_relations WHERE subject_id=? ORDER BY relation, object_id",
            [object_id]).fetchall() if self._has("technical_relations") else []
        inbound = self.conn.execute(
            "SELECT relation, subject_id FROM technical_relations WHERE object_id=? ORDER BY relation, subject_id",
            [object_id]).fetchall() if self._has("technical_relations") else []
        return {**record, "contract": STANDARD_CONTRACT, "technical_object_id": object_id,
                "relations": [{"relation": r[0], "object_id": r[1]} for r in relations],
                "inbound_relations": [{"relation": r[0], "subject_id": r[1]} for r in inbound],
                "certificates": self.certificates(namespace, standard=reference),
                "notice": "Catalogue metadata; the standard's text is protected and only linked."}

    def _has(self, table: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())

    # ----------------------------------------------------------- certificates

    def import_certificates(self, namespace: str, source: str, records: Sequence[Mapping[str, Any]], *, scopes,
                            principal_id: str) -> dict[str, Any]:
        """Explicit import of certificate/conformity records from a named source with locators."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not str(source or "").strip() or not 1 <= len(records) <= 1000:
            raise StandardsError("invalid_import", "name the source and import 1-1000 records")
        imported = 0
        for record in records:
            kind = record.get("kind")
            status = str(record.get("status") or "unknown")
            if kind not in CERTIFICATE_KINDS or status not in CERTIFICATE_STATUSES:
                raise StandardsError("invalid_record", "kind is certificate|conformity-declaration with a known status")
            if not record.get("issuer") or not record.get("certificate_number") or not record.get("locator"):
                raise StandardsError("invalid_record", "issuer, certificate_number and locator are required")
            record_id = "certificate:" + _digest([namespace, source, record["issuer"], record["certificate_number"],
                                                  status, record.get("valid_until")])[:24]
            imported += len(self.conn.execute(
                "INSERT OR IGNORE INTO certificate_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING record_id",
                [record_id, namespace, source, kind, record["issuer"], record.get("scheme"),
                 record["certificate_number"], status, record.get("valid_from"), record.get("valid_until"),
                 _canonical(list(record.get("standards") or [])), _canonical(list(record.get("products") or [])),
                 record["locator"], self.now()]).fetchall())
        del principal_id
        return {"imported": imported}

    def certificates(self, namespace: str, *, standard: str | None = None, number: str | None = None,
                     as_of: str | None = None) -> list[dict[str, Any]]:
        day = as_of or datetime.now(UTC).date().isoformat()
        rows = self.conn.execute(
            "SELECT record_id, source, kind, issuer, scheme, certificate_number, status, valid_from, valid_until, "
            "standards_json, products_json, locator FROM certificate_records WHERE namespace=? "
            "AND (? IS NULL OR certificate_number=?) ORDER BY certificate_number, source",
            [namespace, number, number]).fetchall()
        result = []
        for r in rows:
            standards = _load(r[9], [])
            if standard and standard not in standards:
                continue
            expired = bool(r[8]) and r[8] < day
            result.append({"contract": CERTIFICATE_CONTRACT, "record_id": r[0], "source": r[1], "kind": r[2],
                           "issuer": r[3], "scheme": r[4], "certificate_number": r[5], "declared_status": r[6],
                           "valid_from": r[7], "valid_until": r[8], "standards": standards, "products": _load(r[10], []),
                           "locator": r[11], "expired_on_date": expired,
                           "effective_status": "expired" if expired and r[6] == "valid" else r[6]})
        return result

    def certificate(self, namespace: str, number: str, *, scopes, as_of: str | None = None) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        records = self.certificates(namespace, number=number, as_of=as_of)
        if not records:
            raise StandardsError("not_found", "certificate is not in the imported records")
        statuses = {r["effective_status"] for r in records}
        links = self.conn.execute(
            "SELECT link_id, model_id, state, basis FROM certificate_product_links WHERE namespace=? "
            "AND certificate_number=?", [namespace, number]).fetchall()
        return {"certificate_number": number, "records": records, "status_conflict": len(statuses) > 1,
                "product_links": [dict(zip(("link_id", "model_id", "state", "basis"), link)) for link in links],
                "notice": "Certificates are distinct from the standards they reference and from product specifications."}

    def propose_product_links(self, namespace: str, *, scopes, principal_id: str) -> dict[str, Any]:
        """Candidates only where a certificate names a product identifier held by a Products model."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not self._has("product_identities"):
            return {"candidates": []}
        created = []
        for record in self.certificates(namespace):
            for product in record["products"]:
                rows = self.conn.execute(
                    "SELECT identity_id FROM product_identities WHERE namespace=? AND level='model' "
                    "AND lower(brand)=lower(?) AND designation=?",
                    [namespace, product.get("brand"), product.get("designation")]).fetchall()
                for (model_id,) in rows:
                    link_id = "certificate-link:" + _digest([namespace, record["certificate_number"], model_id])[:24]
                    self.conn.execute(
                        "INSERT OR IGNORE INTO certificate_product_links VALUES (?,?,?,?,?,?,?,?)",
                        [link_id, namespace, record["certificate_number"], record["issuer"], model_id, "candidate",
                         "certificate-names-model", principal_id])
                    created.append(link_id)
        return {"candidates": sorted(set(created))}

    def review_product_link(self, namespace: str, link_id: str, decision: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accepted", "rejected"}:
            raise StandardsError("invalid_decision", "accept or reject")
        updated = self.conn.execute(
            "UPDATE certificate_product_links SET state=? WHERE namespace=? AND link_id=? RETURNING certificate_number",
            ["linked" if decision == "accepted" else "rejected", namespace, link_id]).fetchall()
        if not updated:
            raise StandardsError("not_found", "link is not visible in this namespace")
        return {"link_id": link_id, "state": "linked" if decision == "accepted" else "rejected"}


class StandardsProjector:
    def __init__(self, conn: Any) -> None:
        self.store = StandardsStore(conn)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        namespace = str(dict(source.get("standards") or {}).get("namespace") or DEFAULT_NAMESPACE)
        return self.store.observe_page(namespace, records, run_id=run_id)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}
