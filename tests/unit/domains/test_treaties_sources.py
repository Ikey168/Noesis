"""UN Treaty Collection, CELLAR agreement and Council of Europe acquisition (#2595, #2599, #2605) and the TR01 source
contracts (#2586)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.source_packs import (
    SUPPORTED_CONNECTORS,
    SourcePackConformance,
    SourcePackError,
)
from src.ingestion.treaties_sources import (
    BOUNDED_COVERAGE,
    CONTACT_WITHHELD,
    FORMATS,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    TreatiesAdapter,
    fixture_transport,
    minimisation_violations,
)
from tests.unit import treaties_harness as h


def by_key(source_id, **kwargs):
    return {r["record_key"]: r for r in h.records(source_id, **kwargs)}


def test_contracts_live_verification_minimisation_and_bounded_coverage_are_declared():
    assert set(PROVIDER_CONTRACTS) == {"untc", "eu-cellar", "coe-treaty-office"}
    assert {v["status"] for v in LIVE_VERIFICATION.values()} == {"unverified-live"}
    for contract in PROVIDER_CONTRACTS.values():
        assert {"endpoints", "authentication", "rate_limits", "licence", "attribution", "revisions",
                "personal_data", "access_decision"} <= set(contract)
    assert "contact details (e-mail, telephone, postal address) of designated authorities" in \
        MINIMISATION["never_stored"]
    assert {"untc", "eu-cellar", "coe-treaty-office", "period"} == set(BOUNDED_COVERAGE)
    audit = (h.ROOT / "docs/development/treaties-evidence/source-audit.md").read_text()
    for heading in ("## Access decisions", "## Per-source contract", "## Minimisation decision",
                    "## Bounded first coverage", "## LIVE_VERIFICATION"):
        assert heading in audit
    assert "not re-read live" in audit
    value = h.manifest()
    assert value["version"] == "1.5.0" and "treaties" in SUPPORTED_CONNECTORS
    ours = [s for s in value["sources"] if s["connector"] == "treaties"]
    assert {s["source_id"] for s in ours} == set(h.SOURCES)
    assert all(s["treaties"]["live_verification"] == "unverified-live" for s in ours)
    assert all(s["treaties"]["minimisation"] == "treaties-minimisation-v1" for s in ours)
    assert {s["treaties"]["format"] for s in ours} == set(FORMATS)
    result = SourcePackConformance(h.ROOT).offline(value)
    assert result["valid"]
    counts = {s["source_id"]: s["records"] for s in result["sources"] if s["source_id"] in h.SOURCES}
    assert counts == {"untc-treaty-status": 17, "eu-cellar-agreements": 8, "coe-treaty-office": 17}


def test_untc_status_is_keyed_by_chapter_and_number_with_actions_and_verbatim_statements():
    records = by_key("untc-treaty-status")
    treaty = records[h.UNTC]["fields"]
    assert treaty["identifiers"]["untc_mtdsg"] == "XXVII-99" and treaty["identifiers"]["unts_registration"] == "99001"
    assert treaty["title_as_published"] == "Convention on the Protection of Example Wetlands"
    assert treaty["adoption"] == {"as_published": "Exampletown, 1 March 2090", "date": "2090-03-01"}
    assert treaty["entry_into_force"]["date"] == "2091-06-01"
    assert treaty["entry_into_force"]["conditions_as_published"].endswith("article 20(1).")
    assert records[h.UNTC]["native_revision"] == "2099-01-15T09:15:00"
    assert treaty["text_policy"] == "linked, not mirrored" and treaty["text_references"][0]["url"].endswith(".pdf")
    accession = records["treaties:untc:action:XXVII-99:northwind-republic:accession:1"]["fields"]
    assert accession["deposit_date"] == "2092-05-10" and accession["action_date"] is None
    assert accession["action_type_as_published"].endswith("[a]") and accession["date_text_as_published"] == \
        "10 May 2092 a"
    approval = records["treaties:untc:action:XXVII-99:european-union:approval:1"]["fields"]
    assert approval["action_type_as_published"].endswith("[AA]")
    assert records["treaties:untc:action:XXVII-99:oldland:succession:1"]["fields"]["deposit_date"] == "2093-08-04"
    ratification = records["treaties:untc:action:XXVII-99:exampland:ratification:1"]["fields"]
    assert ratification["footnote_refs"] == ["note1"] and "withdraw the reservation" in \
        ratification["footnotes"][0]["text_verbatim"]
    reservation = records["treaties:untc:statement:XXVII-99:exampland:reservation:dec-exampland-1"]
    assert reservation["fields"]["text_verbatim"].startswith("The Government of Exampland reserves the right")
    assert reservation["fields"]["action_key"] == "treaties:untc:action:XXVII-99:exampland:ratification:1"
    assert reservation["locator"].endswith("#dec-exampland-1")
    objection = records["treaties:untc:statement:XXVII-99:northwind-republic:objection:obj-northwind-1"]["fields"]
    assert objection["objects_to_statement_key"] == reservation["record_key"]
    assert objection["objection_link"] == "linked by the source" and objection["made_on"] == "2092-06-20"
    note = records["treaties:untc:statement:XXVII-99:depositary:footnote:note1"]["fields"]
    assert note["refers_to_participants"] == ["treaties:untc:participant:exampland"]
    assert records["treaties:untc:participant:exampland"]["fields"]["codes"] == []


def test_cellar_agreement_reuses_the_cellar_query_with_expressions_dates_parties_and_act_citations():
    records = by_key("eu-cellar-agreements")
    treaty = records[h.EU]["fields"]
    assert treaty["identifiers"]["celex"] == "22090A0510(01)"
    assert treaty["identifiers"]["eli"] == "http://data.europa.eu/eli/agree_internation/2090/999/oj"
    assert treaty["dates_as_stated"] == {"document": ["2090-05-10"], "signature": ["2092-01-20"]}
    assert treaty["entry_into_force"]["date"] is None  # not stated by CELLAR in the first acquisition
    assert {c["celex"] for c in treaty["citations"]} == {"32092D0101", "32093D0202", "32094R0303"}
    assert all(c["basis"] == "explicit CDM triple" for c in treaty["citations"])
    assert {"scheme": "cets", "value": "999", "as_published": "CETS No. 999",
            "basis": "citation in the published title"} in treaty["cross_references"]
    expressions = [r for r in records.values() if r["record_kind"] == "treaty-expression"]
    assert sorted(e["fields"]["language"] for e in expressions) == ["DEU", "ENG", "FRA"]
    assert all(e["treaty_key"] == h.EU for e in expressions)
    assert sum(1 for r in records.values() if r["record_kind"] == "treaty") == 1  # languages never multiply treaties
    party = records["treaties:eu-cellar:participant:xea"]["fields"]
    assert any(c["scheme"] == "iso3166-1-alpha3" and c["value"] == "XEA" for c in party["codes"])
    assert records["treaties:eu-cellar:action:22090A0510(01):eu:signature:1"]["fields"]["action_date"] == "2092-01-20"
    (page,) = h.fetch("eu-cellar-agreements")
    assert [r["name"] for r in page.receipt["requests"]] == ["expressions", "agreement"]
    assert all("query=sha256" in r["path"] and "SELECT" not in r["path"] for r in page.receipt["requests"])  # SPARQL queries receipted by digest


def test_coe_chart_keeps_members_denunciations_and_redacts_contact_details():
    records = by_key("coe-treaty-office")
    treaty = records[h.COE]["fields"]
    assert treaty["identifiers"]["cets"] == "999" and treaty["entry_into_force"]["date"] == "2091-09-01"
    assert records["treaties:coe:participant:southland"]["fields"]["participant_type_as_published"] == "non-member"
    denunciation = records["treaties:coe:action:999:northwind-republic:denunciation:1"]["fields"]
    assert denunciation["deposit_date"] == "2098-06-30" and denunciation["effective_date"] == "2099-01-01"
    assert records["treaties:coe:action:999:northwind-republic:ratification:1"]["publication_status"] == "published"
    declaration = records["treaties:coe:statement:999:exampland:declaration:coe-999-exampland-d1"]["fields"]
    assert declaration["contact_details_withheld"] is True and CONTACT_WITHHELD in declaration["text_verbatim"]
    assert "example.org" not in json.dumps(records) and "456 789" not in json.dumps(records)
    reservation = records["treaties:coe:statement:999:exampland:reservation:coe-999-exampland-r1"]["fields"]
    assert reservation["made_on"] == "2091-03-12" and reservation["articles_as_published"] == "Articles concerned: 7"
    objection = records["treaties:coe:statement:999:northwind-republic:objection:coe-999-northwind-o1"]["fields"]
    assert objection["objects_to_statement_key"].endswith("coe-999-exampland-r1")
    assert objection["action_key"] is None and objection["action_link_basis"] == "not stated by the source"


def test_personal_fields_are_rejected_and_units_are_bounded_and_same_host():
    record = next(iter(by_key("coe-treaty-office").values()))
    assert minimisation_violations({**record, "fields": {**record["fields"], "signatory_name": "A. Person"}})
    item = h.source("untc-treaty-status")
    item["treaties"]["selection"] = {"treaties": [{"mtdsg_no": "not-an-id", "chapter": 27}]}
    with pytest.raises(SourcePackError):
        TreatiesAdapter(item)
    item = h.source("untc-treaty-status")
    item["auth"] = {"kind": "required-secret", "secret_ref": "NOESIS_EXAMPLE"}
    with pytest.raises(SourcePackError):
        TreatiesAdapter(item)
    pages = h.native_pages("untc-treaty-status")
    pages[0]["final_url"] = "https://elsewhere.example/Pages/ViewDetails.aspx"
    adapter = TreatiesAdapter(h.source("untc-treaty-status"), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 500}, cursor=None)
    assert exc.value.code == "network_policy"
    broken = h.native_pages("untc-treaty-status", bodies={"status": "<html><body>moved</body></html>"})
    adapter = TreatiesAdapter(h.source("untc-treaty-status"), transport=fixture_transport(broken))
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 500}, cursor=None)
    assert exc.value.code == "schema_drift"
    adapter = TreatiesAdapter(h.source("untc-treaty-status"),
                              transport=fixture_transport(h.native_pages("untc-treaty-status")))
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 5}, cursor=None)
    assert exc.value.code == "budget_exhausted"  # never truncated
    (page,) = h.fetch("untc-treaty-status")
    assert page.receipt["complete_for"] == [h.UNTC] and page.receipt["evidence_origin"] == "fixture"
    assert page.receipt["requests"][0]["path"].startswith("/Pages/ViewDetails.aspx?chapter=27")
