"""Further BODS publishers (Denmark CVR, Slovakia RPVS) and OpenCorporates (#2887), and a chain across three
jurisdictions resolved only through reviewed identity decisions. Offline, over authored fictional fixtures."""

import json

import pytest

from src.ingestion.ownership_opencorporates import ADAPTERS as OC_ADAPTERS
from src.ingestion.ownership_providers import (
    ADAPTERS,
    FIXTURE_SECRET,
    PROVIDER_CONTRACTS,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.ownership_graph import OwnershipGraph
from src.kb.ownership_identity import OwnershipIdentityService
from tests.unit.ownership import harness
from tests.unit.ownership.harness import NS, SCOPES

# ------------------------------------------------- #2887 BODS and OpenCorporates


def test_each_bods_publisher_keeps_its_own_statements():
    dk = harness.drain_source("open-ownership-bods-dk")
    sk = harness.drain_source("open-ownership-bods-sk")
    dk_coverage = dk[0]["ownership_part"]["coverage"]
    assert set(dk_coverage["publishers"]["Open Ownership Register"]) == {
        "Denmark Central Business Register (Centrale Virksomhedsregister [CVR])"}
    assert dk_coverage["not_selected"] == {"GB Persons Of Significant Control Register": 1}
    assert "open-ownership:statement:oo-fixture-dk-ent-gbsrc" not in harness.flat_records(dk)
    sk_records = harness.flat_records(sk)
    assert all(r["source"]["note"].startswith("Slovakia") for r in sk_records.values())
    assert sk_records["open-ownership:statement:oo-fixture-sk-per-1"]["owner_scoped"] is True
    from src.kb.ownership_bundle import load_source_pack

    bad = next(s for s in load_source_pack()["sources"] if s["source_id"] == "open-ownership-bods-dk")
    bad["ownership"]["publishers"] = ["Latvia Register of Enterprises beneficial owners"]
    with pytest.raises(SourcePackError):
        ADAPTERS["bods"](bad, transport=fixture_transport([]), secret=None)


def test_opencorporates_needs_its_key_and_stays_an_aggregator():
    with pytest.raises(SourcePackError) as caught:
        harness.drain_source("opencorporates-companies", secret=None, adapters=OC_ADAPTERS)
    assert caught.value.code == "authentication_failed"
    envelopes = harness.drain_source("opencorporates-companies", adapters=OC_ADAPTERS)
    assert [e["ownership_part"]["outcome"] for e in envelopes] == ["returned", "returned", "none_reported"]
    assert FIXTURE_SECRET not in json.dumps(envelopes)  # the token is never retained
    records = harness.flat_records(envelopes)
    dk = records["opencorporates:company:dk:99990011"]
    assert {(i["scheme"], i["value"]) for i in dk["identifiers"]} == {("opencorporates", "dk/99990011"),
                                                                      ("dk-cvr", "99990011")}
    assert dk["source"]["publisher"] == "OpenCorporates" and "ODbL" in dk["source"]["license"]
    assert dk["source"]["url"] == "https://opencorporates.com/companies/dk/99990011"
    officers = [r for r in records.values() if r["kind"] == "officer_role"]
    assert {o["officer"]["name"] for o in officers} == {"METTE EKSEMPEL", "SØREN FIKTIV"}
    assert PROVIDER_CONTRACTS["opencorporates"]["status"] == "implemented"
    assert "never an authoritative substitute" in PROVIDER_CONTRACTS["opencorporates"]["role"]


def test_a_chain_across_three_jurisdictions_resolves_only_through_reviewed_identity():
    env = harness.Env().ready()
    service = OwnershipIdentityService(env.conn, now=env.now)
    service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    start = "open-ownership:statement:oo-fixture-sk-ent-1"

    def chain():
        graph = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
        result = graph.control_chain(start, "2025-06-01", max_depth=5)
        return graph, [p["entities"] for p in result["paths"]]

    graph, paths = chain()
    assert paths == [[start, "open-ownership:statement:oo-fixture-sk-ent-dk"]]  # SK register only
    dk = harness.candidate(service, "open-ownership:statement:oo-fixture-sk-ent-dk",
                           "open-ownership:statement:oo-fixture-dk-ent-1")
    gb = harness.candidate(service, "open-ownership:statement:oo-fixture-dk-ent-gb", "companies-house:gb-coh:09990001")
    oc = harness.candidate(service, "opencorporates:company:dk:99990011", "open-ownership:statement:oo-fixture-dk-ent-1")
    assert dk["basis"] == gb["basis"] == "exact-identifier"
    assert oc["basis"] == "cross-referenced-identifier"  # the aggregator never carries a primary identity
    for item in (dk, gb):
        service.review(NS, item["candidate_id"], "accept", "same register number", principal_id=harness.REVIEWER,
                       scopes=harness.REVIEW_SCOPES)
    graph, paths = chain()
    assert len(paths) == 1 and len(paths[0]) == 3
    assert paths[0][-1] == graph.cluster("companies-house:gb-coh:09990001")
    jurisdictions = [{n["jurisdiction"] for n in graph.describe(e)["names"]} for e in paths[0]]
    assert ["SK" in jurisdictions[0], "DK" in jurisdictions[1], "GB" in jurisdictions[2]] == [True, True, True]
    # The two registers' statements about the Danish company's holder coexist as a conflict.
    parents = graph.direct_parents("open-ownership:statement:oo-fixture-dk-ent-1", "2019-03-01")
    assert parents["conflicts"] and len(parents["groups"]) == 2
    service.revert(NS, gb["candidate_id"], "checking", principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    graph, paths = chain()
    assert paths[0][-1] != graph.cluster("companies-house:gb-coh:09990001")
