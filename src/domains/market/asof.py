"""Immutable, cross-domain point-in-time manifests for market research.

Source values stay in their authoritative stores. A manifest records exact
revision IDs and hashes selected from those stores under one effective-time,
public-availability, and local-acquisition policy.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any

READ_SCOPE = "market:asof:read"
WRITE_SCOPE = "market:asof:write"
CONTRACT = "noesis-market-asof-snapshot-v1"

_DDL = """
CREATE TABLE IF NOT EXISTS market_asof_snapshots (
 snapshot_id TEXT PRIMARY KEY,
 namespace TEXT NOT NULL,
 owner TEXT NOT NULL,
 request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 input_hash TEXT NOT NULL,
 manifest_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, owner, request_key)
);
"""

_SELECTION_KEYS = frozenset(
    {
        "listings",
        "prices",
        "actions",
        "financial_facts",
        "economic_snapshots",
        "documents",
    }
)
_MAX_ITEMS = 100
_TRANSFORMATION_KINDS = frozenset({"market_adjustment", "quantitative"})
_PRICE_INTERVALS = frozenset({"1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mo"})


class MarketAsOfError(ValueError):
    """Typed point-in-time manifest error safe for API surfaces."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


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
        raise MarketAsOfError("invalid_request", "request must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketAsOfError(
            "invalid_request", f"{field} must be bounded nonempty text"
        )
    return value.strip()


def _millis(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketAsOfError(
            "invalid_request", f"{field} must be nonnegative epoch milliseconds"
        )
    return value


def _source_clock(record: Mapping[str, Any], field: str) -> int | None:
    if record.get(field) is not None:
        return int(record[field])
    refs = record.get("source_refs") or []
    values = [int(ref[field]) for ref in refs if ref.get(field) is not None]
    return max(values) if values else None


def _source_summary(record: Mapping[str, Any]) -> dict[str, Any]:
    refs = list(record.get("source_refs") or [])
    return {
        "source_ref_ids": sorted(
            {str(ref["source_ref_id"]) for ref in refs if ref.get("source_ref_id")}
        ),
        "entitlement_ids": sorted(
            {str(ref["entitlement_id"]) for ref in refs if ref.get("entitlement_id")}
        ),
        "source_entitlements": [
            {
                "source_ref_id": str(ref["source_ref_id"]),
                "provider": str(ref["provider"]),
                "license_id": str(ref["license_id"]),
                "entitlement_id": str(ref["entitlement_id"]),
                "retrieved_at_ms": int(ref["retrieved_at_ms"]),
            }
            for ref in refs
        ],
    }


def _input_ref(
    kind: str,
    object_id: str,
    record: Mapping[str, Any],
    *,
    revision_id: str | None = None,
    record_hash: str | None = None,
) -> dict[str, Any]:
    retrieved_at_ms = _source_clock(record, "retrieved_at_ms")
    if retrieved_at_ms is None:
        retrieved_at_ms = record.get("recorded_at_ms")
    return {
        "kind": kind,
        "object_id": object_id,
        "revision_id": revision_id or record.get("revision_id"),
        "record_hash": record_hash or record.get("record_hash"),
        "public_at_ms": _source_clock(record, "public_at_ms"),
        "retrieved_at_ms": retrieved_at_ms,
        **_source_summary(record),
    }


def _sort_specs(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(values, key=_canonical)


def _document_public_time(payload: Mapping[str, Any]) -> tuple[int | None, str]:
    metadata = dict(payload.get("metadata") or {})
    value = payload.get("public_at_ms")
    basis = "payload.public_at_ms"
    if value is None:
        value = metadata.get("public_at_ms")
        basis = "metadata.public_at_ms"
    if value is not None:
        try:
            return _millis(value, "document.public_at_ms"), basis
        except MarketAsOfError:
            return None, "invalid_public_time"
    value = payload.get("published_at_ms")
    if value is None:
        value = metadata.get("published_at_ms")
    if value is not None:
        try:
            return _millis(value, "document.published_at_ms"), "published_at_ms"
        except MarketAsOfError:
            return None, "invalid_public_time"
    return None, "unavailable"


class MarketAsOfSnapshotStore:
    """Compose exact market, economic, and document revisions under two clocks."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(
        namespace: str,
        principal_id: str,
        scopes: set[str],
        *,
        write: bool,
    ) -> None:
        _text(namespace, "namespace", 100)
        _text(principal_id, "principal_id", 200)
        if "operator" in scopes:
            return
        required = WRITE_SCOPE if write else READ_SCOPE
        namespace_scopes = (
            {f"namespace:{namespace}:write"}
            if write
            else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
        )
        if required not in scopes or not (namespace_scopes & scopes):
            raise MarketAsOfError(
                "unauthorized", "current market snapshot and namespace access is required"
            )

    @staticmethod
    def _normalize_request(
        namespace: str,
        request_key: str,
        *,
        effective_at_ms: int,
        publicly_available_by_ms: int,
        acquired_by_ms: int,
        selection: Mapping[str, Any],
        transformations: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        availability_policy: str,
        gap_policy: str,
    ) -> dict[str, Any]:
        _text(namespace, "namespace", 100)
        _text(request_key, "request_key", 200)
        effective_at_ms = _millis(effective_at_ms, "effective_at_ms")
        publicly_available_by_ms = _millis(
            publicly_available_by_ms, "publicly_available_by_ms"
        )
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if publicly_available_by_ms > acquired_by_ms:
            raise MarketAsOfError(
                "invalid_request", "public cutoff cannot be later than acquisition cutoff"
            )
        if availability_policy != "public_and_acquired":
            raise MarketAsOfError(
                "invalid_request", "availability_policy must be public_and_acquired"
            )
        if gap_policy not in {"fail", "record"}:
            raise MarketAsOfError("invalid_request", "gap_policy must be fail or record")
        if not isinstance(selection, Mapping) or set(selection) - _SELECTION_KEYS:
            raise MarketAsOfError(
                "invalid_selection", "selection contains an unsupported input kind"
            )
        selected = {key: selection.get(key, []) for key in sorted(_SELECTION_KEYS)}
        if not any(selected.values()):
            raise MarketAsOfError("invalid_selection", "at least one input is required")
        for kind, values in selected.items():
            if not isinstance(values, list) or len(values) > _MAX_ITEMS:
                raise MarketAsOfError(
                    "invalid_selection", f"{kind} must be a bounded list"
                )

        listing_ids = [
            _text(item, "listing_id", 200) for item in selected["listings"]
        ]
        selected["listings"] = sorted(set(listing_ids))
        normalized_prices = []
        for item in selected["prices"]:
            if not isinstance(item, Mapping) or set(item) != {
                "listing_id",
                "interval",
                "start_ms",
                "end_ms",
            }:
                raise MarketAsOfError(
                    "invalid_selection", "price selectors require listing, interval, and bounds"
                )
            price = {
                "listing_id": _text(item["listing_id"], "listing_id", 200),
                "interval": _text(item["interval"], "interval", 20),
                "start_ms": _millis(item["start_ms"], "start_ms"),
                "end_ms": _millis(item["end_ms"], "end_ms"),
            }
            if price["interval"] not in _PRICE_INTERVALS:
                raise MarketAsOfError("invalid_selection", "price interval is unsupported")
            if price["start_ms"] >= price["end_ms"]:
                raise MarketAsOfError("invalid_selection", "price range must be nonempty")
            if price["end_ms"] > effective_at_ms + 1:
                raise MarketAsOfError(
                    "invalid_selection", "price range cannot extend beyond effective_at_ms"
                )
            normalized_prices.append(price)
        selected["prices"] = _sort_specs(normalized_prices)

        normalized_actions = []
        for item in selected["actions"]:
            if not isinstance(item, Mapping) or set(item) != {"security_id"}:
                raise MarketAsOfError(
                    "invalid_selection", "action selectors require security_id"
                )
            normalized_actions.append(
                {"security_id": _text(item["security_id"], "security_id", 200)}
            )
        selected["actions"] = _sort_specs(normalized_actions)

        normalized_facts = []
        fact_fields = {"issuer_id", "taxonomy", "concept", "filing_form"}
        for item in selected["financial_facts"]:
            if not isinstance(item, Mapping) or set(item) - fact_fields or "issuer_id" not in item:
                raise MarketAsOfError(
                    "invalid_selection", "financial-fact selectors require issuer_id and optional filters"
                )
            fact = {"issuer_id": _text(item["issuer_id"], "issuer_id", 200)}
            for field in sorted(fact_fields - {"issuer_id"}):
                if item.get(field) is not None:
                    fact[field] = _text(item[field], field, 200)
            normalized_facts.append(fact)
        selected["financial_facts"] = _sort_specs(normalized_facts)

        normalized_economic = []
        for item in selected["economic_snapshots"]:
            if not isinstance(item, Mapping) or set(item) != {"namespace", "snapshot_id"}:
                raise MarketAsOfError(
                    "invalid_selection", "economic snapshots require namespace and snapshot_id"
                )
            normalized_economic.append(
                {
                    "namespace": _text(item["namespace"], "economic namespace", 100),
                    "snapshot_id": _text(item["snapshot_id"], "snapshot_id", 200),
                }
            )
        selected["economic_snapshots"] = _sort_specs(normalized_economic)

        normalized_documents = []
        for item in selected["documents"]:
            if not isinstance(item, Mapping) or set(item) != {"document_id"}:
                raise MarketAsOfError(
                    "invalid_selection", "document selectors require document_id"
                )
            normalized_documents.append(
                {"document_id": _text(item["document_id"], "document_id", 300)}
            )
        selected["documents"] = _sort_specs(normalized_documents)
        if (
            not isinstance(transformations, (list, tuple))
            or len(transformations) > _MAX_ITEMS
        ):
            raise MarketAsOfError(
                "invalid_selection", "transformations must be a bounded list"
            )
        normalized_transformations = []
        for item in transformations:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"kind", "calculation_id"}
                or item.get("kind") not in _TRANSFORMATION_KINDS
            ):
                raise MarketAsOfError(
                    "invalid_selection",
                    "transformations require a supported kind and calculation_id",
                )
            normalized_transformations.append(
                {
                    "kind": item["kind"],
                    "calculation_id": _text(item["calculation_id"], "calculation_id", 200),
                }
            )
        return {
            "request_key": request_key,
            "effective_at_ms": effective_at_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "acquired_by_ms": acquired_by_ms,
            "availability_policy": availability_policy,
            "gap_policy": gap_policy,
            "selection": selected,
            "transformation_selectors": _sort_specs(normalized_transformations),
        }

    def create_snapshot(
        self,
        namespace: str,
        request_key: str,
        *,
        effective_at_ms: int,
        publicly_available_by_ms: int,
        acquired_by_ms: int,
        selection: Mapping[str, Any],
        transformations: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
        principal_id: str,
        scopes: set[str],
        availability_policy: str = "public_and_acquired",
        gap_policy: str = "record",
    ) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, write=True)
        request = self._normalize_request(
            namespace,
            request_key,
            effective_at_ms=effective_at_ms,
            publicly_available_by_ms=publicly_available_by_ms,
            acquired_by_ms=acquired_by_ms,
            selection=selection,
            transformations=transformations,
            availability_policy=availability_policy,
            gap_policy=gap_policy,
        )
        request_hash = _digest([namespace, principal_id, request])
        prior = self.conn.execute(
            "SELECT request_hash,manifest_json FROM market_asof_snapshots "
            "WHERE namespace=? AND owner=? AND request_key=?",
            [namespace, principal_id, request_key],
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise MarketAsOfError(
                    "idempotency_conflict", "request_key already identifies another snapshot"
                )
            snapshot = json.loads(prior[1])
            self._verify_inputs(snapshot, principal_id=principal_id, scopes=scopes)
            return {**snapshot, "idempotent": True}

        captured = self._capture(
            namespace,
            request,
            principal_id=principal_id,
            scopes=scopes,
        )
        if captured["coverage"]["status"] != "complete" and gap_policy == "fail":
            raise MarketAsOfError(
                "historical_inputs_incomplete",
                "requested historical inputs are incomplete",
                gaps=captured["gaps"],
                coverage=captured["coverage"],
            )
        core = {
            "contract": CONTRACT,
            "namespace": namespace,
            "owner": principal_id,
            **request,
            "inputs": captured["inputs"],
            "transformations": captured["transformations"],
            "gaps": captured["gaps"],
            "coverage": captured["coverage"],
            "input_hash": _digest(captured),
        }
        snapshot_id = "market-asof:" + _digest([namespace, principal_id, request_key])[:32]
        manifest = {
            **core,
            "snapshot_id": snapshot_id,
            "created_at_ms": _millis(self.now(), "created_at_ms"),
            "request_hash": request_hash,
        }
        manifest["manifest_hash"] = _digest(manifest)
        self.conn.execute(
            "INSERT INTO market_asof_snapshots VALUES (?,?,?,?,?,?,?,?)",
            [
                snapshot_id,
                namespace,
                principal_id,
                request_key,
                request_hash,
                manifest["input_hash"],
                _canonical(manifest),
                manifest["created_at_ms"],
            ],
        )
        return manifest

    def inspect_snapshot(
        self,
        namespace: str,
        snapshot_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, write=False)
        row = self.conn.execute(
            "SELECT owner,manifest_json FROM market_asof_snapshots "
            "WHERE namespace=? AND snapshot_id=?",
            [namespace, _text(snapshot_id, "snapshot_id", 200)],
        ).fetchone()
        if not row:
            raise MarketAsOfError("snapshot_unavailable", "snapshot is unavailable")
        if row[0] != principal_id and "operator" not in scopes:
            raise MarketAsOfError("unauthorized", "snapshot ownership is required")
        snapshot = json.loads(row[1])
        self._verify_inputs(snapshot, principal_id=principal_id, scopes=scopes)
        return snapshot

    def export_snapshot(
        self,
        namespace: str,
        snapshot_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        external: bool = False,
    ) -> dict[str, Any]:
        """Export a provenance-only manifest after current provider-rights checks."""

        if type(external) is not bool:
            raise MarketAsOfError("invalid_request", "external must be boolean")
        snapshot = self.inspect_snapshot(
            namespace,
            snapshot_id,
            principal_id=principal_id,
            scopes=scopes,
        )
        source_refs = {
            ref["source_ref_id"]: ref
            for item in snapshot.get("inputs", [])
            for ref in item.get("source_entitlements", [])
        }
        decisions = []
        if source_refs:
            from src.domains.market.entitlements import (
                MarketEntitlementError,
                authorize_market_sources,
            )

            try:
                decisions = authorize_market_sources(
                    self.conn,
                    namespace,
                    list(source_refs.values()),
                    operation="export",
                    principal_id=principal_id,
                    scopes=scopes,
                    external=external,
                    now_ms=_millis(self.now(), "now_ms"),
                )
            except MarketEntitlementError as exc:
                raise MarketAsOfError(exc.code, exc.message, **exc.details) from exc
        return {
            "contract": "noesis-market-asof-export-v1",
            "namespace": namespace,
            "snapshot_id": snapshot["snapshot_id"],
            "manifest_hash": snapshot["manifest_hash"],
            "external": external,
            "source_payloads_included": False,
            "rights_decisions": decisions,
            "snapshot": snapshot,
        }

    def _verify_inputs(self, snapshot, *, principal_id, scopes) -> None:
        request = {
            "request_key": snapshot["request_key"],
            "effective_at_ms": snapshot["effective_at_ms"],
            "publicly_available_by_ms": snapshot["publicly_available_by_ms"],
            "acquired_by_ms": snapshot["acquired_by_ms"],
            "availability_policy": snapshot["availability_policy"],
            "gap_policy": snapshot["gap_policy"],
            "selection": snapshot["selection"],
            "transformation_selectors": snapshot.get("transformation_selectors", []),
        }
        if _digest([snapshot["namespace"], snapshot["owner"], request]) != snapshot.get(
            "request_hash"
        ):
            raise MarketAsOfError("snapshot_integrity_error", "snapshot request hash is invalid")
        expected_manifest_hash = _digest(
            {key: value for key, value in snapshot.items() if key != "manifest_hash"}
        )
        if expected_manifest_hash != snapshot.get("manifest_hash"):
            raise MarketAsOfError("snapshot_integrity_error", "snapshot manifest hash is invalid")
        captured = self._capture(
            snapshot["namespace"], request, principal_id=principal_id, scopes=scopes
        )
        if _digest(captured) != snapshot["input_hash"]:
            raise MarketAsOfError(
                "snapshot_inputs_changed",
                "one or more pinned source inputs are no longer readable as captured",
            )

    def _capture(self, namespace, request, *, principal_id, scopes):
        from src.domains.economic.releases import EconomicReleaseError, EconomicReleaseStore
        from src.domains.market.actions import (
            MAX_ACTION_ROWS,
            MarketActionError,
            MarketCorporateActionStore,
        )
        from src.domains.market.financial_facts import (
            MarketFinancialFactError,
            MarketFinancialFactStore,
        )
        from src.domains.market.instruments import MarketInstrumentError, MarketInstrumentStore
        from src.domains.market.prices import (
            MAX_HISTORY_ROWS,
            MarketPriceError,
            MarketPriceStore,
        )
        from src.ingestion.revisions import DocumentRevisionStore, RevisionError

        effective_at = request["effective_at_ms"]
        public_cutoff = request["publicly_available_by_ms"]
        acquired_cutoff = request["acquired_by_ms"]
        inputs: list[dict[str, Any]] = []
        transformations: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        known = set()
        identities: dict[str, dict[str, Any]] = {}

        def gap(kind: str, object_id: str, reason: str) -> None:
            gaps.append({"kind": kind, "object_id": object_id, "reason": reason})

        def append_input(item: dict[str, Any]) -> None:
            identity = (item["kind"], item.get("revision_id") or item["object_id"])
            if identity in known:
                return
            known.add(identity)
            inputs.append(item)
            if item.get("revision_id"):
                known.add(("reference", item["revision_id"]))

        instrument_store = MarketInstrumentStore(self.conn)

        def capture_identity(object_type: str, object_id: str) -> dict[str, Any] | None:
            key = f"{object_type}:{object_id}"
            if key in identities:
                return identities[key]
            try:
                payload = instrument_store.get_instrument(
                    namespace,
                    object_type,
                    object_id,
                    acquired_by_ms=acquired_cutoff,
                    publicly_available_by_ms=public_cutoff,
                    principal_id=principal_id,
                    scopes=scopes,
                )
            except MarketInstrumentError as exc:
                if exc.code == "not_found":
                    gap(object_type, object_id, "revision_unavailable_at_cutoffs")
                    identities[key] = None
                    return None
                raise
            source = _input_ref(
                object_type,
                object_id,
                payload,
                record_hash=payload.get("record_hash"),
            )
            source["valid_from_ms"] = payload.get("valid_from_ms")
            source["valid_to_ms"] = payload.get("valid_to_ms")
            source["status"] = payload.get("status")
            append_input(source)
            identities[key] = payload
            if object_type == "issuer":
                return payload
            if object_type == "security":
                capture_identity("issuer", payload["issuer_id"])
            elif object_type == "listing":
                capture_identity("security", payload["security_id"])
            return payload

        for listing_id in request["selection"]["listings"]:
            listing = capture_identity("listing", listing_id)
            if listing is None:
                continue
            valid_to = listing.get("valid_to_ms")
            if (
                int(listing["valid_from_ms"]) > effective_at
                or (valid_to is not None and int(valid_to) <= effective_at)
                or listing.get("status") != "active"
            ):
                gap("listing", listing_id, "not_active_at_effective_time")

        price_store = MarketPriceStore(self.conn)
        for selector in request["selection"]["prices"]:
            listing_id = selector["listing_id"]
            listing = capture_identity("listing", listing_id)
            try:
                bars = price_store.get_bars(
                    namespace,
                    listing_id,
                    start_ms=selector["start_ms"],
                    end_ms=selector["end_ms"],
                    acquired_by_ms=acquired_cutoff,
                    publicly_available_by_ms=public_cutoff,
                    principal_id=principal_id,
                    scopes=scopes,
                )
            except MarketPriceError as exc:
                if exc.code == "not_found":
                    bars = []
                else:
                    raise
            bars = [bar for bar in bars if bar.get("interval") == selector["interval"]]
            if not bars:
                gap("prices", listing_id, "no_history_retained_at_cutoffs")
            elif len(bars) == MAX_HISTORY_ROWS:
                gap("prices", listing_id, "history_query_row_limit_reached")
            for bar in bars:
                append_input(
                    {
                        **_input_ref(
                            "price_bar",
                            bar["bar_id"],
                            bar,
                            record_hash=bar.get("record_hash"),
                        ),
                        "listing_id": listing_id,
                        "interval": bar["interval"],
                        "bar_start_ms": bar["bar_start_ms"],
                    }
                )
            coverage = "observed_not_proven_complete" if bars else "missing"
            inputs.append(
                {
                    "kind": "price_range",
                    "object_id": f"{listing_id}:{selector['interval']}:{selector['start_ms']}:{selector['end_ms']}",
                    "revision_ids": [bar["revision_id"] for bar in bars],
                    "coverage": coverage,
                    "count": len(bars),
                    "requested_start_ms": selector["start_ms"],
                    "requested_end_ms": selector["end_ms"],
                }
            )

        action_store = MarketCorporateActionStore(self.conn)
        for selector in request["selection"]["actions"]:
            security_id = selector["security_id"]
            capture_identity("security", security_id)
            try:
                actions = action_store.get_actions(
                    namespace,
                    security_id,
                    acquired_by_ms=acquired_cutoff,
                    publicly_available_by_ms=public_cutoff,
                    principal_id=principal_id,
                    scopes=scopes,
                    require_complete=True,
                )
            except MarketActionError as exc:
                if exc.code == "action_history_unavailable":
                    gap("actions", security_id, "history_or_entitlement_unavailable")
                    actions = []
                else:
                    raise
            if not actions:
                gap("actions", security_id, "no_actions_retained_at_cutoffs")
            elif len(actions) == MAX_ACTION_ROWS:
                gap("actions", security_id, "action_query_row_limit_reached")
            for action in actions:
                append_input(
                    _input_ref(
                        "corporate_action",
                        action["action_id"],
                        action,
                        record_hash=action.get("record_hash"),
                    )
                )

        fact_store = MarketFinancialFactStore(self.conn)
        from src.domains.market.financial_facts import MAX_FINANCIAL_FACT_ROWS

        for selector in request["selection"]["financial_facts"]:
            issuer_id = selector["issuer_id"]
            capture_identity("issuer", issuer_id)
            filters = {
                key: selector[key]
                for key in ("taxonomy", "concept", "filing_form")
                if key in selector
            }
            try:
                facts = fact_store.get_facts(
                    namespace,
                    issuer_id,
                    acquired_by_ms=acquired_cutoff,
                    publicly_available_by_ms=public_cutoff,
                    principal_id=principal_id,
                    scopes=scopes,
                    require_complete=True,
                    **filters,
                )
            except MarketFinancialFactError as exc:
                if exc.code == "fact_history_unavailable":
                    gap("financial_facts", issuer_id, "history_or_entitlement_unavailable")
                    facts = []
                else:
                    raise
            if not facts:
                gap("financial_facts", issuer_id, "no_facts_retained_at_cutoffs")
            elif len(facts) == MAX_FINANCIAL_FACT_ROWS:
                gap("financial_facts", issuer_id, "fact_query_row_limit_reached")
            for fact in facts:
                append_input(
                    {
                        **_input_ref(
                            "financial_fact",
                            fact["fact_observation_id"],
                            fact,
                            record_hash=fact.get("record_hash"),
                        ),
                        "filing_accession": fact["filing_accession"],
                        "source_document_revision_id": fact.get(
                            "source_document_revision_id"
                        ),
                        "context_id": fact["context_id"],
                    }
                )

        economic_store = EconomicReleaseStore(self.conn)
        for selector in request["selection"]["economic_snapshots"]:
            try:
                economic = economic_store.inspect_snapshot(
                    selector["namespace"],
                    selector["snapshot_id"],
                    principal_id=principal_id,
                    scopes=scopes,
                    offset=0,
                    limit=50,
                )
            except EconomicReleaseError as exc:
                if exc.code == "unauthorized":
                    raise MarketAsOfError(
                        "unauthorized", "current economic snapshot access is required"
                    ) from exc
                if exc.code == "not_found":
                    raise MarketAsOfError(
                        "economic_input_unavailable",
                        "an economic release snapshot is unavailable under current access",
                    ) from exc
                raise
            if (
                economic["release_cutoff_ms"] > public_cutoff
                or economic["acquired_cutoff_ms"] > acquired_cutoff
            ):
                gap(
                    "economic_snapshot",
                    selector["snapshot_id"],
                    "snapshot_cutoffs_exceed_requested_cutoffs",
                )
            if economic.get("coverage") != "complete_for_retained_selection":
                gap("economic_snapshot", selector["snapshot_id"], "retained_history_incomplete")
            series_revisions = []
            acquired_times = []
            for series in economic["series"]:
                if series.get("retrieved_at_ms") is not None:
                    retrieved_at = int(series["retrieved_at_ms"])
                    acquired_times.append(retrieved_at)
                    if retrieved_at > acquired_cutoff:
                        gap(
                            "economic_series",
                            series["series_id"],
                            "not_acquired_by_cutoff",
                        )
                else:
                    gap(
                        "economic_series",
                        series["series_id"],
                        "local_acquisition_time_unavailable",
                    )
                series_revisions.append(
                    {
                        "series_id": series["series_id"],
                        "source_revision_id": series.get("source_revision_id"),
                        "vintage_id": series.get("vintage_id"),
                        "provider_vintage_ms": series.get("provider_vintage_ms"),
                        "release_at_ms": series.get("release_at_ms"),
                        "release_at_basis": series.get("release_at_basis"),
                        "retrieved_at_ms": series.get("retrieved_at_ms"),
                        "observation_ids": sorted(
                            observation["observation_id"]
                            for observation in series["observations"]
                        ),
                    }
                )
            append_input(
                {
                    "kind": "economic_snapshot",
                    "object_id": selector["snapshot_id"],
                    "revision_id": selector["snapshot_id"],
                    "record_hash": _digest(economic),
                    "public_at_ms": None,
                    "release_cutoff_ms": economic["release_cutoff_ms"],
                    "public_time_status": "series_release_time_basis_required",
                    "retrieved_at_ms": max(acquired_times) if acquired_times else None,
                    "artifact_created_at_ms": economic["created_at_ms"],
                    "namespace": selector["namespace"],
                    "series": series_revisions,
                    "input_observation_ids": sorted(
                        observation["observation_id"]
                        for series in economic["series"]
                        for observation in series["observations"]
                    ),
                }
            )
            for series in economic["series"]:
                if series["status"] != "available":
                    gap(
                        "economic_series",
                        series["series_id"],
                        series.get("unavailable_reason") or "unavailable",
                    )
                if series.get("vintage_id"):
                    known.add(("reference", series["vintage_id"]))
                for observation in series["observations"]:
                    known.add(("reference", observation["observation_id"]))

        revisions = DocumentRevisionStore(self.conn, initialize=False)
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        for selector in request["selection"]["documents"]:
            document_id = selector["document_id"]
            if "operator" not in scopes and f"document:{document_id}:read" not in scopes:
                raise MarketAsOfError(
                    "unauthorized", "current document read access is required"
                )
            if "document_revision_records" not in tables:
                gap("document", document_id, "revision_history_unavailable")
                continue
            try:
                history = revisions.history(document_id, include_retracted=True)
            except RevisionError as exc:
                if exc.code == "payload_reclaimed":
                    gap("document", document_id, "revision_payload_reclaimed")
                    continue
                raise
            if not history:
                gap("document", document_id, "revision_history_unavailable")
                continue
            acquired = [
                revision
                for revision in history
                if int(revision["observed_at_ms"]) <= acquired_cutoff
            ]
            valid = [
                revision
                for revision in acquired
                if (revision.get("valid_from_ms") is None or revision["valid_from_ms"] <= effective_at)
                and (revision.get("valid_to_ms") is None or revision["valid_to_ms"] > effective_at)
            ]
            published = []
            public_time_missing = False
            for revision in valid:
                public_at, _basis = _document_public_time(revision["payload"])
                if public_at is None:
                    public_time_missing = True
                elif public_at <= public_cutoff:
                    published.append((revision, public_at, _basis))
            if not published:
                reason = (
                    "not_acquired_by_cutoff"
                    if not acquired
                    else "document_not_valid_at_effective_time"
                    if not valid
                    else "public_time_unavailable"
                    if public_time_missing
                    else "not_public_by_cutoff"
                )
                gap("document", document_id, reason)
                continue
            revision, public_at, public_basis = max(
                published, key=lambda item: int(item[0]["revision"])
            )
            if revision["lifecycle"] != "active":
                gap("document", document_id, "document_deleted_or_retracted_at_cutoffs")
                continue
            append_input(
                {
                    "kind": "document",
                    "object_id": document_id,
                    "revision_id": revision["revision_id"],
                    "record_hash": revision["payload_hash"],
                    "content_hash": revision["content_hash"],
                    "public_at_ms": public_at,
                    "public_time_basis": public_basis,
                    "retrieved_at_ms": revision["observed_at_ms"],
                    "lifecycle": revision["lifecycle"],
                    "generation": revision["generation"],
                }
            )

        selected_ids = {
            item.get("revision_id")
            for item in inputs
            if item.get("revision_id") is not None
        }
        selected_ids.update(
            item.get("object_id") for item in inputs if item.get("object_id")
        )
        for item in inputs:
            selected_ids.update(item.get("input_observation_ids") or [])

        for selector in request["transformation_selectors"]:
            kind = selector["kind"]
            if kind == "market_adjustment":
                calculation = action_store.get_calculation(
                    namespace,
                    selector["calculation_id"],
                    principal_id=principal_id,
                    scopes=scopes,
                )
                if calculation is None:
                    gap("transformation", selector["calculation_id"], "calculation_unavailable")
                    continue
                if (
                    calculation["public_cutoff_ms"] > public_cutoff
                    or calculation["acquired_cutoff_ms"] > acquired_cutoff
                ):
                    gap("transformation", selector["calculation_id"], "calculation_cutoffs_exceed_snapshot")
                references = set(calculation["bar_revision_ids"]) | set(
                    calculation["action_revision_ids"]
                )
                missing = sorted(references - selected_ids)
                if missing:
                    gap(
                        "transformation",
                        selector["calculation_id"],
                        "calculation_inputs_not_selected",
                    )
                transformations.append(
                    {
                        "kind": kind,
                        "calculation_id": calculation["calculation_id"],
                        "record_hash": calculation["record_hash"],
                        "formula_version": calculation["formula_version"],
                        "input_revision_ids": sorted(references),
                        "missing_input_revision_ids": missing,
                    }
                )
            else:
                from src.kb.quantitative import QuantitativeStore

                calculation = QuantitativeStore(self.conn).replay_calculation(
                    namespace, selector["calculation_id"], scopes=scopes
                )
                missing = sorted(set(calculation["input_ids"]) - selected_ids)
                if not calculation["deterministic"]:
                    gap("transformation", selector["calculation_id"], "replay_hash_mismatch")
                if missing:
                    gap(
                        "transformation",
                        selector["calculation_id"],
                        "calculation_inputs_not_selected",
                    )
                transformations.append(
                    {
                        "kind": kind,
                        "namespace": namespace,
                        "calculation_id": selector["calculation_id"],
                        "calculation_hash": calculation["stored_hash"],
                        "replayed_hash": calculation["replayed_hash"],
                        "deterministic": calculation["deterministic"],
                        "formula_revision_id": calculation["formula_revision_id"],
                        "input_ids": sorted(calculation["input_ids"]),
                        "missing_input_ids": missing,
                    }
                )

        source_refs = {
            ref["source_ref_id"]: ref
            for item in inputs
            for ref in item.get("source_entitlements", [])
        }
        if source_refs:
            from src.domains.market.entitlements import (
                MarketEntitlementError,
                authorize_market_sources,
            )

            try:
                decisions = authorize_market_sources(
                    self.conn,
                    namespace,
                    list(source_refs.values()),
                    operation="read",
                    principal_id=principal_id,
                    scopes=scopes,
                    now_ms=_millis(self.now(), "now_ms"),
                )
            except MarketEntitlementError as exc:
                raise MarketAsOfError(exc.code, exc.message, **exc.details) from exc
            by_source_ref = {
                item["source_ref_id"]: item for item in decisions
            }
            for item in inputs:
                for source in item.get("source_entitlements", []):
                    decision = by_source_ref.get(source["source_ref_id"])
                    if decision:
                        source["policy_revision_id"] = decision[
                            "policy_revision_id"
                        ]
                        source["authorized_operation"] = "read"

        gaps.sort(key=_canonical)
        inputs.sort(key=lambda item: (item["kind"], item["object_id"], item.get("revision_id") or ""))
        transformations.sort(key=lambda item: (item["kind"], item["calculation_id"]))
        selected_by_kind: dict[str, int] = {}
        unproven_price_ranges = []
        for item in inputs:
            selected_by_kind[item["kind"]] = selected_by_kind.get(item["kind"], 0) + 1
            if item.get("kind") == "price_range" and item.get("coverage") != "complete":
                unproven_price_ranges.append(item["object_id"])
        return {
            "inputs": inputs,
            "transformations": transformations,
            "gaps": gaps,
            "coverage": {
                "status": "complete" if not gaps and not unproven_price_ranges else "partial",
                "selected_by_kind": dict(sorted(selected_by_kind.items())),
                "gap_count": len(gaps),
                "unproven_price_ranges": sorted(unproven_price_ranges),
                "availability_policy": request["availability_policy"],
            },
        }
