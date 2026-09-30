"""Loss estimates linked to hazard events and insurers by citation (IN08), answered as of a date (IN09) (#2230)."""

from __future__ import annotations

import pytest

import tests.unit.insurance_harness as h
from src.domains.market.asof import MarketAsOfSnapshotStore
from src.domains.market.insurance import (
    CONTRACT,
    InsuranceError,
    InsuranceLinks,
    InsuranceQueries,
    InsuranceStore,
    end_of_day_ms,
)


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.acquire_all(conn)
    h.lei_records(conn)
    events = h.hazard_events(conn)
    return conn, events


def _links(conn):
    return InsuranceLinks(conn).propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, hazard_namespace=h.HAZ_NS)


def test_published_identifiers_link_name_date_matches_are_candidates_and_the_rest_stay_unlinked(world):
    conn, events = world
    result = _links(conn)
    assert result["hazard_events"]["status"] == "available"
    by_estimate = {}
    for view in InsuranceStore(conn, initialize=False).visible(h.NS, kinds=("catastrophe_loss_estimate",)):
        by_estimate[(view["record"]["source"]["provider"], view["record"]["event"]["name"])] = [
            link for link in result["links"] if link["record_id"] == view["record_id"]]
    [linked] = by_estimate[("florida-oir-claims", "Hurricane Fiktiva")]
    assert linked["state"] == "linked" and linked["basis"] == "published-identifier"
    assert linked["target_id"] == events["storm"] and linked["evidence"]["identifier"] == "AL992024"
    assert linked["record_revision_id"] and linked["target_revision_id"]
    [candidate] = by_estimate[("noaa-ncei-billion-dollar", "Hurricane Fiktiva")]
    assert candidate["state"] == "candidate" and candidate["basis"] == "name-date-proximity"
    assert not candidate["effective"]
    # A name match outside the date window is not even a candidate: the estimate stays queryable by its name.
    assert by_estimate[("florida-oir-claims", "Hurricane Beispiel")] == []
    beispiel = InsuranceQueries(conn).event(h.NS, "Hurricane Beispiel", as_of="2025-01-01", scopes=h.SCOPES)
    assert beispiel["status"] == "on_record" and beispiel["series"][0]["links"] == []
    # Idempotent.
    assert _links(conn)["changes"] == []


def test_a_reviewed_candidate_becomes_a_link_and_the_hazard_event_reaches_both_publishers(world):
    conn, events = world
    links = InsuranceLinks(conn)
    candidate = next(link for link in _links(conn)["links"] if link["state"] == "candidate")
    with pytest.raises(InsuranceError):
        links.review(h.NS, candidate["link_id"], "accept", "", principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    accepted = links.review(h.NS, candidate["link_id"], "accept", "NCEI names the same storm and dates",
                            principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert accepted["effective"] and accepted["effective_basis"] == "reviewed-candidate"
    answer = InsuranceQueries(conn).event(h.NS, events["storm"], as_of="2025-08-01", scopes=h.SCOPES)
    assert {s["provider"] for s in answer["series"]} == {"florida-oir-claims", "noaa-ncei-billion-dollar"}


def test_without_natural_hazards_records_links_report_none_on_record():
    conn = h.connection()
    h.acquire(conn, "florida", "2025-02-01")
    result = _links(conn)
    assert result["hazard_events"]["status"] == "none_on_record" and result["links"] == []


def test_insurer_links_rest_on_a_published_lei():
    conn = h.connection()
    h.acquire(conn, "sfcr", "2025-05-02")
    InsuranceStore(conn, initialize=False).apply(h.NS, [{
        "contract": CONTRACT, "kind": "catastrophe_loss_estimate",
        "source": {"provider": "florida-oir-claims", "source_id": "insurer-stated", "url": "https://floir.com/x.csv"},
        "publication_date": "2025-05-10", "publisher": "Florida Office of Insurance Regulation",
        "event": {"name": "Hurricane Fiktiva"}, "estimate_type": "insured", "measure": "insurer-reported losses",
        "value": "1", "unit": "USD", "currency": "USD",
        "insurer": {"name": h.GROUP, "lei": h.GROUP_LEI, "reporting_level": "group"},
    }], run_id="manual", observed_at_ms=h.ms("2025-05-11"))
    links = _links(conn)["links"]
    assert [(link["target_kind"], link["basis"]) for link in links] == [("insurer-report", "published-identifier")]


def test_event_estimates_as_of_a_date_list_publishers_separately_with_revisions(world):
    conn, _ = world
    queries = InsuranceQueries(conn)
    early = queries.event(h.NS, "Hurricane Fiktiva", as_of="2024-11-20", scopes=h.SCOPES)
    [florida] = early["series"]  # NCEI had not published yet
    assert florida["in_force"]["value"] == "1500000000" and len(florida["revisions"]) == 2
    late = queries.event(h.NS, "AL992024", as_of="2025-08-01", scopes=h.SCOPES)
    assert [s["publisher"] for s in late["series"]] == ["Florida Office of Insurance Regulation"]
    both = queries.event(h.NS, "Hurricane Fiktiva", as_of="2025-08-01", scopes=h.SCOPES)
    assert both["merged"] is False and len(both["series"]) == 2
    series = {s["provider"]: s for s in both["series"]}
    assert [r["value"] for r in series["florida-oir-claims"]["revisions"]] == [
        "1000000000", "1500000000", "1450000000"]
    assert series["noaa-ncei-billion-dollar"]["in_force"]["value"] == "3200000000"
    assert series["noaa-ncei-billion-dollar"]["estimate_type"] == "economic"
    assert all(r["citation"]["licence"]["decision"] == "in-scope" for s in both["series"] for r in s["revisions"])
    decisions = {c["provider"]: c["decision"] for c in both["coverage"]}
    assert decisions["perils"] == "excluded" and decisions["naic-public"] == "metadata-only"
    nothing = queries.event(h.NS, "Hurricane Nobody", as_of="2025-08-01", scopes=h.SCOPES)
    assert nothing["status"] == "none_on_record" and nothing["series"] == []


def test_market_and_insurer_answers_side_by_side_with_vintages_and_corrections(world):
    conn, _ = world
    queries = InsuranceQueries(conn)
    july = queries.market(h.NS, "DE", as_of="2025-07-15", scopes=h.SCOPES)
    rows = {(r["indicator"], r["dimensions"]["line_of_business"]): r for r in july["by_publisher"][
        "European Insurance and Occupational Pensions Authority (EIOPA)"]}
    motor = rows[("gross_written_premiums", "Motor vehicle liability insurance")]
    assert motor["value"] == "1000.5" and motor["release"] == "2025-06" and motor["currency"] == "EUR"
    assert rows[("gross_written_premiums", "Fire and other damage to property insurance")]["marker"] == "c"
    january = queries.market(h.NS, "DE", as_of="2026-01-10", scopes=h.SCOPES, indicator="gross_written_premiums")
    motor = next(r for r in january["by_publisher"]["European Insurance and Occupational Pensions Authority (EIOPA)"]
                 if r["dimensions"]["line_of_business"] == "Motor vehicle liability insurance")
    assert motor["value"] == "1010.5" and [h_["value"] for h_ in motor["history"]] == ["1000.5", "1010.5"]
    assert queries.market(h.NS, "IT", as_of="2026-01-10", scopes=h.SCOPES)["status"] == "none_on_record"
    before = queries.insurer(h.NS, h.GROUP_LEI, as_of="2025-04-29", scopes=h.SCOPES)
    assert before["status"] == "none_on_record"
    first = queries.insurer(h.NS, h.GROUP_LEI, as_of="2025-05-31", scopes=h.SCOPES)
    ratio = next(f for f in first["reports"][0]["figures"] if f["row"] == "R0690")
    assert ratio["value"] == "212" and ratio["citation"]["locator"]["template"] == "S.23.01.22"
    corrected = queries.insurer(h.NS, h.GROUP_LEI, as_of="2025-06-30", scopes=h.SCOPES)
    assert next(f for f in corrected["reports"][0]["figures"] if f["row"] == "R0690")["value"] == "210"
    assert len(corrected["reports"][0]["history"]) == 2
    # Acquired before the correction arrived: the earlier report is what was known.
    pinned = queries.insurer(h.NS, h.GROUP_LEI, as_of="2025-06-30", scopes=h.SCOPES,
                             acquired_by_ms=h.ms("2025-06-01"))
    assert next(f for f in pinned["reports"][0]["figures"] if f["row"] == "R0690")["value"] == "212"
    nobody = queries.insurer(h.NS, "529900MUSTERVERSAG57", as_of="2025-06-30", scopes=h.SCOPES)
    assert nobody["status"] == "none_on_record"
    with pytest.raises(InsuranceError):
        queries.market(h.NS, "DE", as_of="2025-07-15", scopes={"market:insurance:read"})


def test_market_asof_snapshots_pin_only_published_insurance_revisions(world):
    conn, _ = world
    snapshots = MarketAsOfSnapshotStore(conn, now=lambda: 1)
    scopes = h.SCOPES | {"operator"}

    def capture(day, key):
        return snapshots.create_snapshot(
            "market:asof-test", key, effective_at_ms=end_of_day_ms(day), publicly_available_by_ms=end_of_day_ms(day),
            acquired_by_ms=h.ms("2026-12-31"), principal_id=h.PRINCIPAL, scopes=scopes,
            selection={"insurance_records": [{"namespace": h.NS, "kinds": ["catastrophe_loss_estimate"]}]},
        )

    early = capture("2024-01-01", "early")
    assert early["inputs"] == [] and early["gaps"][0]["reason"] == "no_records_published_at_cutoffs"
    late = capture("2024-10-20", "late")
    assert [i["kind"] for i in late["inputs"]] == ["insurance_record"]
    assert all(i["public_at_ms"] <= end_of_day_ms("2024-10-20") for i in late["inputs"])
