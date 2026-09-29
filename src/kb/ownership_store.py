"""Revisioned, namespace-scoped store for ``noesis-ownership-record-v1`` records.

Store rules (C01.2): one authoritative store per record type (``ownership_records``
plus immutable ``ownership_record_revisions``); a stable ``record_id`` per
namespace and source ``record_key``; a new revision only when the source-stated
content changes (replayed pages are idempotent); every revision keeps the run
and observation time that produced it, so any read can be pinned to exact
revisions or to a record time.

Entities reference the shared ``canonical_entities`` owner by an
identifier-based id (:func:`src.kb.entities.register_canonical_entity`); this
store never merges entities. Person statements whose source terms require it
are *owner-scoped*: stored per acquiring principal and redacted for everyone
else unless they hold the ownership review scope.

GLEIF Level 1/Level 2 data stays in ``src.kb.lei`` (market.lei);
:meth:`OwnershipStore.project_gleif` projects it with the LEI record as source.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.ownership_records import (
    ENTITY_KINDS,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    OwnershipRecordError,
    canonical,
    digest,
    record,
    validate_record,
)

DEFAULT_NAMESPACE = "ownership"
_DDL = """
CREATE TABLE IF NOT EXISTS ownership_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, record_key TEXT NOT NULL,
  provider TEXT NOT NULL, owner TEXT, current_revision BIGINT NOT NULL, canonical_entity_id TEXT,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS ownership_record_revisions (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision BIGINT NOT NULL, revision_id TEXT NOT NULL,
  record_hash TEXT NOT NULL, payload_json TEXT NOT NULL, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id, revision)
);
"""


class OwnershipError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise OwnershipError("unauthorized", f"{required} and namespace access are required")


def canonical_entity_id(record_key: str) -> str:
    return "ent-own-" + re.sub(r"[^a-z0-9]+", "-", record_key.lower()).strip("-")[:280]


def record_id(namespace: str, record_key: str, owner: str | None = None) -> str:
    return "own:" + digest([namespace, record_key, owner])[:24]


def redact(view: dict[str, Any]) -> dict[str, Any]:
    body = view["record"]
    kept = {k: body.get(k) for k in ("contract", "kind", "record_key", "owner_scoped", "unknowns")}
    if body["kind"] == "ownership_assertion":
        kept.update({k: body.get(k) for k in ("subject_key", "assertion_kind", "share", "validity", "statement_date")})
        kept["holder"] = {"key": None, "name": None, "kind": (body.get("holder") or {}).get("kind")}
    kept["source"] = {k: body["source"].get(k) for k in ("provider", "publisher", "license")}
    kept["source"]["provider_record_id"] = "[owner-scoped]"
    return {**view, "record": kept, "redacted": True,
            "redaction": "owner-scoped person statement; visible to the acquiring principal or an ownership reviewer"}


class OwnershipStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------- writes

    def apply(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, observed_at_ms: int,
              principal_id: str) -> dict[str, int]:
        """Validate and append records; content-identical replays are no-ops."""
        counts = {"inserted": 0, "revised": 0, "unchanged": 0}
        validated = [validate_record(dict(item)) for item in records]
        self.conn.execute("BEGIN")
        try:
            for item in validated:
                owner = principal_id if item.get("owner_scoped") else None
                rid = record_id(namespace, item["record_key"], owner)
                entity_id = None
                if item["kind"] in ENTITY_KINDS:
                    from src.kb.entities import register_canonical_entity

                    entity_id = register_canonical_entity(self.conn, canonical_entity_id(item["record_key"]),
                                                          item["name"] if not owner else "owner-scoped person",
                                                          "Person" if item["kind"] == "person" else "Organization")
                    item["canonical_entity_id"] = entity_id
                    item = validate_record(item)
                record_hash = digest(item)
                row = self.conn.execute(
                    "SELECT r.current_revision, v.record_hash FROM ownership_records r JOIN ownership_record_revisions v "
                    "ON v.namespace=r.namespace AND v.record_id=r.record_id AND v.revision=r.current_revision "
                    "WHERE r.namespace=? AND r.record_id=?", [namespace, rid]).fetchone()
                if row and row[1] == record_hash:
                    counts["unchanged"] += 1
                    continue
                revision = 1 if row is None else int(row[0]) + 1
                revision_id = "own-rev:" + digest([rid, revision, record_hash])[:24]
                self.conn.execute("INSERT INTO ownership_record_revisions VALUES (?,?,?,?,?,?,?,?)",
                                  [namespace, rid, revision, revision_id, record_hash, canonical(item), run_id,
                                   int(observed_at_ms)])
                if row is None:
                    self.conn.execute("INSERT INTO ownership_records VALUES (?,?,?,?,?,?,?,?,?)",
                                      [namespace, rid, item["kind"], item["record_key"], item["source"]["provider"],
                                       owner, revision, entity_id, int(observed_at_ms)])
                    counts["inserted"] += 1
                else:
                    self.conn.execute("UPDATE ownership_records SET current_revision=? WHERE namespace=? AND record_id=?",
                                      [revision, namespace, rid])
                    counts["revised"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def put(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, principal_id: str,
            scopes: Iterable[str]) -> dict[str, int]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        return self.apply(namespace, records, run_id=run_id, observed_at_ms=self.now(), principal_id=principal_id)

    # -------------------------------------------------------------- reads

    def _rows(self, namespace: str, *, kinds: Sequence[str] | None = None, known_at_ms: int | None = None,
              pins: Mapping[str, int] | None = None) -> list[tuple]:
        """(record_id, owner, revision, revision_id, record_hash, payload, run_id, observed_at_ms) per record.

        ``pins`` selects exact revisions (replay); ``known_at_ms`` the latest
        revision observed by that record time; otherwise the current one.
        """
        if not self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='ownership_records'").fetchone():
            return []
        rows = self.conn.execute(
            "SELECT r.record_id, r.owner, v.revision, v.revision_id, v.record_hash, v.payload_json, v.run_id, "
            "v.observed_at_ms, r.kind FROM ownership_records r JOIN ownership_record_revisions v "
            "ON v.namespace=r.namespace AND v.record_id=r.record_id WHERE r.namespace=? ORDER BY r.record_id, v.revision",
            [namespace]).fetchall()
        chosen: dict[str, tuple] = {}
        for row in rows:
            rid, revision, observed, kind = row[0], int(row[2]), int(row[7]), row[8]
            if kinds and kind not in kinds:
                continue
            if pins is not None:
                if pins.get(rid) == revision:
                    chosen[rid] = row
            elif known_at_ms is None or observed <= known_at_ms:
                chosen[rid] = row
        return [chosen[k] for k in sorted(chosen)]

    @staticmethod
    def _view(row: tuple, principal_id: str | None, scopes: set[str]) -> dict[str, Any]:
        view = {"record_id": row[0], "revision": int(row[2]), "revision_id": row[3], "record_hash": row[4],
                "run_id": row[6], "observed_at_ms": int(row[7]), "record": json.loads(row[5])}
        if row[1] is not None and row[1] != principal_id and REVIEW_SCOPE not in scopes and "operator" not in scopes:
            return redact(view)
        return view

    def records(self, namespace: str, *, principal_id: str | None, scopes: Iterable[str],
                kinds: Sequence[str] | None = None, known_at_ms: int | None = None,
                pins: Mapping[str, int] | None = None) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        # Other principals' owner-scoped records appear redacted, so an analyst
        # sees that a person-level statement exists without seeing who it names.
        return [self._view(row, principal_id, scopes)
                for row in self._rows(namespace, kinds=kinds, known_at_ms=known_at_ms, pins=pins)]

    def get(self, namespace: str, rid: str, *, principal_id: str | None, scopes: Iterable[str],
            revision: int | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT r.record_id, r.owner, v.revision, v.revision_id, v.record_hash, v.payload_json, v.run_id, "
            "v.observed_at_ms FROM ownership_records r JOIN ownership_record_revisions v ON v.namespace=r.namespace "
            "AND v.record_id=r.record_id WHERE r.namespace=? AND r.record_id=? AND v.revision=COALESCE(?, r.current_revision)",
            [namespace, rid, revision]).fetchall()
        if not rows:
            raise OwnershipError("not_found", "ownership record is not visible in this namespace")
        return self._view(rows[0], principal_id, scopes)

    def history(self, namespace: str, rid: str, *, principal_id: str | None, scopes: Iterable[str]) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT r.record_id, r.owner, v.revision, v.revision_id, v.record_hash, v.payload_json, v.run_id, "
            "v.observed_at_ms FROM ownership_records r JOIN ownership_record_revisions v ON v.namespace=r.namespace "
            "AND v.record_id=r.record_id WHERE r.namespace=? AND r.record_id=? ORDER BY v.revision", [namespace, rid]).fetchall()
        return [self._view(row, principal_id, scopes) for row in rows]

    def by_key(self, namespace: str, record_key: str, *, principal_id: str | None, scopes: Iterable[str]) -> dict[str, Any] | None:
        for owner in (None, principal_id):
            row = self.conn.execute("SELECT record_id FROM ownership_records WHERE namespace=? AND record_id=?",
                                    [namespace, record_id(namespace, record_key, owner)]).fetchone()
            if row:
                return self.get(namespace, row[0], principal_id=principal_id, scopes=scopes)
        return None

    def lookup(self, namespace: str, scheme: str, value: str, *, principal_id: str | None,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Entities carrying an identifier, or name + jurisdiction candidates (never a pick)."""
        scheme = str(scheme or "").lower()
        wanted = re.sub(r"[\s.\-/]", "", str(value or "")).upper()
        entities = self.records(namespace, principal_id=principal_id, scopes=scopes, kinds=ENTITY_KINDS)
        if scheme == "name":
            name, _, jurisdiction = str(value).partition("|")
            from src.kb.entities import normalize_surface

            target = normalize_surface(name)
            matches = [e for e in entities if not e.get("redacted")
                       and normalize_surface(e["record"]["name"]) == target
                       and (not jurisdiction or str(e["record"].get("jurisdiction") or "").upper().startswith(jurisdiction.upper()))]
            return {"query": {"scheme": scheme, "value": value}, "status": "candidates" if matches else "not_found",
                    "matches": matches, "note": "name + jurisdiction lookups return candidates for review; they never "
                                                "select an entity"}
        aliases = {"cik": "sec-cik", "company_number": "gb-coh", "register": "gb-coh"}
        scheme = aliases.get(scheme, scheme)
        matches = []
        for entity in entities:
            if entity.get("redacted"):
                continue
            for identifier in entity["record"].get("identifiers") or []:
                value_key = re.sub(r"[\s.\-/]", "", str(identifier.get("value"))).upper()
                if scheme == "sec-cik":
                    value_key, wanted_key = value_key.lstrip("0"), wanted.lstrip("0")
                else:
                    wanted_key = wanted
                if identifier.get("scheme") == scheme and value_key == wanted_key:
                    matches.append(entity)
                    break
        return {"query": {"scheme": scheme, "value": value}, "status": "found" if matches else "not_found",
                "matches": matches}

    # --------------------------------------------------- source projections

    def project_gleif(self, namespace: str, leis: Sequence[str], *, lei_namespace: str, run_id: str,
                      principal_id: str) -> dict[str, Any]:
        """Project stored LEI records' Level 2 data (O03); Level 1 records are reused, not duplicated."""
        from src.ingestion.ownership_providers import parse_gleif_level2
        from src.kb.lei import LeiStore

        lei_store = LeiStore(self.conn)
        records, missing = [], []
        for lei in leis:
            level2 = lei_store.level2(lei_namespace, lei)
            if level2["record"] is None:
                missing.append(lei)
                continue
            records.extend(parse_gleif_level2(level2, lei_namespace=lei_namespace))
        counts = self.apply(namespace, records, run_id=run_id, observed_at_ms=self.now(), principal_id=principal_id)
        return {"projected": len(records), "counts": counts, "not_acquired": missing}

    def record_register_document(self, namespace: str, *, provider: str, register: str, number: str,
                                 jurisdiction: str, document: Mapping[str, Any], name: str | None = None,
                                 status: str | None = None, registered_on: str | None = None,
                                 principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Record a registration from an official register document the user obtained (O07).

        Handelsregister, Unternehmensregister and BRIS have no supported
        machine access, so nothing is fetched: the user supplies the official
        document reference (and its digest), which is retained on the record.
        """
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if provider not in {"handelsregister", "unternehmensregister", "bris"}:
            raise OwnershipError("invalid_request", "register documents are recorded for handelsregister, "
                                                    "unternehmensregister or bris")
        if not isinstance(document, Mapping) or not document.get("reference") or not re.fullmatch(
                r"[0-9a-f]{64}", str(document.get("sha256") or "")):
            raise OwnershipError("invalid_request", "an official document reference and its SHA-256 are required")
        entity = f"{provider}:register:{re.sub(r'[^A-Za-z0-9]+', '-', register).strip('-')}:{re.sub(r'[^A-Za-z0-9]+', '', number)}"
        source = {"provider": provider, "provider_record_id": f"{register}:{number}", "source_type": "user-supplied official document",
                  "note": "recorded from an official register document supplied by the user; not acquired by Noesis"}
        records = [record("registration", f"{entity}:registration", {**source, "locator": {"field": "document_reference"}},
                          entity_key=entity, register=register, number=number, jurisdiction=jurisdiction,
                          status=status, registered_on=registered_on,
                          document_reference={**{k: document.get(k) for k in ("kind", "reference", "url", "sha256", "retrieved_on")
                                                 if document.get(k) is not None}, "official": True})]
        if name:
            records.append(record("legal_entity", entity, source, name=name, jurisdiction=jurisdiction,
                                  identifiers=[{"scheme": "register", "value": number, "authority": register}],
                                  status=status))
        counts = self.apply(namespace, records, run_id=f"register-document:{principal_id}:{self.now()}",
                            observed_at_ms=self.now(), principal_id=principal_id)
        return {"counts": counts, "entity_key": entity,
                "records": [self.by_key(namespace, r["record_key"], principal_id=principal_id, scopes=scopes) for r in records]}


class OwnershipProjector:
    """Source-pack runtime projector for ``noesis-ownership-part-v1`` pages."""

    def __init__(self, conn: Any) -> None:
        self.store = OwnershipStore(conn)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt
        namespace = str(dict(source.get("ownership") or {}).get("namespace") or DEFAULT_NAMESPACE)
        projected = []
        for item in records:
            part = dict(item.get("ownership_part") or {})
            projected.extend(part.get("records") or [])
        # Record time is the runtime's observation time of this page, so
        # record-time reads line up with the run receipt and its documents.
        observed = max((int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None),
                       default=self.store.now())
        try:
            return self.store.apply(namespace, projected, run_id=run_id, observed_at_ms=observed,
                                    principal_id=principal_id)
        except OwnershipRecordError as exc:
            from src.ingestion.source_packs import SourcePackError

            raise SourcePackError("mapping_failed", str(exc)) from exc

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}
