"""AI model and dataset registry records for the Technology ``technology.ai-models`` provider (#2742, AI02).

One record contract, ``noesis-ai-model-record-v2`` (the wave's record contract line is 2.x), covers the statements the
provider owns, in the existing record shapes (``registry-records``, ``versioned-documents``, ``observations``):

* ``hub_repository_revision`` - a Hugging Face Hub model or dataset repository at one revision ``sha`` with its
  ``lastModified``: a pinned ``sha`` is immutable and a new ``sha`` is a new revision. Declared fields (pipeline tag,
  library, tags, base model, datasets, languages, the declared licence as stated), the file names, and the card file:
  its front matter (allow-listed keys) with digests, the body cited by revision URL and digest and never stored
  (``versioned-documents``). A repository that turns gated or disabled gets a ``withdrawn`` revision, one that answers
  404 a ``removed_by_source`` revision, a same-host redirect a ``renamed`` revision; history is kept;
* ``hub_refs`` - the branches and tags the repository states, with their target commits;
* ``openml_dataset_revision`` - an OpenML dataset version (its own id) with name, version, ``status``, declared
  ``licence`` as stated, format, ``md5_checksum``, the description's digest and computed qualities; a changed status,
  description or checksum is a new revision;
* ``openml_task_revision`` - a task definition (type, dataset, target, estimation procedure, measures);
* ``epoch_model_revision`` - an Epoch AI notable-model row: organisation, publication date, domain and Epoch's stated
  estimates with their confidence label, vintaged by the file's content digest; a revised estimate is a new revision
  and a declared row missing from a complete later file is ``removed_by_source``;
* ``observation`` - self-reported card results (``model-index``) and OpenML run evaluations, each labelled with who
  reported it (``reported_by``); evaluations are immutable per run id and a run no longer listed becomes
  ``not-returned``. Self-reported results, OpenML evaluations and Epoch estimates are never merged;
* ``evaluation_listing`` and ``epoch_listing`` - complete listings the store uses to mark runs ``not-returned`` and rows
  ``removed_by_source``; never kept as records of their own.

**Minimisation decision (AI01).** Person fields (Hub user authors and commit authors, OpenML creators, contributors
and uploaders, Epoch authors) are refused at parse time by :func:`validate`, reusing
:func:`src.kb.oss_ecosystem_records.check_no_people` plus this provider's own person keys; card bodies, commit
history, discussions and download, like and trending counts are refused as excluded fields. Declared licences are
stored as stated and normalised to SPDX only on an exact licence-id match through :mod:`src.kb.oss_spdx`; nothing
interprets compliance.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.ai_models_sources import (
    EXCLUSIONS,
    FRONT_MATTER_KEYS,
    LIVE_VERIFICATION,
    MINIMISATION,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    PROVIDERS,
)
from src.kb.oss_ecosystem_records import OssRecordError, check_no_people

CONTRACT = "noesis-ai-model-record-v2"
ANSWER_CONTRACT = "noesis-ai-model-answer-v1"
READ_SCOPE = "knowledge:technical:ai-models:read"
WRITE_SCOPE = "knowledge:technical:ai-models:write"
REVIEW_SCOPE = "knowledge:technical:ai-models:review"
DEFAULT_NAMESPACE = "global"
PROVIDER_ID = "technology.ai-models"
BUNDLE = "technology"
FEATURES = {"ai-models-hub": "huggingface-hub", "ai-models-openml": "openml", "ai-models-epoch": "epoch-ai"}
RECORD_TYPES = ("hub_repository_revision", "hub_refs", "openml_dataset_revision", "openml_task_revision",
                "epoch_model_revision", "observation")
LISTINGS = ("evaluation_listing", "epoch_listing")
OBSERVATION_KINDS = ("self_reported_result", "openml_run_evaluation")
RECORD_KINDS = ("hub-model", "hub-dataset", "hub-refs", "openml-dataset", "openml-task", "epoch-model")
HUB_STATES = ("published", "withdrawn", "removed_by_source", "renamed")
REVISION_STATES = ("published", "withdrawn", "removed_by_source", "renamed")
OPENML_STATUSES = ("active", "in_preparation", "deactivated")
OBSERVATION_STATES = ("reported", "not-returned")
SCHEMA_FILE = f"contracts/schemas/jsonschema/{CONTRACT}.json"
# Person keys this provider's sources carry beyond the OSS set (OpenML creators and uploaders, Hub commit authors).
PERSON_FIELDS = frozenset({
    "creator", "creators", "uploader", "uploader_id", "uploader_name", "uploaders", "commit_authors",
    "author_name", "author_email", "authored_by", "fullname_of_user", "user_id", "orcid",
})
# Keys of excluded content: card bodies, commit history, discussions, popularity counts and verdicts.
EXCLUDED_FIELDS = frozenset({
    "card_body", "card_text", "readme_text", "body_text", "commit_history", "commits", "discussions",
    "downloads", "downloadsalltime", "downloads_all_time", "likes", "trendingscore", "trending_score", "trending",
    "leaderboard", "ranking", "rank", "verdict", "capability_verdict", "safety_verdict", "quality_score",
    "risk_score", "openness_score", "compliance", "licence_compliance", "license_compliance", "weights",
    "description_text", "citation_text", "inferred_parameters", "inferred_compute", "inferred_training_data",
})
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$")
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class AiModelsError(ValueError):
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
    if isinstance(value, (int, float)) and not isinstance(value, bool):
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
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise AiModelsError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes or ())
    if "operator" not in scopes and required not in scopes:
        raise AiModelsError("unauthorized", f"{required} is required for this part of the answer")


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
    """Paths of keys that could hold a person (the OSS set plus this provider's own); used on answers too."""
    from src.kb.oss_ecosystem_records import FORBIDDEN_FIELDS

    return _paths(value, FORBIDDEN_FIELDS | PERSON_FIELDS)


def excluded_paths(value: Any) -> list[str]:
    return _paths(value, EXCLUDED_FIELDS)


def check_people(value: Any) -> None:
    """Refuse any person field: the OSS ``check_no_people`` first, then this provider's own person keys."""
    try:
        check_no_people(value)
    except OssRecordError as exc:
        raise AiModelsError("personal_field", str(exc)) from exc
    leaked = _paths(value, PERSON_FIELDS)
    if leaked:
        raise AiModelsError("personal_field", "a statement field could hold an individual's identity and is never "
                                              "stored", fields=leaked)


def _text(value: Any, field: str, *, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AiModelsError("invalid_record", f"{field} must be nonempty text")
    return value


def _instant(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        raise AiModelsError("invalid_record", f"{field} must be a UTC ISO-8601 instant")
    return value


def validate(statement: Mapping[str, Any]) -> dict[str, Any]:
    """A validated statement: persons and excluded fields refused, identity and revision fields checked."""
    if not isinstance(statement, Mapping):
        raise AiModelsError("invalid_record", "a statement is an object")
    value = json.loads(canonical(dict(statement)))
    check_people(value)
    excluded = excluded_paths(value)
    if excluded:
        raise AiModelsError("excluded_field", "card bodies, commit history, discussions, popularity counts and "
                                              "verdicts are never stored", fields=excluded)
    record_type = value.get("record_type")
    if record_type not in (*RECORD_TYPES, *LISTINGS):
        raise AiModelsError("invalid_record", f"record_type is one of {RECORD_TYPES + LISTINGS}")
    if value.get("source") not in PROVIDERS:
        raise AiModelsError("invalid_record", f"source is one of {PROVIDERS}")
    if record_type in {"hub_repository_revision", "hub_refs"} or (
            record_type == "observation" and value.get("observation_kind") == "self_reported_result"):
        if value["source"] != "huggingface-hub":
            raise AiModelsError("invalid_record", "Hub statements come from the Hub")
        subject = dict(value.get("subject") or {}) if record_type == "observation" else value
        if subject.get("kind") not in {"model", "dataset"} or not _REPO.fullmatch(str(subject.get("repo_id") or "")):
            raise AiModelsError("invalid_record", "a Hub statement names its kind and namespace/name repository id")
    if record_type == "hub_repository_revision":
        if value.get("state") not in HUB_STATES:
            raise AiModelsError("invalid_record", f"a Hub revision state is one of {HUB_STATES}")
        if value["state"] == "published":
            if not _SHA.fullmatch(str(value.get("sha") or "")):
                raise AiModelsError("invalid_record", "a published Hub revision is keyed by its 40-hex sha")
            _instant(value.get("last_modified"), "last_modified")
            card = dict(value.get("card") or {})
            if card.get("status") not in {"stated", "none", "over_cap"}:
                raise AiModelsError("invalid_record", "a card is stated, none or over the 1 MB cap")
            if set(card.get("front_matter") or {}) - set(FRONT_MATTER_KEYS):
                raise AiModelsError("excluded_field", "only allow-listed card front-matter keys are stored")
            if card.get("body") not in (None, "cited by revision URL and digest; never stored"):
                raise AiModelsError("excluded_field", "the card body is cited by revision URL and digest, never "
                                                      "stored")
        elif "sha" in value:
            raise AiModelsError("invalid_record", "a source-stated state revision carries no sha of its own")
    elif record_type == "openml_dataset_revision":
        if type(value.get("dataset_id")) is not int or value.get("status") not in OPENML_STATUSES:
            raise AiModelsError("invalid_record", f"an OpenML dataset states its id and status ({OPENML_STATUSES})")
        _text(value.get("name"), "name")
    elif record_type == "openml_task_revision":
        if type(value.get("task_id")) is not int or type(value.get("dataset_id")) is not int:
            raise AiModelsError("invalid_record", "an OpenML task states its id and dataset")
    elif record_type == "epoch_model_revision":
        _text(value.get("model"), "model", limit=400)
        for key, estimate in dict(value.get("estimates") or {}).items():
            if not isinstance(estimate, Mapping) or not str(estimate.get("value_text") or "").strip():
                raise AiModelsError("invalid_record", f"estimate {key} is stated as published, never inferred")
            if "confidence" not in estimate:
                raise AiModelsError("invalid_record", "every Epoch estimate carries its confidence label")
        _text(value.get("reported_by"), "reported_by")
    elif record_type == "observation":
        if value.get("observation_kind") not in OBSERVATION_KINDS:
            raise AiModelsError("invalid_record", f"observation_kind is one of {OBSERVATION_KINDS}")
        _text(value.get("reported_by"), "reported_by")
        if value["observation_kind"] == "self_reported_result":
            if not _SHA.fullmatch(str(value.get("sha") or "")) or not dict(value.get("metric") or {}).get(
                    "value_text"):
                raise AiModelsError("invalid_record", "a self-reported result names its revision sha and value")
        elif value["source"] != "openml" or not all(
                type(value.get(k)) is int for k in ("task_id", "run_id")) or not value.get("measure"):
            raise AiModelsError("invalid_record", "an OpenML evaluation names its task, run and measure")
    elif record_type == "evaluation_listing":
        if type(value.get("task_id")) is not int or not isinstance(value.get("run_ids"), list) or \
                not isinstance(value.get("complete"), bool):
            raise AiModelsError("invalid_record", "an evaluation listing names its task, runs and completeness")
    elif record_type == "epoch_listing":
        if not isinstance(value.get("present"), list) or not isinstance(value.get("declared"), list):
            raise AiModelsError("invalid_record", "an Epoch listing names the declared and present rows")
    return value


def record_identity(value: Mapping[str, Any]) -> tuple[str, str]:
    """``(record_kind, native_key)`` of the record a statement revises."""
    record_type = value["record_type"]
    if record_type == "hub_repository_revision":
        return f"hub-{value['kind']}", str(value["repo_id"])
    if record_type == "hub_refs":
        return "hub-refs", f"{value['kind']}:{value['repo_id']}"
    if record_type == "openml_dataset_revision":
        return "openml-dataset", str(value["dataset_id"])
    if record_type == "openml_task_revision":
        return "openml-task", str(value["task_id"])
    if record_type == "epoch_model_revision":
        return "epoch-model", str(value["model"])
    if record_type == "observation" and value["observation_kind"] == "self_reported_result":
        return f"hub-{value['subject']['kind']}", str(value["subject"]["repo_id"])
    if record_type == "observation":
        return "openml-task", str(value["task_id"])
    raise AiModelsError("invalid_record", f"{record_type} has no record identity")


def record_id(namespace: str, record_kind: str, native_key: str) -> str:
    return f"ai:{record_kind}:" + digest([namespace, record_kind, native_key])[:20]


def observation_key(value: Mapping[str, Any]) -> list[Any]:
    if value["observation_kind"] == "openml_run_evaluation":
        return ["openml_run_evaluation", value["task_id"], value["measure"], value["run_id"]]
    return ["self_reported_result", value["subject"]["kind"], value["subject"]["repo_id"], value["sha"],
            value.get("model_name"), value.get("task"), value.get("dataset"),
            {k: v for k, v in dict(value["metric"]).items() if k != "value_text"}]


def licence_of(statement: Mapping[str, Any]) -> dict[str, Any]:
    """The declared licence fields of a revision as stated (Hub ``license``/``license_name``/``license_link``,
    OpenML ``licence``); ``{}`` when the record type declares none."""
    if statement.get("record_type") == "hub_repository_revision":
        return dict(dict(statement.get("declared") or {}).get("licence") or {})
    if statement.get("record_type") == "openml_dataset_revision":
        return dict(statement.get("licence") or {})
    return {}


def declared_licence_value(licence: Mapping[str, Any]) -> str | None:
    value = licence.get("license") or licence.get("licence")
    return str(value).strip() if value not in (None, "") else None


def normalise_licence(licence: Mapping[str, Any], spdx: Any) -> dict[str, Any]:
    """SPDX only on an exact licence-id match in the pinned list (:mod:`src.kb.oss_spdx`); otherwise kept as declared.

    ``other``, ``openrail``, model-specific licences, names and expressions are never mapped. No compliance,
    permissiveness or openness statement is derived.
    """
    from src.kb.oss_spdx import parse_expression

    declared = declared_licence_value(licence)
    if declared is None:
        return {"status": "none_declared", "declared": None}
    if spdx is None:
        return {"status": "no_spdx_list", "declared": declared,
                "reason": "no SPDX License List release is held (OSS ecosystems licences); kept as declared"}
    entry = spdx.licences.get(declared.casefold()) if re.fullmatch(r"[A-Za-z0-9.\-+]+", declared) else None
    if entry is None:
        return {"status": "not_normalised", "declared": declared, "spdx_list_version": spdx.version,
                "reason": "not an exact SPDX licence id in the pinned list; kept as declared, never mapped"}
    parsed = parse_expression(str(entry["id"]), spdx)
    return {"status": "normalised", "declared": declared, "spdx_id": parsed["expression"],
            "deprecated": bool(entry.get("deprecated")), "rule": "exact-spdx-id (ids are case-insensitive)",
            "spdx_list_version": spdx.version}


def _selected_features(conn: Any) -> list[str]:
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
        ).fetchall()}
        if len(tables) < 4:
            return []
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get(BUNDLE) or [])


def features_enabled(conn: Any) -> dict[str, bool]:
    """Which of the Technology bundle's optional ai-models features are selected in the active plan."""
    selected = set(_selected_features(conn))
    return {feature: feature in selected for feature in FEATURES}


def readiness(conn: Any) -> dict[str, Any]:
    ready = table_exists(conn, "ai_revisions")
    selected = features_enabled(conn)
    providers = {}
    for feature, provider in FEATURES.items():
        revisions, state = 0, {"stale": True, "reason": "never acquired"}
        if ready:
            revisions = int(conn.execute("SELECT count(*) FROM ai_revisions r JOIN ai_records c ON "
                                         "c.namespace=r.namespace AND c.record_id=r.record_id WHERE c.source=?",
                                         [provider]).fetchone()[0])
        if table_exists(conn, "ai_receipts"):
            rows = conn.execute("SELECT outcome FROM ai_receipts WHERE provider=? ORDER BY rowid",
                                [provider]).fetchall()
            if any(r[0] in {"applied", "unchanged"} for r in rows):
                failed = rows[-1][0] == "failed"
                state = {"stale": failed, "reason": "last run failed" if failed else None}
        providers[provider] = {"feature": feature, "selected": selected[feature],
                               "delivers": PROVIDER_CONTRACTS[provider]["delivers"],
                               "live_verification": LIVE_VERIFICATION[provider]["status"], "revisions": revisions,
                               **state}
    return {
        "provider": PROVIDER_ID, "features": selected, "stores_ready": ready, "providers": providers,
        "exclusions": list(EXCLUSIONS), "never": NEVER_SENTENCE, "minimisation": MINIMISATION["decision"],
        "note": "offline fixture evidence and live evidence are reported per revision (evidence_origin); no provider "
                "is live until a dated run verifies it (AI13)",
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
            "provenance": {"kind": "imported", "source": "packs/technology"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"ai-models-schema:{name}:2.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


__all__ = [
    "ANSWER_CONTRACT",
    "CONTRACT",
    "EXCLUDED_FIELDS",
    "FEATURES",
    "OBSERVATION_KINDS",
    "PERSON_FIELDS",
    "PROVIDER_ID",
    "READ_SCOPE",
    "RECORD_KINDS",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "AiModelsError",
    "authorize",
    "canonical",
    "check_people",
    "declared_licence_value",
    "digest",
    "excluded_paths",
    "features_enabled",
    "iso",
    "licence_of",
    "normalise_licence",
    "observation_key",
    "personal_data_paths",
    "readiness",
    "record_id",
    "record_identity",
    "register_schemas",
    "require_scope",
    "table_exists",
    "to_ms",
    "validate",
]
