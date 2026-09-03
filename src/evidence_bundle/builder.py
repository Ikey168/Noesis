"""Content-addressed builder for ``noesis-evidence-bundle-v1``."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from .canonical import CANONICALIZATION, HASH_ALGORITHM, sha256_digest

CONTRACT_VERSION = "noesis-evidence-bundle-v1"


class EvidenceBundleError(ValueError):
    """Raised when a bundle cannot be built without violating its contract."""


def _identity_document(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """The non-cyclic subset covered by ``bundle_id``.

    Object bytes are covered through the digests in ``manifest.entries``.
    ``bundle_id`` itself is intentionally excluded.
    """
    return {
        "contract": bundle["contract"],
        "created_at_ms": bundle["created_at_ms"],
        "operation": bundle["operation"],
        "roots": bundle["roots"],
        "manifest": bundle["manifest"],
        "external_references": bundle["external_references"],
        "completeness": bundle["completeness"],
    }


def compute_bundle_id(bundle: Mapping[str, Any]) -> str:
    return sha256_digest(_identity_document(bundle))


def compute_object_digest(record: Mapping[str, Any]) -> str:
    return sha256_digest(
        {
            "id": record["id"],
            "type": record["type"],
            "payload": record["payload"],
            "references": record.get("references", []),
        }
    )


class EvidenceBundleBuilder:
    """Accumulate objects and emit a deterministically ordered bundle."""

    def __init__(
        self,
        operation_type: str,
        inputs: Mapping[str, Any] | None = None,
        *,
        created_at_ms: int | None = None,
        as_of_ms: int | None = None,
    ) -> None:
        if not operation_type or not operation_type.strip():
            raise EvidenceBundleError("operation_type must be non-empty")
        self.created_at_ms = int(
            time.time() * 1000 if created_at_ms is None else created_at_ms
        )
        self.operation: dict[str, Any] = {
            "type": operation_type,
            "inputs": deepcopy(dict(inputs or {})),
        }
        if as_of_ms is not None:
            self.operation["as_of_ms"] = int(as_of_ms)
        self._objects: dict[str, dict[str, Any]] = {}
        self._roots: list[str] = []
        self._external: dict[str, dict[str, Any]] = {}
        self._omissions: list[dict[str, Any]] = []

    def add_object(
        self,
        object_type: str,
        payload: Any,
        *,
        object_id: str | None = None,
        references: Iterable[str] = (),
        root: bool = False,
    ) -> str:
        refs = sorted(dict.fromkeys(str(ref) for ref in references))
        payload_copy = deepcopy(payload)
        if object_id is None:
            seed = sha256_digest({"type": object_type, "payload": payload_copy})
            object_id = f"{object_type}:{seed.split(':', 1)[1][:24]}"
        if not object_id or not object_type:
            raise EvidenceBundleError("object id and type must be non-empty")
        record = {
            "id": str(object_id),
            "type": str(object_type),
            "payload": payload_copy,
            "references": refs,
        }
        record["sha256"] = compute_object_digest(record)
        existing = self._objects.get(record["id"])
        if existing is not None and existing != record:
            raise EvidenceBundleError(f"object id collision: {record['id']}")
        self._objects[record["id"]] = record
        if root and record["id"] not in self._roots:
            self._roots.append(record["id"])
        return record["id"]

    def add_external_reference(
        self,
        reference_id: str,
        locator: str,
        *,
        mode: str = "external",
        sha256: str | None = None,
        required: bool = True,
        media_type: str | None = None,
    ) -> None:
        if mode not in {"external", "adjacent"}:
            raise EvidenceBundleError(
                "external reference mode must be external or adjacent"
            )
        item: dict[str, Any] = {
            "id": reference_id,
            "mode": mode,
            "locator": locator,
            "required": bool(required),
        }
        if sha256 is not None:
            item["sha256"] = sha256
        if media_type is not None:
            item["media_type"] = media_type
        existing = self._external.get(reference_id)
        if existing is not None and existing != item:
            raise EvidenceBundleError(
                f"external reference id collision: {reference_id}"
            )
        self._external[reference_id] = item

    def add_omission(self, reason: str, *, object_id: str | None = None) -> None:
        if not reason.strip():
            raise EvidenceBundleError("omission reason must be non-empty")
        item: dict[str, Any] = {"reason": reason}
        if object_id is not None:
            item["object_id"] = object_id
        self._omissions.append(item)

    def build(self) -> dict[str, Any]:
        if not self._roots:
            raise EvidenceBundleError("bundle must have at least one root object")
        missing = sorted(
            {
                ref
                for record in self._objects.values()
                for ref in record["references"]
                if ref not in self._objects
            }
        )
        if missing:
            raise EvidenceBundleError(f"unresolved bundle-local references: {missing}")
        objects = sorted(self._objects.values(), key=lambda item: item["id"])
        entries = [
            {"id": item["id"], "type": item["type"], "sha256": item["sha256"]}
            for item in objects
        ]
        bundle: dict[str, Any] = {
            "contract": CONTRACT_VERSION,
            "bundle_id": "",
            "created_at_ms": self.created_at_ms,
            "operation": self.operation,
            "roots": sorted(self._roots),
            "objects": objects,
            "external_references": sorted(
                self._external.values(), key=lambda item: item["id"]
            ),
            "completeness": {
                "status": "partial" if self._omissions else "complete",
                "omissions": deepcopy(self._omissions),
            },
            "manifest": {
                "hash_algorithm": HASH_ALGORITHM,
                "canonicalization": CANONICALIZATION,
                "entry_count": len(entries),
                "entries": entries,
            },
        }
        bundle["bundle_id"] = compute_bundle_id(bundle)
        return bundle
