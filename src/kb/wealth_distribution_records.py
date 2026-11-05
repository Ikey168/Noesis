"""Wealth distribution series records for the Society bundle's ``society.wealth`` provider (#2882).

One record contract, ``noesis-wealth-distribution-record-v2`` (the Society record contract line is 2.x), covers the
record types the provider owns:

* ``release`` - one acquired publication (a WID API response dated by the declared WID update, an ECB DWA response,
  a Fed DFA archive member dated by the declared Z.1 release; else the labelled retrieval time) with its release and
  retrieval clocks, digests and evidence origin;
* ``series`` - keyed by source, dataset, indicator (wealth share, level, average, median or wealth-income ratio),
  wealth concept (net personal wealth, household net wealth, net worth, ...), wealth group, unit, population or unit
  of analysis, method, place and frequency, each with the code as published;
* ``definition`` - the definition and the method statement as the source states them, revisioned: a changed
  definition or method statement (a new WID DINA method, for example) is a new revision;
* ``vintage`` - one release of one series; a revision is a new vintage, never an overwrite; a series a complete later
  release no longer states gets a ``removed_by_source`` vintage, never a deletion;
* ``observation`` - period, value exactly as published (shares as fractions of 1 or percent as the source publishes
  them, never rescaled), cell status (``reported``, ``confidential``, ``not_published``), the publication status the
  source states (``estimated``, ``provisional``, ``extrapolated``, ``projected``: model-based estimates keep it) and
  flags verbatim;
* ``comparability_note`` - source-stated and reviewer-recorded notes between series; a pair without one is
  ``comparability_unknown``.

Numeric values also live in the Economics series storage (``economic_vintages``, ``dataset_observations``) through
:func:`src.domains.economic.model.register_series`, domain ``society``, as the income and social protection tracks do.

**Minimisation decision (WD01).** All implemented sources publish aggregate statistics; none returns data about a
person or a household. :func:`check_item` refuses at write time any key that would carry a person- or household-level
field (HFCS, PHF or SCF microdata, implicates, survey weights, rich lists, named individuals or families) and any key
or value origin that would carry a derived, filled, blended, re-harmonised or forecast value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.wealth_distribution_sources import (
    AREA_SCHEMES,
    EXCLUSIONS,
    INDICATORS,
    KEY_FIELDS,
    LIVE_VERIFICATION,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    PUBLICATION_STATUSES,
    STATUSES,
    decimal_text,
)

CONTRACT = "noesis-wealth-distribution-record-v2"
ANSWER_CONTRACT = "noesis-wealth-distribution-answer-v1"
READ_SCOPE = "knowledge:wealth:read"
WRITE_SCOPE = "knowledge:wealth:write"
REVIEW_SCOPE = "knowledge:wealth:review"
DEFAULT_NAMESPACE = "global"
ECONOMIC_DOMAIN = "society"
PROVIDER_ID = "society.wealth"
BUNDLE_ID = "society"
# Optional Society features, one per implemented source (default off); feature id -> provider.
FEATURES = {"wid": "wid-wealth-distribution", "dwa": "ecb-distributional-wealth-accounts",
            "dfa": "fed-distributional-financial-accounts", "hfcs": "ecb-hfcs-statistical-tables",
            "phf": "bundesbank-phf-aggregates"}
RECORD_TYPES = ("release", "series", "definition", "vintage", "observation", "comparability_note")
SCHEMA_FILE = f"contracts/schemas/jsonschema/{CONTRACT}.json"
# Keys that would carry data about a person or a household (microdata, weights, rich lists, named holders).
PERSONAL_DATA_FIELDS = frozenset({
    "person_id", "person_name", "full_name", "first_name", "last_name", "name_of_person", "date_of_birth",
    "birth_date", "email", "phone", "address", "home_address", "national_id", "tax_id", "ssn",
    "household_id", "hh_id", "respondent_id", "household_net_wealth", "net_worth_of_household",
    "household_weight", "person_weight", "survey_weight", "final_weight", "replicate_weight", "implicate",
    "microdata", "survey_response", "rich_list_name", "billionaire_name", "family_name", "owner_name",
    "individual_wealth", "household_record",
})
# Keys that would carry a derived, filled, blended, re-harmonised or forecast number of our own.
FORBIDDEN_KEYS = frozenset({
    "nowcast", "nowcasted", "forecast", "forecast_value", "predicted", "imputed", "imputed_value", "interpolated",
    "interpolated_value", "gap_filled", "filled_value", "blended", "blended_value", "combined_value",
    "average_value", "harmonised_value", "harmonized_value", "rescaled_value", "own_share", "derived_share",
    "own_top_share", "pareto_fit", "pareto_alpha", "own_ratio", "recomputed_value", "derived_value", "derived_rate",
    "ratio_value", "corrected_estimate", "re_estimated",
})
PUBLISHED_ORIGIN = "published"
MINIMISATION = {
    "decision": "published aggregates only; no person- or household-level field is stored",
    "stored": "published aggregates per place, period and series key, with flags, method statements, definitions and "
              "citations",
    "redacted": "nothing (no personal field is ever acquired)",
    "excluded": "HFCS, PHF and SCF microdata, implicates and survey weights, rich lists and named individuals or "
                "families, household-level records, confidential cells (stored as their published status, never as "
                "a value)",
    "retention": "release vintages are kept for provenance; no erasure workflow applies",
    "who_may_query": f"principals holding {READ_SCOPE} and access to the namespace",
    "enforced_by": "wealth_distribution_records.check_item at write time",
}


class WealthError(ValueError):
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
    raw = str(value).strip().replace("Z", "+00:00")
    if len(raw) == 10:
        raw += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def cutoff_ms(as_of: Any) -> int | None:
    """A date means the end of that day (released *by* the date); a time is used as given."""
    if as_of in (None, ""):
        return None
    if isinstance(as_of, (int, float)):
        return int(as_of)
    text = str(as_of).strip()
    return to_ms(text) + 86_399_999 if len(text) == 10 else to_ms(text)


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise WealthError("unauthorized", f"{required} and namespace access are required")


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
    """Validate one published series item before anything is written (record rules and the WD01 minimisation)."""
    leaked = personal_data_paths(dict(item))
    if leaked:
        raise WealthError("personal_data", "records never carry person- or household-level data", fields=leaked)
    derived = forbidden_paths(dict(item))
    if derived:
        raise WealthError("derived_value", "published records carry no derived, filled, blended, re-harmonised or "
                                           "forecast value", fields=derived)
    for key in ("provider", "dataset", "native_key", "area", "definition", "frequency", *KEY_FIELDS):
        if not item.get(key):
            raise WealthError("invalid_record", f"a wealth series states its {key}")
    if item["provider"] not in PROVIDERS:
        raise WealthError("invalid_record", f"provider is one of {PROVIDERS}")
    if dict(item["indicator"]).get("concept") not in INDICATORS:
        raise WealthError("invalid_record", f"indicator concept is one of {INDICATORS}")
    for field in KEY_FIELDS:
        if not dict(item[field]).get("code"):
            raise WealthError("invalid_record", f"{field} states the code as published")
    if dict(item["area"]).get("scheme") not in AREA_SCHEMES or not dict(item["area"]).get("code"):
        raise WealthError("invalid_record", f"area scheme is one of {AREA_SCHEMES}")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("origin", PUBLISHED_ORIGIN) != PUBLISHED_ORIGIN:
            raise WealthError("derived_value", f"only values the source published are stored (origin "
                                               f"{obs.get('origin')!r} refused)")
        if obs.get("status") not in STATUSES:
            raise WealthError("invalid_record", f"each observation states its status ({STATUSES})")
        if obs["status"] != "reported" and obs.get("value") is not None:
            raise WealthError("invalid_record", "a confidential or unpublished cell carries no value")
        if obs["status"] == "reported" and decimal_text(obs.get("value_text")) != obs.get("value"):
            raise WealthError("recomputed_value", "values are stored exactly as published, never recomputed",
                              period=obs.get("period"))
        if obs.get("publication_status", "not-stated") not in PUBLICATION_STATUSES:
            raise WealthError("invalid_record", f"publication status is one of {PUBLICATION_STATUSES}")
        if obs["period"] in periods:
            raise WealthError("invalid_record", "a series states a period twice")
        periods.add(obs["period"])


def comparability_basis(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recorded differences between two series keys (facts of the keys; nothing is harmonised or blended)."""
    differences = []
    fields = (("provider", "different_source"), ("indicator", "different_indicator"),
              ("wealth_concept", "different_wealth_concept"), ("group", "different_group"),
              ("unit", "different_unit"), ("population", "different_population"), ("method", "different_method"),
              ("area", "different_place"), ("frequency", "different_frequency"))
    for field, kind in fields:
        a, b = left.get(field), right.get(field)
        if isinstance(a, Mapping) and isinstance(b, Mapping):
            a, b = {k: a.get(k) for k in ("scheme", "code", "concept")}, {k: b.get(k) for k in ("scheme", "code",
                                                                                                  "concept")}
        if a != b:
            differences.append({"kind": kind, "field": field, "left": left.get(field), "right": right.get(field)})
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
        results.append(registry.register(definition, f"wealth-schema:{name}:2.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


# ------------------------------------------------------------------ features and readiness


def _coordinator(conn: Any) -> Any:
    if not table_exists(conn, "composition_authority"):
        return None
    from src.composition.lifecycle import CompositionCoordinator

    coordinator = CompositionCoordinator(conn)
    return coordinator if coordinator.is_composition_managed(BUNDLE_ID) else None


def enabled_providers(conn: Any) -> set[str]:
    """The sources whose optional Society feature is selected (every one before composition management)."""
    try:
        coordinator = _coordinator(conn)
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return set()
    if coordinator is None:
        return set(PROVIDERS)
    features = ((coordinator.active() or {}).get("plan") or {}).get("features", {}).get(BUNDLE_ID)
    return {FEATURES[f] for f in features or [] if f in FEATURES}


def readiness(conn: Any, namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    """ready / fixture-only / stale / unavailable / feature-disabled / not-implemented per source."""
    from src.kb.wealth_distribution_store import WealthStore

    store = WealthStore(conn, initialize=False)
    selected = enabled_providers(conn)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        entry = {"delivers": contract["delivers"], "access_decision": contract["status"],
                 "method": contract["method"], "live_verification": LIVE_VERIFICATION[provider]}
        if provider not in PROVIDERS:
            providers[provider] = {**entry, "status": "not-implemented"}
            continue
        if provider not in selected:
            providers[provider] = {**entry, "status": "feature-disabled"}
            continue
        state = store.provider_state(namespace, provider)
        if state.get("last_success_ms") is None:
            status = "unavailable"
        elif state["stale"]:
            status = "stale"
        elif state.get("last_execution") == "live":
            status = "ready"
        else:
            status = "fixture-only"
        providers[provider] = {**entry, "status": status, "state": state}
    return {
        "provider": PROVIDER_ID, "bundle": BUNDLE_ID, "namespace": namespace,
        "stores_ready": store.ready(), "providers": providers,
        "series_storage": "economic_indicators, economic_series_map, economic_vintages and dataset_observations "
                          f"(domain {ECONOMIC_DOMAIN})",
        "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE, "minimisation": MINIMISATION["decision"],
        "evidence": "offline fixture results and live results are reported separately; no source is live until a "
                    "dated run verifies it (docs/development/wealth-distribution-evidence/)",
    }


__all__ = [
    "ANSWER_CONTRACT",
    "CONTRACT",
    "ECONOMIC_DOMAIN",
    "EXCLUSIONS",
    "FEATURES",
    "MINIMISATION",
    "PROVIDER_ID",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "WealthError",
    "authorize",
    "canonical",
    "check_item",
    "comparability_basis",
    "cutoff_ms",
    "digest",
    "enabled_providers",
    "forbidden_paths",
    "iso",
    "load",
    "personal_data_paths",
    "readiness",
    "register_schemas",
    "table_exists",
    "to_ms",
]
