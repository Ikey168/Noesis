"""Package-registry adapters with fixture-first, opt-in live retrieval."""

from __future__ import annotations

import abc
import hashlib
import json
import os
import re
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

    # -- OSS Ecosystems history (#2195): revisions instead of a current snapshot.

    history_source = ""

    def history_items(self, name: str, versions: list[str]) -> list[dict[str, Any]]:
        """The bounded documents to read for a package: one package-level listing plus per-version documents."""
        raise RegistryError(f"{self.ecosystem} has no release-history support")

    def history(
        self,
        payload: Any,
        *,
        kind: str,
        source_url: str,
        package: str,
        version: str | None = None,
    ) -> list[dict[str, Any]]:
        """``noesis-oss-ecosystem-record-v1`` statements for one document.

        Maintainer, author and publisher-user fields are never read; only
        organisation-level publisher declarations are emitted.
        """
        raise RegistryError(f"{self.ecosystem} has no release-history support")


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

    history_source = "pypi"

    def history_items(self, name: str, versions: list[str]) -> list[dict[str, Any]]:
        return _pypi_items(self, name, versions)

    def history(self, payload, *, kind, source_url, package, version=None):
        return _pypi_history(
            self,
            payload,
            kind=kind,
            source_url=source_url,
            package=package,
            version=version,
        )

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

    history_source = "npm"

    def history_items(self, name: str, versions: list[str]) -> list[dict[str, Any]]:
        return _npm_items(self, name, versions)

    def history(self, payload, *, kind, source_url, package, version=None):
        return _npm_history(
            self,
            payload,
            kind=kind,
            source_url=source_url,
            package=package,
            version=version,
        )

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

    history_source = "maven-central"

    def history_items(self, name: str, versions: list[str]) -> list[dict[str, Any]]:
        return _maven_items(self, name, versions)

    def history(self, payload, *, kind, source_url, package, version=None):
        return _maven_history(
            self,
            payload,
            kind=kind,
            source_url=source_url,
            package=package,
            version=version,
        )

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

    history_source = "crates-io"

    def history_items(self, name: str, versions: list[str]) -> list[dict[str, Any]]:
        return _crates_items(self, name, versions)

    def history(self, payload, *, kind, source_url, package, version=None):
        return _crates_history(
            self,
            payload,
            kind=kind,
            source_url=source_url,
            package=package,
            version=version,
        )

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


# ---------------------------------------------------------------- release history (#2195)
#
# Each helper reads only the documented fields named in
# docs/development/oss-ecosystems-evidence/source-audit.md. Author, maintainer,
# owner-user and publisher-user fields are never read, so no statement can
# carry a person. Nothing here resolves a dependency constraint.

_REPOSITORY_KEYS = ("source", "source code", "repository", "code", "github", "gitlab")


def _statement(
    source: str,
    ecosystem: str,
    package: str,
    source_url: str,
    record_type: str,
    **fields: Any,
) -> dict[str, Any]:
    body = {
        "record_type": record_type,
        "source": source,
        "ecosystem": ecosystem,
        "package": package,
        "source_url": source_url,
    }
    body.update({k: v for k, v in fields.items() if v not in (None, "", [], {})})
    return body


def _earliest(values: list[Any]) -> Any:
    present = [str(v) for v in values if v]
    return min(present) if present else None


def _pep508(requirement: str) -> dict[str, Any]:
    from packaging.requirements import InvalidRequirement, Requirement

    raw = str(requirement).strip()
    try:
        parsed = Requirement(raw)
    except InvalidRequirement:
        return {
            "ecosystem": "pypi",
            "name": re.split(r"[\s\[(<>=!~;@]", raw, maxsplit=1)[0] or raw,
            "scope": "runtime",
            "requirement": raw,
            "unsupported": "not a PEP 508 requirement",
        }
    body = raw.split(";", 1)[0].strip()
    body = re.sub(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?", "", body).strip()
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1].strip()
    marker = str(parsed.marker) if parsed.marker is not None else None
    extra = re.search(r"""extra\s*==\s*["']([^"']+)["']""", marker or "")
    entry: dict[str, Any] = {
        "ecosystem": "pypi",
        "name": parsed.name,
        "requirement": raw,
        "constraint": body or None,
        "extras": sorted(parsed.extras) or None,
        "marker": marker,
        "scope": "optional" if extra else "runtime",
        "optional": bool(extra),
        "extra": extra[1] if extra else None,
    }
    return {k: v for k, v in entry.items() if v is not None}


def _pypi_items(provider: Any, name: str, versions: list[str]) -> list[dict[str, Any]]:
    base = f"{provider.registry_url}/pypi/{quote(name, safe='')}"
    return [{"url": f"{base}/json", "kind": "project"}] + [
        {
            "url": f"{base}/{quote(str(v), safe='')}/json",
            "kind": "version",
            "version": str(v),
        }
        for v in versions
    ]


def _pypi_state(
    files: list[dict[str, Any]], info: dict[str, Any] | None = None
) -> tuple[str, str | None]:
    yanked = bool(files) and all(bool(item.get("yanked")) for item in files)
    if info is not None and "yanked" in info:
        yanked = bool(info.get("yanked"))
    if not yanked:
        return "published", None
    reasons = [
        str(item.get("yanked_reason")) for item in files if item.get("yanked_reason")
    ]
    reason = (info or {}).get("yanked_reason") or (reasons[0] if reasons else None)
    return "yanked", reason


def _pypi_history(
    provider: Any,
    payload: Any,
    *,
    kind: str,
    source_url: str,
    package: str,
    version: str | None,
) -> list[dict[str, Any]]:
    info = dict(payload.get("info") or {})
    name = str(info.get("name") or package)
    out: list[dict[str, Any]] = []

    def emit(record_type: str, **fields: Any) -> None:
        out.append(_statement("pypi", "pypi", name, source_url, record_type, **fields))

    if kind == "project":
        releases = dict(payload.get("releases") or {})
        for number, files in sorted(releases.items()):
            files = list(files or [])
            state, reason = _pypi_state(files)
            emit(
                "release_state_revision",
                version=str(number),
                state=state,
                reason=reason,
                published_at=_earliest(
                    [
                        f.get("upload_time_iso_8601") or f.get("upload_time")
                        for f in files
                    ]
                ),
            )
        emit("release_listing", versions=sorted(releases))
        organisation = dict(payload.get("ownership") or {}).get("organization")
        if organisation:
            emit(
                "publisher_organisation",
                organisations=[
                    {
                        "kind": "pypi-organisation",
                        "id": str(organisation),
                        "verification": "organisation account as stated by PyPI (verify)",
                    }
                ],
            )
        urls = {
            str(k).casefold(): v
            for k, v in dict(info.get("project_urls") or {}).items()
        }
        links = [
            {"url": urls[k], "field": f"info.project_urls.{k}"}
            for k in _REPOSITORY_KEYS
            if urls.get(k)
        ]
        emit("repository_link_assertion", links=links[:1])
        return [
            s
            for s in out
            if s["record_type"] != "repository_link_assertion" or s.get("links")
        ]
    number = str(version or info.get("version"))
    state, reason = _pypi_state(list(payload.get("urls") or []), info)
    emit(
        "release_state_revision",
        version=number,
        state=state,
        reason=reason,
        published_at=_earliest(
            [f.get("upload_time_iso_8601") for f in payload.get("urls") or []]
        ),
    )
    emit("declared_dependency_set", version=number)
    out[-1]["entries"] = [_pep508(r) for r in info.get("requires_dist") or []]
    licence_text = info.get("license")
    if licence_text and len(str(licence_text)) > 300:
        licence_text = None  # a pasted licence body is not a declaration to normalise; classifiers still apply
    emit(
        "licence_declaration_revision",
        version=number,
        raw={
            "expression": info.get("license_expression"),
            "text": licence_text,
            "classifiers": [
                c
                for c in info.get("classifiers") or []
                if str(c).startswith("License ::")
            ],
        },
    )
    return out


def _npm_items(provider: Any, name: str, versions: list[str]) -> list[dict[str, Any]]:
    del versions  # the packument carries every version
    return [{"url": provider.endpoint(name), "kind": "packument"}]


def _npm_repository(value: Any) -> str | None:
    if isinstance(value, dict):
        return value.get("url")
    return value if isinstance(value, str) else None


def _npm_licence(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("license")
    if isinstance(value, str):
        return {"expression": value}
    names = []
    if isinstance(value, dict) and value.get("type"):
        names.append(str(value["type"]))
    for entry in item.get("licenses") or []:
        if isinstance(entry, dict) and entry.get("type"):
            names.append(str(entry["type"]))
        elif isinstance(entry, str):
            names.append(entry)
    return {"names": names}


def _npm_history(
    provider: Any,
    payload: Any,
    *,
    kind: str,
    source_url: str,
    package: str,
    version: str | None,
) -> list[dict[str, Any]]:
    del kind, version
    name = str(payload.get("name") or package)
    times = dict(payload.get("time") or {})
    modified = times.get("modified")
    versions = dict(payload.get("versions") or {})
    out: list[dict[str, Any]] = []

    def emit(record_type: str, **fields: Any) -> None:
        out.append(
            _statement(
                "npm",
                "npm",
                name,
                source_url,
                record_type,
                source_modified_at=modified,
                **fields,
            )
        )

    unpublished = (
        times.get("unpublished") if isinstance(times.get("unpublished"), dict) else None
    )
    stated_gone = {str(v) for v in (unpublished or {}).get("versions") or []}
    timed = {k for k in times if k not in {"created", "modified", "unpublished"}}
    for number in sorted(set(versions) | timed | stated_gone):
        item = dict(versions.get(number) or {})
        if number in versions:
            message = item.get("deprecated")
            state, reason = (
                ("deprecated", str(message))
                if isinstance(message, str) and message
                else ("published", None)
            )
            stated_at = None
        elif number in stated_gone:
            state, reason, stated_at = (
                "unpublished",
                "the package was unpublished (time.unpublished)",
                unpublished.get("time"),
            )
        else:
            state, stated_at = "unpublished", None
            reason = "listed in the registry's time map but no longer in versions (npm's record of an unpublished version)"
        emit(
            "release_state_revision",
            version=number,
            state=state,
            reason=reason,
            published_at=times.get(number),
            state_stated_at=stated_at,
        )
        if number not in versions:
            continue
        optional = dict(item.get("optionalDependencies") or {})
        peer_meta = dict(item.get("peerDependenciesMeta") or {})
        entries = []
        for key, scope in (
            ("dependencies", "runtime"),
            ("devDependencies", "dev"),
            ("peerDependencies", "peer"),
            ("optionalDependencies", "optional"),
        ):
            for dep, constraint in dict(item.get(key) or {}).items():
                if key == "dependencies" and dep in optional:
                    continue  # npm copies optionalDependencies into dependencies (verify)
                entries.append(
                    {
                        "ecosystem": "npm",
                        "name": dep,
                        "constraint": str(constraint),
                        "scope": scope,
                        "source_scope": key,
                        "optional": scope == "optional"
                        or bool(dict(peer_meta.get(dep) or {}).get("optional")),
                    }
                )
        emit("declared_dependency_set", version=number)
        out[-1]["entries"] = entries
        emit("licence_declaration_revision", version=number, raw=_npm_licence(item))
    emit("release_listing", versions=sorted(set(versions) | timed))
    if name.startswith("@") and "/" in name:
        emit(
            "publisher_organisation",
            organisations=[
                {
                    "kind": "npm-scope",
                    "id": name.split("/", 1)[0],
                    "verification": "npm does not state whether a scope belongs to an organisation or a user (verify)",
                }
            ],
        )
    repository = _npm_repository(payload.get("repository"))
    if repository:
        emit(
            "repository_link_assertion",
            links=[{"url": repository, "field": "repository"}],
        )
    return out


def _crates_items(
    provider: Any, name: str, versions: list[str]
) -> list[dict[str, Any]]:
    base = f"{provider.registry_url}/api/v1/crates/{quote(name, safe='')}"
    return [
        {"url": base, "kind": "crate"},
        {"url": f"{base}/owner_team", "kind": "owner_team"},
    ] + [
        {
            "url": f"{base}/{quote(str(v), safe='')}/dependencies",
            "kind": "dependencies",
            "version": str(v),
        }
        for v in versions
    ]


def _crates_history(
    provider: Any,
    payload: Any,
    *,
    kind: str,
    source_url: str,
    package: str,
    version: str | None,
) -> list[dict[str, Any]]:
    crate = dict(payload.get("crate") or {})
    name = str(crate.get("name") or package)
    out: list[dict[str, Any]] = []

    def emit(record_type: str, **fields: Any) -> None:
        out.append(
            _statement("crates-io", "cargo", name, source_url, record_type, **fields)
        )

    if kind == "crate":
        numbers = []
        for item in payload.get("versions") or []:
            number = str(item.get("num"))
            numbers.append(number)
            yanked = bool(item.get("yanked"))
            emit(
                "release_state_revision",
                version=number,
                state="yanked" if yanked else "published",
                reason=item.get("yank_message") if yanked else None,
                published_at=item.get("created_at"),
                source_modified_at=item.get("updated_at"),
            )
            emit(
                "licence_declaration_revision",
                version=number,
                raw={"expression": item.get("license")},
                source_modified_at=item.get("updated_at"),
            )
        emit("release_listing", versions=sorted(numbers))
        if crate.get("repository"):
            emit(
                "repository_link_assertion",
                links=[{"url": crate["repository"], "field": "crate.repository"}],
            )
        return out
    if kind == "owner_team":
        teams = [
            {
                "kind": "crates-team",
                "id": str(t.get("login")),
                "name": t.get("name"),
                "verification": "team of a code-host organisation as stated by crates.io",
            }
            for t in payload.get("teams") or []
            if t.get("kind", "team") == "team" and t.get("login")
        ]
        if teams:
            emit("publisher_organisation", organisations=teams)
        return out
    entries = []
    for item in payload.get("dependencies") or []:
        cargo_kind = str(item.get("kind") or "normal")
        scope = {"normal": "runtime", "dev": "dev", "build": "build"}.get(
            cargo_kind, "runtime"
        )
        if item.get("optional") and scope == "runtime":
            scope = "optional"
        entries.append(
            {
                "ecosystem": "cargo",
                "name": item.get("crate_id"),
                "constraint": item.get("req"),
                "scope": scope,
                "source_scope": cargo_kind,
                "optional": bool(item.get("optional")),
                "target": item.get("target"),
            }
        )
    emit("declared_dependency_set", version=str(version))
    out[-1]["entries"] = entries
    return out


def _maven_items(provider: Any, name: str, versions: list[str]) -> list[dict[str, Any]]:
    group, artifact = name.split(":", 1)
    path = f"https://repo1.maven.org/maven2/{group.replace('.', '/')}/{artifact}"
    return [{"url": provider.endpoint(name), "kind": "search"}] + [
        {"url": f"{path}/{v}/{artifact}-{v}.pom", "kind": "pom", "version": str(v)}
        for v in versions
    ]


def _xml_children(node: Any, tag: str) -> list[Any]:
    return [child for child in list(node) if child.tag.rsplit("}", 1)[-1] == tag]


def _xml_text(node: Any, tag: str) -> str | None:
    found = _xml_children(node, tag) if node is not None else []
    return (found[0].text or "").strip() or None if found else None


def _maven_history(
    provider: Any,
    payload: Any,
    *,
    kind: str,
    source_url: str,
    package: str,
    version: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def emit(record_type: str, **fields: Any) -> None:
        out.append(
            _statement(
                "maven-central", "maven", package, source_url, record_type, **fields
            )
        )

    if kind == "search":
        docs = (payload.get("response") or {}).get("docs") or []
        numbers = []
        for item in docs:
            number = str(item.get("v") or item.get("version"))
            numbers.append(number)
            emit(
                "release_state_revision",
                version=number,
                state="published",
                published_at=item.get("timestamp"),
            )
        emit("release_listing", versions=sorted(numbers))
        emit(
            "publisher_organisation",
            organisations=[
                {
                    "kind": "maven-groupid",
                    "id": package.split(":", 1)[0],
                    "verification": "groupId namespace; Central verifies namespace ownership before publication (verify)",
                }
            ],
        )
        return out
    import xml.etree.ElementTree as ET  # POMs are publisher-supplied; no external entities are resolved

    raw = payload if isinstance(payload, (bytes, str)) else str(payload)
    if (
        isinstance(raw, bytes)
        and b"<!DOCTYPE" in raw
        or isinstance(raw, str)
        and "<!DOCTYPE" in raw
    ):
        raise RegistryError("POM with a DOCTYPE is refused")
    root = ET.fromstring(raw)
    entries = []
    for dependencies in _xml_children(root, "dependencies"):
        for dep in _xml_children(dependencies, "dependency"):
            source_scope = _xml_text(dep, "scope") or "compile"
            optional = (_xml_text(dep, "optional") or "false").lower() == "true"
            scope = {
                "compile": "runtime",
                "runtime": "runtime",
                "test": "dev",
                "provided": "build",
                "system": "build",
                "import": "build",
            }.get(source_scope, "runtime")
            entries.append(
                {
                    "ecosystem": "maven",
                    "name": f"{_xml_text(dep, 'groupId')}:{_xml_text(dep, 'artifactId')}",
                    "constraint": _xml_text(dep, "version"),
                    "scope": "optional" if optional else scope,
                    "source_scope": source_scope,
                    "optional": optional,
                }
            )
    number = str(version or _xml_text(root, "version"))
    emit("declared_dependency_set", version=number)
    out[-1]["entries"] = entries
    names = [
        _xml_text(lic, "name")
        for block in _xml_children(root, "licenses")
        for lic in _xml_children(block, "license")
    ]
    emit(
        "licence_declaration_revision",
        version=number,
        raw={"names": [n for n in names if n]},
    )
    scm = _xml_children(root, "scm")
    url = _xml_text(scm[0], "url") or _xml_text(scm[0], "connection") if scm else None
    if url:
        emit(
            "repository_link_assertion",
            version=number,
            links=[{"url": url, "field": "scm"}],
        )
    organisation = _xml_children(root, "organization")
    org_name = _xml_text(organisation[0], "name") if organisation else None
    if org_name:
        emit(
            "publisher_organisation",
            organisations=[
                {
                    "kind": "pom-organisation",
                    "id": org_name,
                    "name": org_name,
                    "verification": "declared in the POM by the publisher",
                }
            ],
        )
    return out


def registry_history(
    ecosystem: str,
    payload: Any,
    *,
    kind: str,
    source_url: str,
    package: str,
    version: str | None = None,
) -> list[dict[str, Any]]:
    """History statements for one registry document through the ecosystem's provider."""

    return PROVIDERS[canonical_ecosystem(ecosystem)]().history(
        payload, kind=kind, source_url=source_url, package=package, version=version
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

    def history(
        self, ref: SourceRef, payload: Any, *, kind: str, version: str | None = None
    ) -> list[dict[str, Any]]:
        """Release, dependency, licence, publisher and repository statements for one fetched document (#2195)."""

        return registry_history(
            str(ref.metadata["ecosystem"]),
            payload,
            kind=kind,
            source_url=ref.locator,
            package=str(ref.metadata["package"]),
            version=version,
        )


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
    "registry_history",
]
