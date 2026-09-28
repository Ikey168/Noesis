"""Who funded what where as of a date: cited activities, per-publisher coverage and CRS vintages (#2035)."""

from __future__ import annotations

import pytest

from src.kb.development_finance import DevelopmentFinanceError
from src.kb.development_finance_identity import DevelopmentFinanceIdentity, subject_key
from src.kb.development_finance_queries import DevelopmentFinanceQueries
from tests.unit.funding import development_finance_harness as h

WATER = "XM-DAC-99901-FICT-0001"
FDPA_ID = "iati:ref:XM-DAC-99901"
NGO_ID = "iati:ref:XI-IATI-FICTNGO"


@pytest.fixture()
def env():
    env = h.Env().load()
    yield env
    env.conn.close()


def queries(env):
    return DevelopmentFinanceQueries(env.conn, now=env.now)


def test_activities_are_listed_per_publisher_with_citations_and_coverage(env):
    answer = queries(env).list_activities(
        h.NS, {"country": "KE"}, principal_id="analyst", scopes=h.SCOPES
    )
    publishers = {p["publisher_id"]: p for p in answer["publishers"]}
    assert set(publishers) == {FDPA_ID, NGO_ID}
    water = publishers[FDPA_ID]["activities"][0]
    assert (
        water["iati_identifier"] == WATER
        and water["cites"]["dataset_id"]
        and water["cites"]["revision_id"]
    )
    assert publishers[FDPA_ID]["coverage"]["state"] == "complete for its selections"
    assert answer["conflicts"][0]["iati_identifier"] == WATER
    assert {f["field"] for f in answer["conflicts"][0]["fields"]} >= {
        "title",
        "participating_orgs",
    }
    assert "total" not in str(answer["publishers"]).lower()
    assert answer["receipt"]["answer_id"].startswith("devfin-answer:")
    assert answer["boundary"].startswith("Published aid activities")


def test_the_revision_in_force_is_selected_before_filtering_and_as_of(env):
    # In June publisher A's education activity is no longer published and FICT-0003 appears in KE.
    env.at("2098-06-05T08:00:00").iati("iati_fdpa_2098-06.xml", observation="r2:fdpa")
    march = queries(env).list_activities(
        h.NS,
        {"sector": "122", "as_of": "2098-04-30"},
        principal_id="a",
        scopes=h.SCOPES,
    )
    assert march["publishers"] == []  # the health activity was not yet observed
    june = queries(env).list_activities(
        h.NS, {"sector": "122"}, principal_id="a", scopes=h.SCOPES
    )
    assert [a["iati_identifier"] for a in june["publishers"][0]["activities"]] == [
        "XM-DAC-99901-FICT-0003"
    ]
    # An edit that removes the country from the current revision removes the activity from a country query,
    # even though an older revision named the country.
    moved = h.edited(
        "iati_fdpa_2098-06.xml",
        (
            '<recipient-country code="KE" percentage="60"/>',
            '<recipient-country code="TZ" percentage="60"/>',
        ),
        ("2098-06-01T10:00:00Z", "2098-08-01T10:00:00Z"),
    )
    env.at("2098-08-05T08:00:00").iati(moved, observation="r3:fdpa")
    kenya = queries(env).list_activities(
        h.NS, {"country": "KE", "publisher": h.FDPA}, principal_id="a", scopes=h.SCOPES
    )
    assert WATER not in [
        a["iati_identifier"] for p in kenya["publishers"] for a in p["activities"]
    ]
    before = queries(env).list_activities(
        h.NS,
        {"country": "KE", "publisher": h.FDPA, "as_of": "2098-07-01"},
        principal_id="a",
        scopes=h.SCOPES,
    )
    assert WATER in [
        a["iati_identifier"] for p in before["publishers"] for a in p["activities"]
    ]


def test_transactions_totals_stay_within_one_publisher_type_and_currency(env):
    answer = queries(env).search_transactions(
        h.NS, {"iati_identifier": WATER}, principal_id="a", scopes=h.SCOPES
    )
    publishers = {p["publisher_id"]: p for p in answer["publishers"]}
    fdpa = {
        (t["transaction_type"], t["currency"]): t for t in publishers[FDPA_ID]["totals"]
    }
    disbursed = fdpa[("3", "EUR")]
    assert disbursed["sum"] == "250000" and disbursed["value_date_range"] == [
        "2098-02-20",
        "2098-02-20",
    ]
    assert (
        disbursed["excluded_unknown_amounts"] == 1
    )  # the undated disbursement stays unknown
    ngo = {
        (t["transaction_type"], t["currency"]): t for t in publishers[NGO_ID]["totals"]
    }
    assert ngo[("1", "EUR")]["sum"] == "240000"
    assert "never across publishers" in answer["note"]


def test_inspect_shows_publishers_side_by_side_with_history_results_and_changes(env):
    env.at("2098-06-05T08:00:00").iati("iati_fdpa_2098-06.xml", observation="r2:fdpa")
    answer = queries(env).inspect_activity(
        h.NS, WATER, principal_id="a", scopes=h.SCOPES
    )
    reports = {r["publisher_id"]: r for r in answer["reports"]}
    assert set(reports) == {FDPA_ID, NGO_ID}
    fdpa = reports[FDPA_ID]
    assert [c["revision_no"] for c in fdpa["history"]] == [1, 2]
    changes = {(c["change"], c["transaction_key"]) for c in fdpa["transaction_changes"]}
    assert changes == {("corrected", "ref:FICT-0001-D1"), ("new", "ref:FICT-0001-D3")}
    (result,) = fdpa["current"]["results"]
    period = result["indicators"][0]["periods"][0]
    assert (
        period["actuals"][0]["value"] == "2100"
        and "no achievement judgement" in result["note"]
    )
    assert answer["world_bank"][0]["state"] == "linked"
    march = queries(env).inspect_activity(
        h.NS, WATER, as_of="2098-03-10", principal_id="a", scopes=h.SCOPES
    )
    assert [r["publisher_id"] for r in march["reports"]] == [FDPA_ID]
    with pytest.raises(DevelopmentFinanceError):
        queries(env).inspect_activity(
            h.NS, WATER, as_of="2098-01-01", principal_id="a", scopes=h.SCOPES
        )


def test_crs_aggregates_appear_separately_with_their_vintage(env):
    answer = queries(env).list_activities(
        h.NS,
        {"country": "KE", "sector": "14030", "crs_recipient": "KEN"},
        principal_id="a",
        scopes=h.SCOPES,
    )
    cells = {c["price_basis"]: c for c in answer["crs"]}
    assert set(cells) == {"current", "constant"}
    assert cells["current"]["vintage"]["published_on"] == "2099-01-15"
    assert cells["constant"]["vintage"]["base_year"] == "2097"
    env.at("2099-07-20T08:00:00").crs(
        "crs_deu_ken_140_2099-07.csv",
        release={"label": "CRS release 2099-07", "published_on": "2099-07-15"},
    )
    later = queries(env).crs_aggregates(
        h.NS,
        {"recipient": "KEN", "price_basis": "current"},
        principal_id="a",
        scopes=h.SCOPES,
    )
    (cell,) = later["cells"]
    assert (
        cell["vintage"]["published_on"] == "2099-07-15"
        and len(cell["earlier_vintages"]) == 1
    )
    before = queries(env).crs_aggregates(
        h.NS,
        {"recipient": "KEN", "price_basis": "current", "as_of": "2099-06-30"},
        principal_id="a",
        scopes=h.SCOPES,
    )
    assert before["cells"][0]["vintage"]["published_on"] == "2099-01-15"
    assert "never decomposed" in cell["note"]


def test_world_bank_links_need_identifiers_and_similarity_is_a_candidate(env):
    links = queries(env).world_bank_links(h.NS, scopes=h.SCOPES)
    by = {(link["project_id"], link["state"]): link for link in links["links"]}
    assert by[("P999001", "linked")]["evidence"][0]["kind"] == "related-activity"
    candidate = by[("P999002", "candidate")]
    assert (
        candidate["activity_key"].endswith("FICT-0002")
        and "similar title" in candidate["reasons"]
    )
    assert ("P999002", "linked") not in by
    assert links["unlinked_projects"] == ["P999003"]


def test_a_funder_query_resolves_through_reviewed_identity_across_publishers(env):
    identity = DevelopmentFinanceIdentity(env.conn, now=env.now)
    result = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)
    for candidate in result["candidates"]:
        if "devfin:publisher:" + FDPA_ID in candidate["records"]:
            identity.service.review(
                h.NS,
                candidate["candidate_id"],
                "accept",
                "stated reference",
                principal_id="reviewer",
                scopes=h.REVIEW_SCOPES,
            )
    answer = queries(env).list_activities(
        h.NS,
        {"funder": "devfin:publisher:" + FDPA_ID},
        principal_id="a",
        scopes=h.SCOPES,
    )
    assert {p["publisher_id"] for p in answer["publishers"]} == {FDPA_ID, NGO_ID}
    trust = subject_key(FDPA_ID, None, "Fictional Learning Trust")
    unmatched = {o["record_key"] for o in answer["unknowns"]["unmatched_organisations"]}
    assert trust in unmatched


def test_answers_are_receipts_a_project_pins_and_a_report_cites(env):
    from src.kb.research_projects import ResearchProjectStore

    answer = queries(env).list_activities(
        h.NS,
        {"country": "KE", "as_of": "2098-05-01"},
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    answer_id = answer["receipt"]["answer_id"]
    project = ResearchProjectStore(env.conn, now=env.now).create(
        h.NS,
        "aid-question",
        questions=["Who funds water in Kenya?"],
        success_criteria=["cited activities"],
        scope={"domains": [], "namespaces": [h.NS]},
        budget={"tokens": 0, "requests": 0, "usd_micros": 0},
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    attached = queries(env).attach_to_project(
        h.NS,
        answer_id,
        project["project_id"],
        project["revision"],
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    assert (
        attached["answer"]["request"]["as_of"] == "2098-05-01"
        and attached["answer"]["coverage"]
    )
    with pytest.raises(DevelopmentFinanceError):
        queries(env).attach_to_project(
            h.NS,
            "devfin-answer:forged",
            project["project_id"],
            attached["revision"],
            principal_id="analyst",
            scopes=h.SCOPES,
        )
    citation = queries(env).report_citation(h.NS, answer_id, scopes=h.SCOPES)
    assert "as of 2098-05-01" in citation["bibliography"]["text"]
    from src.kb.authored_reports import validate_content

    validate_content(
        {
            "title": "Aid to water",
            "snapshot": {"id": answer_id, "generations": {h.NS: 1}},
            "bibliography": [citation["bibliography"]],
            "limitations": ["bounded publisher coverage"],
            "sections": [
                {
                    "id": "s1",
                    "title": "Activities",
                    "assertions": [
                        {
                            "id": "a1",
                            "text": "Two publishers report the water activity.",
                            "kind": "sourced",
                            "dependencies": [citation["dependency"]],
                            "citations": [answer_id],
                        }
                    ],
                }
            ],
        }
    )


def test_reads_before_any_acquisition_are_not_ready_and_scopes_are_checked():
    import duckdb

    empty = DevelopmentFinanceQueries(duckdb.connect())
    with pytest.raises(DevelopmentFinanceError) as error:
        empty.list_activities(h.NS, {}, principal_id="a", scopes=h.SCOPES)
    assert error.value.code == "not_ready"
    with pytest.raises(DevelopmentFinanceError) as crs:
        empty.crs_aggregates(h.NS, {}, principal_id="a", scopes=h.SCOPES)
    assert crs.value.code == "not_ready"
    with pytest.raises(DevelopmentFinanceError) as denied:
        empty.list_activities(
            h.NS, {}, principal_id="a", scopes={"knowledge:funding:read"}
        )
    assert denied.value.code == "unauthorized"
