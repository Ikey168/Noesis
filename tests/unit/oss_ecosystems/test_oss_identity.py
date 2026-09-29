"""Repository and organisation identity through reviewable, revertible decisions (OS06)."""

from __future__ import annotations

import pytest

from src.kb.oss_ecosystem_identity import (
    OssIdentity,
    package_key,
    publisher_key,
    repository_key_of,
)
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError
from tests.unit.oss_ecosystems import harness as h


@pytest.fixture()
def conn():
    conn = h.world()
    conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT, entity_type TEXT)"
    )
    conn.execute(
        "INSERT INTO canonical_entities VALUES ('ent:fixture-labs', 'Fixture Labs', 'ORG'), "
        "('ent:ada', 'fixture-labs', 'PERSON')"
    )
    return conn


def _by_pair(candidates):
    return {(c["left_key"], c["right_key"]): c for c in candidates}


def test_repository_candidates_come_from_sources_are_upgraded_by_archive_tags_and_flag_shared_claims(
    conn,
):
    identity = OssIdentity(conn)
    result = identity.propose_repositories(h.NS, principal_id="alice", scopes=h.WRITE)
    pairs = _by_pair(result["candidates"])
    parser = pairs[
        (
            package_key("pkg:pypi:fixture-parser"),
            repository_key_of("github.com/fixture-labs/parser"),
        )
    ]
    assert (
        parser["basis"] == "archive-tag-corroborated" and parser["state"] == "proposed"
    )
    assert {e["tag"] for e in parser["evidence"] if e["kind"] == "archive-tag"} == {
        "refs/tags/v1.0.0",
        "refs/tags/v1.1.0",
        "refs/tags/v2.0.0",
    }
    # A second package claims the same repository: flagged on both, resolved on neither.
    assert parser["shared_claim"] == [
        {"package": "pkg:pypi:fixture-tokens", "state": "proposed"}
    ]
    tokens = pairs[
        (
            package_key("pkg:pypi:fixture-tokens"),
            repository_key_of("github.com/fixture-labs/parser"),
        )
    ]
    assert tokens["shared_claim"][0]["package"] == "pkg:pypi:fixture-parser"
    tokenizer = pairs[
        (
            package_key("pkg:npm:@fixture-labs/tokenizer"),
            repository_key_of("github.com/fixture-labs/tokenizer"),
        )
    ]
    assert (
        tokenizer["basis"] == "registry-declared"
    )  # the registry outranks the deps.dev related project
    assert {e["source"] for e in tokenizer["evidence"]} == {"npm", "deps-dev"}
    again = identity.propose_repositories(h.NS, principal_id="alice", scopes=h.WRITE)
    assert all(o["change"] is None for o in again["offered"])


def test_review_is_an_entity_decision_and_reverts_never_reactivate(conn):
    identity = OssIdentity(conn)
    identity.propose_repositories(h.NS, principal_id="alice", scopes=h.WRITE)
    candidate = identity.candidates(
        h.NS, scopes=h.READ, key=repository_key_of("github.com/fixture-labs/codec")
    )[0]
    accepted = identity.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "crate metadata names it",
        principal_id="rev",
        scopes=h.REVIEW,
    )
    assert accepted["state"] == "accepted" and accepted["decision_id"].startswith(
        "entity-decision:"
    )
    assert [
        r["right_key"]
        for r in identity.reviewed_repositories(h.NS, "pkg:cargo:fixture-codec")
    ] == [repository_key_of("github.com/fixture-labs/codec")]
    assert identity.packages_for_repository(h.NS, "github.com/fixture-labs/codec") == [
        "pkg:cargo:fixture-codec"
    ]
    reverted = identity.revert(
        h.NS,
        candidate["candidate_id"],
        "wrong fork",
        principal_id="rev",
        scopes=h.REVIEW,
    )
    assert (
        reverted["state"] == "reverted"
        and identity.reviewed_repositories(h.NS, "pkg:cargo:fixture-codec") == []
    )
    with pytest.raises(OssStoreError):
        identity.review(
            h.NS,
            candidate["candidate_id"],
            "accept",
            "again",
            principal_id="rev",
            scopes=h.REVIEW,
        )
    decisions = conn.execute(
        "SELECT decision_type FROM entity_identity_decisions ORDER BY created_at_ms"
    ).fetchall()
    assert [d[0] for d in decisions] == ["match", "undo"]
    with pytest.raises(OssStoreError) as caught:
        identity.review(
            h.NS,
            candidate["candidate_id"],
            "accept",
            "x",
            principal_id="rev",
            scopes=h.WRITE,
        )
    assert caught.value.code == "unauthorized"


def test_the_same_name_in_two_ecosystems_is_never_one_match(conn):
    store = OssEcosystemStore(conn)
    store.apply(
        h.NS,
        [
            {
                "record_type": "repository_link_assertion",
                "source": "npm",
                "ecosystem": "npm",
                "package": "fixture-parser",
                "links": [{"url": "https://github.com/other/parser-js"}],
            }
        ],
        run_id="npm-twin",
        scopes=h.WRITE,
        observed_at_ms=h.fb.POLL_MS[2],
    )
    identity = OssIdentity(conn)
    candidates = identity.propose_repositories(
        h.NS, principal_id="alice", scopes=h.WRITE
    )["candidates"]
    npm_twin = [
        c for c in candidates if c["left_key"] == package_key("pkg:npm:fixture-parser")
    ]
    pypi = [
        c for c in candidates if c["left_key"] == package_key("pkg:pypi:fixture-parser")
    ]
    assert [c["right_key"] for c in npm_twin] == [
        repository_key_of("github.com/other/parser-js")
    ]
    assert repository_key_of("github.com/other/parser-js") not in {
        c["right_key"] for c in pypi
    }


def test_organisations_match_only_organisation_entities_through_review(conn):
    identity = OssIdentity(conn)
    result = identity.propose_organisations(h.NS, principal_id="alice", scopes=h.WRITE)
    pairs = {(c["left_key"], c["right_key"]) for c in result["candidates"]}
    assert (
        publisher_key("maven-central", "pom-organisation", "Fixture Labs"),
        "entity:ent:fixture-labs",
    ) in pairs
    assert (
        publisher_key("pypi", "pypi-organisation", "fixture-labs"),
        "entity:ent:fixture-labs",
    ) in pairs
    assert (
        publisher_key("crates-io", "crates-team", "github:fixture-labs:crates"),
        "entity:ent:fixture-labs",
    ) in pairs
    assert not any(
        right == "entity:ent:ada" for _, right in pairs
    )  # a person is never a candidate
    assert not any(
        ":npm-scope:" in left or ":maven-groupid:" in left for left, _ in pairs
    )
    with pytest.raises(OssStoreError) as caught:
        identity.propose_link(
            h.NS,
            left_key=publisher_key("npm", "npm-scope", "@fixture-labs"),
            right_key="entity:ent:ada",
            reason="guess",
            principal_id="alice",
            scopes=h.WRITE,
        )
    assert caught.value.code == "not_an_organisation"
