"""
Install a domain pack into a running instance (M9.3).

Given a published pack (M9.2), install compiles its declarative manifest (M9.1)
into live registrations, so a fresh instance gains the pack's capabilities with
**no code changes**:

* **panels** -> registered into the catalog runtime, so they validate in a spec
  and surface on ``GET /api/v1/ui/panels``;
* **planner_keywords** -> merged into the planner's facet routing, so the pack's
  vocabulary steers layout;
* **enrichers** -> the declarative descriptors compile to pure functions and,
  with the pack's ``ui_flags``, register + enable a :class:`DomainPack`;
* **provisioning_templates** -> registered as named, deployable templates
  (enabled: :func:`deploy_template` stands one up through the Provisioner).

Install is reversible (:func:`uninstall`) and idempotent per pack name. It holds
no database handle: the warehouse connection is only needed to *deploy* a
template, and is injected there.

Stdlib-only; the provisioning import is lazy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from src.domains import pack_registry
from src.domains import registry as domain_registry
from src.domains.base import DomainPack, Enricher
from src.domains.pack_format import PackManifest

# Installed packs and the runtime registrations to undo on uninstall.
_INSTALLED: Dict[str, Dict[str, Any]] = {}
# Named provisioning templates contributed by installed packs.
_TEMPLATES: Dict[str, Dict[str, Any]] = {}


class PackInstallError(RuntimeError):
    """A pack could not be installed (e.g. not found, or an unknown enricher)."""


def _doc_field(document: Any, field: str) -> Optional[str]:
    if isinstance(document, dict):
        value = document.get(field)
    else:
        value = getattr(document, field, None)
    return value if isinstance(value, str) else None


def _compile_enricher(desc: Dict[str, Any]) -> Enricher:
    """Compile a declarative enricher descriptor into a runtime Enricher. Only
    the vetted, code-free kinds are accepted (no arbitrary code executes)."""
    kind = desc.get("kind")
    params = desc.get("params", {}) or {}
    if kind == "keyword_tag":
        field = params["field"]
        label = params["label"]
        keywords = [str(k).lower() for k in params["keywords"]]

        def fn(document, _field=field, _label=label, _kws=tuple(keywords)):
            text = _doc_field(document, _field)
            if text and any(kw in text.lower() for kw in _kws):
                return {"tag": _label}
            return None

        return Enricher(
            name=desc["name"],
            fn=fn,
            source_types=list(desc.get("source_types", []) or []),
            description=desc.get("description", ""),
        )
    raise PackInstallError(f"unknown enricher kind {kind!r}")


def install_manifest(manifest: PackManifest) -> Dict[str, Any]:
    """Install an in-memory manifest's capabilities into the running instance.
    Reinstalling the same pack replaces its prior registration.

    A pack's ``panels`` and ``planner_keywords`` are recorded but no longer
    registered anywhere: the generative UI they fed has been retired, so they
    are advisory metadata now. The pack's enrichers, ui_flags and provisioning
    templates install as before."""
    if manifest.name in _INSTALLED:
        uninstall(manifest.name)

    # Enrichers -> a synthesized, enabled DomainPack (also carries the ui_flags).
    enrichers = [_compile_enricher(e) for e in manifest.enrichers]
    pack = DomainPack(
        name=manifest.name,
        description=manifest.description,
        source_types=list(manifest.source_types),
        enrichers=enrichers,
        ui_flags=dict(manifest.ui_flags),
    )
    domain_registry.register_pack(pack)
    domain_registry.enable_pack(manifest.name)

    # Provisioning templates -> the deployable-template registry.
    template_names: List[str] = []
    for template in manifest.provisioning_templates:
        _TEMPLATES[template["name"]] = {**template, "pack": manifest.name}
        template_names.append(template["name"])

    _INSTALLED[manifest.name] = {
        "version": manifest.version,
        "panels": [p["type"] for p in manifest.panels],
        "keywords": {f: list(w) for f, w in manifest.planner_keywords.items()},
        "templates": template_names,
    }
    return {
        "name": manifest.name,
        "version": manifest.version,
        "panels": [p["type"] for p in manifest.panels],
        "enrichers": [e.name for e in enrichers],
        "ui_flags": dict(manifest.ui_flags),
        "planner_facets": sorted(manifest.planner_keywords.keys()),
        "templates": template_names,
    }


def install(name: str, version: Optional[str] = None, root: Optional[str] = None) -> Dict[str, Any]:
    """Pull a pack from the registry (latest, or a pinned version) and install
    it. Raises :class:`PackInstallError` if it is not published."""
    manifest = pack_registry.get(name, version, root)
    if manifest is None:
        raise PackInstallError(f"pack {name!r} (version {version or 'latest'}) not found in registry")
    return install_manifest(manifest)


def installed_packs() -> Dict[str, str]:
    """Installed pack names mapped to their installed versions."""
    return {name: info["version"] for name, info in _INSTALLED.items()}


def is_installed(name: str) -> bool:
    return name in _INSTALLED


# --- provisioning templates ---------------------------------------------------

def list_templates() -> List[str]:
    """Names of all provisioning templates enabled by installed packs."""
    return sorted(_TEMPLATES.keys())


def get_template(name: str) -> Optional[Dict[str, Any]]:
    return _TEMPLATES.get(name)


def deploy_template(conn, name: str, provisioner: Any = None, approve: bool = True) -> Dict[str, Any]:
    """Stand up an installed provisioning template through the Provisioner:
    deploy the KG (with the template's ontology and backend) and attach its
    sources. Returns the deploy result, or a ``template_not_found`` error."""
    template = _TEMPLATES.get(name)
    if template is None:
        return {"error": f"template {name!r} is not installed", "code": "template_not_found"}
    from src.provisioning.provisioner import Provisioner

    prov = provisioner or Provisioner(conn)
    result = prov.deploy(
        template["name"],
        template.get("description", ""),
        ontology=template.get("ontology"),
        approve=approve,
        backend=template.get("backend", "table-prefix"),
    )
    if template.get("sources") and not result.get("error"):
        prov.attach_sources(template["name"], sources=list(template["sources"]))
    return result


def uninstall(name: str) -> bool:
    """Remove an installed pack's runtime registrations. Returns True if the pack
    was installed."""
    info = _INSTALLED.pop(name, None)
    if info is None:
        return False
    for template_name in info["templates"]:
        _TEMPLATES.pop(template_name, None)
    domain_registry.disable_pack(name)
    return True


__all__ = [
    "PackInstallError",
    "install",
    "install_manifest",
    "installed_packs",
    "is_installed",
    "list_templates",
    "get_template",
    "deploy_template",
    "uninstall",
]
