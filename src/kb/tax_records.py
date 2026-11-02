"""Tax transparency records for the Economics ``economics.tax`` provider (#2889).

One record contract, ``noesis-tax-transparency-record-v2`` (the wave's record contract line is 2.x), covers the record
types the provider owns:

* ``release`` - one acquired **edition** (an OECD Corporate Tax Statistics edition, an Atlas of the Offshore World
  update, a Global Tax Evasion Report) with its declared edition label and publication date, retrieval clock, document
  key, digests and evidence origin;
* ``series`` - keyed by dataset, indicator, jurisdiction, counterpart grouping (CbCR partner groupings), unit with its
  multiplier, frequency and **nature** (``published_statistic`` or ``model_estimate``). The edition is never part of
  the key, so every edition of the same key is a vintage of the same series;
* ``vintage`` - one edition of one series with release and retrieval clocks, the definition and (for model estimates)
  the method in force, and the changes against the previous edition. A revised value is a new vintage, never an
  overwrite; a series a complete later edition no longer states gets a ``removed_by_source`` vintage;
* ``observation`` - period, value exactly as published, status (``reported`` for published statistics, ``estimated``
  for model estimates, ``confidential``, ``not_published``) and status letters verbatim;
* ``company_report`` - a **link-only** reference to a public CbCR report (Directive (EU) 2021/2101): company name as
  published, published identifiers, financial year, report URL, publication date and channel; never a figure.

Published statistics also live in the Economics series storage (``economic_vintages``, ``dataset_observations``)
through :func:`src.domains.economic.model.register_series`, domain ``economics``. **Model estimates never do**: they stay
in the provider's own vintage tables with their method and edition, so no generic observation reader can present them
as observations.

**Minimisation decision (TX01).** :func:`check_item` refuses at write time any key that would carry a person-level or
taxpayer-level field or the name of a wealth holder, and any key that would carry a derived, filled, blended, ranked or
forecast value (an own effective tax rate, a ratio, a share, a rank, a score, a conversion). A model-estimate series
without its method or edition is refused, and so is a published statistic carrying an ``estimated`` cell.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.tax_sources import (
    DATASETS,
    EXCLUSIONS,
    FREQUENCIES,
    JURISDICTION_SCHEMES,
    LIVE_VERIFICATION,
    NATURES,
    NEVER_SENTENCE,
    NOT_IMPLEMENTED,
    NOT_IMPLEMENTED_ANSWER,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    REFERENCE_ONLY,
    STATUSES,
    period_matches,
)

CONTRACT = "noesis-tax-transparency-record-v2"
ANSWER_CONTRACT = "noesis-tax-transparency-answer-v1"
MATCH_CONTRACT = "noesis-tax-company-match-v1"
READ_SCOPE = "knowledge:tax:read"
WRITE_SCOPE = "knowledge:tax:write"
REVIEW_SCOPE = "knowledge:tax:review"
DEFAULT_NAMESPACE = "global"
ECONOMIC_DOMAIN = "economics"
PROVIDER_ID = "economics.tax"
# One optional, default-off Economics feature per source (each adds the economics.tax provider).
FEATURES = {
    "tax-oecd-cbcr": "oecd-cts-cbcr",
    "tax-oecd-rates": "oecd-cts-rates",
    "tax-eutax-offshore-wealth": "eutax-offshore-wealth",
    "tax-eutax-billionaire-etr": "eutax-billionaire-etr",
    "tax-public-cbcr": "public-cbcr",
}
PROVIDER_FEATURES = {provider: feature for feature, provider in FEATURES.items()}
RECORD_TYPES = ("release", "series", "vintage", "observation", "company_report")
CHANGE_KINDS = ("new_series", "new_period", "revised_value", "method_change", "definition_change",
                "removed_by_source")
SCHEMA_FILE = f"contracts/schemas/jsonschema/{CONTRACT}.json"
# Keys that would carry data about a person, a taxpayer or a named wealth holder.
PERSONAL_DATA_FIELDS = frozenset({
    "person_id", "person_name", "full_name", "first_name", "last_name", "date_of_birth", "birth_date", "email",
    "phone", "home_address", "address", "national_id", "passport_number", "taxpayer_id", "tax_id", "tin",
    "taxpayer_name", "billionaire_name", "wealth_holder", "wealth_holder_name", "beneficial_owner",
    "beneficial_owners", "account_holder", "respondent_id", "microdata",
})
# Keys that would carry a derived, filled, blended, ranked or forecast number.
FORBIDDEN_KEYS = frozenset({
    "nowcast", "forecast", "forecast_value", "projection", "predicted", "imputed", "imputed_value", "interpolated",
    "gap_filled", "filled_value", "filled_year", "filled_jurisdiction", "blended", "blended_value", "combined_value",
    "average_value", "own_effective_tax_rate", "computed_effective_tax_rate", "derived_etr", "implied_tax_rate",
    "tax_gap_estimate", "profit_shifting_estimate", "ratio", "derived_ratio", "share_value", "per_capita",
    "per_capita_value", "per_employee", "per_employee_value", "rank", "ranking", "risk_score", "haven_score",
    "secrecy_score", "converted_value", "value_in_eur", "value_in_usd", "derived_value", "unconfidential_value",
})
# Keys a link-only public CbCR reference never carries (company-level figures).
REPORT_FIGURE_KEYS = frozenset({
    "profit", "profit_before_tax", "profit_before_income_tax", "income_tax_paid", "income_tax_accrued", "tax_paid",
    "tax_accrued", "revenue", "revenues", "turnover", "employees", "number_of_employees", "accumulated_earnings",
    "tangible_assets", "values", "figures", "observations",
})
MINIMISATION = {
    "decision": "published aggregates and estimates per jurisdiction only; public CbCR reports as link-only references; "
                "no person-, taxpayer- or wealth-holder-level field is stored",
    "stored": "published aggregates and estimates per jurisdiction, period and series key, with status letters, notes, "
              "methods, editions and citations; public CbCR references (company name as published, published "
              "identifiers, financial year, report URL, publication date and channel)",
    "redacted": "nothing (no personal field is ever acquired)",
    "excluded": "taxpayer- or person-level data; names of individual wealth holders; company-level figures from public "
                "CbCR reports; Tax Justice Network scores and ranks; confidential cells, stored as their status only",
    "retention": "editions and vintages are kept for provenance; no erasure workflow applies",
    "who_may_query": f"principals holding {READ_SCOPE} and access to the namespace",
    "enforced_by": "tax_records.check_item and tax_records.check_reference at write time",
}


class TaxError(ValueError):
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


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise TaxError("unauthorized", f"{required} and namespace access are required")


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
    """Validate one published series item before anything is written (TX01 rules and minimisation)."""
    leaked = personal_data_paths(dict(item))
    if leaked:
        raise TaxError("personal_data", "records never carry person-, taxpayer- or wealth-holder-level data",
                       fields=leaked)
    derived = forbidden_paths(dict(item))
    if derived:
        raise TaxError("derived_value", "published records carry no derived, filled, blended, ranked or forecast value",
                       fields=derived)
    for key in ("provider", "dataset", "native_key", "indicator", "jurisdiction", "counterpart", "unit", "definition",
                "frequency", "nature", "edition"):
        if not item.get(key):
            raise TaxError("invalid_record", f"a tax series states its {key}")
    if item["provider"] not in PROVIDERS:
        raise TaxError("invalid_record", f"provider is one of {PROVIDERS}")
    dataset = DATASETS.get(str(item["dataset"]))
    if dataset is None or dataset["provider"] != item["provider"]:
        raise TaxError("invalid_record", "the dataset is one of the provider's audited datasets")
    if item["nature"] not in NATURES or item["nature"] != dataset["nature"]:
        raise TaxError("invalid_record", f"{item['dataset']} publishes {dataset['nature']} values; a model estimate is "
                                         "never relabelled as a published statistic or the other way round")
    if item["nature"] == "model_estimate":
        method = dict(item.get("method") or {})
        if not method.get("label") or not method.get("description") or not method.get("reference"):
            raise TaxError("invalid_record", "a model estimate keeps its method (label, description, reference)")
    elif item.get("method"):
        raise TaxError("invalid_record", "a published statistic carries no model method")
    concept = dict(item["indicator"]).get("concept")
    if concept not in dataset["concepts"]:
        raise TaxError("invalid_record", f"indicator concept is one of {dataset['concepts']}")
    if item["frequency"] not in FREQUENCIES:
        raise TaxError("invalid_record", f"frequency is one of {FREQUENCIES}")
    jurisdiction = dict(item["jurisdiction"])
    if jurisdiction.get("scheme") not in JURISDICTION_SCHEMES or not jurisdiction.get("code"):
        raise TaxError("invalid_record", "a series states its jurisdiction with its code scheme")
    if dataset["counterpart"] and dict(item["counterpart"]).get("code") in (None, "not_applicable"):
        raise TaxError("invalid_record", "a CbCR series states its partner-jurisdiction grouping")
    periods = set()
    value_status = "estimated" if item["nature"] == "model_estimate" else "reported"
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise TaxError("invalid_record", f"each observation states its status ({STATUSES})")
        if obs["status"] in {"reported", "estimated"} and obs["status"] != value_status:
            raise TaxError("invalid_record", f"a {item['nature']} value has status {value_status}")
        if obs["status"] in {"confidential", "not_published"} and obs.get("value") is not None:
            raise TaxError("invalid_record", "a confidential or unpublished cell carries no value (a status, never a "
                                             "value)")
        if not period_matches(obs["period"], item["frequency"]):
            raise TaxError("invalid_record", f"{obs['period']} is not an annual period")
        if obs["period"] in periods:
            raise TaxError("invalid_record", "a series states a period twice")
        periods.add(obs["period"])


def check_reference(reference: Mapping[str, Any]) -> None:
    """A public CbCR reference is link-only: name, identifiers, financial year, URL, dates; never a figure."""
    if personal_data_paths(dict(reference)):
        raise TaxError("personal_data", "a report reference carries no personal field")
    figures = _paths(dict(reference), REPORT_FIGURE_KEYS) + forbidden_paths(dict(reference))
    if figures:
        raise TaxError("reference_only", "public CbCR references are link-only until a machine-readable source exists; "
                                         "no company-level figure is stored", fields=figures)
    for key in ("company_name", "fiscal_year", "report_url", "published_on", "channel"):
        if not reference.get(key):
            raise TaxError("invalid_reference", f"a report reference states its {key}")
    if not str(reference["report_url"]).startswith("https://"):
        raise TaxError("invalid_reference", "a report reference links an https URL")
    year = dict(reference["fiscal_year"])
    start, end = str(year.get("start") or ""), str(year.get("end") or "")
    if len(start) != 10 or len(end) != 10 or end <= start:
        raise TaxError("invalid_reference", "a report reference states the financial year start and end dates")
    if start < "2024-06-22":
        raise TaxError("invalid_reference", "Directive (EU) 2021/2101 reports start with financial years beginning on "
                                            "or after 2024-06-22 (verify)")
    if reference["channel"] not in ("company-website", "business-register"):
        raise TaxError("invalid_reference", "the channel is company-website or business-register")
    for identifier in reference.get("identifiers") or []:
        if not dict(identifier).get("scheme") or not dict(identifier).get("value"):
            raise TaxError("invalid_reference", "each identifier states its scheme and value as published")


def _managed_features(conn: Any) -> list[str] | None:
    """The Economics features of the active plan, or ``None`` before composition manages the bundle."""
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
        ).fetchall()}
        if len(tables) < 4:
            return None
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='economics'").fetchone()
        if not managed or managed[0] != "composition":
            return None
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get("economics") or [])


def feature_enabled(conn: Any, feature: str | None = None) -> bool:
    """Whether a tax feature (any of them, or the one named) is selected in the active plan."""
    selected = set(_managed_features(conn) or [])
    return bool(selected & set(FEATURES)) if feature is None else feature in selected


def enabled_providers(conn: Any) -> set[str]:
    """The sources whose optional feature is selected (every one before composition manages the bundle)."""
    managed = _managed_features(conn)
    if managed is None:
        return set(FEATURES.values())
    return {FEATURES[f] for f in managed if f in FEATURES}


def readiness(conn: Any) -> dict[str, Any]:
    ready = table_exists(conn, "tax_vintages") and table_exists(conn, "tax_releases")
    selected = set(_managed_features(conn) or [])
    providers: dict[str, dict[str, Any]] = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        base = {"delivers": contract["delivers"], "access_decision": contract["status"],
                "live_verification": LIVE_VERIFICATION[provider]["status"]}
        if provider in NOT_IMPLEMENTED:
            providers[provider] = {**base, "releases": 0, "stale": None, "reason": NOT_IMPLEMENTED_ANSWER["reason"]}
            continue
        feature = PROVIDER_FEATURES[provider]
        if provider in REFERENCE_ONLY:
            count = int(conn.execute("SELECT count(DISTINCT reference_key) FROM tax_company_reports").fetchone()[0]) \
                if table_exists(conn, "tax_company_reports") else 0
            providers[provider] = {**base, "references": count, "stale": None, "feature": feature,
                                   "feature_selected": feature in selected,
                                   "reason": "link-only references declared by operators; no figures"}
            continue
        releases, state = 0, {"stale": True, "reason": "never acquired"}
        if ready:
            releases = int(conn.execute("SELECT count(*) FROM tax_releases WHERE provider=?", [provider]).fetchone()[0])
        if table_exists(conn, "tax_receipts"):
            rows = conn.execute("SELECT outcome FROM tax_receipts WHERE provider=? ORDER BY created_at_ms, receipt_id",
                                [provider]).fetchall()
            if any(r[0] in {"applied", "unchanged"} for r in rows):
                failed = rows[-1][0] == "failed"
                state = {"stale": failed, "reason": "last run failed" if failed else None}
            elif rows:
                state = {"stale": True, "reason": "last run failed"}
        providers[provider] = {**base, "releases": releases, "feature": feature,
                               "feature_selected": feature in selected, **state}
    return {
        "features": list(FEATURES),
        "selected": bool(selected & set(FEATURES)),
        "features_selected": sorted(selected & set(FEATURES)),
        "stores_ready": ready,
        "series_storage": "published statistics also in economic_indicators, economic_series_map, economic_vintages and "
                          "dataset_observations; model estimates only in tax_vintages and tax_observations",
        "providers": providers,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "minimisation": MINIMISATION["decision"],
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider is "
                "live until a dated run verifies it",
    }


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
            "provenance": {"kind": "imported", "source": "packs/economics"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"tax-schema:{name}:2.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


__all__ = [
    "ANSWER_CONTRACT",
    "CHANGE_KINDS",
    "CONTRACT",
    "ECONOMIC_DOMAIN",
    "EXCLUSIONS",
    "FEATURES",
    "MATCH_CONTRACT",
    "MINIMISATION",
    "PROVIDER_FEATURES",
    "PROVIDER_ID",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "TaxError",
    "authorize",
    "canonical",
    "check_item",
    "check_reference",
    "digest",
    "enabled_providers",
    "feature_enabled",
    "forbidden_paths",
    "iso",
    "personal_data_paths",
    "readiness",
    "register_schemas",
    "table_exists",
    "to_ms",
]
