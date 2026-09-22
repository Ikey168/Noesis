"""Deterministic enrichment for explicitly structured economic documents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def economic_record_enricher(document: Any) -> dict[str, Any] | None:
    metadata = (
        document.get("metadata", {})
        if isinstance(document, dict)
        else getattr(document, "metadata", {})
    )
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    if not any(
        metadata.get(key)
        for key in ("indicator_id", "release_id", "filing_id", "policy_id")
    ):
        return None
    return {
        "domain": "economics",
        "indicator_id": metadata.get("indicator_id"),
        "release_id": metadata.get("release_id"),
        "filing_id": metadata.get("filing_id"),
        "policy_id": metadata.get("policy_id"),
        "institution_id": metadata.get("institution_id"),
        "company_id": metadata.get("company_id"),
        "enricher": "economic-record",
    }


__all__ = ["economic_record_enricher"]
