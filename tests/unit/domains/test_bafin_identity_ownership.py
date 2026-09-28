"""Reviewable identity for BaFin parties and voting_rights control assertions in Corporate Ownership (#2106, BF07, BF08)."""

from __future__ import annotations

import pytest

from src.domains.market.bafin_identity import (
    BafinIdentity,
    organisation_key,
    person_key,
    resolve_issuer,
)
from src.domains.market.bafin_notices import BafinError
from src.domains.market.bafin_ownership import BafinOwnershipProjection
from src.kb.ownership_graph import query
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_store import OwnershipError, OwnershipStore
from tests.unit import bafin_harness as h

HOLDING = organisation_key("Fiktiva Holding SE")


def world(*, until=None):
    conn = h.connection()
    h.acquire_all(conn, until=until)
    h.instruments(conn)
    h.ownership_entities(conn)
    return conn


def candidate(identity, left, right):
    return next(
        c
        for c in identity.candidates(h.NS, scopes=h.SCOPES)
        if sorted(c["records"]) == sorted([left, right])
    )


def test_issuers_resolve_by_isin_as_of_the_notice_date_and_failures_stay_unresolved():
    conn = world()
    issuer = {"isin": h.ISSUER, "lei": h.ISSUER_LEI, "name": "Musterwerke AG"}
    resolved = resolve_issuer(
        conn,
        issuer,
        on="2026-03-05",
        market_namespace=h.MARKET_NS,
        principal_id=h.PRINCIPAL,
        scopes={"operator"},
    )
    assert resolved["instrument"]["status"] == "resolved"
    assert resolved["instrument"]["issuer_id"] == "issuer:musterwerke"
    before = resolve_issuer(
        conn,
        issuer,
        on="2019-06-01",
        market_namespace=h.MARKET_NS,
        principal_id=h.PRINCIPAL,
        scopes={"operator"},
    )
    assert (
        before["instrument"]["status"] == "unresolved"
    )  # the security was not on record then
    other = resolve_issuer(
        conn,
        {"isin": h.OTHER_ISSUER},
        on="2026-03-05",
        market_namespace=h.MARKET_NS,
        principal_id=h.PRINCIPAL,
        scopes={"operator"},
    )
    assert other["instrument"]["status"] == "unresolved"
    lei = resolve_issuer(
        conn,
        issuer,
        on="2026-03-05",
        market_namespace=None,
        principal_id=h.PRINCIPAL,
        scopes={"operator"},
        lei_namespace="global",
    )
    assert lei["lei"] == {
        "status": "unresolved",
        "lei": h.ISSUER_LEI,
        "reason": "LEI records have not been acquired",
    }
    assert lei["instrument"]["status"] == "not_requested"
    empty = resolve_issuer(
        h.connection(),
        issuer,
        on="2026-03-05",
        market_namespace=h.MARKET_NS,
        principal_id=h.PRINCIPAL,
        scopes={"operator"},
    )
    assert (
        empty["instrument"]["reason"]
        == "the market instrument master has not been acquired"
    )


def test_candidates_carry_evidence_and_stay_proposed_and_persons_are_never_proposed():
    conn = world()
    identity = BafinIdentity(conn)
    first = identity.propose(
        h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS
    )
    candidates = first["candidates"]
    assert candidates and all(c["state"] == "proposed" for c in candidates)
    bases = {tuple(sorted(c["records"])): c["basis"] for c in candidates}
    assert (
        bases[("bafin:authorised:123456", f"lei:{h.FIKTIVA_INVEST_LEI}")]
        == "exact-identifier"
    )
    assert bases[(HOLDING, "register:fiktiva-holding")] == "name-jurisdiction"
    assert (
        bases[("bafin:named:fiktiva invest", f"lei:{h.FIKTIVA_INVEST_LEI}")]
        == "similar-name"
    )
    assert not [
        c
        for c in candidates
        if any(k.startswith("bafin:person:") for k in c["records"])
    ]
    assert (
        candidate(identity, HOLDING, "register:fiktiva-holding")["evidence"][0][
            "country"
        ]
        == "DE"
    )
    again = identity.propose(
        h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS
    )
    assert again["proposed"] == []  # idempotent
    # A warning string is never attributed: its similar-name candidate cannot be accepted.
    warning = candidate(
        identity, "bafin:named:fiktiva invest", f"lei:{h.FIKTIVA_INVEST_LEI}"
    )
    with pytest.raises(OwnershipError) as caught:
        identity.service.review(
            h.NS,
            warning["candidate_id"],
            "accept",
            "same name",
            principal_id="reviewer",
            scopes=h.SCOPES,
        )
    assert caught.value.code == "insufficient_evidence"


def test_stronger_evidence_upgrades_a_pending_candidate():
    conn = world()
    identity = BafinIdentity(conn)
    identity.propose(
        h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS
    )
    weak = candidate(
        identity, organisation_key("Fiktiva Invest GmbH"), f"lei:{h.FIKTIVA_INVEST_LEI}"
    )
    assert weak["basis"] == "similar-name"
    upgraded = identity.propose_link(
        h.NS,
        organisation_key("Fiktiva Invest GmbH"),
        target_key=f"lei:{h.FIKTIVA_INVEST_LEI}",
        target_entity=None,
        evidence={
            "kind": "name",
            "value": "Fiktiva Invest GmbH",
            "target_source": "GLEIF record legal name",
            "attributes": [{"kind": "country", "value": "DE"}],
        },
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    assert upgraded["change"] == "upgraded"
    assert (
        candidate(
            identity,
            organisation_key("Fiktiva Invest GmbH"),
            f"lei:{h.FIKTIVA_INVEST_LEI}",
        )["basis"]
        == "name-jurisdiction"
    )
    with pytest.raises(BafinError) as caught:
        identity.propose_link(
            h.NS,
            organisation_key("Fiktiva Invest GmbH"),
            target_key="lei:X",
            target_entity=None,
            evidence={"kind": "lei", "value": h.ISSUER_LEI, "target_source": "x"},
            principal_id="reviewer",
            scopes=h.SCOPES,
        )
    assert (
        caught.value.code == "not_stated"
    )  # candidates rest only on what a notice states


def test_natural_persons_are_keyed_within_the_issuer_and_cross_issuer_links_need_a_reviewer():
    assert person_key(h.ISSUER, "Dr. Erika Musterfrau") == person_key(
        h.ISSUER, "Erika Musterfrau"
    )
    assert person_key(h.ISSUER, "Erika Musterfrau") != person_key(
        h.OTHER_ISSUER, "Erika Musterfrau"
    )
    conn = world()
    identity = BafinIdentity(conn)
    parties = identity.parties(h.NS)
    managers = [p for p in parties.values() if p["kind"] == "natural_person"]
    assert {tuple(p["issuers"]) for p in managers} == {(h.ISSUER,)}
    erika = next(p for p in managers if "Dr. Erika Musterfrau" in p["names"])
    link = identity.propose_link(
        h.NS,
        erika["record_key"],
        target_key=person_key(h.OTHER_ISSUER, "Erika Musterfrau"),
        target_entity=None,
        evidence={
            "kind": "name",
            "value": "Erika Musterfrau",
            "target_source": "annual report",
        },
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    stored = identity.service._row(h.NS, link["candidate_id"])
    assert stored["state"] == "proposed" and stored["basis"] == "similar-name"
    # Withdrawn person data is never a party: the removed transaction's manager no longer appears by name.
    assert all(
        not name.startswith("[withdrawn") for p in managers for name in p["names"]
    )


def test_projection_writes_voting_rights_assertions_per_chain_member_and_basis_citing_the_revision():
    conn = world(until="2026-03-10")
    projection = BafinOwnershipProjection(conn)
    result = projection.project(
        h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    assert (
        result["counts"]["inserted"] == 1 + 5
    )  # the issuer entity and five stated percentages
    store = OwnershipStore(conn)
    assertions = [
        v
        for v in store.records(
            h.OWN_NS,
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
            kinds=("ownership_assertion",),
        )
    ]
    assert all(a["record"]["assertion_kind"] == "voting_rights" for a in assertions)
    invest = [
        a["record"]
        for a in assertions
        if a["record"]["holder"]["name"] == "Fiktiva Invest GmbH"
    ]
    assert {(a["share"]["exact"], a["basis"]) for a in invest} == {
        ("5.12", "WpHG § 33 (shares) as stated by the notifier"),
        ("5.12", "WpHG § 39 (total) as stated by the notifier"),
    }
    body = invest[0]
    assert (
        body["native"]["chain_position"] == 3
        and body["native"]["controlled_by"] == "Fiktiva Beteiligungs GmbH"
    )
    assert (
        body["source"]["provider"] == "bafin"
        and body["source"]["provider_record_id"] == "VR-2026-0001"
    )
    assert body["source"]["statement_id"].startswith("bafin-rev:")
    assert (
        body["validity"]["from"] == "2026-03-02"
        and body["statement_date"] == "2026-03-05"
    )
    assert body["holder"] == {
        "name": "Fiktiva Invest GmbH",
        "kind": "entity",
    }  # unreviewed: a source string
    assert projection.project(
        h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )["counts"] == {"inserted": 0, "revised": 0, "unchanged": 6}


def test_a_correction_supersedes_the_projected_assertion_and_history_is_kept():
    conn = world(until="2026-03-10")
    projection = BafinOwnershipProjection(conn)
    projection.project(h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    h.acquire(conn, "voting", "2026-04-20")
    result = projection.project(
        h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    assert (
        result["counts"]["revised"] == 5 and result["counts"]["inserted"] == 3
    )  # corrected chain + Nordlicht
    store = OwnershipStore(conn)
    corrected = next(
        v
        for v in store.records(
            h.OWN_NS,
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
            kinds=("ownership_assertion",),
        )
        if v["record"]["holder"]["name"] == "Fiktiva Invest GmbH"
        and v["record"]["basis"].startswith("WpHG § 33")
    )
    history = store.history(
        h.OWN_NS, corrected["record_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    assert [v["record"]["share"]["exact"] for v in history] == ["5.12", "5.21"]
    assert [v["record"]["source"]["provider_record_id"] for v in history] == [
        "VR-2026-0001",
        "VR-2026-0007",
    ]


def test_a_member_a_correction_drops_is_closed_not_deleted():
    conn = world(until="2026-03-10")
    projection = BafinOwnershipProjection(conn)
    projection.project(h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    lines = (h.FIXTURES / "voting_rights_2026-04-20.csv").read_text().splitlines()
    header, rows = lines[0], "\n".join(lines[1:])
    rows = rows.replace(
        "Fiktiva Beteiligungs GmbH | - | - | -\nFiktiva Invest GmbH | 5,21 | - | 5,21",
        "Fiktiva Invest GmbH | 5,21 | - | 5,21",
    )
    rows = rows.replace(
        "Fiktiva Holding SE | - | - | -\nFiktiva Invest GmbH | 5,21 | - | 5,21",
        "Fiktiva Holding SE | - | - | -\nFiktiva Neu GmbH | 5,21 | - | 5,21",
    )
    source = h.fictional("voting", ["dropped.csv"])
    from src.domains.market.bafin_notices import BafinNoticeProjector
    from src.ingestion.bafin_sources import BafinNoticeAdapter, fixture_transport

    pages = [
        {
            "request": "/fixture/dropped.csv",
            "status": 200,
            "body": header + "\n" + rows + "\n",
        }
    ]
    page = BafinNoticeAdapter(source, transport=fixture_transport(pages)).fetch_page(
        {"operation": "documents", "parameters": {}, "limit": 100}, cursor=None
    )
    BafinNoticeProjector(conn).project_page(
        run_id="dropped",
        manifest={},
        source=source,
        records=page.records,
        documents=[{"ingested_at": h.ms("2026-04-20")}],
        page_receipt=dict(page.receipt),
        principal_id=h.PRINCIPAL,
    )
    result = projection.project(
        h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    assert len(result["closed"]) == 2  # Fiktiva Invest GmbH's § 33 and § 39 assertions
    closed = [
        v["record"]
        for v in OwnershipStore(conn).records(
            h.OWN_NS,
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
            kinds=("ownership_assertion",),
        )
        if v["record"]["record_key"] in result["closed"]
    ]
    assert all(
        c["validity"]["from"] == c["validity"]["to"]
        and c["relationship_status"].startswith("dropped")
        for c in closed
    )
    graph = query(
        conn,
        h.OWN_NS,
        "direct_parents",
        "bafin-issuer:" + h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        as_of="2026-05-01",
    )
    excluded = {e["record_id"] for e in graph["excluded"]}
    assert {
        v["record_id"]
        for v in OwnershipStore(conn).records(
            h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
        )
        if v["record"]["record_key"] in result["closed"]
    } <= excluded
    assert (
        projection.project(h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
            "counts"
        ]["revised"]
        == 0
    )


def test_a_reviewed_holder_keys_the_assertion_and_a_revert_restores_the_source_string():
    conn = world()
    identity = BafinIdentity(conn)
    identity.propose(
        h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS
    )
    projection = BafinOwnershipProjection(conn)
    projection.project(h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    pending = candidate(identity, HOLDING, "register:fiktiva-holding")
    identity.service.review(
        h.NS,
        pending["candidate_id"],
        "accept",
        "register name and seat country agree",
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    projection.project(h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    graph = query(
        conn,
        h.OWN_NS,
        "direct_parents",
        "bafin-issuer:" + h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        as_of="2026-05-01",
    )
    holders = {
        e["holder"]["name"]: e["holder"]
        for e in graph["other_holdings"]["minority_as_stated"]
    }
    assert holders["Fiktiva Holding SE"]["key"] == "register:fiktiva-holding"
    assert holders["Fiktiva Invest GmbH"].get("key") is None
    assert "voting_rights" in {
        e["assertion_kind"] for e in graph["other_holdings"]["minority_as_stated"]
    }
    # The BaFin party link never regroups ownership entities.
    assert OwnershipIdentityService(conn).clusters(h.NS) == {}
    identity.service.revert(
        h.NS,
        pending["candidate_id"],
        "wrong register",
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    projection.project(h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    graph = query(
        conn,
        h.OWN_NS,
        "direct_parents",
        "bafin-issuer:" + h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        as_of="2026-05-01",
    )
    holders = {
        e["holder"]["name"]: e["holder"]
        for e in graph["other_holdings"]["minority_as_stated"]
    }
    assert holders["Fiktiva Holding SE"].get("key") is None
    # The graph never computes an ultimate owner from these statements.
    ultimate = query(
        conn,
        h.OWN_NS,
        "ultimate_parents",
        "bafin-issuer:" + h.ISSUER,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        as_of="2026-05-01",
    )
    assert ultimate["groups"] == []


def test_projection_needs_bafin_read_and_ownership_write():
    conn = world(until="2026-03-10")
    with pytest.raises(OwnershipError):
        BafinOwnershipProjection(conn).project(
            h.NS,
            h.OWN_NS,
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES - {"knowledge:ownership:write"},
        )
    with pytest.raises(BafinError):
        BafinOwnershipProjection(conn).project(
            h.NS,
            h.OWN_NS,
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES - {"market:bafin:read"},
        )


def _open_assertions(conn):
    rows = OwnershipStore(conn).records(
        h.OWN_NS,
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        kinds=("ownership_assertion",),
    )
    return sorted(
        (
            v["record"]["record_key"],
            v["record"]["holder"]["name"],
            v["record"]["basis"],
            v["record"]["share"]["exact"],
        )
        for v in rows
        if v["record"]["validity"].get("to_status") != "stated"
    )


def _voting_notice(conn, source_id, day):
    """One row of the later voting-rights export applied on its own at a simulated observation day."""
    from src.domains.market.bafin_notices import BafinNoticeProjector
    from src.ingestion.bafin_sources import BafinNoticeAdapter, fixture_transport

    lines = (h.FIXTURES / "voting_rights_2026-03-10.csv").read_text().splitlines()
    later = (h.FIXTURES / "voting_rights_2026-04-20.csv").read_text()
    rows = {
        "VR-2026-0001": "\n".join(lines[1:5]),
        "VR-2026-0007": later.split("\n", 1)[1].split('\n"VR-2026-0009"')[0],
    }
    body = lines[0] + "\n" + rows[source_id] + "\n"
    source = h.fictional("voting", [f"{source_id}.csv"])
    source["bafin"]["documents"][0]["listing"] = "partial"
    source = h._rehash(source)
    pages = [{"request": f"/fixture/{source_id}.csv", "status": 200, "body": body}]
    page = BafinNoticeAdapter(source, transport=fixture_transport(pages)).fetch_page(
        {"operation": "documents", "parameters": {}, "limit": 100}, cursor=None
    )
    assert [r["bafin_notice"]["source"]["source_id"] for r in page.records] == [
        source_id
    ]
    BafinNoticeProjector(conn).project_page(
        run_id=f"run:{source_id}",
        manifest={},
        source=source,
        records=page.records,
        documents=[{"ingested_at": h.ms(day)}],
        page_receipt=dict(page.receipt),
        principal_id=h.PRINCIPAL,
    )


@pytest.mark.parametrize(
    "order", [("VR-2026-0001", "VR-2026-0007"), ("VR-2026-0007", "VR-2026-0001")]
)
def test_projection_is_the_same_whichever_order_a_correction_and_its_original_arrive(
    order,
):
    results = []
    for sequence in (order, ("VR-2026-0001", "VR-2026-0007")):
        conn = h.connection()
        for index, source_id in enumerate(sequence):
            _voting_notice(conn, source_id, f"2026-04-{20 + index}")
            BafinOwnershipProjection(conn).project(
                h.NS, h.OWN_NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
            )
        results.append(_open_assertions(conn))
    first, reference = results
    assert first == reference
    # One open assertion per holder and basis, all from the correction (5.21 %), never duplicated.
    holders = [(name, basis) for _, name, basis, _ in first]
    assert len(holders) == len(set(holders)) == 5
    assert {share for _, name, _, share in first if name == "Fiktiva Invest GmbH"} == {
        "5.21"
    }
