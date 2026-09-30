"""Research-entities source audit contracts and ROR/ORCID/DataCite/CORDIS acquisition (#2584, #2594, #2601, #2606,
#2609)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.research_entities_sources import (
    BOUNDED_COVERAGE,
    LIVE_VERIFICATION,
    MINIMISATION,
    NOT_IMPLEMENTED,
    ORCID_EXCLUDED,
    PROVIDER_CONTRACTS,
    ResearchEntitiesAdapter,
    ResearchEntitiesFormatError,
    doi,
    fixture_transport,
    orcid_id,
    research_declaration,
    ror_id,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import research_entities_harness as h


def items(name, later=False):
    return [r["research_entity"] for page in h.fetch(name, later=later) for r in page]


def test_audit_contracts_cover_every_source_with_terms_limits_revisions_and_unverified_live():
    assert set(PROVIDER_CONTRACTS) == {"ror", "orcid", "datacite", "cordis"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("entry_points", "authentication", "key_handling", "licence", "rate_limits", "revision_model",
                    "corrections_and_removals", "personal_data", "sources"):
            assert contract[key], (provider, key)
        assert all(s["read_on"] == "2026-09-30" and s["url"].startswith("https://") for s in contract["sources"])
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert provider in BOUNDED_COVERAGE
    assert "openaire-graph" in NOT_IMPLEMENTED and NOT_IMPLEMENTED["openaire-graph"]["decision"] == "not_implemented"
    assert "emails" in ORCID_EXCLUDED and "biography" in ORCID_EXCLUDED
    assert "researchers" in MINIMISATION["access"] and "never" in MINIMISATION["merges"]


def test_source_pack_entries_validate_replay_and_declare_live_verification():
    manifest = h.manifest()
    ours = [s for s in manifest["sources"] if s["connector"] == "research-entities"]
    assert {s["source_id"] for s in ours} == set(h.SOURCES.values())
    assert all(s["research_entities"]["live_verification"] == "unverified-live" for s in ours)
    orcid = next(s for s in ours if s["research_entities"]["provider"] == "orcid")
    assert orcid["auth"] == {"kind": "optional-secret", "secret_ref": "NOESIS_ORCID_READ_PUBLIC_TOKEN"}
    report = SourcePackConformance(h.ROOT).offline(json.loads((h.ROOT / "config/source_packs/research.json")
                                                              .read_text()))
    assert all(r["valid"] for r in report["sources"] if r["connector"] == "research-entities")


def test_identifiers_are_validated():
    assert ror_id("0re1ab101") == "https://ror.org/0re1ab101"
    assert orcid_id("https://orcid.org/0009-0001-2345-6786") == h.R1
    assert doi("https://doi.org/10.9999/RENT.Paper1") == "10.9999/rent.paper1"
    for parser, bad in ((ror_id, "0ILOU0001"), (orcid_id, "0009-0001-2345-6787"), (doi, "not-a-doi")):
        with pytest.raises(ResearchEntitiesFormatError):
            parser(bad)


def test_ror_release_keeps_declared_ids_status_relationships_external_ids_and_reports_missing():
    first = {i["native_id"]: i for i in items("ror")}
    assert set(first) == {h.A1, h.A2, h.M1, h.MISSING}  # the undeclared record is never read
    assert first[h.MISSING]["status"] == "not_in_release" and first[h.MISSING]["body"] == {}
    a1 = first[h.A1]
    assert a1["revision"] == {"marker": "v9.1-2099-01-15", "basis": "ror-release",
                              "effective_at": "2099-01-15T00:00:00+00:00", "provider_modified": "2098-11-20"}
    assert {e["type"] for e in a1["body"]["external_ids"]} == {"grid", "isni", "wikidata", "fundref"}
    assert a1["body"]["relationships"] == [{"type": "child", "id": h.A2, "label": "Examplia Institute for Data"}]
    later = {i["native_id"]: i for i in items("ror", later=True)}
    assert later[h.M1]["status"] == "inactive"
    assert later[h.M1]["body"]["relationships"][0] == {"type": "successor", "id": h.A1,
                                                      "label": "Universitaet Beispielstadt"}


def test_orcid_record_is_reduced_to_the_minimised_public_fields():
    records = {i["native_id"]: i for i in items("orcid")}
    r1 = records[h.R1]
    assert set(r1["body"]) == {"orcid", "display_name", "employments", "works"}
    text = json.dumps(r1)
    for leaked in ("example.invalid", "biography", "Scopus", "Fictional School", "Hidden Employer",
                   "Researcher", "Fictional Department", "rent.private", "rent.book1", "Fictional Journal"):
        assert leaked not in text, leaked
    assert r1["revision"]["basis"] == "orcid-last-modified"
    assert [w["identifiers"] for w in r1["body"]["works"]] == [
        [{"type": "doi", "value": "10.9999/rent.paper1"}],
        [{"type": "arxiv", "value": "2098.00001"}, {"type": "doi", "value": "10.9999/rent.paper2"}]]
    assert r1["body"]["employments"][0]["organisation"]["disambiguated"] == {"source": "ROR", "identifier": h.A1}
    later = {i["native_id"]: i for i in items("orcid", later=True)}
    assert later[h.R2]["status"] == "deactivated" and later[h.R2]["body"] == {}


def test_orcid_unknown_and_locked_records_become_removal_statements():
    source = h.source("orcid")
    for status, expected in ((404, "not_found"), (409, "locked")):
        pages = [{"request": f"/v3.0/{d['orcid']}/record", "status": status, "body": "{}"}
                 for d in source["research_entities"]["documents"]]
        out = h.fetch("orcid", transport=fixture_transport(pages))
        assert {r["research_entity"]["status"] for page in out for r in page} == {expected}


def test_datacite_keeps_related_identifiers_as_published_and_minimises_creators():
    records = items("datacite")
    d1 = next(i for i in records if i["native_id"] == "10.9999/rent.data1")
    assert d1["revision"]["marker"] == "v1:2099-02-10T09:30:00+00:00"
    assert {r["relationType"] for r in d1["body"]["related_identifiers"]} == {"IsSupplementTo", "Cites",
                                                                              "IsDocumentedBy"}
    assert d1["body"]["related_identifiers"][1]["relatedIdentifier"] == "10.9999/RENT.PAPER2"  # as published
    person, organisation = d1["body"]["creators"]
    assert person == {"position": 0, "name_type": "Personal", "name": None, "orcid": h.R1,
                      "affiliation_identifiers": [{"scheme": "ROR", "identifier": h.A1}]}
    assert organisation["name"] == "Examplia Institute for Data"
    text = json.dumps(d1)
    for leaked in ("Beispiel, Ada", "citationCount", "viewCount", "Contact, Fictional", "description"):
        assert leaked not in text
    later = next(i for i in items("datacite", later=True) if i["native_id"] == "10.9999/rent.data1")
    assert later["body"]["metadata_version"] == 2
    assert "IsVersionOf" in {r["relationType"] for r in later["body"]["related_identifiers"]}


def test_cordis_projects_keep_participants_pic_and_contributions_with_currency_as_published():
    records = {i["native_id"]: i for i in items("cordis")}
    assert set(records) == {"HORIZON:101999001", "HORIZON:101999002", "HORIZON:101999003"}
    assert records["HORIZON:101999003"]["status"] == "not_in_release"
    project = records["HORIZON:101999001"]["body"]
    assert project["programme"] == "HORIZON" and project["grant_doi"] == "10.9999/cordis.101999001"
    coordinator = project["participants"][0]
    assert coordinator["pic"] == "999999901" and coordinator["role"] == "coordinator"
    assert coordinator["ec_contribution"] == {"amount": "2000000", "currency": "EUR", "published": "2000000",
                                              "currency_basis": "declared by the document"}
    assert "street" not in json.dumps(project) and "Fictional Street" not in json.dumps(project)
    poly = records["HORIZON:101999002"]["body"]
    assert poly["total_cost"]["amount"] == "800000.50" and poly["total_cost"]["published"] == "800000,50"


def test_bounded_acquisition_receipts_and_refusals():
    source = h.source("ror")
    adapter = ResearchEntitiesAdapter(source, transport=fixture_transport(h.pages("ror")))
    page = adapter.fetch_page({"operation": "registry-records", "parameters": {}, "limit": 200}, cursor=None)
    assert page.receipt["evidence_origin"] == "fixture" and page.receipt["missing"] == [h.MISSING]
    assert page.records[0]["research_entities_release"]["live_verification"] == "unverified-live"
    with pytest.raises(SourcePackError) as budget:
        adapter.fetch_page({"operation": "registry-records", "parameters": {}, "limit": 2}, cursor=None)
    assert budget.value.code == "budget_exhausted"
    with pytest.raises(SourcePackError) as params:
        adapter.fetch_page({"operation": "registry-records", "parameters": {"query": "x"}}, cursor=None)
    assert params.value.code == "parameter_forbidden"
    limited = [{**p, "status": 429, "headers": {"Retry-After": "30"}} for p in h.pages("ror")]
    with pytest.raises(SourcePackError) as rate:
        ResearchEntitiesAdapter(source, transport=fixture_transport(limited)).fetch_page(
            {"operation": "registry-records", "parameters": {}}, cursor=None)
    assert rate.value.code == "rate_limited"
    wrong = copy.deepcopy(source)
    wrong["endpoint"] = "https://example.org/records"
    with pytest.raises(SourcePackError):
        research_declaration(wrong)
    unbounded = copy.deepcopy(source)
    unbounded["research_entities"]["documents"][0]["ror_ids"] = []
    with pytest.raises(SourcePackError):
        research_declaration(unbounded)
    no_reason = copy.deepcopy(h.source("orcid"))
    del no_reason["research_entities"]["documents"][0]["reason"]
    with pytest.raises(SourcePackError):
        research_declaration(no_reason)
