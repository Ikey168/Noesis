"""Research-entities source audit, declared sources and acquisition through the real adapter (#2584, #2594, #2601,
#2606, #2609)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion import research_entities_sources as re_src
from src.ingestion.source_packs import SourcePackError, _digest, replay_native_fixture
from tests.unit import research_entities_harness as h


def by_key(records):
    return {r["record_key"]: r for r in records}


def test_the_audit_records_contracts_terms_minimisation_and_bounded_coverage():
    audit = (h.ROOT / "docs/development/research-entities-evidence/source-audit.md").read_text()
    for source_id in h.SOURCES:
        assert f"`{source_id}`" in audit
    for needed in ("NOESIS_ORCID_PUBLIC_TOKEN", "CC0", "Decision\n  2011/833/EU", "not_in_response", "withdrawn",
                   "metadataVersion", "contentUpdateDate", "Retention", "Who may query them",
                   "researchers:read", "Bounded first coverage", "documented-not-acquired",
                   "Terms were not\nre-verified live"):
        assert needed in audit, needed
    assert set(re_src.PROVIDER_CONTRACTS) == {"ror", "orcid", "datacite", "cordis", "openaire-graph"}
    for contract in re_src.PROVIDER_CONTRACTS.values():
        assert {"endpoints", "authentication", "rate_limits", "licence", "revisions", "corrections_and_removals",
                "access_decision"} <= set(contract)
    assert re_src.PROVIDER_CONTRACTS["openaire-graph"]["access_decision"] == "documented-not-acquired"
    assert "inferred" in re_src.PROVIDER_CONTRACTS["openaire-graph"]["reason"]
    assert all(v["status"] != "verified-live" for v in re_src.LIVE_VERIFICATION.values())
    assert "biography" in re_src.MINIMISATION["never_stored_for_researchers"]
    assert "never matched" in re_src.MINIMISATION["matching"]
    assert set(re_src.BOUNDED_COVERAGE) >= {"ror", "orcid", "datacite", "cordis", "periods", "caps"}


def test_the_pack_declares_every_source_bounded_minimised_and_replaying_its_pinned_output():
    manifest = h.manifest()
    assert manifest["version"] == "1.5.0"
    ours = {s["source_id"]: s for s in manifest["sources"] if s["connector"] == "research-entities"}
    assert set(ours) == set(h.SOURCES)
    for source_id, item in ours.items():
        declared = item["research_entities"]
        assert declared["live_verification"] == "unverified-live"
        assert declared["minimisation"] == re_src.MINIMISATION_POLICY
        assert declared["format"] == h.FORMATS[source_id]
        keyed = re_src.FORMATS[declared["format"]]["keyed"]
        assert (item["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_ORCID_PUBLIC_TOKEN"}) is keyed
        fixture = json.loads((h.ROOT / item["fixture"]["path"]).read_text())
        assert fixture == json.loads(json.dumps(h.source_pack_fixture(source_id)))  # generated from the harness
        assert _digest(list(replay_native_fixture(item, fixture))) == item["fixture"]["expected_output_hash"]
    earlier = [s for s in manifest["sources"] if s["connector"] != "research-entities"]
    assert {s["source_id"] for s in earlier} >= {"crossref-works", "openalex-works", "datacite-dois"}


def test_ror_releases_are_vintages_keeping_withdrawn_records_successors_and_external_ids():
    records, receipts = h.fetch(h.ROR_SOURCE)
    first = [r for r in records if r["release"]["label"] == "v9.1"]
    second = [r for r in records if r["release"]["label"] == "v9.2"]
    assert {r["native_id"] for r in first} == {h.EXAMPLA, h.MARINE, h.HOSPITAL, h.NORTHWIND_POLY}
    assert receipts[0]["not_in_response"] == [h.NORTHWIND_TECH]  # reported, never a deletion
    assert "0zzzzz999" not in json.dumps(records)  # undeclared organisations are not kept
    poly = by_key(second)["research-entities:ror:0zznwd303"]
    assert poly["status"] == "withdrawn" and poly["fields"]["successors"] == [h.NORTHWIND_TECH]
    assert by_key(second)["research-entities:ror:0zznwd404"]["fields"]["predecessors"] == [h.NORTHWIND_POLY]
    exampla = by_key(first)["research-entities:ror:0zzexa101"]
    assert {e["type"] for e in exampla["fields"]["external_ids"]} == {"fundref", "grid", "isni", "wikidata"}
    assert next(e for e in exampla["fields"]["external_ids"] if e["type"] == "fundref")["preferred"] is None
    assert exampla["as_of"] == "2099-03-01" and exampla["native_revision"] == "release:v9.1"
    assert first[0]["revision_order"] < second[0]["revision_order"]
    assert all(r["evidence_origin"] == "fixture" for r in records)
    assert receipts[0]["requests"][0]["path"].startswith("/records/99999901/files/")


def test_orcid_records_keep_only_the_minimised_public_fields_and_works_as_asserted_identifiers():
    records, receipts = h.fetch(h.ORCID_SOURCE)
    ada = by_key(records)[f"research-entities:orcid:{h.ADA}"]
    fields = ada["fields"]
    assert set(fields) <= re_src.RESEARCHER_ALLOWED_FIELDS
    assert fields["name"] == {"given_names": "Ada", "family_name": "Exampla", "credit_name": "A. Exampla"}
    assert fields["last_modified"] == "2099-02-14T09:00:00+00:00" == ada["as_of"]
    assert [w["external_ids"][0]["value"] for w in fields["works"]] == [h.PAPER1, h.DS1]
    assert {w["asserted_by"]["kind"] for w in fields["works"]} == {"self", "member-client"}
    assert all(w["asserted_by"]["name"] is None for w in fields["works"] if w["asserted_by"]["kind"] == "self")
    assert [e["organisation"]["disambiguated"]["ror_id"] for e in fields["employments"]] == [
        h.NORTHWIND_POLY, h.EXAMPLA]  # the private employment is not stored
    assert {"biography", "emails", "educations", "keywords", "other-names"} <= set(fields["withheld_sections"])
    limited = by_key(records)[f"research-entities:orcid:{h.CY}"]["fields"]
    assert limited["name"] is None and limited["name_status"] == "not-public"
    text = json.dumps([records, receipts])
    assert not [p for p in h.PERSONAL if p in text]
    assert re_src.FIXTURE_SECRET not in text


def test_a_deactivated_orcid_record_is_a_withdrawn_revision_without_personal_fields():
    records, _ = h.fetch(h.ORCID_SOURCE, "v2")
    bo = by_key(records)[f"research-entities:orcid:{h.BO}"]
    assert bo["status"] == "deactivated" and bo["fields"]["name"] is None and bo["fields"]["works"] == []
    assert bo["native_revision"] == "http-409" and bo["as_of"] is None
    assert "Northwind\"" not in json.dumps(bo)


def test_orcid_needs_its_client_token_and_reports_the_source_unavailable_without_it():
    fetcher = h.adapter(h.ORCID_SOURCE, secret=None)
    with pytest.raises(SourcePackError) as caught:
        fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 50}, cursor=None)
    assert caught.value.code == "authentication_failed"
    seen = {}

    def transport(**kwargs):
        seen.update(kwargs)
        return h.fixture_transport(h.native_pages(h.ORCID_SOURCE))(**kwargs)

    h.ResearchEntitiesAdapter(h.source(h.ORCID_SOURCE), transport=transport, secret="token").fetch_page(
        {"operation": "selection", "parameters": {}, "limit": 50}, cursor=None)
    assert seen["headers"]["Authorization"] == "Bearer token"
    assert seen["headers"]["Accept"] == "application/vnd.orcid+json"


def test_datacite_keeps_metadata_versions_related_identifiers_and_minimised_creators():
    records, _ = h.fetch(h.DATACITE_SOURCE)
    ds1 = by_key(records)[f"research-entities:doi:{h.DS1}"]
    fields = ds1["fields"]
    assert fields["metadata_version"] == 2 and ds1["as_of"] == "2099-02-15T12:00:00+00:00"
    assert {(r["relation_type"], r["doi"]) for r in fields["related_identifiers"]} == {
        ("IsSupplementTo", h.PAPER1), ("Cites", "10.99998/exampla.paper.009"), ("IsVersionOf", "10.99999/exampla.ds.000")}
    persons = [c for c in fields["creators"] if c["name_type"] == "Personal"]
    assert persons == [
        {"position": 0, "name_type": "Personal", "orcid": h.ADA, "affiliation_ids": [h.EXAMPLA], "contributor_type": None},
        {"position": 1, "name_type": "Personal", "orcid": None, "affiliation_ids": [h.MARINE], "contributor_type": None}]
    organisation = next(c for c in fields["creators"] if c["name_type"] == "Organizational")
    assert organisation["name"] == "Exampla Institute of Marine Research"
    assert fields["funding_references"][0]["award_number"] == h.EXAMPLAR
    assert "creators.givenName" in ds1["minimisation"]["withheld"]
    assert not [p for p in h.PERSONAL if p in json.dumps(records)]
    later, _ = h.fetch(h.DATACITE_SOURCE, "v2")
    assert by_key(later)[f"research-entities:doi:{h.DS1}"]["fields"]["metadata_version"] == 3
    gone = by_key(later)[f"research-entities:doi:{h.DS2}"]
    assert gone["status"] == "unavailable" and gone["native_revision"] == "http-404"


def test_cordis_projects_keep_programme_participants_pic_and_contributions_with_currency():
    records, receipts = h.fetch(h.CORDIS_SOURCE)
    assert set(by_key(records)) == {f"research-entities:cordis:HORIZON:{h.EXAMPLAR}",
                                    f"research-entities:cordis:HORIZON:{h.NORTHWAVE}"}
    northwave = by_key(records)[f"research-entities:cordis:HORIZON:{h.NORTHWAVE}"]["fields"]
    assert northwave["programme"] == "HORIZON" and northwave["topics"] == ["HORIZON-CL5-2092-EXAMPLE-02"]
    coordinator = northwave["participants"][0]
    assert (coordinator["pic"], coordinator["name"], coordinator["role"]) == (
        h.PIC_NORTHWIND, "NORTHWIND POLYTECHNIC", "coordinator")
    assert coordinator["ec_contribution"] == {
        "amount": "500000.5", "currency": "EUR", "as_published": "500000,5",
        "currency_basis": "CORDIS exports publish euro amounts without a currency column; stated as EUR"}
    assert all("street" not in p and "contact_form" not in p for p in northwave["participants"])
    assert "UNDECLARED" not in json.dumps(records)
    assert receipts[0]["unit"] == {"programme": "HORIZON", "path": "/data/cordis-HORIZONprojects-csv.zip"}
    assert not [p for p in h.PERSONAL if p in json.dumps(records)]


def test_declarations_refuse_unbounded_foreign_or_unminimised_selections():
    item = h.source(h.ORCID_SOURCE)
    bad = copy.deepcopy(item)
    bad["research_entities"]["selection"]["orcids"] = ["0000-0009-9999-0012"]  # checksum fails
    with pytest.raises(SourcePackError):
        re_src.ResearchEntitiesAdapter(bad)
    bad = copy.deepcopy(item)
    bad["research_entities"]["minimisation"] = "none"
    with pytest.raises(SourcePackError):
        re_src.ResearchEntitiesAdapter(bad)
    bad = copy.deepcopy(item)
    bad["endpoint"] = "https://orcid.example"
    with pytest.raises(SourcePackError):
        re_src.ResearchEntitiesAdapter(bad)
    bad = copy.deepcopy(h.source(h.CORDIS_SOURCE))
    bad["research_entities"]["selection"]["programmes"][0]["path"] = "/data/other.zip"
    with pytest.raises(SourcePackError):
        re_src.ResearchEntitiesAdapter(bad)


def test_a_redirect_to_another_host_rate_limits_and_budgets_are_explicit_failures():
    item = h.source(h.DATACITE_SOURCE)
    pages = h.native_pages(h.DATACITE_SOURCE)
    pages[0]["final_url"] = "https://elsewhere.example/dois/x"
    fetcher = re_src.ResearchEntitiesAdapter(item, transport=h.fixture_transport(pages))
    with pytest.raises(SourcePackError) as caught:
        fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 50}, cursor=None)
    assert caught.value.code == "network_policy"
    pages = h.native_pages(h.DATACITE_SOURCE)
    pages[0].update({"status": 429, "headers": {"Retry-After": "30"}})
    fetcher = re_src.ResearchEntitiesAdapter(item, transport=h.fixture_transport(pages))
    with pytest.raises(SourcePackError) as caught:
        fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 50}, cursor=None)
    assert caught.value.code == "rate_limited"
    fetcher = h.adapter(h.ROR_SOURCE)
    with pytest.raises(SourcePackError) as caught:
        fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 2}, cursor=None)
    assert caught.value.code == "budget_exhausted"  # never a truncated release
    with pytest.raises(SourcePackError):
        fetcher.fetch_page({"operation": "selection", "parameters": {"q": "x"}, "limit": 2}, cursor=None)
