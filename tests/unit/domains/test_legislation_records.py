"""US and UK bill records mapped onto the existing legislative dossier model (#2395)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from src.domains.political import legislation_mapping as mapping
from src.domains.political.legislative_dossiers import LegislativeDossierStore
from src.kb.legislation import LegislationDossiers, LegislationError, LegislationStore
from tests.unit import legislation_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def schema(name):
    return Draft202012Validator(json.loads((h.ROOT / "contracts/schemas/jsonschema" / name).read_text()))


def test_official_identifiers_and_stage_categories():
    assert mapping.us_bill_key(156, "HR", "9901") == "us-bill:156-hr-9901"
    assert mapping.uk_bill_key("3901") == "uk-bill:3901"
    with pytest.raises(mapping.LegislationMappingError):
        mapping.us_bill_key(156, "amdt", 1)
    assert [mapping.version_stage(c) for c in ("ih", "rh", "eh", "enr")] == [
        "proposal", "amendment", "amendment", "adoption"]
    assert mapping.uk_stage_category("Royal Assent") == "adoption"
    assert mapping.uk_stage_category("Committee stage") == "amendment"
    assert mapping.uk_stage_category("2nd reading") == "proposal"


def test_records_validate_against_the_record_contract(conn):
    validator = schema("noesis-legislation-record-v1.json")
    rows = LegislationStore(conn).records(h.NS, scopes=h.SCOPES)
    assert len(rows) == 18
    for row in rows:
        validator.validate(row["record"])


def test_a_us_bill_is_one_dossier_in_the_existing_store_with_cited_record_revisions(conn):
    dossiers = LegislationDossiers(conn, now=h.Clock())
    saved = dossiers.build(h.NS, h.US_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    schema("noesis-legislative-dossier-v1.json").validate(saved)
    assert saved["jurisdiction"] == "US" and saved["procedure_id"] == h.US_BILL
    kinds = sorted(s["legislation"]["record_kind"] for s in saved["stages"])
    assert kinds == ["us-bill", "us-bill-status", "us-roll-call", "us-roll-call", "us-text-version",
                     "us-text-version", "us-text-version"]
    version = next(s for s in saved["stages"] if s["legislation"]["record_key"].endswith("ih"))
    assert version["stage"] == "proposal" and version["legislation"]["fields"]["version_code"] == "ih"
    assert all(s["citation"]["source_id"] in mapping.LEGISLATION_SOURCES for s in saved["stages"])
    assert saved["evidence_origin"] == "fixture"
    # the same dossier table as Bundestag and EUR-Lex dossiers: no parallel store
    assert conn.execute("SELECT count(*) FROM legislative_dossiers").fetchone()[0] == 1
    again = dossiers.build(h.NS, h.US_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    assert again["idempotent"] and again["revision"] == 1
    h.apply(conn, "us-congress-gov-bills", v2=True)
    revised = dossiers.build(h.NS, h.US_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    assert revised["revision"] == 2
    change = LegislativeDossierStore(conn).compare(
        h.DOSSIER_NS, saved["dossier_id"], 1, 2, principal_id="alice",
        scopes=h.SCOPES | LegislationDossiers.document_scopes(
            LegislationStore(conn).records(h.NS, scopes=h.SCOPES)))
    assert change["counts"]["changed"] == 1 and change["interpretation"] is None


def test_uk_divisions_and_debates_stay_candidates_until_a_reviewer_accepts(conn):
    dossiers = LegislationDossiers(conn, now=h.Clock())
    saved = dossiers.build(h.NS, h.UK_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    assert saved["jurisdiction"] == "GB"
    assert sorted(c["legislation"]["record_kind"] for c in saved["review_candidates"]) == [
        "uk-debate-reference", "uk-division", "uk-division"]
    assert "vote" in saved["missing_stages"]
    store = dossiers.store
    candidates = store.link_candidates(h.NS, h.UK_BILL, scopes=h.SCOPES)
    assert {c["state"] for c in candidates} == {"candidate"}
    with pytest.raises(LegislationError) as denied:
        store.review_link(h.NS, "uk-division:commons-1701", "uk-commons-divisions", h.UK_BILL, "accept",
                          "division title names the bill", principal_id="rev", scopes=h.SCOPES)
    assert denied.value.code == "unauthorized"
    accepted = store.review_link(h.NS, "uk-division:commons-1701", "uk-commons-divisions", h.UK_BILL, "accept",
                                 "the division title names the bill's second reading", principal_id="rev",
                                 scopes=h.REVIEW_SCOPES, evidence={"title": "Example Heat Networks Bill: Second Reading"})
    assert accepted["state"] == "accepted" and accepted["history"][-1]["by"] == "rev"
    store.review_link(h.NS, "uk-debate:ABCD1234-0000-4000-8000-000000000001", "uk-hansard-debates", h.UK_BILL,
                      "accept", "the section title is the bill", principal_id="rev", scopes=h.REVIEW_SCOPES)
    linked = dossiers.build(h.NS, h.UK_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    reviewed = [s for s in linked["stages"] if s.get("link_basis") == "reviewed_assertion"]
    assert {s["stage"] for s in reviewed} == {"vote", "debate"}
    assert reviewed[0]["link_review"]["reviewer"] == "rev"
    assert "vote" not in linked["missing_stages"] and "debate" not in linked["missing_stages"]
    store.revert_link(h.NS, accepted["review_id"], "wrong division", principal_id="rev", scopes=h.REVIEW_SCOPES)
    reverted = dossiers.build(h.NS, h.UK_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    assert any(c["legislation"]["record_key"] == "uk-division:commons-1701" for c in reverted["review_candidates"])


def test_a_bill_with_no_record_is_reported(conn):
    with pytest.raises(LegislationError) as none:
        LegislationDossiers(conn).build(h.NS, "us-bill:156-s-1", h.DOSSIER_NS, principal_id="alice",
                                        scopes=h.SCOPES)
    assert none.value.code == "none_on_record"
