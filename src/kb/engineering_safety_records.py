"""Engineering Safety records: what an authority published, and nothing Noesis concluded (#2059, ES02 #2063).

One versioned statement contract, ``noesis-engineering-safety-record-v1``,
carries every record the pack stores. A statement is one authority's
publication about one native identifier, as published:

* ``directive`` - an airworthiness directive (FAA AD number, EASA AD number
  without its revision suffix). A statement's **applicability** clauses keep
  the published text; a structured parse (models, serial ranges, part
  numbers) is attached only when unambiguous and always names its locator.
  **Supersession** and revision relations are stated relations, never
  inferred. Effective date, compliance-time and required-action text are
  verbatim statements with locators;
* ``investigation`` - an accident or incident investigation (NTSB number,
  CSB investigation, BFU file, BEA report) with its **occurrence** (time,
  place, subject and severity as published), **findings** and the
  **probable-cause statement**, verbatim with locator and report status
  (``preliminary`` or ``final``);
* ``safety_recommendation`` - a recommendation keyed by its number, with its
  addressee and every published status change as a dated
  **recommendation response**;
* ``occurrence`` - a stand-alone reported occurrence (a PHMSA pipeline
  incident report) with the operator as published and the file vintage;
* ``defect_investigation`` - an NHTSA ODI action (PE, EA, DP, RQ) with the
  recall campaigns it cites;
* ``complaint`` - an NHTSA complaint, a source record only, never a confirmed
  defect.

Every distinct statement for a native identifier is an immutable revision
(``src.kb.engineering_safety_store``). Native identifiers are never
rewritten. No statement carries a verdict, risk score, ranking or
compliance determination: :data:`FORBIDDEN_KEYS` are refused at the door.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

CONTRACT = "noesis-engineering-safety-record-v1"
MATCH_CONTRACT = "noesis-engineering-safety-subject-match-v1"
DOSSIER_CONTRACT = "noesis-engineering-safety-dossier-v1"
DIRECTIVES_CONTRACT = "noesis-engineering-safety-directives-as-of-v1"
NOTIFICATION_CONTRACT = "noesis-engineering-safety-notification-v1"
READ_SCOPE = "knowledge:engineering-safety:read"
WRITE_SCOPE = "knowledge:engineering-safety:write"
REVIEW_SCOPE = "knowledge:engineering-safety:review"
DEFAULT_NAMESPACE = "global"
SOURCE_PACK = "engineering-safety"
RECORD_KINDS = (
    "directive",
    "investigation",
    "safety_recommendation",
    "occurrence",
    "defect_investigation",
    "complaint",
)
# provider -> (declared authority, authority code, jurisdiction)
AUTHORITIES: dict[str, tuple[str, str, str]] = {
    "faa-ad": ("Federal Aviation Administration", "us-faa", "US"),
    "easa-ad": ("European Union Aviation Safety Agency", "eu-easa", "EU"),
    "ntsb": ("National Transportation Safety Board", "us-ntsb", "US"),
    "phmsa": ("Pipeline and Hazardous Materials Safety Administration", "us-phmsa", "US"),
    "csb": ("U.S. Chemical Safety and Hazard Investigation Board", "us-csb", "US"),
    "nhtsa-odi": ("NHTSA Office of Defects Investigation", "us-nhtsa-odi", "US"),
    "nhtsa-complaints": ("NHTSA Office of Defects Investigation (complaints)", "us-nhtsa-odi", "US"),
    "bfu": ("Bundesstelle für Flugunfalluntersuchung", "de-bfu", "DE"),
    "bea": ("Bureau d'Enquêtes et d'Analyses pour la sécurité de l'aviation civile", "fr-bea", "FR"),
}
PROVIDER_KINDS: dict[str, frozenset[str]] = {
    "faa-ad": frozenset({"directive"}),
    "easa-ad": frozenset({"directive"}),
    "ntsb": frozenset({"investigation", "safety_recommendation"}),
    "phmsa": frozenset({"occurrence"}),
    "csb": frozenset({"investigation", "safety_recommendation"}),
    "nhtsa-odi": frozenset({"defect_investigation"}),
    "nhtsa-complaints": frozenset({"complaint"}),
    "bfu": frozenset({"investigation", "safety_recommendation"}),
    "bea": frozenset({"investigation"}),
}
STATEMENT_KINDS = frozenset(
    {
        "required_action",
        "compliance_time",
        "unsafe_condition",
        "subject_code",
        "finding",
        "probable_cause",
        "root_cause",
        "summary",
        "recommendation_text",
        "cause_category",
        "incorporated_material",
        "other",
    }
)
RELATIONS = frozenset(
    {
        "supersedes",
        "revises",
        "cross_reference",
        "upgraded_from",
        "upgraded_to",
        "issued_recommendation",
        "recommendation_of",
        "cites_recall",
    }
)
SUBJECT_KINDS = frozenset(
    {
        "aircraft_model",
        "aircraft",
        "engine_model",
        "vehicle",
        "component",
        "pipeline_operator",
        "facility",
        "organisation",
    }
)
REPORT_STATUSES = ("preliminary", "final")
FORBIDDEN_KEYS = frozenset(
    {
        "verdict",
        "safe",
        "unsafe",
        "is_safe",
        "airworthy",
        "compliant",
        "compliance_status",
        "risk_score",
        "score",
        "ranking",
        "safety_rating",
        "cause_inferred",
        "advice",
    }
)
BOUNDARY = (
    "Records are quoted as the issuing authority published them. No safety verdict, risk score, ranking, "
    "compliance determination or inferred cause is given; a subject with no record on file has none on record, "
    "which is not a statement that it is safe."
)
NONE_ON_RECORD = "none on record"


class EngineeringSafetyError(ValueError):
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


def iso_date(value: Any) -> date | None:
    """A date from an ISO string or a date; absent or unparseable is ``None``, never a string."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def iso_text(value: Any) -> str | None:
    parsed = iso_date(value)
    return None if parsed is None else parsed.isoformat()


def date_rank(value: Any) -> date:
    return iso_date(value) or date.min


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
        raise EngineeringSafetyError("unauthorized", f"{required} and namespace access are required")


def require(scopes: Iterable[str], *required: str) -> None:
    """Call-time check for scopes an optional argument or a consumed store needs."""
    scopes = set(scopes)
    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise EngineeringSafetyError("unauthorized", f"{', '.join(missing)} is required for this request")


def designation_key(value: Any) -> str:
    """One comparison key for model designations on every side: case and separators ignored, nothing else."""
    from src.kb.products import designation_key as products_designation_key

    return products_designation_key(value)


def name_key(value: Any) -> str:
    """One comparison key for organisation names on every side (the canonical-entity surface normalisation)."""
    from src.kb.entities import normalize_surface

    text = str(value or "")
    return normalize_surface(text) or re.sub(r"\s+", " ", text).strip().casefold()


def subject_key(kind: str, fields: Mapping[str, Any]) -> str | None:
    """The per-domain identity key of a published subject (ES10); ``None`` when the identifying field is absent.

    * aircraft and engine models: normalised model designation (the type-certificate holder is evidence, not
      part of the key, because sources state it inconsistently);
    * vehicles: make + model + model year (year optional);
    * components: part number + manufacturer;
    * pipeline operators: the PHMSA operator ID;
    * facilities: facility name + address;
    * organisations: the normalised name.
    """
    if kind in {"aircraft_model", "aircraft", "engine_model"}:
        model = designation_key(fields.get("model"))
        return f"{'engine' if kind == 'engine_model' else 'aircraft'}-model:{model}" if model else None
    if kind == "vehicle":
        make, model = name_key(fields.get("make")), designation_key(fields.get("model"))
        if not make or not model:
            return None
        year = str(fields.get("model_year") or "").strip()
        return f"vehicle:{make}:{model}" + (f":{year}" if year else "")
    if kind == "component":
        part = designation_key(fields.get("part_number"))
        maker = name_key(fields.get("manufacturer"))
        return f"component:{maker}:{part}" if part else None
    if kind == "pipeline_operator":
        operator = str(fields.get("operator_id") or "").strip()
        return f"pipeline-operator:{operator}" if operator else None
    if kind == "facility":
        name = name_key(fields.get("name"))
        address = re.sub(r"\s+", " ", str(fields.get("address") or "")).strip().casefold()
        return f"facility:{name}:{address}" if name else None
    if kind == "organisation":
        name = name_key(fields.get("name"))
        return f"organisation:{name}" if name else None
    return None


def record_id_for(namespace: str, provider: str, kind: str, native_id: str) -> str:
    return "es-record:" + digest([namespace, provider, kind, native_id])[:24]


def clean(value: Any) -> Any:
    """Drop absent values (never stored as "None" or empty strings) recursively."""
    def absent(item: Any) -> bool:
        return item is None or item == "" or item == [] or item == {}

    if isinstance(value, Mapping):
        cleaned = {str(k): clean(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if not absent(v)}
    if isinstance(value, list):
        return [c for c in (clean(v) for v in value) if not absent(c)]
    return value


def _forbidden(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.add(str(key))
            found |= _forbidden(item)
    elif isinstance(value, list):
        for item in value:
            found |= _forbidden(item)
    return found


def validate_statement(statement: Mapping[str, Any]) -> dict[str, Any]:
    """A normalised statement, or ``invalid_record``. Absent values are dropped, never written as strings."""
    value = clean(dict(statement))
    provider, kind = str(value.get("provider") or ""), str(value.get("record_kind") or "")
    native = str(value.get("native_id") or "").strip()
    if value.get("contract") != CONTRACT or provider not in AUTHORITIES or not native:
        raise EngineeringSafetyError("invalid_record", "a statement needs its contract, provider and native id")
    if kind not in PROVIDER_KINDS[provider]:
        raise EngineeringSafetyError("invalid_record", f"{provider} does not publish {kind!r} records")
    forbidden = _forbidden(value)
    if forbidden:
        raise EngineeringSafetyError(
            "invalid_record", "statements never carry verdicts, scores, rankings or compliance determinations",
            keys=sorted(forbidden))
    if value.get("report_status") is not None and value["report_status"] not in REPORT_STATUSES:
        raise EngineeringSafetyError("invalid_record", "report_status is preliminary or final")
    for date_field in ("revision_date", "effective_date"):
        if date_field in value and iso_date(value[date_field]) is None:
            raise EngineeringSafetyError("invalid_record", f"{date_field} must be an ISO date")
    for item in value.get("statements") or []:
        if item.get("kind") not in STATEMENT_KINDS or not str(item.get("text") or "").strip():
            raise EngineeringSafetyError("invalid_record", "each statement needs a known kind and verbatim text")
        if not item.get("locator"):
            raise EngineeringSafetyError("invalid_record", "each verbatim statement needs a locator")
    for item in value.get("relations") or []:
        if item.get("relation") not in RELATIONS or not item.get("target_native_id"):
            raise EngineeringSafetyError("invalid_record", "each relation needs a known relation and a target")
    for item in value.get("subjects") or []:
        if item.get("kind") not in SUBJECT_KINDS or not item.get("source_string"):
            raise EngineeringSafetyError("invalid_record", "each subject needs a known kind and its source string")
    for item in value.get("applicability") or []:
        if not str(item.get("text") or "").strip() or not item.get("locator"):
            raise EngineeringSafetyError("invalid_record", "applicability keeps its published text and locator")
        if item.get("parse_state") not in {"parsed", "unparsed"}:
            raise EngineeringSafetyError("invalid_record", "applicability parse_state is parsed or unparsed")
    for item in value.get("responses") or []:
        if not str(item.get("status") or "").strip():
            raise EngineeringSafetyError("invalid_record", "a recommendation response needs its status")
    value["native_id"] = native
    return value


def content_view(statement: Mapping[str, Any]) -> dict[str, Any]:
    """The source-independent content a revision is deduplicated on.

    Locators (row and line numbers, JSON pointers, spans, file vintages) describe where a statement sat in one
    delivery, so they are left out: a re-served file with reordered rows, or an unchanged row in a newer file
    vintage, is the same publication. The file-vintage date is left out for the same reason; the stored revision
    keeps every locator and the vintage it was first seen in.
    """

    def strip(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {k: strip(v) for k, v in value.items() if k != "locator"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    view = strip(json.loads(canonical(statement)))
    if view.get("revision_date_basis") == "file-vintage":
        view.pop("revision_date", None)
    return view


# ------------------------------------------------------------ schema registry

SCHEMA_FILES = {
    "noesis-engineering-safety-record": "contracts/schemas/jsonschema/noesis-engineering-safety-record-v1.json",
    "noesis-engineering-safety-dossier": "contracts/schemas/jsonschema/noesis-engineering-safety-dossier-v1.json",
}


def schema_definitions(root: Any = None) -> dict[str, Any]:
    from pathlib import Path

    base = Path(root) if root else Path(__file__).resolve().parents[2]
    return {name: json.loads((base / path).read_text()) for name, path in SCHEMA_FILES.items()}


def register_schemas(conn: Any, *, principal_id: str, scopes: Iterable[str], root: Any = None) -> list[dict]:
    """Register the pack's contracts in the existing schema registry (idempotent per version)."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1",
            "name": name,
            "kind": "schema",
            "semantic_version": "1.0.0",
            "content": content,
            "owner": "engineering-safety.investigations"
            if name.endswith("dossier")
            else "engineering-safety.directives",
            "dependencies": [],
            "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/engineering-safety"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(
            registry.register(
                definition,
                f"engineering-safety-schema:{name}:1.0.0:{digest(content)[:16]}",
                principal_id=principal_id,
                scopes=set(scopes),
            )
        )
    return results
