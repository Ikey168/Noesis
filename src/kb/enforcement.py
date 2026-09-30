"""Regulatory enforcement in the Legal pack: revisioned store, projector, receipts and readiness (#2651, EN02).

``noesis-enforcement-record-v1`` records (:mod:`src.kb.enforcement_records`)
arrive through the ``legal-research`` source-pack runtime (connector
``enforcement``) and are persisted here, following the
:mod:`src.kb.entity_history` / ownership-store revision pattern:

* one stable ``record_id`` per namespace and ``record_key``;
* an **immutable revision** only when the published content changes (a replay
  of an unchanged page is a no-op), each with the run and observation time
  that produced it;
* corrections and removals by the source arrive as new revisions
  (``source_status``), never as deletions - nothing in this module deletes;
* any read can be pinned to a record time (``known_at_ms``: the revision the
  store held then) or to exact revisions.

Acquisition receipts are kept per run and unit. Coverage is four optional Legal
features (``enforcement-sec``, ``enforcement-fca``, ``enforcement-epa``,
``enforcement-edpb``; default off), selected through the active composition
plan. It records what regulators published and never
scores risk or compliance, infers wrongdoing from an initiated action, merges
a settled "neither admit nor deny" outcome into a finding, profiles named
individuals or gives legal advice.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.enforcement_records import (
    CONTRACT,
    PERSONAL_FIELDS,
    EnforcementRecordError,
    canonical,
    digest,
    validate_record,
)

READ_SCOPE = "knowledge:legal:read"
WRITE_SCOPE = "knowledge:legal:write"
REVIEW_SCOPE = "knowledge:legal:review"
DEFAULT_NAMESPACE = "global"
# SEC, FCA, EPA and EDPB coverage are separate optional Legal features (EN12), each default off.
FEATURES = ("enforcement-sec", "enforcement-fca", "enforcement-epa", "enforcement-edpb")
BUNDLE = "legal"
PACK_ID = "legal-research"
RECORD_CONTRACT = CONTRACT
# Keys no answer may carry (#2651 exclusions); the EN01 minimisation decision adds the personal fields.
FORBIDDEN_KEYS = frozenset({"risk_score", "compliance_score", "risk_rating", "compliance_rating", "wrongdoing",
                            "finding_of_wrongdoing", "violation_found", "guilty", "culpability", "prediction",
                            "predicted_outcome", "legal_advice", "person_profile"}) | frozenset(PERSONAL_FIELDS)
EXCLUSIONS = ("no risk or compliance scoring, no inference of wrongdoing from an initiated action, no merging of "
              "settled 'neither admit nor deny' outcomes into findings, no profiling of named individuals, no legal "
              "advice")
_DDL = """
CREATE TABLE IF NOT EXISTS enforcement_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, record_key TEXT NOT NULL,
  provider TEXT NOT NULL, authority TEXT NOT NULL, action_key TEXT, current_revision BIGINT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS enforcement_record_revisions (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision BIGINT NOT NULL, revision_id TEXT NOT NULL,
  record_hash TEXT NOT NULL, payload_json TEXT NOT NULL, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id, revision)
);
CREATE TABLE IF NOT EXISTS enforcement_receipts (
  receipt_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  provider TEXT NOT NULL, unit_json TEXT NOT NULL, requests_json TEXT NOT NULL, counts_json TEXT NOT NULL,
  evidence_origin TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL
);
"""


class EnforcementError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise EnforcementError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def forbidden_keys(value: Any, path: str = "") -> list[str]:
    """Paths of any forbidden (score, inferred finding, advice, personal attribute) key in an answer."""
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                found.append(f"{path}/{key}")
            found += forbidden_keys(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}/{index}")
    return found


def feature_enabled(conn: Any, feature: str | None = None) -> bool:
    """Whether an optional Legal enforcement feature (any of them when ``feature`` is None) is selected.

    Features default to off and are selected through the active composition plan; reads only.
    """
    from src.kb.legal import legal_feature_enabled

    if feature is not None and feature not in FEATURES:
        raise EnforcementError("invalid_feature", f"feature is one of {FEATURES}")
    return any(legal_feature_enabled(conn, f) for f in ([feature] if feature else FEATURES))


def record_id(namespace: str, record_key: str) -> str:
    return "enf:" + digest([namespace, record_key])[:24]


class EnforcementStore:
    """Enforcement records as immutable revisions, acquisition receipts and typed reads."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------- writes

    def apply(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str,
              observed_at_ms: int | None = None) -> dict[str, int]:
        """Validate (minimisation enforced) and append revisions; content-identical replays are no-ops.

        A ``removal`` marker (a declared unit the publisher answers with 404 or
        410) becomes a ``removed_by_source`` revision of the stored action,
        carrying the previous content forward; nothing is deleted.
        """
        counts = {"inserted": 0, "revised": 0, "unchanged": 0}
        items = [dict(item) for item in records]
        removals = [item for item in items if item.get("kind") == "removal"]
        items = [item for item in items if item.get("kind") != "removal"]
        for marker in removals:
            removed = self._removed(namespace, marker)
            if removed is None:
                counts["removal_unmatched"] = counts.get("removal_unmatched", 0) + 1
            else:
                items.append(removed)
        try:
            validated = [validate_record(item) for item in items]
        except EnforcementRecordError as exc:
            raise EnforcementError(exc.code, str(exc)) from exc
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        self.conn.execute("BEGIN")
        try:
            for item in validated:
                rid = record_id(namespace, item["record_key"])
                # The evidence origin describes the acquisition, not the published content.
                record_hash = digest({**item, "source": {k: v for k, v in item["source"].items()
                                                         if k != "evidence_origin"}})
                row = self.conn.execute(
                    "SELECT r.current_revision, v.record_hash FROM enforcement_records r JOIN "
                    "enforcement_record_revisions v ON v.namespace=r.namespace AND v.record_id=r.record_id AND "
                    "v.revision=r.current_revision WHERE r.namespace=? AND r.record_id=?", [namespace, rid]).fetchone()
                if row and row[1] == record_hash:
                    counts["unchanged"] += 1
                    continue
                revision = 1 if row is None else int(row[0]) + 1
                revision_id = "enf-rev:" + digest([rid, revision, record_hash])[:24]
                self.conn.execute("INSERT INTO enforcement_record_revisions VALUES (?,?,?,?,?,?,?,?)",
                                  [namespace, rid, revision, revision_id, record_hash, canonical(item), run_id,
                                   observed])
                if row is None:
                    self.conn.execute("INSERT INTO enforcement_records VALUES (?,?,?,?,?,?,?,?,?)",
                                      [namespace, rid, item["kind"], item["record_key"], item["source"]["provider"],
                                       item["authority"], item.get("action_key"), revision, observed])
                    counts["inserted"] += 1
                else:
                    self.conn.execute("UPDATE enforcement_records SET current_revision=? WHERE namespace=? AND "
                                      "record_id=?", [revision, namespace, rid])
                    counts["revised"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def _removed(self, namespace: str, marker: Mapping[str, Any]) -> dict[str, Any] | None:
        current = self.by_key(namespace, marker["record_key"]) if marker.get("record_key") else None
        if current is None and marker.get("action_number") and table_exists(self.conn, "enforcement_records"):
            row = self.conn.execute(
                "SELECT record_key FROM enforcement_records WHERE namespace=? AND kind='enforcement_action' AND "
                "provider=? AND record_key LIKE ?", [namespace, marker.get("provider"),
                                                     f"%:{marker['action_number']}"]).fetchone()
            current = self.by_key(namespace, row[0]) if row else None
        if current is None:
            return None
        body = dict(current["record"])
        body["source_status"] = "removed_by_source"
        body["source"] = {**body["source"], "revision": f"removed by source (HTTP {marker.get('http_status')})",
                          "evidence_origin": marker.get("evidence_origin") or body["source"].get("evidence_origin")}
        body.pop("unknowns", None)
        return body

    def put(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str,
            scopes: Iterable[str]) -> dict[str, int]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        return self.apply(namespace, records, run_id=run_id)

    def record_receipt(self, namespace: str, run_id: str, receipt: Mapping[str, Any], counts: Mapping[str, int]
                       ) -> None:
        if not receipt:
            return
        receipt_id = "enforcement-receipt:" + digest([namespace, run_id, receipt.get("source_id"),
                                                      receipt.get("unit_index"), receipt.get("requests")])[:24]
        self.conn.execute("INSERT OR IGNORE INTO enforcement_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [receipt_id, namespace, run_id, str(receipt.get("source_id")), str(receipt.get("provider")),
                           canonical(receipt.get("unit") or {}), canonical(receipt.get("requests") or []),
                           canonical(dict(counts)), str(receipt.get("evidence_origin") or "live"), self.now()])

    # -------------------------------------------------------------- reads

    @staticmethod
    def _view(row: Sequence[Any]) -> dict[str, Any]:
        return {"record_id": row[0], "revision": int(row[1]), "revision_id": row[2], "record_hash": row[3],
                "record": json.loads(row[4]), "run_id": row[5], "observed_at_ms": int(row[6])}

    def views(self, namespace: str, kinds: Sequence[str] | None = None, *, known_at_ms: int | None = None,
              action_key: str | None = None) -> list[dict[str, Any]]:
        """Current revisions, or the revision each record had at record time ``known_at_ms``; no scope check."""
        if not table_exists(self.conn, "enforcement_records"):
            return []
        rows = self.conn.execute(
            "SELECT r.record_id, v.revision, v.revision_id, v.record_hash, v.payload_json, v.run_id, v.observed_at_ms, "
            "r.kind, r.action_key FROM enforcement_records r JOIN enforcement_record_revisions v ON "
            "v.namespace=r.namespace AND v.record_id=r.record_id WHERE r.namespace=? ORDER BY r.record_id, v.revision",
            [namespace]).fetchall()
        chosen: dict[str, tuple] = {}
        for row in rows:
            if kinds and row[7] not in kinds:
                continue
            if action_key is not None and row[8] != action_key:
                continue
            if known_at_ms is None or int(row[6]) <= known_at_ms:
                chosen[row[0]] = row
        return sorted((self._view(r) for r in chosen.values()), key=lambda v: v["record"]["record_key"])

    def history(self, namespace: str, record_key: str) -> list[dict[str, Any]]:
        """Every revision of one record in order (the revision chain)."""
        if not table_exists(self.conn, "enforcement_records"):
            return []
        rows = self.conn.execute(
            "SELECT record_id, revision, revision_id, record_hash, payload_json, run_id, observed_at_ms FROM "
            "enforcement_record_revisions WHERE namespace=? AND record_id=? ORDER BY revision",
            [namespace, record_id(namespace, record_key)]).fetchall()
        return [self._view(r) for r in rows]

    def by_key(self, namespace: str, record_key: str, *, known_at_ms: int | None = None) -> dict[str, Any] | None:
        """The current revision, or the revision held at record time ``known_at_ms`` (as-of lookup)."""
        chain = [v for v in self.history(namespace, record_key)
                 if known_at_ms is None or v["observed_at_ms"] <= known_at_ms]
        return chain[-1] if chain else None

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT record_id, revision, revision_id, record_hash, payload_json, run_id, observed_at_ms FROM "
            "enforcement_record_revisions WHERE namespace=? AND revision_id=?", [namespace, revision_id]).fetchone() \
            if table_exists(self.conn, "enforcement_record_revisions") else None
        return self._view(row) if row else None

    def action_children(self, namespace: str, action_key: str, *, known_at_ms: int | None = None
                        ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {"respondent": [], "enforcement_decision": [], "penalty": [],
                                                "appeal": []}
        for view in self.views(namespace, tuple(out), known_at_ms=known_at_ms, action_key=action_key):
            out[view["record"]["kind"]].append(view)
        return out

    def receipts(self, namespace: str, run_id: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "enforcement_receipts"):
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, source_id, provider, unit_json, requests_json, counts_json, evidence_origin FROM "
            "enforcement_receipts WHERE namespace=? AND run_id=? ORDER BY source_id, receipt_id",
            [namespace, run_id]).fetchall()
        return [{"receipt_id": r[0], "source_id": r[1], "provider": r[2], "unit": json.loads(r[3]),
                 "requests": json.loads(r[4]), "counts": json.loads(r[5]), "evidence_origin": r[6]} for r in rows]

    def record_history(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """The revision chain of one record, with each revision's source, run and observation time."""
        authorize(namespace, scopes, READ_SCOPE)
        chain = self.history(namespace, record_key)
        return {"record_key": record_key, "status": "answered" if chain else "not_found",
                "revisions": [{"revision": v["revision"], "revision_id": v["revision_id"], "run_id": v["run_id"],
                               "observed_at_ms": v["observed_at_ms"], "record": v["record"]} for v in chain],
                "notice": "revisions are immutable; corrections and removals by the source are later revisions"}


class EnforcementProjector:
    """Source-pack runtime projector for ``noesis-enforcement-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = EnforcementStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("enforcement") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project(self, namespace: str, records: Iterable[Mapping[str, Any]], *, run_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        records = [dict(r) for r in records]
        if any(r.get("contract") != RECORD_CONTRACT for r in records):
            raise EnforcementError("invalid_record", "page record is not an enforcement record")
        counts = self.store.apply(namespace, records, run_id=run_id, observed_at_ms=observed_at_ms)
        self.store.record_receipt(namespace, run_id, dict(receipt or {}), counts)
        return {"counts": counts}

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        from src.ingestion.source_packs import SourcePackError

        observed = max((int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None),
                       default=self.store.now())
        try:
            return [self.project(self._namespace(source), [r["enforcement_record"] for r in records], run_id=run_id,
                                 receipt=dict(page_receipt or {}), observed_at_ms=observed)]
        except EnforcementError as exc:
            raise SourcePackError("mapping_failed", str(exc)) from exc

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}


def readiness(conn: Any, namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    """Whether the feature is selected and what each provider has acquired; unverified live access is stated."""
    from src.ingestion.enforcement_sources import FORMATS, LIVE_VERIFICATION

    counts: dict[str, int] = {}
    if table_exists(conn, "enforcement_records"):
        for provider, count in conn.execute("SELECT provider, count(*) FROM enforcement_records WHERE namespace=? "
                                            "GROUP BY 1", [namespace]).fetchall():
            counts[provider] = int(count)
    providers = {}
    for fmt, spec in FORMATS.items():
        providers[spec["provider"]] = {"feature_coverage": spec["coverage"], "format": fmt,
                                       "records": counts.get(spec["provider"], 0),
                                       "live": LIVE_VERIFICATION[spec["provider"]]}
    return {"pack_id": PACK_ID, "bundle": BUNDLE, "features": {f: feature_enabled(conn, f) for f in FEATURES},
            "namespace": namespace, "providers": providers, "exclusions": EXCLUSIONS,
            "notice": "unverified-live providers have fixture evidence only; a dated live run is outstanding (#2720)"}
