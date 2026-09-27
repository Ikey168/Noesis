"""Plans linked to acts and dossiers by explicit citation, beneficiaries to award history as context (#1968)."""

from __future__ import annotations

import pytest

from src.kb.public_finance import PublicFinanceError, PublicFinanceStore
from src.kb.public_finance_identity import PublicFinanceIdentity
from src.kb.public_finance_links import PublicFinanceLinks
from tests.unit import public_finance_harness as h

DE_BENEFICIARY = "public-finance:beneficiary:eu-fts:vat:DE:DE999999999"


@pytest.fixture()
def conn():
    conn = h.connection()
    h.load_budgets(conn)
    yield conn
    conn.close()


def _plan(conn, provider, kind):
    return next(
        p
        for p in PublicFinanceStore(conn).plans(h.NS, provider=provider)
        if p["figure_kind"] == kind
    )


def test_acts_link_only_by_an_exact_stated_citation_and_titles_are_candidates(conn):
    h.load_acts(conn)
    links = PublicFinanceLinks(conn)
    with pytest.raises(PublicFinanceError):
        links.link_acts(
            h.NS, legal_namespace=h.LEGAL_NS, principal_id="a", scopes=h.SCOPES
        )
    scopes = h.REVIEW_SCOPES | h.LEGAL_SCOPES
    result = links.link_acts(
        h.NS, legal_namespace=h.LEGAL_NS, principal_id="a", scopes=scopes
    )
    assert (
        len(result["linked"]) == 2 and len(result["unresolved"]) == 1
    )  # both Berlin years cite the act
    berlin = _plan(conn, "berlin-senfin", "plan")
    (linked,) = links.links(
        h.NS, scopes=scopes, subject_id=berlin["plan_id"], states=["linked"]
    )
    assert (
        linked["basis"] == "explicit-citation"
        and linked["reference"]["identifier"] == "GVBl. 2098 S. 999"
    )
    assert linked["evidence"]["plan_source_revision"]["provider"] == "berlin-senfin"
    assert linked["rule"] == "exact identifier via lookup_legal_work"
    federal = _plan(conn, "bundeshaushalt", "plan")
    (unresolved,) = links.links(
        h.NS, scopes=scopes, subject_id=federal["plan_id"], target_kind="legal-work"
    )
    assert unresolved["state"] == "unresolved" and unresolved["target_id"] is None
    assert unresolved["reference"]["identifier"] == "BGBl. 2098 I Nr. 999"
    # The supplementary Berlin act shares words and the year with the Berlin plan: a candidate, never a link.
    (candidate,) = links.links(
        h.NS, scopes=scopes, subject_id=berlin["plan_id"], states=["candidate"]
    )
    assert (
        candidate["basis"] == "discovery"
        and "Nachtragshaushaltsgesetz" in candidate["evidence"]["work_title"]
    )
    again = links.link_acts(
        h.NS, legal_namespace=h.LEGAL_NS, principal_id="a", scopes=scopes
    )
    assert again == {"linked": [], "unresolved": [], "candidates": []}
    accepted = links.review(
        h.NS,
        candidate["link_id"],
        "accept",
        "the plan cites it on page 2",
        principal_id="r",
        scopes=scopes,
    )
    assert accepted["link_kind"] == "reviewed-assertion" and accepted["reviewer"] == "r"
    reverted = links.revert(
        h.NS, candidate["link_id"], "wrong act", principal_id="r", scopes=scopes
    )
    assert reverted["state"] == "reverted" and [
        s["state"] for s in reverted["history"]
    ] == ["candidate", "accepted", "reverted"]
    with pytest.raises(PublicFinanceError) as own:
        links.revert(h.NS, linked["link_id"], "x", principal_id="r", scopes=scopes)
    assert (
        own.value.code == "invalid_state"
    )  # an explicit citation is the source's own statement


def test_dossiers_link_by_their_own_identifier_and_keywords_are_candidates(conn):
    dossiers = h.budget_dossiers(conn)
    links = PublicFinanceLinks(conn)
    scopes = h.REVIEW_SCOPES | dossiers["scopes"]
    budget = links.link_dossier(
        h.NS,
        h.DOSSIER_NS,
        dossiers["budget"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    federal = _plan(conn, "bundeshaushalt", "plan")
    (linked,) = [links.link(h.NS, i, scopes=scopes) for i in budget["linked"]]
    assert (
        linked["subject_id"] == federal["plan_id"]
        and linked["reference"]["key"] == "de-drucksache:99/1001"
    )
    assert linked["target_revision"] == "1" and linked["basis"] == "dossier-identifier"
    supplementary = links.link_dossier(
        h.NS,
        h.DOSSIER_NS,
        dossiers["supplementary"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    assert (
        supplementary["linked"] == []
    )  # the Nachtrag states 99/1002, the dossier is 99/1009
    (candidate,) = [
        links.link(h.NS, i, scopes=scopes) for i in supplementary["candidates"]
    ]
    assert (
        candidate["state"] == "candidate"
        and candidate["subject_id"]
        == _plan(conn, "bundeshaushalt", "supplementary_plan")["plan_id"]
    )
    with pytest.raises(PublicFinanceError) as denied:
        links.link_dossier(
            h.NS,
            h.DOSSIER_NS,
            dossiers["budget"]["dossier_id"],
            principal_id="alice",
            scopes=h.REVIEW_SCOPES,
        )
    assert denied.value.code == "unauthorized"


def test_award_history_is_context_for_a_reviewed_supplier_match_only(conn):
    h.load_awards(conn)
    links = PublicFinanceLinks(conn)
    scopes = h.REVIEW_SCOPES | h.PROCUREMENT_SCOPES
    before = links.award_context(h.NS, DE_BENEFICIARY, h.PROCUREMENT_NS, scopes=scopes)
    assert before["awards"] == [] and "no reviewed supplier match" in before["note"]
    proposed = links.propose_award_parties(
        h.NS, h.PROCUREMENT_NS, principal_id="a", scopes=scopes
    )
    (candidate,) = proposed["candidates"]
    assert (
        candidate["basis"] == "cross-referenced-identifier"
        and DE_BENEFICIARY in candidate["records"]
    )
    identity = PublicFinanceIdentity(conn)
    identity.service.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "same VAT number",
        principal_id="r",
        scopes=scopes,
    )
    context = links.award_context(h.NS, DE_BENEFICIARY, h.PROCUREMENT_NS, scopes=scopes)
    (award,) = context["awards"]
    assert (
        award["notice_id"] == "999001-2099"
        and "never shows that a payment was made" in award["semantics"]
    )
    identity.service.revert(
        h.NS,
        candidate["candidate_id"],
        "different company",
        principal_id="r",
        scopes=scopes,
    )
    assert (
        links.award_context(h.NS, DE_BENEFICIARY, h.PROCUREMENT_NS, scopes=scopes)[
            "awards"
        ]
        == []
    )
    with pytest.raises(PublicFinanceError):
        links.award_context(
            h.NS, DE_BENEFICIARY, h.PROCUREMENT_NS, scopes=h.REVIEW_SCOPES
        )
