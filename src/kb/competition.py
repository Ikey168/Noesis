"""Competition cases and state aid in Corporate Ownership: store facade, projector and readiness (#2217).

``noesis-competition-record-v1`` records (:mod:`src.kb.competition_records`)
arrive through the ``corporate-ownership`` source-pack runtime (connector
``competition``) and are persisted through the one ownership store
(:class:`src.kb.ownership_store.OwnershipStore`): one stable record per
``record_key``, a new immutable revision only when the published content
changes, every revision with its run and observation time. Stages are
append-only because nothing here deletes a record. Acquisition receipts are
kept per run and unit.

The feature is the Corporate Ownership bundle's optional ``competition``
feature (default off), selected through the active composition plan. It
records what authorities published and never predicts outcomes, assesses
market power or aid compatibility, or gives legal advice.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.ownership_records import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE, canonical, digest  # noqa: F401
from src.kb.ownership_store import OwnershipError, OwnershipStore
from src.kb.ownership_store import authorize as _ownership_authorize

DEFAULT_NAMESPACE = "competition"
FEATURE = "competition"
BUNDLE = "corporate-ownership"
RECORD_CONTRACT = "noesis-competition-record-v1"
CASE_KINDS = ("competition_case", "case_stage", "case_party", "decision_document")
# Keys no answer may carry (#2217 exclusions).
FORBIDDEN_KEYS = frozenset({"outcome_prediction", "prediction", "predicted_outcome", "market_power",
                            "dominance_assessment", "compatibility_assessment", "legality_assessment",
                            "legal_advice", "risk_score"})
_DDL = """
CREATE TABLE IF NOT EXISTS competition_receipts (
  receipt_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  provider TEXT NOT NULL, unit_json TEXT NOT NULL, requests_json TEXT NOT NULL, counts_json TEXT NOT NULL,
  evidence_origin TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL
);
"""


class CompetitionError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    try:
        _ownership_authorize(namespace, set(scopes), required, write=write)
    except OwnershipError as exc:
        raise CompetitionError(exc.code, str(exc)) from exc


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def forbidden_keys(value: Any, path: str = "") -> list[str]:
    """Paths of any forbidden (prediction, market-power, compatibility, advice) key in an answer."""
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


def feature_enabled(conn: Any) -> bool:
    """Whether the optional Corporate Ownership ``competition`` feature is selected in the active plan.

    Features default to off and there is no separate enablement flag; before
    the bundle is composition-managed nothing selects a feature. Reads only.
    """
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ('composition_authority', "
            "'composition_active', 'composition_generations', 'composition_plans')").fetchall()}
        if len(tables) < 4:
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON "
            "g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return FEATURE in ((plan.get("features") or {}).get(BUNDLE) or [])


class CompetitionStore:
    """Competition records through the ownership store, plus acquisition receipts and typed reads."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = OwnershipStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------- writes

    def apply(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str,
              observed_at_ms: int | None = None, principal_id: str = "operator") -> dict[str, int]:
        from src.kb.competition_records import CompetitionRecordError

        if any(r.get("contract") != RECORD_CONTRACT for r in records):
            raise CompetitionError("invalid_record", "page record is not a competition record")
        try:
            return self.store.apply(namespace, records, run_id=run_id,
                                    observed_at_ms=int(observed_at_ms if observed_at_ms is not None else self.now()),
                                    principal_id=principal_id)
        except CompetitionRecordError as exc:
            raise CompetitionError(exc.code, str(exc)) from exc

    def record_receipt(self, namespace: str, run_id: str, receipt: Mapping[str, Any], counts: Mapping[str, int]
                       ) -> None:
        if not receipt:
            return
        receipt_id = "competition-receipt:" + digest([namespace, run_id, receipt.get("source_id"),
                                                      receipt.get("unit_index"), receipt.get("requests")])[:24]
        self.conn.execute("INSERT OR IGNORE INTO competition_receipts VALUES (?,?,?,?,?,?,?,?,?,?)",
                          [receipt_id, namespace, run_id, str(receipt.get("source_id")), str(receipt.get("provider")),
                           canonical(receipt.get("unit") or {}), canonical(receipt.get("requests") or []),
                           canonical(dict(counts)), str(receipt.get("evidence_origin") or "live"), self.now()])

    def put(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, principal_id: str,
            scopes: Iterable[str]) -> dict[str, int]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        return self.apply(namespace, records, run_id=run_id, principal_id=principal_id)

    # -------------------------------------------------------------- reads

    def views(self, namespace: str, kinds: Sequence[str], *, known_at_ms: int | None = None) -> list[dict[str, Any]]:
        """Current (or record-time) revisions of competition records of the given kinds; no scope check."""
        return [v for v in self.store.records(namespace, principal_id=None, scopes={"operator"}, kinds=kinds,
                                              known_at_ms=known_at_ms)
                if v["record"].get("contract") == RECORD_CONTRACT]

    def by_key(self, namespace: str, record_key: str) -> dict[str, Any] | None:
        return self.store.by_key(namespace, record_key, principal_id=None, scopes={"operator"})

    def history(self, namespace: str, record_key: str) -> list[dict[str, Any]]:
        current = self.by_key(namespace, record_key)
        if current is None:
            return []
        return self.store.history(namespace, current["record_id"], principal_id=None, scopes={"operator"})

    def case_children(self, namespace: str, case_key: str, *, known_at_ms: int | None = None
                      ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {"case_stage": [], "case_party": [], "decision_document": []}
        for view in self.views(namespace, tuple(out), known_at_ms=known_at_ms):
            if view["record"].get("case_key") == case_key:
                out[view["record"]["kind"]].append(view)
        return out

    def receipts(self, namespace: str, run_id: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "competition_receipts"):
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, source_id, provider, unit_json, requests_json, counts_json, evidence_origin FROM "
            "competition_receipts WHERE namespace=? AND run_id=? ORDER BY source_id, receipt_id",
            [namespace, run_id]).fetchall()
        return [{"receipt_id": r[0], "source_id": r[1], "provider": r[2], "unit": json.loads(r[3]),
                 "requests": json.loads(r[4]), "counts": json.loads(r[5]), "evidence_origin": r[6]} for r in rows]


class CompetitionProjector:
    """Source-pack runtime projector for ``noesis-competition-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = CompetitionStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("competition") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project(self, namespace: str, records: Iterable[Mapping[str, Any]], *, run_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None,
                principal_id: str = "operator") -> dict[str, Any]:
        counts = self.store.apply(namespace, [dict(r) for r in records], run_id=run_id,
                                  observed_at_ms=observed_at_ms, principal_id=principal_id)
        self.store.record_receipt(namespace, run_id, dict(receipt or {}), counts)
        return {"counts": counts}

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest
        from src.ingestion.source_packs import SourcePackError

        observed = max((int(d["ingested_at"]) for d in documents or [] if d.get("ingested_at") is not None),
                       default=self.store.now())
        try:
            return [self.project(self._namespace(source), [r["competition_record"] for r in records], run_id=run_id,
                                 receipt=dict(page_receipt or {}), observed_at_ms=observed,
                                 principal_id=principal_id)]
        except CompetitionError as exc:
            raise SourcePackError("mapping_failed", str(exc)) from exc

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}


def readiness(conn: Any, namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    """Whether the feature is selected and what each provider has acquired; unverified live access is stated."""
    from src.ingestion.competition_sources import FORMATS, LIVE_VERIFICATION

    counts: dict[str, int] = {}
    if table_exists(conn, "ownership_records"):
        for provider, count in conn.execute(
                "SELECT provider, count(*) FROM ownership_records WHERE namespace=? AND record_key LIKE "
                "'competition:%' GROUP BY 1", [namespace]).fetchall():
            counts[provider] = int(count)
    providers = {}
    for fmt, spec in FORMATS.items():
        providers[spec["provider"]] = {"authority": spec["authority"], "format": fmt,
                                       "records": counts.get(spec["provider"], 0),
                                       "live": LIVE_VERIFICATION[spec["provider"]]}
    return {"bundle": BUNDLE, "feature": FEATURE, "enabled": feature_enabled(conn), "namespace": namespace,
            "providers": providers,
            "notice": "unverified-live providers have fixture evidence only; a dated live run is outstanding (#2368)"}
