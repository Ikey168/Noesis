"""Health-system capacity indicators, definitions, definition revisions and observations (#2215, HS02).

The ``clinical.health-capacity`` records (contract ``noesis-health-capacity-record-v1``) are *views composed over
the existing surveillance series and vintage storage* (:mod:`src.kb.surveillance`): a capacity series is a
surveillance series whose condition scheme is ``health-capacity`` and whose condition code is the capacity domain
(``beds``, ``workforce``, ``expenditure``). No second series, vintage or definition store exists.

* **capacity-indicator** - one series: source (provider) and the source's own indicator or measure code, unit,
  interval, place (code system and code), the definition text as published and the definition-revision history.
* **indicator-definition** / **definition-revision** - the publisher's definition (for example the GHO indicator
  metadata, the OECD definitions and country notes, the Eurostat ESMS and SHA edition) declared with valid-from
  dates; each revision keeps the release it was declared with and that release's retrieval time. A change of
  edition is a **definition break** in the series (the surveillance store's case-definition break), never applied to
  earlier values.
* **capacity-observation** - one published value: place, reference period, the source's flags verbatim, the release
  vintage it belongs to and the as-of time (release clock and retrieval time). A missing value stays unknown.

Nothing here ranks places, scores performance or quality, harmonises definitions, adjusts or re-estimates values or
computes a combined metric.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from src.ingestion.health_capacity_sources import BOUNDARY
from src.ingestion.surveillance_sources import CAPACITY_DOMAINS
from src.kb.surveillance import (
    READ_SCOPE,
    SurveillanceError,
    SurveillanceStore,
    _day,
    authorize,
    release_ms,
    table_exists,
)

CONTRACT = "noesis-health-capacity-record-v1"
SCHEME = "health-capacity"
DOMAINS = CAPACITY_DOMAINS
# The issue names it ``health_capacity``; composition feature ids are kebab-case, so it is declared as below.
FEATURE = "health-capacity"
RECORD_TYPES = ("capacity-indicator", "indicator-definition", "definition-revision", "capacity-observation")
NEVER_SENTENCE = BOUNDARY
AGGREGATE_SYSTEMS = ("who-region", "who-global", "eurostat-aggregate", "oecd-aggregate", "ecdc-aggregate")


class HealthCapacityError(SurveillanceError):
    """A capacity request that cannot be answered as asked (same codes as the surveillance owner)."""


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Clinical Evidence bundle's optional ``health_capacity`` feature is selected in the active plan."""
    del namespace  # composition selection is deployment-wide
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return False
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle='clinical-evidence'"
        ).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return FEATURE in ((plan.get("features") or {}).get("clinical-evidence") or [])


def as_of_ms(as_of: str | None) -> int | None:
    """The end of an as-of day (UTC) in epoch milliseconds: a vintage released that day counts."""
    return None if as_of is None else release_ms(_day(as_of), None) + 86_400_000 - 1


def _break_view(item: dict[str, Any]) -> dict[str, Any]:
    kind = {"case-definition": "definition-break", "publisher-flag": "publisher-flagged-break",
            "geography": "geography-break"}.get(item["kind"], item["kind"])
    return {**item, "capacity_kind": kind}


class HealthCapacityStore:
    """Capacity views over :class:`~src.kb.surveillance.SurveillanceStore` (composed, not copied)."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.series_store = SurveillanceStore(conn, initialize=initialize, now=now)
        self.now = self.series_store.now

    def ready(self) -> bool:
        return self.series_store.ready()

    # ------------------------------------------------------------------ indicators and definitions

    def _capacity_series(self, namespace: str, series_id: str) -> dict[str, Any]:
        series = self.series_store.series(namespace, series_id)
        if series["condition"].get("scheme") != SCHEME:
            raise HealthCapacityError("not_found", "series is not a health-capacity indicator")
        return series

    def definition_history(self, namespace: str, series_id: str) -> dict[str, Any]:
        """The publisher's definition revisions for a capacity series (editions by valid-from, corrections apart)."""
        series = self._capacity_series(namespace, series_id)
        if not series["definition_key"]:
            return {"contract": CONTRACT, "record_type": "indicator-definition", "namespace": namespace,
                    "series_id": series_id, "definition_key": None, "revisions": [],
                    "status": "no_definition_declared",
                    "note": "the source declares no definition for this indicator; none is inferred"}
        history = self.series_store.definition_history(namespace, series["definition_key"])
        revisions = []
        for revision in history["revisions"]:
            source = revision["source_revision"]
            revisions.append({
                "record_type": "definition-revision",
                "revision_id": revision["revision_id"],
                "version": revision["version"],
                "valid_from": revision["valid_from"],
                "valid_to": revision["valid_to"],
                "text": revision["content"].get("text"),
                "locator": revision["content"].get("locator"),
                "declared_on": revision["declared_on"],
                "retrieved_at_ms": source["retrieved_at_ms"],
                "predecessor_version": revision["predecessor_version"],
                "correction_of": revision["correction_of"],
                "current_for_edition": revision["current_for_edition"],
                "source_revision": source,
            })
        return {
            "contract": CONTRACT,
            "record_type": "indicator-definition",
            "namespace": namespace,
            "series_id": series_id,
            "definition_key": series["definition_key"],
            "provider": history["provider"],
            "revisions": revisions,
            "status": "declared",
            "note": "definitions as published; each edition applies from its valid-from date and a change of "
            "edition is a marked break, never a restatement of earlier values",
        }

    def indicator(self, namespace: str, series_id: str, *, scopes: Iterable[str] | None = None) -> dict[str, Any]:
        if scopes is not None:
            authorize(namespace, set(scopes), READ_SCOPE)
        series = self._capacity_series(namespace, series_id)
        history = self.definition_history(namespace, series_id)
        editions = [r for r in history["revisions"] if r["current_for_edition"]]
        return {
            "contract": CONTRACT,
            "record_type": "capacity-indicator",
            "namespace": namespace,
            "series_id": series_id,
            "domain": series["condition"]["code"],
            "provider": series["provider"],
            "source_code": series["indicator_code"],
            "indicator": series["indicator"],
            "place": series["geography"],
            "aggregate": series["geography"]["system"] in AGGREGATE_SYSTEMS,
            "unit": series["unit"],
            "interval": series["interval"],
            "kind": series["kind"],
            "dimensions": series["dimensions"],
            "citations": series["citations"],
            "definition": editions[-1] if editions else None,
            "definition_history": history["revisions"],
            "breaks": [_break_view(b) for b in series["breaks"]],
            "vintage_count": series["vintage_count"],
            "current_vintage_id": series["current_vintage_id"],
        }

    def indicators(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        domain: str | None = None,
        provider: str | None = None,
        geography_system: str | None = None,
        geography_code: str | None = None,
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if domain is not None and domain not in DOMAINS:
            raise HealthCapacityError("invalid_domain", f"domain is one of {DOMAINS}")
        if not table_exists(self.conn, "surveillance_series"):
            return []
        found = self.series_store.find_series(
            namespace, provider=provider, condition_scheme=SCHEME,
            condition_codes=None if domain is None else [domain],
            geography_system=geography_system, geography_code=geography_code,
        )
        return [self.indicator(namespace, s["series_id"]) for s in found]

    # ------------------------------------------------------------------ observations

    def observations(
        self,
        namespace: str,
        series_id: str,
        *,
        scopes: Iterable[str],
        as_of: str | None = None,
        vintage_id: str | None = None,
    ) -> dict[str, Any]:
        """The values of the vintage in force at ``as_of`` (released at or before the end of that day), or of a
        named vintage, each with place, reference period, flags verbatim, the vintage and the as-of time."""
        authorize(namespace, set(scopes), READ_SCOPE)
        series = self._capacity_series(namespace, series_id)
        vintages = self.series_store.vintage_rows(namespace, series_id)
        if vintage_id is not None:
            vintage = next((v for v in vintages if v["vintage_id"] == vintage_id), None)
            if vintage is None:
                raise HealthCapacityError("not_found", "vintage does not belong to this series")
            reason = None
        else:
            vintage, reason = self.series_store.select_vintage(namespace, series_id, as_of_ms=as_of_ms(as_of))
        base = {
            "contract": CONTRACT,
            "series_id": series_id,
            "request": {"as_of": None if as_of is None else _day(as_of), "vintage_id": vintage_id},
            "later_vintages": 0 if vintage is None else sum(
                1 for v in vintages if v["release_at_ms"] > vintage["release_at_ms"]),
        }
        if vintage is None:
            return {**base, "status": "unavailable", "reason": reason, "values": [],
                    "note": "no vintage of this indicator was released by the as-of time"}
        source = self.series_store.source_revision(namespace, vintage["release_id"])
        versions: dict[str, dict[str, Any]] = {}
        values = []
        for value in self.series_store.value_rows(namespace, vintage["vintage_id"]):
            revision_id = value["case_definition_revision_id"]
            if revision_id and revision_id not in versions:
                versions[revision_id] = self.series_store.definition_revision(namespace, revision_id)
            revision = versions.get(revision_id)
            values.append({
                "record_type": "capacity-observation",
                "place": {k: series["geography"][k] for k in ("system", "code", "label")},
                "reference_period": value["reference_period"],
                "value_text": value["value_text"],
                "value": value["value"],
                "status": "published" if value["value"] is not None else "unknown",
                "flags": value["flags"],
                "attributes": value.get("attributes") or {},
                "unit": series["unit"]["label"],
                "definition": None if revision is None else {
                    "revision_id": revision["revision_id"], "version": revision["version"],
                    "valid_from": revision["valid_from"], "valid_to": revision["valid_to"]},
                "vintage_id": vintage["vintage_id"],
                "as_of_time": {"release_at_ms": vintage["release_at_ms"],
                               "release_basis": vintage["release_basis"],
                               "retrieved_at_ms": vintage["retrieved_at_ms"]},
            })
        return {
            **base,
            "status": "available",
            "vintage": {k: vintage[k] for k in ("vintage_id", "sequence", "release_at_ms", "release_basis",
                                                "retrieved_at_ms", "native_revision", "revision_of")},
            "source_revision": source,
            "values": values,
            "note": "values as published under the definition in force on their reference period; flags verbatim; "
            "a missing value is unknown and never estimated",
        }


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.health_capacity_sources import BOUNDED_COVERAGE, CAPACITY_CONTRACTS

    store = SurveillanceStore(conn, initialize=False)
    ready = store.ready()
    providers = {}
    for provider, contract in CAPACITY_CONTRACTS.items():
        series = 0
        origins: list[str] = []
        if ready:
            series = int(conn.execute(
                "SELECT count(*) FROM surveillance_series WHERE provider=? AND condition_scheme=?",
                [provider, SCHEME]).fetchone()[0])
            origins = sorted({r[0] for r in conn.execute(
                "SELECT DISTINCT r.evidence_origin FROM surveillance_releases r JOIN surveillance_release_members m "
                "ON m.namespace=r.namespace AND m.release_id=r.release_id JOIN surveillance_series s ON "
                "s.namespace=m.namespace AND s.series_id=m.series_id WHERE r.provider=? AND s.condition_scheme=?",
                [provider, SCHEME]).fetchall()})
        providers[provider] = {"access_decision": contract["access_decision"], "reuses": contract["reuses"],
                               "series": series, "evidence_origins": origins}
    return {
        "feature": FEATURE,
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "providers": providers,
        "bounded_coverage": BOUNDED_COVERAGE,
        "boundary": NEVER_SENTENCE,
        "note": "fixture and live evidence are reported per release (evidence_origin); no provider is live until "
        "a dated run verifies it (#2481)",
    }


__all__ = [
    "AGGREGATE_SYSTEMS", "CONTRACT", "DOMAINS", "FEATURE", "HealthCapacityError", "HealthCapacityStore",
    "NEVER_SENTENCE", "RECORD_TYPES", "SCHEME", "as_of_ms", "feature_enabled", "readiness",
]
