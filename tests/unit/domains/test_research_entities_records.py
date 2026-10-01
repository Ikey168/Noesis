"""Research-entity records: immutable revisions, as-of lookup and the RE01 minimisation guard at write time (#2589)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from src.kb.research_entities_records import (
    RESEARCHER_SCOPE,
    ResearchEntityError,
    ResearchEntityStore,
    forbidden_keys,
    readiness,
)
from tests.unit import research_entities_harness as h

SCHEMA = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-research-entity-record-v1.json").read_text())


def loaded():
    conn = h.connection()
    h.load_all(conn)
    h.load_second(conn)
    return conn, ResearchEntityStore(conn)


def test_every_acquired_record_validates_against_the_contract_and_carries_source_revision_and_as_of():
    validator = Draft202012Validator(SCHEMA)
    for source_id in h.SOURCES:
        for record in h.fetch(source_id, "v2")[0]:
            assert not list(validator.iter_errors(record)), record["record_key"]
            assert record["provider"] and record["native_revision"] and "as_of" in record


def test_organisation_releases_form_a_revision_chain_and_answer_as_of_a_date():
    _conn, store = loaded()
    key = "research-entities:ror:0zznwd303"
    history = store.history(h.NS, key, scopes=h.SCOPES)
    assert [(v["revision_no"], v["change"], v["status"], v["native_revision"]) for v in history] == [
        (1, "new", "active", "release:v9.1"), (2, "revised", "withdrawn", "release:v9.2")]
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    assert store.as_of(h.NS, key, "2099-04-01", scopes=h.SCOPES)["revision_id"] == history[0]["revision_id"]
    assert store.as_of(h.NS, key, "2099-06-01", scopes=h.SCOPES)["revision_id"] == history[1]["revision_id"]
    assert store.as_of(h.NS, key, "2099-02-28", scopes=h.SCOPES) is None  # not yet published
    assert store.as_of(h.NS, key, None, scopes=h.SCOPES)["status"] == "withdrawn"
    unchanged = store.history(h.NS, "research-entities:ror:0zzexa202", scopes=h.SCOPES)
    assert [v["native_revision"] for v in unchanged] == ["release:v9.1", "release:v9.2"]  # release stamp differs
    citation = history[1]["citation"]
    assert citation["release"]["label"] == "v9.2" and citation["source_as_of"] == "2099-06-01"
    assert citation["observed_at_ms"] == h.SECOND_RUN_MS and citation["evidence_origin"] == "fixture"


def test_reacquiring_is_idempotent_and_an_older_response_never_becomes_current():
    conn, store = loaded()
    before = conn.execute("SELECT count(*) FROM research_entity_revisions").fetchone()[0]
    outcome = h.apply(conn, h.ORCID_SOURCE, version="v2", run_id="run:again")
    assert all(set(o["changes"].values()) == {"unchanged"} for o in outcome)
    assert conn.execute("SELECT count(*) FROM research_entity_revisions").fetchone()[0] == before
    h.apply(conn, h.ORCID_SOURCE, version="v1", run_id="run:replay-old")  # already on record: nothing appended
    assert conn.execute("SELECT count(*) FROM research_entity_revisions").fetchone()[0] == before
    record = store.records(h.NS, scopes=h.SCOPES, record_keys=[f"research-entities:orcid:{h.ADA}"])[0]
    assert record["native_revision"] == "2099-05-22T09:00:00+00:00"
    old = copy.deepcopy(record["record"])
    old["fields"]["works"] = old["fields"]["works"][:1]
    old["as_of"] = old["fields"]["last_modified"] = old["native_revision"] = "2099-01-01T00:00:00+00:00"
    old["revision_order"] = "2099-01-01T00:00:00+00:00"
    result = store.project(h.NS, [old], run_id="run:late", source_id=h.ORCID_SOURCE)
    assert result["counts"]["older-observation"] == 1
    assert store.records(h.NS, scopes=h.SCOPES, record_keys=[record["record_key"]])[0]["revision_id"] == \
        record["revision_id"]


def test_removals_by_the_source_are_revisions_never_deletions():
    _conn, store = loaded()
    bo = store.history(h.NS, f"research-entities:orcid:{h.BO}", scopes=h.SCOPES)
    assert [v["status"] for v in bo] == ["active", "deactivated"]
    assert bo[0]["record"]["fields"]["employments"]  # the earlier revision is kept as acquired
    assert store.as_of(h.NS, bo[0]["record_key"], "2099-03-10", scopes=h.SCOPES)["status"] == "active"
    assert store.as_of(h.NS, bo[0]["record_key"], "2099-06-10", scopes=h.SCOPES)["status"] == "deactivated"
    ds2 = store.history(h.NS, f"research-entities:doi:{h.DS2}", scopes=h.SCOPES)
    assert [v["status"] for v in ds2] == ["findable", "unavailable"]
    cordis = store.history(h.NS, f"research-entities:cordis:HORIZON:{h.EXAMPLAR}", scopes=h.SCOPES)
    assert [v["change"] for v in cordis] == ["new", "revised"]
    assert cordis[1]["record"]["fields"]["participants"][1]["ec_contribution"]["amount"] == "750000"
    assert cordis[0]["record"]["fields"]["participants"][1]["ec_contribution"]["amount"] == "800000"


def test_minimisation_is_enforced_at_write_time():
    _conn, store = loaded()
    researcher = store.records(h.NS, scopes=h.SCOPES, kinds=["researcher"])[0]["record"]
    for mutate, path in (
        (lambda r: r["fields"].update(biography="x"), "$.fields.biography"),
        (lambda r: r["fields"].update(emails=["x@example.org"]), "$.fields.emails"),
        (lambda r: r["fields"]["works"][0].update(contributors=["someone"]), "$.fields.works[0].contributors"),
        (lambda r: r["fields"]["name"].update(other_names=["x"]), "$.fields.name.other_names"),
    ):
        bad = copy.deepcopy(researcher)
        mutate(bad)
        with pytest.raises(ResearchEntityError) as caught:
            store.project(h.NS, [bad], run_id="run:bad", source_id=h.ORCID_SOURCE)
        assert caught.value.code == "minimisation_violation" and path in caught.value.details["paths"]
    dataset = store.records(h.NS, scopes=h.SCOPES, kinds=["dataset"])[0]["record"]
    bad = copy.deepcopy(dataset)
    bad["fields"]["creators"][0]["given_name"] = "Ada"
    with pytest.raises(ResearchEntityError):
        store.project(h.NS, [bad], run_id="run:bad", source_id=h.DATACITE_SOURCE)
    project = store.records(h.NS, scopes=h.SCOPES, kinds=["project"])[0]["record"]
    bad = copy.deepcopy(project)
    bad["fields"]["participants"][0]["street"] = "1 Example Street"
    with pytest.raises(ResearchEntityError):
        store.project(h.NS, [bad], run_id="run:bad", source_id=h.CORDIS_SOURCE)
    bad = copy.deepcopy(project)
    bad["fields"]["score"] = 1
    with pytest.raises(ResearchEntityError):
        store.project(h.NS, [bad], run_id="run:bad", source_id=h.CORDIS_SOURCE)


def test_researcher_records_need_the_researcher_scope_and_can_be_redacted():
    _conn, store = loaded()
    assert not store.records(h.NS, scopes=h.NO_RESEARCHERS, kinds=["researcher"])
    assert store.withheld_researchers(h.NS, scopes=h.NO_RESEARCHERS) == 3
    assert len(store.records(h.NS, scopes=h.SCOPES, kinds=["researcher"])) == 3
    key = f"research-entities:orcid:{h.ADA}"
    revision = store.records(h.NS, scopes=h.SCOPES, record_keys=[key])[0]["revision_id"]
    with pytest.raises(ResearchEntityError):
        store.revision(h.NS, revision, scopes=h.NO_RESEARCHERS)
    with pytest.raises(ResearchEntityError):
        store.redact_researcher(h.NS, key, "holder request", principal_id="op", scopes=h.NO_RESEARCHERS)
    redaction = store.redact_researcher(h.NS, key, "holder request (fictional)", principal_id="op", scopes=h.SCOPES)
    assert len(redaction["revisions"]) == 2
    for view in store.history(h.NS, key, scopes=h.SCOPES):
        assert view["redacted"] and view["record"]["fields"]["name"] is None
        assert "Exampla\"" not in json.dumps(view["record"]["fields"]["name"])
    assert RESEARCHER_SCOPE == h.RESEARCHERS


def test_reads_are_scoped_and_readiness_reports_the_degraded_orcid_source():
    conn, store = loaded()
    with pytest.raises(ResearchEntityError):
        store.records(h.NS, scopes={"knowledge:science:research-entities:read"})
    report = readiness(conn)
    assert report["store_ready"] is True
    assert report["providers"]["ror"]["records"] == 5 and report["providers"]["cordis"]["records"] == 2
    assert "NOESIS_ORCID_PUBLIC_TOKEN" in report["providers"]["orcid"]["degraded"]
    assert {f["selected"] for f in report["features"].values()} == {False}
    assert report["providers"]["openaire-graph"]["access_decision"] == "documented-not-acquired"
    assert not forbidden_keys(store.records(h.NS, scopes=h.SCOPES))
    assert len(store.receipts(h.NS, scopes=h.SCOPES)) >= 8
