"""Canonical, version-aware ontology for technical knowledge."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from src.kb.temporal import parse_source_time, record_temporal_assertion

TECHNICAL_MODEL = "noesis-technical-model-v1"
OBJECT_TYPES = frozenset(
    {
        "repository",
        "commit",
        "release",
        "package",
        "version",
        "dependency",
        "advisory",
        "vulnerability",
        "standard",
        "specification",
        "implementation",
    }
)
RELATION_TYPES = frozenset(
    {
        "contains",
        "released_as",
        "depends_on",
        "optional_dependency",
        "affected_by",
        "fixed_in",
        "supersedes",
        "implements",
        "fork_of",
        "renamed_from",
        "vendored_from",
        "breaking_change",
        "alias_of",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS technical_objects (
    domain TEXT NOT NULL,
    object_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    coordinate TEXT,
    canonical_name TEXT NOT NULL,
    version TEXT,
    immutable_id TEXT,
    status TEXT,
    published_at_ms BIGINT,
    modified_at_ms BIGINT,
    observed_at_ms BIGINT NOT NULL,
    source_url TEXT,
    source_document_id TEXT,
    metadata_json TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    PRIMARY KEY (domain, object_id)
);
CREATE TABLE IF NOT EXISTS technical_aliases (
    domain TEXT NOT NULL,
    alias TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    alias_kind TEXT NOT NULL,
    source_document_id TEXT,
    observed_at_ms BIGINT NOT NULL,
    PRIMARY KEY (domain, alias)
);
CREATE TABLE IF NOT EXISTS technical_relations (
    domain TEXT NOT NULL,
    relation_id TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    object_id TEXT NOT NULL,
    constraint_text TEXT,
    optional BOOLEAN NOT NULL,
    observed_at_ms BIGINT NOT NULL,
    source_url TEXT,
    source_document_id TEXT,
    metadata_json TEXT NOT NULL,
    PRIMARY KEY (domain, relation_id)
);
CREATE TABLE IF NOT EXISTS technical_advisory_ranges (
    domain TEXT NOT NULL,
    advisory_id TEXT NOT NULL,
    package_id TEXT NOT NULL,
    ecosystem TEXT NOT NULL,
    range_type TEXT NOT NULL,
    introduced TEXT,
    fixed TEXT,
    last_affected TEXT,
    limit_version TEXT,
    events_json TEXT NOT NULL,
    observed_at_ms BIGINT NOT NULL,
    source_document_id TEXT,
    PRIMARY KEY (domain, advisory_id, package_id, range_type, events_json)
);
"""


class TechnicalModelError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _stable_id(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_json(value).encode()).hexdigest()[:24]
    return f"{prefix}:{digest}"


def _millis(value: Any, field: str, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return parse_source_time(value, field=field)[0]
    except Exception as exc:
        raise TechnicalModelError(
            "bad_time", f"{field} must be ISO-8601 or epoch milliseconds"
        ) from exc


def ensure_technical_schema(conn: Any) -> None:
    conn.execute(_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_technical_coordinate "
        "ON technical_objects (domain, coordinate, version)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_technical_relation_subject "
        "ON technical_relations (domain, subject_id, relation)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_technical_relation_object "
        "ON technical_relations (domain, object_id, relation)"
    )


def canonical_ecosystem(ecosystem: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "", ecosystem.casefold())
    aliases = {
        "python": "pypi",
        "pypi": "pypi",
        "node": "npm",
        "nodejs": "npm",
        "npm": "npm",
        "maven": "maven",
        "mavencentral": "maven",
        "cargo": "cargo",
        "crate": "cargo",
        "cratesio": "cargo",
        "go": "golang",
        "golang": "golang",
        "gomodule": "golang",
    }
    if key not in aliases:
        raise TechnicalModelError("unknown_ecosystem", f"unsupported ecosystem {ecosystem!r}")
    return aliases[key]


def canonical_package_coordinate(ecosystem: str, name: str) -> str:
    """Return a purl-shaped coordinate without discarding the source name."""

    eco = canonical_ecosystem(ecosystem)
    raw = str(name).strip()
    if not raw:
        raise TechnicalModelError("bad_coordinate", "package name must be non-empty")
    if eco == "pypi":
        normalized = re.sub(r"[-_.]+", "-", raw).casefold()
    elif eco == "npm":
        normalized = raw.casefold()
        if normalized.startswith("@") and normalized.count("/") != 1:
            raise TechnicalModelError("bad_coordinate", "scoped npm names require @scope/name")
    elif eco == "maven":
        if raw.count(":") != 1 or any(not part for part in raw.split(":")):
            raise TechnicalModelError("bad_coordinate", "Maven names require group:artifact")
        normalized = raw
    elif eco == "cargo":
        normalized = raw.casefold()
    else:
        normalized = re.sub(r"^https?://", "", raw).rstrip("/")
        if "/" not in normalized:
            raise TechnicalModelError("bad_coordinate", "Go module names require a module path")
    return "pkg:" + eco + ":" + quote(normalized, safe="/@:.!~_-")


def package_object_id(coordinate: str) -> str:
    return _stable_id("package", coordinate)


def immutable_artifact_id(coordinate: str, version: str, checksum: str | None = None) -> str:
    version = str(version).strip()
    if not version:
        raise TechnicalModelError("bad_coordinate", "version must be non-empty")
    suffix = f"?checksum={quote(checksum, safe=':')}" if checksum else ""
    return f"{coordinate}@{quote(version, safe='.+!~_-')}{suffix}"


def sanitize_repository_url(url: str) -> str:
    """Strip credentials and fragments before a repository locator is persisted."""

    value = str(url).strip()
    if "://" not in value:
        return value
    parts = urlsplit(value)
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def record_object(
    conn: Any,
    *,
    object_type: str,
    canonical_name: str,
    object_id: str | None = None,
    coordinate: str | None = None,
    version: str | None = None,
    immutable_id: str | None = None,
    status: str | None = None,
    published_at: Any = None,
    modified_at: Any = None,
    observed_at: Any = None,
    source_url: str | None = None,
    source_document_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    provenance: Mapping[str, Any] | None = None,
    domain: str = "technology",
    backing: str = "corpus-view",
    visibility: str = "public",
) -> dict[str, Any]:
    if object_type not in OBJECT_TYPES:
        raise TechnicalModelError("bad_object_type", f"unsupported object type {object_type!r}")
    ensure_technical_schema(conn)
    observed = _millis(observed_at, "observed_at", int(time.time() * 1000))
    published = _millis(published_at, "published_at")
    modified = _millis(modified_at, "modified_at")
    safe_url = sanitize_repository_url(source_url) if source_url else None
    oid = object_id or _stable_id(object_type, [coordinate, version, canonical_name])
    existing = conn.execute(
        "SELECT object_type, coordinate, version, immutable_id FROM technical_objects "
        "WHERE domain=? AND object_id=?",
        [domain, oid],
    ).fetchone()
    identity = (object_type, coordinate, version, immutable_id)
    if existing is not None and tuple(existing) != identity:
        raise TechnicalModelError(
            "identity_conflict", "a technical object ID cannot change immutable identity"
        )
    payload = {
        "technical_contract": TECHNICAL_MODEL,
        "object_id": oid,
        "object_type": object_type,
        "coordinate": coordinate,
        "canonical_name": str(canonical_name).strip(),
        "version": version,
        "immutable_id": immutable_id,
        "status": status,
        "published_at_ms": published,
        "modified_at_ms": modified,
        "observed_at_ms": observed,
        "source_url": safe_url,
        "source_document_id": source_document_id,
        "metadata": dict(metadata or {}),
        "provenance": dict(provenance or {}),
    }
    conn.execute(
        "INSERT OR REPLACE INTO technical_objects VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            domain, oid, object_type, coordinate, payload["canonical_name"], version,
            immutable_id, status, published, modified, observed, safe_url,
            source_document_id, _json(payload["metadata"]), _json(payload["provenance"]),
        ],
    )
    record_temporal_assertion(
        conn,
        domain=domain,
        backing=backing,
        assertion_kind="entity",
        assertion_id=oid,
        payload=payload,
        observed_at_ms=observed,
        valid_from_ms=published,
        valid_time_precision="millisecond" if published is not None else "unknown",
        source_document_id=source_document_id,
        visibility=visibility,
    )
    return payload


def record_alias(
    conn: Any,
    alias: str,
    canonical_id: str,
    *,
    alias_kind: str = "alias",
    source_document_id: str | None = None,
    observed_at: Any = None,
    domain: str = "technology",
) -> None:
    ensure_technical_schema(conn)
    observed = _millis(observed_at, "observed_at", int(time.time() * 1000))
    conn.execute(
        "INSERT OR REPLACE INTO technical_aliases VALUES (?, ?, ?, ?, ?, ?)",
        [domain, alias, canonical_id, alias_kind, source_document_id, observed],
    )


def record_relation(
    conn: Any,
    subject_id: str,
    relation: str,
    object_id: str,
    *,
    constraint: str | None = None,
    optional: bool = False,
    observed_at: Any = None,
    source_url: str | None = None,
    source_document_id: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    domain: str = "technology",
    backing: str = "corpus-view",
    visibility: str = "public",
) -> dict[str, Any]:
    if relation not in RELATION_TYPES:
        raise TechnicalModelError("bad_relation_type", f"unsupported relation {relation!r}")
    ensure_technical_schema(conn)
    observed = _millis(observed_at, "observed_at", int(time.time() * 1000))
    relation_id = _stable_id(
        "technical-relation",
        [domain, subject_id, relation, object_id, constraint, optional, source_document_id],
    )
    safe_url = sanitize_repository_url(source_url) if source_url else None
    attributes = dict(metadata or {})
    conn.execute(
        "INSERT OR REPLACE INTO technical_relations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            domain, relation_id, subject_id, relation, object_id, constraint,
            bool(optional), observed, safe_url, source_document_id, _json(attributes),
        ],
    )
    payload = {
        "relation_id": relation_id,
        "subject_id": subject_id,
        "relation": relation,
        "object_id": object_id,
        "constraint": constraint,
        "optional": bool(optional),
        "observed_at_ms": observed,
        "source_url": safe_url,
        "source_document_id": source_document_id,
        "metadata": attributes,
    }
    record_temporal_assertion(
        conn,
        domain=domain,
        backing=backing,
        assertion_kind="relation",
        assertion_id=relation_id,
        payload=payload,
        observed_at_ms=observed,
        source_document_id=source_document_id,
        visibility=visibility,
    )
    return payload


def resolve_package(
    conn: Any, coordinate_or_alias: str, *, domain: str = "technology"
) -> dict[str, Any] | None:
    """Resolve only exact canonical coordinates or recorded aliases."""

    ensure_technical_schema(conn)
    row = conn.execute(
        "SELECT object_id, coordinate, canonical_name, metadata_json FROM technical_objects "
        "WHERE domain=? AND object_type='package' AND coordinate=?",
        [domain, coordinate_or_alias],
    ).fetchone()
    if row is None:
        alias = conn.execute(
            "SELECT canonical_id FROM technical_aliases WHERE domain=? AND alias=?",
            [domain, coordinate_or_alias],
        ).fetchone()
        if alias:
            row = conn.execute(
                "SELECT object_id, coordinate, canonical_name, metadata_json FROM technical_objects "
                "WHERE domain=? AND object_id=?",
                [domain, alias[0]],
            ).fetchone()
    if row is None:
        return None
    return {
        "object_id": row[0],
        "coordinate": row[1],
        "canonical_name": row[2],
        "metadata": json.loads(row[3]),
    }


def record_advisory_range(
    conn: Any,
    advisory_id: str,
    package_id: str,
    *,
    ecosystem: str,
    range_type: str,
    events: list[Mapping[str, Any]],
    observed_at: Any = None,
    source_document_id: str | None = None,
    domain: str = "technology",
) -> None:
    ensure_technical_schema(conn)
    observed = _millis(observed_at, "observed_at", int(time.time() * 1000))
    normalized = [dict(event) for event in events]
    introduced = next((e["introduced"] for e in normalized if "introduced" in e), None)
    fixed = next((e["fixed"] for e in normalized if "fixed" in e), None)
    last_affected = next((e["last_affected"] for e in normalized if "last_affected" in e), None)
    limit_version = next((e["limit"] for e in normalized if "limit" in e), None)
    conn.execute(
        "INSERT OR REPLACE INTO technical_advisory_ranges VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            domain, advisory_id, package_id, canonical_ecosystem(ecosystem),
            range_type.upper(), introduced, fixed, last_affected, limit_version,
            _json(normalized), observed, source_document_id,
        ],
    )


def version_key(version: str) -> tuple[tuple[int, ...], int, int]:
    """Deterministic loose ordering used only where no native comparator exists."""

    cleaned = str(version).strip().lstrip("vV")
    release_match = re.match(r"\d+(?:\.\d+)*", cleaned)
    release = (
        tuple(int(part) for part in release_match.group(0).split("."))
        if release_match else ()
    )
    release = (release + (0,) * 8)[:8]
    suffix = cleaned[release_match.end():].casefold() if release_match else cleaned.casefold()
    qualifier = 0
    if any(value in suffix for value in ("dev", "snapshot")):
        qualifier = -4
    elif re.search(r"(?:^|[-_.])(?:alpha|a)\d*", suffix):
        qualifier = -3
    elif re.search(r"(?:^|[-_.])(?:beta|b)\d*", suffix):
        qualifier = -2
    elif any(value in suffix for value in ("rc", "pre")):
        qualifier = -1
    elif any(value in suffix for value in ("post", "rev")):
        qualifier = 1
    number_match = re.search(r"\d+", suffix)
    return release, qualifier, int(number_match.group(0)) if number_match else 0


def version_in_events(version: str, events: list[Mapping[str, Any]]) -> bool:
    """Evaluate ordered OSV events without treating unknown syntax as affected."""

    current = False
    target = version_key(version)
    for event in events:
        if "introduced" in event and (
            str(event["introduced"]) == "0" or target >= version_key(str(event["introduced"]))
        ):
            current = True
        if "fixed" in event and target >= version_key(str(event["fixed"])):
            current = False
        if "last_affected" in event and target > version_key(str(event["last_affected"])):
            current = False
        if "limit" in event and target >= version_key(str(event["limit"])):
            current = False
    return current


__all__ = [
    "OBJECT_TYPES",
    "RELATION_TYPES",
    "TECHNICAL_MODEL",
    "TechnicalModelError",
    "canonical_ecosystem",
    "canonical_package_coordinate",
    "ensure_technical_schema",
    "immutable_artifact_id",
    "package_object_id",
    "record_advisory_range",
    "record_alias",
    "record_object",
    "record_relation",
    "resolve_package",
    "sanitize_repository_url",
    "version_in_events",
    "version_key",
]
