"""Docket, docket-entry, opinion, party and justice-statistic records on the Legal work model (#2385)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft202012Validator

from src.kb.courts_justice import CourtsJusticeError, CourtsJusticeProjector
from src.kb.justice_statistics import JusticeStatisticsStore
from src.kb.legal import LegalStore
from src.kb.legal_dockets import LegalDocketStore
from tests.unit import courts_justice_harness as h

SCOPES = {"knowledge:legal:read", "namespace:global:read"}


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def all_records():
    return [item["court_justice_record"] for source_id in h.SOURCES for page in h.fetch(source_id)
            for item in page.records]


def test_records_validate_and_round_trip_through_json():
    validator = Draft202012Validator(json.loads(
        (h.ROOT / "contracts/schemas/jsonschema/noesis-court-justice-record-v1.json").read_text()))
    records = all_records()
    assert {r["record_kind"] for r in records} == {"docket", "opinion-cluster", "statistics-release"}
    for record in records:
        validator.validate(record)
        assert json.loads(json.dumps(record)) == record
    bad = copy.deepcopy(next(r for r in records if r["record_kind"] == "docket"))
    bad["fields"]["parties"][1]["name_as_published"] = "Jane Roe"
    assert list(validator.iter_errors(bad))
    scored = copy.deepcopy(records[0])
    scored["fields"]["outcome_label"] = "won"
    assert list(validator.iter_errors(scored))
    pack = json.loads((h.ROOT / "packs/legal/pack.json").read_text())
    assert pack["schema_versions"]["court-justice-record"] == "1.0.0"


def test_dockets_and_opinions_are_legal_works_expressions_and_versions(conn):
    legal = LegalStore(conn)
    found = legal.lookup(h.NS, scopes=SCOPES, identifier="1:99-cv-00101")
    assert found["status"] == "found"
    docket = legal.inspect(h.NS, found["works"][0]["work_id"], scopes=SCOPES)
    assert docket["work_kind"] == "docket" and docket["provider"] == "courtlistener"
    assert docket["identifiers"]["court_id"] == "dcd" and docket["title"] == "Example Data Co. v. Roe"
    decision = legal.lookup(h.NS, scopes=SCOPES, identifier="990 F.4th 12")
    work = legal.inspect(h.NS, decision["works"][0]["work_id"], scopes=SCOPES)
    assert work["work_kind"] == "decision" and work["versions"][0]["content_coverage"] == "captured-text"
    passages = legal.passages(h.NS, work["versions"][0]["version_id"], scopes=SCOPES, contains="AFFIRMED")
    assert passages["passages"][0]["locator"] == {"opinion_id": 90002, "paragraph": 3}
    selection = legal.select_as_of(h.NS, work["work_id"], "2098-06-01", scopes=SCOPES)
    assert selection["status"] == "selected"


def test_parties_are_stored_under_the_minimisation_decision_only(conn):
    rows = conn.execute("SELECT party_type, name_as_published, pseudonym, roles_json, party_key "
                        "FROM legal_docket_parties ORDER BY ordinal").fetchall()
    assert rows[1] == ("natural_person", None, "natural person 1 (Defendant)", '["Defendant"]', None)
    assert "Jane Roe" not in json.dumps(conn.execute("SELECT * FROM legal_docket_parties").fetchall())
    record = copy.deepcopy(next(r for r in all_records() if r["record_kind"] == "docket"))
    record["fields"]["parties"][1]["name_as_published"] = "Jane Roe"
    record["fields"]["date_modified"] = "2099-09-09T00:00:00Z"
    with pytest.raises(CourtsJusticeError) as caught:
        CourtsJusticeProjector(conn).project(h.NS, [record], run_id="bad", source_id="courtlistener-dockets")
    assert caught.value.code == "minimisation_violation"


def test_outcomes_are_quoted_disposition_text_only(conn):
    decision = LegalDocketStore(conn).decision_as_of(h.NS, h.CLUSTER, "2099-07-01")
    assert decision["disposition"]["quoted"] == "GRANTED in part and DENIED in part"
    assert decision["disposition"]["locator"]["field"] == "disposition"
    columns = {r[1] for r in conn.execute("SELECT * FROM information_schema.columns WHERE table_name IN "
                                          "('legal_opinion_revisions','legal_docket_revisions')").fetchall()}
    assert not {"outcome", "outcome_label", "winner", "prevailing_party"} & columns


def test_statistic_definitions_observations_and_vintages_carry_source_revision_and_as_of(conn):
    stats = JusticeStatisticsStore(conn)
    vintage = next(v for v in stats.vintages(h.NS) if v["provider"] == "eurostat")
    assert vintage["release_label"] == "2099-04-01T11:00:00+0200" and vintage["source_id"] == "eurostat-crime-iccs"
    assert vintage["observed_at_ms"] > 0 and vintage["evidence_origin"] == "fixture"
    (definition,) = stats.definition_revisions(h.NS, "eurostat", "ICCS", "ICCS0401")
    assert definition["label"] == "Robbery" and definition["revision_no"] == 1
    source = h.source("fbi-cde-summarized")
    changed = copy.deepcopy(source)
    changed["courts_justice"]["selection"]["series"][0]["definition"]["text"] = "A revised definition text."
    h.apply(conn, "fbi-cde-summarized", item=changed, run_id="run:definition-change")
    revisions = stats.definition_revisions(h.NS, "fbi-cde", "UCR-SRS", "burglary")
    assert [d["revision_no"] for d in revisions] == [1, 2]
