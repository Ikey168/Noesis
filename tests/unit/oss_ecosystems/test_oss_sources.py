"""Registry, deps.dev, SPDX and Software Heritage acquisition through the real adapter and runtime (OS03-OS05)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.domains.technical.registries import (
    PROVIDERS,
    PackageRegistryConnector,
    registry_history,
)
from src.ingestion.oss_ecosystem_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    OssEcosystemAdapter,
    fixture_transport,
    store_repository_origins,
)
from src.ingestion.source_packs import (
    SUPPORTED_CONNECTORS,
    SourcePackConformance,
    SourcePackError,
)
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError
from tests.unit.oss_ecosystems import fixture_builder as fb
from tests.unit.oss_ecosystems import harness as h


def _current(store, record_type, source, coordinate, version=None):
    records = store.records(
        h.NS,
        record_type=record_type,
        source=source,
        coordinate=coordinate,
        version=version,
    )
    assert len(records) == 1, records
    return store.current(records[0]["record_id"])["statement"]


@pytest.fixture(scope="module")
def world():
    return h.world()


def test_committed_fixtures_are_the_authored_poll_and_replay_offline():
    for item in h.manifest()["sources"]:
        assert fb.fixture_path(item["source_id"]).read_text() == fb.source_pack_fixture(
            item["source_id"]
        )
    result = SourcePackConformance(h.ROOT).offline(h.manifest())
    assert result["valid"], [s for s in result["sources"] if not s["valid"]]
    assert "oss-ecosystem" in SUPPORTED_CONNECTORS
    assert set(LIVE_VERIFICATION) == {
        n for n, c in PROVIDER_CONTRACTS.items() if c["decision"] == "implement"
    }
    assert all(v["status"] == "unverified-live" for v in LIVE_VERIFICATION.values())
    assert PROVIDER_CONTRACTS["libraries-io"]["decision"] == "not implemented"
    assert PROVIDER_CONTRACTS["pypi-bigquery"]["decision"] == "out of scope"


def test_existing_registry_providers_emit_history_and_keep_ingest_package(world):
    assert all(PROVIDERS[e]().history_source for e in ("pypi", "npm", "cargo", "maven"))
    connector = PackageRegistryConnector()
    ref = next(connector.discover({"ecosystem": "pypi", "package": "fixture-parser"}))
    project = next(
        p["body"]
        for p in fb.pypi_pages(2)
        if p["request"].endswith("/fixture-parser/json")
    )
    statements = connector.history(ref, project, kind="project")
    assert {s["record_type"] for s in statements} >= {
        "release_state_revision",
        "release_listing",
    }
    assert statements == registry_history(
        "pypi",
        project,
        kind="project",
        source_url=ref.locator,
        package="fixture-parser",
    )


def test_yanks_deprecations_and_unpublishes_are_kept_verbatim(world):
    store = OssEcosystemStore(world)
    pypi = _current(
        store, "release_state_revision", "pypi", "pkg:pypi:fixture-parser", "1.1.0"
    )
    assert (
        pypi["state"] == "yanked"
        and pypi["reason"] == "Broken wheel metadata; use 1.0.0"
    )
    npm = _current(
        store,
        "release_state_revision",
        "npm",
        "pkg:npm:@fixture-labs/tokenizer",
        "1.1.0",
    )
    assert (
        npm["state"] == "deprecated"
        and npm["reason"] == "Tokenizer bug; upgrade to 1.2.0"
    )
    gone = _current(
        store,
        "release_state_revision",
        "npm",
        "pkg:npm:@fixture-labs/tokenizer",
        "0.9.0",
    )
    assert gone["state"] == "unpublished" and "time map" in gone["reason"]
    crate = _current(
        store, "release_state_revision", "crates-io", "pkg:cargo:fixture-bytes", "0.4.1"
    )
    assert (
        crate["state"] == "yanked" and crate["reason"] == "Data race in the buffer pool"
    )
    maven = _current(
        store,
        "release_state_revision",
        "maven-central",
        "pkg:maven:org.fixturelabs:fixture-core",
        "2.0",
    )
    assert (
        maven["state"] == "published"
        and maven["published_at"] == "2026-05-01T00:00:00Z"
    )
    history = store.history(
        store.records(
            h.NS,
            record_type="release_state_revision",
            source="pypi",
            coordinate="pkg:pypi:fixture-parser",
            version="1.1.0",
        )[0]["record_id"]
    )
    assert [r["statement"]["state"] for r in history] == ["published", "yanked"]


def test_declared_dependencies_are_parsed_as_written_without_resolution(world):
    store = OssEcosystemStore(world)
    pypi = _current(
        store, "declared_dependency_set", "pypi", "pkg:pypi:fixture-parser", "1.1.0"
    )["entries"]
    by_name = {e["name"]: e for e in pypi}
    assert (
        by_name["fixture-tokens"]["constraint"] == ">=1.0,<2"
        and by_name["fixture-tokens"]["scope"] == "runtime"
    )
    assert (
        by_name["pytest"]["scope"] == "optional"
        and by_name["pytest"]["extra"] == "test"
    )
    assert by_name["colorama"]["marker"] == 'sys_platform == "win32"'
    npm = {
        e["name"]: e
        for e in _current(
            store,
            "declared_dependency_set",
            "npm",
            "pkg:npm:@fixture-labs/tokenizer",
            "1.1.0",
        )["entries"]
    }
    assert (
        npm["fixture-lexer"]["scope"] == "runtime"
        and npm["fixture-test"]["scope"] == "dev"
    )
    assert (
        npm["fixture-host"]["scope"] == "peer"
        and npm["fixture-host"]["optional"] is True
    )
    assert npm["fixture-native"]["scope"] == "optional" and len(npm) == 4
    cargo = {
        e["name"]: e
        for e in _current(
            store,
            "declared_dependency_set",
            "crates-io",
            "pkg:cargo:fixture-codec",
            "0.1.0",
        )["entries"]
    }
    assert (
        cargo["fixture-bench"]["scope"] == "dev"
        and cargo["fixture-simd"]["scope"] == "optional"
    )
    assert cargo["fixture-simd"]["target"] == 'cfg(target_arch = "x86_64")'
    maven = {
        e["name"]: e
        for e in _current(
            store,
            "declared_dependency_set",
            "maven-central",
            "pkg:maven:org.fixturelabs:fixture-core",
            "1.1",
        )["entries"]
    }
    assert maven["org.fixturelabs:fixture-util"]["constraint"] == "[1.0,2.0)"
    assert maven["org.fixturelabs:fixture-extra"]["constraint"] == "${extra.version}"
    assert maven["org.fixturelabs:fixture-extra"]["scope"] == "optional"
    junit = {
        e["name"]: e
        for e in _current(
            store,
            "declared_dependency_set",
            "maven-central",
            "pkg:maven:org.fixturelabs:fixture-core",
            "1.0",
        )["entries"]
    }["junit:junit"]
    assert junit["scope"] == "dev" and junit["source_scope"] == "test"


def test_people_are_dropped_and_only_organisations_are_kept(world):
    text = h.all_statements(world)
    assert not [p for p in h.PERSONAL_STRINGS if p in text]
    assert "GHSA-f1x7-zzzz-zzzz" not in text  # deps.dev advisory keys are never copied
    store = OssEcosystemStore(world)
    kinds = {
        o["kind"]
        for r in store.records(h.NS, record_type="publisher_organisation")
        for o in store.current(r["record_id"])["statement"]["organisations"]
    }
    assert kinds == {
        "pypi-organisation",
        "npm-scope",
        "crates-team",
        "maven-groupid",
        "pom-organisation",
    }


def test_deps_dev_is_a_second_source_kept_side_by_side(world):
    store = OssEcosystemStore(world)
    registry = _current(
        store,
        "licence_declaration_revision",
        "npm",
        "pkg:npm:@fixture-labs/tokenizer",
        "1.1.0",
    )
    depsdev = _current(
        store,
        "licence_declaration_revision",
        "deps-dev",
        "pkg:npm:@fixture-labs/tokenizer",
        "1.1.0",
    )
    assert registry["raw"] == {"expression": "MIT"} and depsdev["raw"] == {
        "expression": "Apache-2.0"
    }
    graph = _current(
        store,
        "published_dependency_graph",
        "deps-dev",
        "pkg:npm:@fixture-labs/tokenizer",
        "1.1.0",
    )
    assert (
        graph["semantics"].startswith("deps.dev's own resolution")
        and len(graph["nodes"]) == 3
    )
    link = _current(
        store,
        "repository_link_assertion",
        "deps-dev",
        "pkg:npm:@fixture-labs/tokenizer",
        "1.1.0",
    )
    assert [x["repository_key"] for x in link["links"]] == [
        "github.com/fixture-labs/tokenizer"
    ]


def test_spdx_list_and_software_heritage_provenance(world):
    store = OssEcosystemStore(world)
    assert store.spdx_versions(h.NS) == ["3.25"]
    assert store.spdx_list(h.NS).licences["gpl-2.0"]["deprecated"] is True
    snapshots = [
        r
        for r in store.records(h.NS, record_type="archive_provenance")
        if store.current(r["record_id"])["statement"]["detail"] == "snapshot"
    ]
    assert len(snapshots) == 2
    latest = max(
        (store.current(r["record_id"])["statement"] for r in snapshots),
        key=lambda s: s["visit"],
    )
    assert [b["name"] for b in latest["branches"]] == [
        "refs/tags/v1.0.0",
        "refs/tags/v1.1.0",
        "refs/tags/v2.0.0",
    ]
    assert latest["branches"][0]["target_swhid"] == "swh:1:rel:" + "1" * 40
    assert store_repository_origins(world, h.NS)[0].startswith(
        "https://github.com/fixture-labs/"
    )


def test_archive_origins_must_be_asserted_and_rate_limits_are_honoured():
    conn = duckdb.connect()
    store = OssEcosystemStore(conn)
    with pytest.raises(OssStoreError) as caught:
        store.apply(
            h.NS,
            [
                {
                    "record_type": "archive_provenance",
                    "source": "software-heritage",
                    "detail": "visit",
                    "origin_url": "https://github.com/elsewhere/repo",
                    "visit": 1,
                }
            ],
            run_id="r",
            scopes=h.WRITE,
        )
    assert caught.value.code == "origin_not_asserted"
    pages = fb.swh_pages(1)
    pages[0]["headers"] = {
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Reset": "1767225660",
        "Date": "Thu, 01 Jan 2026 00:00:00 GMT",
    }
    adapter = OssEcosystemAdapter(
        h.source("software-heritage"), transport=fixture_transport(pages)
    )
    with pytest.raises(SourcePackError) as limited:
        adapter.fetch_page(
            {"operation": "acquire", "parameters": {}, "limit": 100}, cursor=None
        )
    assert (
        limited.value.code == "rate_limited"
        and limited.value.details["retry_after_ms"] == 60_000
    )


def test_runs_only_narrow_the_declared_selection():
    adapter = OssEcosystemAdapter(
        h.source("pypi-json"), transport=fixture_transport(fb.pypi_pages(1))
    )
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page(
            {"operation": "acquire", "parameters": {"packages": ["requests"]}},
            cursor=None,
        )
    assert caught.value.code == "parameter_forbidden"
    page = adapter.fetch_page(
        {"operation": "acquire", "parameters": {"packages": ["fixture-tokens"]}},
        cursor=None,
    )
    assert (
        page.next_cursor is None and page.receipt["not_published"]
    )  # 1.3.0 is not published in poll 1


def test_the_source_pack_runtime_projects_idempotently():
    conn = duckdb.connect()
    receipt = h.run_fixture_pack(conn, "first")
    assert receipt["status"] == "complete", json.dumps(receipt["sources"])[:2000]
    count = conn.execute("SELECT count(*) FROM oss_revisions").fetchone()[0]
    assert count > 0
    again = h.run_fixture_pack(conn, "second")
    assert again["status"] == "complete"
    assert conn.execute("SELECT count(*) FROM oss_revisions").fetchone()[0] == count
