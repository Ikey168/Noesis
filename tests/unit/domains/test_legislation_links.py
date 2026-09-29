"""Bill dossiers linked to lobbying disclosures and enacted Legal works by citation (#2431)."""

from __future__ import annotations

import pytest

from src.kb.legislation import LegislationDossiers, LegislationError
from src.kb.legislation_links import LegislationLinks, enactment_citations
from src.kb.lobbying_links import LobbyingDossierLinks
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


def test_lda_disclosure_naming_the_bill_is_linked_and_other_bills_are_reported(conn):
    h.apply_lda(conn)
    dossier = build(conn, h.US_BILL)
    result = LegislationLinks(conn, now=h.Clock()).link_lobbying(
        h.NS, h.US_BILL, h.DOSSIER_NS, h.NS, principal_id="alice", scopes=h.SCOPES)
    assert result["status"] == "linked" and result["dossier_revision"] == dossier["revision"]
    explicit = [link for link in result["links"] if link["basis"] == "explicit-field"]
    assert {link["reference"]["key"] for link in explicit} == {h.US_BILL}
    assert all(link["dossier_revision"] == dossier["revision"] and link["revision_id"] for link in explicit)
    assert explicit[0]["reference"]["congress_basis"] == "filing year"
    # a disclosure sharing only title words is a reviewable candidate, never a link
    candidates = [link for link in result["links"] if link["state"] == "candidate"]
    assert candidates and all(link["basis"] == "discovery" for link in candidates)
    (missing,) = result["missing_targets"]
    assert missing["reference"] == "us-bill:156-s-9950" and missing["status"] == "missing_target"
    assert "influence" in result["notice"]


def test_uk_links_are_reviewer_assertions_only(conn):
    h.apply_lda(conn)
    build(conn, h.UK_BILL)
    result = LegislationLinks(conn).link_lobbying(h.NS, h.UK_BILL, h.DOSSIER_NS, h.NS, principal_id="alice",
                                                  scopes=h.SCOPES)
    assert result["status"] == "none_on_record" and result["links"] == []


def test_lobbying_links_degrade_when_the_lobbying_store_is_absent(conn):
    build(conn, h.US_BILL)
    result = LegislationLinks(conn).link_lobbying(h.NS, h.US_BILL, h.DOSSIER_NS, h.NS, principal_id="alice",
                                                  scopes=h.SCOPES)
    assert result["status"] == "lobbying_unavailable"


def test_enactment_links_follow_published_citations_and_report_missing_targets(conn):
    build(conn, h.UK_BILL)
    links = LegislationLinks(conn, now=h.Clock())
    assert links.link_enactment(h.NS, h.UK_BILL, h.DOSSIER_NS, principal_id="alice",
                                scopes=h.SCOPES)["status"] == "none_on_record"  # no Act published yet
    h.apply(conn, "uk-parliament-bills", v2=True)
    h.apply(conn, "us-congress-gov-bills", v2=True)
    uk = build(conn, h.UK_BILL)
    unavailable = links.link_enactment(h.NS, h.UK_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    assert [link["status"] for link in unavailable["links"]] == ["legal_unavailable"]
    work_id = h.seed_uk_act(conn)
    result = links.link_enactment(h.NS, h.UK_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    (link,) = [x for x in result["links"] if x["status"] == "linked"]
    assert link["work_id"] == work_id and link["citation"] == "ukpga/2099/5" and link["relation"] == "enacted_as"
    assert link["record_key"] == "uk-publication:3901-40003" and link["dossier_revision"] == uk["revision"]
    assert link["record_revision_id"] and "legislation.gov.uk" in link["citation_basis"]
    assert link["legal_link_id"]
    from src.kb.legal import LegalStore

    procedure = LegalStore(conn).inspect(h.DOSSIER_NS, work_id, scopes=h.SCOPES)["procedure_links"]
    assert procedure[0]["dossier_id"] == uk["dossier_id"] and procedure[0]["relation"] == "enacted_as"
    build(conn, h.US_BILL)
    us = links.link_enactment(h.NS, h.US_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    (missing,) = us["links"]
    assert missing["citation"] == "Pub. L. 156-12" and missing["status"] == "missing_target"
    assert missing["evidence"]["lookup_status"] == "not_covered"


def test_citations_come_only_from_published_fields():
    record = {"record_kind": "us-bill", "provider": "congress-gov",
              "fields": {"laws": [{"type": "Public Law", "number": "156-12", "citation": "Pub. L. 156-12"}]}}
    (cited,) = enactment_citations(record)
    assert "Public Law 156-12" in cited["forms"]
    bill = {"record_kind": "uk-publication", "provider": "uk-bills",
            "fields": {"publication_type": "Bill", "links": [{"url": "https://www.legislation.gov.uk/ukpga/2099/5"}]}}
    assert enactment_citations(bill) == []  # only an Act publication is an enactment citation


def test_a_dossier_must_exist_before_linking(conn):
    with pytest.raises(LegislationError) as missing:
        LegislationLinks(conn).link_enactment(h.NS, h.US_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)
    assert missing.value.code == "dossier_not_found"
    assert LobbyingDossierLinks  # the lobbying links store is reused, not copied
