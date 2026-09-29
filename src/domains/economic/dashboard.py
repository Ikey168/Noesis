"""Revision-aware macro dashboards composed from existing economic stores."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from src.domains.economic.releases import EconomicReleaseStore
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.entitlements import MarketEntitlementStore
from src.domains.market.prices import MarketPriceStore

MAX_SERIES = 50
MAX_CALENDAR_ROWS = 500
MAX_UNIVERSE_ROWS = 100


class EconomicDashboardError(ValueError):
    """Typed macro-dashboard failure safe for adapter boundaries."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def _text(value: Any, name: str, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise EconomicDashboardError("invalid_request", f"{name} must be bounded text")
    return value.strip()


def _ms(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise EconomicDashboardError("invalid_request", f"{name} must be nonnegative milliseconds")
    return value


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise EconomicDashboardError("invalid_request", "consensus value must be finite") from exc
    if not result.is_finite():
        raise EconomicDashboardError("invalid_request", "consensus value must be finite")
    return result


def _error(exc: Exception) -> dict[str, Any]:
    return {
        "code": str(getattr(exc, "code", "economic_unavailable")),
        "message": str(getattr(exc, "message", str(exc)))[:300],
    }


class EconomicDashboardStore:
    """Build a bounded macro view without introducing a second vintage store."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now
        self.releases = EconomicReleaseStore(conn, initialize=initialize, now=now)
        self.instruments = MarketInstrumentStore(conn, initialize=initialize, now=now)
        self.prices = MarketPriceStore(conn, initialize=initialize, now=now)

    @staticmethod
    def _series_selectors(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise EconomicDashboardError("invalid_request", "series must be a list")
        if not 1 <= len(value) <= MAX_SERIES:
            raise EconomicDashboardError("bound_exceeded", "series must contain 1 to 50 entries")
        result: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in value:
            if isinstance(item, str):
                selector = {"series_id": _text(item, "series_id", limit=200)}
            elif isinstance(item, Mapping):
                selector = {
                    key: item[key]
                    for key in (
                        "series_id",
                        "vintage_id",
                        "provider_release_id",
                        "methodology_id",
                        "source_revision_id",
                    )
                    if key in item
                }
                selector["series_id"] = _text(selector.get("series_id"), "series_id", limit=200)
            else:
                raise EconomicDashboardError("invalid_request", "each series must be text or an object")
            if selector["series_id"] in seen:
                raise EconomicDashboardError("invalid_request", "series must be unique")
            seen.add(selector["series_id"])
            result.append(selector)
        return result

    def _calendar(
        self,
        namespace: str,
        series_ids: Sequence[str],
        *,
        acquired_by_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in series_ids)
        rows = self.conn.execute(
            """SELECT v.series_id,v.as_of,v.vintage_id,v.release_at_ms,v.retrieved_at_ms,
                      v.revision_of,v.source_url,v.source_document_id,v.release_at_basis,
                      v.retrieved_at_basis,v.vintage_basis,v.release_time_status
               FROM economic_vintages v
               WHERE v.domain='economics' AND v.series_id IN ("""
            + placeholders
            + ") AND v.retrieved_at_ms<=? ORDER BY v.release_at_ms,v.series_id LIMIT ?",
            [*series_ids, acquired_by_ms, MAX_CALENDAR_ROWS],
        ).fetchall()
        keys = (
            "series_id",
            "provider_vintage_ms",
            "vintage_id",
            "release_at_ms",
            "retrieved_at_ms",
            "revision_of",
            "source_url",
            "source_document_id",
            "release_at_basis",
            "retrieved_at_basis",
            "vintage_basis",
            "release_time_status",
        )
        result = [dict(zip(keys, row)) for row in rows]
        if "operator" not in scopes:
            # Calendar metadata can be displayed without source payloads, but a
            # source-document locator still needs the caller's existing access.
            for item in result:
                document_id = item.get("source_document_id")
                if document_id and f"document:{document_id}:read" not in scopes:
                    item["source_document_id"] = None
                    item["source_access"] = "restricted"
        return result

    def _consensus(
        self,
        snapshot: Mapping[str, Any],
        consensus: Any,
        *,
        namespace: str,
        public_cutoff_ms: int,
        acquired_by_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> list[dict[str, Any]]:
        by_series = {
            str(item.get("series_id")): item
            for item in snapshot.get("series", [])
            if isinstance(item, Mapping)
        }
        if consensus is None:
            return [
                {
                    "series_id": series_id,
                    "status": "unavailable",
                    "reason": "pre_release_consensus_not_supplied",
                }
                for series_id in sorted(by_series)
            ]
        if not isinstance(consensus, Sequence) or isinstance(consensus, (str, bytes)):
            raise EconomicDashboardError("invalid_request", "consensus must be a list")
        supplied = {str(item.get("series_id")): item for item in consensus if isinstance(item, Mapping)}
        result = []
        for series_id, series in by_series.items():
            item = supplied.get(series_id)
            if item is None:
                result.append({"series_id": series_id, "status": "unavailable", "reason": "consensus_missing"})
                continue
            value = _decimal(item.get("value"))
            public_at = _ms(item.get("public_at_ms"), "consensus.public_at_ms")
            retrieved_at = _ms(item.get("retrieved_at_ms"), "consensus.retrieved_at_ms")
            if public_at > public_cutoff_ms or retrieved_at > acquired_by_ms:
                result.append({"series_id": series_id, "status": "unavailable", "reason": "consensus_not_asof"})
                continue
            refs = item.get("source_refs")
            if not isinstance(refs, list) or not refs:
                result.append({"series_id": series_id, "status": "unavailable", "reason": "consensus_source_required"})
                continue
            if not all(isinstance(ref, Mapping) and ref.get("entitlement_id") for ref in refs):
                result.append({"series_id": series_id, "status": "unavailable", "reason": "consensus_entitlement_required"})
                continue
            try:
                MarketEntitlementStore(self.conn, initialize=True, now=self.now).authorize_sources(
                    namespace,
                    [dict(ref) for ref in refs],
                    operation="display",
                    principal_id=principal_id,
                    scopes=scopes,
                    now_ms=public_at,
                )
            except Exception as exc:
                result.append({"series_id": series_id, "status": "unavailable", "reason": str(getattr(exc, "code", "consensus_entitlement_unavailable"))})
                continue
            observations = [item for item in series.get("observations", []) if item.get("value") is not None]
            actual = observations[-1]["value"] if observations else None
            result.append(
                {
                    "series_id": series_id,
                    "status": "available" if actual is not None else "unavailable",
                    "reason": None if actual is not None else "actual_release_value_missing",
                    "consensus": str(value),
                    "actual": actual,
                    "surprise": None if actual is None else str(_decimal(actual) - value),
                    "public_at_ms": public_at,
                    "retrieved_at_ms": retrieved_at,
                    "source_refs": refs,
                }
            )
        return result

    def _breadth(
        self,
        namespace: str,
        universe_id: str | None,
        *,
        as_of_ms: int,
        start_ms: int,
        end_ms: int,
        acquired_by_ms: int,
        public_cutoff_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        if universe_id is None:
            return {"status": "not_requested", "universe_id": None, "limitations": ["No dated market universe was supplied"]}
        members = self.instruments.list_universe_members(
            namespace,
            _text(universe_id, "universe_id", limit=200),
            as_of_ms=as_of_ms,
            acquired_by_ms=acquired_by_ms,
            publicly_available_by_ms=public_cutoff_ms,
            principal_id=principal_id,
            scopes=scopes,
        )
        observations: list[dict[str, Any]] = []
        for member in members[:MAX_UNIVERSE_ROWS]:
            listings = self.instruments.list_listings_for_security(
                namespace,
                member["security"]["security_id"],
                as_of_ms=as_of_ms,
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
                limit=1,
            )
            if not listings:
                observations.append({"security_id": member["security"]["security_id"], "status": "missing", "reason": "listing_missing"})
                continue
            listing_id = listings[0]["listing"]["listing_id"]
            try:
                page = self.prices.get_bars(
                    namespace,
                    listing_id,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    acquired_by_ms=acquired_by_ms,
                    publicly_available_by_ms=public_cutoff_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                    limit=500,
                    include_page=True,
                )
                bars = page["items"]
                if len(bars) < 2 or bars[0].get("close") in (None, 0) or bars[-1].get("close") is None:
                    observations.append({"security_id": member["security"]["security_id"], "listing_id": listing_id, "status": "missing", "reason": "insufficient_history"})
                    continue
                change = Decimal(str(bars[-1]["close"])) / Decimal(str(bars[0]["close"])) - Decimal(1)
                observations.append({"security_id": member["security"]["security_id"], "listing_id": listing_id, "status": "available", "return": str(change), "input_revision_ids": [item["revision_id"] for item in bars if item.get("revision_id")]})
            except Exception as exc:
                observations.append({"security_id": member["security"]["security_id"], "listing_id": listing_id, "status": "error", "error": _error(exc)})
        available = [item for item in observations if item["status"] == "available"]
        positive = [item for item in available if Decimal(item["return"]) > 0]
        return {
            "status": "available" if available else "empty",
            "universe_id": universe_id,
            "as_of_ms": as_of_ms,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "calendar": "retained bar sessions; no cross-venue calendar coercion",
            "frequency": "retained daily bars",
            "aggregation": "equal-weight count of securities with accessible two-point returns",
            "breadth": None if not available else str(Decimal(len(positive)) / Decimal(len(available))),
            "positive_count": len(positive),
            "available_count": len(available),
            "member_count": len(members),
            "members": observations,
        }

    def build(
        self,
        namespace: str,
        *,
        release_id: str,
        request_key: str,
        series: Sequence[Any],
        release_cutoff_ms: int,
        acquired_cutoff_ms: int,
        initial_release_cutoff_ms: int | None = None,
        consensus: Sequence[Mapping[str, Any]] | None = None,
        universe_id: str | None = None,
        universe_as_of_ms: int | None = None,
        breadth_start_ms: int | None = None,
        breadth_end_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        namespace = _text(namespace, "namespace", limit=100)
        release_id = _text(release_id, "release_id", limit=200)
        request_key = _text(request_key, "request_key", limit=200)
        release_cutoff_ms = _ms(release_cutoff_ms, "release_cutoff_ms")
        acquired_cutoff_ms = _ms(acquired_cutoff_ms, "acquired_cutoff_ms")
        if release_cutoff_ms > acquired_cutoff_ms:
            raise EconomicDashboardError("invalid_request", "release cutoff cannot exceed acquisition cutoff")
        selectors = self._series_selectors(series)
        latest = self.releases.create_snapshot(
            namespace,
            f"{request_key}:latest",
            release_id,
            release_cutoff_ms=release_cutoff_ms,
            acquired_cutoff_ms=acquired_cutoff_ms,
            series=selectors,
            principal_id=principal_id,
            scopes=scopes,
        )
        initial_cutoff = release_cutoff_ms if initial_release_cutoff_ms is None else _ms(initial_release_cutoff_ms, "initial_release_cutoff_ms")
        if initial_cutoff > acquired_cutoff_ms:
            raise EconomicDashboardError("invalid_request", "initial release cutoff cannot exceed acquisition cutoff")
        # A same-cutoff dashboard has one immutable snapshot.  Reusing it is
        # important because the release store keys artifacts by their content
        # hash, while the dashboard deliberately exposes distinct initial and
        # latest roles.
        if initial_cutoff == release_cutoff_ms:
            initial = latest
        else:
            initial = self.releases.create_snapshot(
                namespace,
                f"{request_key}:initial",
                release_id,
                release_cutoff_ms=initial_cutoff,
                acquired_cutoff_ms=acquired_cutoff_ms,
                series=selectors,
                principal_id=principal_id,
                scopes=scopes,
            )
        comparison = None
        if initial["snapshot_id"] != latest["snapshot_id"]:
            comparison = self.releases.compare(
                namespace,
                f"{request_key}:comparison",
                initial["snapshot_id"],
                latest["snapshot_id"],
                principal_id=principal_id,
                scopes=scopes,
                assumptions=["Dashboard compares retained vintages; it does not infer causal surprises."],
            )
        calendar = self._calendar(
            namespace,
            [item["series_id"] for item in selectors],
            acquired_by_ms=acquired_cutoff_ms,
            principal_id=principal_id,
            scopes=scopes,
        )
        breadth = self._breadth(
            namespace,
            universe_id,
            as_of_ms=(release_cutoff_ms if universe_as_of_ms is None else _ms(universe_as_of_ms, "universe_as_of_ms")),
            start_ms=(
                max(0, release_cutoff_ms - 365 * 86_400_000)
                if breadth_start_ms is None
                else _ms(breadth_start_ms, "breadth_start_ms")
            ),
            end_ms=(release_cutoff_ms if breadth_end_ms is None else _ms(breadth_end_ms, "breadth_end_ms")),
            acquired_by_ms=acquired_cutoff_ms,
            public_cutoff_ms=release_cutoff_ms,
            principal_id=principal_id,
            scopes=scopes,
        ) if universe_id else {"status": "not_requested", "universe_id": None}
        return {
            "contract": "noesis-economic-market-dashboard-v1",
            "namespace": namespace,
            "release_id": release_id,
            "snapshots": {
                "initial": initial,
                "latest": latest,
                "comparison": comparison,
            },
            "release_calendar": calendar,
            "consensus_surprises": self._consensus(
                latest,
                consensus,
                namespace=namespace,
                public_cutoff_ms=release_cutoff_ms,
                acquired_by_ms=acquired_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
            ),
            "breadth": breadth,
            "cutoffs": {
                "release_cutoff_ms": release_cutoff_ms,
                "acquired_cutoff_ms": acquired_cutoff_ms,
            },
            "limitations": [
                "Release calendars expose retained provider timestamps and their basis; intraday availability is not inferred.",
                "Surprises are unavailable unless a timestamped, source-referenced pre-release consensus is supplied.",
                "Breadth uses accessible retained bars and equal-weight counts; no FX or cross-venue calendar coercion is performed.",
            ],
        }


__all__ = ["EconomicDashboardError", "EconomicDashboardStore"]
