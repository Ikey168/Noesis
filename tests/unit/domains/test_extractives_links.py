"""Extractives links to other packs by citation, shared identifier or accepted match (#2653, EX07 #2687)."""

from __future__ import annotations

import pytest

from src.kb.extractives_identity import ExtractivesIdentity
from src.kb.extractives_links import ExtractivesLinks
from src.kb.extractives_records import ExtractivesError
from src.kb.extractives_store import ExtractivesStore
from tests.unit import extractives_harness as h
from tests.unit.energy.harness import acquire as acquire_energy

CITATION = {"text": "Royalties are booked under budget article 9 (fixture)", "locator": "report p. 42"}


def _accept_all(identity, matches):
    for match in matches:
        identity.review(h.NS, match["match_id"], "accept", "checked", principal_id="reviewer", scopes=h.SCOPES)


def test_missing_providers_and_targets_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    links = ExtractivesLinks(conn)
    stream = f"{h.NL_REPORT}:stream:1415e1:royalties"
    absent = links.link_public_finance(h.NS, stream, "pf-line:missing", CITATION, principal_id="analyst",
                                       scopes=h.SCOPES)
    assert absent["status"] == "provider_absent" and absent["basis"] == "explicit-citation"
    assert absent["subject"]["revision_id"] and absent["citation"] == CITATION
    assert links.link_trade_flows(h.NS, principal_id="analyst", scopes=h.SCOPES)["status"] == "provider_absent"
    energy = links.link_energy(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert energy["status"] == "provider_absent" and len(energy["unlinked"]) == 2
    assert links.link_infrastructure(h.NS, principal_id="analyst", scopes=h.SCOPES)["status"] == "provider_absent"
    assert [link["status"] for link in links.links(h.NS)] == ["provider_absent"]  # recorded, not dropped
    with pytest.raises(ExtractivesError) as caught:
        links.link_public_finance(h.NS, stream, "pf-line:x", {"text": "no locator"}, principal_id="analyst",
                                  scopes=h.SCOPES)
    assert caught.value.code == "invalid_citation"


def test_commodity_series_link_to_trade_flows_only_through_an_accepted_hs_match_and_the_published_country():
    conn = h.connection()
    h.load_all(conn)
    trade = h.seed_trade(conn)
    links = ExtractivesLinks(conn)
    # Without an accepted HS match nothing is joined.
    none = links.link_trade_flows(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert none["linked"] == [] and none["status"] == "no_target_on_record"
    identity = ExtractivesIdentity(conn)
    identity.import_concordance(h.NS, h.CONCORDANCE, principal_id="op", scopes=h.SCOPES)
    _accept_all(identity, identity.propose_commodities(h.NS, principal_id="analyst", scopes=h.SCOPES)["matches"])
    result = links.link_trade_flows(h.NS, principal_id="analyst", scopes=h.SCOPES)
    store = ExtractivesStore(conn)
    (imports,) = store.find_series(h.NS, provider="bgs-wms", commodity="copper", statistic="imports", country="DE")
    made = links.links(h.NS, subject_id=imports["series_id"], target_kind="trade-series")
    assert {m["target"]["id"] for m in made} == {trade["comtrade"], trade["comext"]}
    for link in made:
        assert link["basis"] == "accepted-match" and link["match_id"] and link["status"] == "linked"
        assert link["subject"]["revision_id"] == imports["current_vintage_id"] and link["target"]["revision_id"]
        assert link["shared"]["hs_heading"] == "2603" and link["side_by_side"]["combined"] is False
    schemes = {m["shared"]["country"]["scheme"] for m in made}
    assert schemes == {"m49", "eurostat-geo"}
    # Chile has no trade series held: reported as no target, not dropped.
    chile = [u for u in result["unlinked"] if u["series_id"] in
             {s["series_id"] for s in store.find_series(h.NS, country="CL", commodity="copper")}]
    assert chile and {u["status"] for u in chile} == {"no_target_on_record"}


def test_hydrocarbon_series_link_to_energy_by_shared_siec_or_citation_and_projects_through_accepted_matches():
    conn = h.connection()
    h.load_all(conn)
    acquire_energy(conn, "energy-eurostat-balances")
    links = ExtractivesLinks(conn)
    energy = links.link_energy(h.NS, principal_id="analyst", scopes=h.SCOPES)
    # The Energy fixtures publish balances for SIEC TOTAL only: no shared SIEC code, reported per series.
    assert energy["status"] == "no_target_on_record"
    assert {(u["siec"], u["country"]) for u in energy["unlinked"]} == {("O4100_TOT", "DE"), ("O4100_TOT", "NL")}
    store = ExtractivesStore(conn)
    (de_oil,) = store.find_series(h.NS, commodity="crude-petroleum", country="DE")
    from src.kb.energy_store import EnergyStore

    target = next(s for s in EnergyStore(conn).series("energy", scopes=h.SCOPES, subject_codes=["DE"]))
    cited = links.link_energy_by_citation(h.NS, de_oil["series_id"], target["series_id"],
                                          {"text": "BGS crude production feeds the balance (fixture)",
                                           "locator": "methodology note"}, principal_id="analyst", scopes=h.SCOPES)
    assert cited["status"] == "linked" and cited["target"]["revision_id"] and cited["basis"] == "explicit-citation"
    missing = links.link_energy_by_citation(h.NS, de_oil["series_id"], "energy-series:none", CITATION,
                                            principal_id="analyst", scopes=h.SCOPES)
    assert missing["status"] == "target_not_found"
    # Projects: only an accepted EX06 match links; no ownership is inferred.
    asset_id = h.seed_infrastructure(conn)
    identity = ExtractivesIdentity(conn)
    proposed = identity.propose_projects(h.NS, infrastructure_namespace="infra", principal_id="analyst",
                                         scopes=h.SCOPES)
    assert links.link_infrastructure(h.NS, principal_id="analyst", scopes=h.SCOPES)["linked"] == []
    _accept_all(identity, proposed["matches"])
    result = links.link_infrastructure(h.NS, principal_id="analyst", scopes=h.SCOPES)
    (linked,) = result["linked"]
    (link,) = links.links(h.NS, subject_id=linked["record_key"])
    assert link["target"]["id"] == asset_id and link["basis"] == "accepted-match" and link["match_id"]
    assert "no ownership" in link["side_by_side"]["note"]


def test_a_revenue_stream_links_to_a_public_finance_line_by_explicit_citation_pointing_at_revisions():
    from tests.unit import public_finance_harness as pf

    conn = h.connection()
    h.load_all(conn)
    pf.load_budgets(conn)
    line_id = conn.execute("SELECT line_id FROM public_finance_lines ORDER BY line_id LIMIT 1").fetchone()[0]
    links = ExtractivesLinks(conn)
    stream = f"{h.NL_REPORT}:stream:1415e1:royalties"
    link = links.link_public_finance(h.NS, stream, line_id, CITATION, principal_id="analyst", scopes=h.SCOPES)
    assert link["status"] == "linked" and link["target"]["revision_id"] and link["subject"]["revision_id"]
    assert link["side_by_side"]["combined"] is False
    missing = links.link_public_finance(h.NS, stream, "pf-line:none", CITATION, principal_id="analyst",
                                        scopes=h.SCOPES)
    assert missing["status"] == "target_not_found"
    with pytest.raises(ExtractivesError):
        links.link_public_finance(h.NS, "extractives:eiti:payment:none", line_id, CITATION, principal_id="analyst",
                                  scopes=h.SCOPES)
