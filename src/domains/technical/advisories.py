"""Versioned OSV and CVE advisory adapters with exact package resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from src.domains.technical.model import (
    TechnicalModelError,
    canonical_package_coordinate,
    record_advisory_range,
    record_alias,
    record_object,
    record_relation,
    resolve_package,
)


class AdvisoryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AffectedPackage:
    coordinate: str | None
    ecosystem: str | None
    ranges: tuple[dict[str, Any], ...] = ()
    versions: tuple[str, ...] = ()
    resolution_error: str | None = None


@dataclass(frozen=True)
class AdvisoryRecord:
    advisory_id: str
    source_format: str
    source_version: str
    summary: str
    details: str
    aliases: tuple[str, ...] = ()
    severities: tuple[dict[str, Any], ...] = ()
    affected: tuple[AffectedPackage, ...] = ()
    references: tuple[str, ...] = ()
    published_at: Any = None
    modified_at: Any = None
    withdrawn_at: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


class OSVAdapter:
    """OSV schema adapter; preserves ordered range events and corrections."""

    source_version = "1.6.0"

    def parse(self, payload: Mapping[str, Any]) -> AdvisoryRecord:
        advisory_id = str(payload.get("id") or "").strip()
        if not advisory_id:
            raise AdvisoryError("bad_advisory", "OSV id is required")
        affected = []
        for item in payload.get("affected") or ():
            package = item.get("package") or {}
            ecosystem, name = package.get("ecosystem"), package.get("name")
            coordinate = None
            error = None
            try:
                coordinate = canonical_package_coordinate(str(ecosystem), str(name))
            except (TechnicalModelError, TypeError) as exc:
                error = str(exc)
            ranges = tuple(
                {
                    "type": str(value.get("type") or "ECOSYSTEM").upper(),
                    "repo": value.get("repo"),
                    "events": list(value.get("events") or ()),
                    "database_specific": value.get("database_specific") or {},
                }
                for value in item.get("ranges") or ()
            )
            affected.append(
                AffectedPackage(
                    coordinate, str(ecosystem) if ecosystem else None, ranges,
                    tuple(str(v) for v in item.get("versions") or ()), error,
                )
            )
        return AdvisoryRecord(
            advisory_id=advisory_id,
            source_format="OSV",
            source_version=str(payload.get("schema_version") or self.source_version),
            summary=str(payload.get("summary") or ""),
            details=str(payload.get("details") or ""),
            aliases=tuple(str(value) for value in payload.get("aliases") or ()),
            severities=tuple(dict(value) for value in payload.get("severity") or ()),
            affected=tuple(affected),
            references=tuple(
                str(value.get("url")) for value in payload.get("references") or ()
                if value.get("url")
            ),
            published_at=payload.get("published"),
            modified_at=payload.get("modified"),
            withdrawn_at=payload.get("withdrawn"),
            metadata={
                "database_specific": payload.get("database_specific") or {},
                "credits": payload.get("credits") or [],
            },
        )


class CVEAdapter:
    """CVE 5 adapter requiring an explicit product-to-coordinate mapping."""

    def __init__(self, package_mappings: Mapping[str, str] | None = None) -> None:
        self.package_mappings = dict(package_mappings or {})

    def parse(self, payload: Mapping[str, Any]) -> AdvisoryRecord:
        metadata = payload.get("cveMetadata") or {}
        advisory_id = str(metadata.get("cveId") or payload.get("id") or "").strip()
        if not advisory_id:
            raise AdvisoryError("bad_advisory", "CVE id is required")
        containers = payload.get("containers") or {}
        cna = containers.get("cna") or payload.get("cna") or {}
        descriptions = cna.get("descriptions") or ()
        details = next(
            (str(item.get("value")) for item in descriptions if item.get("lang") == "en"),
            str(descriptions[0].get("value")) if descriptions else "",
        )
        severities: list[dict[str, Any]] = []
        for metric in cna.get("metrics") or ():
            for key, value in metric.items():
                if key.startswith("cvss") and isinstance(value, Mapping):
                    severities.append(
                        {
                            "type": key.upper(),
                            "score": value.get("baseScore"),
                            "vector": value.get("vectorString"),
                            "severity": value.get("baseSeverity"),
                            "source": "CNA",
                        }
                    )
        for adp in containers.get("adp") or ():
            for metric in adp.get("metrics") or ():
                for key, value in metric.items():
                    if key.startswith("cvss") and isinstance(value, Mapping):
                        severities.append(
                            {
                                "type": key.upper(),
                                "score": value.get("baseScore"),
                                "vector": value.get("vectorString"),
                                "severity": value.get("baseSeverity"),
                                "source": str(adp.get("providerMetadata", {}).get("shortName") or "ADP"),
                            }
                        )
        affected = []
        for item in cna.get("affected") or ():
            vendor = str(item.get("vendor") or "").strip()
            product = str(item.get("product") or "").strip()
            mapping_key = f"{vendor}/{product}"
            coordinate = item.get("packageURL") or item.get("purl") or self.package_mappings.get(mapping_key)
            ranges = []
            for version in item.get("versions") or ():
                if str(version.get("status")).casefold() != "affected":
                    continue
                events = [{"introduced": str(version.get("version") or "0")}]
                if version.get("lessThan"):
                    events.append({"fixed": str(version["lessThan"])})
                elif version.get("lessThanOrEqual"):
                    events.append({"last_affected": str(version["lessThanOrEqual"])})
                ranges.append({"type": str(version.get("versionType") or "ECOSYSTEM"), "events": events})
            error = None if coordinate else f"no exact package mapping for {mapping_key}"
            affected.append(
                AffectedPackage(
                    str(coordinate) if coordinate else None,
                    _ecosystem_from_coordinate(str(coordinate)) if coordinate else None,
                    tuple(ranges),
                    resolution_error=error,
                )
            )
        refs = tuple(
            str(item.get("url")) for item in cna.get("references") or () if item.get("url")
        )
        state = str(metadata.get("state") or "").casefold()
        return AdvisoryRecord(
            advisory_id=advisory_id,
            source_format="CVE",
            source_version=str(payload.get("dataVersion") or payload.get("data_version") or "5.0"),
            summary=details.split("\n", 1)[0][:280],
            details=details,
            aliases=tuple(str(item) for item in payload.get("aliases") or ()),
            severities=tuple(severities),
            affected=tuple(affected),
            references=refs,
            published_at=metadata.get("datePublished"),
            modified_at=metadata.get("dateUpdated"),
            withdrawn_at=metadata.get("dateRejected") if state == "rejected" else None,
            metadata={"state": state, "assigner": metadata.get("assignerShortName")},
        )


def _ecosystem_from_coordinate(coordinate: str) -> str | None:
    if coordinate.startswith("pkg:") and coordinate.count(":") >= 2:
        return coordinate.split(":", 2)[1]
    return None


def ingest_advisory(
    conn: Any,
    record: AdvisoryRecord,
    *,
    source_url: str,
    source_document_id: str | None = None,
    observed_at: Any = None,
    domain: str = "technology",
) -> dict[str, Any]:
    """Persist an advisory; ambiguous packages remain explicit and unlinked."""

    status = "withdrawn" if record.withdrawn_at else "active"
    advisory = record_object(
        conn,
        object_type="advisory",
        object_id=f"advisory:{record.advisory_id}",
        canonical_name=record.advisory_id,
        immutable_id=f"{record.source_format}:{record.advisory_id}:{record.source_version}",
        status=status,
        published_at=record.published_at,
        modified_at=record.modified_at,
        observed_at=observed_at,
        source_url=source_url,
        source_document_id=source_document_id,
        metadata={
            **record.metadata,
            "summary": record.summary,
            "details": record.details,
            "aliases": list(record.aliases),
            "severities": list(record.severities),
            "references": list(record.references),
            "withdrawn_at": record.withdrawn_at,
            "severity_conflict": len(
                {str(item.get("score") or item.get("severity")) for item in record.severities}
            ) > 1,
        },
        domain=domain,
    )
    for alias in record.aliases:
        record_alias(
            conn, alias, advisory["object_id"], alias_kind="advisory_alias",
            source_document_id=source_document_id, observed_at=observed_at, domain=domain,
        )
    unresolved, linked = [], []
    for affected in record.affected:
        if not affected.coordinate:
            unresolved.append({"error": affected.resolution_error})
            continue
        package = resolve_package(conn, affected.coordinate, domain=domain)
        if package is None:
            unresolved.append(
                {
                    "coordinate": affected.coordinate,
                    "error": "exact canonical package is not present; no fuzzy match attempted",
                }
            )
            continue
        record_relation(
            conn, package["object_id"], "affected_by", advisory["object_id"],
            observed_at=observed_at, source_url=source_url,
            source_document_id=source_document_id,
            metadata={"explicit_versions": list(affected.versions)}, domain=domain,
        )
        for item in affected.ranges:
            events = list(item.get("events") or ())
            record_advisory_range(
                conn, advisory["object_id"], package["object_id"],
                ecosystem=affected.ecosystem or _ecosystem_from_coordinate(affected.coordinate) or "",
                range_type=str(item.get("type") or "ECOSYSTEM"),
                events=events, observed_at=observed_at,
                source_document_id=source_document_id, domain=domain,
            )
            fixed = next((event.get("fixed") for event in events if event.get("fixed")), None)
            if fixed:
                stored_version = conn.execute(
                    "SELECT object_id FROM technical_objects WHERE domain=? "
                    "AND object_type='version' AND coordinate=? AND version=? "
                    "ORDER BY object_id LIMIT 1",
                    [domain, affected.coordinate, str(fixed)],
                ).fetchone()
                fixed_id = (
                    str(stored_version[0])
                    if stored_version
                    else f"version:{affected.coordinate}@{fixed}"
                )
                record_relation(
                    conn, advisory["object_id"], "fixed_in", fixed_id,
                    constraint=str(fixed), observed_at=observed_at, source_url=source_url,
                    source_document_id=source_document_id,
                    metadata={"coordinate": affected.coordinate}, domain=domain,
                )
        linked.append(affected.coordinate)
    return {"advisory": advisory, "linked_packages": linked, "unresolved_packages": unresolved}


__all__ = [
    "AdvisoryError",
    "AdvisoryRecord",
    "AffectedPackage",
    "CVEAdapter",
    "OSVAdapter",
    "ingest_advisory",
]
