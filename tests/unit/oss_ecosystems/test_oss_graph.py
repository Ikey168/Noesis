"""Dependency graphs as of a date with stated semantics, unresolved edges and replayable receipts (OS07)."""

from __future__ import annotations

import duckdb
import pytest

from src.kb.oss_ecosystem_graph import (
    SEMANTICS,
    DependencyGraphs,
    dependency_graph_as_of,
)
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError
from tests.unit.oss_ecosystems import fixture_builder as fb
from tests.unit.oss_ecosystems import harness as h

D1, D2 = fb.DATES["d1"], fb.DATES["d2"]


@pytest.fixture(scope="module")
def world():
    return h.world()


def graph(conn, package, version, date, **options):
    return dependency_graph_as_of(
        conn, h.NS, package, version, date, scopes=h.READ, **options
    )


def targets(answer):
    return sorted(e["to"] for e in answer["edges"])


def test_a_new_release_changes_the_graph_between_two_dates(world):
    first = graph(world, "pkg:pypi:fixture-parser", "1.1.0", D1)
    second = graph(world, "pkg:pypi:fixture-parser", "1.1.0", D2)
    assert targets(first) == [
        "pkg:pypi:fixture-tokens@1.2.0"
    ]  # 2.0rc1 is a pre-release, never chosen
    assert targets(second) == ["pkg:pypi:fixture-tokens@1.3.0"]
    assert first["semantics"] == SEMANTICS and "not an observed lockfile" in SEMANTICS
    assert (
        first["knowledge_cutoff"] == "2026-04-01T23:59:59.999Z"
        and first["generation"] != second["generation"]
    )
    npm = graph(world, "pkg:npm:@fixture-labs/tokenizer", "1.1.0", D2)
    assert targets(npm) == [
        "pkg:npm:fixture-lexer@2.3.0"
    ]  # 3.0.0-beta.1 is outside ^2.1.0


def test_a_yank_changes_the_graph_between_two_dates(world):
    assert targets(graph(world, "pkg:cargo:fixture-codec", "0.1.0", D1)) == [
        "pkg:cargo:fixture-bytes@0.4.1"
    ]
    after = graph(world, "pkg:cargo:fixture-codec", "0.1.0", D2)
    assert targets(after) == ["pkg:cargo:fixture-bytes@0.4.0"]
    reported = {e["name"]: e["reason"] for e in after["reported_not_resolved"]}
    assert reported == {
        "fixture-bench": "dev dependency reported, not resolved",
        "fixture-simd": "optional dependency reported, not resolved",
    }


def test_unsatisfiable_unknown_and_out_of_bounds_edges_are_listed_with_reasons(world):
    answer = graph(world, "pkg:pypi:fixture-parser", "2.0.0", D2)
    assert answer["edges"] == [] and answer["unresolved"][0]["reason"].startswith(
        "unsatisfiable"
    )
    before = graph(world, "pkg:pypi:fixture-parser", "2.0.0", D1)
    assert (
        before["unresolved"][0]["reason"] == "root_not_known"
    )  # not acquired by that cutoff
    out = {
        u["name"]: u["reason"]
        for u in graph(world, "pkg:pypi:fixture-parser", "1.1.0", D2)["unresolved"]
    }
    assert out["fixture-missing"].startswith("package not acquired")
    maven = graph(world, "pkg:maven:org.fixturelabs:fixture-core", "1.1", D2)
    assert targets(maven) == ["pkg:maven:org.fixturelabs:fixture-util@1.6"]
    shallow = graph(world, "pkg:pypi:fixture-parser", "1.1.0", D2, depth=0)
    assert shallow["edges"] == [] and any(
        u["reason"].startswith("depth_limit") for u in shallow["unresolved"]
    )
    with pytest.raises(OssStoreError):
        graph(world, "pkg:pypi:fixture-parser", "1.1.0", D2, depth=9)


def test_markers_are_reported_and_evaluated_only_on_request(world):
    default = graph(world, "pkg:pypi:fixture-parser", "1.1.0", D2)
    colorama = next(u for u in default["unresolved"] if u["name"] == "colorama")
    assert colorama["marker_note"] == "environment marker reported, not evaluated"
    linux = graph(
        world,
        "pkg:pypi:fixture-parser",
        "1.1.0",
        D2,
        environment={"sys_platform": "linux"},
    )
    assert {
        "name": "colorama",
        "reason": "excluded by its environment marker",
    }.items() <= next(
        e for e in linux["reported_not_resolved"] if e["name"] == "colorama"
    ).items()
    extras = graph(
        world,
        "pkg:pypi:fixture-parser",
        "1.1.0",
        D2,
        include_scopes=["runtime", "optional"],
    )
    assert any(
        u["name"] == "pytest" for u in extras["unresolved"]
    )  # requested, but outside the selection


def test_a_yanked_pypi_release_is_eligible_only_when_pinned_exactly():
    conn = h.world()
    store = OssEcosystemStore(conn)
    base = {
        "source": "pypi",
        "ecosystem": "pypi",
        "package": "fixture-app",
        "version": "1.0.0",
    }
    store.apply(
        h.NS,
        [
            {
                **base,
                "record_type": "release_state_revision",
                "state": "published",
                "published_at": "2026-01-01",
            },
            {
                **base,
                "record_type": "declared_dependency_set",
                "entries": [
                    {
                        "name": "fixture-parser",
                        "constraint": "==1.1.0",
                        "scope": "runtime",
                    }
                ],
            },
        ],
        run_id="app",
        scopes=h.WRITE,
        observed_at_ms=fb.POLL_MS[2],
    )
    answer = graph(conn, "pkg:pypi:fixture-app", "1.0.0", D2)
    direct = next(
        e for e in answer["edges"] if e["from"] == "pkg:pypi:fixture-app@1.0.0"
    )
    assert direct["to"] == "pkg:pypi:fixture-parser@1.1.0"
    assert direct["note"] == "yanked, but pinned exactly (PEP 592)"
    assert "pkg:pypi:fixture-tokens@1.3.0" in targets(
        answer
    )  # transitive, resolved from 1.1.0's declaration


def test_deps_dev_graphs_are_shown_beside_with_differences(world):
    answer = graph(world, "pkg:npm:@fixture-labs/tokenizer", "1.1.0", D2)
    published = answer["published_graphs"][0]
    assert published["source"] == "deps-dev" and published["semantics"].startswith(
        "deps.dev's own resolution"
    )
    assert {d["coordinate"] for d in published["differences"]} == {
        "pkg:npm:fixture-lexer",
        "pkg:npm:fixture-native",
    }


def test_receipts_replay_and_arrival_order_does_not_matter(world):
    answer = graph(world, "pkg:cargo:fixture-codec", "0.1.0", D1)
    assert answer["receipt"]["pinned_revisions"]
    assert DependencyGraphs(world).replay(answer["receipt"], scopes=h.READ)["matched"]
    reversed_world = duckdb.connect()
    h.ingest(reversed_world, 2)
    h.ingest(reversed_world, 1)  # late older data
    for package, version in (
        ("pkg:cargo:fixture-codec", "0.1.0"),
        ("pkg:pypi:fixture-parser", "1.1.0"),
    ):
        for date in (D1, D2):
            a, b = (
                graph(world, package, version, date),
                graph(reversed_world, package, version, date),
            )
            assert (a["nodes"], a["edges"], a["unresolved"]) == (
                b["nodes"],
                b["edges"],
                b["unresolved"],
            )
            assert a["receipt"]["result_digest"] == b["receipt"]["result_digest"]


def test_graph_reads_before_any_source_ran_are_not_ready():
    with pytest.raises(OssStoreError) as caught:
        graph(duckdb.connect(), "pkg:pypi:fixture-parser", "1.1.0", D2)
    assert caught.value.code == "not_ready"
