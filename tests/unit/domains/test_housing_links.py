"""Rent-index editions and plans linked to legal works and decisions by explicit citation (#1995, U08)."""

from __future__ import annotations

import pytest

from src.kb.housing import HousingError, HousingStore
from src.kb.housing_links import HousingLinks
from tests.unit import housing_harness as h
from tests.unit.domains.test_housing_records import _mietspiegel

NS = h.NS
SCOPES = h.REVIEW_SCOPES | h.LEGAL_SCOPES


@pytest.fixture()
def env():
    env = h.Env().world()
    env.bplan_stage_change()
    h.load_legal_works(env.conn)
    yield env
    env.conn.close()


def by_identifier(links):
    return {
        link["reference"]["identifier"]: link for link in links if link.get("reference")
    }


def test_explicit_citations_link_to_exactly_one_work_and_others_stay_unresolved_with_their_decision(
    env,
):
    links = HousingLinks(env.conn)
    result = links.link_citations(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    edition = by_identifier(
        links.links(NS, scopes=SCOPES, subject_kind="rent-index-edition")
    )
    assert edition["GVBl. 2098 S. 42"]["state"] == "linked"
    assert edition["GVBl. 2098 S. 42"]["relation"] == "legal_basis"
    assert "Mietspiegel" in edition["GVBl. 2098 S. 42"]["evidence"]["work_title"]
    assert (
        edition["GVBl. 2098 S. 42"]["evidence"]["stated_in"]["page"]
        == "S. 14, Mietspiegeltabelle"
    )
    assert (
        edition["BGBl. I 2097 S. 99"]["state"] == "unresolved"
    )  # the federal act is not acquired
    assert edition["ABl. 2099 S. 1234"]["state"] == "unresolved"
    plan = by_identifier(
        links.links(NS, scopes=SCOPES, subject_kind="plan", subject_id="1-99A")
    )
    fixed = plan["GVBl. 2099 S. 321"]
    assert fixed["state"] == "linked" and fixed["relation"] == "published_in"
    assert fixed["evidence"]["stage"] == {
        "stage": "festgesetzt",
        "stage_label": "festgesetzt",
        "stage_date": "2099-06-15",
        "plan_id": "1-99a",
    }
    decided = plan["ABl. 2098 S. 777"]
    assert (
        decided["state"] == "unresolved"
        and decided["evidence"]["decision_reference"] == "ABl. 2098 S. 777"
    )
    assert decided["evidence"]["stage"]["stage"] == "aufstellungsbeschluss"
    assert decided["evidence"]["stage"]["stage_date"] == "2098-03-15"
    other = by_identifier(
        links.links(NS, scopes=SCOPES, subject_kind="plan", subject_id="1-98")
    )
    assert (
        other["GVBl. 2097 S. 55"]["state"] == "unresolved"
    )  # the acquired ordinance states S. 56
    again = links.link_citations(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    assert again == {"linked": [], "unresolved": []} and result["linked"]
    assert all(
        link["counts_as_link"] == (link["state"] == "linked")
        for link in links.links(NS, scopes=SCOPES)
    )
    assert all(
        "not tenancy or legal advice" in link["note"]
        for link in links.links(NS, scopes=SCOPES)
    )


def test_name_similarity_is_a_candidate_reviewed_by_someone_else_and_reversible(env):
    links = HousingLinks(env.conn)
    proposed = links.propose_candidates(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    candidates = [links.link(NS, i, scopes=SCOPES) for i in proposed["candidates"]]
    plan_candidate = next(c for c in candidates if c["subject_id"] == "1-98")
    assert (
        plan_candidate["basis"] == "discovery"
        and plan_candidate["counts_as_link"] is False
    )
    assert "1-98" in plan_candidate["evidence"]["shared_words"]
    with pytest.raises(HousingError, match="someone other"):
        links.review(
            NS,
            plan_candidate["link_id"],
            "accept",
            "same plan",
            principal_id="alice",
            scopes=SCOPES,
        )
    accepted = links.review(
        NS,
        plan_candidate["link_id"],
        "accept",
        "same plan number",
        principal_id="bob",
        scopes=SCOPES,
    )
    assert accepted["state"] == "accepted" and accepted["counts_as_link"]
    reverted = links.revert(
        NS,
        plan_candidate["link_id"],
        "wrong ordinance",
        principal_id="bob",
        scopes=SCOPES,
    )
    assert reverted["state"] == "reverted" and not reverted["counts_as_link"]
    with pytest.raises(HousingError):
        links.revert(
            NS, plan_candidate["link_id"], "again", principal_id="bob", scopes=SCOPES
        )
    again = links.propose_candidates(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    assert plan_candidate["link_id"] not in again["candidates"]


def test_an_explicit_citation_supersedes_a_pending_candidate_for_the_same_pair(env):
    links = HousingLinks(env.conn)
    proposed = links.propose_candidates(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    pending = {
        links.link(NS, i, scopes=SCOPES)["subject_id"]: i
        for i in proposed["candidates"]
    }
    assert {"1-99A", "1-98"} <= set(pending)
    links.link_citations(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    superseded = links.link(NS, pending["1-99A"], scopes=SCOPES)
    assert superseded["state"] == "superseded" and superseded["history"][-1][
        "reason"
    ].startswith("an explicit")
    # 1-98's own citation matched nothing, so its name candidate stays pending for review.
    assert links.link(NS, pending["1-98"], scopes=SCOPES)["state"] == "candidate"


def test_a_printed_paper_reference_links_to_a_dossier_stage(env):
    from tests.unit import public_finance_harness as ph

    dossiers = ph.budget_dossiers(env.conn)
    header, items = _mietspiegel()
    header["document"] = {
        **header["document"],
        "references": [
            {
                "scheme": "de-drucksache",
                "identifier": "Drucksache 99/1001",
                "relation": "referenced_in",
            }
        ],
    }
    HousingStore(env.conn).apply_publication(
        NS, header, items, source_id="berlin-mietspiegel-dossier"
    )
    links = HousingLinks(env.conn)
    scopes = SCOPES | dossiers["scopes"] | {"knowledge:political:dossier:read"}
    result = links.link_dossier(
        NS,
        ph.DOSSIER_NS,
        dossiers["budget"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    (link_id,) = result["linked"]
    link = links.link(NS, link_id, scopes=scopes)
    assert link["target_kind"] == "dossier" and link["target_revision"] == str(
        result["dossier_revision"]
    )
    with pytest.raises(HousingError) as denied:
        links.link_dossier(
            NS,
            ph.DOSSIER_NS,
            dossiers["budget"]["dossier_id"],
            principal_id="alice",
            scopes=h.REVIEW_SCOPES - {"knowledge:political:dossier:read"},
        )
    assert denied.value.code == "unauthorized"


def test_news_items_are_discovery_context_only_and_links_appear_in_the_dossier(env):
    from src.kb.housing_places import HousingPlaces

    HousingLinks(env.conn).link_citations(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    from src.ingestion.document_store import DocumentStore

    DocumentStore(env.conn)
    env.conn.execute(
        "INSERT INTO documents (document_id, source_type, source_id, url, content_hash, title, content, created_at, "
        "ingested_at) VALUES ('doc-1', 'news', 'fiktive-zeitung', 'https://news.example/1', 'h1', "
        "'Bebauungsplan 1-99a festgesetzt (fiktiv)', 'Der Plan 1-99a ...', 4102444800000, 4102444800000)"
    )
    answer = HousingPlaces(env.conn).dossier(
        NS,
        as_of="2100-06-01",
        principal_id="alice",
        scopes=SCOPES,
        point=h.wgs84(h.ADDRESS_UTM),
        include_news=True,
    )
    (news,) = answer["news_context"]
    assert news["mentions"] == "1-99a" and "not evidence" in news["role"]
    assert news["source"]["url"] == "https://news.example/1"
    cited = {
        link["reference"]["identifier"]
        for link in answer["links"]
        if link.get("reference")
    }
    assert {"GVBl. 2099 S. 321", "GVBl. 2098 S. 42"} <= cited
    assert all(
        link["state"] in {"linked", "accepted", "unresolved"}
        for link in answer["links"]
    )


def test_listing_survives_a_broken_link_row(env):
    links = HousingLinks(env.conn)
    links.link_citations(
        NS, legal_namespace=h.LEGAL_NS, principal_id="alice", scopes=SCOPES
    )
    first = links.links(NS, scopes=SCOPES)[0]
    env.conn.execute(
        "UPDATE housing_links SET evidence_json='{broken' WHERE link_id=?",
        [first["link_id"]],
    )
    listed = links.links(NS, scopes=SCOPES)
    assert sum(bool(item.get("invalid")) for item in listed) == 1 and len(listed) > 1
