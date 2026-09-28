"""Authored provider responses for the OSS Ecosystems pack: two polls of fictional packages.

Every payload is written in the provider's documented shape for fictional
packages, organisations and repositories; none is a capture. Personal fields
(``author``, ``maintainers``, ``published_by``, ``<developers>``) are included
on purpose so the tests prove they are dropped at parse time.

Poll 1 is observed on 2026-03-01, poll 2 on 2026-06-01. Between them:

* PyPI ``fixture-parser`` 1.1.0 is yanked, 2.0.0 is released and changes the
  licence from ``MIT`` to ``BUSL-1.1``; ``fixture-tokens`` 1.3.0 is released;
* npm ``@fixture-labs/tokenizer`` 1.1.0 is deprecated, 1.2.0 is released with
  ``Apache-2.0`` (was ``MIT``), 0.9.0 is unpublished; ``fixture-lexer`` 2.3.0
  is released;
* crates.io ``fixture-bytes`` 0.4.1 and ``fixture-codec`` 0.2.0 are yanked;
* Maven ``org.fixturelabs:fixture-core`` 2.0 moves to ``EPL-2.0``.

``source_pack_fixture`` writes poll 1 as the committed source-pack fixtures.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from src.domains.technical.registries import PROVIDERS
from src.ingestion.oss_ecosystem_sources import fixture_key

ROOT = Path(__file__).resolve().parents[3]
POLL_MS = {1: 1_772_323_200_000, 2: 1_780_272_000_000}  # 2026-03-01, 2026-06-01 (UTC)
DATES = {"d1": "2026-04-01", "d2": "2026-07-01"}
SWH = "https://archive.softwareheritage.org/api/1"
DEPS_DEV = "https://api.deps.dev/v3"
SPDX = "https://raw.githubusercontent.com/spdx/license-list-data"
PERSON = {
    "author": "Ada Example",
    "author_email": "ada@example.invalid",
    "maintainer": "Grace Example",
}


def _page(
    url: str,
    body: Any,
    *,
    status: int = 200,
    params: dict | None = None,
    headers: dict | None = None,
) -> dict:
    page = {"request": fixture_key(url, params), "status": status, "body": body}
    if headers:
        page["headers"] = headers
    return page


def _file(
    version: str, uploaded: str, *, yanked: bool = False, reason: str | None = None
) -> dict:
    return {
        "filename": f"fixture-{version}.tar.gz",
        "upload_time_iso_8601": uploaded,
        "yanked": yanked,
        "yanked_reason": reason,
        "digests": {"sha256": hashlib.sha256(version.encode()).hexdigest()},
        "uploaded_via": "twine/5.0.0",
    }


# ---------------------------------------------------------------- PyPI

PYPI = {
    "fixture-parser": {
        "repository": "https://github.com/Fixture-Labs/parser",
        "releases": {
            "1.0.0": {
                "uploaded": "2025-10-01T09:00:00Z",
                "licence": "MIT",
                "requires": ["fixture-tokens>=1.0,<2"],
            },
            "1.1.0": {
                "uploaded": "2026-01-10T09:00:00Z",
                "licence": "MIT",
                "requires": [
                    "fixture-tokens (>=1.0,<2)",
                    "fixture-missing>=1",
                    "pytest>=7; extra == 'test'",
                    "colorama>=0.4; sys_platform == 'win32'",
                ],
                "yanked_in": 2,
                "reason": "Broken wheel metadata; use 1.0.0",
            },
            "2.0.0": {
                "uploaded": "2026-05-01T09:00:00Z",
                "licence": "BUSL-1.1",
                "since": 2,
                "requires": ["fixture-tokens>=9"],
            },
        },
    },
    "fixture-tokens": {
        "repository": "https://github.com/fixture-labs/parser",  # the same repository as fixture-parser
        "releases": {
            "1.0.0": {
                "uploaded": "2025-09-01T09:00:00Z",
                "licence": "MIT",
                "requires": [],
            },
            "1.2.0": {
                "uploaded": "2026-02-01T09:00:00Z",
                "licence": "MIT",
                "requires": [],
            },
            "1.3.0": {
                "uploaded": "2026-05-10T09:00:00Z",
                "licence": "MIT",
                "requires": [],
                "since": 2,
            },
            "2.0rc1": {
                "uploaded": "2026-02-10T09:00:00Z",
                "licence": "MIT",
                "requires": [],
            },
        },
    },
}


def _live(release: dict, poll: int) -> bool:
    return release.get("since", 1) <= poll


def pypi_pages(poll: int) -> list[dict]:
    provider = PROVIDERS["pypi"]()
    pages = []
    for name, package in PYPI.items():
        releases = {v: r for v, r in package["releases"].items() if _live(r, poll)}
        items = provider.history_items(name, list(package["releases"]))
        files = {}
        for version, release in releases.items():
            yanked = release.get("yanked_in", 99) <= poll
            files[version] = [
                _file(
                    version,
                    release["uploaded"],
                    yanked=yanked,
                    reason=release.get("reason") if yanked else None,
                )
            ]
        latest = max(releases, key=lambda v: releases[v]["uploaded"])
        project = {
            "info": {
                "name": name,
                "version": latest,
                **PERSON,
                "project_urls": {
                    "Homepage": "https://fixture-labs.example",
                    "Source": package["repository"],
                },
            },
            "releases": files,
            "ownership": {
                "organization": "fixture-labs",
                "roles": [{"role": "Owner", "user": "ada-example"}],
            },
            "vulnerabilities": [],
            "last_serial": 1000 + poll,
        }
        pages.append(_page(items[0]["url"], project))
        for item in items[1:]:
            release = package["releases"][item["version"]]
            if not _live(release, poll):
                pages.append(_page(item["url"], {"message": "Not Found"}, status=404))
                continue
            info = {
                "name": name,
                "version": item["version"],
                "requires_dist": release["requires"],
                "license_expression": release["licence"],
                "license": None,
                "classifiers": ["Programming Language :: Python :: 3"],
                **PERSON,
            }
            if files[item["version"]][0]["yanked"]:
                info.update({"yanked": True, "yanked_reason": release["reason"]})
            pages.append(
                _page(
                    item["url"],
                    {
                        "info": info,
                        "urls": files[item["version"]],
                        "vulnerabilities": [],
                    },
                )
            )
    return pages


# ---------------------------------------------------------------- npm

NPM = {
    "@fixture-labs/tokenizer": {
        "repository": {
            "type": "git",
            "url": "git+https://github.com/fixture-labs/tokenizer.git",
        },
        "versions": {
            "0.9.0": {"time": "2025-08-01T00:00:00.000Z", "license": "MIT", "until": 1},
            "1.0.0": {
                "time": "2025-11-01T00:00:00.000Z",
                "license": "MIT",
                "dependencies": {"fixture-lexer": "^2.1.0"},
            },
            "1.1.0": {
                "time": "2026-02-01T00:00:00.000Z",
                "license": "MIT",
                "dependencies": {"fixture-lexer": "^2.1.0", "fixture-native": "~1.0.0"},
                "optionalDependencies": {"fixture-native": "~1.0.0"},
                "devDependencies": {"fixture-test": "^1.0.0"},
                "peerDependencies": {"fixture-host": ">=3"},
                "peerDependenciesMeta": {"fixture-host": {"optional": True}},
                "deprecated_in": 2,
                "deprecated": "Tokenizer bug; upgrade to 1.2.0",
            },
            "1.2.0": {
                "time": "2026-05-05T00:00:00.000Z",
                "license": "Apache-2.0",
                "since": 2,
                "dependencies": {"fixture-lexer": "^2.1.0"},
            },
        },
    },
    "fixture-lexer": {
        "repository": "github:fixture-labs/lexer",
        "versions": {
            "2.1.0": {"time": "2025-10-01T00:00:00.000Z", "license": "MIT"},
            "2.2.0": {"time": "2026-01-15T00:00:00.000Z", "license": "MIT"},
            "2.3.0": {"time": "2026-05-20T00:00:00.000Z", "license": "MIT", "since": 2},
            "3.0.0-beta.1": {"time": "2026-02-20T00:00:00.000Z", "license": "MIT"},
        },
    },
}


def npm_pages(poll: int) -> list[dict]:
    provider = PROVIDERS["npm"]()
    pages = []
    for name, package in NPM.items():
        live = {v: r for v, r in package["versions"].items() if _live(r, poll)}
        versions, times = {}, {}
        for version, release in live.items():
            times[version] = release["time"]
            if release.get("until", 99) < poll:
                continue  # unpublished: stays in time, leaves versions
            body = {
                "name": name,
                "version": version,
                "license": release["license"],
                "_npmUser": {"name": "ada-example", "email": "ada@example.invalid"},
                "maintainers": [{"name": "ada-example"}],
            }
            for key in (
                "dependencies",
                "optionalDependencies",
                "devDependencies",
                "peerDependencies",
                "peerDependenciesMeta",
            ):
                if release.get(key):
                    body[key] = release[key]
            if release.get("deprecated_in", 99) <= poll:
                body["deprecated"] = release["deprecated"]
            versions[version] = body
        modified = max(times.values())
        packument = {
            "name": name,
            "versions": versions,
            "time": {"created": min(times.values()), "modified": modified, **times},
            "maintainers": [{"name": "ada-example", "email": "ada@example.invalid"}],
            "author": {"name": "Ada Example"},
            "repository": package["repository"],
            "dist-tags": {"latest": max(versions)},
        }
        pages.append(_page(provider.history_items(name, [])[0]["url"], packument))
    return pages


# ---------------------------------------------------------------- crates.io

CRATES = {
    "fixture-codec": {
        "repository": "https://github.com/fixture-labs/codec",
        "versions": {
            "0.1.0": {
                "created": "2025-12-01T00:00:00Z",
                "license": "MIT/Apache-2.0",
                "deps": [
                    {"crate_id": "fixture-bytes", "req": "^0.4", "kind": "normal"},
                    {"crate_id": "fixture-bench", "req": "0.1", "kind": "dev"},
                    {
                        "crate_id": "fixture-simd",
                        "req": "1",
                        "kind": "normal",
                        "optional": True,
                        "target": 'cfg(target_arch = "x86_64")',
                    },
                ],
            },
            "0.2.0": {
                "created": "2026-02-01T00:00:00Z",
                "license": "MIT OR Apache-2.0",
                "deps": [
                    {
                        "crate_id": "fixture-bytes",
                        "req": ">=0.4.1, <0.5",
                        "kind": "normal",
                    }
                ],
                "yanked_in": 2,
                "reason": "Unsound decoder",
            },
        },
    },
    "fixture-bytes": {
        "repository": "https://github.com/fixture-labs/bytes",
        "versions": {
            "0.4.0": {"created": "2025-10-01T00:00:00Z", "license": "MIT", "deps": []},
            "0.4.1": {
                "created": "2026-01-05T00:00:00Z",
                "license": "MIT",
                "deps": [],
                "yanked_in": 2,
                "reason": "Data race in the buffer pool",
            },
        },
    },
}


def crates_pages(poll: int) -> list[dict]:
    provider = PROVIDERS["cargo"]()
    pages = []
    for name, crate in CRATES.items():
        items = provider.history_items(name, list(crate["versions"]))
        versions = []
        for number, release in crate["versions"].items():
            yanked = release.get("yanked_in", 99) <= poll
            versions.append(
                {
                    "num": number,
                    "created_at": release["created"],
                    "updated_at": "2026-05-15T00:00:00Z"
                    if yanked
                    else release["created"],
                    "yanked": yanked,
                    "yank_message": release.get("reason") if yanked else None,
                    "license": release["license"],
                    "checksum": "0" * 64,
                    "published_by": {"login": "ada-example", "name": "Ada Example"},
                    "audit_actions": [
                        {"action": "publish", "user": {"login": "ada-example"}}
                    ],
                }
            )
        pages.append(
            _page(
                items[0]["url"],
                {
                    "crate": {
                        "name": name,
                        "repository": crate["repository"],
                        "downloads": 12345,
                    },
                    "versions": versions,
                },
            )
        )
        pages.append(
            _page(
                items[1]["url"],
                {
                    "teams": [
                        {
                            "login": "github:fixture-labs:crates",
                            "kind": "team",
                            "name": "Crates",
                        }
                    ]
                },
            )
        )
        for item in items[2:]:
            deps = [
                {
                    "version_id": 1,
                    "default_features": True,
                    "features": [],
                    "optional": False,
                    "target": None,
                    **d,
                }
                for d in crate["versions"][item["version"]]["deps"]
            ]
            pages.append(_page(item["url"], {"dependencies": deps}))
    return pages


# ---------------------------------------------------------------- Maven Central

MAVEN = {
    "org.fixturelabs:fixture-core": {
        "versions": {
            "1.0": {
                "timestamp": 1_764_547_200_000,
                "licence": "The Apache Software License, Version 2.0",
                "deps": [
                    ("org.fixturelabs", "fixture-util", "[1.0,2.0)", None, None),
                    ("junit", "junit", "4.13.2", "test", None),
                ],
            },
            "1.1": {
                "timestamp": 1_771_113_600_000,
                "licence": "The Apache Software License, Version 2.0",
                "deps": [
                    ("org.fixturelabs", "fixture-util", "[1.0,2.0)", None, None),
                    (
                        "org.fixturelabs",
                        "fixture-extra",
                        "${extra.version}",
                        None,
                        "true",
                    ),
                ],
            },
            "2.0": {
                "timestamp": 1_777_593_600_000,
                "licence": "Eclipse Public License - v 2.0",
                "since": 2,
                "deps": [("org.fixturelabs", "fixture-util", "1.5", None, None)],
            },
        },
    },
    "org.fixturelabs:fixture-util": {
        "versions": {
            "1.0": {
                "timestamp": 1_756_684_800_000,
                "licence": "The Apache Software License, Version 2.0",
                "deps": [],
            },
            "1.5": {
                "timestamp": 1_767_225_600_000,
                "licence": "The Apache Software License, Version 2.0",
                "deps": [],
            },
            "1.6": {
                "timestamp": 1_778_371_200_000,
                "licence": "The Apache Software License, Version 2.0",
                "deps": [],
                "since": 2,
            },
        },
    },
}


def _pom(group: str, artifact: str, version: str, release: dict) -> str:
    deps = "".join(
        f"<dependency><groupId>{g}</groupId><artifactId>{a}</artifactId><version>{v}</version>"
        + (f"<scope>{s}</scope>" if s else "")
        + (f"<optional>{o}</optional>" if o else "")
        + "</dependency>"
        for g, a, v, s, o in release["deps"]
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion>'
        f"<groupId>{group}</groupId><artifactId>{artifact}</artifactId><version>{version}</version>"
        "<organization><name>Fixture Labs</name><url>https://fixture-labs.example</url></organization>"
        f"<licenses><license><name>{release['licence']}</name></license></licenses>"
        f"<scm><url>https://github.com/fixture-labs/{artifact}</url></scm>"
        "<developers><developer><id>ada</id><name>Ada Example</name><email>ada@example.invalid</email>"
        "</developer></developers>"
        f"<dependencies>{deps}</dependencies></project>"
    )


def maven_pages(poll: int) -> list[dict]:
    provider = PROVIDERS["maven"]()
    pages = []
    for name, package in MAVEN.items():
        group, artifact = name.split(":")
        items = provider.history_items(name, list(package["versions"]))
        docs = [
            {
                "id": f"{name}:{v}",
                "g": group,
                "a": artifact,
                "v": v,
                "timestamp": r["timestamp"],
                "developers": ["Ada Example"],
            }
            for v, r in package["versions"].items()
            if _live(r, poll)
        ]
        pages.append(
            _page(items[0]["url"], {"response": {"numFound": len(docs), "docs": docs}})
        )
        for item in items[1:]:
            release = package["versions"][item["version"]]
            if not _live(release, poll):
                pages.append(_page(item["url"], "Not Found", status=404))
            else:
                pages.append(
                    _page(item["url"], _pom(group, artifact, item["version"], release))
                )
    return pages


# ---------------------------------------------------------------- deps.dev, SPDX, Software Heritage


def deps_dev_pages(poll: int) -> list[dict]:
    base = f"{DEPS_DEV}/systems/NPM/packages/%40fixture-labs%2Ftokenizer"
    versions = [
        {
            "versionKey": {
                "system": "NPM",
                "name": "@fixture-labs/tokenizer",
                "version": v,
            },
            "publishedAt": r["time"].replace(".000", ""),
            "isDefault": v == "1.1.0",
        }
        for v, r in NPM["@fixture-labs/tokenizer"]["versions"].items()
        if _live(r, poll)
    ]
    pages = [
        _page(
            base,
            {
                "packageKey": {"system": "NPM", "name": "@fixture-labs/tokenizer"},
                "versions": versions,
            },
        )
    ]
    for version in ("1.1.0", "1.2.0"):
        if version == "1.2.0" and poll < 2:
            pages += [
                _page(f"{base}/versions/{version}", {"error": "not found"}, status=404),
                _page(
                    f"{base}/versions/{version}:dependencies",
                    {"error": "not found"},
                    status=404,
                ),
            ]
            continue
        # deps.dev states Apache-2.0 for 1.1.0 where the registry states MIT: kept side by side.
        pages.append(
            _page(
                f"{base}/versions/{version}",
                {
                    "versionKey": {
                        "system": "NPM",
                        "name": "@fixture-labs/tokenizer",
                        "version": version,
                    },
                    "licenses": ["Apache-2.0"],
                    "advisoryKeys": [{"id": "GHSA-f1x7-zzzz-zzzz"}],
                    "relatedProjects": [
                        {
                            "projectKey": {"id": "github.com/fixture-labs/tokenizer"},
                            "relationProvenance": "UNVERIFIED_METADATA",
                            "relationType": "SOURCE_REPO",
                        },
                        {
                            "projectKey": {"id": "github.com/fixture-labs/issues"},
                            "relationType": "ISSUE_TRACKER",
                        },
                    ],
                },
            )
        )
        pages.append(
            _page(
                f"{base}/versions/{version}:dependencies",
                {
                    "nodes": [
                        {
                            "versionKey": {
                                "system": "NPM",
                                "name": "@fixture-labs/tokenizer",
                                "version": version,
                            },
                            "relation": "SELF",
                            "bundled": False,
                            "errors": [],
                        },
                        {
                            "versionKey": {
                                "system": "NPM",
                                "name": "fixture-lexer",
                                "version": "2.2.0",
                            },
                            "relation": "DIRECT",
                            "bundled": False,
                            "errors": [],
                        },
                        {
                            "versionKey": {
                                "system": "NPM",
                                "name": "fixture-native",
                                "version": "1.0.3",
                            },
                            "relation": "DIRECT",
                            "bundled": False,
                            "errors": [],
                        },
                    ],
                    "edges": [
                        {"fromNode": 0, "toNode": 1, "requirement": "^2.1.0"},
                        {"fromNode": 0, "toNode": 2, "requirement": "~1.0.0"},
                    ],
                },
            )
        )
    return pages


def spdx_pages(poll: int) -> list[dict]:
    del poll
    licences = [
        ("MIT", "MIT License", False),
        ("Apache-2.0", "Apache License 2.0", False),
        ("BUSL-1.1", "Business Source License 1.1", False),
        ("EPL-2.0", "Eclipse Public License 2.0", False),
        ("GPL-2.0", "GNU General Public License v2.0 only", True),
        ("GPL-2.0-or-later", "GNU General Public License v2.0 or later", False),
    ]
    return [
        _page(
            f"{SPDX}/v3.25/json/licenses.json",
            {
                "licenseListVersion": "3.25",
                "releaseDate": "2024-08-19",
                "licenses": [
                    {
                        "licenseId": i,
                        "name": n,
                        "isDeprecatedLicenseId": d,
                        "isOsiApproved": True,
                        "reference": f"https://spdx.org/licenses/{i}.html",
                    }
                    for i, n, d in licences
                ],
            },
        ),
        _page(
            f"{SPDX}/v3.25/json/exceptions.json",
            {
                "licenseListVersion": "3.25",
                "releaseDate": "2024-08-19",
                "exceptions": [
                    {
                        "licenseExceptionId": "Classpath-exception-2.0",
                        "name": "Classpath exception 2.0",
                        "isDeprecatedLicenseId": False,
                    }
                ],
            },
        ),
    ]


ORIGIN = "https://github.com/fixture-labs/parser"
SNAPSHOT = {1: "a" * 40, 2: "b" * 40}


def swh_pages(poll: int) -> list[dict]:
    visits = [
        {
            "origin": ORIGIN,
            "visit": 1,
            "date": "2026-01-15T10:00:00+00:00",
            "status": "full",
            "snapshot": SNAPSHOT[1],
            "type": "git",
        }
    ]
    if poll >= 2:
        visits.insert(
            0,
            {
                "origin": ORIGIN,
                "visit": 2,
                "date": "2026-05-02T10:00:00+00:00",
                "status": "full",
                "snapshot": SNAPSHOT[2],
                "type": "git",
            },
        )
    tags = {"refs/tags/v1.0.0": "1" * 40, "refs/tags/v1.1.0": "2" * 40}
    if poll >= 2:
        tags["refs/tags/v2.0.0"] = "3" * 40
    branches = {
        name: {"target": target, "target_type": "release"}
        for name, target in tags.items()
    }
    branches["refs/heads/main"] = {"target": "9" * 40, "target_type": "revision"}
    branches["HEAD"] = {"target": "refs/heads/main", "target_type": "alias"}
    snapshot = SNAPSHOT[poll]
    return [
        _page(
            f"{SWH}/origin/{ORIGIN}/visits/",
            visits,
            params={"per_page": 20},
            headers={"X-RateLimit-Limit": "120", "X-RateLimit-Remaining": "118"},
        ),
        _page(
            f"{SWH}/snapshot/{snapshot}/",
            {"id": snapshot, "branches": branches, "next_branch": None},
            params={"branches_count": 1000, "target_types": "release,revision"},
        ),
    ]


BUILDERS = {
    "pypi-json": pypi_pages,
    "npm-registry": npm_pages,
    "crates-io": crates_pages,
    "maven-central": maven_pages,
    "deps-dev-npm": deps_dev_pages,
    "spdx-license-list": spdx_pages,
    "software-heritage": swh_pages,
}


def pages(source_id: str, poll: int) -> list[dict]:
    return BUILDERS[source_id](poll)


def fixture_path(source_id: str) -> Path:
    return ROOT / "tests/fixtures/source_packs" / f"oss-{source_id}.json"


def source_pack_fixture(source_id: str) -> str:
    return (
        json.dumps(
            {
                "authored": True,
                "native_pages": pages(source_id, 1),
                "scenarios": ["authored poll 1 of fictional packages"],
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
