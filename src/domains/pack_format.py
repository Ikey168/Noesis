"""
Distributable domain-pack format (M9.1).

A :class:`~src.domains.base.DomainPack` is defined in Python and registered at
import. That is fine for the built-in packs, but it cannot be *distributed*: you
cannot hand someone a pack and have them install it without editing code. This
module defines a declarative, serializable pack format, ``noesis-pack-v1``, that
bundles everything a pack contributes into one installable manifest:

* **panels** - the renderable panel definitions (catalog ``PanelDef`` shape),
* **planner_keywords** - facet -> keywords, merged into the planner's routing,
* **enrichers** - declarative enricher descriptors (a small, code-free rule set),
* **provisioning_templates** - named deploy/attach templates (Track P / P2),
* **ui_flags** and **source_types** - the gating and routing metadata.
* **capabilities**, **schema_versions**, and **ontology_extensions** - explicit
  machine-readable contracts for domain-specific knowledge behavior.

The manifest is pure data (JSON), so a pack is a single file that can be
validated, published (M9.2) and installed into a fresh instance without code
changes (M9.3). :func:`validate_manifest` mirrors the JSON-schema contract at
``contracts/schemas/jsonschema/noesis-pack-v1.json`` in pure Python (the same
double-validation convention the ui-spec uses), so validation never depends on
an optional package.

Stdlib-only; imports only the light, stdlib-safe ``catalog`` / ``spec`` modules.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from src.domains.pack_vocab import FACETS
from src.domains.pack_vocab import MAX_SPAN, MIN_SPAN, SOURCE_TYPES

PACK_FORMAT = "noesis-pack-v1"

# A pack name is a namespace token; a version is semver-ish (major.minor.patch).
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")

# The declarative enricher kinds a distributable pack may ship. Kept deliberately
# small and code-free: each compiles to a pure, deterministic function at install
# (M9.3). ``keyword_tag`` tags a document when its text matches any keyword.
ENRICHER_KINDS = ("keyword_tag",)

# Provisioning-template backends (mirror src.provisioning.namespaces).
TEMPLATE_BACKENDS = ("table-prefix", "attached", "postgres")

MANIFEST_FILENAME = "pack.json"


@dataclass
class PackManifest:
    """A declarative, distributable domain pack."""

    name: str
    version: str
    description: str = ""
    source_types: List[str] = field(default_factory=list)
    ui_flags: Dict[str, bool] = field(default_factory=dict)
    panels: List[Dict[str, Any]] = field(default_factory=list)
    planner_keywords: Dict[str, List[str]] = field(default_factory=dict)
    enrichers: List[Dict[str, Any]] = field(default_factory=list)
    provisioning_templates: List[Dict[str, Any]] = field(default_factory=list)
    capabilities: List[str] = field(default_factory=list)
    schema_versions: Dict[str, str] = field(default_factory=dict)
    ontology_extensions: Dict[str, Any] = field(default_factory=dict)
    pack_format: str = PACK_FORMAT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pack_format": self.pack_format,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "source_types": list(self.source_types),
            "ui_flags": dict(self.ui_flags),
            "panels": [dict(p) for p in self.panels],
            "planner_keywords": {k: list(v) for k, v in self.planner_keywords.items()},
            "enrichers": [dict(e) for e in self.enrichers],
            "provisioning_templates": [dict(t) for t in self.provisioning_templates],
            "capabilities": list(self.capabilities),
            "schema_versions": dict(self.schema_versions),
            "ontology_extensions": dict(self.ontology_extensions),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PackManifest":
        """Build a manifest from a plain dict with light coercion (never raises);
        call :func:`validate_manifest` to enforce the contract."""
        data = data or {}
        return cls(
            name=str(data.get("name") or ""),
            version=str(data.get("version") or ""),
            description=str(data.get("description") or ""),
            source_types=[str(s) for s in (data.get("source_types") or [])],
            ui_flags={str(k): bool(v) for k, v in (data.get("ui_flags") or {}).items()},
            panels=[p for p in (data.get("panels") or []) if isinstance(p, dict)],
            planner_keywords={
                str(k): [str(w) for w in (v or [])]
                for k, v in (data.get("planner_keywords") or {}).items()
            },
            enrichers=[e for e in (data.get("enrichers") or []) if isinstance(e, dict)],
            provisioning_templates=[
                t for t in (data.get("provisioning_templates") or []) if isinstance(t, dict)
            ],
            capabilities=[str(value) for value in (data.get("capabilities") or [])],
            schema_versions={
                str(key): str(value)
                for key, value in (data.get("schema_versions") or {}).items()
            },
            ontology_extensions=dict(data.get("ontology_extensions") or {}),
            pack_format=str(data.get("pack_format") or PACK_FORMAT),
        )


def _validate_panel(panel: Dict[str, Any], where: str, errors: List[str]) -> None:
    if not isinstance(panel, dict):
        errors.append(f"{where} must be an object")
        return
    ptype = panel.get("type")
    if not isinstance(ptype, str) or not ptype:
        errors.append(f"{where}.type must be a non-empty string")
    if not isinstance(panel.get("title"), str) or not panel.get("title"):
        errors.append(f"{where}.title must be a non-empty string")
    facets = panel.get("facets", [])
    if not isinstance(facets, list) or any(f not in FACETS for f in facets):
        errors.append(f"{where}.facets must be a list drawn from {FACETS}")
    span = panel.get("default_span", MIN_SPAN)
    if not isinstance(span, int) or isinstance(span, bool) or not MIN_SPAN <= span <= MAX_SPAN:
        errors.append(f"{where}.default_span must be an integer in [{MIN_SPAN}, {MAX_SPAN}]")
    for key in ("endpoint", "ui_flag", "topic_param", "source_type_param", "description"):
        val = panel.get(key)
        if val is not None and not isinstance(val, str):
            errors.append(f"{where}.{key} must be null or a string")
    tables = panel.get("tables", [])
    if not isinstance(tables, list) or any(not isinstance(t, str) for t in tables):
        errors.append(f"{where}.tables must be a list of strings")


def _validate_enricher(enricher: Dict[str, Any], where: str, errors: List[str]) -> None:
    if not isinstance(enricher, dict):
        errors.append(f"{where} must be an object")
        return
    if not isinstance(enricher.get("name"), str) or not enricher.get("name"):
        errors.append(f"{where}.name must be a non-empty string")
    kind = enricher.get("kind")
    if kind not in ENRICHER_KINDS:
        errors.append(f"{where}.kind must be one of {ENRICHER_KINDS}")
    st = enricher.get("source_types", [])
    if not isinstance(st, list) or any(s not in SOURCE_TYPES for s in st):
        errors.append(f"{where}.source_types must be a list drawn from {SOURCE_TYPES}")
    params = enricher.get("params", {})
    if not isinstance(params, dict):
        errors.append(f"{where}.params must be an object")
    elif kind == "keyword_tag":
        # keyword_tag tags `label` onto a document when `field`'s text contains
        # any of `keywords`.
        if not isinstance(params.get("field"), str) or not params.get("field"):
            errors.append(f"{where}.params.field must be a non-empty string")
        if not isinstance(params.get("label"), str) or not params.get("label"):
            errors.append(f"{where}.params.label must be a non-empty string")
        kws = params.get("keywords")
        if not isinstance(kws, list) or not kws or any(not isinstance(k, str) for k in kws):
            errors.append(f"{where}.params.keywords must be a non-empty list of strings")


def _validate_template(template: Dict[str, Any], where: str, errors: List[str]) -> None:
    if not isinstance(template, dict):
        errors.append(f"{where} must be an object")
        return
    name = template.get("name")
    if not isinstance(name, str) or not re.match(r"^[a-z][a-z0-9_]{1,30}$", name or ""):
        errors.append(f"{where}.name must be a valid KG name ([a-z][a-z0-9_]{{1,30}})")
    if not isinstance(template.get("description", ""), str):
        errors.append(f"{where}.description must be a string")
    sources = template.get("sources", [])
    if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
        errors.append(f"{where}.sources must be a list of strings")
    backend = template.get("backend", "table-prefix")
    if backend not in TEMPLATE_BACKENDS:
        errors.append(f"{where}.backend must be one of {TEMPLATE_BACKENDS}")


def validate_manifest(data: Dict[str, Any]) -> List[str]:
    """Validate a manifest dict against the ``noesis-pack-v1`` contract.

    Returns a list of human-readable errors; empty means valid.
    """
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["manifest must be an object"]

    if data.get("pack_format") != PACK_FORMAT:
        errors.append(f"pack_format must be '{PACK_FORMAT}'")

    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name or ""):
        errors.append("name must match [a-z][a-z0-9_-]{1,31}")

    version = data.get("version")
    if not isinstance(version, str) or not VERSION_RE.match(version or ""):
        errors.append("version must be semver major.minor.patch")

    if not isinstance(data.get("description", ""), str):
        errors.append("description must be a string")

    source_types = data.get("source_types", [])
    if not isinstance(source_types, list) or any(s not in SOURCE_TYPES for s in source_types):
        errors.append(f"source_types must be a list drawn from {SOURCE_TYPES}")

    ui_flags = data.get("ui_flags", {})
    if not isinstance(ui_flags, dict) or any(not isinstance(v, bool) for v in ui_flags.values()):
        errors.append("ui_flags must be a map of string -> bool")

    panels = data.get("panels", [])
    if not isinstance(panels, list):
        errors.append("panels must be a list")
    else:
        for i, panel in enumerate(panels):
            _validate_panel(panel, f"panels[{i}]", errors)

    keywords = data.get("planner_keywords", {})
    if not isinstance(keywords, dict):
        errors.append("planner_keywords must be an object")
    else:
        for facet, words in keywords.items():
            if facet not in FACETS:
                errors.append(f"planner_keywords facet '{facet}' is not in {FACETS}")
            if not isinstance(words, list) or any(not isinstance(w, str) for w in words):
                errors.append(f"planner_keywords['{facet}'] must be a list of strings")

    enrichers = data.get("enrichers", [])
    if not isinstance(enrichers, list):
        errors.append("enrichers must be a list")
    else:
        for i, enricher in enumerate(enrichers):
            _validate_enricher(enricher, f"enrichers[{i}]", errors)

    templates = data.get("provisioning_templates", [])
    if not isinstance(templates, list):
        errors.append("provisioning_templates must be a list")
    else:
        for i, template in enumerate(templates):
            _validate_template(template, f"provisioning_templates[{i}]", errors)

    capabilities = data.get("capabilities", [])
    if not isinstance(capabilities, list) or any(
        not isinstance(value, str) or not value for value in capabilities
    ):
        errors.append("capabilities must be a list of non-empty strings")

    schema_versions = data.get("schema_versions", {})
    if not isinstance(schema_versions, dict) or any(
        not isinstance(key, str)
        or not key
        or not isinstance(value, str)
        or not VERSION_RE.match(value)
        for key, value in schema_versions.items()
    ):
        errors.append("schema_versions must map non-empty names to semantic versions")

    ontology = data.get("ontology_extensions", {})
    if not isinstance(ontology, dict):
        errors.append("ontology_extensions must be an object")

    # A pack must contribute *something* to be worth distributing.
    if not any(data.get(k) for k in ("panels", "planner_keywords", "enrichers",
                                     "provisioning_templates", "ui_flags",
                                     "capabilities", "ontology_extensions")):
        errors.append("a pack must contribute at least one capability")

    return errors


class PackFormatError(ValueError):
    """A manifest failed validation."""


def package_pack(manifest: PackManifest, dest_dir: str) -> str:
    """Serialize a validated manifest into ``<dest_dir>/pack.json`` and return
    the path. Refuses to write an invalid manifest, so a package on disk is
    always contract-valid."""
    payload = manifest.to_dict()
    errors = validate_manifest(payload)
    if errors:
        raise PackFormatError(f"manifest is invalid: {'; '.join(errors[:3])}")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / MANIFEST_FILENAME
    path.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding="utf-8")
    return str(path)


def load_manifest(path: str) -> PackManifest:
    """Read a packaged manifest from ``path`` (a ``pack.json`` file or its
    directory), validate it, and return the :class:`PackManifest`. Raises
    :class:`PackFormatError` on an invalid or unreadable package."""
    p = Path(path)
    if p.is_dir():
        p = p / MANIFEST_FILENAME
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as err:
        raise PackFormatError(f"could not read pack manifest at {path}: {err}")
    errors = validate_manifest(data)
    if errors:
        raise PackFormatError(f"manifest is invalid: {'; '.join(errors[:3])}")
    return PackManifest.from_dict(data)


__all__ = [
    "PACK_FORMAT",
    "ENRICHER_KINDS",
    "TEMPLATE_BACKENDS",
    "MANIFEST_FILENAME",
    "PackManifest",
    "PackFormatError",
    "validate_manifest",
    "package_pack",
    "load_manifest",
]
