"""Classification and restriction status as of a date, history and substance dossiers (CH09, #2306)."""

from __future__ import annotations

import json

import jsonschema
import pytest

from src.kb.substances_identity import SubstanceIdentity
from src.kb.substances_links import SubstanceLinks
from src.kb.substances_queries import SubstanceQueries
from src.kb.substances_records import SubstanceError
from tests.unit.chemicals import harness as h

NS = h.NS
DOSSIER_SCHEMA = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-substance-dossier-v1.json").read_text())


def accept_all(env, identity, subjects):
    identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    for candidate in identity.candidates(NS, scopes=h.READ, state="proposed"):
        if {candidate["left_key"], candidate["right_key"]} <= set(subjects):
            identity.review(NS, candidate["candidate_id"], "accept", "identifiers agree", principal_id="reviewer",
                            scopes=h.REVIEW)


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    item.identity = SubstanceIdentity(item.conn, now=lambda: next(item.clock))
    accept_all(item, item.identity, ["pubchem:cid:6623", "echa:substance:100.001.133",
                                     "comptox:dtxsid:DTXSID7020182", "pubchem:cid:5988", "echa:substance:100.000.304"])
    item.queries = SubstanceQueries(item.conn)
    yield item
    item.conn.close()


def test_status_before_the_atp_applies_keeps_the_prior_classification_and_lists_what_is_scheduled(env):
    status = env.queries.status_as_of(NS, scopes=h.READ, query="80-05-7", as_of="2017-06-01")
    (harmonised,) = status["harmonised_classification"]
    assert harmonised["as_published"]["hazard_classes"][0]["hazard_class_category"] == "Repr. 2"
    assert harmonised["citation"]["legal_act"]["celex"] == "32008R1272" and harmonised["since"] == "2009-01-20"
    (svhc,) = status["candidate_list"]
    assert svhc["state"] == "listed" and svhc["since"] == "2017-01-12" and svhc["event"] == "inclusion"
    scheduled = {(s["record_type"], s["applies_from"]) for s in status["scheduled"]}
    assert {("classification", "2018-03-01"), ("candidate_listing", "2017-07-07"),
            ("restriction", "2020-01-02")} <= scheduled
    assert status["restriction"] == [] and "restriction" in status["none_on_record"]
    assert "not a statement that the substance is safe" in status["none_on_record"]["restriction"]


def test_status_after_the_atp_names_the_act_that_set_it_and_the_restriction_in_force(env):
    status = env.queries.status_as_of(NS, scopes=h.READ, query="bisphenol A", as_of="2026-09-01")
    (harmonised,) = status["harmonised_classification"]
    assert harmonised["as_published"]["hazard_classes"][0]["hazard_class_category"] == "Repr. 1B"
    assert harmonised["citation"]["legal_act"] == {
        "title": "Commission Regulation (EU) 2016/1179 (9th ATP)", "celex": "32016R1179", "eli": None,
        "atp": "ATP 9", "entry": "Annex VI, Table 3, index 604-030-00-0",
        "locator": "Annex VI Part 3 Table 3 index No 604-030-00-0"}
    assert harmonised["revisions_to_date"] == 2 and harmonised["first_dated"] == "2009-01-20"
    assert len(status["notified_classifications"]) == 2
    assert all(n["state"] == "notified (quoted as published)" for n in status["notified_classifications"])
    (restriction,) = status["restriction"]
    assert restriction["state"] == "in force" and restriction["as_published"]["entry_number"] == "66"
    assert restriction["citation"]["legal_act"]["celex"] == "32016R2235"
    (registration,) = status["registration"]
    assert registration["as_published"]["status"] == "Active"
    assert not h.forbidden_keys(status)


def test_authorisation_dates_and_a_removed_listing(env):
    SubstanceIdentity(env.conn).propose(NS, principal_id="matcher", scopes=h.WRITE)
    status = env.queries.status_as_of(NS, scopes=h.READ, subject_key="echa:substance:100.003.829",
                                      as_of="2016-01-01")
    (xiv,) = status["authorisation"]
    assert xiv["dates_as_published"] == {"latest_application_date": "2013-08-21", "sunset_date": "2015-02-21",
                                         "sunset_date_on_or_before_as_of": True}
    assert env.run("second", source_ids=["echa-reach-lists"], overrides=h.LATER["second"])["status"] == "complete"
    assert env.run("third", source_ids=["echa-reach-lists"], overrides=h.LATER["third"])["status"] == "complete"
    during = env.queries.status_as_of(NS, scopes=h.READ, subject_key="echa:substance:100.000.526",
                                      as_of="2026-09-20")
    assert [e["state"] for e in during["candidate_list"]] == ["listed"]
    after = env.queries.status_as_of(NS, scopes=h.READ, subject_key="echa:substance:100.000.526",
                                     as_of="2026-10-02")
    assert [e["state"] for e in after["candidate_list"]] == ["removed"]
    assert "candidate_list" in after["none_on_record"]


def test_history_lists_every_revision_with_dates_and_citations(env):
    history = env.queries.history(NS, scopes=h.READ, query="IISBACLAFKSPIT-UHFFFAOYSA-N")
    harmonised = [e for e in history["revisions"] if e["record_type"] == "classification"
                  and e["as_published"]["kind"] == "harmonised"]
    assert [e["effective_from"] for e in harmonised] == ["2009-01-20", "2018-03-01"]
    assert all(e["citation"]["url"].startswith("https://chem.echa.europa.eu/") for e in history["revisions"])
    assert {e["record_type"] for e in history["revisions"]} == {"classification", "candidate_listing",
                                                                "restriction", "registration"}


def test_a_substance_without_entries_has_none_on_record_never_safe(env):
    status = env.queries.status_as_of(NS, scopes=h.READ, query="57-50-1", as_of="2026-09-01")
    assert status["harmonised_classification"] == [] and status["candidate_list"] == []
    assert set(status["none_on_record"]) == {"harmonised_classification", "candidate_list", "authorisation",
                                             "restriction"}
    text = json.dumps(status).lower()
    assert "not a statement that the substance is safe" in text and "unregulated" in text
    missing = env.queries.dossier(NS, scopes=h.READ, query="7732-18-5", as_of="2026-09-01")
    assert missing["status"] == "not_found"
    assert "not a statement that the substance is safe" in missing["statement"]


def test_an_ambiguous_query_names_the_records_instead_of_guessing(env):
    with pytest.raises(SubstanceError) as caught:
        env.queries.status_as_of(NS, scopes=h.READ, query="64-17-5", as_of="2026-09-01")
    assert caught.value.code == "ambiguous" and len(caught.value.details["candidates"]) == 3


def test_dossier_assembles_identity_status_history_regulation_text_notices_and_data_points(env):
    env.seed_legal_act("32016R2235", "Commission Regulation (EU) 2016/2235",
                       [("Annex XVII/entry 66", "66. Bisphenol A ... thermal paper ... 0,02 % by weight.")])
    env.seed_notices()
    links = SubstanceLinks(env.conn)
    links.link_legal(NS, scopes=h.ALL, principal_id="linker")
    links.link_product_notices(NS, scopes=h.ALL, principal_id="linker")
    dossier = env.queries.dossier(NS, scopes=h.ALL, query="80-05-7", as_of="2026-09-01")
    jsonschema.validate(dossier, DOSSIER_SCHEMA)
    assert len(dossier["identity"]["members"]) == 3 and len(dossier["identity"]["matches"]) >= 2
    assert all(m["reviewer"] == "reviewer" for m in dossier["identity"]["matches"])
    (regulation,) = dossier["regulations"]
    assert regulation["text"]["status"] == "found" and "thermal paper" in regulation["text"]["passages"][0]["text"]
    (notice,) = dossier["linked_notices"]
    assert notice["notice"]["notice_number"] == "SR/07001/26" and notice["notice"]["provider"] == "safety-gate"
    assert any("80-05-7" in c["citing_text"] for c in notice["citations"])
    assert len(dossier["data_points"]) == 2 and all("not a hazard" in p["label"] for p in dossier["data_points"])
    assert {s["provider"] for s in dossier["sources_consulted"]} == {"pubchem", "echa-clp", "echa-reach", "comptox"}
    assert {s["evidence_origin"] for s in dossier["sources_consulted"]} == {"fixture"}
    assert not h.forbidden_keys(dossier)
    again = env.queries.dossier(NS, scopes=h.ALL, query="80-05-7", as_of="2026-09-01")
    assert again["dossier_hash"] == dossier["dossier_hash"]
