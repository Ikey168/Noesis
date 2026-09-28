"""Open-source Software Ecosystems records: the history the Technology package objects do not keep (OS02).

``noesis-oss-ecosystem-record-v1`` is the provider-neutral statement every
OSS Ecosystems adapter emits and :class:`src.kb.oss_ecosystem_store.OssEcosystemStore`
keeps as append-only revisions:

* ``release_state_revision`` - one release's state as a source states it
  (``published`` | ``yanked`` | ``deprecated`` | ``unpublished`` | ``restored``,
  or ``not_observed`` when a complete listing no longer names a release the
  source never said was removed), with the reason text verbatim;
* ``declared_dependency_set`` - the dependencies one release declares, as
  written (constraint text, scope, optional flag, environment marker or target);
* ``licence_declaration_revision`` - the licence fields one release declares,
  quoted; the SPDX normalisation (expression, list version, receipt or
  ``unparseable``) is attached per pinned list version by the store;
* ``publisher_organisation`` - organisation-level publisher declarations only
  (PyPI organisation, npm scope as a namespace, Maven ``groupId``, crates.io
  team, a POM ``<organization>``);
* ``repository_link_assertion`` - a repository URL a source asserts for a
  package, sanitised with :func:`src.domains.technical.model.sanitize_repository_url`;
* ``archive_provenance`` - a Software Heritage ``visit`` of an asserted origin
  (date, status, snapshot SWHID) or a ``snapshot`` with its tag branches and
  their target SWHIDs, as the archive states them;
* ``published_dependency_graph`` - a resolved graph *as a source published it*
  (deps.dev), kept apart from Noesis's own declared-constraint resolution;
* ``spdx_list_release`` - one pinned SPDX License List release.

Package and version identity is the Technology model's:
``canonical_package_coordinate`` (PEP 503 names on PyPI, ``group:artifact`` on
Maven), ``package_object_id`` and ``immutable_artifact_id``. No record
re-creates a package or version object. No record has a field for an
individual person: :data:`FORBIDDEN_FIELDS` are rejected anywhere in a
statement. Missing values are absent, never ``"None"``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.domains.technical.model import (
    canonical_package_coordinate,
    immutable_artifact_id,
    package_object_id,
    sanitize_repository_url,
)
from src.kb.oss_ecosystem_versions import normalise_version

CONTRACT = "noesis-oss-ecosystem-record-v1"
ANSWER_CONTRACT = "noesis-oss-ecosystem-answer-v1"
READ_SCOPE = "knowledge:oss:read"
WRITE_SCOPE = "knowledge:oss:write"
REVIEW_SCOPE = "knowledge:oss:review"
ECOSYSTEMS = ("pypi", "npm", "cargo", "maven")
REGISTRY_SOURCES = {
    "pypi": "pypi",
    "npm": "npm",
    "cargo": "crates-io",
    "maven": "maven-central",
}
SOURCES = (
    "pypi",
    "npm",
    "crates-io",
    "maven-central",
    "deps-dev",
    "spdx",
    "software-heritage",
)
RECORD_TYPES = (
    "release_state_revision",
    "declared_dependency_set",
    "licence_declaration_revision",
    "publisher_organisation",
    "repository_link_assertion",
    "archive_provenance",
    "published_dependency_graph",
    "spdx_list_release",
)
# Applied by the store, never kept as a record of its own: a complete release listing.
LISTING = "release_listing"
PER_RELEASE = frozenset(
    {
        "release_state_revision",
        "declared_dependency_set",
        "licence_declaration_revision",
        "published_dependency_graph",
    }
)
RELEASE_STATES = (
    "published",
    "yanked",
    "deprecated",
    "unpublished",
    "restored",
    "not_observed",
)
SCOPES = ("runtime", "dev", "optional", "peer", "build")
ORGANISATION_KINDS = (
    "pypi-organisation",
    "npm-scope",
    "maven-groupid",
    "crates-team",
    "pom-organisation",
)
LICENCE_FIELDS = ("expression", "text", "classifiers", "names")
# Fields that could hold an individual's identity or activity; rejected anywhere in a statement.
FORBIDDEN_FIELDS = frozenset(
    {
        "author",
        "authors",
        "author_email",
        "maintainer",
        "maintainers",
        "maintainer_email",
        "contributor",
        "contributors",
        "committer",
        "committers",
        "email",
        "emails",
        "user",
        "users",
        "username",
        "login",
        "published_by",
        "owner_user",
        "owners",
        "developers",
        "roles",
        "_npmuser",
        "npmuser",
        "person",
        "people",
        "name_of_person",
        "commit_author",
        "activity",
    }
)
_INSTANT = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_SWHID = re.compile(r"^swh:1:(?:snp|rel|rev|dir|cnt):[0-9a-f]{40}$")
SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema"
SCHEMA_NAMES = ("noesis-oss-ecosystem-record-v1", "noesis-oss-ecosystem-answer-v1")


class OssRecordError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message: str, code: str = "invalid_oss_record") -> None:
    raise OssRecordError(code, message)


def iso_instant(value: Any, field: str) -> str | None:
    """A source date as a UTC ISO-8601 instant (dates are UTC midnight); ``None`` when absent."""

    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(float(value) / 1000, tz=UTC)
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{14}", text):  # Maven lastUpdated (yyyyMMddHHmmss, UTC)
            moment = datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        else:
            if not _INSTANT.fullmatch(text):
                _fail(f"{field} must be an ISO-8601 date or instant")
            text = text.replace(" ", "T").replace("Z", "+00:00")
            if len(text) == 10:
                text += "T00:00:00+00:00"
            moment = datetime.fromisoformat(text)
            if moment.tzinfo is None:
                moment = moment.replace(
                    tzinfo=UTC
                )  # registries publish UTC without an offset (verify)
    moment = moment.astimezone(UTC)
    return (
        moment.strftime("%Y-%m-%dT%H:%M:%S")
        + (f".{moment.microsecond // 1000:03d}" if moment.microsecond else "")
        + "Z"
    )


def ms(value: str | None) -> int | None:
    if value is None:
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def check_no_people(value: Any, path: str = "statement") -> None:
    """Reject any key that could carry an individual's identity."""

    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_FIELDS:
                _fail(
                    f"{path}.{key} could hold an individual's identity and is never stored",
                    "personal_field",
                )
            check_no_people(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            check_no_people(item, f"{path}[{index}]")


def repository_key(url: str) -> str:
    """A source-independent repository identity: host and path, no scheme, credentials, ``.git`` or case on hosts that ignore it."""

    text = str(url).strip()
    text = (
        re.sub(r"^(?:scm:)?(?:git|svn|hg):", "", text)
        if text.startswith("scm:")
        else text
    )
    text = re.sub(r"^git\+", "", text)
    shorthand = re.fullmatch(r"(github|gitlab|bitbucket):([\w.-]+/[\w.-]+)", text)
    if shorthand:
        text = f"https://{shorthand[1]}.{'org' if shorthand[1] == 'bitbucket' else 'com'}/{shorthand[2]}"
    elif re.fullmatch(r"[\w.-]+/[\w.-]+", text):
        text = f"https://github.com/{text}"  # npm's owner/repo shorthand means GitHub (verify)
    ssh = re.fullmatch(r"(?:ssh://)?git@([\w.-]+)[:/](.+)", text)
    if ssh:
        text = f"https://{ssh[1]}/{ssh[2]}"
    if "://" not in text:
        text = "https://" + text
    text = sanitize_repository_url(text)
    match = re.fullmatch(r"[a-z+]+://([^/?#]+)(/[^?#]*)?(?:\?.*)?", text, re.IGNORECASE)
    if match is None:
        _fail(f"{url!r} is not a repository URL")
    host = match[1].casefold()
    path = re.sub(r"\.git$", "", (match[2] or "").rstrip("/"))
    if host in {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}:
        path = (
            path.casefold()
        )  # these forges resolve owner/repository case-insensitively (verify)
        path = "/".join(
            path.split("/")[:3]
        )  # owner/repository; deeper paths (tree/...) are not the repository
    return f"{host}{path}"


def coordinate(ecosystem: str, name: str) -> str:
    if ecosystem not in ECOSYSTEMS:
        _fail(f"ecosystem must be one of {ECOSYSTEMS}")
    try:
        return canonical_package_coordinate(ecosystem, name)
    except ValueError as exc:
        raise OssRecordError("invalid_coordinate", str(exc)) from exc


def _text(
    value: Any, field: str, *, optional: bool = False, limit: int = 4000
) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value


def _entries(values: Any, ecosystem: str) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        _fail("entries must be a list")
    result = []
    for item in values:
        if not isinstance(item, dict):
            _fail("each dependency entry is an object")
        scope = item.get("scope")
        if scope not in SCOPES:
            _fail(f"dependency scope must be one of {SCOPES}")
        dep_ecosystem = item.get("ecosystem") or ecosystem
        entry = {
            "ecosystem": dep_ecosystem,
            "name": _text(item.get("name"), "dependency name"),
            "scope": scope,
            "optional": bool(item.get("optional")),
        }
        try:
            entry["coordinate"] = canonical_package_coordinate(
                dep_ecosystem, entry["name"]
            )
        except ValueError:
            entry["unsupported"] = (
                "the dependency's ecosystem or name has no canonical coordinate"
            )
        for key in (
            "constraint",
            "source_scope",
            "marker",
            "target",
            "requirement",
            "extra",
        ):
            if item.get(key) not in (None, ""):
                entry[key] = str(item[key])
        if item.get("extras"):
            entry["extras"] = sorted(str(e) for e in item["extras"])
        result.append(entry)
    return sorted(result, key=canonical)


def validate(statement: dict[str, Any]) -> dict[str, Any]:
    """A validated statement with identity fields (coordinate, object ids) filled in."""

    if not isinstance(statement, dict):
        _fail("a statement is an object")
    check_no_people(statement)
    record_type = statement.get("record_type")
    if record_type not in (*RECORD_TYPES, LISTING):
        _fail(f"record_type must be one of {RECORD_TYPES}")
    source = statement.get("source")
    if source not in SOURCES:
        _fail(f"source must be one of {SOURCES}")
    value = {k: v for k, v in statement.items() if v is not None}
    for key in (
        "source_modified_at",
        "published_at",
        "state_stated_at",
        "visit_date",
        "release_date",
    ):
        if key in value:
            value[key] = iso_instant(value[key], key)
            if value[key] is None:
                del value[key]
    if record_type == "spdx_list_release":
        _text(value.get("list_version"), "list_version", limit=40)
        for key in ("licences", "exceptions"):
            if not isinstance(value.get(key), list):
                _fail(f"{key} must be a list")
        return value
    if record_type == "archive_provenance":
        _text(value.get("origin_url"), "origin_url")
        value["origin_url"] = sanitize_repository_url(value["origin_url"])
        value["repository_key"] = repository_key(value["origin_url"])
        if type(value.get("visit")) is not int or value["visit"] < 1:
            _fail("visit must be a positive integer")
        if value.get("snapshot_swhid") and not _SWHID.fullmatch(
            value["snapshot_swhid"]
        ):
            _fail("snapshot_swhid must be a snapshot SWHID")
        for branch in value.get("branches") or []:
            if branch.get("target_swhid") and not _SWHID.fullmatch(
                branch["target_swhid"]
            ):
                _fail("branch targets must be SWHIDs")
        if value.get("detail") not in {"visit", "snapshot"}:
            _fail("archive provenance is a visit or a snapshot")
        if value["detail"] == "snapshot":
            if not value.get("snapshot_swhid"):
                _fail("a snapshot record names its snapshot SWHID")
            value["branches"] = sorted(
                value.get("branches") or [], key=lambda b: b["name"]
            )
        elif "branches" in value:
            _fail("visit records carry no branches; snapshots do")
        return value
    ecosystem = value.get("ecosystem")
    value["package"] = _text(value.get("package"), "package", limit=400)
    value["coordinate"] = coordinate(ecosystem, value["package"])
    value["package_object_id"] = package_object_id(value["coordinate"])
    if record_type == LISTING:
        if not isinstance(value.get("versions"), list):
            _fail("a release listing names its versions")
        value["versions"] = sorted(
            {normalise_version(ecosystem, str(v)) for v in value["versions"]}
        )
        return value
    if record_type in PER_RELEASE or (
        record_type == "repository_link_assertion" and value.get("version")
    ):
        value["version"] = normalise_version(
            ecosystem, _text(value.get("version"), "version", limit=200)
        )
        value["artifact_id"] = immutable_artifact_id(
            value["coordinate"], value["version"]
        )
    if record_type == "release_state_revision":
        if value.get("state") not in RELEASE_STATES:
            _fail(f"state must be one of {RELEASE_STATES}")
        if "reason" in value:
            _text(value["reason"], "reason", limit=4000)
    elif record_type == "declared_dependency_set":
        value["entries"] = _entries(value.get("entries"), ecosystem)
    elif record_type == "licence_declaration_revision":
        raw = value.get("raw")
        if not isinstance(raw, dict) or set(raw) - set(LICENCE_FIELDS):
            _fail(f"raw holds only the licence fields {LICENCE_FIELDS}")
        clean: dict[str, Any] = {}
        for key in LICENCE_FIELDS:
            item = raw.get(key)
            if isinstance(item, list):
                item = [str(v) for v in item if str(v).strip()]
            elif isinstance(item, str):
                item = item if item.strip() else None
            if item:
                clean[key] = item
        value["raw"] = clean
    elif record_type == "publisher_organisation":
        organisations = []
        for item in value.get("organisations") or []:
            if item.get("kind") not in ORGANISATION_KINDS:
                _fail(f"organisation kind must be one of {ORGANISATION_KINDS}")
            entry = {
                "kind": item["kind"],
                "id": _text(item.get("id"), "organisation id", limit=400),
                "verification": str(
                    item.get("verification") or "not stated by the source"
                ),
            }
            if item.get("name"):
                entry["name"] = str(item["name"])
            organisations.append(entry)
        value["organisations"] = sorted(organisations, key=canonical)
    elif record_type == "repository_link_assertion":
        links = []
        for item in value.get("links") or []:
            url = sanitize_repository_url(_text(item.get("url"), "repository url"))
            try:
                key = repository_key(url)
            except OssRecordError:
                continue  # not a repository locator (e.g. a homepage); never guessed
            links.append(
                {
                    "url": url,
                    "repository_key": key,
                    "field": str(item.get("field") or "repository"),
                    "relation": str(item.get("relation") or "source-repository"),
                }
            )
        value["links"] = sorted(links, key=canonical)
    elif record_type == "published_dependency_graph":
        if not isinstance(value.get("nodes"), list) or not isinstance(
            value.get("edges"), list
        ):
            _fail("a published graph has nodes and edges")
        value["semantics"] = str(
            value.get("semantics") or "the source's own resolution, as published"
        )
    return value


def record_key(value: dict[str, Any]) -> list[Any]:
    record_type = value["record_type"]
    if record_type == "spdx_list_release":
        return [record_type, "spdx", value["list_version"]]
    if record_type == "archive_provenance":
        if value["detail"] == "snapshot":
            # Snapshots are content-addressed: a later visit reaching the same snapshot changes nothing.
            return [
                record_type,
                value["source"],
                value["repository_key"],
                "snapshot",
                value["snapshot_swhid"],
            ]
        return [
            record_type,
            value["source"],
            value["repository_key"],
            "visit",
            value["visit"],
        ]
    key = [record_type, value["source"], value["coordinate"]]
    if record_type == "publisher_organisation":
        # One record per kind of declaration (a Maven groupId and a POM <organization> are two statements).
        key.append(
            "+".join(sorted({o["kind"] for o in value.get("organisations") or []}))
        )
    if record_type in PER_RELEASE or record_type == "repository_link_assertion":
        key.append(value.get("version") or "")
    return key


# Fields that are identity or observation bookkeeping, not content: excluded from the content hash.
IDENTITY_FIELDS = frozenset(
    {
        "record_type",
        "source",
        "ecosystem",
        "package",
        "coordinate",
        "package_object_id",
        "version",
        "artifact_id",
        "source_url",
        "source_modified_at",
        "origin_url",
        "repository_key",
        "visit",
        "list_version",
    }
)


def content_of(value: dict[str, Any]) -> dict[str, Any]:
    """The comparable content of a statement (what a revision changes)."""

    return {k: v for k, v in value.items() if k not in IDENTITY_FIELDS}


# ----------------------------------------------------------------- schemas


def schema_definitions(root: Path | None = None) -> dict[str, dict[str, Any]]:
    base = root or SCHEMA_ROOT
    return {
        name: json.loads((base / f"{name}.json").read_text()) for name in SCHEMA_NAMES
    }


def register_schemas(
    conn: Any, *, principal_id: str, scopes: set[str], root: Path | None = None
) -> list[dict[str, Any]]:
    """Register the pack's contracts in the existing schema registry (idempotent per version)."""

    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1",
            "name": name,
            "kind": "schema",
            "semantic_version": "1.0.0",
            "content": content,
            "owner": "oss.registries",
            "dependencies": [],
            "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/oss-ecosystems"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(
            registry.register(
                definition,
                f"oss-schema:{name}:1.0.0:{digest(content)[:16]}",
                principal_id=principal_id,
                scopes=scopes,
            )
        )
    return results


__all__ = [
    "ANSWER_CONTRACT",
    "CONTRACT",
    "ECOSYSTEMS",
    "FORBIDDEN_FIELDS",
    "LISTING",
    "ORGANISATION_KINDS",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REGISTRY_SOURCES",
    "RELEASE_STATES",
    "REVIEW_SCOPE",
    "SCOPES",
    "SOURCES",
    "WRITE_SCOPE",
    "OssRecordError",
    "canonical",
    "check_no_people",
    "content_of",
    "coordinate",
    "digest",
    "iso_instant",
    "ms",
    "record_key",
    "register_schemas",
    "repository_key",
    "schema_definitions",
    "validate",
]
