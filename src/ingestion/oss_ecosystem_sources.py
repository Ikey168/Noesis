"""OSS Ecosystems acquisition: registries, deps.dev, SPDX License List releases and Software Heritage (OS03-OS05).

One runtime connector (``oss-ecosystem``) reads one bounded, declared
selection per source under :mod:`src.ingestion.source_pack_runtime` and emits
``noesis-oss-ecosystem-record-v1`` statements for
:class:`src.kb.oss_ecosystem_store.OssEcosystemProjector`:

* **Registries** go through the existing providers in
  :mod:`src.domains.technical.registries` (their endpoints and their
  ``history`` parsers); this module adds no registry client, only the paging.
* **deps.dev** (optional feature ``oss_deps_dev``) is a second, attributed
  source: its versions, licences, related source repositories and the graph it
  resolves are kept side by side with the registries', never merged. Advisory
  keys and attestations are dropped.
* **SPDX License List** pinned releases (licences and exceptions with
  deprecation flags) so each normalisation cites the list version it used.
* **Software Heritage** (optional feature ``oss_software_heritage``): visits
  and snapshot tag branches for origins that a repository link assertion names
  (the projector refuses any other origin). Release and revision objects,
  which carry author and committer identities, are never fetched. Rate-limit
  headers are honoured and an optional token comes from the secret store.

The transport is the runtime's (same-host redirects, byte ceiling, timeout) or,
for an explicitly budgeted acquisition,
:func:`durable_transport` over :class:`src.ingestion.provider_execution.DurableHTTP`.
Fixtures replay authored responses through the same adapter.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.oss_ecosystem_records import canonical, check_no_people

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-oss-ecosystem-record-v1"
CONNECTOR = "oss-ecosystem"
FIXTURE_SECRET = None
REGISTRY_FORMATS = {
    "pypi-json": "pypi",
    "npm-packument": "npm",
    "crates-io": "cargo",
    "maven-central": "maven",
}
FORMATS = (*REGISTRY_FORMATS, "deps-dev", "spdx-license-list", "software-heritage")
DEPS_DEV_SYSTEMS = {"pypi": "PYPI", "npm": "NPM", "cargo": "CARGO", "maven": "MAVEN"}
MAX_PACKAGES = 50
MAX_VERSIONS = 50
MAX_ORIGINS = 20
MAX_SWH_VISITS = 20
MAX_SWH_BRANCHES = 1000
PACING_S = {
    "crates-io": 1.0
}  # crates.io crawler policy: at most one request per second (verify)
USER_AGENT = "Noesis/oss-ecosystems (+https://github.com/Ikey168/Noesis)"
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "pypi": {
        "decision": "implement",
        "format": "pypi-json",
        "auth": "none",
        "endpoints": [
            "https://pypi.org/pypi/{project}/json",
            "https://pypi.org/pypi/{project}/{version}/json",
        ],
        "terms": "PyPI Terms of Service (verify)",
        "history": "per-file yanked and yanked_reason (PEP 592)",
        "dropped_fields": [
            "info.author",
            "info.author_email",
            "info.maintainer",
            "info.maintainer_email",
            "ownership.roles",
            "vulnerabilities",
        ],
    },
    "pypi-simple": {
        "decision": "link-only",
        "reason": "the JSON API carries the same yank flags (verify)",
    },
    "pypi-bigquery": {
        "decision": "out of scope",
        "reason": "download statistics are popularity signals; billed",
    },
    "npm": {
        "decision": "implement",
        "format": "npm-packument",
        "auth": "none",
        "endpoints": ["https://registry.npmjs.org/{name}"],
        "terms": "npm Terms of Use (verify)",
        "history": "time map, deprecated messages, time.unpublished",
        "dropped_fields": ["maintainers", "author", "contributors", "_npmUser"],
    },
    "crates-io": {
        "decision": "implement",
        "format": "crates-io",
        "auth": "none",
        "endpoints": [
            "https://crates.io/api/v1/crates/{name}",
            "https://crates.io/api/v1/crates/{name}/{version}/dependencies",
            "https://crates.io/api/v1/crates/{name}/owner_team",
        ],
        "terms": "crates.io data access policy; 1 request/second and a contact User-Agent (verify)",
        "history": "yanked with yank_message, updated_at",
        "dropped_fields": ["published_by", "audit_actions", "owner_user endpoint"],
    },
    "maven-central": {
        "decision": "implement",
        "format": "maven-central",
        "auth": "none",
        "endpoints": [
            "https://search.maven.org/solrsearch/select?core=gav",
            "https://repo1.maven.org/maven2/{group}/{artifact}/{version}/{artifact}-{version}.pom",
        ],
        "terms": "Sonatype Central terms (verify)",
        "history": "no yank, deprecation or unpublish exists on Maven Central (verify)",
        "dropped_fields": ["developers", "contributors"],
    },
    "deps-dev": {
        "decision": "implement",
        "feature": "oss_deps_dev",
        "format": "deps-dev",
        "auth": "none",
        "endpoints": [
            "https://api.deps.dev/v3/systems/{system}/packages/{name}",
            "https://api.deps.dev/v3/systems/{system}/packages/{name}/versions/{version}",
            "https://api.deps.dev/v3/systems/{system}/packages/{name}/versions/{version}:dependencies",
        ],
        "terms": "CC-BY 4.0, attribution 'Open Source Insights (deps.dev)' (verify)",
        "semantics": "deps.dev resolves graphs with its own resolver; kept as a published graph",
        "dropped_fields": ["advisoryKeys", "slsaProvenances", "attestations"],
    },
    "spdx": {
        "decision": "implement",
        "format": "spdx-license-list",
        "auth": "none",
        "endpoints": [
            "https://raw.githubusercontent.com/spdx/license-list-data/v{version}/json/licenses.json",
            "https://raw.githubusercontent.com/spdx/license-list-data/v{version}/json/exceptions.json",
        ],
        "terms": "list data CC0-1.0 (verify)",
    },
    "software-heritage": {
        "decision": "implement",
        "feature": "oss_software_heritage",
        "format": "software-heritage",
        "auth": "optional bearer token (NOESIS_SWH_TOKEN)",
        "endpoints": [
            "https://archive.softwareheritage.org/api/1/origin/{origin}/visits/",
            "https://archive.softwareheritage.org/api/1/snapshot/{id}/",
        ],
        "terms": "Software Heritage terms of use (verify)",
        "limits": "X-RateLimit-* headers honoured (verify)",
        "dropped_fields": [
            "release and revision objects (authors, committers, messages)"
        ],
    },
    "libraries-io": {
        "decision": "not implemented",
        "reason": "duplicates registries and deps.dev; CC BY-SA 4.0 share-alike (verify); "
        "contributor and popularity fields are excluded by the pack's non-goals",
    },
}
LIVE_VERIFICATION = {
    name: {"status": "unverified-live", "issue": "#2205"}
    for name in PROVIDER_CONTRACTS
    if PROVIDER_CONTRACTS[name]["decision"] == "implement"
}


def _registry(ecosystem: str) -> Any:
    from src.domains.technical.registries import PROVIDERS

    return PROVIDERS[ecosystem]()


def oss_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("oss") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS:
        raise SourcePackError(
            "invalid_mapping", f"OSS sources declare a format in {FORMATS}"
        )
    selection = dict(declared.get("selection") or {})
    if fmt in REGISTRY_FORMATS or fmt == "deps-dev":
        packages = selection.get("packages")
        if not isinstance(packages, list) or not 0 < len(packages) <= MAX_PACKAGES:
            raise SourcePackError(
                "unbounded_source",
                f"OSS registry sources pin 1-{MAX_PACKAGES} packages",
            )
        for item in packages:
            versions = item.get("versions") or []
            if (
                not item.get("name")
                or not isinstance(versions, list)
                or len(versions) > MAX_VERSIONS
            ):
                raise SourcePackError(
                    "unbounded_source",
                    f"each package names at most {MAX_VERSIONS} versions",
                )
        if fmt == "deps-dev" and selection.get("ecosystem") not in DEPS_DEV_SYSTEMS:
            raise SourcePackError(
                "invalid_mapping", "deps.dev sources name one ecosystem"
            )
    elif fmt == "spdx-license-list":
        if not selection.get("list_versions"):
            raise SourcePackError("unbounded_source", "SPDX sources pin list versions")
    elif (
        not isinstance(selection.get("origins", []), list)
        or len(selection.get("origins", [])) > MAX_ORIGINS
    ):
        raise SourcePackError(
            "unbounded_source",
            f"Software Heritage runs name at most {MAX_ORIGINS} origins",
        )
    return {**declared, "selection": selection}


def _origin_ok(url: Any) -> bool:
    parts = urlsplit(str(url))
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and not parts.username
        and not parts.fragment
    )


def _statement_id(source_id: str, statement: Mapping[str, Any]) -> str:
    key = [
        statement["record_type"],
        statement.get("package")
        or statement.get("origin_url")
        or statement.get("list_version"),
        statement.get("version") or "",
        statement.get("detail") or "",
        statement.get("visit") or statement.get("snapshot_swhid") or "",
    ]
    return f"{source_id}:" + hashlib.sha256(canonical(key).encode()).hexdigest()[:24]


class OssEcosystemAdapter:
    """Fetch one declared item (a package, a list version or an origin) per page and emit its statements."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = oss_declaration(self.source)
        live = transport is None
        if live:
            from functools import partial

            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        self.secret = secret
        self.sleep = sleep or (time.sleep if live else (lambda _s: None))
        self.clock = clock or time.monotonic
        self.format = self.declared["format"]
        label = REGISTRY_FORMATS.get(self.format)
        self.label = (
            _registry(label).history_source
            if label
            else {
                "deps-dev": "deps-dev",
                "spdx-license-list": "spdx",
                "software-heritage": "software-heritage",
            }[self.format]
        )
        self.pacing_s = PACING_S.get(self.label, 0.0)
        self._last: float | None = None
        self._rate_blocked_ms: int | None = None
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "oss": {
                "format": self.format,
                "source": self.label,
                "pacing_s": self.pacing_s,
                "keyed": bool(secret),
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    # -------------------------------------------------------------- planning

    def _selection(self, request: Mapping[str, Any]) -> dict[str, Any]:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if request.get("from_ms") is not None or request.get("to_ms") is not None:
            raise SourcePackError(
                "parameter_forbidden", "OSS sources take no time window"
            )
        selection = dict(self.declared["selection"])
        parameters = dict(request.get("parameters") or {})
        if self.format == "software-heritage":
            if set(parameters) - {"origins"}:
                raise SourcePackError(
                    "parameter_forbidden", "Software Heritage runs accept only origins"
                )
            if "origins" in parameters:
                selection["origins"] = parameters["origins"]
            origins = selection.get("origins") or []
            if not 0 < len(origins) <= MAX_ORIGINS or not all(
                _origin_ok(o) for o in origins
            ):
                raise SourcePackError(
                    "parameter_forbidden", f"name 1-{MAX_ORIGINS} https origins"
                )
            return selection
        if set(parameters) - {"packages"}:
            raise SourcePackError(
                "parameter_forbidden", "OSS runs accept only a narrower package list"
            )
        if "packages" in parameters:
            declared = {p["name"]: p for p in selection.get("packages") or []}
            wanted = parameters["packages"]
            if (
                not isinstance(wanted, list)
                or not wanted
                or any(n not in declared for n in wanted)
            ):
                raise SourcePackError(
                    "parameter_forbidden", "runs narrow to packages the source declares"
                )
            selection["packages"] = [declared[n] for n in wanted]
        return selection

    def _items(self, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
        endpoint = self.source["endpoint"].rstrip("/")
        if self.format in REGISTRY_FORMATS:
            provider = _registry(REGISTRY_FORMATS[self.format])
            return [
                {
                    "package": p["name"],
                    "requests": provider.history_items(
                        p["name"], list(p.get("versions") or [])
                    ),
                }
                for p in selection["packages"]
            ]
        if self.format == "deps-dev":
            system = DEPS_DEV_SYSTEMS[selection["ecosystem"]]
            items = []
            for package in selection["packages"]:
                base = f"{endpoint}/systems/{system}/packages/{quote(package['name'], safe='')}"
                requests = [{"url": base, "kind": "package"}]
                for version in package.get("versions") or []:
                    requests += [
                        {
                            "url": f"{base}/versions/{quote(version, safe='')}",
                            "kind": "version",
                            "version": version,
                        },
                        {
                            "url": f"{base}/versions/{quote(version, safe='')}:dependencies",
                            "kind": "dependencies",
                            "version": version,
                        },
                    ]
                items.append({"package": package["name"], "requests": requests})
            return items
        if self.format == "spdx-license-list":
            return [
                {
                    "list_version": v,
                    "requests": [
                        {
                            "url": f"{endpoint}/v{v}/json/licenses.json",
                            "kind": "licences",
                        },
                        {
                            "url": f"{endpoint}/v{v}/json/exceptions.json",
                            "kind": "exceptions",
                        },
                    ],
                }
                for v in selection["list_versions"]
            ]
        # The origin URL is embedded as-is in the API path (verify).
        return [
            {
                "origin": o,
                "requests": [
                    {
                        "url": f"{endpoint}/origin/{quote(o, safe=':/')}/visits/",
                        "kind": "visits",
                        "params": {"per_page": MAX_SWH_VISITS},
                    }
                ],
            }
            for o in selection["origins"]
        ]

    # -------------------------------------------------------------- fetching

    def _pace(self) -> None:
        if self.pacing_s and self._last is not None:
            wait = self.pacing_s - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        self._last = self.clock()

    def _rate_headers(self, headers: Mapping[str, Any]) -> None:
        remaining = headers.get("x-ratelimit-remaining")
        if remaining is None or str(remaining).strip() != "0":
            return
        reset, date = headers.get("x-ratelimit-reset"), headers.get("date")
        wait = 60_000
        try:
            if reset is not None and date is not None:
                wait = max(
                    0,
                    int(float(reset)) * 1000
                    - int(
                        email.utils.parsedate_to_datetime(str(date)).timestamp() * 1000
                    ),
                )
        except (TypeError, ValueError, OverflowError):
            wait = 60_000
        self._rate_blocked_ms = wait

    def _get(self, url: str, params: Mapping[str, Any] | None = None) -> bytes:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        if self._rate_blocked_ms is not None:
            raise SourcePackError(
                "rate_limited",
                "the source's rate limit is exhausted",
                retry_after_ms=self._rate_blocked_ms,
            )
        host = (urlsplit(url).hostname or "").casefold()
        headers = {
            "Accept": "application/json, application/xml;q=0.8",
            "User-Agent": USER_AGENT,
        }
        if self.secret and self.format == "software-heritage":
            headers["Authorization"] = f"Bearer {self.secret}"
        self._pace()
        response = self.transport(
            url=url,
            params=dict(params or {}),
            headers=headers,
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "OSS source was served from another host"
            )
        status = int(response.get("status", 200))
        response_headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "source response exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(
                    response_headers.get("retry-after") or 60
                ),
            )
        self._rate_headers(response_headers)
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"source refused the request (HTTP {status})"
            )
        if status == 404:
            raise SourcePackError(
                "source_unavailable",
                "the source does not publish the requested document",
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"source returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError("schema_drift", f"source returned HTTP {status}")
        return raw

    @staticmethod
    def _json(raw: bytes) -> Any:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not JSON") from exc

    def _statements(
        self, item: Mapping[str, Any]
    ) -> tuple[list[dict[str, Any]], list[str], int]:
        statements: list[dict[str, Any]] = []
        digests: list[str] = []
        missing: list[str] = []
        size = 0
        if self.format in REGISTRY_FORMATS:
            provider = _registry(REGISTRY_FORMATS[self.format])
            for request in item["requests"]:
                try:
                    raw = self._get(request["url"])
                except SourcePackError as exc:
                    if exc.code != "source_unavailable" or "version" not in request:
                        raise
                    missing.append(
                        request["url"]
                    )  # a listed version the registry does not (yet) publish
                    continue
                size += len(raw)
                digests.append(hashlib.sha256(raw).hexdigest())
                payload = raw if request["kind"] == "pom" else self._json(raw)
                try:
                    statements += provider.history(
                        payload,
                        kind=request["kind"],
                        source_url=request["url"],
                        package=item["package"],
                        version=request.get("version"),
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    raise SourcePackError(
                        "schema_drift", f"{request['kind']} document: {exc}"
                    ) from exc
        elif self.format == "deps-dev":
            for request in item["requests"]:
                try:
                    raw = self._get(request["url"])
                except SourcePackError as exc:
                    if exc.code != "source_unavailable" or "version" not in request:
                        raise
                    missing.append(request["url"])
                    continue
                size += len(raw)
                digests.append(hashlib.sha256(raw).hexdigest())
                statements += parse_deps_dev(
                    self._json(raw),
                    kind=request["kind"],
                    source_url=request["url"],
                    ecosystem=self.declared["selection"]["ecosystem"],
                    package=item["package"],
                    version=request.get("version"),
                )
        elif self.format == "spdx-license-list":
            documents = {}
            for request in item["requests"]:
                raw = self._get(request["url"])
                size += len(raw)
                digests.append(hashlib.sha256(raw).hexdigest())
                documents[request["kind"]] = self._json(raw)
            statements.append(
                parse_spdx_list(
                    documents["licences"],
                    documents["exceptions"],
                    expected=item["list_version"],
                )
            )
        else:
            raw = self._get(
                item["requests"][0]["url"], item["requests"][0].get("params")
            )
            size += len(raw)
            digests.append(hashlib.sha256(raw).hexdigest())
            visits = parse_swh_visits(self._json(raw), origin=item["origin"])
            statements += visits
            latest = next(
                (
                    v
                    for v in sorted(visits, key=lambda v: -v["visit"])
                    if v.get("snapshot_swhid") and v.get("visit_status") == "full"
                ),
                None,
            )
            if latest is not None:
                snapshot_id = latest["snapshot_swhid"].rsplit(":", 1)[1]
                url = f"{self.source['endpoint'].rstrip('/')}/snapshot/{snapshot_id}/"
                raw = self._get(
                    url,
                    {
                        "branches_count": MAX_SWH_BRANCHES,
                        "target_types": "release,revision",
                    },
                )
                size += len(raw)
                digests.append(hashlib.sha256(raw).hexdigest())
                statements.append(
                    parse_swh_snapshot(
                        self._json(raw), origin=item["origin"], visit=latest["visit"]
                    )
                )
        for statement in statements:
            check_no_people(statement)
        self._missing = missing
        return statements, digests, size

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        selection = self._selection(request)
        items = self._items(selection)
        try:
            index = int(cursor or "0")
        except ValueError as exc:
            raise SourcePackError("cursor_drift", "unrecognised OSS cursor") from exc
        if not 0 <= index < len(items):
            raise SourcePackError(
                "cursor_drift", "cursor is outside the declared selection"
            )
        statements, digests, size = self._statements(items[index])
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            raise SourcePackError(
                "budget_exhausted", "page has more records than the run's result budget"
            )
        next_cursor = str(index + 1) if index + 1 < len(items) else None
        records = [self._record(s) for s in statements]
        receipt = {
            "status": 200,
            "format": self.format,
            "item": index,
            "responses_sha256": digests,
            "records": len(records),
            "final_page": next_cursor is None,
            "not_published": list(self._missing),
        }
        return RuntimePage(tuple(records), next_cursor, size, receipt=receipt)

    def _record(self, statement: Mapping[str, Any]) -> dict[str, Any]:
        title = " ".join(
            str(statement.get(k))
            for k in ("package", "version", "origin_url", "list_version")
            if statement.get(k)
        )
        return {
            "id": _statement_id(self.source["source_id"], statement),
            "title": f"{self.label} {statement['record_type']} {title}".strip(),
            "url": statement.get("source_url")
            or statement.get("origin_url")
            or self.source["endpoint"],
            "language": "en",
            "content": canonical(dict(statement)),
            "oss_record": dict(statement),
        }


ADAPTERS = {CONNECTOR: OssEcosystemAdapter}


# ------------------------------------------------------------------ parsers


def parse_deps_dev(
    payload: Mapping[str, Any],
    *,
    kind: str,
    source_url: str,
    ecosystem: str,
    package: str,
    version: str | None,
) -> list[dict[str, Any]]:
    """deps.dev v3 documents as statements from the ``deps-dev`` source (its own view, never merged)."""

    base = {
        "source": "deps-dev",
        "ecosystem": ecosystem,
        "package": package,
        "source_url": source_url,
    }
    if kind == "package":
        out = []
        for item in payload.get("versions") or []:
            number = dict(item.get("versionKey") or {}).get("version")
            if number:
                body = {
                    **base,
                    "record_type": "release_state_revision",
                    "version": number,
                    "state": "published",
                }
                if item.get("publishedAt"):
                    body["published_at"] = item["publishedAt"]
                out.append(body)
        return out
    if kind == "version":
        out = []
        licences = [str(v) for v in payload.get("licenses") or [] if str(v).strip()]
        raw = {"expression": licences[0]} if len(licences) == 1 else {"names": licences}
        out.append(
            {
                **base,
                "record_type": "licence_declaration_revision",
                "version": version,
                "raw": raw,
            }
        )
        links = [
            {
                "url": "https://" + dict(p.get("projectKey") or {}).get("id", ""),
                "field": "relatedProjects",
                "relation": "source-repository",
            }
            for p in payload.get("relatedProjects") or []
            if p.get("relationType") == "SOURCE_REPO"
            and dict(p.get("projectKey") or {}).get("id")
        ]
        if links:
            out.append(
                {
                    **base,
                    "record_type": "repository_link_assertion",
                    "version": version,
                    "links": links,
                }
            )
        return out
    nodes = []
    for node in payload.get("nodes") or []:
        key = dict(node.get("versionKey") or {})
        nodes.append(
            {
                "name": key.get("name"),
                "version": key.get("version"),
                "relation": node.get("relation"),
                "bundled": bool(node.get("bundled")),
                **(
                    {"errors": [str(e) for e in node["errors"]]}
                    if node.get("errors")
                    else {}
                ),
            }
        )
    edges = [
        {
            "from": int(e.get("fromNode", 0)),
            "to": int(e.get("toNode", 0)),
            "requirement": str(e.get("requirement") or ""),
        }
        for e in payload.get("edges") or []
    ]
    body = {
        **base,
        "record_type": "published_dependency_graph",
        "version": version,
        "nodes": nodes,
        "edges": edges,
        "semantics": "deps.dev's own resolution of the release's dependencies, as published",
    }
    if payload.get("error"):
        body["error"] = str(payload["error"])
    return [body]


def parse_spdx_list(
    licences: Mapping[str, Any], exceptions: Mapping[str, Any], *, expected: str
) -> dict[str, Any]:
    from src.kb.oss_spdx import list_content

    version = str(licences.get("licenseListVersion") or "")
    if (
        version != str(expected)
        or str(exceptions.get("licenseListVersion") or "") != version
    ):
        raise SourcePackError(
            "schema_drift", "SPDX documents do not carry the pinned list version"
        )
    content = list_content(
        version,
        licences.get("releaseDate"),
        [
            {
                "id": e["licenseId"],
                "name": e.get("name"),
                "deprecated": e.get("isDeprecatedLicenseId"),
            }
            for e in licences.get("licenses") or []
        ],
        [
            {
                "id": e["licenseExceptionId"],
                "name": e.get("name"),
                "deprecated": e.get("isDeprecatedLicenseId"),
            }
            for e in exceptions.get("exceptions") or []
        ],
    )
    return {"record_type": "spdx_list_release", "source": "spdx", **content}


def _swhid(kind: str, value: Any) -> str | None:
    text = str(value or "")
    if text.startswith("swh:1:"):
        return text
    return f"swh:1:{kind}:{text}" if re.fullmatch(r"[0-9a-f]{40}", text) else None


def parse_swh_visits(payload: Any, *, origin: str) -> list[dict[str, Any]]:
    if not isinstance(payload, list):
        raise SourcePackError("schema_drift", "Software Heritage visits are a list")
    out = []
    for item in payload[:MAX_SWH_VISITS]:
        body = {
            "record_type": "archive_provenance",
            "source": "software-heritage",
            "detail": "visit",
            "origin_url": origin,
            "visit": int(item["visit"]),
            "visit_date": item.get("date"),
            "visit_status": item.get("status"),
            "visit_type": item.get("type"),
            "snapshot_swhid": _swhid("snp", item.get("snapshot")),
        }
        out.append({k: v for k, v in body.items() if v is not None})
    return out


def parse_swh_snapshot(
    payload: Mapping[str, Any], *, origin: str, visit: int
) -> dict[str, Any]:
    branches = []
    for name, target in sorted(dict(payload.get("branches") or {}).items())[
        :MAX_SWH_BRANCHES
    ]:
        if not str(name).startswith("refs/tags/") or not isinstance(target, Mapping):
            continue
        kind = {"release": "rel", "revision": "rev"}.get(str(target.get("target_type")))
        if kind is None:
            continue
        branches.append(
            {
                "name": str(name),
                "target_type": str(target["target_type"]),
                "target_swhid": _swhid(kind, target.get("target")),
            }
        )
    return {
        "record_type": "archive_provenance",
        "source": "software-heritage",
        "detail": "snapshot",
        "origin_url": origin,
        "visit": visit,
        "snapshot_swhid": _swhid("snp", payload.get("id")),
        "branches": [b for b in branches if b["target_swhid"]],
    }


# ------------------------------------------------------------------ transports


def fixture_key(url: str, params: Mapping[str, Any] | None = None) -> str:
    """URL path plus its own query and the encoded parameters (the fixture lookup key)."""

    parts = urlsplit(url)
    query = "&".join(q for q in (parts.query, urlencode(dict(params or {}))) if q)
    return parts.path + ("?" + query if query else "")


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored provider responses keyed by URL path plus the encoded query."""

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        key = fixture_key(url, params)
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("body_base64") is not None:
            content = base64.b64decode(page["body_base64"])
        else:
            body = page.get("body")
            content = (
                body.encode()
                if isinstance(body, str)
                else b""
                if body is None
                else json.dumps(body).encode()
            )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def durable_transport(
    http: Any, *, principal_id: str, run_key: str
) -> Callable[..., Mapping[str, Any]]:
    """Adapt :class:`DurableHTTP` (explicit budget, receipts, host allow-list) to the adapter transport."""

    def transport(*, url, params, headers, timeout):
        public = {
            k: v for k, v in dict(headers).items() if k.lower() != "authorization"
        }
        secret = {
            k: v for k, v in dict(headers).items() if k.lower() == "authorization"
        }
        key = (
            f"{run_key}:"
            + hashlib.sha256(canonical([url, dict(params)]).encode()).hexdigest()[:32]
        )
        captured = http.request(
            key,
            url,
            principal_id=principal_id,
            params=dict(params),
            headers=public,
            secret_headers=secret,
            timeout_s=max(0.1, min(60.0, float(timeout))),
        )
        receipt = dict(captured.receipt)
        return {
            "status": int(receipt.get("status", 200)),
            "headers": dict(receipt.get("headers") or {}),
            "content": captured.content,
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = OssEcosystemAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
    )
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page(
            {
                "operation": min(source["operations"]),
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


def store_repository_origins(
    conn: Any, namespace: str, *, limit: int = MAX_ORIGINS
) -> list[str]:
    """Origins for a Software Heritage run: https repository URLs asserted in link assertions, bounded."""

    from src.kb.oss_ecosystem_store import OssEcosystemStore

    asserted = (
        OssEcosystemStore(conn, initialize=False).asserted_repositories(namespace)
        if conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='oss_records'"
        ).fetchone()
        else {}
    )
    origins = sorted({"https://" + key for key in asserted})
    return origins[:limit]


__all__ = [
    "ADAPTERS",
    "CONNECTOR",
    "FORMATS",
    "LIVE_VERIFICATION",
    "PROVIDER_CONTRACTS",
    "OssEcosystemAdapter",
    "durable_transport",
    "fixture_transport",
    "oss_declaration",
    "parse_deps_dev",
    "parse_spdx_list",
    "parse_swh_snapshot",
    "parse_swh_visits",
    "replay_native_fixture",
    "store_repository_origins",
]
