"""Humanitarian Response and Conflict Events records (HR02, #2235).

One record contract, ``noesis-humanitarian-record-v1``, covers the record
types the bundle owns:

* ``situation_report`` and ``appeal``: ReliefWeb reports, with their
  publishing organisations, report date and country/disaster tags as
  published; the body is referenced by locator and never mirrored;
* ``crisis``: a ReliefWeb disaster entry (name, GLIDE, status, types);
* ``dataset``: an HDX dataset; each revision is a *dataset revision* that
  keeps the licence, access flags, resources with their hashes and the HXL
  hashtags read from each resource's header rows;
* ``conflict_event``: one coder's event (UCDP GED, UCDP Candidate or ACLED)
  with its coding source, the coder's precision codes (where, when, type),
  actor labels and counts exactly as published; each release is an *event
  revision*;
* ``event_release``: one coder release (dataset version, window, country and
  the event ids it contained), so a release that drops a candidate event
  records a revision instead of a deletion.

Every record carries ``source``, ``source_id``, ``revision`` (the provider's
revision or release label), ``retrieved_at`` and ``as_of`` (when the provider
published this revision). Revisions are appended by
:mod:`src.kb.humanitarian_store`, never overwritten.

Places are stored as published (name, code, scheme, level); a link to a
``geospatial`` place exists only as a separate reviewable assertion
(:mod:`src.kb.humanitarian_identity`). No record carries personal data about
affected people: :func:`validate_record` rejects personal-data fields anywhere
in a record. There are no derived casualty figures and no merged counts.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT = "noesis-humanitarian-record-v1"
READ_SCOPE = "knowledge:humanitarian:read"
WRITE_SCOPE = "knowledge:humanitarian:write"
REVIEW_SCOPE = "knowledge:humanitarian:review"
INGEST_SCOPE = "knowledge:ingestion:execute"
RECORD_TYPES = ("situation_report", "appeal", "crisis", "dataset", "conflict_event", "event_release")
SOURCES = ("reliefweb", "hdx", "ucdp-ged", "ucdp-candidate", "acled")
CODING_SOURCES = ("ucdp-ged", "ucdp-candidate", "acled")
CODING_STATUSES = ("final", "candidate", "dropped-in-release")
PLACE_SCHEMES = ("iso3", "cod-ab-pcode", "gw", "name-only", "ucdp-adm", "acled-admin", "hdx-group")
# Keys that would carry data about individuals (or free text that can name them). A record containing any
# of them anywhere is rejected: parsers must drop such fields before a record is built.
PERSONAL_DATA_FIELDS = frozenset({
    "person_name", "full_name", "first_name", "last_name", "given_name", "family_name", "victim_name",
    "victim_names", "date_of_birth", "dob", "birth_date", "age_of_person", "gender_of_person", "email",
    "e_mail", "phone", "phone_number", "telephone", "mobile", "national_id", "passport", "passport_number",
    "id_number", "household_id", "beneficiary_id", "beneficiary_name", "individual_id", "home_address",
    "contact_name", "contact_email", "contact_phone", "notes", "source_headline", "source_original",
    "where_description",
})
EXCLUSIONS = (
    "casualty estimation or derived casualty figures",
    "event deduplication across coders into a single true count",
    "early-warning scores or conflict forecasts",
    "severity ranking or summarised situation assessment",
    "operational humanitarian advice or targeting information",
    "personal data about affected people",
)
SCHEMA_FILE = "contracts/schemas/jsonschema/noesis-humanitarian-record-v1.json"


class HumanitarianError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def to_ms(value: Any) -> int | None:
    """ISO date/time (or epoch ms) to epoch milliseconds, UTC for naive values."""

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("Z", "+00:00")
    if len(text) == 10:
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def record_key(record: Mapping[str, Any]) -> str:
    """Stable identity of a record across revisions: ``<source>:<record_type>:<source_id>``.

    UCDP GED and Candidate releases of the same event id share one key (``ucdp``), so a
    final release is a revision of the candidate event, never a second event.
    """

    source = str(record["source"])
    family = "ucdp" if source.startswith("ucdp-") else source
    return f"{family}:{record['record_type']}:{record['source_id']}"


def personal_data_paths(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if str(key).casefold() in PERSONAL_DATA_FIELDS:
                found.append(here)
            found += personal_data_paths(item, here)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found += personal_data_paths(item, f"{path}[{index}]")
    return found


def _place(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping) or not (item.get("name") or item.get("code")):
        raise HumanitarianError("invalid_record", f"places[{index}] needs a published name or code")
    scheme = str(item.get("scheme") or ("name-only" if not item.get("code") else ""))
    if scheme not in PLACE_SCHEMES:
        raise HumanitarianError("invalid_record", f"places[{index}] has unknown scheme {scheme!r}")
    if "place_id" in item or "geospatial_place_id" in item:
        raise HumanitarianError("invalid_record", "places are stored as published; geospatial links are reviewable "
                                                  "assertions, never part of the record")
    return {"name": item.get("name"), "code": item.get("code"), "scheme": scheme, "level": item.get("level"),
            "role": item.get("role")}


def _require(record: Mapping[str, Any], fields: Iterable[str]) -> None:
    missing = [f for f in fields if record.get(f) in (None, "", [])]
    if missing:
        raise HumanitarianError("invalid_record", f"{record.get('record_type')} record is missing {', '.join(missing)}")


def _conflict_event(record: dict[str, Any]) -> None:
    _require(record, ("coding_source", "dataset_version", "coding_status", "precision", "counts"))
    if record["coding_source"] not in CODING_SOURCES or record["coding_source"] != record["source"]:
        raise HumanitarianError("invalid_record", "a conflict event keeps its own coding source")
    if record["coding_status"] not in CODING_STATUSES:
        raise HumanitarianError("invalid_record", f"coding_status must be one of {CODING_STATUSES}")
    precision = record["precision"]
    if not isinstance(precision, Mapping) or set(precision) - {"where", "when", "type"} or "where" not in precision:
        raise HumanitarianError("invalid_record", "precision holds the coder's where/when/type codes")
    for axis, value in precision.items():
        if not isinstance(value, Mapping) or "code" not in value or not value.get("scheme"):
            raise HumanitarianError("invalid_record", f"precision.{axis} keeps the published code and its scheme")
    counts = record["counts"]
    if not isinstance(counts, Mapping) or not counts:
        raise HumanitarianError("invalid_record", "counts are stored as published (e.g. best/low/high or fatalities)")
    for key in counts:
        if key in {"total", "merged", "true_count", "estimate", "sum"}:
            raise HumanitarianError("derived_count", "derived or merged counts are excluded")
    for actor in record.get("actors") or []:
        if not isinstance(actor, Mapping) or not actor.get("label") or "entity_id" in actor:
            raise HumanitarianError("invalid_record", "actors are coder-published labels; entity matches are "
                                                      "reviewable assertions, never part of the record")


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one record and return its normalised copy; raise :class:`HumanitarianError`."""

    if not isinstance(record, Mapping):
        raise HumanitarianError("invalid_record", "a record is an object")
    value = json.loads(json.dumps(dict(record)))
    leaked = personal_data_paths(value)
    if leaked:
        raise HumanitarianError("personal_data", "records never carry personal data about individuals",
                                fields=sorted(leaked))
    value.setdefault("contract", CONTRACT)
    if value["contract"] != CONTRACT:
        raise HumanitarianError("invalid_record", f"contract must be {CONTRACT}")
    _require(value, ("record_type", "source", "source_id", "revision", "as_of", "title"))
    if value["record_type"] not in RECORD_TYPES:
        raise HumanitarianError("invalid_record", f"record_type must be one of {RECORD_TYPES}")
    if value["source"] not in SOURCES:
        raise HumanitarianError("invalid_record", f"source must be one of {SOURCES}")
    value["source_id"] = str(value["source_id"])
    value["revision"] = str(value["revision"])
    for field in ("as_of", "retrieved_at"):
        if value.get(field) is not None:
            try:
                value[field] = iso(to_ms(value[field]))
            except (TypeError, ValueError) as exc:
                raise HumanitarianError("invalid_record", f"{field} is not a date or time") from exc
    url = value.get("source_url")
    if url is not None and not str(url).startswith("https://"):
        raise HumanitarianError("invalid_record", "source_url must be an https locator")
    value["places"] = [_place(item, index) for index, item in enumerate(value.get("places") or [])]
    value["unknowns"] = sorted({str(u) for u in value.get("unknowns") or []})
    kind = value["record_type"]
    if kind in {"situation_report", "appeal"}:
        _require(value, ("publishers", "report_date"))
        body = dict(value.get("body") or {})
        if body.get("mirrored") or body.get("text"):
            raise HumanitarianError("mirrored_body", "report bodies are referenced by locator, never mirrored")
        value["body"] = {"locator": body.get("locator") or url, "mirrored": False}
    elif kind == "crisis":
        _require(value, ("name",))
    elif kind == "dataset":
        _require(value, ("licence", "access"))
        for resource in value.get("resources") or []:
            if not resource.get("resource_id"):
                raise HumanitarianError("invalid_record", "dataset resources are keyed by resource id")
            hxl = resource.get("hxl") or {}
            if hxl.get("status") not in {"tagged", "untagged", "not-read"}:
                raise HumanitarianError("invalid_record", "each resource states whether HXL tags were read")
            if value.get("metadata_only") and hxl.get("status") != "not-read":
                raise HumanitarianError("restricted_resource", "metadata-only datasets never have resources read")
    elif kind == "conflict_event":
        _conflict_event(value)
    elif kind == "event_release":
        _require(value, ("coding_source", "dataset_version"))
        value["event_ids"] = sorted({str(e) for e in value.get("event_ids") or []})
    return value


def changed_fields(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Top-level fields that differ between two revisions (before/after as published)."""

    if before is None:
        return []
    skip = {"retrieved_at"}
    keys = sorted((set(before) | set(after)) - skip)
    return [{"field": k, "before": before.get(k), "after": after.get(k)} for k in keys if before.get(k) != after.get(k)]


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
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": content, "owner": "humanitarian.core", "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/humanitarian"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"humanitarian-schema:{name}:1.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results
