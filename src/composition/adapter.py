"""Read-only adapter from v1 bundles to composition manifests (C02.2).

Every ``packs/*/pack.json`` (``noesis-pack-v1``) and every code-registered
:class:`~src.domains.base.DomainPack` can be expressed as a
``noesis-pack-composition-v1`` manifest with the same runtime meaning. The
adapter never writes back to v1 manifests and never changes enablement.

What v1 cannot express is filled with declared, conservative defaults and
listed in ``adapter.supplied_defaults``:

* ``requires`` / ``optional_features``: v1 has no requirements, so none;
* ``capability-contracts``: each contributed capability gets the contract
  ``legacy.<bundle>.<capability>`` at ``1.0.0`` until a provider descriptor
  declares the real contract;
* ``store-ownership``: v1 declares no stores; ownership lives only in
  provider descriptors, so C03 treats an adapted bundle as owning none.

Naming (inventory unknowns): directory names ``economic`` / ``technical`` and
the code pack ``research`` are aliases of the bundles ``economics``,
``technology`` and ``science``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from src.composition.contracts import MANIFEST_CONTRACT, seal_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
V1_FORMAT = "noesis-pack-v1"
LEGACY_ALIASES = {"economic": "economics", "technical": "technology", "research": "science"}
_ADVISORY_V1 = ("panels", "planner_keywords", "query_examples", "exclusions")
_V1_FIELDS = {"pack_format", "name", "version", "description", "source_types", "ui_flags", "panels",
              "planner_keywords", "enrichers", "provisioning_templates", "capabilities", "schema_versions",
              "ontology_extensions", "source_pack", "query_examples", "exclusions"}
DEFAULT_VERSION = "1.0.0"


def bundle_id(name: str) -> str:
    return LEGACY_ALIASES.get(name, name)


def capability_id(bundle: str, legacy_name: str) -> str:
    return f"{bundle}.{legacy_name}"


def _source_pack_ref(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    config = REPO_ROOT / path
    try:
        data = json.loads(config.read_text())
    except (OSError, ValueError):
        return [{"pack_id": Path(path).stem, "version": "unresolved"}]
    return [{"pack_id": str(data["pack_id"]), "version": str(data["version"])}]


def _capabilities(bundle: str, names: Iterable[str]) -> list[dict[str, Any]]:
    return [{"id": capability_id(bundle, name), "legacy_name": name,
             "contract": {"name": f"legacy.{bundle}.{name}", "version": DEFAULT_VERSION}}
            for name in names]


def from_pack_manifest(manifest: Any) -> dict[str, Any]:
    """Adapt a v1 manifest (``PackManifest`` or its dict) without dropping fields."""

    data = manifest.to_dict() if hasattr(manifest, "to_dict") else dict(manifest)
    if data.get("pack_format", V1_FORMAT) != V1_FORMAT:
        raise ValueError("adapter input must be noesis-pack-v1")
    bundle = bundle_id(str(data["name"]))
    unknown = sorted(set(data) - _V1_FIELDS)
    advisory = {key: data[key] for key in _ADVISORY_V1 if data.get(key)}
    if unknown:
        advisory["v1_unrecognized_fields"] = {key: data[key] for key in unknown}
    document = {
        "pack_format": MANIFEST_CONTRACT,
        "id": bundle,
        "version": str(data["version"]),
        "description": str(data.get("description") or ""),
        "contributes": {
            "capabilities": _capabilities(bundle, data.get("capabilities") or []),
            "source_packs": _source_pack_ref(data.get("source_pack")),
            "enrichers": [dict(e) for e in data.get("enrichers") or []],
            "schema_versions": dict(data.get("schema_versions") or {}),
            "ontology_extensions": dict(data.get("ontology_extensions") or {}),
            "provisioning_templates": [dict(t) for t in data.get("provisioning_templates") or []],
            "ui_flags": dict(data.get("ui_flags") or {}),
            "source_types": list(data.get("source_types") or []),
        },
        "requires": [],
        "compatibility_aliases": {alias: bundle for alias, target in LEGACY_ALIASES.items()
                                  if target == bundle and alias != bundle},
        "advisory": advisory,
        "adapter": {"source": V1_FORMAT,
                    "supplied_defaults": ["requires", "optional_features", "capability-contracts",
                                          "store-ownership"],
                    "legacy_names": sorted({str(data["name"]), bundle})},
    }
    return seal_manifest(document)


def from_domain_pack(pack: Any, *, version: str = DEFAULT_VERSION) -> dict[str, Any]:
    """Adapt a code-registered ``DomainPack``.

    Route modules and code enrichers are *registrations*, not contributions a
    manifest may execute; they are recorded as advisory identifiers so the
    catalog can preserve them, and are never loaded from the manifest.
    """

    bundle = bundle_id(pack.name)
    advisory: dict[str, Any] = {}
    if pack.route_modules:
        advisory["legacy_route_modules"] = list(pack.route_modules)
    if pack.enrichers:
        advisory["legacy_code_enrichers"] = [e.name for e in pack.enrichers]
    document = {
        "pack_format": MANIFEST_CONTRACT,
        "id": bundle,
        "version": version,
        "description": pack.description,
        "contributes": {
            "capabilities": _capabilities(bundle, pack.capabilities),
            "schema_versions": dict(pack.schema_versions),
            "ontology_extensions": dict(pack.ontology_extensions),
            "ui_flags": dict(pack.ui_flags),
            "source_types": list(pack.source_types),
        },
        "requires": [],
        "compatibility_aliases": {alias: bundle for alias, target in LEGACY_ALIASES.items()
                                  if target == bundle and alias != bundle},
        "advisory": advisory,
        "adapter": {"source": "domain-pack",
                    "supplied_defaults": ["requires", "optional_features", "capability-contracts",
                                          "store-ownership", "version"],
                    "legacy_names": sorted({pack.name, bundle})},
    }
    return seal_manifest(document)


def adapt_bundle(manifest: Any = None, domain_pack: Any = None) -> dict[str, Any]:
    """One composition manifest per bundle when both a v1 manifest and a code pack exist.

    The manifest is the declaration source; the code pack contributes its
    advisory registrations and any capability strings the manifest lacks.
    """

    if manifest is None and domain_pack is None:
        raise ValueError("adapt_bundle needs a manifest or a domain pack")
    if manifest is None:
        return from_domain_pack(domain_pack)
    adapted = from_pack_manifest(manifest)
    if domain_pack is None:
        return adapted
    code = from_domain_pack(domain_pack, version=adapted["version"])
    body = {k: v for k, v in adapted.items() if k != "content_hash"}
    have = {c["id"] for c in body["contributes"]["capabilities"]}
    body["contributes"]["capabilities"] += [c for c in code["contributes"]["capabilities"] if c["id"] not in have]
    for key in ("schema_versions", "ontology_extensions", "ui_flags"):
        body["contributes"][key] = {**code["contributes"].get(key, {}), **body["contributes"].get(key, {})}
    body["advisory"] = {**code["advisory"], **body["advisory"]}
    body["adapter"] = {"source": "noesis-pack-v1+domain-pack",
                       "supplied_defaults": sorted(set(adapted["adapter"]["supplied_defaults"])),
                       "legacy_names": sorted(set(adapted["adapter"]["legacy_names"])
                                              | set(code["adapter"]["legacy_names"]))}
    return seal_manifest(body)


def apply_overlay(manifest: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge a bundle's ``composition.json`` declarations into its adapted manifest.

    The overlay supplies what v1 cannot express (requirements, optional
    features, provider-backed capabilities, workflow templates, profiles and
    ranged source-pack references). Legacy fields are kept as they are, so the
    v1 registration and public behavior do not change.
    """

    body = json.loads(json.dumps({k: v for k, v in manifest.items() if k != "content_hash"}))
    supplied = set(body["adapter"]["supplied_defaults"])
    for key in ("requires", "optional_features"):
        if key in overlay:
            body[key] = [dict(item) for item in overlay[key]]
            supplied.discard(key)
    contributes = body.setdefault("contributes", {})
    for key, items in (overlay.get("contributes") or {}).items():
        if key in {"capabilities", "providers", "workflow_templates", "profiles"}:
            ids = {item["id"] for item in items}
            contributes[key] = [c for c in contributes.get(key) or [] if c.get("id") not in ids] + [dict(i) for i in items]
        elif key == "source_packs":
            packs = {item["pack_id"] for item in items}
            contributes[key] = ([c for c in contributes.get(key) or [] if c.get("pack_id") not in packs]
                                + [dict(i) for i in items])
        else:
            contributes[key] = items
    body["adapter"] = {**body["adapter"], "source": body["adapter"]["source"] + "+composition-overlay",
                       "supplied_defaults": sorted(supplied)}
    return seal_manifest(body)


def workflow_templates(root: Path | None = None) -> list[dict[str, Any]]:
    """Workflow templates shipped under ``packs/<bundle>/workflows/*.json``."""

    root = root or REPO_ROOT / "packs"
    return [json.loads(path.read_text()) for path in sorted(root.glob("*/workflows/*.json"))]


def v1_view(composition: Mapping[str, Any]) -> dict[str, Any]:
    """What a v1 registration of this bundle exposes, for round-trip checks.

    For adapted bundles only the legacy capability names count; capabilities
    a composition overlay adds are composition contributions, not v1 names.
    """

    contributes = composition.get("contributes") or {}
    capabilities = contributes.get("capabilities") or []
    if composition.get("adapter"):
        capabilities = [c for c in capabilities if c.get("legacy_name")]
    return {
        "capabilities": [c.get("legacy_name") or c["id"].split(".", 1)[1] for c in capabilities],
        "schema_versions": dict(contributes.get("schema_versions") or {}),
        "ontology_extensions": dict(contributes.get("ontology_extensions") or {}),
    }


def code_packs() -> list[Any]:
    """Code-registered bundles under ``src/domains`` (import registers them)."""

    import importlib

    packs = []
    for init in sorted((REPO_ROOT / "src/domains").glob("*/pack.py")):
        module = importlib.import_module(f"src.domains.{init.parent.name}.pack")
        packs += [v for v in vars(module).values() if type(v).__name__ == "DomainPack"]
    return packs


def adapt_all(root: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every bundle in the checkout as one composition manifest, keyed by bundle id."""

    root = root or REPO_ROOT / "packs"
    manifests = {}
    for path in sorted(root.glob("*/pack.json")):
        data = json.loads(path.read_text())
        manifests[bundle_id(data["name"])] = data
    overlays = {bundle_id(path.parent.name): json.loads(path.read_text())
                for path in sorted(root.glob("*/composition.json"))}
    code = {bundle_id(p.name): p for p in code_packs()}
    adapted = {}
    for bundle in sorted(set(manifests) | set(code)):
        manifest = adapt_bundle(manifests.get(bundle), code.get(bundle))
        adapted[bundle] = apply_overlay(manifest, overlays[bundle]) if bundle in overlays else manifest
    for manifest in native_manifests(root):
        adapted[manifest["id"]] = manifest
    return adapted


def native_manifests(root: Path | None = None) -> list[dict[str, Any]]:
    """Bundles authored directly as composition manifests (``packs/<bundle>/manifest.json``)."""

    root = root or REPO_ROOT / "packs"
    return [json.loads(path.read_text()) for path in sorted(root.glob("*/manifest.json"))]
