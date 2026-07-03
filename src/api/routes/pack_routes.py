"""Domain-pack ecosystem routes (M9, live entry point).

Expose the M9 registry and installer over HTTP so an operator can discover,
install, and manage domain packs at runtime instead of only from Python:

    GET    /api/v1/packs                      discover published packs + status
    GET    /api/v1/packs/installed            the currently-installed packs
    GET    /api/v1/packs/templates            provisioning templates from packs
    POST   /api/v1/packs/install              install a pack (name, version?)
    DELETE /api/v1/packs/{name}               uninstall a pack
    POST   /api/v1/packs/publish              publish a manifest to the registry
    POST   /api/v1/packs/templates/{name}/deploy   deploy a pack's KG template

Reads are always available. The mutating operations (install, uninstall,
publish, deploy) change the running instance, so they are gated behind
``NOESIS_PACKS_ADMIN`` (off by default); ``GET /api/v1/packs`` reports whether
they are enabled so the frontend can adapt.
"""

import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.domains import pack_install, pack_registry
from src.domains.pack_format import PackFormatError, PackManifest, validate_manifest

router = APIRouter(prefix="/api/v1/packs", tags=["domain_packs"])


def admin_enabled() -> bool:
    """Whether pack-mutating operations are enabled (``NOESIS_PACKS_ADMIN``)."""
    return os.getenv("NOESIS_PACKS_ADMIN", "off").lower() in ("on", "1", "true")


def _require_admin() -> None:
    if not admin_enabled():
        raise HTTPException(
            status_code=404,
            detail="pack administration is disabled (set NOESIS_PACKS_ADMIN=on)",
        )


def _conn():
    from src.database.local_analytics_connector import _LOCK, get_shared_connection

    return get_shared_connection(), _LOCK


class InstallRequest(BaseModel):
    name: str = Field(..., max_length=64)
    version: Optional[str] = Field(default=None, max_length=32)


class PublishRequest(BaseModel):
    manifest: Dict[str, Any] = Field(..., description="A noesis-pack-v1 manifest")
    force: bool = Field(default=False)


@router.get("")
def list_packs() -> Dict[str, Any]:
    """Discover published packs with their installed state and admin status."""
    try:
        installed = pack_install.installed_packs()
        packs = pack_registry.discover()
        for entry in packs:
            entry["installed_version"] = installed.get(entry["name"])
        return {
            "admin_enabled": admin_enabled(),
            "packs": packs,
            "installed": installed,
            "count": len(packs),
        }
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"pack discovery failed: {err}")


@router.get("/installed")
def installed() -> Dict[str, Any]:
    """The currently-installed packs and their versions."""
    packs = pack_install.installed_packs()
    return {"installed": packs, "count": len(packs)}


@router.get("/templates")
def templates() -> Dict[str, Any]:
    """The provisioning templates enabled by installed packs."""
    names = pack_install.list_templates()
    return {"templates": [pack_install.get_template(n) for n in names], "count": len(names)}


@router.post("/install")
def install(request: InstallRequest) -> Dict[str, Any]:
    """Install a published pack into the running instance."""
    _require_admin()
    try:
        report = pack_install.install(request.name, version=request.version)
    except pack_install.PackInstallError as err:
        raise HTTPException(status_code=404, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"pack install failed: {err}")
    return {"installed": report}


@router.delete("/{name}")
def uninstall(name: str) -> Dict[str, Any]:
    """Uninstall a pack, withdrawing its panels, keywords, enrichers and
    templates."""
    _require_admin()
    removed = pack_install.uninstall(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"pack {name!r} is not installed")
    return {"uninstalled": name}


@router.post("/publish")
def publish(request: PublishRequest) -> Dict[str, Any]:
    """Publish a manifest to the registry (validated first; versions immutable)."""
    _require_admin()
    errors = validate_manifest(request.manifest)
    if errors:
        raise HTTPException(status_code=400, detail=f"invalid manifest: {'; '.join(errors[:3])}")
    try:
        published = pack_registry.publish(
            PackManifest.from_dict(request.manifest), force=request.force
        )
    except pack_registry.PackRegistryError as err:
        raise HTTPException(status_code=409, detail=str(err))
    except PackFormatError as err:
        raise HTTPException(status_code=400, detail=str(err))
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"pack publish failed: {err}")
    return {"published": published}


@router.post("/templates/{name}/deploy")
def deploy_template(name: str) -> Dict[str, Any]:
    """Deploy a pack's provisioning template (its KG) through the Provisioner."""
    _require_admin()
    conn, lock = _conn()
    try:
        with lock:
            result = pack_install.deploy_template(conn, name)
    except Exception as err:
        raise HTTPException(status_code=500, detail=f"template deploy failed: {err}")
    if result.get("code") == "template_not_found":
        raise HTTPException(status_code=404, detail=result.get("error"))
    return {"deployed": result}
