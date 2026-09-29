"""A bill's stage, text version, sponsors and votes as of a date (#2436)."""

from __future__ import annotations

import pytest

from src.domains.political.legislation_queries import LegislationQueries
from src.kb.legislation import LegislationDossiers, LegislationError, forbidden_keys
from src.kb.legislation_identity import LegislationIdentity
from src.kb.legislation_links import LegislationLinks
from tests.unit import elections_harness as eh
from tests.unit import legislation_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def build(conn, bill):
    return LegislationDossiers(conn, now=h.Clock()).build(h.NS, bill, h.DOSSIER_NS, principal_id="alice",
                                                          scopes=h.SCOPES)


def ask(conn, bill, as_of, **kw):
    return LegislationQueries(conn).bill_as_of(h.NS, bill, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES,
                                               as_of=as_of, **kw)


def test_us_stage_and_text_version_are_selected_by_source_dates_and_named(conn):
    build(conn, h.US_BILL)
    early = ask(conn, h.US_BILL, "2099-02-10")
    assert early["stage"]["latest_action"]["text"] == "Referred to the House Committee on Energy and Commerce."
    assert early["text_version"]["version_code"] == "ih" and early["text_version"]["used"]["revision_id"]
    assert early["votes"]["held"] == []
    later = ask(conn, h.US_BILL, "2099-03-10")
    assert later["stage"]["latest_action"]["action_code"] == "H38310"
    assert later["text_version"]["version_code"] == "rh"
    assert [v["version_code"] for v in later["text_versions_on_record"]] == ["ih", "rh"]
    (house,) = later["votes"]["held"]
    positions = {p["member_id"]: p["position"] for p in house["positions"]}
    assert positions["P009903"] == "Nay" and house["link_basis"] == "procedure_id"
    assert later["stage"]["used"]["provider"] == "congress-gov"
    assert later["selection_basis"].startswith("source-dated records")


def test_sponsors_as_of_a_date_keep_withdrawn_cosponsors_apart(conn):
    build(conn, h.US_BILL)
    during = ask(conn, h.US_BILL, "2099-02-25")
    assert {s["member_id"] for s in during["sponsors"]["current"]} == {"S009901", "E009902", "P009903"}
    after = ask(conn, h.US_BILL, "2099-03-05")
    assert {s["member_id"] for s in after["sponsors"]["current"]} == {"S009901", "E009902"}
    assert [s["member_id"] for s in after["sponsors"]["withdrawn_cosponsors"]] == ["P009903"]


def test_source_disagreements_are_shown_not_resolved(conn):
    build(conn, h.US_BILL)
    answer = ask(conn, h.US_BILL, "2099-03-20")
    fields = {d["field"]: d for d in answer["source_disagreements"]}
    assert set(fields) == {"latest_action", "cosponsors"}
    assert fields["latest_action"]["congress_gov"]["text"] == "Received in the Senate."
    assert fields["latest_action"]["govinfo_billstatus"]["action_date"] == "2099-03-05"
    assert fields["cosponsors"]["resolution"] == "not resolved" and len(fields["cosponsors"]["citations"]) == 2


def test_uk_answer_uses_reviewed_divisions_and_accepted_identities_only(conn):
    eh.apply(conn, "gb", eh.UK)
    build(conn, h.UK_BILL)
    before = ask(conn, h.UK_BILL, "2099-02-20")
    assert before["stage"]["description"] == "Committee stage" and before["royal_assent"] == "not on record"
    assert before["text_version"]["title"].endswith("(as amended in Public Bill Committee)")
    assert before["votes"]["held"] == [] and len(before["votes"]["unlinked_candidates"]) == 1
    store = LegislationDossiers(conn).store
    store.review_link(h.NS, "uk-division:commons-1701", "uk-commons-divisions", h.UK_BILL, "accept",
                      "title names the bill", principal_id="rev", scopes=h.REVIEW_SCOPES)
    identity = LegislationIdentity(conn, now=h.Clock())
    (candidate,) = [c for c in identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)["candidates"]
                    if "legislation:member:uk-parliament:4002" in c["records"]]
    build(conn, h.UK_BILL)
    proposed_only = ask(conn, h.UK_BILL, "2099-02-20")
    (division,) = proposed_only["votes"]["held"]
    sample = next(p for p in division["positions"] if p["member_id"] == "4002")
    assert sample["identity"]["state"] == "unmatched"  # a proposal is never shown as a match
    identity.review(h.NS, candidate["candidate_id"], "accept", "same member", principal_id="rev",
                    scopes=h.REVIEW_SCOPES)
    accepted = ask(conn, h.UK_BILL, "2099-02-20")
    sample = next(p for p in accepted["votes"]["held"][0]["positions"] if p["member_id"] == "4002")
    assert sample["identity"]["state"] == "matched" and sample["position"] == "aye"
    assert division["link_basis"] == "reviewed_assertion"


def test_royal_assent_lobbying_and_enactment_appear_only_when_on_record(conn):
    h.apply(conn, "uk-parliament-bills", v2=True)
    build(conn, h.UK_BILL)
    h.seed_uk_act(conn)
    LegislationLinks(conn).link_enactment(h.NS, h.UK_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    answer = ask(conn, h.UK_BILL, "2099-06-01", lobbying_namespace=h.NS)
    assert answer["royal_assent"] == "published" and answer["stage"]["category"] == "adoption"
    assert answer["lobbying"]["status"] == "unavailable"  # the lobbying feature is absent here
    (enacted,) = answer["enactment"]
    assert enacted["status"] == "linked" and enacted["citation"] == "ukpga/2099/5"
    h.apply_lda(conn)
    build(conn, h.US_BILL)
    LegislationLinks(conn).link_lobbying(h.NS, h.US_BILL, h.DOSSIER_NS, h.NS, principal_id="alice", scopes=h.SCOPES)
    us = ask(conn, h.US_BILL, "2099-06-01", lobbying_namespace=h.NS)
    (disclosure,) = us["lobbying"]["disclosures"]
    assert disclosure["register"] == "us-lda" and disclosure["link_kind"] == "explicit-field"
    assert ask(conn, h.US_BILL, "2099-03-01", lobbying_namespace=h.NS)["lobbying"]["status"] == "none_on_record"


def test_none_on_record_evidence_bundle_and_exclusions(conn):
    missing = ask(conn, "us-bill:156-s-1", "2099-03-01")
    assert missing["status"] == "none_on_record"
    assert ask(conn, h.US_BILL, "2099-03-01")["status"] == "dossier_not_built"
    build(conn, h.US_BILL)
    answer = ask(conn, h.US_BILL, "2099-03-11")
    bundle = answer["evidence_bundle"]
    assertions = bundle["sections"][0]["assertions"]
    assert {a["id"] for a in assertions} >= {"stage", "text-version", "sponsors"}
    ids = {b["id"] for b in bundle["bibliography"]}
    assert all(a["dependencies"][0]["revision"] in ids and a["dependencies"][0]["kind"] == "source"
               for a in assertions)
    assert "fixture evidence" in bundle["bibliography"][0]["text"]
    assert forbidden_keys(answer) == [] and "passage prediction" in answer["exclusions"]
    with pytest.raises(LegislationError):
        ask(conn, h.US_BILL, "not-a-date")


def test_sponsor_lookup_lists_bills_per_record(conn):
    answer = LegislationQueries(conn).sponsor_bills(h.NS, "legislation:member:us-bioguide:S009901", scopes=h.SCOPES)
    assert {(b["record_kind"], b["role"]) for b in answer["bills"]} == {("us-bill", "sponsor"),
                                                                        ("us-bill-status", "sponsor")}
    assert answer["identity"]["state"] == "unmatched"
