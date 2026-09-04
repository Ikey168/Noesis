"""Package-registry adapters with fixture-first, opt-in live retrieval."""

from __future__ import annotations

import abc
import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from services.ingest.common.document_model import Document
from src.domains.technical.model import (
    canonical_ecosystem,
    canonical_package_coordinate,
    immutable_artifact_id,
    package_object_id,
    record_alias,
    record_object,
    record_relation,
)
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector

LIVE_ENV = "NOESIS_TECHNICAL_LIVE"


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageVersion:
    version: str
    published_at: str | int | None = None
    checksum: str | None = None
    yanked: bool = False
    deprecated: bool = False
    licenses: tuple[str, ...] = ()
    dependencies: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PackageRecord:
    ecosystem: str
    name: str
    source_url: str
    versions: tuple[PackageVersion, ...]
    maintainers: tuple[str, ...] = ()
    licenses: tuple[str, ...] = ()
    repository_url: str | None = None
    deprecated: bool = False
    aliases: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def coordinate(self) -> str:
        return canonical_package_coordinate(self.ecosystem, self.name)


class RateLimiter:
    """Small process-local limiter, injectable for deterministic tests."""

    def __init__(self, minimum_interval: float = 0.25, clock=time.monotonic, sleep=time.sleep):
        self.minimum_interval = max(0.0, float(minimum_interval))
        self.clock, self.sleep = clock, sleep
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            delay = self.minimum_interval - (now - self._last)
            if delay > 0:
                self.sleep(delay)
                now = self.clock()
            self._last = now


class PackageRegistryProvider(abc.ABC):
    ecosystem = ""
    registry_url = ""

    def __init__(
        self,
        *,
        opener: Callable[..., Any] = urlopen,
        limiter: RateLimiter | None = None,
    ) -> None:
        self.opener = opener
        self.limiter = limiter or RateLimiter()

    @abc.abstractmethod
    def endpoint(self, name: str) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def parse(self, payload: dict[str, Any], *, source_url: str) -> PackageRecord:
        raise NotImplementedError

    def fetch_live(self, name: str) -> PackageRecord:
        if os.getenv(LIVE_ENV) != "1":
            raise RegistryError(f"live registry access requires {LIVE_ENV}=1")
        self.limiter.wait()
        endpoint = self.endpoint(name)
        request = Request(endpoint, headers={"User-Agent": "Noesis/technical-knowledge"})
        with self.opener(request, timeout=20) as response:
            payload = json.loads(response.read().decode())
        return self.parse(payload, source_url=endpoint)

    def load_fixture(self, path: str | Path) -> PackageRecord:
        fixture = Path(path)
        return self.parse(json.loads(fixture.read_text()), source_url=fixture.as_uri())


def _strings(values: Any, *keys: str) -> tuple[str, ...]:
    result: list[str] = []
    for value in values or []:
        if isinstance(value, str):
            result.append(value)
        elif isinstance(value, dict):
            candidate = next((value.get(key) for key in keys if value.get(key)), None)
            if candidate:
                result.append(str(candidate))
    return tuple(dict.fromkeys(result))


class PyPIProvider(PackageRegistryProvider):
    ecosystem, registry_url = "pypi", "https://pypi.org"

    def endpoint(self, name: str) -> str:
        return f"{self.registry_url}/pypi/{quote(name, safe='')}/json"

    def parse(self, payload: dict[str, Any], *, source_url: str) -> PackageRecord:
        info = payload.get("info") or {}
        name = str(info.get("name") or payload.get("name") or "")
        versions = []
        for version, files in (payload.get("releases") or {}).items():
            files = files or []
            first = files[0] if files else {}
            versions.append(
                PackageVersion(
                    str(version),
                    published_at=first.get("upload_time_iso_8601") or first.get("upload_time"),
                    checksum=(first.get("digests") or {}).get("sha256"),
                    yanked=any(bool(item.get("yanked")) for item in files),
                    licenses=tuple(filter(None, [info.get("license")])),
                    metadata={"filenames": [item.get("filename") for item in files if item.get("filename")]},
                )
            )
        project_urls = info.get("project_urls") or {}
        repository = project_urls.get("Source") or project_urls.get("Repository")
        return PackageRecord(
            self.ecosystem,
            name,
            source_url,
            tuple(versions),
            maintainers=_strings(info.get("maintainers"), "name", "username"),
            licenses=tuple(filter(None, [info.get("license")])),
            repository_url=repository,
            deprecated=bool(info.get("yanked")),
            metadata={"summary": info.get("summary"), "original_name": name},
        )


class NpmProvider(PackageRegistryProvider):
    ecosystem, registry_url = "npm", "https://registry.npmjs.org"

    def endpoint(self, name: str) -> str:
        return f"{self.registry_url}/{quote(name, safe='')}"

    def parse(self, payload: dict[str, Any], *, source_url: str) -> PackageRecord:
        name = str(payload.get("name") or "")
        published = payload.get("time") or {}
        versions = []
        for version, item in (payload.get("versions") or {}).items():
            dist = item.get("dist") or {}
            dependencies = tuple(
                {"ecosystem": "npm", "name": dep, "constraint": constraint}
                for dep, constraint in (item.get("dependencies") or {}).items()
            )
            optional = tuple(
                {"ecosystem": "npm", "name": dep, "constraint": constraint, "optional": True}
                for dep, constraint in (item.get("optionalDependencies") or {}).items()
            )
            versions.append(
                PackageVersion(
                    str(version),
                    published_at=published.get(version),
                    checksum=dist.get("integrity") or dist.get("shasum"),
                    deprecated=bool(item.get("deprecated")),
                    licenses=tuple(filter(None, [item.get("license")])),
                    dependencies=dependencies + optional,
                )
            )
        repository = payload.get("repository")
        if isinstance(repository, dict):
            repository = repository.get("url")
        return PackageRecord(
            self.ecosystem,
            name,
            source_url,
            tuple(versions),
            maintainers=_strings(payload.get("maintainers"), "name", "email"),
            licenses=tuple(filter(None, [payload.get("license")])),
            repository_url=repository,
            deprecated=bool(payload.get("deprecated")),
            aliases=tuple(payload.get("aliases") or ()),
            metadata={"dist_tags": payload.get("dist-tags") or {}, "original_name": name},
        )


class MavenCentralProvider(PackageRegistryProvider):
    ecosystem, registry_url = "maven", "https://search.maven.org"

    def endpoint(self, name: str) -> str:
        group, artifact = name.split(":", 1)
        return (
            f"{self.registry_url}/solrsearch/select?q=g:%22{quote(group)}%22"
            f"+AND+a:%22{quote(artifact)}%22&core=gav&rows=200&wt=json"
        )

    def parse(self, payload: dict[str, Any], *, source_url: str) -> PackageRecord:
        docs = (payload.get("response") or {}).get("docs") or payload.get("versions") or []
        if not docs:
            raise RegistryError("Maven response contains no versions")
        first = docs[0]
        name = f"{first.get('g') or first.get('group')}:{first.get('a') or first.get('artifact')}"
        versions = tuple(
            PackageVersion(
                str(item.get("v") or item.get("version")),
                published_at=item.get("timestamp"),
                checksum=item.get("sha256"),
                yanked=bool(item.get("yanked")),
                licenses=_strings(item.get("licenses")),
                dependencies=tuple(item.get("dependencies") or ()),
            )
            for item in docs
        )
        return PackageRecord(
            self.ecosystem,
            name,
            source_url,
            versions,
            maintainers=_strings(first.get("developers"), "name", "id"),
            licenses=_strings(first.get("licenses")),
            repository_url=first.get("scm"),
            metadata={"original_name": name},
        )


class CratesIOProvider(PackageRegistryProvider):
    ecosystem, registry_url = "cargo", "https://crates.io"

    def endpoint(self, name: str) -> str:
        return f"{self.registry_url}/api/v1/crates/{quote(name, safe='')}"

    def parse(self, payload: dict[str, Any], *, source_url: str) -> PackageRecord:
        crate = payload.get("crate") or {}
        name = str(crate.get("name") or payload.get("name") or "")
        versions = tuple(
            PackageVersion(
                str(item.get("num") or item.get("version")),
                published_at=item.get("created_at"),
                checksum=item.get("checksum"),
                yanked=bool(item.get("yanked")),
                licenses=tuple(filter(None, [item.get("license")])),
                dependencies=tuple(item.get("dependencies") or ()),
            )
            for item in payload.get("versions") or ()
        )
        return PackageRecord(
            self.ecosystem,
            name,
            source_url,
            versions,
            licenses=tuple(filter(None, [crate.get("license")])),
            repository_url=crate.get("repository"),
            deprecated=bool(crate.get("deprecated")),
            metadata={"downloads": crate.get("downloads"), "original_name": name},
        )


class GoModuleProvider(PackageRegistryProvider):
    ecosystem, registry_url = "golang", "https://proxy.golang.org"

    def endpoint(self, name: str) -> str:
        return f"{self.registry_url}/{quote(name, safe='/')}/@latest"

    def parse(self, payload: dict[str, Any], *, source_url: str) -> PackageRecord:
        name = str(payload.get("module") or payload.get("Path") or payload.get("name") or "")
        raw_versions = payload.get("versions")
        if raw_versions is None:
            raw_versions = [payload]
        versions = tuple(
            PackageVersion(
                str(item.get("version") or item.get("Version")),
                published_at=item.get("time") or item.get("Time"),
                checksum=item.get("sum") or item.get("Sum"),
                deprecated=bool(item.get("Deprecated")),
                dependencies=tuple(item.get("dependencies") or ()),
            )
            for item in raw_versions
        )
        return PackageRecord(
            self.ecosystem,
            name,
            source_url,
            versions,
            repository_url=payload.get("repository"),
            deprecated=bool(payload.get("Deprecated")),
            metadata={"original_name": name},
        )


PROVIDERS = {
    "pypi": PyPIProvider,
    "npm": NpmProvider,
    "maven": MavenCentralProvider,
    "cargo": CratesIOProvider,
    "golang": GoModuleProvider,
}


@register_connector
class PackageRegistryConnector(Connector):
    """Expose registry records through the common document connector contract."""

    source_type = "web"
    name = "package-registry"

    def discover(self, query: Any = None):
        query = dict(query or {})
        try:
            ecosystem = canonical_ecosystem(str(query.get("ecosystem") or ""))
        except Exception as exc:
            raise RegistryError("a supported ecosystem is required") from exc
        package = str(query.get("package") or query.get("name") or "").strip()
        if not package:
            raise RegistryError("ecosystem and package are required")
        provider = PROVIDERS[ecosystem]()
        locator = str(query.get("fixture") or provider.endpoint(package))
        yield SourceRef(
            locator,
            package,
            {
                "source_id": f"registry:{ecosystem}:{package}",
                "ecosystem": ecosystem,
                "package": package,
                "fixture": bool(query.get("fixture")),
            },
        )

    def fetch(self, ref: SourceRef) -> RawDocument:
        provider = PROVIDERS[str(ref.metadata["ecosystem"])]()
        record = (
            provider.load_fixture(ref.locator)
            if ref.metadata.get("fixture")
            else provider.fetch_live(str(ref.metadata["package"]))
        )
        return RawDocument(
            ref=ref,
            content=json.dumps(asdict(record), default=str),
            content_type="application/json",
        )

    def parse(self, raw: RawDocument) -> list[Document]:
        payload = json.loads(raw.content)
        coordinate = canonical_package_coordinate(payload["ecosystem"], payload["name"])
        common = {
            "kind": "package_registry",
            "coordinate": coordinate,
            "ecosystem": payload["ecosystem"],
            "original_name": payload["name"],
            "maintainers": payload["maintainers"],
            "licenses": payload["licenses"],
            "repository_url": payload.get("repository_url"),
            "registry_source_url": payload["source_url"],
        }
        documents = []
        for release in payload["versions"]:
            version = str(release["version"])
            documents.append(
                Document(
                    document_id=(
                        "technical:registry:"
                        + hashlib.sha256(
                            f"{coordinate}@{version}".encode()
                        ).hexdigest()[:28]
                    ),
                    source_type=self.source_type,
                    language="en",
                    ingested_at=raw.fetched_at,
                    source_id=raw.ref.source_id,
                    url=payload["source_url"],
                    title=f"{payload['name']} {version}",
                    content=json.dumps(release, sort_keys=True),
                    created_at=_registry_millis(release.get("published_at")),
                    metadata={
                        **common,
                        "version": version,
                        "checksum": release.get("checksum"),
                        "yanked": bool(release.get("yanked")),
                        "deprecated": bool(release.get("deprecated")),
                    },
                )
            )
        return documents


def _registry_millis(value: Any) -> int | None:
    if value is None:
        return None
    from src.kb.temporal import parse_source_time

    return parse_source_time(value, field="published_at")[0]


def ingest_package(
    conn: Any,
    record: PackageRecord,
    *,
    observed_at: int | str | None = None,
    source_document_id: str | None = None,
    domain: str = "technology",
) -> dict[str, Any]:
    """Persist one registry result and its dependency edges."""

    coordinate = record.coordinate
    package_id = package_object_id(coordinate)
    package = record_object(
        conn,
        object_type="package",
        object_id=package_id,
        coordinate=coordinate,
        canonical_name=record.name,
        status="deprecated" if record.deprecated else "active",
        observed_at=observed_at,
        source_url=record.source_url,
        source_document_id=source_document_id,
        metadata={
            **record.metadata,
            "maintainers": list(record.maintainers),
            "licenses": list(record.licenses),
            "repository_url": record.repository_url,
        },
        domain=domain,
    )
    for alias in record.aliases:
        record_alias(
            conn, alias, package_id, alias_kind="registry_alias",
            source_document_id=source_document_id, observed_at=observed_at, domain=domain,
        )
    stored_versions = []
    for release in record.versions:
        artifact = immutable_artifact_id(coordinate, release.version, release.checksum)
        version_id = "version:" + artifact
        stored = record_object(
            conn,
            object_type="version",
            object_id=version_id,
            coordinate=coordinate,
            canonical_name=f"{record.name} {release.version}",
            version=release.version,
            immutable_id=artifact,
            status="yanked" if release.yanked else ("deprecated" if release.deprecated else "active"),
            published_at=release.published_at,
            observed_at=observed_at,
            source_url=record.source_url,
            source_document_id=source_document_id,
            metadata={
                **release.metadata,
                "checksum": release.checksum,
                "licenses": list(release.licenses),
            },
            domain=domain,
        )
        record_relation(
            conn, package_id, "released_as", version_id,
            observed_at=observed_at, source_url=record.source_url,
            source_document_id=source_document_id, domain=domain,
        )
        for dependency in release.dependencies:
            dep_coordinate = canonical_package_coordinate(
                str(dependency.get("ecosystem") or record.ecosystem),
                str(dependency.get("name") or dependency.get("package")),
            )
            dep_id = package_object_id(dep_coordinate)
            if not record_object_exists(conn, dep_id, domain=domain):
                record_object(
                    conn, object_type="package", object_id=dep_id,
                    coordinate=dep_coordinate,
                    canonical_name=str(dependency.get("name") or dependency.get("package")),
                    status="unresolved", observed_at=observed_at,
                    source_url=record.source_url, source_document_id=source_document_id,
                    metadata={"placeholder": True}, domain=domain,
                )
            optional = bool(dependency.get("optional"))
            record_relation(
                conn, version_id, "optional_dependency" if optional else "depends_on", dep_id,
                constraint=str(dependency.get("constraint") or dependency.get("req") or "*"),
                optional=optional, observed_at=observed_at, source_url=record.source_url,
                source_document_id=source_document_id,
                metadata={"scope": dependency.get("scope"), "registry_recorded": True},
                domain=domain,
            )
        stored_versions.append(stored)
    return {"package": package, "versions": stored_versions}


def record_object_exists(conn: Any, object_id: str, *, domain: str) -> bool:
    from src.domains.technical.model import ensure_technical_schema

    ensure_technical_schema(conn)
    return bool(
        conn.execute(
            "SELECT 1 FROM technical_objects WHERE domain=? AND object_id=?",
            [domain, object_id],
        ).fetchone()
    )


__all__ = [
    "LIVE_ENV",
    "PROVIDERS",
    "CratesIOProvider",
    "GoModuleProvider",
    "MavenCentralProvider",
    "NpmProvider",
    "PackageRecord",
    "PackageRegistryConnector",
    "PackageRegistryProvider",
    "PackageVersion",
    "PyPIProvider",
    "RateLimiter",
    "RegistryError",
    "ingest_package",
]
