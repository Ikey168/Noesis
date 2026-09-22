"""Offline verification for ``noesis-evidence-bundle-v1``.

Verification establishes schema validity, content integrity, reference
closure, evidence discipline, and honesty-envelope shape.  It does not assert
that a cited source is true or that an analytical method is appropriate.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from src.analytics.honesty import is_interval, validate_analytic_output

from .builder import compute_bundle_id, compute_object_digest
from .canonical import sha256_file

_REPOSITORY_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "schemas"
    / "jsonschema"
    / "noesis-evidence-bundle-v1.json"
)


def _default_schema_path() -> Path:
    if _REPOSITORY_SCHEMA_PATH.is_file():
        return _REPOSITORY_SCHEMA_PATH
    try:
        from importlib.resources import files

        packaged = files("contracts.schemas.jsonschema").joinpath(
            "noesis-evidence-bundle-v1.json"
        )
        if packaged.is_file():
            return Path(str(packaged))
    except (ImportError, ModuleNotFoundError):
        pass
    return _REPOSITORY_SCHEMA_PATH


SCHEMA_PATH = _default_schema_path()

VALID = "valid"
VALID_EXTERNAL = "valid_with_external_references"
INCOMPLETE = "incomplete"
INVALID = "invalid"
MAX_BUNDLE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class VerificationResult:
    status: str
    valid: bool
    bundle_id: str | None
    errors: list[str]
    warnings: list[str]
    stats: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _path(parts: Iterable[Any]) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered


def _schema_errors(bundle: Any, schema_path: Path) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return ["schema validation unavailable: install jsonschema"]
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"could not load evidence-bundle schema: {exc}"]
    validator = jsonschema.Draft7Validator(schema)
    return [
        f"schema {_path(error.absolute_path)}: {error.message}"
        for error in sorted(
            validator.iter_errors(bundle),
            key=lambda e: tuple(str(part) for part in e.absolute_path),
        )
    ]


def _walk_honesty(value: Any, errors: list[str], parts: tuple[Any, ...] = ()) -> None:
    if isinstance(value, dict):
        honesty = {"n", "method", "assumptions"}
        if honesty & set(value):
            for message in validate_analytic_output(value):
                errors.append(f"honesty {_path(parts)}: {message}")
        if {"lo", "hi", "level"} <= set(value) and not is_interval(value):
            errors.append(f"honesty {_path(parts)}: malformed interval")
        for key, child in value.items():
            _walk_honesty(child, errors, (*parts, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_honesty(child, errors, (*parts, index))


def _statements(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    direct = payload.get("statements")
    if isinstance(direct, list):
        return [row for row in direct if isinstance(row, dict)]
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("statements"), list):
        return [row for row in data["statements"] if isinstance(row, dict)]
    return []


def _cited_evidence(
    object_map: Mapping[str, Mapping[str, Any]], refs: Iterable[str]
) -> bool:
    for ref in refs:
        record = object_map.get(ref)
        if not record or record.get("type") != "evidence":
            continue
        payload = record.get("payload")
        locator = payload.get("locator") if isinstance(payload, dict) else None
        if isinstance(locator, dict) and locator.get("cited") is True:
            return True
    return False


def _prediction_models(value: Any) -> set[str]:
    models: set[str] = set()
    if isinstance(value, dict):
        mode = value.get("prediction_mode")
        if isinstance(mode, str) and mode.startswith(("pretrained:", "zero-shot:")):
            model = mode.split(":", 1)[1].strip()
            if model:
                models.add(model)
        for child in value.values():
            models |= _prediction_models(child)
    elif isinstance(value, list):
        for child in value:
            models |= _prediction_models(child)
    return models


def _semantic_checks(bundle: Mapping[str, Any], errors: list[str]) -> None:
    objects = bundle.get("objects", [])
    object_map = {
        item.get("id"): item
        for item in objects
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    allowed_uncited = {
        "unverifiable",
        "uncited",
        "refused",
        "insufficient_evidence",
        "not_found",
    }
    pinned_models: set[str] = set()
    unresolved_models: set[str] = set()
    for record in objects:
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        payload = record.get("payload")
        if kind == "evidence":
            locator = payload.get("locator") if isinstance(payload, dict) else None
            if not isinstance(locator, dict) or not isinstance(
                locator.get("cited"), bool
            ):
                errors.append(
                    f"evidence {record.get('id')!r} has no explicit cited state"
                )
            elif locator["cited"] and not locator.get("document_id"):
                errors.append(
                    f"evidence {record.get('id')!r} is cited without a document_id"
                )
        elif kind == "model_pin":
            if not isinstance(payload, dict) or not isinstance(
                payload.get("model"), str
            ):
                errors.append(f"model pin {record.get('id')!r} has no model name")
                continue
            model = payload["model"]
            if payload.get("status") == "unresolved":
                unresolved_models.add(model)
            elif payload.get("status") == "pinned":
                revision = str(payload.get("resolved_revision") or "")
                if len(revision) != 40 or any(
                    c not in "0123456789abcdef" for c in revision
                ):
                    errors.append(
                        f"model pin {record.get('id')!r} has no immutable revision"
                    )
                else:
                    pinned_models.add(model)
            else:
                errors.append(f"model pin {record.get('id')!r} has invalid status")
        elif kind == "answer":
            statements = _statements(payload)
            if not statements:
                errors.append(
                    f"answer {record.get('id')!r} has no structured statements"
                )
            for index, statement in enumerate(statements):
                refs = statement.get("evidence_refs", [])
                verdict = str(
                    statement.get("verdict") or statement.get("status") or ""
                ).lower()
                if (
                    not _cited_evidence(object_map, refs)
                    and verdict not in allowed_uncited
                ):
                    errors.append(
                        f"answer {record.get('id')!r} statement {index} has no cited evidence "
                        "and no explicit unverifiable/uncited verdict"
                    )
        elif kind == "claim":
            refs = record.get("references", [])
            citation_state = (
                payload.get("citation_state") if isinstance(payload, dict) else None
            )
            if not _cited_evidence(object_map, refs) and citation_state != "uncited":
                errors.append(f"claim {record.get('id')!r} has no cited evidence")
        elif kind == "integrity" and isinstance(payload, dict):
            for index, finding in enumerate(payload.get("findings", [])):
                evidence = (
                    finding.get("evidence") if isinstance(finding, dict) else None
                )
                if not isinstance(evidence, list) or not evidence:
                    errors.append(
                        f"integrity {record.get('id')!r} finding {index} has no evidence"
                    )
    required_models = _prediction_models(
        [record.get("payload") for record in objects if isinstance(record, dict)]
    )
    missing_models = sorted(required_models - pinned_models - unresolved_models)
    if missing_models:
        errors.append(f"prediction modes have no model-pin objects: {missing_models}")
    completeness = bundle.get("completeness")
    is_partial = (
        isinstance(completeness, dict) and completeness.get("status") == "partial"
    )
    if unresolved_models and not is_partial:
        errors.append(
            f"unresolved model pins require a partial bundle: {sorted(unresolved_models)}"
        )


def _safe_adjacent(base_dir: Path, locator: str) -> Path | None:
    pure = PurePosixPath(locator.replace("\\", "/"))
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    root = base_dir.resolve()
    candidate = (root / Path(*pure.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def verify_bundle(
    bundle: Any,
    *,
    bundle_path: Path | None = None,
    schema_path: Path = SCHEMA_PATH,
) -> VerificationResult:
    """Verify an already-decoded bundle without network access."""
    errors = _schema_errors(bundle, schema_path)
    warnings: list[str] = []
    object_count = 0
    evidence_count = 0
    external_count = 0
    partial = False
    has_external = False

    if isinstance(bundle, dict):
        objects = bundle.get("objects")
        if isinstance(objects, list):
            object_count = len(objects)
            ids = [item.get("id") for item in objects if isinstance(item, dict)]
            duplicates = sorted({item for item in ids if ids.count(item) > 1})
            if duplicates:
                errors.append(f"duplicate object ids: {duplicates}")
            object_map = {
                item.get("id"): item
                for item in objects
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            evidence_count = sum(
                item.get("type") == "evidence"
                for item in objects
                if isinstance(item, dict)
            )
            for item in objects:
                if not isinstance(item, dict) or not {"id", "type", "payload"} <= set(
                    item
                ):
                    continue
                try:
                    actual = compute_object_digest(item)
                except (KeyError, TypeError, ValueError) as exc:
                    errors.append(f"object {item.get('id')!r} cannot be hashed: {exc}")
                    continue
                if actual != item.get("sha256"):
                    errors.append(f"object {item.get('id')!r} digest mismatch")
                for ref in item.get("references", []):
                    if ref not in object_map:
                        errors.append(
                            f"object {item.get('id')!r} has unresolved reference {ref!r}"
                        )
            for root in bundle.get("roots", []):
                if root not in object_map:
                    errors.append(f"root {root!r} does not resolve")
            manifest = bundle.get("manifest")
            if isinstance(manifest, dict):
                expected_entries = sorted(
                    [
                        {
                            "id": item.get("id"),
                            "type": item.get("type"),
                            "sha256": item.get("sha256"),
                        }
                        for item in objects
                        if isinstance(item, dict)
                    ],
                    key=lambda item: str(item["id"]),
                )
                if manifest.get("entries") != expected_entries:
                    errors.append(
                        "manifest entries do not exactly match bundle objects"
                    )
                if manifest.get("entry_count") != len(expected_entries):
                    errors.append("manifest entry_count does not match bundle objects")
        try:
            expected_id = compute_bundle_id(bundle)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"bundle identity cannot be computed: {exc}")
        else:
            if bundle.get("bundle_id") != expected_id:
                errors.append("bundle_id mismatch")

        completeness = bundle.get("completeness")
        partial = (
            isinstance(completeness, dict) and completeness.get("status") == "partial"
        )
        external = bundle.get("external_references", [])
        if isinstance(external, list):
            external_count = len(external)
            external_ids = [
                item.get("id") for item in external if isinstance(item, dict)
            ]
            duplicate_external = sorted(
                {item for item in external_ids if external_ids.count(item) > 1}
            )
            if duplicate_external:
                errors.append(f"duplicate external reference ids: {duplicate_external}")
            base_dir = bundle_path.parent if bundle_path is not None else None
            for item in external:
                if not isinstance(item, dict):
                    continue
                mode = item.get("mode")
                if mode == "external":
                    has_external = True
                    warnings.append(
                        f"external reference {item.get('id')!r} was not fetched (offline verification)"
                    )
                elif mode == "adjacent":
                    if base_dir is None:
                        partial = True
                        warnings.append(
                            f"adjacent reference {item.get('id')!r} not checked without bundle path"
                        )
                        continue
                    candidate = _safe_adjacent(base_dir, str(item.get("locator", "")))
                    if candidate is None:
                        errors.append(
                            f"adjacent reference {item.get('id')!r} has an unsafe path"
                        )
                    elif not candidate.is_file():
                        partial = True
                        warnings.append(
                            f"adjacent reference {item.get('id')!r} is missing"
                        )
                    else:
                        actual = sha256_file(candidate)
                        if actual != item.get("sha256"):
                            errors.append(
                                f"adjacent reference {item.get('id')!r} digest mismatch"
                            )
        _walk_honesty(bundle.get("objects", []), errors, ("objects",))
        _semantic_checks(bundle, errors)
        if isinstance(objects, list):
            root_types = {
                item.get("type")
                for item in objects
                if isinstance(item, dict) and item.get("id") in bundle.get("roots", [])
            }
            operation = bundle.get("operation")
            operation_type = (
                operation.get("type") if isinstance(operation, dict) else None
            )
            if operation_type not in root_types:
                errors.append("operation type does not match any root object")

    if errors:
        status = INVALID
    elif partial:
        status = INCOMPLETE
    elif has_external:
        status = VALID_EXTERNAL
    else:
        status = VALID
    return VerificationResult(
        status=status,
        valid=status in {VALID, VALID_EXTERNAL},
        bundle_id=bundle.get("bundle_id") if isinstance(bundle, dict) else None,
        errors=errors,
        warnings=warnings,
        stats={
            "objects": object_count,
            "evidence_objects": evidence_count,
            "external_references": external_count,
        },
    )


def verify_file(path: Path, *, schema_path: Path = SCHEMA_PATH) -> VerificationResult:
    try:
        if path.stat().st_size > MAX_BUNDLE_BYTES:
            raise ValueError(
                f"bundle exceeds the {MAX_BUNDLE_BYTES}-byte offline verification limit"
            )
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return VerificationResult(
            status=INVALID,
            valid=False,
            bundle_id=None,
            errors=[f"could not read bundle JSON: {exc}"],
            warnings=[],
            stats={"objects": 0, "evidence_objects": 0, "external_references": 0},
        )
    return verify_bundle(bundle, bundle_path=path, schema_path=schema_path)
