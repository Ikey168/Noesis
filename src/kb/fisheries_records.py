"""Fisheries record model: authorisations, IUU listings, effort aggregates and catch observations (#2222, FI02 #2307).

``noesis-fisheries-record-v1`` is the provider-neutral statement every
Fisheries adapter (``src/ingestion/fisheries_sources.py``) emits and
:class:`src.kb.fisheries_store.FisheriesStore` keeps as immutable revisions:

* **vessel** - a Global Fishing Watch vessel identity segment (self-reported
  name, flag, call sign, IMO, MMSI and the transmission period) keyed by the
  GFW vessel id and dataset version;
* **authorisation** - one entry of an RFMO authorised-vessel register
  (register, register number, vessel, flag, call sign, IMO, gear, validity
  period, owner or operator as published);
* **listing** - one entry of an RFMO IUU vessel list or of the Combined IUU
  Vessel List (list body, entry, listing and delisting dates, the stated
  reason *verbatim*, previous identities, and for the combined list the
  originating RFMO listings it cites);
* **effort_aggregate** - a published fishing-effort aggregate (area, grid
  cell and resolution, period, flag, gear, unit, value and the publisher's
  method note: GFW's *apparent* fishing effort, never confirmed fishing);
* **catch_observation** - a FishStat capture statistic (FAO major area,
  ASFIS species, country or flag, year, quantity, unit and FAO status flags)
  tied to the release that published it.

Revisions are new records, never overwrites: a newer snapshot or release
supersedes an older one without deleting it, and a vessel absent from a later
register snapshot gets a dated ``removed`` revision. Nothing here derives an
"illegal", "compliant" or enforcement status: listing reasons are quoted, and
no status field is computed for unlisted vessels.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTRACT = "noesis-fisheries-record-v1"
IDENTITY_CONTRACT = "noesis-fisheries-identity-v1"
LINK_CONTRACT = "noesis-fisheries-link-v1"
STATUS_CONTRACT = "noesis-fisheries-vessel-status-v1"
AGGREGATE_CONTRACT = "noesis-fisheries-aggregates-v1"
READ_SCOPE = "knowledge:fisheries:read"
WRITE_SCOPE = "knowledge:fisheries:write"
REVIEW_SCOPE = "knowledge:fisheries:review"
SOURCE_PACK = "fisheries-maritime"
SCHEMA_VERSIONS = {
    "fisheries-record": "1.0.0",
    "fisheries-identity": "1.0.0",
    "fisheries-link": "1.0.0",
    "fisheries-answer": "1.0.0",
    "source-pack": "1.0.0",
}
RECORD_TYPES = ("vessel", "authorisation", "listing", "effort_aggregate", "catch_observation")
PROVIDERS = ("gfw", "fao-fishstat", "iccat", "wcpfc", "iotc", "combined-iuu")
RFMOS = ("iccat", "wcpfc", "iotc")
SUBJECT_KINDS = ("vessel", "area")
EVENTS = ("published", "authorised", "amended", "removed", "listed", "delisted", "release")
LIST_KINDS = ("authorised-vessels", "iuu-vessels")
METHOD_NOTE_GFW = ("apparent fishing effort estimated by Global Fishing Watch from AIS positions with a machine-"
                   "learning model, as published; an aggregate estimate, not confirmed fishing activity")
# Keys that must never appear in a statement: derived legality verdicts, inferred IUU status or enforcement advice.
FORBIDDEN_KEYS = frozenset({
    "illegal", "is_illegal", "legal", "is_legal", "legality", "legal_status", "iuu_status", "is_iuu",
    "inferred_iuu", "suspected_illegal", "compliant", "is_compliant", "compliance", "verdict", "risk", "risk_score",
    "enforcement", "enforcement_action", "recommendation", "advice", "confirmed_fishing",
})
NEVER = (
    "infer illegal fishing from movement or effort patterns",
    "derive an illegal, compliant or legal status for any vessel",
    "recommend or imply an enforcement action",
    "report a vessel without records as legal, compliant or authorised",
    "present apparent fishing effort as confirmed fishing",
    "combine effort and catch into a derived indicator",
    "count a Combined IUU Vessel List entry as an independent confirmation of its originating listing",
    "store per-vessel tracks or positions",
)

_IMO = re.compile(r"^(?:IMO)?(\d{7})$", re.IGNORECASE)
_ISO3 = re.compile(r"^[A-Z]{3}$")
_ASFIS = re.compile(r"^[A-Z]{3}$")
_FAO_AREA = re.compile(r"^\d{2}(?:\.\d+[a-z]?)*$")


class FisheriesError(ValueError):
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
        raise FisheriesError("unauthorized", f"{required} and namespace access are required")


def require(scopes: Iterable[str], *required: str) -> None:
    scopes = set(scopes)
    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise FisheriesError("unauthorized", f"{missing[0]} scope is required")


# ------------------------------------------------------------------ identifiers


def imo_key(value: Any) -> str | None:
    """The 7-digit IMO ship number when well formed and its check digit verifies; else None."""
    match = _IMO.fullmatch(str(value or "").strip().replace(" ", ""))
    if not match:
        return None
    digits = match.group(1)
    check = sum(int(d) * w for d, w in zip(digits[:6], range(7, 1, -1))) % 10
    return digits if check == int(digits[6]) else None


def name_key(value: Any) -> str:
    """Exact-name comparison key: case-folded, punctuation dropped, whitespace collapsed. Never similarity."""
    return " ".join(re.sub(r"[^\w\s]", " ", str(value or "").casefold()).split())


def call_sign_key(value: Any) -> str | None:
    text = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    return text or None


def flag_code(value: Any) -> str | None:
    """A published flag as an ISO 3166 alpha-2 code when it is an ISO alpha-2/alpha-3 code; names stay unresolved."""
    from src.ingestion.connectors.dataset.normalize import normalize_geography

    text = str(value or "").strip()
    if len(text) == 2 and text.isalpha():
        return text.upper()
    if len(text) == 3 and text.isalpha():
        code = normalize_geography(text)
        return code if code and len(code) == 2 else None
    return None


def area_key(scheme: str, code: Any) -> str | None:
    text = str(code or "").strip()
    if not text:
        return None
    if scheme == "fao-major-area":
        return text if _FAO_AREA.fullmatch(text) else None
    return text


def species_key(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    return text if _ASFIS.fullmatch(text) else None


# ------------------------------------------------------------------ validation


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema/noesis-fisheries-record-v1.json"
    return json.loads(path.read_text())


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
    """Validate one statement: contract schema, no derived legality/IUU fields, method note on effort."""
    import jsonschema

    value = json.loads(canonical(statement))
    if value.get("contract") != CONTRACT:
        raise FisheriesError("invalid_record", "statement is not a noesis-fisheries-record-v1 statement")
    bad = forbidden_keys(value)
    if bad:
        raise FisheriesError("invalid_record", f"statement carries a derived status or advice: {sorted(bad)}")
    try:
        jsonschema.validate(value, _schema())
    except jsonschema.ValidationError as exc:
        raise FisheriesError("invalid_record", f"schema: {exc.message}") from exc
    published = value["as_published"]
    if value["record_type"] == "effort_aggregate" and value["provider"] == "gfw" \
            and "not confirmed fishing" not in str(published.get("method") or ""):
        raise FisheriesError("invalid_record", "GFW effort aggregates carry the apparent-fishing method note")
    if value["record_type"] == "listing" and value["provider"] == "combined-iuu" \
            and published.get("independent_confirmation") is not False:
        raise FisheriesError("invalid_record", "a Combined IUU Vessel List entry is never an independent "
                                               "confirmation of its originating listing")
    if published.get("imo") and not published.get("imo_malformed") and imo_key(published["imo"]) is None:
        raise FisheriesError("invalid_record", "an IMO number failing its check digit is flagged imo_malformed")
    return value


def statement(record_type: str, provider: str, subject: Mapping[str, Any], record_key: str,
              as_published: Mapping[str, Any], *, source: Mapping[str, Any], event: str = "published",
              effective_from: str | None = None, effective_to: str | None = None,
              date_basis: str | None = None) -> dict[str, Any]:
    """Build (and validate) one statement."""
    value: dict[str, Any] = {
        "contract": CONTRACT, "record_type": record_type, "provider": provider,
        "subject": {"key": subject["key"], "kind": subject.get("kind") or "vessel", "name": subject.get("name")},
        "record_key": record_key, "as_published": dict(as_published),
        "effective": {"from": effective_from, "to": effective_to, "event": event, "date_basis": date_basis},
        "source": dict(source),
    }
    return validate_statement(value)


def schema_versions() -> dict[str, str]:
    return dict(SCHEMA_VERSIONS)


__all__ = [
    "AGGREGATE_CONTRACT", "CONTRACT", "EVENTS", "FORBIDDEN_KEYS", "FisheriesError", "IDENTITY_CONTRACT",
    "LINK_CONTRACT", "LIST_KINDS", "METHOD_NOTE_GFW", "NEVER", "PROVIDERS", "READ_SCOPE", "RECORD_TYPES",
    "REVIEW_SCOPE", "RFMOS", "SCHEMA_VERSIONS", "SOURCE_PACK", "STATUS_CONTRACT", "SUBJECT_KINDS", "WRITE_SCOPE",
    "area_key", "authorize", "call_sign_key", "canonical", "digest", "flag_code", "forbidden_keys", "imo_key",
    "name_key", "require", "schema_versions", "species_key", "statement", "validate_statement",
]
