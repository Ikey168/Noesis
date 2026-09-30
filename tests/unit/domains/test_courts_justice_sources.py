"""CourtListener, FBI CDE, data.police.uk and Eurostat crime acquisition (#2389, #2394, #2399, #2403) and the
CJ01 source contracts (#2380)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.courts_justice_sources import (
    BOUNDED_COVERAGE,
    FORMATS,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    CourtsJusticeAdapter,
    fixture_transport,
    party_type,
)
from src.ingestion.source_packs import SUPPORTED_CONNECTORS, SourcePackConformance, SourcePackError
from src.kb.justice_statistics import JusticeStatisticsStore
from src.kb.legal_dockets import LegalDocketStore
from tests.unit import courts_justice_harness as h


def records(source_id, **kwargs):
    return [item["court_justice_record"] for page in h.fetch(source_id, **kwargs) for item in page.records]


def test_contracts_live_verification_minimisation_and_bounded_coverage_are_declared():
    assert set(PROVIDER_CONTRACTS) == {"courtlistener", "fbi-cde", "police-uk", "eurostat"}
    assert {v["status"] for v in LIVE_VERIFICATION.values()} == {"unverified-live"}
    for contract in PROVIDER_CONTRACTS.values():
        assert {"authentication", "rate_limits", "licence", "attribution", "revisions"} <= set(contract)
    assert "natural-person names in party records" in MINIMISATION["never_stored"]
    assert set(BOUNDED_COVERAGE) == {"dockets", "fbi-cde", "police-uk", "eurostat"}
    audit = (h.ROOT / "docs/development/courts-justice-evidence/source-audit.md").read_text()
    assert "Minimisation decision" in audit and "LIVE_VERIFICATION" in audit
    value = h.manifest()
    ours = [s for s in value["sources"] if s["connector"] == "courts-justice"]
    assert {s["source_id"] for s in ours} == set(h.SOURCES) and "courts-justice" in SUPPORTED_CONNECTORS
    assert all(s["courts_justice"]["live_verification"] == "unverified-live" for s in ours)
    assert {s["courts_justice"]["format"] for s in ours} == set(FORMATS)
    result = SourcePackConformance(h.ROOT).offline(value)
    assert result["valid"]


def test_docket_entries_are_verbatim_documents_linked_and_parties_minimised():
    (docket,) = records("courtlistener-dockets")
    fields = docket["fields"]
    assert docket["record_key"] == h.DOCKET and fields["court_id"] == "dcd"
    assert fields["docket_number"] == "1:99-cv-00101" and fields["case_name"] == "Example Data Co. v. Roe"
    assert fields["entries"][0]["description"].startswith("COMPLAINT against Jane Roe ( Filing fee $ 405)")
    document = fields["entries"][0]["documents"][0]
    assert document["is_available"] is True and document["link"].startswith("https://www.courtlistener.com/")
    assert "filepath_local" not in json.dumps(fields)
    parties = {p["ordinal"]: p for p in fields["parties"]}
    assert parties[1] == {"ordinal": 1, "party_type": "organisation", "name_as_published": "Example Data Co.",
                          "roles": ["Plaintiff"], "party_key": "courts:party:courtlistener:70001:1"}
    assert parties[2]["party_type"] == "natural_person" and parties[2]["name_as_published"] is None
    assert parties[2]["pseudonym"] == "natural person 1 (Defendant)" and parties[2]["party_key"] is None
    encoded = json.dumps(fields["parties"])
    for leaked in ("Jane Roe", "Private Lane", "Counsel Fixture", "Delaware", "7300002"):
        assert leaked not in encoded
    assert party_type("United States Department of Examples") == "organisation"
    assert party_type("A. Person") == "natural_person"


def test_opinion_clusters_keep_citations_quoted_dispositions_and_paragraphs():
    by_key = {r["record_key"]: r for r in records("courtlistener-opinions")}
    fields = by_key[h.CLUSTER]["fields"]
    assert fields["citations"] == ["999 F. Supp. 4th 201"]
    assert fields["disposition"] == "GRANTED in part and DENIED in part"
    (opinion,) = fields["opinions"]
    assert opinion["opinions_cited"] == [90002] and opinion["author_str"] == "Judge Fixture"
    assert len(opinion["paragraphs"]) == 5 and "42 U.S.C. § 1983" in opinion["paragraphs"][1]
    prior = by_key[h.PRIOR_CLUSTER]["fields"]
    assert prior["opinions"][0]["per_curiam"] is True and prior["citations"] == ["990 F.4th 12"]


def test_fbi_series_keep_programme_coverage_definition_and_refresh_vintage():
    state, agency = records("fbi-cde-summarized")
    fields = state["fields"]
    assert fields["release"] == {"label": "2099-09-15", "released_at": "2099-09-15"}
    assert fields["definitions"][0]["classification"] == "UCR-SRS"
    march = [o for o in fields["observations"] if o["period"] == "2098-03" and o["measure"] == "actuals"]
    clearances = next(o for o in march if "Clearances" in o["indicator"])
    assert clearances["value"] is None and clearances["suppressed"] and clearances["flags"] == ["value not published"]
    offences = next(o for o in march if "Offenses" in o["indicator"])
    assert offences["coverage"] == {"population": 3900000, "participated_population": 3700000,
                                    "programme": "UCR-SRS", "estimated": None}
    assert not any("United States" in o["indicator"] for o in fields["observations"])
    assert agency["fields"]["observations"][0]["place"] == {"scheme": "fbi-ori", "code": "EX0000100",
                                                           "label": "Example City Police Department"}


def test_police_uk_counts_categories_and_outcomes_without_locations():
    (release,) = records("police-uk-street-crime")
    fields = release["fields"]
    counts = {o["indicator"]: o["value"] for o in fields["observations"] if o["measure"] == "crimes"}
    assert counts == {"anti-social-behaviour:crimes": 1, "burglary:crimes": 3, "vehicle-crime:crimes": 1}
    outcomes = {o["outcome_category"]: o["value"] for o in fields["observations"]
                if o["measure"] == "outcomes" and o["indicator"] == "burglary:outcome"}
    assert outcomes == {"Investigation complete; no suspect identified": 2, "Under investigation": 1}
    assert fields["coverage_notes"][0]["kind"] == "anonymisation"
    encoded = json.dumps(release)
    for leaked in ("latitude", "Example Street", "persistent_id", "51.505"):
        assert leaked not in encoded


def test_eurostat_flags_footnotes_and_esms_notes_are_verbatim():
    (release,) = records("eurostat-crime-iccs")
    fields = release["fields"]
    fr = [o for o in fields["observations"] if o["place"]["code"] == "FR"]
    assert {(o["period"], o["measure"]): [f["code"] for f in o["flags"]] for o in fr if o["flags"]} == {
        ("2096", "NR"): ["b"], ("2096", "P_HTHAB"): ["b"], ("2098", "P_HTHAB"): ["c"]}
    confidential = next(o for o in fr if o["period"] == "2098" and o["measure"] == "P_HTHAB")
    assert confidential["value"] is None and confidential["suppressed"]
    assert confidential["flags"][0]["label"] == "confidential"
    kinds = {n["kind"] for n in fields["coverage_notes"]}
    assert kinds == {"national_definition_footnote", "esms_comparability"}
    esms = next(n for n in fields["coverage_notes"] if n["kind"] == "esms_comparability")
    assert esms["covers"] == ["DE", "FR"] and esms["text"].startswith("Comparisons between countries should be")
    assert fields["definitions"][0]["classification"] == "ICCS"


def test_units_fail_rather_than_truncate_and_stay_on_their_host():
    body = json.loads((h.FIXTURES / "cl_docket_70001_entries.json").read_text())
    body["next"] = "https://www.courtlistener.com/api/rest/v4/docket-entries/?cursor=abc"
    with pytest.raises(SourcePackError) as caught:
        h.fetch("courtlistener-dockets",
                bodies={"/api/rest/v4/docket-entries/?docket=70001&page_size=100": json.dumps(body)})
    assert caught.value.code == "budget_exhausted"
    pages = h.native_pages("police-uk-street-crime")
    pages[0]["final_url"] = "https://elsewhere.example/api/crime-last-updated"
    adapter = CourtsJusticeAdapter(h.source("police-uk-street-crime"), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 50}, cursor=None)
    assert caught.value.code == "network_policy"
    with pytest.raises(SourcePackError) as caught:
        h.fetch("courtlistener-dockets", secret=None)
    assert caught.value.code == "authentication_failed"


def test_receipts_name_requests_and_never_carry_the_secret():
    (page,) = h.fetch("courtlistener-dockets")
    receipt = page.receipt
    assert [r["name"] for r in receipt["requests"]] == ["docket", "entries", "parties"]
    assert receipt["evidence_origin"] == "fixture" and receipt["live_verification"] == "unverified-live"
    assert "fixture-credential" not in json.dumps(receipt) + json.dumps([dict(r) for r in page.records])


def test_updated_dockets_and_re_released_statistics_are_new_revisions_and_replays_add_nothing():
    conn = h.connection()
    h.load_all(conn)
    h.load_all(conn)  # a replay of unchanged responses adds nothing
    dockets = LegalDocketStore(conn)
    assert [r["revision_no"] for r in dockets.docket_revisions(h.NS, h.DOCKET)] == [1]
    h.load_all(conn, v2=True)
    revisions = dockets.docket_revisions(h.NS, h.DOCKET)
    assert [r["revision_no"] for r in revisions] == [1, 2]
    first = dockets.docket_as_of(h.NS, h.DOCKET, "2099-04-01")
    assert len(first["entries"]) == 2 and first["date_terminated"] is None
    later = dockets.docket_as_of(h.NS, h.DOCKET, "2099-08-01")
    assert len(later["entries"]) == 4 and later["date_terminated"] == "2099-06-20"
    stats = JusticeStatisticsStore(conn)
    fbi = [v for v in stats.vintages(h.NS) if v["provider"] == "fbi-cde" and ":state:" in v["record_key"]]
    assert [(v["vintage_no"], v["release_label"]) for v in fbi] == [(1, "2099-09-15"), (2, "2099-12-01")]
    agency = [v for v in stats.vintages(h.NS) if ":agency:" in v["record_key"]]
    assert len(agency) == 1  # the agency response did not change
    assert {v["provider"] for v in stats.vintages(h.NS)} == {"fbi-cde", "police-uk", "eurostat"}
    conn.close()
