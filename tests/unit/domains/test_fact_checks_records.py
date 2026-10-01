"""Fact-check and publisher records: immutable revisions, removals as revisions and as-of lookup (#2670, FC02)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.kb.fact_checks_records import FactCheckError, FactChecksStore, forbidden_keys
from tests.unit import fact_checks_harness as h

ROOT = Path(__file__).resolve().parents[3]


def seals_key(store):
    (row,) = [r for r in store.records(h.NS, scopes=h.SCOPES, providers=["datacommons"])
              if r["publisher_site"] == "factcheck.example.org"]
    return row["record_key"]


def loaded(version="v1"):
    conn = h.connection()
    h.load_all(conn)
    if version == "v2":
        h.load_all(conn, version="v2", run_id="run:v2")
    return conn, FactChecksStore(conn)


def test_every_record_validates_against_the_contract_and_carries_source_revision_and_as_of_time():
    _, store = loaded()
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-fact-check-record-v1.json").read_text())
    validator = Draft202012Validator(schema)
    rows = store.records(h.NS, scopes=h.SCOPES)
    assert len(rows) == 4 + 3 + 3  # Google, Data Commons and IFCN
    for row in rows:
        assert not list(validator.iter_errors(row["record"])), row["record_key"]
        citation = row["citation"]
        assert citation["source_id"] and citation["revision_id"] and citation["observed_at_ms"]
        assert citation["evidence_origin"] == "fixture"
    assert forbidden_keys([r["record"] for r in rows]) == []


def test_publisher_updates_are_new_revisions_and_replays_add_nothing():
    conn, store = loaded("v2")
    key = seals_key(store)
    history = store.history(h.NS, key, scopes=h.SCOPES)
    by_source = {}
    for revision in history:
        by_source.setdefault(revision["source_id"], []).append(revision)
    for source in (h.GOOGLE, h.DATACOMMONS):
        first, second = by_source[source]
        assert (first["change"], second["change"]) == ("new", "revised")
        assert second["previous_revision_id"] == first["revision_id"]
        assert first["record"]["fields"]["claims"][0]["rating"]["textual_rating"] == "False"
        assert second["record"]["fields"]["claims"][0]["rating"]["textual_rating"] == "Mostly false"
    assert by_source[h.DATACOMMONS][1]["vintage"].startswith("2025-07-01")
    outcomes = h.apply(conn, h.DATACOMMONS, version="v2", run_id="run:again")
    assert outcomes[0]["counts"]["unchanged"] == 2 and outcomes[0]["counts"]["revised"] == 0
    assert len(store.history(h.NS, key, scopes=h.SCOPES)) == len(history)
    # replaying the older release adds nothing new for records already on record, and removes nothing
    h.apply(conn, h.DATACOMMONS, version="v1", run_id="run:old-release")
    replayed = store.history(h.NS, key, scopes=h.SCOPES, source_id=h.DATACOMMONS)
    assert replayed[-1]["change"] in {"older-observation", "revised"}
    assert store.records(h.NS, scopes=h.SCOPES, record_keys=[key], providers=["datacommons"])[0]["revision_no"] >= 2


def test_removals_by_the_source_are_revisions_never_deletions():
    _, store = loaded("v2")
    grid = [r for r in store.records(h.NS, scopes=h.SCOPES, providers=["datacommons"])
            if r["publisher_site"] == "claimwatch.example.com"]
    assert [r["status"] for r in grid] == ["absent-from-release"]
    history = store.history(h.NS, grid[0]["record_key"], scopes=h.SCOPES)
    assert [r["change"] for r in history] == ["new", "absent"]
    assert history[0]["record"]["fields"]["claims"][0]["rating"]["textual_rating"] == "Correct"  # still on record
    claimwatch = store.history(h.NS, "fact-checks:ifcn:claimwatch-example", scopes=h.SCOPES)
    assert [r["status"] for r in claimwatch] == ["published", "absent-from-listing"]
    # Google search results that stop appearing are never removals
    assert all(r["status"] == "published" for r in store.records(h.NS, scopes=h.SCOPES,
                                                                providers=["google-fact-check-tools"]))


def test_ifcn_status_changes_are_dated_revisions_and_as_of_returns_the_status_in_effect():
    _, store = loaded("v2")
    key = "fact-checks:ifcn:verifica-example"
    history = store.history(h.NS, key, scopes=h.SCOPES)
    assert [r["record"]["fields"]["status_as_published"] for r in history] == ["Under renewal", "Verified"]
    assert [r["effective_on"] for r in history] == ["2025-02-01", "2025-08-15"]
    assert store.as_of(h.NS, key, "2025-03-12", scopes=h.SCOPES)[0]["record"]["fields"]["status_as_published"] \
        == "Under renewal"
    assert store.as_of(h.NS, key, "2025-09-01", scopes=h.SCOPES)[0]["record"]["fields"]["status_as_published"] \
        == "Verified"
    assert store.as_of(h.NS, key, "2025-01-01", scopes=h.SCOPES) == []


def test_as_of_lookup_returns_each_sources_revision_published_by_the_date():
    _, store = loaded("v2")
    key = seals_key(store)
    before = {r["source_id"]: r for r in store.as_of(h.NS, key, "2025-04-01", scopes=h.SCOPES)}
    after = {r["source_id"]: r for r in store.as_of(h.NS, key, "2025-12-31", scopes=h.SCOPES)}
    assert set(before) == set(after) == {h.GOOGLE, h.DATACOMMONS}
    assert all(r["record"]["fields"]["claims"][0]["rating"]["textual_rating"] == "False" for r in before.values())
    assert all(r["record"]["fields"]["claims"][0]["rating"]["textual_rating"] == "Mostly false"
               for r in after.values())
    assert store.as_of(h.NS, key, "2025-03-01", scopes=h.SCOPES) == []  # not yet published


def test_minimisation_and_verdict_fields_are_refused_at_write_time():
    _, store = loaded()
    (row,) = [r for r in store.records(h.NS, scopes=h.SCOPES, providers=["datacommons"])
              if r["publisher_site"] == "factcheck.example.org"]
    for mutate in (lambda r: r["fields"]["claims"][0].update(image="https://x.example/p.jpg"),
                   lambda r: r["fields"].update(reviewer_name="Reviewer Placeholder"),
                   lambda r: r["fields"]["claims"][0]["rating"].update(normalised_rating="false"),
                   lambda r: r["fields"].update(verdict="false")):
        record = copy.deepcopy(row["record"])
        mutate(record)
        with pytest.raises(FactCheckError) as refused:
            store.project(h.NS, [record], run_id="bad", source_id=h.DATACOMMONS)
        assert refused.value.code == "minimisation_violation"
    assert len(store.history(h.NS, row["record_key"], scopes=h.SCOPES, source_id=h.DATACOMMONS)) == 1
    missing = copy.deepcopy(row["record"])
    del missing["fields"]["claims"][0]["rating"]
    with pytest.raises(FactCheckError, match="rating as published"):
        store.project(h.NS, [missing], run_id="bad", source_id=h.DATACOMMONS)


def test_reads_need_the_read_scope_and_namespace_access():
    _, store = loaded()
    with pytest.raises(FactCheckError) as refused:
        store.records(h.NS, scopes={"knowledge:news:fact-checks:read"})
    assert refused.value.code == "unauthorized"
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["minimisation"] == "fact-checks-minimisation-v1" for r in receipts)
