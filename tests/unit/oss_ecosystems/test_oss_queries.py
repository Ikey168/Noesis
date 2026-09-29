"""Release-state, licence-change and organisation queries as of a date (OS08)."""

from __future__ import annotations

import duckdb
import pytest

from src.kb.oss_ecosystem_identity import OssIdentity, publisher_key
from src.kb.oss_ecosystem_queries import (
    licence_history,
    package_release_history,
    packages_by_organisation,
)
from src.kb.oss_ecosystem_store import OssStoreError
from tests.unit.oss_ecosystems import fixture_builder as fb
from tests.unit.oss_ecosystems import harness as h


@pytest.fixture(scope="module")
def world():
    return h.world()


def states(answer):
    return {r["version"]: (r["state"], r.get("reason")) for r in answer["releases"]}


def test_release_history_states_each_release_on_a_date_with_verbatim_reasons(world):
    before = package_release_history(
        world,
        h.NS,
        "fixture-parser",
        ecosystem="pypi",
        scopes=h.READ,
        as_of=fb.DATES["d1"],
    )
    after = package_release_history(
        world,
        h.NS,
        "fixture-parser",
        ecosystem="pypi",
        scopes=h.READ,
        as_of=fb.DATES["d2"],
        include_revisions=True,
    )
    assert states(before) == {
        "1.0.0": ("published", None),
        "1.1.0": ("published", None),
    }
    assert states(after)["1.1.0"] == ("yanked", "Broken wheel metadata; use 1.0.0")
    assert states(after)["2.0.0"] == ("published", None)
    assert [
        r["state"]
        for r in next(x for x in after["releases"] if x["version"] == "1.1.0")[
            "revisions"
        ]
    ] == ["published", "yanked"]
    assert (
        before["knowledge_cutoff"] == "2026-04-01T23:59:59.999Z"
        and before["generation"] != after["generation"]
    )
    assert after["gaps"][-1]["note"] == "no observation of this package in this period"
    npm = package_release_history(
        world,
        h.NS,
        "pkg:npm:@fixture-labs/tokenizer",
        scopes=h.READ,
        as_of=fb.DATES["d2"],
    )
    assert states(npm)["0.9.0"][0] == "unpublished" and states(npm)["1.1.0"] == (
        "deprecated",
        "Tokenizer bug; upgrade to 1.2.0",
    )
    assert set(npm["other_sources"]) == {"deps-dev"}
    assert "notice" in npm and "verdict" in npm["notice"]


def test_release_state_before_the_first_observation_is_a_gap(world):
    answer = package_release_history(
        world,
        h.NS,
        "fixture-tokens",
        ecosystem="pypi",
        scopes=h.READ,
        as_of="2026-02-15",
        acquired_by=fb.DATES["d2"],
    )
    first = next(r for r in answer["releases"] if r["version"] == "1.0.0")
    assert first["state"] == "published" and first["gap"]["note"].startswith(
        "state before the first observation"
    )
    assert any(g.get("version") == "1.0.0" for g in answer["gaps"])


def test_licence_history_highlights_changes_with_the_list_version_and_source_disagreements(
    world,
):
    pypi = licence_history(
        world, h.NS, "fixture-parser", ecosystem="pypi", scopes=h.READ
    )
    assert [
        (c["from_version"], c["to_version"], c["from"], c["to"])
        for c in pypi["changes"]
    ] == [("1.1.0", "2.0.0", "MIT", "BUSL-1.1")]  # permissive to source-available
    assert (
        pypi["spdx_list_version"] == "3.25"
        and pypi["changes"][0]["spdx_list_version"] == "3.25"
    )
    cargo = licence_history(
        world, h.NS, "fixture-codec", ecosystem="cargo", scopes=h.READ
    )
    assert (
        cargo["changes"] == []
    )  # "MIT/Apache-2.0" and "MIT OR Apache-2.0" compare equal
    npm = licence_history(world, h.NS, "pkg:npm:@fixture-labs/tokenizer", scopes=h.READ)
    assert npm["source_disagreements"] == [
        {"version": "1.1.0", "deps-dev": "Apache-2.0", "npm": "MIT"}
    ]
    maven = licence_history(
        world, h.NS, "pkg:maven:org.fixturelabs:fixture-core", scopes=h.READ
    )
    assert [(c["from"], c["to"]) for c in maven["changes"]] == [
        ("Apache-2.0", "EPL-2.0")
    ]
    with pytest.raises(OssStoreError):
        licence_history(
            world,
            h.NS,
            "fixture-parser",
            ecosystem="pypi",
            scopes=h.READ,
            list_version="9.99",
        )


def test_packages_by_organisation_never_list_an_individual_or_an_unreviewed_npm_scope(
    world,
):
    answer = packages_by_organisation(world, h.NS, "fixture-labs", scopes=h.READ)
    assert {p["package"] for p in answer["packages"]} == {
        "pkg:pypi:fixture-parser",
        "pkg:pypi:fixture-tokens",
        "pkg:cargo:fixture-codec",
        "pkg:cargo:fixture-bytes",
    }
    scope = packages_by_organisation(world, h.NS, "@fixture-labs", scopes=h.READ)
    assert (
        scope["packages"] == []
        and scope["excluded"][0]["package"] == "pkg:npm:@fixture-labs/tokenizer"
    )


def test_organisation_queries_follow_reviewed_entity_matches():
    conn = h.world()
    conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT, "
        "entity_type TEXT)"
    )
    conn.execute(
        "INSERT INTO canonical_entities VALUES ('ent:fixture-labs', 'Fixture Labs', 'ORG')"
    )
    identity = OssIdentity(conn)
    identity.propose_organisations(h.NS, principal_id="alice", scopes=h.WRITE)
    scope = identity.propose_link(
        h.NS,
        left_key=publisher_key("npm", "npm-scope", "@fixture-labs"),
        right_key="entity:ent:fixture-labs",
        reason="npm org page links the company",
        principal_id="alice",
        scopes=h.WRITE,
    )
    identity.review(
        h.NS,
        scope["candidate_id"],
        "accept",
        "confirmed",
        principal_id="rev",
        scopes=h.REVIEW,
    )
    answer = packages_by_organisation(
        conn, h.NS, "entity:ent:fixture-labs", scopes=h.READ
    )
    assert [p["package"] for p in answer["packages"]] == [
        "pkg:npm:@fixture-labs/tokenizer"
    ]


def test_queries_before_any_run_are_not_ready():
    conn = duckdb.connect()
    for call in (
        lambda: package_release_history(
            conn, h.NS, "x", ecosystem="pypi", scopes=h.READ
        ),
        lambda: licence_history(conn, h.NS, "x", ecosystem="pypi", scopes=h.READ),
        lambda: packages_by_organisation(conn, h.NS, "x", scopes=h.READ),
    ):
        with pytest.raises(OssStoreError) as caught:
            call()
        assert caught.value.code == "not_ready"
