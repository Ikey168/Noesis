"""Evidence-bound advisory impact checks for pinned project inventories."""

from __future__ import annotations

import json
import re
from typing import Any

from packaging.version import InvalidVersion, Version

from src.domains.technical.inventory import InventoryStore
from src.domains.technical.model import package_object_id

CONTRACT = "noesis-technical-impact-v1"
_NPM_VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def _npm_key(value: str) -> tuple[Any, ...]:
    match = _NPM_VERSION.fullmatch(value)
    if match is None:
        raise ValueError("invalid npm version")
    prerelease = match[4]
    parts = (
        ()
        if prerelease is None
        else tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in prerelease.split(".")
        )
    )
    return (
        int(match[1]),
        int(match[2]),
        int(match[3]),
        1 if prerelease is None else 0,
        parts,
    )


def _compare(ecosystem: str, left: str, right: str) -> int:
    if ecosystem == "pypi":
        a, b = Version(left), Version(right)
    elif ecosystem == "npm":
        a, b = _npm_key(left), _npm_key(right)
    else:
        raise ValueError("unsupported ecosystem")
    return (a > b) - (a < b)


def _in_events(ecosystem: str, version: str, events: list[dict[str, Any]]) -> bool:
    if not events or not any("introduced" in event for event in events):
        raise ValueError("range has no introduction")
    affected = False
    for event in events:
        if "introduced" in event:
            start = str(event["introduced"])
            if start == "0" or _compare(ecosystem, version, start) >= 0:
                affected = True
        if "fixed" in event and _compare(ecosystem, version, str(event["fixed"])) >= 0:
            affected = False
        if (
            "last_affected" in event
            and _compare(ecosystem, version, str(event["last_affected"])) > 0
        ):
            affected = False
        if "limit" in event and _compare(ecosystem, version, str(event["limit"])) >= 0:
            affected = False
    return affected


def assess_inventory(
    conn: Any, inventory_id: str, *, owner_id: str, limit: int = 100, offset: int = 0
) -> dict[str, Any]:
    """Assess one bounded owner-scoped page against acquired public graph records."""

    page = InventoryStore(conn, initialize=False).inspect(
        inventory_id, owner_id=owner_id, limit=limit, offset=offset
    )
    findings = []
    for entry in page["entries"]:
        result = {
            "entry": entry,
            "overall": "unknown",
            "advisories": [],
            "upstream_review_candidates": [],
        }
        if entry["status"] != "pinned" or not entry.get("acquired_package"):
            result["reason"] = (
                "unresolved_inventory_entry"
                if entry["status"] != "pinned"
                else "package_not_acquired"
            )
            findings.append(result)
            continue
        package_id = package_object_id(entry["coordinate"])
        rows = conn.execute(
            "SELECT r.advisory_id,r.ecosystem,r.range_type,r.events_json,"
            "r.source_document_id,a.status,a.source_url,a.observed_at_ms "
            "FROM technical_advisory_ranges r LEFT JOIN technical_objects a "
            "ON a.domain=r.domain AND a.object_id=r.advisory_id "
            "WHERE r.domain='technology' AND r.package_id=? "
            "ORDER BY r.advisory_id,r.events_json LIMIT 100",
            [package_id],
        ).fetchall()
        for (
            advisory_id,
            ecosystem,
            range_type,
            events_json,
            document_id,
            status,
            source_url,
            observed_at,
        ) in rows:
            state = "unknown"
            reason = None
            if status == "withdrawn":
                reason = "withdrawn_advisory"
            elif ecosystem != entry["ecosystem"] or range_type != "ECOSYSTEM":
                reason = "unsupported_range"
            else:
                try:
                    state = (
                        "affected"
                        if _in_events(
                            ecosystem, entry["version"], json.loads(events_json)
                        )
                        else "unaffected_under_assessed_range"
                    )
                except (ValueError, TypeError, InvalidVersion):
                    reason = "uninterpretable_range"
            if state == "affected":
                result["overall"] = "affected"
            result["advisories"].append(
                {
                    "advisory_id": advisory_id,
                    "finding": state,
                    "reason": reason,
                    "source_document_id": document_id,
                    "source_url": source_url,
                    "observed_at_ms": observed_at,
                    "range_type": range_type,
                }
            )
        if not rows:
            result["reason"] = "no_acquired_advisory_ranges"
        version_rows = conn.execute(
            "SELECT object_id,version,source_document_id,source_url FROM technical_objects "
            "WHERE domain='technology' AND object_type='version' AND coordinate=? "
            "AND source_document_id IS NOT NULL ORDER BY version LIMIT 100",
            [entry["coordinate"]],
        ).fetchall()
        for object_id, version, document_id, source_url in version_rows:
            try:
                newer = _compare(entry["ecosystem"], str(version), entry["version"]) > 0
            except (ValueError, InvalidVersion):
                newer = False
            if newer and len(result["upstream_review_candidates"]) < 5:
                result["upstream_review_candidates"].append(
                    {
                        "object_id": object_id,
                        "version": version,
                        "source_document_id": document_id,
                        "source_url": source_url,
                        "relevance": "heuristic_newer_release",
                    }
                )
        findings.append(result)
    return {
        "contract": CONTRACT,
        "inventory_id": inventory_id,
        "inventory_hash": page["inventory_hash"],
        "offset": offset,
        "total": page["total"],
        "findings": findings,
        "coverage_note": "No acquired range does not establish safety; findings apply only to cited acquired ranges.",
    }


__all__ = ["assess_inventory"]
