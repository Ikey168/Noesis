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


def _in_cpe_bounds(ecosystem: str, version: str, item: dict[str, Any]) -> bool:
    cpe = dict(item.get("cpe") or {})
    bounds = dict(cpe.get("bounds") or {})
    if bounds:
        checks = (
            ("versionStartIncluding", lambda c: c >= 0),
            ("versionStartExcluding", lambda c: c > 0),
            ("versionEndIncluding", lambda c: c <= 0),
            ("versionEndExcluding", lambda c: c < 0),
        )
        return all(
            test(_compare(ecosystem, version, str(bounds[key])))
            for key, test in checks
            if key in bounds
        )
    if cpe.get("all_versions"):
        return True
    versions = [str(v) for v in item.get("versions") or []]
    if not versions:
        raise ValueError("range has no bounds or versions")
    return any(_compare(ecosystem, version, value) == 0 for value in versions)


def _vulnerability_findings(
    conn: Any, namespace: str, entry: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Findings from ranges of reviewed (accepted) component matches in ``src.kb.vulnerabilities``.

    Each finding cites the source, advisory revision, content digest and the
    range it evaluated; exploitation evidence and scores are quoted, never
    turned into a verdict or priority.
    """
    if not conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='vuln_ranges'"
    ).fetchone():
        return [], []
    from src.kb.vulnerabilities import VulnerabilityStore
    from src.kb.vulnerability_identity import VulnerabilityIdentity
    from src.kb.vulnerability_queries import VulnerabilityQueries

    store = VulnerabilityStore(conn, initialize=False)
    identity = VulnerabilityIdentity(conn, initialize=False)
    queries = VulnerabilityQueries(conn)
    findings = []
    for match in identity.accepted_targets(namespace, entry["coordinate"]):
        if match["target_kind"] == "advisory-package":
            _, source, ecosystem_source, package = match["target_key"].split(":", 3)
            rows = [
                r
                for r in store.ranges(namespace, package=package)
                if r["source"] == source
                and r["ecosystem_source"] == ecosystem_source
                and r["range_type"] != "CPE"
            ]
        else:
            product = match["target_key"].split(":", 1)[1]
            rows = [
                r
                for r in store.ranges(namespace, package=product)
                if r["range_type"] == "CPE"
            ]
        for item in rows:
            if (
                store.latest_revision_id(namespace, item["series_id"])
                != item["revision_id"]
            ):
                continue  # only the current revision of each source record is assessed
            revision = store.revision(namespace, item["revision_id"], detail=False)
            cve_id = revision["cve_id"] or queries.resolve_identifier(
                namespace, revision["native_id"]
            ).get("cve_id")
            state, reason = "unknown", None
            if revision["lifecycle"] != "active":
                reason = f"{revision['lifecycle']}_by_source"
            elif item["applicability"] == "conditional":
                reason = "conditional_applicability"
            else:
                try:
                    if item["range_type"] in {"ECOSYSTEM", "SEMVER"}:
                        if item["ecosystem"] != entry["ecosystem"]:
                            raise LookupError
                        hit = _in_events(
                            entry["ecosystem"], entry["version"], item["events"]
                        )
                    elif item["range_type"] in {"CPE", "VERSIONS"}:
                        hit = _in_cpe_bounds(entry["ecosystem"], entry["version"], item)
                    else:
                        raise LookupError
                    state = "affected" if hit else "unaffected_under_assessed_range"
                except LookupError:
                    reason = "unsupported_range"
                except (ValueError, TypeError, InvalidVersion):
                    reason = "uninterpretable_range"
            quoted = {"exploitation": [], "scores": []}
            if cve_id:
                quoted["exploitation"] = store.exploitation(namespace, cve_id=cve_id)
                for series in store.series_list(namespace, cve_id=cve_id):
                    if series["record_kind"] == "score":
                        quoted["scores"] += store.scores(
                            namespace, series=series["series_id"]
                        )
            quoted["scores"] += [
                s for s in store.scores(namespace, revision_id=item["revision_id"])
            ]
            findings.append(
                {
                    "advisory_id": revision["native_id"],
                    "cve_id": cve_id,
                    "source": revision["source"],
                    "series_id": item["series_id"],
                    "revision_id": item["revision_id"],
                    "revision_no": revision["revision_no"],
                    "content_digest": revision["content_digest"],
                    "range_id": item["range_id"],
                    "range_type": item["range_type"],
                    "range_event": {
                        "events": item["events"],
                        "versions": item["versions"],
                        "cpe": item["cpe"],
                    },
                    "finding": state,
                    "reason": reason,
                    "match_id": match["match_id"],
                    "decision_id": match["decision_id"],
                    "quoted_evidence": quoted,
                }
            )
    pending = [
        {"match_id": m["match_id"], "target_key": m["target_key"], "state": m["state"]}
        for m in identity.pending_targets(namespace, entry["coordinate"])
    ]
    return findings, pending


def assess_inventory(
    conn: Any,
    inventory_id: str,
    *,
    owner_id: str,
    limit: int = 100,
    offset: int = 0,
    vulnerability_namespace: str | None = None,
) -> dict[str, Any]:
    """Assess one bounded owner-scoped page against acquired public graph records.

    With ``vulnerability_namespace``, ranges of reviewed component matches in
    the vulnerability store are assessed alongside ``technical_advisory_ranges``.
    """

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
        if (
            vulnerability_namespace
            and entry["status"] == "pinned"
            and entry.get("coordinate")
        ):
            vulnerable, pending = _vulnerability_findings(
                conn, vulnerability_namespace, entry
            )
            result["vulnerability_advisories"] = vulnerable
            result["vulnerability_candidates"] = pending
            if any(item["finding"] == "affected" for item in vulnerable):
                result["overall"] = "affected"
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
    extra = (
        {"vulnerability_namespace": vulnerability_namespace}
        if vulnerability_namespace
        else {}
    )
    return {
        "contract": CONTRACT,
        **extra,
        "inventory_id": inventory_id,
        "inventory_hash": page["inventory_hash"],
        "offset": offset,
        "total": page["total"],
        "findings": findings,
        "coverage_note": "No acquired range does not establish safety; findings apply only to cited acquired ranges.",
    }


__all__ = ["assess_inventory"]
