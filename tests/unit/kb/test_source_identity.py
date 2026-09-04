from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.source_identity import SourceIdentityError, SourceIdentityStore

READ = {"knowledge:source-identity:read"}
WRITE = {"knowledge:source-identity:write"}
REVIEW = {"knowledge:source-identity:review"}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _source(store, name, kind="publication", native=None, namespace="osint"):
    return store.register(
        namespace,
        kind,
        name,
        principal_id="analyst",
        scopes=WRITE,
        native_ids=native or {},
        generation=3,
        observed_at_ms=20,
    )


def test_deterministic_identity_duplicate_names_rename_and_deleted_account():
    conn = duckdb.connect(":memory:")
    store = SourceIdentityStore(conn, now=lambda: 100)
    first = _source(store, "Daily News", native={"issn": "one"})
    _validate("noesis-source-identity-v1.json", first)
    second = _source(store, "Daily News", native={"issn": "two"})
    assert first["source_id"] != second["source_id"]
    assert _source(store, "Daily News", native={"issn": "one"})["idempotent"]
    renamed = store.revise(
        "osint",
        first["source_id"],
        1,
        principal_id="editor",
        scopes=WRITE,
        display_name="The Daily News",
        names={"en": "The Daily News", "de": "Die Tagesnachrichten"},
    )
    assert renamed["source_id"] == first["source_id"] and renamed["revision"] == 2
    account = _source(
        store, "Channel Account", kind="account", native={"handle": "news"}
    )
    deleted = store.revise(
        "osint",
        account["source_id"],
        1,
        principal_id="editor",
        scopes=WRITE,
        lifecycle="deleted",
    )
    assert deleted["lifecycle"] == "deleted"
    history = store.get("osint", first["source_id"], scopes=READ, include_history=True)
    assert [item["display_name"] for item in history["revisions"]] == [
        "Daily News",
        "The Daily News",
    ]
    conn.close()


def test_ambiguous_shared_alias_rebrand_review_and_reversible_split():
    conn = duckdb.connect(":memory:")
    store = SourceIdentityStore(conn, now=lambda: 100)
    old = _source(store, "Old Outlet", native={"id": "old"})
    new = _source(store, "New Outlet", native={"id": "new"})
    old_link = store.decide_alias(
        "osint",
        old["source_id"],
        "domain",
        "https://www.example.test/path",
        reviewer_id="reviewer",
        scopes=REVIEW,
        reason="Historical archive confirms this domain.",
        confidence=0.8,
        provenance={"citation": "archive:1"},
    )
    _validate("noesis-source-alias-decision-v1.json", old_link)
    store.decide_alias(
        "osint",
        new["source_id"],
        "domain",
        "example.test",
        reviewer_id="reviewer",
        scopes=REVIEW,
        reason="Current masthead confirms the rebrand.",
        provenance={"citation": "masthead:1"},
    )
    ambiguous = store.resolve_alias("osint", "domain", "www.example.test", scopes=READ)
    assert ambiguous["ambiguous"] and len(ambiguous["matches"]) == 2
    split = store.decide_alias(
        "osint",
        old["source_id"],
        "domain",
        "example.test",
        reviewer_id="second-reviewer",
        scopes=REVIEW,
        reason="Review assigns the live domain only to the new outlet.",
        action="split",
    )
    resolved = store.resolve_alias("osint", "domain", "example.test", scopes=READ)
    assert (
        resolved["resolved"] and resolved["matches"][0]["source_id"] == new["source_id"]
    )
    assert split["predecessor_decision_id"] == old_link["decision_id"]
    conn.close()


def test_time_bounded_ownership_indirect_control_conflict_and_uncertainty():
    conn = duckdb.connect(":memory:")
    store = SourceIdentityStore(conn, now=lambda: 100)
    outlet = _source(store, "Outlet")
    old_owner = _source(store, "Old Group", kind="organization", native={"lei": "old"})
    new_owner = _source(store, "New Group", kind="organization", native={"lei": "new"})
    state = _source(store, "State Agency", kind="agency")
    store.relate(
        "osint",
        old_owner["source_id"],
        outlet["source_id"],
        "ownership",
        principal_id="analyst",
        scopes=WRITE,
        valid_from_ms=0,
        valid_to_ms=50,
        confidence=0.8,
        uncertainty=0.2,
        evidence=[{"citation": "registry:old"}],
    )
    current = store.relate(
        "osint",
        new_owner["source_id"],
        outlet["source_id"],
        "ownership",
        principal_id="analyst",
        scopes=WRITE,
        valid_from_ms=50,
        confidence=0.7,
        uncertainty=0.3,
        evidence=[{"citation": "registry:new"}],
    )
    _validate("noesis-source-relationship-v1.json", current)
    store.relate(
        "osint",
        state["source_id"],
        new_owner["source_id"],
        "funding",
        principal_id="analyst",
        scopes=WRITE,
        confidence=0.5,
        uncertainty=0.5,
        evidence=[{"citation": "disclosure:1"}],
    )
    old_dossier = store.dossier("osint", outlet["source_id"], scopes=READ, as_of_ms=25)
    new_dossier = store.dossier("osint", outlet["source_id"], scopes=READ, as_of_ms=75)
    assert old_dossier["relationships"][0]["from_source_id"] == old_owner["source_id"]
    assert (
        new_dossier["relationships"][0]["relationship_id"] == current["relationship_id"]
    )
    path = store.path(
        "osint", state["source_id"], outlet["source_id"], scopes=READ, as_of_ms=75
    )
    assert path["found"] and len(path["path"]) == 2
    assert new_dossier["citation_complete"] is True
    _validate("noesis-source-dossier-v1.json", new_dossier)
    conn.close()


def test_independence_wire_copies_corporate_groups_anonymous_and_incomplete():
    conn = duckdb.connect(":memory:")
    store = SourceIdentityStore(conn)
    wire = _source(store, "Wire")
    copy = _source(store, "Copy")
    sister = _source(store, "Sister")
    owner = _source(store, "Owner", kind="organization")
    anonymous = _source(store, "Anonymous", kind="unknown")
    for left, right, kind in (
        (wire, copy, "syndication"),
        (owner, copy, "ownership"),
        (owner, sister, "ownership"),
    ):
        store.relate(
            "osint",
            left["source_id"],
            right["source_id"],
            kind,
            principal_id="analyst",
            scopes=WRITE,
            evidence=[{"citation": f"graph:{kind}"}],
        )
    result = store.explain_independence(
        "osint",
        [
            wire["source_id"],
            copy["source_id"],
            sister["source_id"],
            anonymous["source_id"],
            "missing",
        ],
        scopes=READ,
    )
    connected = next(
        group for group in result["groups"] if wire["source_id"] in group["source_ids"]
    )
    assert {wire["source_id"], copy["source_id"], sister["source_id"]} <= set(
        connected["source_ids"]
    )
    assert result["complete"] is False
    assert result["anonymous_source_ids"] == [anonymous["source_id"]]
    assert result["missing_source_ids"] == ["missing"]
    _validate("noesis-source-independence-v1.json", result)
    conn.close()


def test_dossier_pagination_cursor_citations_and_authorization():
    conn = duckdb.connect(":memory:")
    store = SourceIdentityStore(conn)
    center = _source(store, "Center")
    others = [
        _source(store, f"Other {index}", native={"id": str(index)})
        for index in range(3)
    ]
    for index, other in enumerate(others):
        store.relate(
            "osint",
            other["source_id"],
            center["source_id"],
            "funding",
            principal_id="analyst",
            scopes=WRITE,
            evidence=[{"citation": f"disclosure:{index}"}],
        )
    first = store.dossier("osint", center["source_id"], scopes=READ, limit=2)
    second = store.dossier(
        "osint", center["source_id"], scopes=READ, limit=2, cursor=first["next_cursor"]
    )
    assert len(first["relationships"]) == 2 and len(second["relationships"]) == 1
    assert first["citation_count"] == 2 and first["citation_complete"]
    with pytest.raises(SourceIdentityError, match="cursor"):
        store.dossier("osint", center["source_id"], scopes=READ, cursor="bad")
    with pytest.raises(SourceIdentityError, match="required scope"):
        store.dossier("osint", center["source_id"], scopes=set())
    conn.close()
