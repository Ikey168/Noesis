"""Composition contracts: manifest, provider descriptor, plan, readiness, receipt (C02).

Each contract has a JSON Schema under ``contracts/schemas/jsonschema/`` and a
runtime validator here. The JSON Schema fixes the shape; the validators add
the rules a schema cannot express:

* contract ranges use the schema registry's range grammar (C01.3);
* unknown critical fields fail instead of being dropped (v1 coerces them away);
* manifests and descriptors can never name code to execute;
* tool bindings must name registered catalog tools;
* one store has one owner across a descriptor set;
* plans carry no observation-time or credential fields, and have one
  canonical digest (:func:`plan_digest`).

Validators return a list of :class:`ContractIssue` (empty means valid), the
convention ``src.domains.pack_format.validate_manifest`` already uses.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "contracts/schemas/jsonschema"

MANIFEST_CONTRACT = "noesis-pack-composition-v1"
PROVIDER_CONTRACT = "noesis-provider-descriptor-v1"
PLAN_CONTRACT = "noesis-composition-plan-v1"
READINESS_CONTRACT = "noesis-composition-readiness-v1"
RECEIPT_CONTRACT = "noesis-composition-activation-receipt-v1"
CONTRACTS = (MANIFEST_CONTRACT, PROVIDER_CONTRACT, PLAN_CONTRACT, READINESS_CONTRACT, RECEIPT_CONTRACT)
CONTRACT_VERSION = "1.0.0"

SIDE_EFFECTS = ("read-only", "local-mutation", "acquisition", "external-publication")
BLOCKER_KINDS = ("empty_data", "inaccessible_data", "missing_credentials", "disabled_provider",
                 "unverified_live_access", "failed_execution")
PROBE_KINDS = ("table-exists", "table-rows", "catalog-tool-state", "source-pack-enabled")
ID_RE = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)+$")
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TOOL_RE = re.compile(r"^noesis-[a-z0-9-]+\.[a-z_][a-z0-9_]*$")
# Keys that would let a document point at code. None of the contracts has
# such a field; they are refused wherever they appear, advisory data included.
EXECUTABLE_KEYS = frozenset({"entrypoint", "entry_point", "callable", "module", "command", "script",
                             "code", "import", "exec", "python", "shell", "hook", "plugin"})
# Observation-time and secret fields never belong in an immutable plan.
DYNAMIC_KEYS = frozenset({"observed_at", "observed_at_ms", "checked_at", "checked_at_ms", "health",
                          "status_now", "credential", "credentials", "secret", "token", "api_key",
                          "password", "ready", "readiness"})
SECRET_KEYS = frozenset({"credential", "credentials", "secret", "token", "api_key", "password",
                         "authorization", "bearer"})


@dataclass(frozen=True)
class ContractIssue:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class CompositionContractError(ValueError):
    """Raised by :func:`require_valid` with every issue attached."""

    def __init__(self, contract: str, issues: Sequence[ContractIssue]) -> None:
        self.contract = contract
        self.issues = list(issues)
        self.code = self.issues[0].code if self.issues else "invalid"
        super().__init__(f"{contract}: " + "; ".join(f"{i.path}: {i.message}" for i in self.issues[:5]))


# ------------------------------------------------------------ canonical form


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_sets(value: Any) -> Any:
    """Arrays are sets in composition documents: sort them for digests."""

    if isinstance(value, Mapping):
        return {str(k): _canonical_sets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        items = [_canonical_sets(v) for v in value]
        return sorted(items, key=canonical_json)
    return value


def content_hash(value: Mapping[str, Any], *, exclude: Iterable[str] = ("content_hash", "digest")) -> str:
    body = {k: v for k, v in value.items() if k not in set(exclude)}
    return "sha256:" + hashlib.sha256(canonical_json(_canonical_sets(body)).encode()).hexdigest()


def plan_digest(plan: Mapping[str, Any]) -> str:
    """The one canonicalization rule for plans.

    Keys sorted, arrays treated as sets (sorted by their canonical JSON), the
    ``digest`` field itself excluded, SHA-256 over UTF-8. Plans carry no
    observation-time fields (validation rejects them), so nothing else needs
    excluding.
    """

    return content_hash(plan, exclude=("digest",))


# ------------------------------------------------------------ range grammar


def valid_range(spec: Any) -> bool:
    """Explicit ranges only, in the schema registry grammar (C01.3).

    ``latest``, ``*`` and empty are refused: composition resolves against an
    explicit candidate set and never floats a requirement.
    """

    if not isinstance(spec, str) or spec.strip() in {"", "*", "latest"}:
        return False
    from src.kb.schema_registry import SchemaRegistryError, _satisfies

    try:
        _satisfies((0, 0, 0), spec)
    except SchemaRegistryError:
        return False
    return True


def satisfies(version: str, spec: str) -> bool:
    from src.kb.schema_registry import _satisfies, _semver

    return _satisfies(_semver(version), spec)


# ------------------------------------------------------------ helpers


def load_schema(contract: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / f"{contract}.json").read_text())


def _schema_issues(contract: str, document: Any) -> list[ContractIssue]:
    from jsonschema import Draft7Validator

    validator = Draft7Validator(load_schema(contract))
    issues = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in error.path) or "$"
        code = "unknown_field" if error.validator == "additionalProperties" else "schema"
        issues.append(ContractIssue(code, path, error.message))
    return issues


def _walk_keys(value: Any, path: str = "$"):
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), f"{path}/{key}", item
            yield from _walk_keys(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_keys(item, f"{path}/{index}")


def _executable_issues(document: Any) -> list[ContractIssue]:
    return [ContractIssue("executable_reference", path, f"'{key}' would name code to execute")
            for key, path, _ in _walk_keys(document) if key.casefold() in EXECUTABLE_KEYS]


def _registered_tools() -> frozenset[str]:
    catalog = REPO_ROOT / "contracts/generated/noesis-mcp-catalog-v1.json"
    try:
        return frozenset(tool["id"] for tool in json.loads(catalog.read_text())["tools"])
    except (OSError, ValueError, KeyError):
        return frozenset()


# ------------------------------------------------------------ C02.1 manifest


def validate_composition_manifest(document: Any, *, known_capabilities: Iterable[str] | None = None,
                                  check_hash: bool = True) -> list[ContractIssue]:
    issues = _schema_issues(MANIFEST_CONTRACT, document)
    if not isinstance(document, Mapping):
        return issues
    issues += _executable_issues(document)
    known = set(known_capabilities) if known_capabilities is not None else None
    contributed = {c.get("id") for c in dict(document.get("contributes") or {}).get("capabilities") or []
                   if isinstance(c, Mapping)}
    for index, requirement in enumerate(document.get("requires") or []):
        if not isinstance(requirement, Mapping):
            continue
        path = f"requires/{index}"
        if not valid_range(requirement.get("range")):
            issues.append(ContractIssue("invalid_range", f"{path}/range",
                                        f"contract range {requirement.get('range')!r} is not an explicit range"))
        capability = requirement.get("capability")
        if known is not None and capability not in known | contributed:
            issues.append(ContractIssue("unknown_capability", f"{path}/capability",
                                        f"required capability {capability!r} is not provided by any known descriptor"))
    for index, feature in enumerate(document.get("optional_features") or []):
        for sub, requirement in enumerate((feature or {}).get("requires") or []):
            if isinstance(requirement, Mapping) and not valid_range(requirement.get("range")):
                issues.append(ContractIssue("invalid_range", f"optional_features/{index}/requires/{sub}/range",
                                            "optional requirement range is not explicit"))
    aliases = document.get("compatibility_aliases") or {}
    if isinstance(aliases, Mapping) and document.get("id") in aliases:
        issues.append(ContractIssue("alias_cycle", "compatibility_aliases", "a pack cannot alias itself"))
    if (check_hash and document.get("content_hash") and not issues
            and content_hash(document) != document["content_hash"]):
        issues.append(ContractIssue("hash_mismatch", "content_hash",
                                    "content_hash does not match the manifest body"))
    return issues


def seal_manifest(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the manifest with its immutable ``content_hash`` set."""

    body = {k: v for k, v in document.items() if k != "content_hash"}
    return {**body, "content_hash": content_hash(body)}


# ------------------------------------------------------------ C02.3 provider


def validate_provider_descriptor(document: Any, *, registered_tools: Iterable[str] | None = None,
                                 check_hash: bool = True) -> list[ContractIssue]:
    issues = _schema_issues(PROVIDER_CONTRACT, document)
    if not isinstance(document, Mapping):
        return issues
    issues += _executable_issues(document)
    tools = set(registered_tools) if registered_tools is not None else set(_registered_tools())
    probes = {p.get("id") for p in document.get("readiness_probes") or [] if isinstance(p, Mapping)}
    operations = {}
    for index, operation in enumerate(document.get("operations") or []):
        if not isinstance(operation, Mapping):
            continue
        operations[operation.get("id")] = operation
        tool = operation.get("tool")
        if tools and tool not in tools:
            issues.append(ContractIssue("unregistered_tool", f"operations/{index}/tool",
                                        f"{tool!r} is not a registered catalog tool"))
        if operation.get("readiness_probe") not in probes:
            issues.append(ContractIssue("missing_probe", f"operations/{index}/readiness_probe",
                                        "operation must reference a declared readiness probe"))
        idem = operation.get("idempotency") or {}
        if operation.get("side_effect") != "read-only" and not idem.get("supported") and not idem.get("receipt"):
            issues.append(ContractIssue("unsafe_retry", f"operations/{index}/idempotency",
                                        "mutating operations declare idempotency or an owner receipt"))
    for index, capability in enumerate(document.get("capabilities") or []):
        for sub, op in enumerate((capability or {}).get("operations") or []):
            if op not in operations:
                issues.append(ContractIssue("unknown_operation", f"capabilities/{index}/operations/{sub}",
                                            f"capability references undeclared operation {op!r}"))
    stores = [s.get("record_type") for s in document.get("stores") or [] if isinstance(s, Mapping)]
    for record_type in {r for r in stores if stores.count(r) > 1}:
        issues.append(ContractIssue("conflicting_store_owner", "stores",
                                    f"record type {record_type!r} is declared twice"))
    if (check_hash and document.get("content_hash") and not issues
            and content_hash(document) != document["content_hash"]):
        issues.append(ContractIssue("hash_mismatch", "content_hash", "content_hash does not match"))
    return issues


def validate_provider_set(descriptors: Sequence[Mapping[str, Any]]) -> list[ContractIssue]:
    """Across descriptors: one owner per store, unique identities."""

    issues: list[ContractIssue] = []
    owners: dict[str, str] = {}
    seen: set[tuple[str, str]] = set()
    for descriptor in descriptors:
        provider = str(descriptor.get("id"))
        identity = (provider, str(descriptor.get("version")))
        if identity in seen:
            issues.append(ContractIssue("duplicate_provider", provider, "provider identity repeated"))
        seen.add(identity)
        for store in descriptor.get("stores") or []:
            record_type = str(store.get("record_type"))
            owner = owners.setdefault(record_type, provider)
            if owner != provider:
                issues.append(ContractIssue(
                    "conflicting_store_owner", f"{provider}/stores/{record_type}",
                    f"record type {record_type!r} is already owned by {owner!r}"))
    return issues


# ------------------------------------------------------------ C02.4 plan


def validate_plan(document: Any) -> list[ContractIssue]:
    issues = _schema_issues(PLAN_CONTRACT, document)
    if not isinstance(document, Mapping):
        return issues
    for key, path, _ in _walk_keys(document):
        if key.casefold() in DYNAMIC_KEYS:
            issues.append(ContractIssue("dynamic_field", path,
                                        f"'{key}' is observation-time state and cannot be pinned in a plan"))
    if not issues and document.get("digest") != plan_digest(document):
        issues.append(ContractIssue("digest_mismatch", "digest", "digest does not match the canonical plan"))
    return issues


def seal_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in plan.items() if k != "digest"}
    return {**body, "digest": plan_digest(body)}


# ------------------------------------------------------------ C02.5 records


def _secret_issues(document: Any) -> list[ContractIssue]:
    return [ContractIssue("secret_field", path, f"'{key}' may carry a credential value")
            for key, path, _ in _walk_keys(document) if key.casefold() in SECRET_KEYS]


def validate_readiness(document: Any) -> list[ContractIssue]:
    return _schema_issues(READINESS_CONTRACT, document) + _secret_issues(document)


def validate_receipt(document: Any, *, generation_digests: Mapping[str, str] | None = None) -> list[ContractIssue]:
    issues = _schema_issues(RECEIPT_CONTRACT, document) + _secret_issues(document)
    if not isinstance(document, Mapping) or generation_digests is None:
        return issues
    for field in ("previous_generation", "new_generation"):
        generation = document.get(field)
        if (isinstance(generation, Mapping) and generation.get("id") in generation_digests
                and generation_digests[generation["id"]] != generation.get("plan_digest")):
            issues.append(ContractIssue("generation_digest_mismatch", f"{field}/plan_digest",
                                        "plan digest does not match the recorded generation"))
    return issues


VALIDATORS = {
    MANIFEST_CONTRACT: validate_composition_manifest,
    PROVIDER_CONTRACT: validate_provider_descriptor,
    PLAN_CONTRACT: validate_plan,
    READINESS_CONTRACT: validate_readiness,
    RECEIPT_CONTRACT: validate_receipt,
}


def require_valid(contract: str, document: Any, **options: Any) -> Any:
    issues = VALIDATORS[contract](document, **options)
    if issues:
        raise CompositionContractError(contract, issues)
    return document


# ------------------------------------------------------------ C02.6 registry

REGISTRY_NAMES = {
    MANIFEST_CONTRACT: "pack-composition",
    PROVIDER_CONTRACT: "provider-descriptor",
    PLAN_CONTRACT: "composition-plan",
    READINESS_CONTRACT: "composition-readiness",
    RECEIPT_CONTRACT: "composition-activation-receipt",
}


def resolve_contract(registry: Any, contract: str, version_spec: str = CONTRACT_VERSION) -> dict[str, Any]:
    """Resolve a composition contract by identity and version through the schema registry."""

    from src.kb.schema_registry import READ_SCOPE

    return registry.resolve("schema", REGISTRY_NAMES[contract], version_spec, scopes={READ_SCOPE})


def declare_contract_dependencies(registry: Any, *, principal_id: str, scopes: Iterable[str]) -> list[dict[str, Any]]:
    """Record each composition contract's dependencies with ``declare_dependency``.

    Idempotent: re-declaring replaces the same row.
    """

    from src.kb.schema_registry import READ_SCOPE

    declared = []
    for contract in CONTRACTS:
        module = resolve_contract(registry, contract)
        for dependency in module["dependencies"]:
            target = registry.resolve(dependency["kind"], dependency["name"], dependency["version"],
                                      scopes={READ_SCOPE})
            declared.append(registry.declare_dependency(
                target["module_id"], "module", module["module_id"],
                {"contract": contract, "range": dependency["version"]},
                principal_id=principal_id, scopes=scopes))
    return declared
