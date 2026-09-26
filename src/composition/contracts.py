"""Composition contracts and the v1 adapters (slice C02, #1790).

Five contracts, each a JSON Schema under ``contracts/schemas/jsonschema`` plus
the semantic checks below that a schema cannot express:

* ``noesis-pack-v2`` - pack composition manifest, the versioned successor to
  ``noesis-pack-v1`` (C02.1);
* ``noesis-capability-provider-v1`` - provider descriptor (C02.3);
* ``noesis-composition-plan-v1`` - resolved composition (C02.4);
* ``noesis-composition-readiness-v1`` - readiness assessment (C02.5);
* ``noesis-composition-activation-receipt-v1`` - activation receipt (C02.5).

Validation is strict where v1 was lenient. ``PackManifest.from_dict`` coerces
and never raises; the successor rejects unknown fields, unknown critical
extensions, unknown required capabilities and executable references instead
of dropping them. The adapters (C02.2) express every ``pack.json`` and every
code-registered ``DomainPack`` as a v2 manifest with identical runtime meaning,
and mark the fields v1 cannot express as adapter-supplied defaults.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from src.kb.schema_registry import SchemaRegistryError, _satisfies, _semver

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "contracts/schemas/jsonschema"
PROVIDER_DIR = REPO_ROOT / "config/composition/providers"

PACK_FORMAT_V2 = "noesis-pack-v2"
PROVIDER_CONTRACT = "noesis-capability-provider-v1"
PLAN_CONTRACT = "noesis-composition-plan-v1"
READINESS_CONTRACT = "noesis-composition-readiness-v1"
RECEIPT_CONTRACT = "noesis-composition-activation-receipt-v1"

# Schema-registry identities (module name, version, dependencies) per contract.
CONTRACT_MODULES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    PACK_FORMAT_V2: ("pack-manifest", "2.0.0", ()),
    PROVIDER_CONTRACT: ("capability-provider", "1.0.0", ()),
    PLAN_CONTRACT: ("composition-plan", "1.0.0", ("pack-manifest", "capability-provider")),
    READINESS_CONTRACT: ("composition-readiness", "1.0.0", ("composition-plan",)),
    RECEIPT_CONTRACT: ("composition-activation-receipt", "1.0.0", ("composition-plan",)),
}

CONTRACT_MODULE_VERSIONS = {name: version for name, version, _ in CONTRACT_MODULES.values()}

EFFECTS = ("read-only", "local-mutation", "acquisition", "external-publication")
EFFECT_RANK = {effect: rank for rank, effect in enumerate(EFFECTS)}
BLOCKER_KINDS = (
    "empty-data",
    "inaccessible-data",
    "missing-credentials",
    "provider-disabled",
    "unverified-live",
    "failed-execution",
    "unauthorized",
    "provider-unavailable",
    "binding-missing",
    "aggregate-limit-exhausted",
)

# x- extensions this implementation understands. A manifest that names any
# other extension in ``critical_extensions`` is rejected.
UNDERSTOOD_EXTENSIONS: frozenset[str] = frozenset()

# Keys that would make a manifest name code to execute. They are rejected
# anywhere in a manifest; only capability IDs and references are allowed.
EXECUTABLE_KEYS = frozenset(
    {
        "module", "modules", "entrypoint", "entry_point", "callable", "code",
        "script", "exec", "command", "python", "import", "handler", "function",
    }
)

# Keys that carry observation-time or secret facts. Plans are immutable and may
# not contain them; readiness and receipts forbid them through their schemas.
DYNAMIC_KEYS = frozenset(
    {
        "health", "credential", "credentials", "credential_value", "secret",
        "secrets", "password", "token", "api_key", "readiness", "observed_at_ms",
        "checked_at_ms", "live_status",
    }
)

# Ranges that would re-resolve to whatever is newest. Plans are locked against
# explicit candidates, so manifests and plans may not use them.
_FLOATING_RANGES = frozenset({"", "*", "latest"})


class CompositionError(ValueError):
    """A composition contract, resolution or lifecycle failure with a stable code."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


# --------------------------------------------------------------------------- #
# Canonical form and version rules (reused from the schema registry, C01.3)
# --------------------------------------------------------------------------- #

def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def check_range(spec: str) -> None:
    """Raise unless ``spec`` is an explicit schema-registry version range."""

    if not isinstance(spec, str) or spec.strip() in _FLOATING_RANGES:
        raise CompositionError(
            "floating_range", f"range {spec!r} must be explicit, not floating", range=spec
        )
    try:
        _satisfies((0, 0, 0), spec)
    except SchemaRegistryError as exc:
        raise CompositionError("invalid_range", str(exc), range=spec) from exc


def satisfies(version: str, spec: str) -> bool:
    return _satisfies(_semver(version), spec)


def version_key(version: str) -> tuple[int, int, int]:
    return _semver(version)


@functools.lru_cache(maxsize=16)
def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / f"{name}.json").read_text(encoding="utf-8"))


def schema_errors(payload: Any, contract: str) -> list[str]:
    """Structural JSON Schema errors for ``payload`` against ``contract``."""

    from jsonschema import Draft7Validator

    errors = sorted(
        Draft7Validator(_schema(contract)).iter_errors(payload),
        key=lambda error: (tuple(str(p) for p in error.path), error.message),
    )
    return [
        f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"
        for error in errors[:50]
    ]


def _fail(code: str, contract: str, errors: list[str]) -> None:
    if errors:
        raise CompositionError(code, f"{contract} is invalid: {errors[0]}", errors=errors)


def _keys(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{path}/{key}" if path else str(key)
            yield str(key), where
            yield from _keys(item, where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _keys(item, f"{path}[{index}]")


def _json_copy(data: Any) -> Any:
    try:
        return json.loads(canonical(data))
    except (TypeError, ValueError) as exc:
        raise CompositionError("invalid_document", "document must be finite JSON") from exc


# --------------------------------------------------------------------------- #
# C02.1 - pack composition manifest
# --------------------------------------------------------------------------- #

def manifest_hash(manifest: Mapping[str, Any]) -> str:
    body = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    return digest(body)


def manifest_id(manifest: Mapping[str, Any]) -> str:
    return f"{manifest['name']}@{manifest['version']}"


def validate_manifest(
    data: Mapping[str, Any], *, known_capabilities: Iterable[str] | None = None
) -> dict[str, Any]:
    """Validate a ``noesis-pack-v2`` manifest; return it with ``manifest_hash``.

    ``known_capabilities`` is the capability vocabulary of the candidate
    provider set. When given, a required capability outside it is rejected as
    unknown instead of being dropped.
    """

    if not isinstance(data, Mapping):
        raise CompositionError("invalid_manifest", "manifest must be an object")
    value = _json_copy(dict(data))
    for key, where in _keys(value):
        if key in EXECUTABLE_KEYS:
            raise CompositionError(
                "executable_reference",
                f"manifests cannot name code to execute ({where})",
                path=where,
            )
    _fail("invalid_manifest", PACK_FORMAT_V2, schema_errors(value, PACK_FORMAT_V2))
    for name in value.get("critical_extensions", []):
        if name in value and name not in UNDERSTOOD_EXTENSIONS:
            raise CompositionError(
                "unknown_critical_extension",
                f"critical extension {name} is not understood by this resolver",
                extension=name,
            )
    errors: list[str] = []
    for name in value.get("critical_extensions", []):
        if name not in value:
            errors.append(f"critical extension {name} is declared but absent")
    known = None if known_capabilities is None else set(known_capabilities)
    features = set(value.get("optional_features", {}))
    seen: set[tuple[str, str | None]] = set()
    for index, requirement in enumerate(value["requires"]):
        where = f"requires[{index}]"
        try:
            check_range(requirement["range"])
            for key in ("input_contract", "output_contract"):
                if key in requirement:
                    check_range(requirement[key]["range"])
        except CompositionError as exc:
            raise CompositionError(exc.code, f"{where}: {exc.message}", path=where) from exc
        if known is not None and requirement["capability"] not in known:
            raise CompositionError(
                "unknown_capability",
                f"{where}: required capability {requirement['capability']} is not "
                "provided by any known provider",
                capability=requirement["capability"],
            )
        feature = requirement.get("feature")
        if feature is not None and feature not in features:
            errors.append(f"{where}: feature {feature!r} is not a declared optional feature")
        identity = (requirement["capability"], feature)
        if identity in seen:
            errors.append(f"{where}: duplicate requirement for {identity[0]}")
        seen.add(identity)
    references = value.get("references", {})
    for key in ("contracts", "ontology_modules", "source_packs"):
        for index, ref in enumerate(references.get(key, [])):
            if "range" not in ref:
                continue
            try:
                check_range(ref["range"])
            except CompositionError as exc:
                raise CompositionError(
                    exc.code, f"references.{key}[{index}]: {exc.message}"
                ) from exc
    aliases = value.get("aliases", {}).get("capability_labels", {})
    labels = set(value["contributes"].get("capability_labels", []))
    for label in aliases:
        if labels and label not in labels:
            errors.append(f"aliases.capability_labels: {label!r} is not a declared label")
    contributes = value["contributes"]
    providers = [(p["provider_id"], p["version"]) for p in contributes.get("providers", [])]
    if len(set(providers)) != len(providers):
        errors.append("contributes.providers: duplicate provider reference")
    profile_ids = [p["profile_id"] for p in contributes.get("profiles", [])]
    if len(set(profile_ids)) != len(profile_ids):
        errors.append("contributes.profiles: duplicate profile_id")
    template_ids = [
        (t["template_id"], t["version"]) for t in contributes.get("workflow_templates", [])
    ]
    if len(set(template_ids)) != len(template_ids):
        errors.append("contributes.workflow_templates: duplicate template")
    _fail("invalid_manifest", PACK_FORMAT_V2, errors)
    computed = manifest_hash(value)
    if value.get("manifest_hash") not in (None, computed):
        raise CompositionError(
            "manifest_hash_mismatch",
            "manifest content does not match its declared hash",
            declared=value["manifest_hash"],
            computed=computed,
        )
    value["manifest_hash"] = computed
    return value


# --------------------------------------------------------------------------- #
# C02.2 - adapters from noesis-pack-v1 and code-registered DomainPacks
# --------------------------------------------------------------------------- #

_V1_METADATA_FIELDS = ("query_examples", "exclusions")
V1_SUPPLIED_DEFAULTS = ["contract_ranges", "optional_features", "providers", "requires",
                        "store_ownership"]


def adapt_v1(
    v1: Mapping[str, Any],
    *,
    source_pack_id: str | None = None,
    capability_aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Map a ``noesis-pack-v1`` manifest into a ``noesis-pack-v2`` manifest.

    The v1 runtime fields are retained verbatim under ``legacy_v1`` so the
    adapted pack installs exactly as before. v1 cannot express requirements,
    contract ranges, optional features, providers or store ownership; the
    adapter fills them with empty defaults and lists them under
    ``adapter.supplied_defaults`` so the resolver treats the pack
    conservatively. Capability strings become advisory ``capability_labels``;
    ``schema_versions`` become exact contract references.
    """

    from src.domains.pack_format import PackManifest, validate_manifest as validate_v1

    errors = validate_v1(dict(v1))
    if errors:
        raise CompositionError(
            "invalid_v1_manifest", f"noesis-pack-v1 manifest is invalid: {errors[0]}",
            errors=errors,
        )
    legacy = PackManifest.from_dict(dict(v1)).to_dict()
    references: dict[str, Any] = {}
    if legacy["schema_versions"]:
        references["contracts"] = [
            {"name": name, "range": version}
            for name, version in sorted(legacy["schema_versions"].items())
        ]
    if v1.get("source_pack") and source_pack_id:
        references["source_packs"] = [
            {"pack_id": source_pack_id, "config": str(v1["source_pack"])}
        ]
    contributes: dict[str, Any] = {}
    if legacy["capabilities"]:
        contributes["capability_labels"] = list(legacy["capabilities"])
    if legacy["ontology_extensions"]:
        contributes["ontology"] = copy.deepcopy(legacy["ontology_extensions"])
    metadata = {key: copy.deepcopy(v1[key]) for key in _V1_METADATA_FIELDS if key in v1}
    known = set(legacy) | set(_V1_METADATA_FIELDS) | {"source_pack"}
    extra = {key: copy.deepcopy(v1[key]) for key in sorted(set(v1) - known)}
    if extra:
        metadata["v1_extra"] = extra
    if v1.get("source_pack"):
        metadata["source_pack_config"] = str(v1["source_pack"])
    manifest: dict[str, Any] = {
        "pack_format": PACK_FORMAT_V2,
        "name": legacy["name"],
        "version": legacy["version"],
        "description": legacy["description"],
        "requires": [],
        "contributes": contributes,
        "legacy_v1": legacy,
        "adapter": {"source": "noesis-pack-v1", "supplied_defaults": list(V1_SUPPLIED_DEFAULTS)},
    }
    if references:
        manifest["references"] = references
    if capability_aliases:
        manifest["aliases"] = {"capability_labels": dict(sorted(capability_aliases.items()))}
    if metadata:
        manifest["metadata"] = metadata
    return validate_manifest(manifest)


def adapt_domain_pack(pack: Any, *, version: str = "0.0.0") -> dict[str, Any]:
    """Express a code-registered ``DomainPack`` as a ``noesis-pack-v2`` manifest.

    Code-registered packs have no version of their own; ``version`` defaults to
    ``0.0.0`` and is listed as adapter-supplied. Route modules and enrichers
    are recorded descriptively under ``adapter``; the manifest never causes
    them to be imported - the pack's Python module still registers them.
    """

    capabilities = [str(value) for value in pack.capabilities]
    contributes: dict[str, Any] = {}
    if capabilities:
        contributes["capability_labels"] = capabilities
    if pack.ontology_extensions:
        contributes["ontology"] = copy.deepcopy(dict(pack.ontology_extensions))
    references: dict[str, Any] = {}
    if pack.schema_versions:
        references["contracts"] = [
            {"name": name, "range": ver} for name, ver in sorted(pack.schema_versions.items())
        ]
    manifest: dict[str, Any] = {
        "pack_format": PACK_FORMAT_V2,
        "name": pack.name,
        "version": version,
        "description": pack.description,
        "requires": [],
        "contributes": contributes,
        "adapter": {
            "source": "domain-pack",
            "supplied_defaults": sorted(set(V1_SUPPLIED_DEFAULTS)),
            "declared_routes": list(pack.route_modules),
            "enrichers": [enricher.name for enricher in pack.enrichers],
            "ui_flags": dict(pack.ui_flags),
        },
    }
    if pack.source_types:
        manifest["advisory"] = {"source_types": list(pack.source_types)}
    if references:
        manifest["references"] = references
    return validate_manifest(manifest)


def v1_view(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The ``noesis-pack-v1`` runtime form of a v2 manifest.

    Adapted and overlaid manifests carry their v1 fields in ``legacy_v1``.
    A v2 manifest without them installs as a pack that contributes only its
    capability labels, schema versions and ontology.
    """

    from src.domains.pack_format import PackManifest

    legacy = manifest.get("legacy_v1")
    if legacy:
        return PackManifest.from_dict(dict(legacy)).to_dict()
    contributes = manifest.get("contributes", {})
    return PackManifest.from_dict(
        {
            "name": manifest["name"],
            "version": manifest["version"],
            "description": manifest.get("description", ""),
            "capabilities": list(contributes.get("capability_labels", [])),
            "schema_versions": {
                ref["name"]: ref["range"]
                for ref in manifest.get("references", {}).get("contracts", [])
            },
            "ontology_extensions": dict(contributes.get("ontology", {})),
        }
    ).to_dict()


def _source_pack_id(pack_dir: Path, v1: Mapping[str, Any]) -> str | None:
    path = v1.get("source_pack")
    if not path:
        return None
    config = REPO_ROOT / str(path)
    if not config.exists():
        config = pack_dir / str(path)
    try:
        return str(json.loads(config.read_text(encoding="utf-8"))["pack_id"])
    except (OSError, ValueError, KeyError):
        return None


def load_pack(
    pack_dir: str | Path, *, known_capabilities: Iterable[str] | None = None
) -> dict[str, Any]:
    """Load ``<pack_dir>/pack.json`` as a v2 manifest.

    When ``composition.json`` sits beside it, that v2 document supplies the
    composition fields (requirements, providers, profiles, workflow templates,
    aliases) and the v1 runtime fields come from ``pack.json``. Both must name
    the same pack and version, so a pack keeps one identity during migration.
    """

    directory = Path(pack_dir)
    v1 = json.loads((directory / "pack.json").read_text(encoding="utf-8"))
    adapted = adapt_v1(v1, source_pack_id=_source_pack_id(directory, v1))
    overlay_path = directory / "composition.json"
    if not overlay_path.exists():
        return adapted
    overlay = json.loads(overlay_path.read_text(encoding="utf-8"))
    if overlay.get("name") != adapted["name"] or overlay.get("version") != adapted["version"]:
        raise CompositionError(
            "identity_mismatch",
            "composition.json must name the same pack and version as pack.json",
            pack=adapted["name"],
        )
    for field in ("legacy_v1", "adapter"):
        if field in overlay:
            raise CompositionError(
                "invalid_manifest", f"composition.json must not declare {field}"
            )
    merged = {key: value for key, value in overlay.items() if key != "manifest_hash"}
    merged["legacy_v1"] = adapted["legacy_v1"]
    contributes = dict(merged.get("contributes", {}))
    contributes.setdefault(
        "capability_labels", adapted["contributes"].get("capability_labels", [])
    )
    if "ontology" in adapted["contributes"]:
        contributes.setdefault("ontology", adapted["contributes"]["ontology"])
    merged["contributes"] = {k: v for k, v in contributes.items() if v != []}
    references = dict(adapted.get("references", {}))
    references.update(merged.get("references", {}))
    if references:
        merged["references"] = references
    metadata = dict(adapted.get("metadata", {}))
    metadata.update(merged.get("metadata", {}))
    if metadata:
        merged["metadata"] = metadata
    return validate_manifest(merged, known_capabilities=known_capabilities)


def load_all_packs(
    root: str | Path | None = None, *, known_capabilities: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Every distributable pack under ``packs/`` as a v2 manifest, by name."""

    base = Path(root) if root is not None else REPO_ROOT / "packs"
    known = None if known_capabilities is None else set(known_capabilities)
    return [
        load_pack(path.parent, known_capabilities=known)
        for path in sorted(base.glob("*/pack.json"))
    ]


# --------------------------------------------------------------------------- #
# C02.3 - provider descriptors
# --------------------------------------------------------------------------- #

def descriptor_hash(descriptor: Mapping[str, Any]) -> str:
    body = {key: value for key, value in descriptor.items() if key != "descriptor_hash"}
    return digest(body)


def validate_provider(
    data: Mapping[str, Any],
    *,
    known_tools: Iterable[str] | None = None,
    registered_bindings: Iterable[str] | None = None,
    registered_probes: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate a provider descriptor; return it with ``descriptor_hash``.

    Bindings must name a tool the MCP catalog registers or an allow-listed
    registered binding; probes must be registered. Defaults come from the
    generated catalog and :mod:`src.composition.bindings`.
    """

    if not isinstance(data, Mapping):
        raise CompositionError("invalid_provider", "provider descriptor must be an object")
    value = _json_copy(dict(data))
    for key, where in _keys(value):
        if key in EXECUTABLE_KEYS:
            raise CompositionError(
                "executable_reference",
                f"provider descriptors cannot name code to execute ({where})",
                path=where,
            )
    _fail("invalid_provider", PROVIDER_CONTRACT, schema_errors(value, PROVIDER_CONTRACT))
    from src.composition import bindings as registry

    tools = set(registry.catalog_tool_ids() if known_tools is None else known_tools)
    registered = set(
        registry.registered_binding_ids() if registered_bindings is None else registered_bindings
    )
    probes = set(registry.registered_probe_ids() if registered_probes is None else registered_probes)
    errors: list[str] = []
    capabilities = [c["capability"] for c in value["capabilities"]]
    if len(set(capabilities)) != len(capabilities):
        errors.append("capabilities: a provider declares each capability once")
    kinds = [s["record_kind"] for s in value["stores"]]
    if len(set(kinds)) != len(kinds):
        errors.append("stores: a provider declares each record kind once")
    owned = set(kinds)
    for index, capability in enumerate(value["capabilities"]):
        where = f"capabilities[{index}]"
        unowned = set(capability.get("record_kinds", [])) - owned
        if capability["effect"] == "local-mutation" and unowned:
            errors.append(f"{where}: mutates record kinds it does not own: {sorted(unowned)}")
        for binding in capability["bindings"]:
            if binding["kind"] == "mcp-tool" and binding["id"] not in tools:
                raise CompositionError(
                    "unregistered_tool",
                    f"{where}: binding {binding['id']} is not a registered MCP tool",
                    binding=binding["id"],
                )
            if binding["kind"] == "registered" and binding["id"] not in registered:
                raise CompositionError(
                    "unregistered_binding",
                    f"{where}: binding {binding['id']} is not an allow-listed registered binding",
                    binding=binding["id"],
                )
        if capability["readiness"]["probe"] not in probes:
            raise CompositionError(
                "unregistered_probe",
                f"{where}: readiness probe {capability['readiness']['probe']} is not registered",
                probe=capability["readiness"]["probe"],
            )
        for key in ("input_contract", "output_contract"):
            contract = capability[key]
            if contract["name"] in _FLOATING_RANGES:
                errors.append(f"{where}.{key}: contract name is required")
    for index, source in enumerate(value.get("source_packs", [])):
        try:
            check_range(source["range"])
        except CompositionError as exc:
            errors.append(f"source_packs[{index}]: {exc.message}")
    _fail("invalid_provider", PROVIDER_CONTRACT, errors)
    computed = descriptor_hash(value)
    if value.get("descriptor_hash") not in (None, computed):
        raise CompositionError(
            "descriptor_hash_mismatch",
            "provider descriptor content does not match its declared hash",
            declared=value["descriptor_hash"],
            computed=computed,
        )
    value["descriptor_hash"] = computed
    return value


def validate_providers(descriptors: Iterable[Mapping[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
    """Validate a provider set; two different providers may not own one store."""

    validated = [validate_provider(item, **kwargs) for item in descriptors]
    owners: dict[str, str] = {}
    for descriptor in validated:
        for store in descriptor["stores"]:
            kind = store["record_kind"]
            owner = owners.setdefault(kind, descriptor["provider_id"])
            if owner != descriptor["provider_id"]:
                raise CompositionError(
                    "conflicting_store_owner",
                    f"record kind {kind} is claimed by {owner} and {descriptor['provider_id']}",
                    record_kind=kind,
                    providers=sorted({owner, descriptor["provider_id"]}),
                )
    return validated


def load_providers(root: str | Path | None = None, **kwargs: Any) -> list[dict[str, Any]]:
    """Provider descriptors shipped under ``config/composition/providers``."""

    base = Path(root) if root is not None else PROVIDER_DIR
    documents = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(base.glob("*.json"))
    ]
    return validate_providers(documents, **kwargs)


def provided_capabilities(descriptors: Iterable[Mapping[str, Any]]) -> set[str]:
    return {c["capability"] for d in descriptors for c in d["capabilities"]}


def contract_candidates(*, include_domain_packs: bool = True) -> dict[str, list[str]]:
    """The contract candidate set: name -> available versions.

    Sources, all existing: every JSON Schema file ``noesis-<name>-v<N>.json``
    (under both its full stem and its short ``<name>``, at ``N.0.0``), the
    schema-registry builtins, and contracts that code-registered domain packs
    declare in ``schema_versions``. This function reads files; the resolver
    itself only receives its result.
    """

    import re

    found: dict[str, set[str]] = {}
    for path in SCHEMA_DIR.glob("noesis-*-v*.json"):
        match = re.fullmatch(r"noesis-(.+)-v(\d+)", path.stem)
        if not match:
            continue
        version = f"{int(match.group(2))}.0.0"
        found.setdefault(path.stem, set()).add(version)
        found.setdefault(match.group(1), set()).add(version)
    for contract, (name, version, _) in CONTRACT_MODULES.items():
        found.setdefault(name, set()).add(version)
        found.setdefault(contract, set()).add(version)
    if include_domain_packs:
        from src.composition.identifiers import code_registered_packs

        for pack in code_registered_packs().values():
            for name, version in pack.schema_versions.items():
                found.setdefault(str(name), set()).add(str(version))
    return {name: sorted(versions, key=version_key) for name, versions in sorted(found.items())}


# --------------------------------------------------------------------------- #
# C02.4 - resolved composition plan
# --------------------------------------------------------------------------- #

def canonicalize_plan(value: Any) -> Any:
    """Canonical form of a plan: every array is a set, sorted by canonical JSON.

    Object keys are ordered by :func:`canonical` at serialization time. The
    hash algorithm is SHA-256 over the canonical JSON of the plan without its
    ``digest`` field. Plans contain no observation-time fields, so nothing is
    excluded besides the digest itself.
    """

    if isinstance(value, Mapping):
        return {key: canonicalize_plan(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [canonicalize_plan(item) for item in value]
        return sorted(items, key=canonical)
    return value


def plan_digest(plan: Mapping[str, Any]) -> str:
    return digest(canonicalize_plan({k: v for k, v in plan.items() if k != "digest"}))


def validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    value = _json_copy(dict(plan))
    for key, where in _keys(value):
        if key in DYNAMIC_KEYS:
            raise CompositionError(
                "dynamic_field_in_plan",
                f"plans are immutable and cannot carry observation-time or secret facts ({where})",
                path=where,
            )
    _fail("invalid_plan", PLAN_CONTRACT, schema_errors(value, PLAN_CONTRACT))
    if value["digest"] != plan_digest(value):
        raise CompositionError("plan_digest_mismatch", "plan content does not match its digest")
    return canonicalize_plan(value)


# --------------------------------------------------------------------------- #
# C02.5 - readiness assessment and activation receipt
# --------------------------------------------------------------------------- #

def validate_readiness(assessment: Mapping[str, Any]) -> dict[str, Any]:
    value = _json_copy(dict(assessment))
    _fail("invalid_readiness", READINESS_CONTRACT, schema_errors(value, READINESS_CONTRACT))
    return value


def receipt_hash(receipt: Mapping[str, Any]) -> str:
    return digest({key: value for key, value in receipt.items() if key != "receipt_hash"})


def validate_receipt(
    receipt: Mapping[str, Any], *, generation_plans: Mapping[int, str] | None = None
) -> dict[str, Any]:
    """Validate an activation receipt.

    ``generation_plans`` maps generation numbers to the plan digest each was
    published with; a receipt whose new generation names a different digest
    is rejected.
    """

    value = _json_copy(dict(receipt))
    _fail("invalid_receipt", RECEIPT_CONTRACT, schema_errors(value, RECEIPT_CONTRACT))
    if value["receipt_hash"] != receipt_hash(value):
        raise CompositionError("receipt_hash_mismatch", "receipt content does not match its hash")
    if generation_plans is not None and value["new_generation"] is not None:
        expected = generation_plans.get(int(value["new_generation"]))
        if expected is not None and expected != value["plan_digest"]:
            raise CompositionError(
                "generation_digest_mismatch",
                "receipt plan digest does not match the plan of its generation",
                generation=value["new_generation"],
            )
    return value


# --------------------------------------------------------------------------- #
# C02.6 - schema-registry identities
# --------------------------------------------------------------------------- #

def builtin_contract_definitions() -> list[dict[str, Any]]:
    """Schema-registry module definitions for the composition contracts."""

    definitions = []
    for contract, (name, version, depends) in sorted(CONTRACT_MODULES.items()):
        definitions.append(
            {
                "name": name,
                "kind": "schema",
                "semantic_version": version,
                "content": _schema(contract),
                "dependencies": [
                    {"kind": "schema", "name": dep, "version": f"^{CONTRACT_MODULE_VERSIONS[dep]}"}
                    for dep in depends
                ],
            }
        )
    return definitions



def declare_contract_dependencies(registry: Any, *, principal_id: str, scopes: Iterable[str]) -> list[dict[str, Any]]:
    """Declare contract-to-contract dependencies with ``declare_dependency``."""

    from src.kb.schema_registry import READ_SCOPE

    results = []
    for name, version, depends in CONTRACT_MODULES.values():
        consumer = registry.resolve("schema", name, version, scopes={READ_SCOPE})
        for dep in depends:
            module = registry.resolve(
                "schema", dep, CONTRACT_MODULE_VERSIONS[dep], scopes={READ_SCOPE}
            )
            results.append(
                registry.declare_dependency(
                    module["module_id"], "module", consumer["module_id"],
                    {"relation": "references", "contract": name},
                    principal_id=principal_id, scopes=scopes,
                )
            )
    return results


__all__ = [
    "BLOCKER_KINDS",
    "CONTRACT_MODULES",
    "EFFECTS",
    "EFFECT_RANK",
    "PACK_FORMAT_V2",
    "PLAN_CONTRACT",
    "PROVIDER_CONTRACT",
    "READINESS_CONTRACT",
    "RECEIPT_CONTRACT",
    "CompositionError",
    "adapt_domain_pack",
    "adapt_v1",
    "builtin_contract_definitions",
    "canonical",
    "canonicalize_plan",
    "contract_candidates",
    "check_range",
    "declare_contract_dependencies",
    "descriptor_hash",
    "digest",
    "load_all_packs",
    "load_pack",
    "load_providers",
    "manifest_hash",
    "manifest_id",
    "plan_digest",
    "provided_capabilities",
    "receipt_hash",
    "satisfies",
    "schema_errors",
    "v1_view",
    "validate_manifest",
    "validate_plan",
    "validate_provider",
    "validate_providers",
    "validate_readiness",
    "validate_receipt",
    "version_key",
]
