"""Social protection series records for the Society bundle's ``society.social-protection`` provider (#2741, SS02).

One record contract, ``noesis-social-protection-record-v2`` (the wave's record contract line is 2.x), covers the
record types the provider owns:

* ``release`` - one acquired publication (an ESSPROS dataset as of its ``LAST UPDATE``, a SOCX response dated by the
  declared release or the retrieval time, an ILOSTAT response or World Social Protection Report edition) with its
  release clock and basis, retrieval clock, edition, dataflow version, digests and evidence origin;
* ``series`` - keyed by source, dataset or dataflow, measure (expenditure, beneficiaries or coverage), the
  publisher's own classification (ESSPROS function ``spfunc`` or pension category, SOCX policy area, ILO contingency),
  scheme or benefit type, source of financing, cash or in kind, gross or net basis, sex, unit as published, place and
  frequency. ILO's population denominator is stored per series;
* ``definition`` - the definition as the source states it (the ESSPROS manual edition, the SOCX methodology, ILO's
  ``NOTE_SOURCE``, ``NOTE_INDICATOR`` and ``NOTE_CLASSIF`` verbatim, cited documents), revisioned: a changed definition
  is a new revision;
* ``vintage`` - one release of one series with release and retrieval clocks, the definition revision, edition and
  dataflow version in force and the changes against the previous vintage. A revision or a restating report edition is a
  new vintage, never an overwrite; a series a complete later release no longer states gets a ``removed_by_source``
  vintage, never a deletion;
* ``observation`` - period, value exactly as published (published percentages and per-inhabitant values are never
  recomputed), cell status (``reported``, ``confidential``, ``not_published``), the publication status the source
  states (``provisional``, ``estimated``, ``projected`` - OECD estimates and projections keep it and are never final)
  and flags verbatim (``b``, ``p``, ``e``, ``d``, ``c``; ``c`` is a status, never a value);
* ``comparability_note`` - source-stated breaks, definition differences and published ESSPROS-SOCX scope
  differences, and reviewer notes.

Numeric values also live in the Economics series storage (``economic_vintages``, ``dataset_observations``) through
:func:`src.domains.economic.model.register_series`, domain ``society``, as the labour, demographics and income
tracks do; no new series store or record shape (``statistical-series``).

**Minimisation decision (SS01).** All three sources publish aggregate statistics; none returns data about a person
or a household. :func:`check_item` refuses at write time any key that would carry a person- or household-level field
(benefit-recipient registers, administrative or survey microdata, Social Security Inquiry returns) and any key or
value origin that would carry a derived, filled, blended, re-classified or forecast value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.social_protection_sources import (
    COFOG_SCHEME,
    EXCLUSIONS,
    FUNCTION_SCHEMES,
    KEY_FIELDS,
    LIVE_VERIFICATION,
    MEASURES,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    PUBLICATION_STATUSES,
    STATUSES,
    decimal_text,
)

CONTRACT = "noesis-social-protection-record-v2"
ANSWER_CONTRACT = "noesis-social-protection-answer-v1"
READ_SCOPE = "knowledge:social-protection:read"
WRITE_SCOPE = "knowledge:social-protection:write"
REVIEW_SCOPE = "knowledge:social-protection:review"
DEFAULT_NAMESPACE = "global"
ECONOMIC_DOMAIN = "society"
PROVIDER_ID = "society.social-protection"
BUNDLE_ID = "society"
# Optional Society features, one per source (default off); feature id -> provider.
FEATURES = {"esspros": "eurostat-esspros", "socx": "oecd-socx", "ilo-coverage": "ilo-social-protection-coverage"}
RECORD_TYPES = ("release", "series", "definition", "vintage", "observation", "comparability_note")
CHANGE_KINDS = ("new_series", "new_period", "revised_value", "definition_change", "edition_restatement",
                "removed_by_source")
SCHEMA_FILE = f"contracts/schemas/jsonschema/{CONTRACT}.json"
# Keys that would carry data about a person or a household (registers, microdata, questionnaire returns).
PERSONAL_DATA_FIELDS = frozenset({
    "person_id", "person_name", "full_name", "first_name", "last_name", "name_of_person", "date_of_birth",
    "birth_date", "email", "phone", "address", "home_address", "national_id", "tax_id", "ssn",
    "social_security_number", "insurance_number", "household_id", "hh_id", "respondent_id", "beneficiary_id",
    "beneficiary_name", "recipient_id", "recipient_name", "claimant_id", "claimant_name", "pensioner_id",
    "benefit_amount_of_person", "household_income", "person_weight", "household_weight", "record_weight",
    "microdata", "questionnaire_return", "ssi_return",
})
# Keys that would carry a derived, filled, blended, re-classified or forecast number of our own.
FORBIDDEN_KEYS = frozenset({
    "nowcast", "nowcasted", "forecast", "forecast_value", "projection_of_our_own", "predicted", "imputed",
    "imputed_value", "interpolated", "gap_filled", "filled_value", "blended", "blended_value", "combined_value",
    "average_value", "harmonised_value", "reclassified_function", "cofog_equivalent", "esspros_equivalent",
    "socx_equivalent", "per_capita", "per_capita_value", "per_beneficiary", "per_beneficiary_value",
    "share_of_gdp_own", "own_share_of_gdp", "recomputed_value", "derived_value", "derived_rate", "ratio_value",
})
# A value's origin: the source's own publication only.
PUBLISHED_ORIGIN = "published"
MINIMISATION = {
    "decision": "published aggregates only; no person- or household-level field is stored",
    "stored": "published aggregates per place, reference year and series key, with flags, notes, definitions and "
              "citations",
    "redacted": "nothing (no personal field is ever acquired)",
    "excluded": "benefit-recipient registers, administrative or survey microdata, Social Security Inquiry "
                "questionnaire returns, confidential cells (stored as their published status, never as a value)",
    "retention": "release vintages are kept for provenance; no erasure workflow applies",
    "who_may_query": f"principals holding {READ_SCOPE} and access to the namespace",
    "enforced_by": "social_protection_records.check_item at write time",
}


class SocialProtectionError(ValueError):
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
        raise SocialProtectionError("unauthorized", f"{required} and namespace access are required")


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
    """Validate one published series item before anything is written (SS02 rules and the SS01 minimisation)."""
    leaked = personal_data_paths(dict(item))
    if leaked:
        raise SocialProtectionError("personal_data", "records never carry person- or household-level data",
                                    fields=leaked)
    derived = forbidden_paths(dict(item))
    if derived:
        raise SocialProtectionError("derived_value", "published records carry no derived, filled, blended, "
                                                     "re-classified or forecast value", fields=derived)
    for key in ("provider", "dataset", "native_key", "measure", "area", "definition", "frequency", *KEY_FIELDS):
        if not item.get(key):
            raise SocialProtectionError("invalid_record", f"a social protection series states its {key}")
    provider = item["provider"]
    if provider not in PROVIDERS:
        raise SocialProtectionError("invalid_record", f"provider is one of {PROVIDERS}")
    if dict(item["measure"]).get("concept") not in MEASURES:
        raise SocialProtectionError("invalid_record", f"measure concept is one of {MEASURES}")
    scheme = dict(item["function"]).get("scheme")
    if scheme == COFOG_SCHEME:
        raise SocialProtectionError("reclassified", "COFOG functions belong to economics.public-finance and are a "
                                                    "distinct concept; they are never stored as a social protection "
                                                    "function")
    if scheme not in FUNCTION_SCHEMES[provider] or not dict(item["function"]).get("code"):
        raise SocialProtectionError("reclassified", f"{provider} series state the publisher's own classification "
                                                    f"({FUNCTION_SCHEMES[provider]}), never another publisher's")
    for field in KEY_FIELDS:
        if not dict(item[field]).get("code"):
            raise SocialProtectionError("invalid_record", f"{field} states the code as published")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("origin", PUBLISHED_ORIGIN) != PUBLISHED_ORIGIN:
            raise SocialProtectionError("derived_value", "only values the source published are stored "
                                                         f"(origin {obs.get('origin')!r} refused)")
        if obs.get("status") not in STATUSES:
            raise SocialProtectionError("invalid_record", f"each observation states its status ({STATUSES})")
        if obs["status"] != "reported" and obs.get("value") is not None:
            raise SocialProtectionError("invalid_record", "a confidential or unpublished cell carries no value "
                                                          "('c' is a status, never a value)")
        if obs["status"] == "reported" and decimal_text(obs.get("value_text")) != obs.get("value"):
            raise SocialProtectionError("recomputed_value", "values are stored exactly as published, never "
                                                            "recomputed", period=obs.get("period"))
        if obs.get("publication_status", "not-stated") not in PUBLICATION_STATUSES:
            raise SocialProtectionError("invalid_record", f"publication status is one of {PUBLICATION_STATUSES}")
        if obs["period"] in periods:
            raise SocialProtectionError("invalid_record", "a series states a period twice")
        periods.add(obs["period"])


def comparability_basis(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recorded differences between two series keys (facts of the keys; nothing is harmonised or blended)."""
    differences = []
    fields = (("provider", "different_source"), ("measure", "different_measure"),
              ("function", "different_classification"), ("scheme_type", "different_scheme_type"),
              ("financing", "different_financing"), ("cash_or_kind", "different_cash_or_kind"),
              ("basis", "different_basis"), ("sex", "different_sex"), ("unit", "different_unit"),
              ("area", "different_place"))
    for field, kind in fields:
        a, b = left.get(field), right.get(field)
        if a != b:
            differences.append({"kind": kind, "field": field, "left": a, "right": b})
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
        results.append(registry.register(definition, f"social-protection-schema:{name}:2.0.0:{digest(content)[:16]}",
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
    """ready / fixture-only / stale / unavailable / feature-disabled per source; live verification kept separate."""
    from src.kb.social_protection_store import SocialProtectionStore

    store = SocialProtectionStore(conn, initialize=False)
    selected = enabled_providers(conn)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        entry = {"delivers": contract["delivers"], "access_decision": contract["status"],
                 "live_verification": LIVE_VERIFICATION[provider]}
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
    links = {"economics.demographics": "available" if table_exists(conn, "demographic_series") else "provider_absent",
             "economics.public-finance": "available" if table_exists(conn, "public_finance_gfs_vintages")
             else "provider_absent"}
    return {
        "provider": PROVIDER_ID, "bundle": BUNDLE_ID, "namespace": namespace,
        "stores_ready": store.ready(), "providers": providers, "optional_links": links,
        "series_storage": "economic_indicators, economic_series_map, economic_vintages and dataset_observations "
                          f"(domain {ECONOMIC_DOMAIN})",
        "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE, "minimisation": MINIMISATION["decision"],
        "evidence": "offline fixture results and live results are reported separately; no source is live until a "
                    "dated run verifies it (docs/development/social-protection-evidence/)",
    }


__all__ = [
    "ANSWER_CONTRACT",
    "CHANGE_KINDS",
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
    "SocialProtectionError",
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
