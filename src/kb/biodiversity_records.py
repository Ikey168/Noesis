"""Biodiversity records for the Climate and Environment pack: taxa, identities, occurrences, datasets, assessments.

``noesis-biodiversity-record-v1`` (#2220, BD02 #2507) extends the Climate and
Environment record model (:mod:`src.kb.environment_records`: same scopes,
canonical JSON and digest rules, source/revision/as-of on every record) with
five record types, each a *statement* as one provider published it:

* **taxon** - a name usage in one checklist release (Catalogue of Life):
  scientific name, authorship, rank, taxonomic status (accepted, synonym, ...)
  with the accepted name for synonyms, the higher classification and the
  checklist version, all as published;
* **taxon_identity** - a provider's native taxon key (GBIF ``taxonKey``, CoL
  ID, IUCN taxon/SIS ID) with the name, status and any cross-references the
  provider publishes;
* **occurrence** - one GBIF occurrence: basis of record, event date,
  coordinates *as published* with ``coordinateUncertaintyInMeters``, the
  publisher's generalisation and withheld-information text, GBIF issue flags,
  the record licence and the publishing dataset;
* **dataset** - a publishing dataset, checklist release or download: publisher,
  licence, DOI/citation and version;
* **conservation_assessment** - one Red List assessment at citation level (the
  BD01 reference-only decision): category, criteria, assessment date, year
  published, scope (global/regional), the assessor's ``latest`` designation,
  change reason as published, citation and URL. Assessments are append-only.

Nothing here models a range, an abundance or a precise location beyond what the
publisher released: :data:`FORBIDDEN_KEYS` rejects such fields, withheld
coordinates stay ``None`` and unknown values are listed in ``unknowns``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from src.kb.environment_records import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE, canonical, digest

CONTRACT = "noesis-biodiversity-record-v1"
IDENTITY_CONTRACT = "noesis-biodiversity-identity-match-v1"
LINK_CONTRACT = "noesis-biodiversity-link-v1"
OCCURRENCE_ANSWER_CONTRACT = "noesis-biodiversity-occurrences-v1"
STATUS_ANSWER_CONTRACT = "noesis-biodiversity-status-history-v1"
NOTIFICATION_CONTRACT = "noesis-biodiversity-notification-v1"
SOURCE_PACK = "climate-environment-biodiversity"
SCHEMA_VERSIONS = {"biodiversity-record": "1.0.0", "biodiversity-identity-match": "1.0.0",
                   "biodiversity-link": "1.0.0", "biodiversity-answer": "1.0.0"}
RECORD_TYPES = ("taxon", "taxon_identity", "occurrence", "dataset", "conservation_assessment")
PROVIDERS = ("col", "gbif", "iucn")
SUBJECT_KINDS = {"taxon": "taxon", "taxon_identity": "taxon", "occurrence": "occurrence", "dataset": "dataset",
                 "conservation_assessment": "assessment"}
EVENTS = ("published", "removed")
KEY_SCHEMES = {"gbif": "gbif-taxon-key", "col": "col-id", "iucn": "iucn-sis-id"}
DATASET_KINDS = ("occurrence", "checklist", "checklist-release", "download")
STATUS_CLASSES = {
    "accepted": "accepted", "provisionally accepted": "accepted", "doubtful": "other",
    "synonym": "synonym", "ambiguous synonym": "synonym", "misapplied": "synonym",
    "heterotypic synonym": "synonym", "homotypic synonym": "synonym", "proparte synonym": "synonym",
    "bare name": "other",
}
# Red List categories as published (current and the 1994 lower-risk subcategories, regional RE/NA).
IUCN_CATEGORIES = {
    "EX": "Extinct", "EW": "Extinct in the Wild", "RE": "Regionally Extinct", "CR": "Critically Endangered",
    "EN": "Endangered", "VU": "Vulnerable", "NT": "Near Threatened", "LC": "Least Concern", "DD": "Data Deficient",
    "NE": "Not Evaluated", "NA": "Not Applicable", "LR/cd": "Lower Risk/conservation dependent",
    "LR/nt": "Lower Risk/near threatened", "LR/lc": "Lower Risk/least concern",
}
NOT_ASSESSED = "not assessed on record"
LICENCE_TIER_IUCN = "reference-only"
IUCN_REDISTRIBUTION = ("IUCN Red List data: non-commercial use; citation-level facts only (reference-only licence "
                       "decision, BD01); not for redistribution or derivative datasets without IUCN permission")
# Fields that would turn records into modelled, abundance or Noesis-derived status outputs: never accepted.
FORBIDDEN_KEYS = frozenset({
    "abundance", "density", "population_estimate", "presence", "absence", "presence_absence", "range_model",
    "modelled_range", "distribution_model", "suitability", "predicted", "prediction", "trend", "risk", "risk_score",
    "threat_score", "derived_status", "noesis_status", "precise_location", "degeneralised_coordinates",
})
NEVER = (
    "model species distributions or ranges",
    "estimate abundance, density or presence/absence",
    "de-generalise or geocode sensitive-species locations beyond what the publisher released",
    "derive a threat status, trend or risk score",
    "choose the current assessment instead of the assessor",
    "merge taxonomic concepts without a reviewed identity decision",
)
NEVER_SENTENCE = ("Records as each checklist, GBIF publisher and the IUCN Red List published them: no distribution "
                  "modelling, no abundance or presence/absence, no location more precise than the publisher "
                  "released, and no Noesis-derived threat status.")

_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # record type: (required non-null, optional - null goes to unknowns)
    "dataset": (("dataset_key", "kind", "title"),
                ("publisher", "licence", "doi", "citation", "version", "released", "cited_references",
                 "total_records")),
    "taxon": (("native_id", "scientific_name", "rank", "status", "status_class", "checklist"),
              ("authorship", "accepted", "classification")),
    "taxon_identity": (("key_scheme", "native_key", "scientific_name", "status", "status_class"),
                       ("authorship", "rank", "accepted_key", "accepted_name", "cross_references", "checklist")),
    "occurrence": (("gbif_id", "dataset_key", "generalisation"),
                   ("occurrence_id", "basis_of_record", "event_date", "taxon_key", "accepted_taxon_key",
                    "scientific_name", "coordinates", "coordinate_uncertainty_m", "coordinate_precision",
                    "country_code", "state_province", "locality", "issues", "licence", "modified")),
    "conservation_assessment": (("assessment_id", "taxon_id", "category", "scope", "year_published", "latest",
                                 "licence_tier"),
                                ("scientific_name", "criteria", "assessment_date", "possibly_extinct",
                                 "possibly_extinct_in_wild", "change_reason", "citation", "url")),
}
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?)?)?"
                   r"(/\d{4}(-\d{2}(-\d{2})?)?)?$")
_PRECISION = (
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:km|kilomet)", re.I), 1000.0),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:deg|degree|°)", re.I), 111_320.0),
    (re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:m|meter|metre)s?\b", re.I), 1.0),
)


class BiodiversityError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    """Environment scope plus namespace access (operator bypasses), as for every environment record."""
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise BiodiversityError("unauthorized", f"{required} and namespace access are required")


def require(scopes: Iterable[str], *required: str) -> None:
    scopes = set(scopes or ())
    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise BiodiversityError("unauthorized", f"{missing[0]} scope is required")


def _fail(message: str, code: str = "invalid_biodiversity_record") -> None:
    raise BiodiversityError(code, message)


# ------------------------------------------------------------------ normalisers


def name_key(name: Any) -> str:
    """Exact scientific-name comparison key: whitespace collapsed, case kept (names are case-sensitive)."""
    return " ".join(str(name or "").split())


def authorship_key(value: Any) -> str:
    """Authorship compared exactly after folding spaces, brackets and punctuation around years."""
    text = re.sub(r"[\s.,&]+", " ", str(value or "")).strip()
    return text.replace("( ", "(").replace(" )", ")").casefold()


def status_class(status: Any) -> str:
    return STATUS_CLASSES.get(str(status or "").strip().casefold().replace("_", " "), "other")


def licence(published: Any) -> dict[str, Any]:
    """A published licence (URL, SPDX-like id or text) to its id and use terms; unknown text is kept, not guessed."""
    text = str(published or "").strip()
    folded = text.casefold().replace("_", "-")
    if not text:
        return {"id": None, "published": None, "commercial_use": None, "attribution_required": None}
    if "zero" in folded or folded in {"cc0", "cc0-1.0", "cc0 1.0"} or "publicdomain" in folded:
        return {"id": "CC0-1.0", "published": text, "commercial_use": True, "attribution_required": False}
    if "by-nc" in folded or "by nc" in folded:
        return {"id": "CC-BY-NC-4.0", "published": text, "commercial_use": False, "attribution_required": True}
    if "/by/" in folded or folded in {"cc-by-4.0", "cc by 4.0", "cc-by", "cc by"} or folded.startswith("cc by 4"):
        return {"id": "CC-BY-4.0", "published": text, "commercial_use": True, "attribution_required": True}
    return {"id": None, "published": text, "commercial_use": None, "attribution_required": None}


def generalisation(*, data_generalizations: Any = None, information_withheld: Any = None,
                   coordinates_published: bool, issues: Iterable[str] = ()) -> dict[str, Any]:
    """The publisher's generalisation as published, plus the precision its text states (never inferred)."""
    stated = str(data_generalizations).strip() if data_generalizations not in (None, "") else None
    withheld = str(information_withheld).strip() if information_withheld not in (None, "") else None
    precision = None
    for pattern, factor in _PRECISION:
        match = pattern.search(stated or "")
        if match:
            precision = round(float(match.group(1).replace(",", ".")) * factor, 3)
            break
    coordinates_withheld = not coordinates_published and bool(
        withheld and re.search(r"coordinat|locat|geo", withheld, re.I))
    generalised = bool(stated) or "COORDINATE_ROUNDED" in set(issues)
    return {"generalised": generalised, "data_generalizations": stated, "information_withheld": withheld,
            "precision_m": precision, "coordinates_withheld": coordinates_withheld,
            "sensitivity": ("generalised by the publisher" if generalised else
                            "coordinates withheld by the publisher" if coordinates_withheld else
                            "none stated by the publisher"),
            "policy": "stored at the published precision only; never de-generalised or geocoded"}


def parse_generalisation_precision(text: Any) -> float | None:
    return generalisation(data_generalizations=text, coordinates_published=True)["precision_m"]


# ------------------------------------------------------------------ statements


def _text(value: Any, field: str, *, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value


def _date(value: Any, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not _DATE.fullmatch(value)):
        _fail(f"{field} must be an ISO-8601 date, instant or date range as published")


def _forbidden(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                _fail(f"{path}{key} is a modelled, abundance or derived-status field and is never stored",
                      "forbidden_field")
            _forbidden(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            _forbidden(item, path)


def _typed(record_type: str, published: Mapping[str, Any]) -> None:
    if record_type == "dataset":
        if published["kind"] not in DATASET_KINDS:
            _fail(f"dataset kind must be one of {', '.join(DATASET_KINDS)}")
        _date(published.get("released"), "released")
        if not isinstance(published.get("cited_references") or [], list):
            _fail("cited_references is a list")
    elif record_type == "taxon":
        checklist = published["checklist"]
        if not isinstance(checklist, Mapping) or not checklist.get("dataset_key") or not checklist.get("version"):
            _fail("a taxon names the checklist release (dataset key and version) that published it")
        _date(checklist.get("released"), "checklist.released")
        if published["status_class"] == "synonym" and not published.get("accepted"):
            _fail("a synonym keeps the accepted name the checklist states")
    elif record_type == "taxon_identity":
        if published["key_scheme"] not in KEY_SCHEMES.values():
            _fail("key_scheme is gbif-taxon-key, col-id or iucn-sis-id")
        for ref in published.get("cross_references") or []:
            if not isinstance(ref, Mapping) or not ref.get("scheme") or not ref.get("value"):
                _fail("cross references state a scheme and a value as published")
    elif record_type == "occurrence":
        _date(published.get("event_date"), "event_date")
        coordinates = published.get("coordinates")
        if coordinates is not None:
            lat, lon = (coordinates or {}).get("latitude"), (coordinates or {}).get("longitude")
            if type(lat) not in (int, float) or type(lon) not in (int, float) or not -90 <= lat <= 90 \
                    or not -180 <= lon <= 180:
                _fail("coordinates are WGS84 latitude/longitude as published")
        uncertainty = published.get("coordinate_uncertainty_m")
        if uncertainty is not None and (type(uncertainty) not in (int, float) or uncertainty < 0):
            _fail("coordinate_uncertainty_m is a non-negative distance as published")
        flags = published["generalisation"]
        if not isinstance(flags, Mapping) or "generalised" not in flags:
            _fail("occurrences keep the publisher's generalisation flags")
        if flags.get("coordinates_withheld") and coordinates is not None:
            _fail("withheld coordinates stay withheld", "withheld_coordinates")
    elif record_type == "conservation_assessment":
        if published["category"] not in IUCN_CATEGORIES:
            _fail("category is a Red List category code as published")
        scope = published["scope"]
        if not isinstance(scope, Mapping) or scope.get("kind") not in {"global", "regional"} or not scope.get("label"):
            _fail("scope keeps the published label and whether it is global or regional")
        if type(published["latest"]) is not bool:
            _fail("latest is the assessor's own designation (true/false)")
        if published["licence_tier"] != LICENCE_TIER_IUCN:
            _fail("IUCN assessments are stored at the reference-only licence tier")
        _date(published.get("assessment_date"), "assessment_date")


def statement(record_type: str, provider: str, record_key: str, *, subject_name: str | None, as_published: Mapping,
              source: Mapping[str, Any], event: str = "published", effective_date: str | None = None,
              date_basis: str | None = None, unknowns: Iterable[str] = ()) -> dict[str, Any]:
    """Build and validate one provider statement."""
    if record_type not in RECORD_TYPES:
        _fail("unknown biodiversity record type")
    if provider not in PROVIDERS:
        _fail("unknown biodiversity provider")
    _text(record_key, "record_key", limit=500)
    if event not in EVENTS:
        _fail("event is published or removed")
    required, optional = _FIELDS[record_type]
    published = dict(as_published)
    extra = set(published) - set(required) - set(optional)
    if extra:
        _fail(f"unsupported {record_type} field {sorted(extra)[0]}")
    _forbidden(published)
    for field in required:
        if published.get(field) is None:
            _fail(f"{record_type}.{field} is required as published")
    _typed(record_type, published)
    if not isinstance(source, Mapping) or not str(source.get("url") or "").startswith("https://"):
        _fail("source.url must be the provider's https URL")
    _date(effective_date, "effective.date")
    missing = {f for f in optional if published.get(f) is None}
    return {
        "contract": CONTRACT, "record_type": record_type, "provider": provider, "record_key": record_key,
        "subject": {"key": subject_key(record_type, provider, record_key), "kind": SUBJECT_KINDS[record_type],
                    "name": subject_name},
        "as_published": {**{f: None for f in optional}, **published},
        "effective": {"event": event, "date": effective_date, "date_basis": date_basis},
        "source": dict(source), "unknowns": sorted(missing | set(unknowns)),
    }


def subject_key(record_type: str, provider: str, record_key: str) -> str:
    if record_type in {"taxon", "taxon_identity"}:
        return f"{provider}:{record_key}"
    if record_type == "occurrence":
        return f"gbif-occurrence:{record_key}"
    if record_type == "dataset":
        return f"{provider}-dataset:{record_key}"
    return f"iucn-assessment:{record_key}"


def validate_statement(value: Mapping[str, Any]) -> dict[str, Any]:
    """Re-validate a stored or received statement by rebuilding it (round trip)."""
    if not isinstance(value, Mapping) or value.get("contract") != CONTRACT:
        _fail("not a noesis-biodiversity-record-v1 statement")
    required, optional = _FIELDS.get(value.get("record_type"), ((), ()))
    published = {k: v for k, v in dict(value.get("as_published") or {}).items()
                 if k in required or v is not None}
    effective = dict(value.get("effective") or {})
    return statement(value.get("record_type"), value.get("provider"), value.get("record_key"),
                     subject_name=dict(value.get("subject") or {}).get("name"), as_published=published,
                     source=dict(value.get("source") or {}), event=effective.get("event", "published"),
                     effective_date=effective.get("date"), date_basis=effective.get("date_basis"),
                     unknowns=value.get("unknowns") or ())


# ------------------------------------------------------------------ schema registry

SCHEMA_FILES = {"noesis-biodiversity-record": "contracts/schemas/jsonschema/noesis-biodiversity-record-v1.json"}


def schema_definitions(root: Any = None) -> dict[str, Any]:
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    return {name: json.loads((root / path).read_text()) for name, path in SCHEMA_FILES.items()}


def register_schemas(conn: Any, *, principal_id: str, scopes: Iterable[str], root: Any = None) -> list[dict]:
    """Register the biodiversity record schema in the existing schema registry (idempotent per version)."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": content, "owner": "environment.biodiversity", "dependencies": [],
            "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/climate-environment (biodiversity feature)"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"biodiversity-schema:{name}:1.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


__all__ = [
    "CONTRACT", "FORBIDDEN_KEYS", "IUCN_CATEGORIES", "KEY_SCHEMES", "NEVER", "NEVER_SENTENCE", "NOT_ASSESSED",
    "PROVIDERS", "READ_SCOPE", "RECORD_TYPES", "REVIEW_SCOPE", "SOURCE_PACK", "WRITE_SCOPE", "BiodiversityError",
    "authorize", "authorship_key", "canonical", "digest", "generalisation", "licence", "name_key",
    "register_schemas", "require", "statement", "status_class", "subject_key", "validate_statement",
]
