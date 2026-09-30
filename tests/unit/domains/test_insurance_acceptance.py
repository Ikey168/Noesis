"""Offline insurer-and-event-to-statistics acceptance for the Market ``insurance`` feature (#2230, IN12).

With sockets disabled, authored fixtures go through the real adapter and projector. They cover EIOPA
statistics in two releases, one SFCR with quoted QRT figures (plus its correction and a subsidiary's solo
report), NAIC in its recorded metadata-only state, and two loss-estimate publishers per event (Florida OIR,
NCEI), while a third publisher (PERILS) is refused as excluded. The journey starts from an insurer and from an
event and ends at cited statistics, SFCR figures and loss-estimate revisions, showing the identity match basis,
the as-of revision selection and the publishers side by side. The production pack replays offline through the
source-pack runtime.
"""

from __future__ import annotations

import json
import socket

import pytest

import tests.unit.insurance_harness as h
from src.domains.market.insurance import InsuranceLinks, InsuranceQueries, InsuranceStore
from src.domains.market.insurance_identity import InsuranceIdentity
from src.ingestion.insurance_sources import insurance_declaration
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackError, SourcePackStore, validate_source_pack


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted during the offline acceptance run")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.acquire_all(conn)
    h.lei_records(conn)
    h.ownership_entities(conn)
    events = h.hazard_events(conn)
    InsuranceLinks(conn).propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, hazard_namespace=h.HAZ_NS)
    return conn, events


def test_from_an_insurer_to_cited_sfcr_figures_with_the_identity_basis_and_revision_selection(world):
    conn, _ = world
    identity = InsuranceIdentity(conn, initialize=False)
    exact = identity.exact_matches(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, lei_namespace=h.LEI_NS,
                                   ownership_namespace=h.OWN_NS)
    group_key = f"insurance:insurer:lei:{h.GROUP_LEI}:group"
    assert {m["basis"] for m in exact[group_key]} == {"exact-identifier"}
    queries = InsuranceQueries(conn)
    answer = queries.insurer(h.NS, h.GROUP_LEI, as_of="2025-05-31", scopes=h.SCOPES, lei_namespace=h.LEI_NS)
    assert answer["insurer"]["basis"] == "published-lei" and answer["insurer"]["levels"] == ["group"]
    [report] = answer["reports"]
    ratio = next(f for f in report["figures"] if f["row"] == "R0690")
    assert (ratio["value"], ratio["unit"], ratio["status"]) == ("212", "%", "reported")
    assert ratio["citation"]["locator"] == {"template": "S.23.01.22", "row": "R0690", "column": "C0010",
                                            "page": None}
    assert ratio["citation"]["url"].endswith("/fixture/sfcr_group_2024.pdf")
    assert ratio["citation"]["licence"]["decision"] == "in-scope"
    assert any(f["status"] == "unknown" and f["value"] is None for f in report["figures"])
    later = queries.insurer(h.NS, h.GROUP_LEI, as_of="2025-07-01", scopes=h.SCOPES)
    assert next(f for f in later["reports"][0]["figures"] if f["row"] == "R0690")["value"] == "210"
    assert [r["publication_date"] for r in later["reports"][0]["history"]] == ["2025-04-30", "2025-06-10"]
    # The subsidiary's own answer keeps its solo report and shows the group's only through the GLEIF link.
    solo = queries.insurer(h.NS, h.SOLO_LEI, as_of="2025-07-01", scopes=h.SCOPES, lei_namespace=h.LEI_NS)
    assert [r["record"]["insurer"]["reporting_level"] for r in solo["reports"]] == ["solo"]
    assert solo["group_reports_via_ownership_link"][0]["ownership_link"]["basis"] == "gleif-parent-relationship"
    # NAIC is reported in its decision state in every answer.
    decisions = {c["provider"]: c["decision"] for c in answer["coverage"]}
    assert decisions["naic-public"] == "metadata-only" and decisions["naic-licensed-products"] == "excluded"
    naic = [v for v in InsuranceStore(conn, initialize=False).visible(h.NS, kinds=("publication_reference",))]
    assert len(naic) == 1 and "figures" not in naic[0]["record"]


def test_from_a_market_to_supervisory_statistics_by_release(world):
    conn, _ = world
    answer = InsuranceQueries(conn).market(h.NS, "DE", as_of="2026-01-10", scopes=h.SCOPES)
    [rows] = answer["by_publisher"].values()
    motor = next(r for r in rows if r["indicator"] == "gross_written_premiums"
                 and r["dimensions"]["line_of_business"] == "Motor vehicle liability insurance")
    assert (motor["value"], motor["release"]) == ("1010.5", "2025-12")
    assert [x["release"] for x in motor["history"]] == ["2025-06", "2025-12"]
    assert next(r for r in rows if r["marker"])["marker"] == "c"


def test_from_an_event_to_side_by_side_publishers_with_revisions_and_the_excluded_one_refused(world):
    conn, events = world
    queries = InsuranceQueries(conn)
    by_hazard = queries.event(h.NS, events["storm"], as_of="2025-08-01", scopes=h.SCOPES)
    [florida] = by_hazard["series"]  # only the identifier-linked publisher before any review
    assert florida["links"][0]["basis"] == "published-identifier"
    by_name = queries.event(h.NS, "Hurricane Fiktiva", as_of="2025-08-01", scopes=h.SCOPES)
    series = {s["provider"]: s for s in by_name["series"]}
    assert set(series) == {"florida-oir-claims", "noaa-ncei-billion-dollar"} and by_name["merged"] is False
    assert [r["value"] for r in series["florida-oir-claims"]["revisions"]] == [
        "1000000000", "1500000000", "1450000000"]
    assert series["noaa-ncei-billion-dollar"]["links"][0]["state"] == "candidate"
    as_of_november = queries.event(h.NS, "Hurricane Fiktiva", as_of="2024-11-30", scopes=h.SCOPES)
    [early] = as_of_november["series"]
    assert early["in_force"]["value"] == "1500000000" and early["in_force"]["publication_date"] == "2024-11-15"
    assert all(r["citation"]["url"] and r["citation"]["revision_id"] for s in by_name["series"]
               for r in s["revisions"])
    perils = h.fictional("florida", ["perils_press_release.csv"])
    perils["insurance"]["provider"] = "perils"
    with pytest.raises(SourcePackError) as refused:
        insurance_declaration(perils)
    assert refused.value.code == "licence_excluded"
    assert {c["provider"]: c["decision"] for c in by_name["coverage"]}["perils"] == "excluded"


def test_an_insurer_or_event_with_no_record_is_none_on_record(world):
    conn, _ = world
    queries = InsuranceQueries(conn)
    assert queries.insurer(h.NS, "529900MUSTERVERSAG57", as_of="2026-01-01", scopes=h.SCOPES)["status"] == \
        "none_on_record"
    assert queries.event(h.NS, "Windstorm Fiktiva", as_of="2026-01-01", scopes=h.SCOPES)["status"] == \
        "none_on_record"


def test_production_sources_replay_offline_through_the_runtime():
    conn = h.connection()
    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    SourcePackStore(conn).install(manifest, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for source in manifest["sources"]:
        runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="operator")
    receipt = runtime.run(
        {"pack_id": manifest["pack_id"], "run_key": "insurance-production-offline", "operation": "documents",
         "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 60_000},
        principal_id="operator",
        adapters=runtime.fixture_adapters(manifest["pack_id"], h.ROOT),
        dns_resolver=lambda _host: ["8.8.8.8"],
    )
    assert {s["source_id"]: s["status"] for s in receipt["sources"]} == {
        s["source_id"]: "complete" for s in manifest["sources"]}
    store = InsuranceStore(conn)
    # The production scope excludes every authored row; only NAIC's metadata-only reference is stored.
    assert {v["record"]["kind"] for v in store.visible(h.NS)} == {"publication_reference"}
    receipts = {r["provider"]: r["receipt"] for r in store.receipts(h.NS)}
    assert receipts["eiopa-insurance-statistics"]["counts"]["out_of_scope"] == 3
    assert receipts["naic-public"]["access_decision"] == "metadata-only"
