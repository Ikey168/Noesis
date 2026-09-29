"""Point-in-time issuer, security and listing identities for market research.

The store keeps market identities in additive DuckDB tables and links issuers
to existing Knowledge Graph organizations by ID. It never creates a second
organization graph and never treats a ticker as a security identifier.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

READ_SCOPE = "market:instruments:read"
WRITE_SCOPE = "market:instruments:write"
REVIEW_SCOPE = "market:instruments:review"
_CONTRACTS = {
    "issuer": "noesis-market-issuer-v1",
    "security": "noesis-market-security-v1",
    "listing": "noesis-market-listing-v1",
    "issuer-relationship": "noesis-market-issuer-relationship-v1",
}
_SCHEMA_FILES = {
    "issuer": "noesis-market-issuer-v1.json",
    "security": "noesis-market-security-v1.json",
    "listing": "noesis-market-listing-v1.json",
    "issuer-relationship": "noesis-market-issuer-relationship-v1.json",
}
_DDL = """
CREATE TABLE IF NOT EXISTS market_instrument_object_revisions (
 namespace TEXT NOT NULL,
 object_type TEXT NOT NULL,
 object_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 revision_id TEXT NOT NULL,
 owner TEXT,
 payload_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, object_type, object_id, revision),
 UNIQUE(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS market_instrument_alias_assertions (
 namespace TEXT NOT NULL,
 object_type TEXT NOT NULL,
 object_id TEXT NOT NULL,
 revision_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 scheme TEXT NOT NULL,
 value TEXT NOT NULL,
 normalized_value TEXT NOT NULL,
 valid_from_ms BIGINT NOT NULL,
 valid_to_ms BIGINT,
 assertion_status TEXT NOT NULL,
 public_at_ms BIGINT,
 source_ref_id TEXT NOT NULL,
 entitlement_id TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, object_type, object_id, revision_id, scheme,
             normalized_value, valid_from_ms, source_ref_id)
);
CREATE INDEX IF NOT EXISTS idx_market_instrument_alias_lookup
 ON market_instrument_alias_assertions(namespace, scheme, normalized_value,
                                        valid_from_ms, valid_to_ms, recorded_at_ms);
CREATE TABLE IF NOT EXISTS market_instrument_universe_revisions (
 namespace TEXT NOT NULL,
 universe_id TEXT NOT NULL,
 security_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 revision_id TEXT NOT NULL,
 owner TEXT,
 valid_from_ms BIGINT NOT NULL,
 valid_to_ms BIGINT,
 payload_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, universe_id, security_id, revision),
 UNIQUE(namespace, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_market_universe_asof
 ON market_instrument_universe_revisions(namespace, universe_id, recorded_at_ms,
                                           valid_from_ms, valid_to_ms);
CREATE TABLE IF NOT EXISTS market_instrument_reviews (
 review_id TEXT PRIMARY KEY,
 namespace TEXT NOT NULL,
 owner TEXT NOT NULL,
 status TEXT NOT NULL,
 subject_type TEXT NOT NULL,
 query_value TEXT NOT NULL,
 reason TEXT NOT NULL,
 query_json TEXT NOT NULL,
 candidates_json TEXT NOT NULL,
 decision_json TEXT,
 created_at_ms BIGINT NOT NULL,
 updated_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_market_instrument_review_queue
 ON market_instrument_reviews(namespace, status, created_at_ms);
"""
_WRITE_LOCK = threading.RLock()


class MarketInstrumentError(ValueError):
    """Typed market-instrument failure safe to return through an adapter."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


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
        raise MarketInstrumentError(
            "invalid_request", "payload must be JSON-safe"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketInstrumentError(
            "invalid_request", f"{field} must be nonempty bounded text"
        )
    return value.strip()


def _millis(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketInstrumentError(
            "invalid_request", f"{field} must be nonnegative epoch milliseconds"
        )
    return value


def _normalize_alias(scheme: str, value: str) -> str:
    normalized = value.strip().casefold()
    if scheme.casefold() == "cik":
        raw = re.sub(r"^cik", "", normalized)
        if raw.isdigit():
            normalized = raw.zfill(10)
    return normalized


@lru_cache(maxsize=16)
def _contract_validator(schema_name: str):
    from jsonschema import Draft7Validator

    schema_path = (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema"
        / schema_name
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _validate_contract(payload: Mapping[str, Any], schema_name: str) -> None:
    errors = sorted(
        _contract_validator(schema_name).iter_errors(payload),
        key=lambda error: (tuple(str(part) for part in error.path), error.message),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "$"
        raise MarketInstrumentError(
            "contract_invalid",
            f"{schema_name} rejected {location}: {first.message}",
            schema=schema_name,
            path=location,
        )


def ensure_market_instrument_schema(conn: Any) -> None:
    """Create additive market identity, alias, review and universe tables."""

    conn.execute(_DDL)
    from src.domains.market.quality import ensure_market_quality_schema

    ensure_market_quality_schema(conn)


class MarketInstrumentStore:
    """Append-only instrument identities with exact, temporal alias resolution."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_instrument_schema(conn)

    @staticmethod
    def new_id(kind: str) -> str:
        if kind not in {"issuer", "security", "listing", "relationship"}:
            raise MarketInstrumentError(
                "invalid_request", "unknown market identity kind"
            )
        return f"{kind}:{uuid.uuid4().hex}"

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
        required = WRITE_SCOPE if write else READ_SCOPE
        namespace_scopes = (
            {f"namespace:{namespace}:write"}
            if write
            else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
        )
        if required not in scopes or not (namespace_scopes & scopes):
            raise MarketInstrumentError(
                "unauthorized", "current market and namespace access is required"
            )

    @staticmethod
    def _check_owner(
        owner: str | None, principal_id: str, scopes: set[str], *, write: bool
    ) -> None:
        if owner is None:
            if "operator" not in scopes:
                raise MarketInstrumentError(
                    "unauthorized",
                    "only an operator may write shared market master records",
                )
            return
        if owner != principal_id and "operator" not in scopes:
            raise MarketInstrumentError(
                "unauthorized", "market identity belongs to another principal"
            )

    def _check_entitlements(
        self,
        namespace: str,
        source_refs: Sequence[Mapping[str, Any]],
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
                [dict(ref) for ref in source_refs],
                operation="ingest" if write else "read",
                principal_id=principal_id,
                scopes=scopes,
                now_ms=_millis(self.now(), "now_ms"),
            )
        except MarketEntitlementError as exc:
            raise MarketInstrumentError(exc.code, exc.message, **exc.details) from exc

    def _latest(self, namespace: str, object_type: str, object_id: str):
        row = self.conn.execute(
            """SELECT revision_id, revision, owner, payload_json, recorded_at_ms
               FROM market_instrument_object_revisions
               WHERE namespace=? AND object_type=? AND object_id=?
               ORDER BY revision DESC LIMIT 1""",
            [namespace, object_type, object_id],
        ).fetchone()
        return row

    @staticmethod
    def _semantic_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        ignored = {
            "contract",
            "revision_id",
            "revision",
            "recorded_at_ms",
            "record_hash",
        }
        return {key: value for key, value in payload.items() if key not in ignored}

    def _alias_assertions(self, object_type: str, body: Mapping[str, Any]):
        if object_type == "issuer":
            candidates = [
                ("issuer_name", item) for item in body.get("name_assertions", [])
            ] + [
                (str(item.get("scheme", "")).casefold(), item)
                for item in body.get("identifiers", [])
            ]
        elif object_type == "security":
            candidates = [
                (str(item.get("scheme", "")).casefold(), item)
                for item in body.get("identifiers", [])
            ]
        elif object_type == "listing":
            candidates = [
                ("ticker", item) for item in body.get("ticker_assertions", [])
            ]
        else:
            candidates = []
        refs = {str(ref["source_ref_id"]): ref for ref in body.get("source_refs", [])}
        rows = []
        for scheme, item in candidates:
            value = _text(item.get("value"), "alias.value", limit=200)
            source_ref_id = _text(
                item.get("source_ref_id"), "alias.source_ref_id", limit=200
            )
            source = refs.get(source_ref_id)
            if source is None:
                raise MarketInstrumentError(
                    "invalid_request",
                    "each dated alias must cite a source_ref in the record",
                )
            valid_from = _millis(item.get("valid_from_ms"), "alias.valid_from_ms")
            assertion_status = item.get("assertion_status", "active")
            if assertion_status not in {"active", "retracted"}:
                raise MarketInstrumentError(
                    "invalid_request", "assertion_status must be active or retracted"
                )
            valid_to = item.get("valid_to_ms")
            if valid_to is not None:
                valid_to = _millis(valid_to, "alias.valid_to_ms")
                if assertion_status == "active" and valid_to <= valid_from:
                    raise MarketInstrumentError(
                        "invalid_request",
                        "alias.valid_to_ms must be after valid_from_ms",
                    )
            rows.append(
                (
                    scheme,
                    value,
                    _normalize_alias(scheme, value),
                    valid_from,
                    valid_to,
                    assertion_status,
                    source_ref_id,
                    str(source["entitlement_id"]),
                    None
                    if source.get("public_at_ms") is None
                    else _millis(source["public_at_ms"], "source_ref.public_at_ms"),
                )
            )
        return rows

    def _put_object(
        self,
        object_type: str,
        object_id: str,
        namespace: str,
        body: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        owner: str | None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, write=True)
        self._check_owner(owner, principal_id, scopes, write=True)
        source_refs = body.get("source_refs", [])
        if not isinstance(source_refs, list) or not source_refs:
            raise MarketInstrumentError("invalid_request", "source_refs are required")
        self._check_entitlements(
            namespace, source_refs, principal_id, scopes, write=True
        )
        object_id = _text(object_id, f"{object_type}_id", limit=200)
        prior = self._latest(namespace, object_type, object_id)
        prior_revision = int(prior[1]) if prior else 0
        if expected_revision is not None and expected_revision != prior_revision:
            raise MarketInstrumentError(
                "revision_conflict",
                "market identity changed since it was read",
                expected_revision=expected_revision,
                current_revision=prior_revision,
            )
        revision = prior_revision + 1
        recorded_at_ms = _millis(self.now(), "recorded_at_ms")
        payload = {
            "contract": _CONTRACTS[object_type],
            "namespace": namespace,
            "owner": owner,
            **dict(body),
            f"{object_type.replace('-', '_')}_id": object_id,
            "revision_id": f"market-{object_type}:{object_id}@{revision}",
            "revision": revision,
            "recorded_at_ms": recorded_at_ms,
        }
        payload.pop("record_hash", None)
        payload["record_hash"] = _digest(payload)
        _validate_contract(payload, _SCHEMA_FILES[object_type])
        aliases = self._alias_assertions(object_type, payload)
        if prior:
            previous = json.loads(prior[3])
            if self._semantic_payload(previous) == self._semantic_payload(payload):
                return previous
        encoded = _canonical(payload)
        with _WRITE_LOCK:
            self.conn.execute("BEGIN TRANSACTION")
            try:
                self.conn.execute(
                    """INSERT INTO market_instrument_object_revisions
                       (namespace, object_type, object_id, revision, revision_id,
                        owner, payload_json, record_hash, recorded_at_ms)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        namespace,
                        object_type,
                        object_id,
                        revision,
                        payload["revision_id"],
                        owner,
                        encoded,
                        payload["record_hash"],
                        recorded_at_ms,
                    ],
                )
                for (
                    scheme,
                    value,
                    normalized,
                    valid_from,
                    valid_to,
                    assertion_status,
                    source_id,
                    entitlement,
                    public_at,
                ) in aliases:
                    self.conn.execute(
                        """INSERT INTO market_instrument_alias_assertions
                        (namespace, object_type, object_id, revision_id, scheme,
                            revision, value, normalized_value, valid_from_ms, valid_to_ms,
                            assertion_status, public_at_ms, source_ref_id, entitlement_id, recorded_at_ms)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [
                            namespace,
                            object_type,
                            object_id,
                            payload["revision_id"],
                            scheme,
                            payload["revision"],
                            value,
                            normalized,
                            valid_from,
                            valid_to,
                            assertion_status,
                            public_at,
                            source_id,
                            entitlement,
                            recorded_at_ms,
                        ],
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return payload

    @staticmethod
    def _owner_for_write(
        owner: str | None, principal_id: str, scopes: set[str]
    ) -> str | None:
        if owner is None and "operator" not in scopes:
            return principal_id
        return owner

    def put_issuer(
        self,
        namespace: str,
        *,
        kg_entity_id: str,
        display_name: str,
        source_refs: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        issuer_id: str | None = None,
        legal_name: str | None = None,
        status: str = "active",
        name_assertions: Sequence[Mapping[str, Any]] = (),
        identifiers: Sequence[Mapping[str, Any]] = (),
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        issuer_id = issuer_id or self.new_id("issuer")
        owner = self._owner_for_write(owner, principal_id, scopes)
        body = {
            "kg_entity_id": _text(kg_entity_id, "kg_entity_id"),
            "display_name": _text(display_name, "display_name"),
            "legal_name": legal_name,
            "status": status,
            "name_assertions": [dict(item) for item in name_assertions],
            "identifiers": [dict(item) for item in identifiers],
            "source_refs": [dict(item) for item in source_refs],
        }
        return self._put_object(
            "issuer",
            issuer_id,
            namespace,
            body,
            principal_id=principal_id,
            scopes=scopes,
            owner=owner,
            expected_revision=expected_revision,
        )

    def put_security(
        self,
        namespace: str,
        *,
        issuer_id: str,
        security_type: str,
        source_refs: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        security_id: str | None = None,
        share_class: str | None = None,
        denomination_currency: str | None = None,
        industry_code: str | None = None,
        status: str = "active",
        identifiers: Sequence[Mapping[str, Any]] = (),
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, write=True)
        issuer_id = _text(issuer_id, "issuer_id")
        issuer = self._read_object_asof(
            namespace,
            "issuer",
            issuer_id,
            _millis(self.now(), "now_ms"),
            principal_id=principal_id,
            scopes=scopes,
        )
        if issuer is None:
            raise MarketInstrumentError(
                "issuer_not_found", "issuer identity is unavailable"
            )
        security_id = security_id or self.new_id("security")
        owner = self._owner_for_write(owner, principal_id, scopes)
        body = {
            "issuer_id": issuer_id,
            "security_type": _text(security_type, "security_type", limit=100),
            "share_class": share_class,
            "denomination_currency": denomination_currency,
            "industry_code": industry_code,
            "status": status,
            "identifiers": [dict(item) for item in identifiers],
            "source_refs": [dict(item) for item in source_refs],
        }
        return self._put_object(
            "security",
            security_id,
            namespace,
            body,
            principal_id=principal_id,
            scopes=scopes,
            owner=owner,
            expected_revision=expected_revision,
        )

    def list_listings_for_security(
        self,
        namespace: str,
        security_id: str,
        *,
        as_of_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return readable listings for a security at a historical date.

        This is intentionally a bounded identity lookup for peer/universe
        selection. It returns the same nested listing/security/issuer shape as
        ``get_instrument`` and never treats a ticker as an identity.
        """

        self._authorize(namespace, principal_id, scopes, write=False)
        security_id = _text(security_id, "security_id", limit=200)
        as_of_ms = _millis(as_of_ms, "as_of_ms")
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if as_of_ms > acquired_by_ms:
            raise MarketInstrumentError(
                "invalid_request", "as_of_ms cannot be later than acquired_by_ms"
            )
        if publicly_available_by_ms is not None:
            publicly_available_by_ms = _millis(
                publicly_available_by_ms, "publicly_available_by_ms"
            )
            if publicly_available_by_ms > acquired_by_ms:
                raise MarketInstrumentError(
                    "invalid_request",
                    "publicly_available_by_ms cannot be later than acquired_by_ms",
                )
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MarketInstrumentError(
                "invalid_request", "listing limit must be between 1 and 100"
            )
        rows = self.conn.execute(
            """WITH latest AS (
                   SELECT object_id,payload_json,
                          ROW_NUMBER() OVER (
                              PARTITION BY object_id ORDER BY revision DESC
                          ) AS row_number
                   FROM market_instrument_object_revisions
                   WHERE namespace=? AND object_type='listing'
                     AND recorded_at_ms<=? AND payload_json LIKE ?
               )
               SELECT object_id,payload_json FROM latest
               WHERE row_number=1
               ORDER BY object_id LIMIT ?""",
            [namespace, acquired_by_ms, f'%"security_id":"{security_id}"%', limit],
        ).fetchall()
        result: list[dict[str, Any]] = []
        for object_id, encoded in rows:
            listing = json.loads(encoded)
            if listing.get("security_id") != security_id:
                continue
            valid_from = int(listing.get("valid_from_ms", 0))
            valid_to = listing.get("valid_to_ms")
            if valid_from > as_of_ms or (
                valid_to is not None and int(valid_to) <= as_of_ms
            ):
                continue
            if not self._payload_readable(
                namespace,
                listing,
                principal_id,
                scopes,
                publicly_available_by_ms=publicly_available_by_ms,
            ):
                continue
            nested = self.get_instrument(
                namespace,
                "listing",
                object_id,
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=publicly_available_by_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            security = self.get_instrument(
                namespace,
                "security",
                nested["security_id"],
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=publicly_available_by_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            issuer = self.get_instrument(
                namespace,
                "issuer",
                security["issuer_id"],
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=publicly_available_by_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            result.append({"listing": nested, "security": security, "issuer": issuer})
        return result

    def put_listing(
        self,
        namespace: str,
        *,
        security_id: str,
        mic: str,
        currency: str,
        valid_from_ms: int,
        valid_to_ms: int | None,
        ticker_assertions: Sequence[Mapping[str, Any]],
        source_refs: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        listing_id: str | None = None,
        timezone: str | None = None,
        status: str = "active",
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, write=True)
        security_id = _text(security_id, "security_id")
        security = self._read_object_asof(
            namespace,
            "security",
            security_id,
            _millis(self.now(), "now_ms"),
            principal_id=principal_id,
            scopes=scopes,
        )
        if security is None:
            raise MarketInstrumentError(
                "security_not_found", "security identity is unavailable"
            )
        valid_from_ms = _millis(valid_from_ms, "valid_from_ms")
        if valid_to_ms is not None:
            valid_to_ms = _millis(valid_to_ms, "valid_to_ms")
            if valid_to_ms <= valid_from_ms:
                raise MarketInstrumentError(
                    "invalid_request", "valid_to_ms must follow valid_from_ms"
                )
        if status == "inactive" and valid_to_ms is None:
            raise MarketInstrumentError(
                "invalid_request",
                "an inactive listing needs a valid_to_ms delisting boundary",
            )
        listing_id = listing_id or self.new_id("listing")
        owner = self._owner_for_write(owner, principal_id, scopes)
        body = {
            "security_id": security_id,
            "mic": _text(mic, "mic", limit=4).upper(),
            "currency": _text(currency, "currency", limit=3).upper(),
            "timezone": timezone,
            "status": status,
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "ticker_assertions": [dict(item) for item in ticker_assertions],
            "source_refs": [dict(item) for item in source_refs],
        }
        return self._put_object(
            "listing",
            listing_id,
            namespace,
            body,
            principal_id=principal_id,
            scopes=scopes,
            owner=owner,
            expected_revision=expected_revision,
        )

    def _read_object_asof(
        self,
        namespace: str,
        object_type: str,
        object_id: str,
        acquired_by_ms: int,
        *,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any] | None:
        rows = self.conn.execute(
            """SELECT payload_json FROM market_instrument_object_revisions
               WHERE namespace=? AND object_type=? AND object_id=? AND recorded_at_ms<=?
               ORDER BY revision DESC""",
            [namespace, object_type, object_id, acquired_by_ms],
        ).fetchall()
        for (encoded,) in rows:
            payload = json.loads(encoded)
            if (
                payload.get("owner") not in (None, principal_id)
                and "operator" not in scopes
            ):
                continue
            sources = payload.get("source_refs", [])
            try:
                self._check_entitlements(
                    namespace, sources, principal_id, scopes, write=False
                )
            except MarketInstrumentError:
                continue
            if publicly_available_by_ms is not None and not sources:
                continue
            if publicly_available_by_ms is not None and not all(
                source.get("public_at_ms") is not None
                and int(source["public_at_ms"]) <= publicly_available_by_ms
                for source in sources
            ):
                continue
            return payload
        return None

    def _alias_rows(
        self,
        namespace: str,
        object_type: str,
        scheme: str,
        value: str,
        as_of_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None,
    ) -> list[tuple[Any, ...]]:
        normalized = _normalize_alias(scheme, value)
        public_clause = ""
        params: list[Any] = [namespace, object_type, scheme, normalized, acquired_by_ms]
        if publicly_available_by_ms is not None:
            public_clause = "AND public_at_ms IS NOT NULL AND public_at_ms<=?"
            params.append(publicly_available_by_ms)
        params.extend([as_of_ms, as_of_ms])
        return self.conn.execute(
            """WITH latest AS (
                   SELECT namespace, object_type, object_id, value, source_ref_id,
                          entitlement_id, valid_from_ms, valid_to_ms,
                          assertion_status,
                          ROW_NUMBER() OVER (
                              PARTITION BY namespace, object_type, object_id, scheme,
                                           normalized_value, valid_from_ms
                              ORDER BY recorded_at_ms DESC, revision DESC, source_ref_id DESC
                          ) AS row_number
                   FROM market_instrument_alias_assertions
                   WHERE namespace=? AND object_type=? AND scheme=?
                     AND normalized_value=? AND recorded_at_ms<=?
                     """
            + public_clause
            + """
               )
               SELECT object_id, value, source_ref_id, entitlement_id
               FROM latest
               WHERE row_number=1 AND assertion_status='active' AND valid_from_ms<=?
                 AND (valid_to_ms IS NULL OR valid_to_ms>?)
               ORDER BY object_id, source_ref_id""",
            params,
        ).fetchall()

    def _payload_readable(
        self,
        namespace: str,
        payload: Mapping[str, Any],
        principal_id: str,
        scopes: set[str],
        *,
        publicly_available_by_ms: int | None = None,
    ) -> bool:
        if (
            payload.get("owner") not in (None, principal_id)
            and "operator" not in scopes
        ):
            return False
        try:
            self._check_entitlements(
                namespace,
                payload.get("source_refs", []),
                principal_id,
                scopes,
                write=False,
            )
        except MarketInstrumentError:
            return False
        if publicly_available_by_ms is not None and not all(
            source.get("public_at_ms") is not None
            and int(source["public_at_ms"]) <= publicly_available_by_ms
            for source in payload.get("source_refs", [])
        ):
            return False
        return True

    def _open_review(
        self,
        namespace: str,
        principal_id: str,
        subject_type: str,
        scheme: str,
        value: str,
        as_of_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None,
        reason: str,
        candidates: Sequence[Mapping[str, Any]],
    ) -> str:
        query = {
            "subject_type": subject_type,
            "scheme": scheme,
            "normalized_value": _normalize_alias(scheme, value),
            "as_of_ms": as_of_ms,
            "acquired_by_ms": acquired_by_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
        }
        candidate_ids = [str(item.get("object_id", "")) for item in candidates]
        review_id = (
            "review:"
            + _digest(
                {"namespace": namespace, "query": query, "candidates": candidate_ids}
            )[:24]
        )
        now_ms = _millis(self.now(), "review.created_at_ms")
        self.conn.execute(
            """INSERT INTO market_instrument_reviews
               (review_id, namespace, owner, status, subject_type, query_value,
                reason, query_json, candidates_json, decision_json,
                created_at_ms, updated_at_ms)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, NULL, ?, ?)
               ON CONFLICT(review_id) DO NOTHING""",
            [
                review_id,
                namespace,
                principal_id,
                subject_type,
                value,
                reason,
                _canonical(query),
                _canonical(list(candidates)),
                now_ms,
                now_ms,
            ],
        )
        return review_id

    def _resolve_alias(
        self,
        namespace: str,
        *,
        object_type: str,
        scheme: str,
        value: str,
        as_of_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None,
        principal_id: str,
        scopes: set[str],
        mic: str | None = None,
    ) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, write=False)
        _text(value, "value", limit=200)
        as_of_ms = _millis(as_of_ms, "as_of_ms")
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if publicly_available_by_ms is not None:
            publicly_available_by_ms = _millis(
                publicly_available_by_ms, "publicly_available_by_ms"
            )
            if publicly_available_by_ms > acquired_by_ms:
                raise MarketInstrumentError(
                    "invalid_request",
                    "publicly_available_by_ms cannot be later than acquired_by_ms",
                )
        if as_of_ms > acquired_by_ms:
            raise MarketInstrumentError(
                "invalid_request", "as_of_ms cannot be later than acquired_by_ms"
            )
        rows = self._alias_rows(
            namespace,
            object_type,
            scheme,
            value,
            as_of_ms,
            acquired_by_ms,
            publicly_available_by_ms,
        )
        candidates: dict[str, dict[str, Any]] = {}
        for object_id, alias_value, source_ref_id, _entitlement_id in rows:
            payload = self._read_object_asof(
                namespace,
                object_type,
                object_id,
                acquired_by_ms,
                publicly_available_by_ms=publicly_available_by_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            if payload is None:
                continue
            if object_type == "listing":
                listing_from = int(payload["valid_from_ms"])
                listing_to = payload.get("valid_to_ms")
                if listing_from > as_of_ms or (
                    listing_to is not None and int(listing_to) <= as_of_ms
                ):
                    continue
                if mic and str(payload.get("mic", "")).upper() != mic.upper():
                    continue
                security = self._read_object_asof(
                    namespace,
                    "security",
                    payload["security_id"],
                    acquired_by_ms,
                    publicly_available_by_ms=publicly_available_by_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                )
                if security is None:
                    continue
                issuer = self._read_object_asof(
                    namespace,
                    "issuer",
                    security["issuer_id"],
                    acquired_by_ms,
                    publicly_available_by_ms=publicly_available_by_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                )
                if issuer is None:
                    continue
                candidate = {
                    "object_id": object_id,
                    "listing": payload,
                    "security": security,
                    "issuer": issuer,
                    "matched_alias": {
                        "scheme": scheme,
                        "value": alias_value,
                        "source_ref_id": source_ref_id,
                    },
                }
            elif object_type == "security":
                issuer = self._read_object_asof(
                    namespace,
                    "issuer",
                    payload["issuer_id"],
                    acquired_by_ms,
                    publicly_available_by_ms=publicly_available_by_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                )
                if issuer is None:
                    continue
                candidate = {
                    "object_id": object_id,
                    "security": payload,
                    "issuer": issuer,
                    "matched_alias": {
                        "scheme": scheme,
                        "value": alias_value,
                        "source_ref_id": source_ref_id,
                    },
                }
            else:
                candidate = {
                    "object_id": object_id,
                    "issuer": payload,
                    "matched_alias": {
                        "scheme": scheme,
                        "value": alias_value,
                        "source_ref_id": source_ref_id,
                    },
                }
            candidates[object_id] = candidate
        ordered = [candidates[key] for key in sorted(candidates)]
        if len(ordered) == 1:
            status, reason, review_id = "resolved", None, None
        elif ordered:
            status, reason = "ambiguous", "multiple_active_mappings"
            review_id = self._open_review(
                namespace,
                principal_id,
                object_type,
                scheme,
                value,
                as_of_ms,
                acquired_by_ms,
                publicly_available_by_ms,
                reason,
                ordered,
            )
        else:
            status, reason = "unresolved", "no_active_mapping"
            review_id = self._open_review(
                namespace,
                principal_id,
                object_type,
                scheme,
                value,
                as_of_ms,
                acquired_by_ms,
                publicly_available_by_ms,
                reason,
                [],
            )
        return {
            "status": status,
            "query": {
                "namespace": namespace,
                "object_type": object_type,
                "scheme": scheme,
                "value": value,
                "as_of_ms": as_of_ms,
                "acquired_by_ms": acquired_by_ms,
                "publicly_available_by_ms": publicly_available_by_ms,
                "historical_public_cutoff_applied": publicly_available_by_ms
                is not None,
                "mic": mic,
            },
            "candidates": ordered,
            "reason": reason,
            "review_id": review_id,
        }

    def resolve_symbol(
        self,
        namespace: str,
        symbol: str,
        *,
        as_of_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
        mic: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an exact ticker at both a valid-time and acquisition cutoff.

        If a symbol is ambiguous across venues, issuers or reused listings, the
        result stays ambiguous and an owner-scoped review is queued.
        """

        return self._resolve_alias(
            namespace,
            object_type="listing",
            scheme="ticker",
            value=symbol,
            as_of_ms=as_of_ms,
            acquired_by_ms=acquired_by_ms,
            publicly_available_by_ms=publicly_available_by_ms,
            principal_id=principal_id,
            scopes=scopes,
            mic=mic,
        )

    def resolve_identifier(
        self,
        namespace: str,
        scheme: str,
        value: str,
        *,
        object_type: str,
        as_of_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Resolve an exact issuer/security identifier such as a CIK or FIGI."""

        if object_type not in {"issuer", "security"}:
            raise MarketInstrumentError(
                "invalid_request", "object_type must be issuer or security"
            )
        scheme = _text(scheme, "scheme", limit=100).casefold()
        return self._resolve_alias(
            namespace,
            object_type=object_type,
            scheme=scheme,
            value=value,
            as_of_ms=as_of_ms,
            acquired_by_ms=acquired_by_ms,
            publicly_available_by_ms=publicly_available_by_ms,
            principal_id=principal_id,
            scopes=scopes,
        )

    def list_mapping_reviews(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        status: str = "pending",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self._authorize(namespace, principal_id, scopes, write=False)
        if "operator" not in scopes and REVIEW_SCOPE not in scopes:
            raise MarketInstrumentError(
                "unauthorized", "market mapping review scope is required"
            )
        if status not in {"pending", "resolved", "dismissed", "all"}:
            raise MarketInstrumentError("invalid_request", "unsupported review status")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise MarketInstrumentError(
                "invalid_request", "limit must be between 1 and 500"
            )
        query = (
            "SELECT review_id,owner,status,subject_type,query_value,reason,"
            "query_json,candidates_json,decision_json,created_at_ms,updated_at_ms "
            "FROM market_instrument_reviews WHERE namespace=?"
        )
        params: list[Any] = [namespace]
        if status != "all":
            query += " AND status=?"
            params.append(status)
        if "operator" not in scopes:
            query += " AND owner=?"
            params.append(principal_id)
        query += " ORDER BY created_at_ms,review_id LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [
            {
                "review_id": row[0],
                "owner": row[1],
                "status": row[2],
                "subject_type": row[3],
                "query_value": row[4],
                "reason": row[5],
                "query": json.loads(row[6]),
                "candidates": json.loads(row[7]),
                "decision": json.loads(row[8]) if row[8] else None,
                "created_at_ms": int(row[9]),
                "updated_at_ms": int(row[10]),
            }
            for row in rows
        ]

    def decide_mapping_review(
        self,
        namespace: str,
        review_id: str,
        *,
        decision: str,
        note: str,
        principal_id: str,
        scopes: set[str],
        resolved_object_ids: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Record an explicit human decision on an unresolved symbol mapping."""

        self._authorize(namespace, principal_id, scopes, write=True)
        if "operator" not in scopes and REVIEW_SCOPE not in scopes:
            raise MarketInstrumentError(
                "unauthorized", "market mapping review scope is required"
            )
        if decision not in {"resolved", "dismissed"}:
            raise MarketInstrumentError(
                "invalid_request", "decision must be resolved or dismissed"
            )
        note = _text(note, "note", limit=2000)
        row = self.conn.execute(
            """SELECT owner,status,candidates_json,subject_type FROM market_instrument_reviews
               WHERE namespace=? AND review_id=?""",
            [namespace, review_id],
        ).fetchone()
        if not row:
            raise MarketInstrumentError("not_found", "mapping review is unavailable")
        if row[0] != principal_id and "operator" not in scopes:
            raise MarketInstrumentError(
                "unauthorized", "mapping review belongs to another principal"
            )
        if row[1] != "pending":
            raise MarketInstrumentError(
                "review_closed", "mapping review is already closed"
            )
        resolved_ids = [
            _text(value, "resolved_object_id", limit=200)
            for value in resolved_object_ids
        ]
        if decision == "resolved" and not resolved_ids:
            raise MarketInstrumentError(
                "invalid_request", "resolved_object_ids are required"
            )
        if decision == "resolved":
            for object_id in resolved_ids:
                linked = self._read_object_asof(
                    namespace,
                    row[3],
                    object_id,
                    _millis(self.now(), "now_ms"),
                    principal_id=principal_id,
                    scopes=scopes,
                )
                if linked is None:
                    raise MarketInstrumentError(
                        "invalid_request",
                        "review resolution must cite a readable current candidate",
                    )
        recorded_at_ms = _millis(self.now(), "decision.recorded_at_ms")
        payload = {
            "decision": decision,
            "note": note,
            "resolved_object_ids": resolved_ids,
            "reviewed_by": principal_id,
            "recorded_at_ms": recorded_at_ms,
        }
        self.conn.execute(
            """UPDATE market_instrument_reviews
               SET status=?,decision_json=?,updated_at_ms=?
               WHERE namespace=? AND review_id=? AND status='pending'""",
            [decision, _canonical(payload), recorded_at_ms, namespace, review_id],
        )
        return {"review_id": review_id, **payload}

    def put_universe_membership(
        self,
        namespace: str,
        *,
        universe_id: str,
        security_id: str,
        membership_status: str,
        valid_from_ms: int,
        valid_to_ms: int | None,
        source_refs: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append an included/excluded membership interval; never delete history."""

        self._authorize(namespace, principal_id, scopes, write=True)
        universe_id = _text(universe_id, "universe_id", limit=200)
        security_id = _text(security_id, "security_id", limit=200)
        owner = self._owner_for_write(owner, principal_id, scopes)
        self._check_owner(owner, principal_id, scopes, write=True)
        refs = [dict(item) for item in source_refs]
        self._check_entitlements(namespace, refs, principal_id, scopes, write=True)
        security = self._read_object_asof(
            namespace,
            "security",
            security_id,
            _millis(self.now(), "now_ms"),
            principal_id=principal_id,
            scopes=scopes,
        )
        if security is None:
            raise MarketInstrumentError(
                "security_not_found", "security identity is unavailable"
            )
        if membership_status not in {"included", "excluded"}:
            raise MarketInstrumentError(
                "invalid_request", "membership_status must be included or excluded"
            )
        valid_from_ms = _millis(valid_from_ms, "valid_from_ms")
        if valid_to_ms is not None:
            valid_to_ms = _millis(valid_to_ms, "valid_to_ms")
            if valid_to_ms <= valid_from_ms:
                raise MarketInstrumentError(
                    "invalid_request", "valid_to_ms must follow valid_from_ms"
                )
        membership_id = (
            "membership:"
            + _digest(
                {
                    "namespace": namespace,
                    "universe_id": universe_id,
                    "security_id": security_id,
                }
            )[:24]
        )
        prior = self.conn.execute(
            """SELECT revision,owner,payload_json FROM market_instrument_universe_revisions
               WHERE namespace=? AND universe_id=? AND security_id=?
               ORDER BY revision DESC LIMIT 1""",
            [namespace, universe_id, security_id],
        ).fetchone()
        current_revision = int(prior[0]) if prior else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise MarketInstrumentError(
                "revision_conflict",
                "universe membership changed since it was read",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        revision = current_revision + 1
        recorded_at_ms = _millis(self.now(), "recorded_at_ms")
        payload = {
            "contract": "noesis-market-universe-membership-v1",
            "namespace": namespace,
            "owner": owner,
            "membership_id": membership_id,
            "universe_id": universe_id,
            "security_id": security_id,
            "membership_status": membership_status,
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "revision_id": f"market-universe:{membership_id}@{revision}",
            "revision": revision,
            "source_refs": refs,
            "recorded_at_ms": recorded_at_ms,
        }
        payload["record_hash"] = _digest(payload)
        _validate_contract(payload, "noesis-market-universe-membership-v1.json")
        if prior:
            old = json.loads(prior[2])
            if self._semantic_payload(old) == self._semantic_payload(payload):
                return old
        encoded = _canonical(payload)
        self.conn.execute(
            """INSERT INTO market_instrument_universe_revisions
               (namespace,universe_id,security_id,revision,revision_id,owner,
                valid_from_ms,valid_to_ms,payload_json,record_hash,recorded_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                namespace,
                universe_id,
                security_id,
                revision,
                payload["revision_id"],
                owner,
                valid_from_ms,
                valid_to_ms,
                encoded,
                payload["record_hash"],
                recorded_at_ms,
            ],
        )
        return payload

    def list_universe_members(
        self,
        namespace: str,
        universe_id: str,
        *,
        as_of_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> list[dict[str, Any]]:
        self._authorize(namespace, principal_id, scopes, write=False)
        universe_id = _text(universe_id, "universe_id", limit=200)
        as_of_ms = _millis(as_of_ms, "as_of_ms")
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if publicly_available_by_ms is not None:
            publicly_available_by_ms = _millis(
                publicly_available_by_ms, "publicly_available_by_ms"
            )
            if publicly_available_by_ms > acquired_by_ms:
                raise MarketInstrumentError(
                    "invalid_request",
                    "publicly_available_by_ms cannot be later than acquired_by_ms",
                )
        if as_of_ms > acquired_by_ms:
            raise MarketInstrumentError(
                "invalid_request", "as_of_ms cannot be later than acquired_by_ms"
            )
        rows = self.conn.execute(
            """WITH latest AS (
                   SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY namespace,universe_id,security_id
                       ORDER BY revision DESC
                   ) AS row_number
                   FROM market_instrument_universe_revisions
                   WHERE namespace=? AND universe_id=? AND recorded_at_ms<=?
               )
               SELECT security_id,payload_json FROM latest
               WHERE row_number=1 AND valid_from_ms<=?
                 AND (valid_to_ms IS NULL OR valid_to_ms>?)
               ORDER BY security_id""",
            [namespace, universe_id, acquired_by_ms, as_of_ms, as_of_ms],
        ).fetchall()
        results = []
        for security_id, encoded in rows:
            membership = json.loads(encoded)
            if membership["membership_status"] != "included":
                continue
            if (
                membership.get("owner") not in (None, principal_id)
                and "operator" not in scopes
            ):
                continue
            try:
                self._check_entitlements(
                    namespace,
                    membership["source_refs"],
                    principal_id,
                    scopes,
                    write=False,
                )
            except MarketInstrumentError:
                continue
            if publicly_available_by_ms is not None and not all(
                ref.get("public_at_ms") is not None
                and int(ref["public_at_ms"]) <= publicly_available_by_ms
                for ref in membership["source_refs"]
            ):
                continue
            security = self._read_object_asof(
                namespace,
                "security",
                security_id,
                acquired_by_ms,
                publicly_available_by_ms=publicly_available_by_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            if security is not None:
                results.append({"membership": membership, "security": security})
        return results

    def put_issuer_relationship(
        self,
        namespace: str,
        *,
        issuer_id: str,
        related_issuer_id: str,
        relationship_type: str,
        effective_date: str | None,
        source_refs: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        issuer_relationship_id: str | None = None,
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Record mergers, successor links, spin-offs or renames without deleting issuers."""

        self._authorize(namespace, principal_id, scopes, write=True)
        issuer_id = _text(issuer_id, "issuer_id", limit=200)
        related_issuer_id = _text(related_issuer_id, "related_issuer_id", limit=200)
        for identity in (issuer_id, related_issuer_id):
            if (
                self._read_object_asof(
                    namespace,
                    "issuer",
                    identity,
                    _millis(self.now(), "now_ms"),
                    principal_id=principal_id,
                    scopes=scopes,
                )
                is None
            ):
                raise MarketInstrumentError(
                    "issuer_not_found", "related issuer identity is unavailable"
                )
        issuer_relationship_id = issuer_relationship_id or self.new_id("relationship")
        owner = self._owner_for_write(owner, principal_id, scopes)
        body = {
            "issuer_id": issuer_id,
            "related_issuer_id": related_issuer_id,
            "relationship_type": relationship_type,
            "effective_date": effective_date,
            "source_refs": [dict(item) for item in source_refs],
        }
        return self._put_object(
            "issuer-relationship",
            issuer_relationship_id,
            namespace,
            body,
            principal_id=principal_id,
            scopes=scopes,
            owner=owner,
            expected_revision=expected_revision,
        )

    def list_issuer_relationships(
        self,
        namespace: str,
        issuer_id: str,
        *,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> list[dict[str, Any]]:
        self._authorize(namespace, principal_id, scopes, write=False)
        issuer_id = _text(issuer_id, "issuer_id", limit=200)
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if publicly_available_by_ms is not None:
            publicly_available_by_ms = _millis(
                publicly_available_by_ms, "publicly_available_by_ms"
            )
            if publicly_available_by_ms > acquired_by_ms:
                raise MarketInstrumentError(
                    "invalid_request",
                    "publicly_available_by_ms cannot be later than acquired_by_ms",
                )
        rows = self.conn.execute(
            """WITH latest AS (
                   SELECT object_id,revision,payload_json,
                          ROW_NUMBER() OVER (PARTITION BY object_id ORDER BY revision DESC) AS row_number
                   FROM market_instrument_object_revisions
                   WHERE namespace=? AND object_type='issuer-relationship' AND recorded_at_ms<=?
               )
               SELECT payload_json FROM latest WHERE row_number=1""",
            [namespace, acquired_by_ms],
        ).fetchall()
        results = []
        for (encoded,) in rows:
            relation = json.loads(encoded)
            if issuer_id not in {relation["issuer_id"], relation["related_issuer_id"]}:
                continue
            if self._payload_readable(
                namespace,
                relation,
                principal_id,
                scopes,
                publicly_available_by_ms=publicly_available_by_ms,
            ):
                results.append(relation)
        return sorted(
            results,
            key=lambda item: (
                item.get("effective_date") or "",
                item["issuer_relationship_id"],
            ),
        )

    def get_instrument(
        self,
        namespace: str,
        object_type: str,
        object_id: str,
        *,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Read an issuer, security, listing or issuer relationship revision."""

        self._authorize(namespace, principal_id, scopes, write=False)
        if object_type not in _CONTRACTS:
            raise MarketInstrumentError(
                "invalid_request", "unsupported market object type"
            )
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if publicly_available_by_ms is not None:
            publicly_available_by_ms = _millis(
                publicly_available_by_ms, "publicly_available_by_ms"
            )
            if publicly_available_by_ms > acquired_by_ms:
                raise MarketInstrumentError(
                    "invalid_request",
                    "publicly_available_by_ms cannot be later than acquired_by_ms",
                )
        payload = self._read_object_asof(
            namespace,
            object_type,
            _text(object_id, "object_id", limit=200),
            acquired_by_ms,
            publicly_available_by_ms=(
                None
                if publicly_available_by_ms is None
                else _millis(publicly_available_by_ms, "publicly_available_by_ms")
            ),
            principal_id=principal_id,
            scopes=scopes,
        )
        if payload is None:
            raise MarketInstrumentError("not_found", "market identity is unavailable")
        return payload
