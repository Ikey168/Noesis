"""
Domain-pack registry: publish and discover (M9.2).

A packaged pack (M9.1) is a single ``pack.json`` manifest. The registry is where
those manifests are published so they can be discovered and installed (M9.3) by
a fresh instance. It is a filesystem-backed store, laid out as

    <root>/<name>/<version>/pack.json

so the directory tree *is* the index: a pack's name is a directory, each of its
published versions a sub-directory. Versions are immutable — publishing a version
that already exists is refused unless ``force`` is set — so an installed pack can
always be reproduced from its ``(name, version)``.

The registry root defaults to ``NOESIS_PACK_REGISTRY`` / ``NEURONEWS_PACK_REGISTRY``
(a shared, discoverable location), falling back to ``<repo>/data/pack_registry``.

Stdlib-only; composes with :mod:`src.domains.pack_format`.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.domains.pack_format import (
    MANIFEST_FILENAME,
    PackFormatError,
    PackManifest,
    load_manifest,
    package_pack,
    validate_manifest,
)


def default_root() -> str:
    """The registry root: ``NOESIS_PACK_REGISTRY`` / ``NEURONEWS_PACK_REGISTRY``,
    else ``<repo>/data/pack_registry``."""
    from src.config.env import resolve_env

    configured = resolve_env("PACK_REGISTRY")
    if configured:
        return configured
    return str(Path(__file__).resolve().parents[2] / "data" / "pack_registry")


def _root(root: Optional[str]) -> Path:
    return Path(root) if root is not None else Path(default_root())


def _version_key(version: str) -> Tuple[int, ...]:
    """Parse ``major.minor.patch`` into a comparable tuple; unparseable parts
    sort low so a malformed dir never outranks a real version."""
    parts = []
    for chunk in str(version).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


class PackRegistryError(RuntimeError):
    """A publish/lookup could not be completed (e.g. a duplicate version)."""


def publish(manifest: PackManifest, root: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Publish a manifest to the registry under ``<name>/<version>``. Validates
    first, so only contract-valid packs land. Refuses to overwrite an existing
    version unless ``force`` (versions are immutable). Returns the published
    coordinates."""
    errors = validate_manifest(manifest.to_dict())
    if errors:
        raise PackFormatError(f"manifest is invalid: {'; '.join(errors[:3])}")
    dest = _root(root) / manifest.name / manifest.version
    if (dest / MANIFEST_FILENAME).exists() and not force:
        raise PackRegistryError(
            f"{manifest.name} {manifest.version} is already published "
            f"(versions are immutable; pass force to overwrite)"
        )
    path = package_pack(manifest, str(dest))
    return {"name": manifest.name, "version": manifest.version, "path": path}


def publish_path(pack_path: str, root: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Publish a packaged pack from disk (a ``pack.json`` file or its directory)."""
    return publish(load_manifest(pack_path), root=root, force=force)


def list_packs(root: Optional[str] = None) -> List[str]:
    """The names of all published packs, sorted."""
    base = _root(root)
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir() and versions(p.name, root))


def versions(name: str, root: Optional[str] = None) -> List[str]:
    """Published versions of a pack, newest (highest semver) first."""
    pack_dir = _root(root) / name
    if not pack_dir.is_dir():
        return []
    vs = [
        v.name for v in pack_dir.iterdir()
        if v.is_dir() and (v / MANIFEST_FILENAME).exists()
    ]
    return sorted(vs, key=_version_key, reverse=True)


def latest_version(name: str, root: Optional[str] = None) -> Optional[str]:
    """The highest published version of a pack, or None if unpublished."""
    vs = versions(name, root)
    return vs[0] if vs else None


def get(name: str, version: Optional[str] = None, root: Optional[str] = None) -> Optional[PackManifest]:
    """Load a published pack manifest. With no ``version``, resolves the latest.
    Returns None if the pack (or the requested version) is not published."""
    resolved = version or latest_version(name, root)
    if resolved is None:
        return None
    path = _root(root) / name / resolved / MANIFEST_FILENAME
    if not path.exists():
        return None
    try:
        return load_manifest(str(path))
    except PackFormatError:
        return None


def discover(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """A catalogue of published packs for discovery: each name with its latest
    version, all versions, and the latest description."""
    out = []
    for name in list_packs(root):
        vs = versions(name, root)
        latest = get(name, vs[0], root) if vs else None
        out.append(
            {
                "name": name,
                "latest_version": vs[0] if vs else None,
                "versions": vs,
                "description": latest.description if latest else "",
                "source_types": latest.source_types if latest else [],
            }
        )
    return out


def unpublish(name: str, version: Optional[str] = None, root: Optional[str] = None) -> bool:
    """Remove a published version (or the whole pack when ``version`` is None).
    Returns True if anything was removed. For registry maintenance/tests."""
    base = _root(root) / name
    target = base / version if version else base
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    # Clean up an emptied pack directory.
    if base.is_dir() and not any(base.iterdir()):
        base.rmdir()
    return True


__all__ = [
    "default_root",
    "PackRegistryError",
    "publish",
    "publish_path",
    "list_packs",
    "versions",
    "latest_version",
    "get",
    "discover",
    "unpublish",
]
