"""Series linked to legal acts, court decisions and legislative dossiers by citation only (#1988)."""

from __future__ import annotations

import pytest

from src.kb.demographics import DemographicError, DemographicStore, forbidden_keys
from src.kb.demographics_links import DemographicLinks
from tests.unit import demographics_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn, references=True)
    h.load_acts(conn)
    dossier = h.load_dossier(conn)
    yield conn, dossier
    conn.close()


def _series(conn, **filters):
    return DemographicStore(conn, initialize=False).find_series(h.NS, **filters)


def test_a_publisher_reference_links_the_act_in_its_jurisdiction_only(env):
    conn, _ = env
    links = DemographicLinks(conn)
    result = links.link_references(
        h.NS,
        legal_namespace=h.LEGAL_NS,
        principal_id="op",
        scopes=h.SCOPES | h.LEGAL_SCOPES,
    )
    (link_id,) = result["linked"]
    link = links.link(h.NS, link_id, scopes=h.READ_ONLY)
    immigration = _series(conn, series_code="migr_imm1ctz")[0]
    assert link["subject_id"] == immigration["series_id"]
    assert (
        link["basis"] == "publisher_reference" and link["relation"] == "reported_under"
    )
    assert link["evidence"]["stated_in"]["provider"] == "eurostat"
    assert "does not assert" in link["note"] and forbidden_keys(link) == []
    again = links.link_references(
        h.NS,
        legal_namespace=h.LEGAL_NS,
        principal_id="op",
        scopes=h.SCOPES | h.LEGAL_SCOPES,
    )
    assert again == {"linked": [], "unresolved": []}
    citing = links.series_citing(h.NS, link["target_id"], scopes=h.READ_ONLY)
    assert [s["series_code"] for s in citing["series"]] == ["migr_imm1ctz"]
    with pytest.raises(DemographicError):
        links.link_references(
            h.NS,
            legal_namespace=h.LEGAL_NS,
            principal_id="op",
            scopes=h.SCOPES - {"knowledge:legal:read"},
        )


def test_a_dossier_links_only_through_a_stated_printed_paper(env):
    conn, dossier = env
    links = DemographicLinks(conn)
    scopes = h.SCOPES | dossier["scopes"]
    dossier_id = dossier["dossier"]["dossier_id"]
    result = links.link_dossier(
        h.NS, h.DOSSIER_NS, dossier_id, principal_id="alice", scopes=scopes
    )
    # The 2098 district file states the printed paper: each of its six series is linked.
    assert len(result["linked"]) == 6
    series = {
        links.link(h.NS, i, scopes=h.READ_ONLY)["subject_id"] for i in result["linked"]
    }
    berlin = {s["series_id"] for s in _series(conn, provider="statistik-bb")}
    assert series <= berlin
    assert all(
        links.link(h.NS, i, scopes=h.READ_ONLY)["target_revision"]
        for i in result["linked"]
    )


def test_assertions_link_only_after_review_and_candidates_stay_candidates(env):
    conn, _ = env
    links = DemographicLinks(conn)
    scopes = h.SCOPES | h.LEGAL_SCOPES
    decisions = _series(conn, series_code="migr_asydcfsta")[0]
    work_id = conn.execute(
        "SELECT work_id FROM legal_works WHERE work_kind='decision'"
    ).fetchone()[0]
    asserted = links.assert_link(
        h.NS,
        subject={"series_id": decisions["series_id"]},
        target={"kind": "legal-work", "namespace": h.LEGAL_NS, "id": work_id},
        relation="referenced_in",
        locator="Rn. 12",
        statement="The decision cites the first-instance decision statistics (fictional)",
        principal_id="alice",
        scopes=scopes,
    )
    assert asserted["state"] == "asserted" and asserted["counts_as_link"] is False
    with pytest.raises(DemographicError):
        links.review(
            h.NS,
            asserted["link_id"],
            "accept",
            "self",
            principal_id="alice",
            scopes=h.REVIEW_SCOPES,
        )
    accepted = links.review(
        h.NS,
        asserted["link_id"],
        "accept",
        "checked Rn. 12",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    assert accepted["counts_as_link"] and accepted["link_kind"] == "reviewed-assertion"
    reverted = links.revert(
        h.NS, asserted["link_id"], "misread", principal_id="rev", scopes=h.REVIEW_SCOPES
    )
    assert reverted["counts_as_link"] is False
    proposed = links.propose_candidates(
        h.NS, legal_namespace=h.LEGAL_NS, principal_id="op", scopes=scopes
    )
    assert proposed["candidates"]
    for link_id in proposed["candidates"]:
        candidate = links.link(h.NS, link_id, scopes=h.READ_ONLY)
        assert (
            candidate["basis"] == "discovery" and candidate["counts_as_link"] is False
        )
    # The publisher reference then upgrades the pending candidate for the same pair.
    links.link_references(
        h.NS, legal_namespace=h.LEGAL_NS, principal_id="op", scopes=scopes
    )
    immigration = _series(conn, series_code="migr_imm1ctz")[0]["series_id"]
    states = {
        item["basis"]: item["state"]
        for item in links.links(h.NS, scopes=h.READ_ONLY, subject_id=immigration)
    }
    assert states["publisher_reference"] == "linked"
    assert states.get("discovery") in (None, "superseded")


def test_an_ecli_is_looked_up_in_its_issuing_country(env):
    conn, _ = env
    links = DemographicLinks(conn)
    item = h.source("eurostat", references=True)
    item["demographics"]["documents"][4]["references"] = [
        {"scheme": "ecli", "identifier": h.DECISION_ECLI, "relation": "referenced_in"},
        {
            "scheme": "ecli",
            "identifier": "ECLI:AT:VWGH:2099:999",
            "relation": "referenced_in",
        },
    ]
    h.apply(conn, "eurostat", 4, item=item)
    result = links.link_references(
        h.NS,
        legal_namespace=h.LEGAL_NS,
        principal_id="op",
        scopes=h.SCOPES | h.LEGAL_SCOPES,
    )
    kinds = {
        links.link(h.NS, i, scopes=h.READ_ONLY)["reference"]["identifier"]: links.link(
            h.NS, i, scopes=h.READ_ONLY
        )["state"]
        for i in result["linked"] + result["unresolved"]
    }
    assert kinds[h.DECISION_ECLI] == "linked"
    assert kinds["ECLI:AT:VWGH:2099:999"] == "unresolved"
