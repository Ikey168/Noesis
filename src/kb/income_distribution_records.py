"""Income, poverty and inequality record definitions for the Society bundle's ``society.income`` provider (#2583, IP02).

Records follow contract ``noesis-income-distribution-record-v1``. Numeric values and their vintages live in the
existing Economics series storage (``economic_indicators``, ``economic_series_map``, ``economic_vintages`` and
``dataset_observations`` through :func:`src.domains.economic.model.register_series`); the income-specific metadata
lives in :mod:`src.kb.income_distribution_store`. The record types are:

* **release** - one acquired publication (a PIP response pinned to a PIP release version, or an SDMX-CSV response)
  with its release clock (PIP release date, Eurostat ``LAST UPDATE``, a declared release or the retrieval time) and
  basis label, digests, the release or dataflow version and the evidence origin;
* **series** - keyed by source, indicator, welfare concept (income or consumption), equivalence scale, poverty line
  and its PPP base year, reference-year basis (survey year, PIP's reference-year "lineup" or income year), survey,
  coverage, area and unit (:func:`series_key`). Two sources, lines, PPP rounds or welfare concepts are never one
  series;
* **definition** - the indicator's definition as the source states it (income definition, equivalence scale,
  poverty line, methodology version, notes), revisioned;
* **vintage** - one release of a series with release and retrieval clocks, ``revision_of`` and the recorded changes
  (new, revised and removed periods, a PPP revision, a definition or version change, a withdrawal). A PPP revision
  that restates past values is a new vintage, never an overwrite; a withdrawal by the source is a vintage too;
* **observation** - the reference year, the value as published (exact text), status, flags and the per-value
  attributes (welfare type, survey year, income reference year, survey acronym, PIP's estimation label);
* **comparability note** - typed, cited and reviewable (the labour/demographics pattern).

Records are immutable revisions: nothing is updated in place or deleted. The IP01 minimisation decision is enforced
at write time: a record carrying a person- or household-level key is refused (``personal_data_refused``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from src.ingestion.income_distribution_sources import (
    AGGREGATE_WELFARE,
    CONCEPTS,
    EQUIVALENCE_SCALES,
    FORMATS,
    REFERENCE_YEAR_BASES,
    SOURCE_FEATURES,
    STATUSES,
    WELFARE_CONCEPTS,
    personal_keys,
)

CONTRACT = "noesis-income-distribution-record-v1"
ANSWER_CONTRACT = "noesis-income-distribution-answer-v1"
COMPARABILITY_CONTRACT = "noesis-income-comparability-v1"
READ_SCOPE = "knowledge:income:read"
WRITE_SCOPE = "knowledge:income:write"
REVIEW_SCOPE = "knowledge:income:review"
DEFAULT_NAMESPACE = "global"
BUNDLE = "society"
PROVIDER = "society.income"
SERIES_DOMAIN = "economics"  # the Economics series storage holds the values
RECORD_TYPES = ("release", "series", "definition", "vintage", "observation", "comparability_note")
LINK_FEATURES = ("demographics-links", "labour-links")
FEATURES = tuple(SOURCE_FEATURES.values()) + LINK_FEATURES
RELATIONS = (
    "different_welfare_concept",
    "different_equivalence_scale",
    "different_poverty_line",
    "different_ppp_base_year",
    "different_reference_year_basis",
    "different_survey",
    "different_methodology",
    "ppp_revision",
    "break_in_series",
    "source_note",
    "same_underlying_survey",
    "not_comparable",
)
SINGLE_SIDED = ("break_in_series", "source_note", "ppp_revision")
ACTIVE_STATES = ("source-stated", "proposed", "accepted")
CHANGE_KINDS = ("new_period", "revised_value", "removed_period", "ppp_revision", "definition_change", "withdrawn")
# Keys that would carry a derived, estimated, blended or forecast number of ours.
FORBIDDEN_KEYS = frozenset({
    "nowcast", "nowcasted", "forecast", "forecast_value", "projection", "predicted", "imputed", "imputed_value",
    "gap_filled", "filled_value", "blended", "blended_value", "combined_value", "average_value", "harmonised_value",
    "rebased_value", "derived_value", "own_poverty_line", "converted_value", "ppp_converted_value",
})


class IncomeError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise IncomeError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise IncomeError("unauthorized", f"{required} is required for this part of the answer")


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry a derived, filled, blended or forecast number."""
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def minimised(value: Any) -> Any:
    """Enforce the IP01 minimisation decision on an output: refuse any person- or household-level key."""
    found = personal_keys(value)
    if found:
        raise IncomeError("personal_data_refused", "an answer carries no person- or household-level field",
                          paths=found[:10])
    return value


def release_ms(published_on: str | None, published_at: str | None, retrieved_ms: int) -> int:
    """The release clock (UTC epoch ms): the stated instant, a date's midnight, else the retrieval time."""
    if published_at:
        stamp = datetime.fromisoformat(str(published_at))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return int(stamp.timestamp() * 1000)
    if published_on:
        return int(datetime.combine(date.fromisoformat(published_on), datetime.min.time(),
                                    tzinfo=UTC).timestamp() * 1000)
    return int(retrieved_ms)


def iso_from_ms(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()


def check_item(item: Mapping[str, Any]) -> None:
    """Write-time validation of one series item: minimisation, no derived values, complete key, known vocabularies."""
    if personal_keys(dict(item)):
        raise IncomeError("personal_data_refused", "income records carry no person- or household-level field")
    if forbidden_keys(dict(item)):
        raise IncomeError("invalid_release", "published records carry no derived, filled or forecast value")
    for key in ("provider", "native_key", "indicator", "definition", "unit", "area", "frequency", "survey",
                "coverage", "equivalence_scale", "reference_year_basis"):
        if not item.get(key):
            raise IncomeError("invalid_release", f"an income series states its {key}")
    if item["provider"] not in {f["provider"] for f in FORMATS.values()}:
        raise IncomeError("invalid_release", "unknown income provider")
    if dict(item["indicator"]).get("concept") not in CONCEPTS:
        raise IncomeError("invalid_release", "unknown indicator concept")
    if item.get("welfare_concept") not in WELFARE_CONCEPTS + (AGGREGATE_WELFARE,):
        raise IncomeError("invalid_release", "an income series states its welfare concept (income or consumption)")
    if dict(item["equivalence_scale"]).get("code") not in EQUIVALENCE_SCALES:
        raise IncomeError("invalid_release", "an income series states its equivalence scale")
    if item["reference_year_basis"] not in REFERENCE_YEAR_BASES:
        raise IncomeError("invalid_release", "an income series states its reference-year basis")
    line = item.get("poverty_line")
    if line is not None and dict(line).get("kind") == "absolute" and not dict(line).get("ppp_base_year"):
        raise IncomeError("invalid_release", "an absolute poverty line states its PPP base year")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise IncomeError("invalid_release", "each observation states its status")
        if obs.get("status") != "reported" and obs.get("value") is not None:
            raise IncomeError("invalid_release", "a confidential or unpublished observation carries no value")
        if obs["period"] in periods:
            raise IncomeError("invalid_release", "a series states a reference year twice")
        periods.add(obs["period"])
        if item["provider"] == "pip" and not dict(obs.get("attributes") or {}).get("welfare_type"):
            raise IncomeError("invalid_release", "a PIP value states its welfare type")


def series_key(item: Mapping[str, Any]) -> list[Any]:
    """Source, indicator, welfare concept, equivalence scale, poverty line with its PPP base year, PPP base year,
    reference-year basis, survey, coverage, methodology, area and unit: PIP, EU-SILC and OECD never share a key, nor
    do two lines, PPP rounds or welfare concepts."""
    indicator = dict(item["indicator"])
    return [
        item["provider"],
        str(indicator.get("code") or ""),
        indicator["concept"],
        indicator["measure"],
        item["welfare_concept"],
        dict(item["equivalence_scale"])["code"],
        None if item.get("poverty_line") is None else canonical(dict(item["poverty_line"])),
        item.get("ppp_base_year"),
        item["reference_year_basis"],
        item["survey"],
        item["coverage"],
        item.get("methodology_version"),
        item["area"]["scheme"],
        str(item["area"]["code"]),
        str(item["native_key"]),
        dict(item["unit"]).get("code") or dict(item["unit"]).get("label"),
    ]


def definition_key(item: Mapping[str, Any]) -> str:
    definition = dict(item["definition"])
    return "inc-definition:" + digest([
        item["provider"], definition.get("indicator_code"), definition["concept"], definition["measure"],
        definition["welfare_concept"], canonical(definition.get("equivalence_scale")),
        canonical(definition.get("poverty_line")), definition.get("reference_year_basis"),
        definition.get("methodology_version"),
    ])[:24]


def _selected_features(conn: Any) -> list[str] | None:
    """The Society bundle's selected features, or ``None`` when the bundle is not composition-managed."""
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return None
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return None
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    if not any(p.get("id") == BUNDLE for p in plan.get("packs") or []):
        return []
    return list((plan.get("features") or {}).get(BUNDLE) or [])


def feature_state(conn: Any, feature: str) -> str:
    """``selected`` / ``not_selected`` under composition management, ``unmanaged`` otherwise."""
    if feature not in FEATURES:
        raise IncomeError("invalid_feature", f"feature is one of {FEATURES}")
    selected = _selected_features(conn)
    if selected is None:
        return "unmanaged"
    return "selected" if feature in selected else "not_selected"


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether an optional Society feature is selected in the active plan (False when not composition-managed)."""
    return feature_state(conn, feature) == "selected"


__all__ = [
    "ANSWER_CONTRACT",
    "BUNDLE",
    "CHANGE_KINDS",
    "COMPARABILITY_CONTRACT",
    "CONTRACT",
    "FEATURES",
    "PROVIDER",
    "READ_SCOPE",
    "RELATIONS",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "IncomeError",
    "authorize",
    "check_item",
    "definition_key",
    "feature_enabled",
    "feature_state",
    "forbidden_keys",
    "minimised",
    "series_key",
]
