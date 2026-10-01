"""Regulatory enforcement in the Legal pack: revisioned store, projector, features and readiness (#2651, EN02).

``noesis-enforcement-record-v1`` records (:mod:`src.kb.enforcement_records`)
arrive through the ``legal-research`` source-pack runtime (connector
``enforcement``) and are persisted here, following the
:mod:`src.kb.entity_history` and ownership-store pattern: one stable record per
namespace and ``record_key`` (``enforcement_records``), immutable revisions
(``enforcement_record_revisions``) with the run and observation time that
produced them, and a new revision only when the published content changes, so
replayed pages are no-ops. Any read can be pinned to a record time
(``known_at_ms``) or to exact revisions.

Removals and corrections by a source are revisions, never deletions: when a
declared notice is withdrawn (the publisher answers 404 or 410) the adapter
emits a removal marker and the store appends a revision of the action whose
``publication_status`` is ``removed_by_source``; every earlier revision stays
readable. Acquisition receipts are kept per run and unit
(``enforcement_receipts``).

Coverage is four optional Legal features, default off and independent:
``enforcement-sec``, ``enforcement-fca``, ``enforcement-epa`` and
``enforcement-edpb``. Nothing here scores risk or compliance, infers wrongdoing
from an initiated action, merges a settled "neither admit nor deny" outcome
into a finding or profiles a named individual.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.enforcement_records import (
    CONTRACT as RECORD_CONTRACT,
)
from src.kb.enforcement_records import (
    EnforcementRecordError,
    canonical,
    digest,
    validate_record,
)

READ_SCOPE = "knowledge:legal:read"
WRITE_SCOPE = "knowledge:legal:write"
DEFAULT_NAMESPACE = "enforcement"
BUNDLE = "legal"
# Provider -> optional Legal feature: SEC, FCA, EPA and EDPB coverage are separate features (EN12).
FEATURES = {"us-sec": "enforcement-sec", "uk-fca": "enforcement-fca", "us-epa-echo": "enforcement-epa",
            "edpb": "enforcement-edpb"}
REMOVAL_CONTRACT = "noesis-enforcement-removal-v1"
# Keys no answer may carry (#2651 exclusions).
FORBIDDEN_KEYS = frozenset({"risk_score", "compliance_score", "risk_rating", "severity_score", "wrongdoing",
                            "inferred_wrongdoing", "finding_of_wrongdoing", "guilty", "found_liable",
                            "liability_finding", "violation_found", "culpability", "recidivism", "person_profile",
                            "prediction", "legal_advice", "penalty_total", "total_penalties", "sum"})
NOTICE = ("Enforcement actions as each regulator published them, authority by authority. Outcomes are quoted as "
          "published (a settlement 'without admitting or denying' stays that wording, never a finding), an initiated "
          "action is not a finding of wrongdoing, penalties are never summed across currencies or authorities, "
          "natural persons are pseudonymised and nothing here is a risk or compliance score or legal advice.")
_DDL = """
CREATE TABLE IF NOT EXISTS enforcement_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, kind TEXT NOT NULL, record_key TEXT NOT NULL,
  provider TEXT NOT NULL, action_key TEXT NOT NULL, current_revision BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS enforcement_record_revisions (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision BIGINT NOT NULL, revision_id TEXT NOT NULL,
  record_hash TEXT NOT NULL, payload_json TEXT NOT NULL, run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id, revision)
);
CREATE TABLE IF NOT EXISTS enforcement_receipts (
  receipt_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  provider TEXT NOT NULL, unit_json TEXT NOT NULL, requests_json TEXT NOT NULL, counts_json TEXT NOT NULL,
  withheld_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL
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
    """Paths of any forbidden (score, inferred-wrongdoing, profile, summed-penalty) key in an answer."""
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in FORBIDDEN_KEYS:
                found.append(f"{path}/{key}")
            found += forbidden_keys(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}/{index}")
    return found


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether an optional Legal enforcement feature is selected in the active composition plan (reads only)."""
    from src.kb.legal import legal_feature_enabled

    if feature not in FEATURES.values():
        raise EnforcementError("invalid_feature", f"feature is one of {sorted(FEATURES.values())}")
    return legal_feature_enabled(conn, feature)


def record_id(namespace: str, record_key: str) -> str:
    return "enf:" + digest([namespace, record_key])[:24]


def removal_marker(provider: str, action: str, *, http_status: int, url: str, unit: Mapping[str, Any]
                   ) -> dict[str, Any]:
    """A page item saying the publisher no longer serves a declared notice; the store turns it into a revision."""
    return {"contract": REMOVAL_CONTRACT, "action_key": action, "provider": provider, "http_status": int(http_status),
            "url": url, "unit": dict(unit)}


class EnforcementStore:
    """Enforcement records as immutable revisions, acquisition receipts and typed reads."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------- writes

    def _current(self, namespace: str, rid: str) -> tuple | None:
        return self.conn.execute(
            "SELECT r.current_revision, v.record_hash, v.payload_json FROM enforcement_records r JOIN "
            "enforcement_record_revisions v ON v.namespace=r.namespace AND v.record_id=r.record_id AND "
            "v.revision=r.current_revision WHERE r.namespace=? AND r.record_id=?", [namespace, rid]).fetchone()

    def _append(self, namespace: str, item: dict[str, Any], run_id: str, observed_at_ms: int,
                counts: dict[str, int]) -> None:
        rid = record_id(namespace, item["record_key"])
        record_hash = digest({k: v for k, v in item.items() if k != "source"} |
                             {"source": {k: v for k, v in item["source"].items() if k != "evidence_origin"}})
        row = self._current(namespace, rid)
        if row and row[1] == record_hash:
            counts["unchanged"] += 1
            return
        revision = 1 if row is None else int(row[0]) + 1
        revision_id = "enf-rev:" + digest([rid, revision, record_hash])[:24]
        self.conn.execute("INSERT INTO enforcement_record_revisions VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, rid, revision, revision_id, record_hash, canonical(item), run_id,
                           int(observed_at_ms)])
        if row is None:
            action = item["record_key"] if item["kind"] == "enforcement_action" else item["action_key"]
            self.conn.execute("INSERT INTO enforcement_records VALUES (?,?,?,?,?,?,?,?)",
                              [namespace, rid, item["kind"], item["record_key"], item["source"]["provider"], action,
                               revision, int(observed_at_ms)])
            counts["inserted"] += 1
        else:
            self.conn.execute("UPDATE enforcement_records SET current_revision=? WHERE namespace=? AND record_id=?",
                              [revision, namespace, rid])
            counts["revised"] += 1

    def apply(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str,
              observed_at_ms: int | None = None) -> dict[str, int]:
        """Validate and append records; content-identical replays are no-ops; removals become revisions."""
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        counts = {"inserted": 0, "revised": 0, "unchanged": 0, "removed_by_source": 0, "removal_not_on_record": 0}
        items, removals = [], []
        for item in records:
            if item.get("contract") == REMOVAL_CONTRACT:
                removals.append(dict(item))
            elif item.get("contract") != RECORD_CONTRACT:
                raise EnforcementError("invalid_record", "page record is not an enforcement record")
            else:
                try:
                    items.append(validate_record(dict(item)))
                except EnforcementRecordError as exc:
                    raise EnforcementError(exc.code, str(exc)) from exc
        self.conn.execute("BEGIN")
        try:
            for item in items:
                self._append(namespace, item, run_id, observed, counts)
            for marker in removals:
                row = self._current(namespace, record_id(namespace, marker["action_key"]))
                if row is None:
                    counts["removal_not_on_record"] += 1
                    continue
                body = json.loads(row[2])
                if body.get("publication_status") == "removed_by_source":
                    counts["unchanged"] += 1
                    continue
                native = dict(body.get("native") or {})
                native["removal"] = {"http_status": marker["http_status"], "url": marker["url"],
                                     "note": "the publisher no longer serves this notice; earlier revisions remain"}
                body.update(publication_status="removed_by_source", native=native)
                self._append(namespace, validate_record(body), run_id, observed, counts)
                counts["revised"] -= 1
                counts["removed_by_source"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def put(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, scopes: Iterable[str]
            ) -> dict[str, int]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        return self.apply(namespace, records, run_id=run_id)

    def record_receipt(self, namespace: str, run_id: str, receipt: Mapping[str, Any], counts: Mapping[str, int]
                       ) -> None:
        if not receipt:
            return
        receipt_id = "enforcement-receipt:" + digest([namespace, run_id, receipt.get("source_id"),
                                                      receipt.get("unit_index"), receipt.get("requests")])[:24]
        self.conn.execute("INSERT OR IGNORE INTO enforcement_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          [receipt_id, namespace, run_id, str(receipt.get("source_id")), str(receipt.get("provider")),
                           canonical(receipt.get("unit") or {}), canonical(receipt.get("requests") or []),
                           canonical(dict(counts)), canonical(receipt.get("withheld") or {}),
                           str(receipt.get("evidence_origin") or "live"), self.now()])

    # -------------------------------------------------------------- reads

    def _rows(self, namespace: str, *, kinds: Sequence[str] | None = None, known_at_ms: int | None = None,
              action_key: str | None = None) -> list[tuple]:
        if not table_exists(self.conn, "enforcement_records"):
            return []
        rows = self.conn.execute(
            "SELECT r.record_id, r.kind, v.revision, v.revision_id, v.record_hash, v.payload_json, v.run_id, "
            "v.observed_at_ms FROM enforcement_records r JOIN enforcement_record_revisions v ON "
            "v.namespace=r.namespace AND v.record_id=r.record_id WHERE r.namespace=? AND (? IS NULL OR "
            "r.action_key=?) ORDER BY r.record_id, v.revision", [namespace, action_key, action_key]).fetchall()
        chosen: dict[str, tuple] = {}
        for row in rows:
            if kinds and row[1] not in kinds:
                continue
            if known_at_ms is None or int(row[7]) <= known_at_ms:
                chosen[row[0]] = row
        return [chosen[k] for k in sorted(chosen)]

    @staticmethod
    def _view(row: tuple) -> dict[str, Any]:
        return {"record_id": row[0], "revision": int(row[2]), "revision_id": row[3], "record_hash": row[4],
                "run_id": row[6], "observed_at_ms": int(row[7]), "record": json.loads(row[5])}

    def views(self, namespace: str, kinds: Sequence[str] | None = None, *, known_at_ms: int | None = None,
              action_key: str | None = None) -> list[dict[str, Any]]:
        """Current (or record-time) revisions of the given kinds; no scope check (callers authorise)."""
        return [self._view(r) for r in self._rows(namespace, kinds=kinds, known_at_ms=known_at_ms,
                                                  action_key=action_key)]

    def by_key(self, namespace: str, record_key: str, *, known_at_ms: int | None = None) -> dict[str, Any] | None:
        history = self.history(namespace, record_key)
        if known_at_ms is not None:
            history = [v for v in history if v["observed_at_ms"] <= known_at_ms]
        return history[-1] if history else None

    def history(self, namespace: str, record_key: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "enforcement_records"):
            return []
        rows = self.conn.execute(
            "SELECT r.record_id, r.kind, v.revision, v.revision_id, v.record_hash, v.payload_json, v.run_id, "
            "v.observed_at_ms FROM enforcement_records r JOIN enforcement_record_revisions v ON "
            "v.namespace=r.namespace AND v.record_id=r.record_id WHERE r.namespace=? AND r.record_id=? "
            "ORDER BY v.revision", [namespace, record_id(namespace, record_key)]).fetchall()
        return [self._view(r) for r in rows]

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "enforcement_records"):
            return None
        row = self.conn.execute(
            "SELECT r.record_id, r.kind, v.revision, v.revision_id, v.record_hash, v.payload_json, v.run_id, "
            "v.observed_at_ms FROM enforcement_records r JOIN enforcement_record_revisions v ON "
            "v.namespace=r.namespace AND v.record_id=r.record_id WHERE r.namespace=? AND v.revision_id=?",
            [namespace, revision_id]).fetchone()
        return self._view(row) if row else None

    def action_children(self, namespace: str, action: str, *, known_at_ms: int | None = None
                        ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {k: [] for k in ("respondent", "decision", "penalty", "appeal",
                                                                "notice_document")}
        for view in self.views(namespace, tuple(out), known_at_ms=known_at_ms, action_key=action):
            out[view["record"]["kind"]].append(view)
        for items in out.values():
            items.sort(key=lambda v: v["record"]["record_key"])
        out["respondent"].sort(key=lambda v: v["record"]["ordinal"])
        return out

    def get(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        view = self.by_key(namespace, record_key)
        if view is None:
            raise EnforcementError("not_found", "no enforcement record with that key in this namespace")
        return {**view, "history": [{"revision": v["revision"], "revision_id": v["revision_id"],
                                     "observed_at_ms": v["observed_at_ms"]} for v in self.history(namespace,
                                                                                                 record_key)]}

    def receipts(self, namespace: str, run_id: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "enforcement_receipts"):
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, source_id, provider, unit_json, requests_json, counts_json, withheld_json, "
            "evidence_origin FROM enforcement_receipts WHERE namespace=? AND run_id=? ORDER BY source_id, receipt_id",
            [namespace, run_id]).fetchall()
        return [{"receipt_id": r[0], "source_id": r[1], "provider": r[2], "unit": json.loads(r[3]),
                 "requests": json.loads(r[4]), "counts": json.loads(r[5]), "withheld": json.loads(r[6]),
                 "evidence_origin": r[7]} for r in rows]


class EnforcementProjector:
    """Source-pack runtime projector for ``noesis-enforcement-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = EnforcementStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("enforcement") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project(self, namespace: str, records: Iterable[Mapping[str, Any]], *, run_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        counts = self.store.apply(namespace, [dict(r) for r in records], run_id=run_id, observed_at_ms=observed_at_ms)
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


OPTIONAL_PACKS = {
    "ownership": ("ownership_records", ("respondents stay as published and unmatched; entity and group queries "
                                        "answer 'ownership_unavailable' (published-identifier lookups still work)")),
    "market": ("market_instrument_alias_assertions", ("CIK links to market issuers and filings are reported as "
                                                      "provider_unavailable")),
    "courts": ("legal_docket_revisions", "related court cases and appeals stay docket-number citations"),
    "legal-works": ("legal_works", "cited statutes and regulations stay unresolved references"),
    "competition": ("ownership_records", "cited competition cases stay unresolved references"),
}


def readiness(conn: Any, namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    """Which features are selected, what each provider has acquired and which optional links degrade."""
    from src.ingestion.enforcement_sources import FORMATS, LIVE_VERIFICATION

    counts: dict[str, int] = {}
    if table_exists(conn, "enforcement_records"):
        for provider, count in conn.execute(
                "SELECT provider, count(*) FROM enforcement_records WHERE namespace=? GROUP BY 1",
                [namespace]).fetchall():
            counts[provider] = int(count)
    providers: dict[str, dict[str, Any]] = {}
    for fmt, spec in FORMATS.items():
        entry = providers.setdefault(spec["provider"], {"authority": spec["authority"], "formats": [],
                                                        "feature": FEATURES[spec["provider"]],
                                                        "records": counts.get(spec["provider"], 0),
                                                        "live": LIVE_VERIFICATION[spec["provider"]]})
        entry["formats"].append(fmt)
    return {"bundle": BUNDLE, "namespace": namespace,
            "features": {feature: feature_enabled(conn, feature) for feature in FEATURES.values()},
            "providers": providers,
            "optional_packs": {name: {"installed": table_exists(conn, table), "when_absent": effect}
                               for name, (table, effect) in OPTIONAL_PACKS.items()},
            "notice": "unverified-live providers have fixture evidence only; a dated live run is outstanding (#2720)"}
