"""Small deterministic enrichers for normalized official political records."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _metadata(document: Any) -> dict[str, Any]:
    raw = document.get("metadata", {}) if isinstance(document, dict) else getattr(document, "metadata", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def political_record_enricher(document: Any) -> dict[str, Any] | None:
    """Expose typed source/jurisdiction fields without guessing from prose."""

    metadata = _metadata(document)
    if not metadata.get("source_manifest_id") or not metadata.get("jurisdiction"):
        return None
    return {
        "domain": "political",
        "jurisdiction": metadata["jurisdiction"],
        "institution": metadata.get("issuing_institution"),
        "document_type": metadata.get("document_type"),
        "official_identifier": metadata.get("official_identifier"),
        "source_manifest_id": metadata["source_manifest_id"],
        "enricher": "political-record",
    }


__all__ = ["political_record_enricher"]
