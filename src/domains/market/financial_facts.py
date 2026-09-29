"""As-filed market financial facts indexed from existing filing sources."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

FACT_READ_SCOPE = "market:financial-facts:read"
FACT_WRITE_SCOPE = "market:financial-facts:write"
MAX_FINANCIAL_FACT_ROWS = 100_000
_WRITE_LOCK = threading.RLock()
_DDL = """
CREATE TABLE IF NOT EXISTS market_financial_fact_revisions (
 namespace TEXT NOT NULL,
 fact_observation_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 revision_id TEXT NOT NULL,
 issuer_id TEXT NOT NULL,
 filing_accession TEXT NOT NULL,
 taxonomy TEXT NOT NULL,
 concept TEXT NOT NULL,
 context_id TEXT NOT NULL,
 unit TEXT NOT NULL,
 public_at_ms BIGINT,
 retrieved_at_ms BIGINT NOT NULL,
 owner TEXT,
 payload_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, fact_observation_id, revision),
 UNIQUE(namespace, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_market_financial_facts_asof
 ON market_financial_fact_revisions(namespace, issuer_id, taxonomy, concept,
                                     public_at_ms, recorded_at_ms, filing_accession);
"""
_REVISION_FIELDS = {
    "revision_id",
    "revision",
    "prior_revision_id",
    "recorded_at_ms",
    "record_hash",
}


class MarketFinancialFactError(ValueError):
    """Typed financial-fact access, contract, and revision error."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            value["details"] = self.details
        return value


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MarketFinancialFactError(
            "invalid_request", "financial fact must be JSON-safe"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketFinancialFactError(
            "invalid_request", f"{field} must be bounded nonempty text"
        )
    return value.strip()


def _millis(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketFinancialFactError(
            "invalid_request", f"{field} must be nonnegative epoch milliseconds"
        )
    return value


@lru_cache(maxsize=4)
def _validator():
    from jsonschema import Draft7Validator, FormatChecker

    path = (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema/noesis-market-financial-fact-v1.json"
    )
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema, format_checker=FormatChecker())


def _validate(payload: Mapping[str, Any]) -> None:
    errors = sorted(
        _validator().iter_errors(payload),
        key=lambda error: (tuple(str(part) for part in error.path), error.message),
    )
    if errors:
        first = errors[0]
        path = "/".join(str(item) for item in first.path) or "$"
        raise MarketFinancialFactError(
            "contract_invalid",
            f"noesis-market-financial-fact-v1.json rejected {path}: {first.message}",
            path=path,
        )


def ensure_market_financial_fact_schema(conn: Any) -> None:
    """Create an additive index for filing facts; filing documents stay canonical."""

    conn.execute(_DDL)
    from src.domains.market.quality import ensure_market_quality_schema

    ensure_market_quality_schema(conn)


class MarketFinancialFactStore:
    """Append-only fact index linked to EDGAR filing accessions and revisions."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_financial_fact_schema(conn)

    @staticmethod
    def _authorize(
        namespace: str,
        principal_id: str,
        scopes: set[str],
        *,
        write: bool,
    ) -> None:
        _text(namespace, "namespace", limit=100)
        _text(principal_id, "principal_id", limit=200)
        if "operator" in scopes:
            return
        required = FACT_WRITE_SCOPE if write else FACT_READ_SCOPE
        namespace_scopes = (
            {f"namespace:{namespace}:write"}
            if write
            else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
        )
        if required not in scopes or not (namespace_scopes & scopes):
            raise MarketFinancialFactError(
                "unauthorized",
                "current financial-fact and namespace access is required",
            )

    def _check_source_refs(
        self,
        namespace: str,
        refs: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        *,
        write: bool,
    ) -> None:
        from src.domains.market.entitlements import (
            MarketEntitlementError,
            authorize_market_sources,
        )

        try:
            authorize_market_sources(
                self.conn,
                namespace,
                [dict(ref) for ref in refs],
                operation="ingest" if write else "read",
                principal_id=principal_id,
                scopes=scopes,
                now_ms=_millis(self.now(), "now_ms"),
            )
        except MarketEntitlementError as exc:
            raise MarketFinancialFactError(
                exc.code, exc.message, **exc.details
            ) from exc

    def _record_readable(
        self,
        namespace: str,
        payload: Mapping[str, Any],
        principal_id: str,
        scopes: set[str],
        public_cutoff_ms: int,
    ) -> bool:
        if (
            payload.get("owner") not in (None, principal_id)
            and "operator" not in scopes
        ):
            return False
        if (
            payload.get("public_at_ms") is None
            or int(payload["public_at_ms"]) > public_cutoff_ms
        ):
            return False
        refs = payload.get("source_refs") or []
        if not refs:
            return False
        try:
            self._check_source_refs(namespace, refs, principal_id, scopes, write=False)
        except MarketFinancialFactError:
            return False
        return all(
            ref.get("public_at_ms") is not None
            and int(ref["public_at_ms"]) <= public_cutoff_ms
            for ref in refs
        )

    def _require_issuer(
        self,
        namespace: str,
        issuer_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> None:
        from src.domains.market.instruments import MarketInstrumentStore

        try:
            MarketInstrumentStore(self.conn, initialize=False).get_instrument(
                namespace,
                "issuer",
                issuer_id,
                acquired_by_ms=_millis(self.now(), "now_ms"),
                principal_id=principal_id,
                scopes=scopes,
            )
        except Exception as exc:
            raise MarketFinancialFactError(
                "issuer_unavailable",
                "issuer identity is unavailable in this namespace or access context",
            ) from exc

    def put_fact(
        self,
        namespace: str,
        observation: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Validate and append one sourced fact observation or same-accession correction."""

        self._authorize(namespace, principal_id, scopes, write=True)
        if observation.get("contract") != "noesis-market-financial-fact-v1":
            raise MarketFinancialFactError(
                "contract_invalid", "fact must use noesis-market-financial-fact-v1"
            )
        refs = observation.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise MarketFinancialFactError(
                "invalid_request", "source_refs are required"
            )
        self._check_source_refs(namespace, refs, principal_id, scopes, write=True)
        owner = owner if "operator" in scopes else (owner or principal_id)
        if owner not in (None, principal_id) and "operator" not in scopes:
            raise MarketFinancialFactError(
                "unauthorized", "financial facts belong to another principal"
            )
        payload = dict(observation)
        payload["namespace"] = namespace
        payload["owner"] = owner
        fact_id = _text(payload.get("fact_observation_id"), "fact_observation_id")
        issuer_id = _text(payload.get("issuer_id"), "issuer_id", limit=200)
        self._require_issuer(
            namespace,
            issuer_id,
            principal_id=principal_id,
            scopes=scopes,
        )
        for field in _REVISION_FIELDS:
            payload.pop(field, None)
        latest = self.conn.execute(
            """SELECT revision,revision_id,payload_json
               FROM market_financial_fact_revisions
               WHERE namespace=? AND fact_observation_id=?
               ORDER BY revision DESC LIMIT 1""",
            [namespace, fact_id],
        ).fetchone()
        current_revision = int(latest[0]) if latest else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise MarketFinancialFactError(
                "revision_conflict",
                "financial fact changed since it was read",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        revision = current_revision + 1
        recorded_at_ms = _millis(self.now(), "recorded_at_ms")
        payload.update(
            {
                "revision_id": f"{fact_id}@{revision}",
                "revision": revision,
                "prior_revision_id": latest[1] if latest else None,
                "recorded_at_ms": recorded_at_ms,
                "record_hash": "0" * 64,
            }
        )
        _validate(payload)
        payload["record_hash"] = _digest(
            {key: value for key, value in payload.items() if key != "record_hash"}
        )
        _validate(payload)
        if latest:
            prior = json.loads(latest[2])
            if {k: v for k, v in prior.items() if k not in _REVISION_FIELDS} == {
                k: v for k, v in payload.items() if k not in _REVISION_FIELDS
            }:
                return prior
        encoded = _canonical(payload)
        with _WRITE_LOCK:
            self.conn.execute("BEGIN TRANSACTION")
            try:
                self.conn.execute(
                    """INSERT INTO market_financial_fact_revisions
                       (namespace,fact_observation_id,revision,revision_id,issuer_id,
                        filing_accession,taxonomy,concept,context_id,unit,public_at_ms,
                        retrieved_at_ms,owner,payload_json,record_hash,recorded_at_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        namespace,
                        fact_id,
                        revision,
                        payload["revision_id"],
                        issuer_id,
                        payload["filing_accession"],
                        payload["taxonomy"],
                        payload["concept"],
                        payload["context_id"],
                        payload["unit"],
                        payload["public_at_ms"],
                        payload["retrieved_at_ms"],
                        owner,
                        encoded,
                        payload["record_hash"],
                        recorded_at_ms,
                    ],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return payload

    def ingest_batch(
        self,
        namespace: str,
        batch: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        owner: str | None = None,
    ) -> dict[str, Any]:
        """Index normalized facts while returning extraction diagnostics unchanged."""

        facts = batch.get("facts")
        if not isinstance(facts, list) or len(facts) > MAX_FINANCIAL_FACT_ROWS:
            raise MarketFinancialFactError(
                "invalid_batch", "financial-fact batch must be a bounded list"
            )
        stored = [
            self.put_fact(
                namespace,
                fact,
                principal_id=principal_id,
                scopes=scopes,
                owner=owner,
            )
            for fact in facts
        ]
        return {
            "count": len(stored),
            "fact_revision_ids": [item["revision_id"] for item in stored],
            "diagnostics": list(batch.get("diagnostics") or []),
            "readiness": str(batch.get("readiness") or "unknown"),
        }

    def get_facts(
        self,
        namespace: str,
        issuer_id: str,
        *,
        acquired_by_ms: int,
        publicly_available_by_ms: int,
        principal_id: str,
        scopes: set[str],
        taxonomy: str | None = None,
        concept: str | None = None,
        filing_form: str | None = None,
        limit: int = MAX_FINANCIAL_FACT_ROWS,
        offset: int = 0,
        require_complete: bool = False,
        include_page: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return source revisions selected under explicit public/acquisition cutoffs."""

        self._authorize(namespace, principal_id, scopes, write=False)
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        public_cutoff = _millis(publicly_available_by_ms, "publicly_available_by_ms")
        if public_cutoff > acquired_by_ms:
            raise MarketFinancialFactError(
                "invalid_request",
                "public cutoff cannot be later than acquisition cutoff",
            )
        if type(limit) is not int or not 1 <= limit <= MAX_FINANCIAL_FACT_ROWS:
            raise MarketFinancialFactError(
                "invalid_request", "fact limit is outside the bound"
            )
        if type(offset) is not int or not 0 <= offset <= MAX_FINANCIAL_FACT_ROWS:
            raise MarketFinancialFactError(
                "invalid_request", "fact offset is outside the bound"
            )
        clauses = [
            "namespace=?",
            "issuer_id=?",
            "recorded_at_ms<=?",
            "public_at_ms IS NOT NULL AND public_at_ms<=?",
        ]
        params: list[Any] = [
            namespace,
            _text(issuer_id, "issuer_id", limit=200),
            acquired_by_ms,
            public_cutoff,
        ]
        for column, value in (
            ("taxonomy", taxonomy),
            ("concept", concept),
            ("filing_form", filing_form),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(_text(value, column, limit=200))
        rows = self.conn.execute(
            """WITH ranked AS (
                 SELECT fact_observation_id,revision,revision_id,public_at_ms,filing_accession,payload_json,
                   ROW_NUMBER() OVER (PARTITION BY fact_observation_id ORDER BY revision DESC) AS rn
                 FROM market_financial_fact_revisions WHERE """
            + " AND ".join(clauses)
            + """ ) SELECT payload_json FROM ranked WHERE rn=1 AND NOT EXISTS (
                 SELECT 1 FROM market_quality_quarantine q
                 WHERE q.namespace=? AND q.object_kind='financial_fact'
                   AND q.revision_id=ranked.revision_id AND q.status='quarantined'
               ) ORDER BY public_at_ms,filing_accession,fact_observation_id LIMIT ? OFFSET ?""",
            params + [namespace, min(limit + 1, MAX_FINANCIAL_FACT_ROWS), offset],
        ).fetchall()
        facts = []
        for (encoded,) in rows[:limit]:
            payload = json.loads(encoded)
            if self._record_readable(
                namespace, payload, principal_id, scopes, public_cutoff
            ):
                facts.append(payload)
            elif require_complete:
                raise MarketFinancialFactError(
                    "fact_history_unavailable",
                    "current ownership or source entitlement does not allow a complete fact result",
                )
        if include_page:
            has_more = len(rows) > limit
            scanned = min(len(rows), limit)
            return {
                "items": facts,
                "next_offset": offset + scanned if has_more else None,
                "scanned": scanned,
            }
        return facts

    def latest_facts_by_period(
        self,
        namespace: str,
        issuer_id: str,
        **query: Any,
    ) -> dict[str, Any]:
        """Choose the latest accessible filing per exact tag, unit and actual period.

        Contexts from the selected filing remain separate. Conflicting values in
        one accession are returned with a diagnostic rather than collapsed.
        """

        facts = self.get_facts(namespace, issuer_id, require_complete=True, **query)
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            key = (
                fact["taxonomy"],
                fact["concept"],
                _canonical([fact["unit"], fact["period"], fact["period_class"]]),
            )
            groups[key].append(fact)
        selected = []
        diagnostics = []
        for group in groups.values():
            newest = max(
                group,
                key=lambda item: (
                    item.get("public_at_ms") or 0,
                    item.get("filed_at_ms") or 0,
                    item["filing_accession"],
                ),
            )
            same_filing = [
                item
                for item in group
                if item["filing_accession"] == newest["filing_accession"]
            ]
            values = {item["value_lexical"] for item in same_filing}
            if len(values) > 1:
                diagnostics.append(
                    {
                        "code": "conflicting_contexts",
                        "filing_accession": newest["filing_accession"],
                        "taxonomy": newest["taxonomy"],
                        "concept": newest["concept"],
                        "context_ids": sorted(
                            item["context_id"] for item in same_filing
                        ),
                    }
                )
            selected.extend(same_filing)
        return {
            "facts": sorted(
                selected,
                key=lambda item: (
                    item["taxonomy"],
                    item["concept"],
                    item["unit"],
                    _canonical(item["period"]),
                    item["filing_accession"],
                    item["context_id"],
                ),
            ),
            "diagnostics": diagnostics,
            "readiness": "partial" if diagnostics else "ready",
        }
