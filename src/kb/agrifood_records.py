"""Agri-food record model for the Agriculture and Food Systems pack (#2213, AF02 #2333).

``noesis-agrifood-record-v1`` is the publisher-neutral statement every
agri-food adapter emits (``src/ingestion/agrifood_sources.py``) and
:class:`src.kb.agrifood_store.AgrifoodStore` keeps:

* **commodity** - a publisher's own commodity code and label (FAOSTAT item,
  NASS commodity, PSD commodity, Eurostat crop or product, portal product);
* **release** - a dataset or domain release with its date (FAOSTAT domain
  update, PSD monthly release, Eurostat dataset update);
* **observation** - production, yield, area harvested or planted, producer
  or market price;
* **food_balance** - PSD supply-and-distribution attributes and FAOSTAT food
  balance elements.

Every observation and food-balance figure carries its commodity, place,
element/measure, unit, reference period, the value text and the source flag
**verbatim**, the release vintage and the source. Flags keep the publisher's
code and label; :data:`FLAG_VOCABULARIES` adds classes (official, estimated,
imputed, provisional, withheld, ...) as an index beside them, so the mapping
loses nothing. Marketing-year and calendar-year periods are explicit and never
converted. A revised figure is a new vintage beside the prior one.

Publisher forecasts and projections (NASS forecast reference periods, USDA
PSD projections for the current marketing year) are labelled as the
publisher's (:data:`ESTIMATE_TYPES`); the pack makes no forecast, projection
or food-security score of its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTRACT = "noesis-agrifood-record-v1"
SERIES_CONTRACT = "noesis-agrifood-series-v1"
CROSSWALK_CONTRACT = "noesis-agrifood-crosswalk-v1"
LINK_CONTRACT = "noesis-agrifood-link-v1"
NOTIFICATION_CONTRACT = "noesis-agrifood-notification-v1"
READ_SCOPE = "knowledge:agrifood:read"
WRITE_SCOPE = "knowledge:agrifood:write"
REVIEW_SCOPE = "knowledge:agrifood:review"
SOURCE_PACK = "agrifood"
# Schema versions registered for the pack (packs/agrifood/pack.json ``schema_versions``).
SCHEMA_VERSIONS = {
    "agrifood-record": "1.0.0",
    "agrifood-series": "1.0.0",
    "agrifood-crosswalk": "1.0.0",
    "agrifood-link": "1.0.0",
    "agrifood-notification": "1.0.0",
    "source-pack": "1.0.0",
}
RECORD_TYPES = ("commodity", "release", "observation", "food_balance")
FIGURE_TYPES = ("observation", "food_balance")
PROVIDERS = ("faostat", "nass-quickstats", "fas-psd", "eurostat-agri", "agri-food-portal")
PERIOD_TYPES = ("calendar-year", "marketing-year", "month", "week", "date-range", "other")
MEASURE_KINDS = ("production", "yield", "area_harvested", "area_planted", "producer_price", "market_price",
                 "supply_distribution", "food_balance", "other")
ESTIMATE_TYPES = ("observation", "publisher-estimate", "publisher-forecast", "publisher-projection")
VALUE_STATUSES = ("reported", "withheld", "missing", "not-applicable", "below-rounding")
FLAG_CLASSES = ("official", "estimated", "imputed", "provisional", "projection", "forecast", "revised",
                "series-break", "unofficial", "international-organization", "withheld", "confidential", "missing",
                "not-applicable", "below-rounding", "low-reliability", "definition-differs", "not-significant",
                "not-flagged", "unknown")
# Publisher flag vocabularies: code -> (label as published, classes). The code and label are always stored
# verbatim beside the classes; an unknown code keeps its text with the class ``unknown``.
FLAG_VOCABULARIES: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "faostat-flags": {
        "A": ("Official figure", ("official",)),
        "B": ("Time series break", ("series-break",)),
        "E": ("Estimated value", ("estimated",)),
        "I": ("Imputed value", ("imputed",)),
        "M": ("Missing value (data cannot exist, not applicable)", ("missing", "not-applicable")),
        "O": ("Missing value", ("missing",)),
        "P": ("Provisional value", ("provisional",)),
        "T": ("Unofficial figure", ("unofficial",)),
        "X": ("Figure from international organizations", ("international-organization",)),
    },
    "eurostat-obs-flags": {
        "b": ("break in time series", ("series-break",)),
        "c": ("confidential", ("confidential",)),
        "d": ("definition differs, see metadata", ("definition-differs",)),
        "e": ("estimated", ("estimated",)),
        "f": ("forecast", ("forecast",)),
        "n": ("not significant", ("not-significant",)),
        "p": ("provisional", ("provisional",)),
        "r": ("revised", ("revised",)),
        "s": ("Eurostat estimate", ("estimated",)),
        "u": ("low reliability", ("low-reliability",)),
        "z": ("not applicable", ("not-applicable",)),
    },
    "nass-value-codes": {
        "(D)": ("Withheld to avoid disclosing data for individual operations", ("withheld",)),
        "(NA)": ("Not available", ("missing",)),
        "(S)": ("Insufficient number of reports to establish an estimate", ("withheld",)),
        "(X)": ("Not applicable", ("not-applicable",)),
        "(Z)": ("Less than half the rounding unit", ("below-rounding",)),
        "(H)": ("Coefficient of variation is greater than or equal to 99.95 percent", ("low-reliability",)),
        "(L)": ("Coefficient of variation is less than 0.05 percent", ("official",)),
    },
    "psd-release-convention": {},
    "agri-food-portal": {},
}
NEVER = (
    "forecast or project yields, production or prices",
    "compute food-security indicators or scores beyond quoting the publisher",
    "blend, sum or average figures across sources or across differently defined commodities",
    "convert marketing years to calendar years or impute missing and withheld values",
    "overwrite a published figure: a revision is a new vintage",
    "infer a causal or correlational link (for example weather to yield)",
)
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


class AgrifoodError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise AgrifoodError("unauthorized", f"{required} and namespace access are required")


def require(scopes: Iterable[str], *required: str) -> None:
    scopes = set(scopes)
    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise AgrifoodError("unauthorized", f"{missing[0]} scope is required")


def label_key(value: Any) -> str:
    """Exact-label comparison key: case-folded, whitespace collapsed. Never a similarity measure."""
    return " ".join(str(value or "").casefold().split())


def decimal_text(value: Any) -> str | None:
    """A published number as plain decimal text (thousands separators and currency signs removed), else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:  # NaN
            return None
        number = Decimal(str(value))
    else:
        text = re.sub(r"[\s,€$£]", "", str(value))
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
    if not number.is_finite():
        return None
    text = format(number.normalize(), "f")
    return text if _NUMBER.fullmatch(text) else None


def flag(vocabulary: str, code: Any, *, label: Any = None, note: Any = None,
         default_classes: tuple[str, ...] = ("not-flagged",)) -> dict[str, Any]:
    """A source flag verbatim with its classes. Eurostat combines letters (``ep``): each letter is classified."""
    text = None if code in (None, "") else str(code).strip()
    known = FLAG_VOCABULARIES.get(vocabulary, {})
    classes: list[str] = []
    published_label = None if label in (None, "") else str(label)
    if text is None:
        classes = list(default_classes)
    elif text in known:
        classes = list(known[text][1])
        published_label = published_label or known[text][0]
    elif vocabulary == "eurostat-obs-flags" and all(ch in known for ch in text):
        for ch in text:
            classes += [c for c in known[ch][1] if c not in classes]
        published_label = published_label or "; ".join(known[ch][0] for ch in text)
    else:
        classes = ["unknown"]
    return {"vocabulary": vocabulary, "code": text, "label": published_label, "classes": classes,
            "note": None if note in (None, "") else str(note)}


def series_key(provider: str, dataset: str, commodity: Mapping[str, Any], place: Mapping[str, Any],
               measure: Mapping[str, Any], unit: Any, period_type: str, *, market: Any = None,
               program: Any = None) -> str:
    """The identity of one published series: never shared across publishers, units or period types."""
    return "agrifood-series:" + digest([provider, dataset, commodity["scheme"], commodity["code"], place["scheme"],
                                        place["code"], market, program, measure["element"],
                                        measure.get("element_code"), unit, period_type])[:24]


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema/noesis-agrifood-record-v1.json"
    return json.loads(path.read_text())


FORBIDDEN_KEYS = frozenset({"forecast_by_pack", "projection_by_pack", "food_security_score", "score", "risk",
                            "risk_score", "blended", "aggregate_across_sources", "imputed_by_pack", "verdict"})


def forbidden_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.add(str(key))
            found |= forbidden_keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= forbidden_keys(item)
    return found


def validate_statement(statement: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one statement against the contract; reject derived scores and inconsistent values."""
    import jsonschema

    value = json.loads(canonical(statement))
    if value.get("contract") != CONTRACT:
        raise AgrifoodError("invalid_record", "statement is not a noesis-agrifood-record-v1 statement")
    bad = forbidden_keys(value)
    if bad:
        raise AgrifoodError("invalid_record", f"statement carries excluded content: {sorted(bad)}")
    try:
        jsonschema.validate(value, _schema())
    except jsonschema.ValidationError as exc:
        raise AgrifoodError("invalid_record", f"schema: {exc.message}") from exc
    if value["record_type"] in FIGURE_TYPES:
        figure = value["value"]
        if figure["status"] == "reported" and figure["number"] is None:
            raise AgrifoodError("invalid_record", "a reported figure carries its number")
        if figure["status"] != "reported" and figure["number"] is not None:
            raise AgrifoodError("invalid_record", "a withheld, missing or not-applicable figure is never a number")
        expected = series_key(value["provider"], value["dataset"], value["commodity"], value["place"],
                              value["measure"], value["unit"], value["period"]["type"], market=value.get("market"),
                              program=value.get("program"))
        if value["series_key"] != expected:
            raise AgrifoodError("invalid_record", "series_key does not match the series identity")
    return value


def figure(record_type: str, provider: str, dataset: str, *, commodity: Mapping[str, Any],
           place: Mapping[str, Any], measure: Mapping[str, Any], unit: Any, period: Mapping[str, Any],
           value: Mapping[str, Any], flag_value: Mapping[str, Any], estimate_type: str, release: Mapping[str, Any],
           as_published: Mapping[str, Any], source: Mapping[str, Any], market: Any = None,
           program: Any = None) -> dict[str, Any]:
    """Build (and validate) one observation or food-balance statement."""
    statement = {
        "contract": CONTRACT, "record_type": record_type, "provider": provider, "dataset": dataset,
        "commodity": dict(commodity), "place": dict(place), "market": market, "program": program,
        "measure": dict(measure), "unit": None if unit in (None, "") else str(unit), "period": dict(period),
        "value": dict(value), "flag": dict(flag_value), "estimate_type": estimate_type, "release": dict(release),
        "as_published": dict(as_published), "source": dict(source),
    }
    statement["series_key"] = series_key(provider, dataset, statement["commodity"], statement["place"],
                                         statement["measure"], statement["unit"], statement["period"]["type"],
                                         market=market, program=program)
    return validate_statement(statement)


def declaration(record_type: str, provider: str, dataset: str, *, source: Mapping[str, Any],
                commodity: Mapping[str, Any] | None = None, release: Mapping[str, Any] | None = None,
                as_published: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build (and validate) a commodity or release statement."""
    statement: dict[str, Any] = {"contract": CONTRACT, "record_type": record_type, "provider": provider,
                                 "dataset": dataset, "source": dict(source)}
    if commodity is not None:
        statement["commodity"] = dict(commodity)
    if release is not None:
        statement["release"] = dict(release)
    if as_published is not None:
        statement["as_published"] = dict(as_published)
    return validate_statement(statement)


def schema_versions() -> dict[str, str]:
    return dict(SCHEMA_VERSIONS)


__all__ = [
    "CONTRACT", "CROSSWALK_CONTRACT", "ESTIMATE_TYPES", "FIGURE_TYPES", "FLAG_CLASSES", "FLAG_VOCABULARIES",
    "LINK_CONTRACT", "MEASURE_KINDS", "NEVER", "NOTIFICATION_CONTRACT", "PERIOD_TYPES", "PROVIDERS", "READ_SCOPE",
    "RECORD_TYPES", "REVIEW_SCOPE", "SCHEMA_VERSIONS", "SERIES_CONTRACT", "SOURCE_PACK", "VALUE_STATUSES",
    "WRITE_SCOPE", "AgrifoodError", "authorize", "canonical", "decimal_text", "declaration", "digest", "figure",
    "flag", "forbidden_keys", "label_key", "require", "schema_versions", "series_key", "validate_statement",
]
