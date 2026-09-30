"""RE07-RE09 (#2494, #2498, #2501): reviewable identity, citation links and as-of answers, offline."""

from __future__ import annotations

import json

import pytest

from src.kb.real_estate import RealEstateError, forbidden_keys
from src.kb.real_estate_identity import RealEstateIdentity
from src.kb.real_estate_links import RealEstateLinks
from src.kb.real_estate_queries import RealEstateQueries
from tests.unit.real_estate import fixture_builder as fb
from tests.unit.real_estate.harness import (
    NS,
    REVIEW_SCOPES,
    SCOPES,
    Env,
    load_legal_work,
    owner_markers,
    seed_places,
)


@pytest.fixture()
def world():
    env = Env().loaded()
    ids = seed_places(env)
    identity = RealEstateIdentity(env.conn, now=env.now)
    identity.propose(NS, principal_id="alice", scopes=SCOPES)
    return env, ids, identity


def _tx(env, provider, key):
    return env.store().find(NS, "transaction", provider, key)["record_id"]


def _parcel(env, reference):
    store = env.store()
    return next(r["record_id"] for r in store.records(NS, record_type="parcel")
                if store.current(NS, r["record_id"])["statement"]["as_published"]["national_cadastral_reference"]
                == reference)


def test_exact_candidate_rejected_and_unmatched_cases(world):
    env, ids, identity = world
    matches = identity.matches(NS, scopes=SCOPES)
    multi = _tx(env, "dvf", "2098-1001")
    exact = [m for m in matches if m["transaction_id"] == multi and m["target_kind"] == "parcel"]
    assert {m["target_id"] for m in exact} == {_parcel(env, "75104000AB0012"), _parcel(env, "75104000AB0013")}
    assert all(m["basis"] == "parcel-identifier" and m["state"] == "exact" for m in exact)
    place_codes = [m for m in matches if m["target_kind"] == "place" and m["basis"] == "place-code"]
    assert {m["target_id"] for m in place_codes} >= {ids["district"], ids["paris"], ids["point"]}
    # Address-derived: a candidate until reviewed; accepted by a reviewer with an identity decision.
    address = next(m for m in matches if m["basis"] == "address")
    assert address["state"] == "proposed" and address["evidence_class"] == "candidate"
    accepted = identity.review(NS, address["match_id"], "accept", "same published address", principal_id="bob",
                               scopes=REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["decision_id"]
    # Geometry-derived: the synthetic point lies inside parcel AB0013; proximity is never an identity by itself.
    geometry = [m for m in matches if m["basis"] == "geometry-contains"]
    assert len(geometry) == 1 and geometry[0]["state"] == "proposed"
    assert geometry[0]["transaction_id"] == _tx(env, "hmlr-ppd", fb.TX2.strip("{}"))
    rejected = identity.review(NS, geometry[0]["match_id"], "reject", "a point in a parcel is not the sale",
                               principal_id="bob", scopes=REVIEW_SCOPES)
    assert rejected["state"] == "rejected"
    reverted = identity.revert(NS, geometry[0]["match_id"], "re-open", principal_id="bob", scopes=REVIEW_SCOPES)
    assert reverted["state"] == "reverted"
    with pytest.raises(RealEstateError):
        identity.review(NS, exact[0]["match_id"], "accept", "x", principal_id="bob", scopes=REVIEW_SCOPES)
    # Unmatched: no invented parcel, still queryable by place code.
    unmatched = {u["source_transaction_id"]: u for u in identity.unmatched(NS, scopes=SCOPES)}
    assert unmatched["2098-1002"]["reason"] == "published parcel id has no acquired parcel"
    assert unmatched[fb.TX1.strip("{}")]["reason"] == "the source publishes no parcel reference"
    answer = RealEstateQueries(env.conn).place(NS, scopes=SCOPES, principal_id="alice", as_of="2099-05-01",
                                               codes=[{"scheme": "insee-commune", "code": "75104"}])
    assert "2098-1002" in {t["source_transaction_id"] for t in answer["transactions"]}


def test_parcel_revisions_change_matches_only_through_a_recorded_rematch(world):
    env, _ids, identity = world
    env.parcel_revision()
    identity.propose(NS, principal_id="alice", scopes=SCOPES)  # re-proposal never updates silently
    parcel = _parcel(env, "75104000AB0013")
    (stale,) = [m for m in identity.matches(NS, scopes=SCOPES, transaction_id=_tx(env, "dvf", "2098-1001"))
                if m["target_id"] == parcel]
    assert stale["parcel_revised_since_match"]["pinned_revision_id"] == stale["target_revision_id"]
    result = identity.rematch(NS, stale["match_id"], "boundary corrected by the publisher", principal_id="alice",
                              scopes=SCOPES)
    assert identity.match(NS, stale["match_id"])["state"] == "superseded"
    assert result["match"]["state"] == "exact" and result["match"]["parcel_revised_since_match"] is None
    assert result["match"]["supersedes"] == stale["match_id"]


def test_links_come_from_shared_identifiers_or_citations_and_follow_revisions(world):
    from src.ingestion.connectors.dataset.store import ObservationStore
    from src.kb.housing import HousingStore

    env, _ids, _identity = world
    HousingStore(env.conn)
    env.conn.execute("INSERT INTO housing_plan_stages VALUES (" + ",".join("?" * 21) + ")",
                     ["global", "housing-record:plan-1", "plan-1", 1, None, "created", "h",
                      json.dumps({"parcels": ["75104000AB0012"], "plan": "fictional"}), "{}", "src", "prov", None, 1,
                      1, "bplan:x", "1-1", "festgesetzt", "2099-01-01", "f", "fr", None])
    env.conn.execute("INSERT INTO housing_indicator_vintages VALUES (" + ",".join("?" * 20) + ")",
                     ["global", "housing-record:ind-de", "ind-de", 1, None, "created", "h", "{}", "{}", "src",
                      "destatis", None, 2, 1, "genesis:31111", "DE", "dwellings", 1, "2099-01-01", "sr"])
    ObservationStore(env.conn)
    for series_id, unit in (("estat:prc_hpi_q:FR", "I15_Q"), ("estat:prc_hpi_q:FR:alt", "I10_Q")):
        env.conn.execute("INSERT INTO dataset_series VALUES (?,?,?,?,?,?,?,?,?,?)",
                         [series_id, "eurostat", "House price index", unit, "Q", "FR", "eurostat", 1, "u",
                          json.dumps({"dataset": "prc_hpi_q", "filters": {"unit": unit, "purchase": "TOTAL"}})])
    load_legal_work(env.conn)
    links = RealEstateLinks(env.conn, now=env.now)
    result = links.link_identifiers(NS, principal_id="alice", scopes=SCOPES)
    by_basis: dict[str, list] = {}
    for link in result["links"]:
        by_basis.setdefault(link["basis"], []).append(link)
    parcel12 = _parcel(env, "75104000AB0012")
    (reference,) = by_basis["parcel-reference"]
    assert reference["record_id"] == parcel12 and reference["target_id"] == "housing-record:plan-1"
    assert {link["identifier"] for link in by_basis["geography-code"]} == {"DE"}
    states = {(link["target_id"], link["state"]) for link in by_basis["dataset-code"]}
    assert ("estat:prc_hpi_q:FR", "linked") in states and ("estat:prc_hpi_q:FR:alt", "unresolved") in states
    de_parcel = _parcel(env, "053001001000120003______")
    cited = links.cite(NS, de_parcel, "GVBl. 2099 S. 777", relation="legal_basis", stated_in="fictional notice",
                       legal_namespace="legal", principal_id="alice", scopes=SCOPES)
    assert cited["state"] == "linked"
    missing = links.cite(NS, de_parcel, "GVBl. 2099 S. 999", relation="legal_basis", stated_in="fictional notice",
                         legal_namespace="legal", principal_id="alice", scopes=SCOPES)
    assert missing["state"] == "unresolved"
    # A parcel with no linked records; spatial containment is never a link.
    assert links.links(NS, scopes=SCOPES, record_id=_parcel(env, "75104000AB0013")) == []
    # Links survive revisions and point to the revision in force.
    fr_q2 = next(link for link in by_basis["dataset-code"] if ":FR:2098-Q2:" in env.store().record(
        NS, link["record_id"])["record_key"] and link["state"] == "linked")
    before = links.links(NS, scopes=SCOPES, record_id=fr_q2["record_id"])[0]["revision_in_force"]
    env.eurostat_vintage()
    after = next(link for link in links.links(NS, scopes=SCOPES, record_id=fr_q2["record_id"])
                 if link["link_id"] == fr_q2["link_id"])["revision_in_force"]
    assert before["revision_id"] != after["revision_id"] and after["release"].startswith("2099-04")
    as_of = next(link for link in links.links(NS, scopes=SCOPES, record_id=fr_q2["record_id"], as_of="2099-02-01")
                 if link["link_id"] == fr_q2["link_id"])["revision_in_force"]
    assert as_of["revision_id"] == before["revision_id"]


def test_place_answers_as_of_a_date_side_by_side_with_citations(world):
    env, ids, _identity = world
    queries = RealEstateQueries(env.conn)
    env.ppd_release_2()
    early = queries.place(NS, scopes=SCOPES, principal_id="alice", as_of="2099-03-01", place_id=ids["district"])
    late = queries.place(NS, scopes=SCOPES, principal_id="alice", as_of="2099-04-01", place_id=ids["district"])
    tx1 = fb.TX1
    by_id = {t["source_transaction_id"]: t for t in early["transactions"]}
    assert by_id[tx1]["price"]["value_text"] == "450000" and by_id[tx1]["later_revisions_exist"]
    by_id = {t["source_transaction_id"]: t for t in late["transactions"]}
    assert by_id[tx1]["price"]["value_text"] == "455000" and by_id[tx1]["event"] == "changed"
    assert by_id[fb.TX2]["status"] == "withdrawn" and len(by_id[fb.TX2]["revisions_known"]) == 2
    citation = by_id[tx1]["citation"]
    assert citation["publisher"].startswith("HM Land Registry") and citation["release"] == "2099-03"
    assert citation["attribution"].startswith("Contains HM Land Registry data")
    # UK HPI observations for the borough code sit beside the transactions, in their own unit.
    (index, average, volume) = sorted(late["indices"], key=lambda g: g["index_id"])[:3]
    assert {index["index_id"], average["index_id"], volume["index_id"]} == {
        "ukhpi:average_price", "ukhpi:index", "ukhpi:sales_volume"}
    assert all(g["provider"] == "hmlr-ukhpi" for g in late["indices"])
    assert "average" not in json.dumps([t["price"] for t in late["transactions"]])
    assert not forbidden_keys(late) and not owner_markers(late)


def test_paris_answer_lists_dvf_eurostat_and_parcels_and_a_changed_geometry(world):
    env, ids, _identity = world
    queries = RealEstateQueries(env.conn)
    env.dvf_release()
    env.parcel_revision()
    spring = queries.place(NS, scopes=SCOPES, principal_id="alice", as_of="2099-05-01", place_id=ids["paris"])
    autumn = queries.place(NS, scopes=SCOPES, principal_id="alice", as_of="2099-11-01", place_id=ids["paris"])
    assert {t["source_transaction_id"]: t["status"] for t in spring["transactions"]} == {
        "2098-1001": "published", "2098-1002": "published", "2098-1003": "published"}
    fall = {t["source_transaction_id"]: t for t in autumn["transactions"]}
    assert fall["2098-1003"]["status"] == "removed" and fall["2098-1002"]["price"]["value_text"] == "645000,00"
    multi = fall["2098-1001"]
    assert {m["parcel_record_id"] for m in multi["parcel_matches"]} == {
        _parcel(env, "75104000AB0012"), _parcel(env, "75104000AB0013")}
    providers = {g["provider"] for g in spring["indices"]}
    assert providers == {"eurostat-hpi"} and spring["indices"][0]["unit"] == "Index, 2015=100"
    assert {p["national_cadastral_reference"] for p in spring["parcels"]} == {"75104000AB0012", "75104000AB0013"}
    assert any("dvf" in n.casefold() and "re-identification" in n for n in autumn["notices"])
    parcel13 = _parcel(env, "75104000AB0013")
    before = queries.parcel(NS, scopes=SCOPES, principal_id="alice", as_of="2099-05-01", record_id=parcel13)
    after = queries.parcel(NS, scopes=SCOPES, principal_id="alice", as_of="2099-10-01", record_id=parcel13)
    assert len(before["parcel"]["revision_history"]) == 1 and len(after["parcel"]["revision_history"]) == 2
    assert before["parcel"]["geometry"]["geometry_id"] != after["parcel"]["geometry"]["geometry_id"]
    assert after["transactions"][0]["match"]["parcel_revised_since_match"]
    assert not owner_markers([spring, autumn, before, after])


def test_a_place_or_parcel_with_no_records_is_none_on_record(world):
    env, ids, _identity = world
    queries = RealEstateQueries(env.conn)
    empty = queries.place(NS, scopes=SCOPES, principal_id="alice", as_of="2099-12-31",
                          codes=[{"scheme": "insee-commune", "code": "75056"}])
    assert empty["status"] == "none_on_record" and not empty["transactions"] and not empty["indices"]
    before = queries.place(NS, scopes=SCOPES, principal_id="alice", as_of="2098-01-01", place_id=ids["paris"])
    assert before["status"] == "none_on_record"
    parcel = queries.parcel(NS, scopes=SCOPES, principal_id="alice", reference="75104000ZZ9999")
    assert parcel["status"] == "none_on_record"
    with pytest.raises(RealEstateError):
        queries.place(NS, scopes={"knowledge:housing:read"}, principal_id="alice", codes=[
            {"scheme": "insee-commune", "code": "75104"}])
