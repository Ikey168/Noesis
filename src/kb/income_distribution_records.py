"""Income, poverty and inequality series records for the ``society.income`` provider (IP02, #2592).

One record contract, ``noesis-income-distribution-record-v2``, covers the record types the provider owns:

* ``release`` - one acquired publication (a PIP release version, an EU-SILC dataset as of its ``LAST UPDATE`` or an
  OECD IDD response) with its release clock and basis, retrieval clock, PPP round, dataflow version and digests;
* ``series`` - keyed by source, indicator concept and native measure, welfare concept (income, consumption or PIP's
  ``mixed`` regional aggregate), equivalence scale, poverty line with its PPP base year, place, coverage, survey,
  income definition and methodology. Reference years are the periods of the series' observations; the survey year
  and the income reference year are kept per value;
* ``definition`` - the definition as the source states it (threshold, equivalence scale, income definition,
  methodology notes and cited methodology documents), revisioned: a changed definition is a new revision;
* ``vintage`` - one release of one series with release and retrieval clocks, the definition revision, PPP round and
  dataflow version in force and the changes against the previous vintage. A PPP revision that restates past values is
  a new vintage, never an overwrite; a removal by the source is a vintage too, never a deletion;
* ``observation`` - period, value exactly as published, status, estimation type as the source labels it, survey and
  income reference years, flags verbatim;
* ``comparability_note`` - source-stated breaks and recorded differences between series, cited.

Numeric values also live in the Economics series storage (``economic_vintages``, ``dataset_observations`` through
:func:`src.domains.economic.model.register_series`, domain ``society``).

**Minimisation decision (IP01).** The sources publish aggregate statistics only; no record carries data about a
person or a household. :func:`check_item` refuses, at write time, any key that would carry personal or microdata
fields and any key that would carry a derived, filled, blended or forecast value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.income_distribution_sources import (
    CONCEPTS,
    EQUIVALENCE_SCALES,
    ESTIMATION_TYPES,
    EXCLUSIONS,
    LINE_BASES,
    PROVIDERS,
    STATUSES,
    WELFARE_CONCEPTS,
)

CONTRACT = "noesis-income-distribution-record-v2"
ANSWER_CONTRACT = "noesis-income-distribution-answer-v1"
READ_SCOPE = "knowledge:income:read"
WRITE_SCOPE = "knowledge:income:write"
REVIEW_SCOPE = "knowledge:income:review"
DEFAULT_NAMESPACE = "global"
ECONOMIC_DOMAIN = "society"
PROVIDER_ID = "society.income"
RECORD_TYPES = ("release", "series", "definition", "vintage", "observation", "comparability_note")
CHANGE_KINDS = ("new_series", "new_period", "revised_value", "ppp_revision", "definition_change", "removed_by_source")
SCHEMA_FILE = "contracts/schemas/jsonschema/noesis-income-distribution-record-v2.json"
# Keys that would carry data about a person or a household (microdata); refused anywhere in a record.
PERSONAL_DATA_FIELDS = frozenset({
    "person_id", "person_name", "full_name", "first_name", "last_name", "name_of_person", "household_id",
    "hh_id", "respondent_id", "date_of_birth", "birth_date", "address", "home_address", "email", "phone",
    "national_id", "tax_id", "income_of_person", "household_income", "person_weight", "household_weight",
    "microdata", "record_weight",
})
# Keys that would carry a derived, estimated, filled, blended or forecast number.
FORBIDDEN_KEYS = frozenset({
    "nowcast", "nowcasted", "forecast", "projection", "predicted", "imputed", "imputed_value", "gap_filled",
    "filled_value", "blended", "blended_value", "combined_value", "average_value", "harmonised_value",
    "reharmonised_value", "derived_value", "own_poverty_line",
})
MINIMISATION = {
    "decision": "aggregate statistics only; no person-level or household-level field is stored",
    "stored": "published aggregates per place, year and series key, with flags, notes and citations",
    "redacted": "nothing (no personal field is ever acquired)",
    "excluded": "EU-SILC user database (UDB) and any other microdata, PIP survey microdata, LIS microdata",
    "retention": "release revisions are kept for provenance; nothing is personal, so no erasure workflow applies",
    "who_may_query": f"principals holding {READ_SCOPE} and access to the namespace",
    "enforced_by": "income_distribution_records.check_item at write time",
}


class IncomeError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def to_ms(value: Any) -> int | None:
    """ISO date/time (or epoch ms) to epoch milliseconds; naive values are UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("Z", "+00:00")
    if len(text) == 10:
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise IncomeError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def _paths(value: Any, names: frozenset[str], path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}"
            if str(key).casefold() in names:
                found.append(here)
            found += _paths(item, names, here)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found += _paths(item, names, f"{path}[{index}]")
    return found


def personal_data_paths(value: Any) -> list[str]:
    return _paths(value, PERSONAL_DATA_FIELDS)


def forbidden_paths(value: Any) -> list[str]:
    return _paths(value, FORBIDDEN_KEYS)


def check_item(item: Mapping[str, Any]) -> None:
    """Validate one published series item before anything is written (IP02 rules and the IP01 minimisation)."""
    leaked = personal_data_paths(dict(item))
    if leaked:
        raise IncomeError("personal_data", "records never carry person- or household-level data", fields=leaked)
    derived = forbidden_paths(dict(item))
    if derived:
        raise IncomeError("derived_value", "published records carry no derived, filled, blended or forecast value",
                          fields=derived)
    for key in ("provider", "native_key", "indicator", "definition", "area", "welfare_concept", "equivalence_scale"):
        if not item.get(key):
            raise IncomeError("invalid_record", f"an income series states its {key}")
    if item["provider"] not in PROVIDERS:
        raise IncomeError("invalid_record", f"provider is one of {PROVIDERS}")
    if dict(item["indicator"]).get("concept") not in CONCEPTS:
        raise IncomeError("invalid_record", f"indicator concept is one of {CONCEPTS}")
    if item["welfare_concept"] not in WELFARE_CONCEPTS:
        raise IncomeError("invalid_record", f"welfare concept is one of {WELFARE_CONCEPTS}")
    if item["welfare_concept"] == "mixed" and dict(item.get("coverage") or {}).get("reporting_level") != "regional":
        raise IncomeError("invalid_record", "only a source's regional aggregate states a mixed welfare concept")
    if item["equivalence_scale"] not in EQUIVALENCE_SCALES:
        raise IncomeError("invalid_record", f"equivalence scale is one of {EQUIVALENCE_SCALES}")
    line = item.get("poverty_line")
    if line is not None:
        if dict(line).get("basis") not in LINE_BASES:
            raise IncomeError("invalid_record", f"a poverty line basis is one of {LINE_BASES}")
        if line["basis"] == "absolute-ppp" and not line.get("ppp_base_year"):
            raise IncomeError("invalid_record", "an absolute PPP poverty line states its PPP base year")
    elif dict(item["indicator"]).get("concept") in {"poverty_headcount", "poverty_gap"}:
        raise IncomeError("invalid_record", "a poverty measure states the line the source published it at")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise IncomeError("invalid_record", "each observation states its status")
        if obs.get("status") != "reported" and obs.get("value") is not None:
            raise IncomeError("invalid_record", "a confidential or unpublished observation carries no value")
        if obs.get("estimation_type") not in ESTIMATION_TYPES:
            raise IncomeError("invalid_record", f"estimation type is one of {ESTIMATION_TYPES}")
        if obs["period"] in periods:
            raise IncomeError("invalid_record", "a series states a reference year twice")
        periods.add(obs["period"])


def comparability_basis(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recorded differences between two series keys (facts of the keys; nothing is re-harmonised)."""
    differences = []
    for field in ("provider", "welfare_concept", "equivalence_scale", "poverty_line", "ppp_base_year", "survey",
                  "income_definition", "methodology", "coverage", "measure"):
        if left.get(field) != right.get(field):
            differences.append({"field": field, "left": left.get(field), "right": right.get(field)})
    return differences


def schema_definitions(root: Path | None = None) -> dict[str, dict[str, Any]]:
    base = root or Path(__file__).resolve().parents[2]
    return {CONTRACT: json.loads((base / SCHEMA_FILE).read_text())}


def register_schemas(conn: Any, *, principal_id: str, scopes: Iterable[str], root: Path | None = None) -> list[dict]:
    """Register the record schema in the existing schema registry (idempotent per version)."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "2.0.0",
            "content": content, "owner": PROVIDER_ID, "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/society"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"income-schema:{name}:2.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


__all__ = [
    "ANSWER_CONTRACT",
    "CHANGE_KINDS",
    "CONTRACT",
    "ECONOMIC_DOMAIN",
    "EXCLUSIONS",
    "MINIMISATION",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "IncomeError",
    "authorize",
    "canonical",
    "check_item",
    "comparability_basis",
    "digest",
    "forbidden_paths",
    "iso",
    "personal_data_paths",
    "register_schemas",
    "table_exists",
    "to_ms",
]
