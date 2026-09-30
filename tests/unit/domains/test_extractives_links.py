"""Extractives records linked to other packs by citation, shared identifier or accepted match (#2687)."""

from __future__ import annotations

import pytest

from src.kb.extractives_links import ExtractivesLinks
from src.kb.extractives_records import ExtractivesError
from tests.unit import extractives_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.reviewed(conn)
    return conn, ExtractivesLinks(conn)


def test_missing_providers_are_reported_as_unresolved_links_not_dropped(env):
    _, links = env
    trade = links.link_trade(h.NS, principal_id="svc", scopes=h.SCOPES)
    energy = links.link_energy(h.NS, principal_id="svc", scopes=h.SCOPES)
    finance = links.link_public_finance(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert not trade["linked"] and trade["unresolved"]
    assert not energy["linked"] and energy["unresolved"] and finance["unresolved"]
    reasons = {link["reason"].split(":")[0] for link in links.links(h.NS, scopes=h.READ_ONLY, state="unresolved")}
    assert "provider_absent" in reasons


def test_links_record_their_basis_and_point_at_record_revisions(env):
    conn, links = env
    trade_series = h.seed_trade(conn)
    energy_series = h.seed_energy(conn)
    line = h.seed_public_finance(conn)
    links.link_trade(h.NS, principal_id="svc", scopes=h.SCOPES)
    links.link_energy(h.NS, principal_id="svc", scopes=h.SCOPES)
    links.link_public_finance(h.NS, principal_id="svc", scopes=h.SCOPES)
    links.link_infrastructure(h.NS, principal_id="svc", scopes=h.SCOPES)
    linked = links.links(h.NS, scopes=h.READ_ONLY, state="linked")
    by_owner: dict[str, list] = {}
    for link in linked:
        by_owner.setdefault(link["target_owner"], []).append(link)
        assert link["basis"] in {"citation", "shared-identifier", "accepted-match"}
        assert link["source_revision"] and link["target"]["revision"]
    trade = by_owner["economics.trade"]
    assert {t["target"]["id"] for t in trade} == {trade_series}
    assert {t["target"]["code_relation"] for t in trade} == {"exact", "narrower"}
    assert by_owner["energy.core"][0]["target"]["id"] == energy_series
    assert by_owner["energy.core"][0]["basis"] == "shared-identifier"
    finance = by_owner["economics.public-finance"]
    assert {f["target"]["id"] for f in finance} == {line} and all("@" in f["source_revision"] for f in finance)
    infra = by_owner["geospatial.infrastructure"]
    assert len(infra) == 2 and {i["basis"] for i in infra} == {"accepted-match"}
    # Copper series without a named Energy series stay unresolved: nothing is linked by commodity name.
    assert not any(link["target_owner"] == "energy.core" and "copper" in link["source_id"] for link in linked)


def test_explicit_citation_links_need_a_source_and_locator_and_refuse_inference(env):
    _, links = env
    target = {"owner": "ownership.core", "id": "gleif:lei:724500EXAMPLAINTBV75", "revision": "r1"}
    with pytest.raises(ExtractivesError):
        links.link_by_citation(h.NS, source_kind="project", source_id="p", source_revision="a", relation="operated_by",
                               target=target, citation={"source": "report"}, principal_id="svc", scopes=h.SCOPES)
    with pytest.raises(ExtractivesError) as raised:
        links.link_by_citation(h.NS, source_kind="project", source_id="p", source_revision="a",
                               relation="operated_by", target=target,
                               citation={"source": "report", "locator": "p. 4", "basis": "similar name"},
                               principal_id="svc", scopes=h.SCOPES)
    assert raised.value.code == "inferred_link_refused"
    link = links.link_by_citation(h.NS, source_kind="project", source_id="p", source_revision="a",
                                  relation="operated_by", target=target,
                                  citation={"source": "Peru EITI report 2098 (fixture)", "locator": "annex 3"},
                                  principal_id="svc", scopes=h.SCOPES)
    assert link["basis"] == "citation" and link["state"] == "linked"
