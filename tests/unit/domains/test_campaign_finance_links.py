"""Filings linked to contests, lobbying records and corporate ownership by citation (#2509)."""

from __future__ import annotations

from src.kb.campaign_finance_identity import CampaignFinanceIdentity
from src.kb.campaign_finance_links import CampaignFinanceLinks
from src.kb.campaign_finance_records import forbidden_keys
from tests.unit import campaign_finance_harness as h


def accepted_world(*, elections=True, lobbying=True, ownership=True):
    conn = h.connection()
    h.load_all(conn)
    if ownership:
        h.load_ownership(conn)
    if lobbying:
        h.load_lobbying(conn)
    if elections:
        h.load_elections(conn)
    identity = CampaignFinanceIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES,
                                ownership_namespace=h.OWN_NS if ownership else None)
    for candidate in proposed["candidates"]:
        records = " ".join(candidate["records"])
        if "99003" in records or "donor-committee:C00999909" in records:
            continue  # leave one county record and the unacquired conduit unreviewed
        identity.review(h.NS, candidate["candidate_id"], "accept", "fixture review", principal_id="rev",
                        scopes=h.REVIEW_SCOPES)
    return conn


def test_contest_links_point_at_filing_revisions_and_independent_expenditures():
    conn = accepted_world()
    result = CampaignFinanceLinks(conn).link_contests(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert result["status"] == "linked" and result["missing_targets"] == []
    us = [link for link in result["links"] if link["subject_key"] == "campaign-finance:fec:candidate:P99000001"]
    units = {tuple(link["basis"]["contest_unit"]) for link in us}
    assert units == {("us-fips-county", "99001")}  # only the accepted county record
    filings = {link["record_key"] for link in us if link["record_key"].startswith("campaign-finance:fec:filing:")}
    assert filings == {"campaign-finance:fec:filing:1500101", "campaign-finance:fec:filing:1500150",
                       "campaign-finance:fec:filing:1500201"}
    assert all(link["filing_revision_id"] == link["record_revision_id"] for link in us if link["record_key"] in filings)
    ies = [link for link in us if ":se:" in link["record_key"]]
    assert {link["basis"]["support_oppose_as_published"] for link in ies} == {"S"}
    assert all(link["filing_key"] and link["filing_revision_id"] for link in ies)
    sam = [link for link in result["links"] if link["subject_key"] == "campaign-finance:fec:candidate:P99000002"]
    assert {link["basis"]["support_oppose_as_published"] for link in sam} == {"O"}
    uk = [link for link in result["links"] if link["subject_key"] == "campaign-finance:ukec:entity:9901"]
    assert {link["record_key"] for link in uk} == {"campaign-finance:ukec:filing:spending:9901:uk-parliamentary-"
                                                   "general-election-2099"}
    assert {tuple(link["basis"]["contest_unit"])[1] for link in uk} == {"E14099901", "E14099902", "W07099903"}
    again = CampaignFinanceLinks(conn).link_contests(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert again["linked"] == 0 and len(again["links"]) == len(result["links"])  # idempotent
    assert forbidden_keys(result) == [] and "not evidence of influence" in result["notice"]


def test_lobbying_and_ownership_links_record_their_basis_and_register_revision():
    conn = accepted_world()
    links = CampaignFinanceLinks(conn)
    lobbying = links.link_lobbying(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert lobbying["status"] == "linked"
    (sample, *rest) = [link for link in lobbying["links"] if link["target_key"] == "lobbying:us-lda:9002-8002"]
    assert sample["record_key"] == "campaign-finance:fec:sa:1500310:4000102"
    assert sample["basis"]["method"] == "name+address" and sample["basis"]["register"] == "us-lda"
    assert sample["target_revision"] == sample["basis"]["register_revision_id"]
    ownership = links.link_ownership(h.NS, h.OWN_NS, principal_id="alice", scopes=h.SCOPES)
    targets = {(link["record_key"], link["target_key"], link["basis"]["method"]) for link in ownership["links"]}
    assert ("campaign-finance:fec:sa:1500310:4000101", "lei:5299EXAMPLEENERGY001", "name+address") in targets
    assert ("campaign-finance:fec:committee:C00999902", "lei:5299EXAMPLEINDUSTR01", "name+address") in targets
    assert ("campaign-finance:ukec:donation:C0990001", "gb-coh:09990002", "company-number") in targets
    energy = next(link for link in ownership["links"] if link["target_key"] == "lei:5299EXAMPLEENERGY001")
    assert energy["filing_key"] == "campaign-finance:fec:filing:1500310" and energy["filing_revision_id"]
    assert all(link["record_revision_id"] for link in ownership["links"])


def test_missing_providers_and_targets_are_reported_not_dropped():
    conn = accepted_world(elections=False, lobbying=False, ownership=False)
    links = CampaignFinanceLinks(conn)
    assert links.link_contests(h.NS, principal_id="a", scopes=h.SCOPES)["status"] == "elections_unavailable"
    assert links.link_lobbying(h.NS, principal_id="a", scopes=h.SCOPES)["status"] == "lobbying_unavailable"
    assert links.link_ownership(h.NS, h.OWN_NS, principal_id="a", scopes=h.SCOPES)["status"] == \
        "ownership_unavailable"
    conn = accepted_world()
    # the accepted Companies House match points at a namespace that does not hold the record
    reported = CampaignFinanceLinks(conn).link_ownership(h.NS, "elsewhere", principal_id="a", scopes=h.SCOPES | {
        "namespace:elsewhere:read"})
    assert reported["links"] == [] and {m["target_key"] for m in reported["missing_targets"]} >= {"gb-coh:09990002"}
