"""Bounded, offline imports of pinned project dependency inventories."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any

from src.domains.technical.model import canonical_package_coordinate, package_object_id

CONTRACT = "noesis-technical-inventory-v1"
MAX_BYTES = 2_000_000
MAX_ENTRIES = 5_000
_EXACT_NPM = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)
_PINNED_PYTHON = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;#]+)(?:\s*;\s*(.+))?$"
)
_EXACT_PYTHON = re.compile(r"^[0-9][A-Za-z0-9.!+_-]*$")

_DDL = """
CREATE TABLE IF NOT EXISTS technical_inventories (
  inventory_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, format TEXT NOT NULL,
  inventory_hash TEXT NOT NULL, entries_json TEXT NOT NULL,
  imported_at_ms BIGINT NOT NULL, UNIQUE(owner_id, format, inventory_hash)
);
"""


class InventoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _entry(
    ecosystem: str, name: str, version: Any, origin: str, direct: bool
) -> dict[str, Any]:
    raw_version = str(version or "").strip()
    try:
        coordinate = canonical_package_coordinate(ecosystem, name)
    except ValueError:
        return {
            "ecosystem": ecosystem,
            "name": name,
            "version": raw_version,
            "origin": origin,
            "direct": direct,
            "status": "unsupported",
            "coordinate": None,
        }
    exact = bool(
        (_EXACT_NPM if ecosystem == "npm" else _EXACT_PYTHON).fullmatch(raw_version)
    )
    return {
        "ecosystem": ecosystem,
        "name": name,
        "version": raw_version,
        "origin": origin,
        "direct": direct,
        "status": "pinned" if exact else "unresolved",
        "coordinate": coordinate,
    }


def _npm_name(path: str) -> str:
    suffix = path.rsplit("node_modules/", 1)[-1]
    return suffix


def _parse_lock(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping) or payload.get("lockfileVersion") not in {
        1,
        2,
        3,
    }:
        raise InventoryError(
            "invalid_inventory", "package-lock requires lockfileVersion 1, 2, or 3"
        )
    entries: list[dict[str, Any]] = []
    packages = payload.get("packages")
    if isinstance(packages, Mapping):
        root = packages.get("") or {}
        if not isinstance(root, Mapping):
            raise InventoryError(
                "invalid_inventory", "package-lock root must be an object"
            )
        direct_names = set()
        for field in (
            "dependencies",
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            values = root.get(field) or {}
            if not isinstance(values, Mapping):
                raise InventoryError("invalid_inventory", f"{field} must be an object")
            direct_names.update(values)
        for path, item in packages.items():
            if path == "":
                continue
            if (
                not isinstance(path, str)
                or not isinstance(item, Mapping)
                or "node_modules/" not in path
            ):
                raise InventoryError(
                    "invalid_inventory", "package-lock package entry is malformed"
                )
            name = str(item.get("name") or _npm_name(path))
            direct = path == "node_modules/" + name and name in direct_names
            entry = _entry("npm", name, item.get("version"), path, direct)
            if item.get("link") or str(item.get("resolved") or "").startswith(
                ("file:", "workspace:")
            ):
                entry["status"] = "unsupported"
            entries.append(entry)
    else:
        dependencies = payload.get("dependencies")
        if not isinstance(dependencies, Mapping):
            raise InventoryError(
                "invalid_inventory", "package-lock dependencies must be an object"
            )

        def walk(
            items: Mapping[str, Any], prefix: str, direct: bool, depth: int
        ) -> None:
            if depth > 20:
                raise InventoryError(
                    "too_deep", "package-lock dependency tree exceeds 20 levels"
                )
            for name, item in items.items():
                if not isinstance(item, Mapping):
                    raise InventoryError(
                        "invalid_inventory", "package-lock dependency is malformed"
                    )
                path = prefix + "node_modules/" + str(name)
                entries.append(
                    _entry("npm", str(name), item.get("version"), path, direct)
                )
                nested = item.get("dependencies") or {}
                if not isinstance(nested, Mapping):
                    raise InventoryError(
                        "invalid_inventory", "nested dependencies must be an object"
                    )
                walk(nested, path + "/", False, depth + 1)

        walk(dependencies, "", True, 0)
    return entries


def _parse_requirements(content: str) -> list[dict[str, Any]]:
    entries = []
    for line_number, line in enumerate(content.splitlines(), 1):
        stripped = line.split(" #", 1)[0].strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _PINNED_PYTHON.fullmatch(stripped)
        if match:
            entry = _entry("pypi", match[1], match[2], f"line:{line_number}", True)
            if match[3]:
                entry["status"] = "unresolved"
                entry["environment_marker"] = match[3]
            entries.append(entry)
        else:
            name = re.split(r"[<>=!~;\s]", stripped, maxsplit=1)[0]
            entries.append(_entry("pypi", name, "", f"line:{line_number}", True))
            entries[-1]["status"] = (
                "unsupported"
                if stripped.startswith(("-", "git+", "http:", "https:"))
                else "unresolved"
            )
    return entries


def parse_inventory(content: str, format: str) -> dict[str, Any]:
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_BYTES:
        raise InventoryError("too_large", "inventory must be UTF-8 text within 2 MB")
    if format == "package-lock.json":
        try:
            payload = json.loads(content)
        except (ValueError, TypeError) as exc:
            raise InventoryError(
                "invalid_inventory", "package-lock is not valid JSON"
            ) from exc
        entries = _parse_lock(payload)
    elif format == "requirements.txt":
        entries = _parse_requirements(content)
    else:
        raise InventoryError(
            "unsupported_format",
            "supported formats are package-lock.json and requirements.txt",
        )
    if len(entries) > MAX_ENTRIES:
        raise InventoryError("too_large", "inventory exceeds 5000 entries")
    return {
        "contract": CONTRACT,
        "format": format,
        "inventory_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "entries": entries,
        "counts": {
            status: sum(item["status"] == status for item in entries)
            for status in ("pinned", "unresolved", "unsupported")
        },
    }


class InventoryStore:
    def __init__(self, conn: Any, *, initialize: bool = True) -> None:
        self.conn = conn
        if initialize:
            conn.execute(_DDL)

    def import_inventory(
        self, content: str, format: str, *, owner_id: str
    ) -> dict[str, Any]:
        if not owner_id:
            raise InventoryError("unauthorized", "authenticated owner is required")
        parsed = parse_inventory(content, format)
        inventory_id = (
            "technical-inventory:"
            + hashlib.sha256(
                f"{owner_id}\0{format}\0{parsed['inventory_hash']}".encode()
            ).hexdigest()[:32]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO technical_inventories VALUES (?,?,?,?,?,?)",
            [
                inventory_id,
                owner_id,
                format,
                parsed["inventory_hash"],
                json.dumps(parsed["entries"], sort_keys=True),
                int(time.time() * 1000),
            ],
        )
        return {**parsed, "inventory_id": inventory_id}

    def inspect(
        self, inventory_id: str, *, owner_id: str, limit: int = 100, offset: int = 0
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 5000:
            raise InventoryError(
                "invalid_page", "limit must be 1-100 and offset 0-5000"
            )
        row = self.conn.execute(
            "SELECT format,inventory_hash,entries_json FROM technical_inventories "
            "WHERE inventory_id=? AND owner_id=?",
            [inventory_id, owner_id],
        ).fetchone()
        if row is None:
            raise InventoryError("not_found", "inventory was not found")
        entries = json.loads(row[2])
        page = []
        for entry in entries[offset : offset + limit]:
            item = dict(entry)
            if item["status"] == "pinned":
                package_id = package_object_id(item["coordinate"])
                package = (
                    self.conn.execute(
                        "SELECT object_id,source_document_id FROM technical_objects "
                        "WHERE domain='technology' AND object_id=? AND object_type='package'",
                        [package_id],
                    ).fetchone()
                    if self._has_graph()
                    else None
                )
                item["acquired_package"] = (
                    None
                    if package is None
                    else {"object_id": package[0], "source_document_id": package[1]}
                )
                advisories = (
                    self.conn.execute(
                        "SELECT advisory_id,source_document_id FROM technical_advisory_ranges "
                        "WHERE domain='technology' AND package_id=? ORDER BY advisory_id LIMIT 100",
                        [package_id],
                    ).fetchall()
                    if self._has_graph()
                    else []
                )
                item["acquired_advisories"] = [
                    {"object_id": advisory_id, "source_document_id": document_id}
                    for advisory_id, document_id in advisories
                ]
            page.append(item)
        return {
            "contract": CONTRACT,
            "inventory_id": inventory_id,
            "format": row[0],
            "inventory_hash": row[1],
            "total": len(entries),
            "offset": offset,
            "entries": page,
        }

    def _has_graph(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='technical_objects'"
            ).fetchone()
        )


__all__ = ["InventoryError", "InventoryStore", "parse_inventory"]
