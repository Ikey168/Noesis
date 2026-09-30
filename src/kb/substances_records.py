"""Substance record model for the Chemicals and Substances pack (#2212, CH02 #2284).

``noesis-substance-record-v1`` is the provider-neutral statement every
substance adapter emits (``src/ingestion/substance_sources.py``) and
:class:`src.kb.substances_store.SubstanceStore` keeps as immutable revisions:

* **substance** - one provider's record of a substance (a PubChem CID, an
  ECHA substance or group entry, a CompTox DTXSID) with its kind
  (single- or multi-component, group, mixture, salt, isomer) as published;
* **identifier** - CAS, EC, index number, InChI/InChIKey, CID, DTXSID,
  names and synonyms exactly as the provider publishes them; conflicting
  identifiers from different depositors are all kept and flagged, never
  resolved;
* **classification** - a harmonised (CLP Annex VI) or notified (C&L
  inventory) classification. Each ATP that introduced or amended it is a
  separate, dated **classification-revision** (a store revision of the
  classification record) holding hazard class/category and H-statements as
  published with the legal act, so the prior classification is always kept;
* **registration** - REACH registration status as published (never dossier
  contents);
* **candidate_listing** - an SVHC Candidate List inclusion, amendment or
  removal, dated, with the reason as published;
* **restriction** / **authorisation** - REACH Annex XVII / XIV entries with
  entry number, conditions text verbatim, sunset and latest application
  dates, and the cited legal act;
* **data_point** - a toxicity value as published (endpoint, value, unit,
  study reference, source and data version), labelled as the source's data.

Revisions are new records, never overwrites; removals and amendments are
dated events. Nothing here derives a hazard score, a risk or exposure
conclusion, safety advice, or anything about synthesis or preparation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTRACT = "noesis-substance-record-v1"
IDENTITY_CONTRACT = "noesis-substance-identity-candidate-v1"
LINK_CONTRACT = "noesis-substance-link-v1"
DOSSIER_CONTRACT = "noesis-substance-dossier-v1"
STATUS_CONTRACT = "noesis-substance-status-v1"
READ_SCOPE = "knowledge:substances:read"
WRITE_SCOPE = "knowledge:substances:write"
REVIEW_SCOPE = "knowledge:substances:review"
SOURCE_PACK = "chemicals-substances"
# Schema versions registered for the pack (packs/chemicals/pack.json ``schema_versions``).
SCHEMA_VERSIONS = {
    "substance-record": "1.0.0",
    "substance-identity": "1.0.0",
    "substance-link": "1.0.0",
    "substance-dossier": "1.0.0",
    "source-pack": "1.0.0",
}
RECORD_TYPES = ("substance", "identifier", "classification", "registration", "candidate_listing", "restriction",
                "authorisation", "data_point")
# Views over store revisions: a classification revision is one revision of a classification record.
VIEW_TYPES = {"classification": "classification_revision"}
PROVIDERS = ("pubchem", "echa-clp", "echa-reach", "comptox")
SUBJECT_KINDS = ("single-component", "multi-component", "group", "mixture", "salt", "isomer", "unknown")
# Kinds that are never grouped with another record without an explicit, reviewed match.
COMPOSITE_KINDS = frozenset({"multi-component", "group", "mixture", "salt", "isomer"})
EVENTS = ("published", "inclusion", "amendment", "removal", "notified", "status")
IDENTIFIER_SCHEMES = ("cid", "cas", "ec", "index", "inchi", "inchikey", "dtxsid", "dtxcid", "echa-substance-id",
                      "preferred-name", "iupac-name", "synonym")
STRUCTURAL_SCHEMES = frozenset({"cid", "cas", "ec", "index", "inchi", "inchikey", "dtxsid", "dtxcid",
                                "echa-substance-id"})
NAME_SCHEMES = frozenset({"preferred-name", "iupac-name", "synonym"})
DATA_POINT_LABEL = "the source's published data point, quoted as published; not a hazard or exposure conclusion"
# Keys that must never appear in a statement: derived verdicts and synthesis or preparation content.
FORBIDDEN_KEYS = frozenset({
    "hazard_score", "risk_score", "risk", "verdict", "safe", "unsafe", "is_safe", "safety_advice", "advice",
    "recommendation", "exposure_assessment", "risk_characterisation", "synthesis", "synthesis_route",
    "preparation", "preparation_method", "reaction", "reactions", "precursor", "precursors",
})
NEVER = (
    "derive a hazard score, a risk or exposure conclusion or safety advice",
    "combine, rank or average published data points",
    "acquire or return synthesis, preparation or reaction content",
    "infer a classification, restriction or identity match",
    "report a substance without entries as safe or unregulated",
)

_CAS = re.compile(r"^(\d{2,7})-(\d{2})-(\d)$")
_EC = re.compile(r"^(\d{3})-(\d{3})-(\d)$")
_INDEX = re.compile(r"^\d{3}-\d{3}-\d{2}-\d$")
_INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
_DTXSID = re.compile(r"^DTXSID\d{7,12}$")
_DTXCID = re.compile(r"^DTXCID\d{7,12}$")


class SubstanceError(ValueError):
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
        raise SubstanceError("unauthorized", f"{required} and namespace access are required")


def require(scopes: Iterable[str], *required: str) -> None:
    scopes = set(scopes)
    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise SubstanceError("unauthorized", f"{missing[0]} scope is required")


# ------------------------------------------------------------------ identifiers


def cas_valid(value: Any) -> bool:
    """CAS Registry Number check digit (weighted sum of the digits, right to left, mod 10)."""
    match = _CAS.fullmatch(str(value or "").strip())
    if not match:
        return False
    digits = (match.group(1) + match.group(2))[::-1]
    return sum((i + 1) * int(d) for i, d in enumerate(digits)) % 10 == int(match.group(3))


def ec_valid(value: Any) -> bool:
    """EC (EINECS/ELINCS/NLP) number check digit: weights 1..6 over the first six digits, mod 11."""
    match = _EC.fullmatch(str(value or "").strip())
    if not match:
        return False
    digits = match.group(1) + match.group(2)
    check = sum((i + 1) * int(d) for i, d in enumerate(digits)) % 11
    return check != 10 and check == int(match.group(3))


def name_key(value: Any) -> str:
    """Exact-name comparison key: case-folded, whitespace collapsed. Never a similarity measure."""
    return " ".join(str(value or "").casefold().split())


def identifier_key(scheme: str, value: Any) -> str | None:
    """The comparison key of an identifier, or None when the value is not well formed for its scheme."""
    text = str(value or "").strip()
    if not text:
        return None
    if scheme == "cas":
        return text if cas_valid(text) else None
    if scheme == "ec":
        return text if ec_valid(text) else None
    if scheme == "index":
        return text if _INDEX.fullmatch(text) else None
    if scheme == "inchikey":
        return text.upper() if _INCHIKEY.fullmatch(text.upper()) else None
    if scheme == "dtxsid":
        return text.upper() if _DTXSID.fullmatch(text.upper()) else None
    if scheme == "dtxcid":
        return text.upper() if _DTXCID.fullmatch(text.upper()) else None
    if scheme == "cid":
        return text if text.isdigit() and not text.startswith("0") else None
    if scheme == "inchi":
        return text if text.startswith("InChI=") else None
    if scheme in NAME_SCHEMES:
        return name_key(text) or None
    return text


def classify_query(value: Any) -> tuple[str, str] | None:
    """(scheme, key) of a free-text query when it is a well-formed identifier; names return ('name', key)."""
    text = str(value or "").strip()
    if not text:
        return None
    for scheme in ("cas", "ec", "index", "inchikey", "dtxsid"):
        key = identifier_key(scheme, text)
        if key:
            return scheme, key
    if text.upper().startswith("CID:") and identifier_key("cid", text[4:]):
        return "cid", text[4:]
    return "name", name_key(text)


# ------------------------------------------------------------------ validation


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema/noesis-substance-record-v1.json"
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
    """Validate one statement against the contract; reject verdicts, synthesis content and malformed identifiers."""
    import jsonschema

    value = json.loads(canonical(statement))
    if value.get("contract") != CONTRACT:
        raise SubstanceError("invalid_record", "statement is not a noesis-substance-record-v1 statement")
    bad = forbidden_keys(value)
    if bad:
        raise SubstanceError("invalid_record", f"statement carries excluded content: {sorted(bad)}")
    try:
        jsonschema.validate(value, _schema())
    except jsonschema.ValidationError as exc:
        raise SubstanceError("invalid_record", f"schema: {exc.message}") from exc
    if value["record_type"] == "identifier":
        published = value["as_published"]
        if published["scheme"] in STRUCTURAL_SCHEMES and not identifier_key(published["scheme"], published["value"]):
            # Kept as published, but flagged: a malformed number is never used for matching.
            if not published.get("malformed"):
                raise SubstanceError("invalid_record", f"malformed {published['scheme']} must be flagged malformed")
    if value["record_type"] == "data_point" and value.get("label") != DATA_POINT_LABEL:
        raise SubstanceError("invalid_record", "data points carry the source-data label")
    return value


def statement(record_type: str, provider: str, subject: Mapping[str, Any], record_key: str,
              as_published: Mapping[str, Any], *, source: Mapping[str, Any], event: str = "published",
              effective_from: str | None = None, effective_to: str | None = None, date_basis: str | None = None,
              legal_act: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build (and validate) one statement."""
    value: dict[str, Any] = {
        "contract": CONTRACT, "record_type": record_type, "provider": provider,
        "subject": {"key": subject["key"], "kind": subject.get("kind") or "unknown", "name": subject.get("name")},
        "record_key": record_key, "as_published": dict(as_published),
        "effective": {"from": effective_from, "to": effective_to, "event": event, "date_basis": date_basis},
        "source": dict(source),
    }
    if legal_act is not None or record_type in {"classification", "restriction", "authorisation"}:
        value["legal_act"] = None if legal_act is None else dict(legal_act)
    if record_type == "data_point":
        value["label"] = DATA_POINT_LABEL
    return validate_statement(value)


def schema_versions() -> dict[str, str]:
    return dict(SCHEMA_VERSIONS)


__all__ = [
    "CONTRACT", "COMPOSITE_KINDS", "DATA_POINT_LABEL", "DOSSIER_CONTRACT", "EVENTS", "IDENTIFIER_SCHEMES",
    "IDENTITY_CONTRACT", "LINK_CONTRACT", "NAME_SCHEMES", "NEVER", "PROVIDERS", "READ_SCOPE", "RECORD_TYPES",
    "REVIEW_SCOPE", "SCHEMA_VERSIONS", "SOURCE_PACK", "STATUS_CONTRACT", "STRUCTURAL_SCHEMES", "SUBJECT_KINDS",
    "SubstanceError", "VIEW_TYPES", "WRITE_SCOPE", "authorize", "canonical", "cas_valid", "classify_query", "digest",
    "ec_valid", "forbidden_keys", "identifier_key", "name_key", "require", "schema_versions", "statement",
    "validate_statement",
]
