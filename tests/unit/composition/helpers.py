"""Builders for synthetic composition candidate sets used across composition tests."""

from __future__ import annotations

from typing import Any

from src.composition import contracts as c

TOOL = "noesis-knowledge-engine.calculate_spatial_relation"
KNOWN_TOOLS = {TOOL}
PROBES = {"geospatial.local-store", "geospatial.live-source"}
CONTRACTS = {"noesis-spatial-result-v1": ["1.0.0"], "noesis-geospatial-geometry-v2": ["2.0.0"],
             "fixture-contract": ["1.0.0", "1.1.0"]}


def capability(
    capability_id: str,
    version: str = "1.0.0",
    *,
    effect: str = "read-only",
    semantics: dict[str, Any] | None = None,
    record_kinds: list[str] | None = None,
    idempotent: bool = True,
    receipt: bool = False,
) -> dict[str, Any]:
    return {
        "capability": capability_id,
        "version": version,
        "input_contract": {"name": "noesis-geospatial-geometry-v2", "version": "2.0.0"},
        "output_contract": {"name": "noesis-spatial-result-v1", "version": "1.0.0"},
        "semantics": dict(semantics or {"algorithm": "wgs84-stdlib-v1"}),
        "effect": effect,
        "idempotent": idempotent,
        "execution_receipt": receipt,
        "required_scopes": ["knowledge:geospatial:calculate"],
        "required_context": ["namespace"],
        "record_kinds": list(record_kinds or []),
        "bindings": [{"kind": "mcp-tool", "id": TOOL}],
        "readiness": {"probe": "geospatial.local-store"},
    }


def provider(
    provider_id: str,
    version: str = "1.0.0",
    capabilities: list[dict[str, Any]] | None = None,
    *,
    stores: list[str] | None = None,
    source_packs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    document = {
        "contract": c.PROVIDER_CONTRACT,
        "provider_id": provider_id,
        "version": version,
        "implementation": {"identity": provider_id, "version": version},
        "capabilities": capabilities or [capability("spatial.relation")],
        "stores": [{"record_kind": kind, "tables": [kind]} for kind in (stores or [])],
    }
    if source_packs:
        document["source_packs"] = source_packs
    return c.validate_provider(document, known_tools=KNOWN_TOOLS, registered_bindings=set(),
                               registered_probes=PROBES)


def manifest(
    name: str,
    version: str = "1.0.0",
    *,
    requires: list[dict[str, Any]] | None = None,
    providers: list[tuple[str, str]] | None = None,
    features: list[str] | None = None,
    contracts: list[dict[str, str]] | None = None,
    source_packs: list[dict[str, Any]] | None = None,
    templates: list[dict[str, Any]] | None = None,
    profiles: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "pack_format": c.PACK_FORMAT_V2,
        "name": name,
        "version": version,
        "requires": list(requires or []),
        "contributes": {},
    }
    if providers:
        document["contributes"]["providers"] = [
            {"provider_id": pid, "version": ver} for pid, ver in providers
        ]
    if templates:
        document["contributes"]["workflow_templates"] = templates
    if profiles:
        document["contributes"]["profiles"] = profiles
    if features:
        document["optional_features"] = {f: {"description": f} for f in features}
    references: dict[str, Any] = {}
    if contracts:
        references["contracts"] = contracts
    if source_packs:
        references["source_packs"] = source_packs
    if references:
        document["references"] = references
    return c.validate_manifest(document)


def req(capability_id: str, spec: str = "^1.0.0", **extra: Any) -> dict[str, Any]:
    return {"capability": capability_id, "range": spec, **extra}
