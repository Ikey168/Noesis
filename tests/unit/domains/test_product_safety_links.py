"""Products safety notices: reviewable product matches (R06, #1990) and citation links (R07, #2001)."""

from __future__ import annotations

import re

import pytest

from src.kb.product_safety import (
    ProductSafetyError,
    extract_citations,
    standard_key,
    token_regex,
    token_sql_pattern,
)
from tests.unit import product_safety_harness as h

NS = h.NS


@pytest.fixture(scope="module")
def env():
    item = h.Env().loaded()
    yield item
    item.conn.close()


@pytest.fixture()
def fresh():
    item = h.Env().loaded()
    yield item
    item.conn.close()


# ------------------------------------------------------------------ R06 matching


def test_brand_and_designation_propose_every_provider_model_and_flag_the_sibling(env):
    notice = env.notice("safety-gate", "SR/00417/26")
    states = {
        (m["model_id"], m["candidate_state"], m["basis"])
        for m in env.store.matches_for_notice(NS, notice)
    }
    assert states == {
        (env.model("icecat", "EX-32U8"), "proposed", "brand+designation"),
        (env.model("eprel", "EX-32U8"), "proposed", "brand+designation"),
        (env.model("icecat", "EX-32U8UK"), "ambiguous", "brand+designation-suffix"),
    }
    # Other sizes of the same family are not even candidates.
    assert env.model("icecat", "EX-34Q4") not in {s[0] for s in states}


def test_a_gtin_equal_to_a_variant_gtin_is_proposed(env):
    notice = env.notice("safety-gate", "SR/00431/26")
    match = env.candidate(notice, env.model("icecat", "EX-27Q4"))
    assert (match["candidate_state"], match["basis"]) == ("proposed", "gtin")
    gtin = next(e for e in match["evidence"] if e["kind"] == "gtin")
    # The notice's GTIN-13 and the product's zero-padded GTIN-14 meet through one shared key.
    assert gtin["notice"] == ["4012345000016"] and gtin["product_gtin_keys"] == [
        "04012345000016"
    ]


def test_a_gtin_that_contradicts_the_brand_or_model_cannot_be_accepted(env):
    notice = env.notice("safety-gate", "SR/00502/26")
    matches = env.store.matches_for_notice(NS, notice)
    assert matches and {m["candidate_state"] for m in matches} == {"contradicted"}
    with pytest.raises(ProductSafetyError) as caught:
        env.store.review_match(
            NS,
            matches[0]["match_id"],
            "accepted",
            "looks right",
            scopes=h.REVIEW,
            principal_id="reviewer",
        )
    assert caught.value.code == "contradicted_match"


def test_a_sibling_candidate_is_never_attached(env):
    notice = env.notice("safety-gate", "SR/00417/26")
    sibling = env.candidate(notice, env.model("icecat", "EX-32U8UK"))
    with pytest.raises(ProductSafetyError) as caught:
        env.store.review_match(
            NS,
            sibling["match_id"],
            "accepted",
            "same family",
            scopes=h.REVIEW,
            principal_id="reviewer",
        )
    assert caught.value.code == "sibling_not_named"


def test_reviews_are_append_only_and_a_later_review_reverses_an_earlier_one(fresh):
    notice = fresh.notice("safety-gate", "SR/00417/26")
    model = fresh.model("icecat", "EX-32U8")
    accepted = fresh.accept(notice, model)
    assert accepted["attached"] and accepted["review_state"] == "accepted"
    rejected = fresh.store.review_match(
        NS,
        accepted["match_id"],
        "rejected",
        "adapter only, not the monitor",
        scopes=h.REVIEW,
        principal_id="second-reviewer",
    )
    assert not rejected["attached"]
    assert [r["decision"] for r in rejected["review_history"]] == [
        "accepted",
        "rejected",
    ]
    assert rejected["review_history"][0]["revision_id"] == rejected["revision_id"]
    with pytest.raises(ProductSafetyError) as caught:
        fresh.store.review_match(
            NS, accepted["match_id"], "accepted", "ok", scopes=h.WRITE, principal_id="x"
        )
    assert caught.value.code == "unauthorized"
    with pytest.raises(ProductSafetyError):
        fresh.store.review_match(
            NS, accepted["match_id"], "maybe", "ok", scopes=h.REVIEW, principal_id="x"
        )


def test_proposals_are_idempotent_and_never_auto_accepted(fresh):
    before = fresh.conn.execute(
        "SELECT count(*), sum(updated_at_ms) FROM product_safety_matches"
    ).fetchone()
    again = fresh.store.propose_matches(NS, scopes=h.WRITE, principal_id="matcher")
    assert (
        fresh.conn.execute(
            "SELECT count(*), sum(updated_at_ms) FROM product_safety_matches"
        ).fetchone()
        == before
    )
    assert all(
        c["review_state"] == "unreviewed" and not c["attached"]
        for c in again["candidates"]
    )


def test_unmatched_identifications_stay_source_strings(env):
    # CPSC 26140 names model EX-32U8 without a brand or UPC: no candidate, but queryable by string.
    notice = env.notice("cpsc", "26140")
    assert env.store.matches_for_notice(NS, notice) == []
    found = env.store.lookup(NS, scopes=h.READ, designation="EX-32U8")
    assert {n["notice_number"] for n in found["notices"]} == {"SR/00417/26", "26140"}
    assert all(
        c["kind"] == "identification-string"
        for n in found["notices"]
        for c in n["connections"]
    )


def test_a_later_revision_that_stops_naming_the_model_detaches_the_match(fresh):
    notice = fresh.notice("safety-gate", "SR/00417/26")
    accepted = fresh.accept(notice, fresh.model("icecat", "EX-32U8"))
    native = h.pages("safety-gate-alerts")
    page = h.alert_page(native, "SR/00417/26")
    page["body"]["alert"].update({"lastUpdateDate": "2026-04-10"})
    page["body"]["alert"]["product"]["typeNumberOfModel"] = (
        "EX-3208"  # a corrected, unrelated designation
    )
    fresh.run_notices(
        "renamed",
        adapters={"safety-gate-alerts": fresh.compiled("safety-gate-alerts", native)},
        source_ids=["safety-gate-alerts"],
    )
    fresh.store.propose_matches(NS, scopes=h.WRITE, principal_id="matcher")
    after = fresh.store.match(NS, accepted["match_id"])
    assert after["candidate_state"] == "not_named_in_current_revision"
    assert (
        after["review_state"] == "accepted"
        and not after["attached"]
        and after["needs_re_review"]
    )


def test_manufacturer_names_link_only_through_entity_identity_decisions(fresh):
    from src.kb.entities import normalize_surface

    fresh.conn.execute(
        "CREATE TABLE IF NOT EXISTS canonical_entities (canonical_id TEXT PRIMARY KEY, "
        "preferred_name TEXT, entity_type TEXT)"
    )
    fresh.conn.execute(
        "CREATE TABLE IF NOT EXISTS entity_aliases (surface_form TEXT, canonical_id TEXT, "
        "method TEXT, score DOUBLE)"
    )
    fresh.conn.execute(
        "INSERT INTO canonical_entities VALUES ('ent:brightway', 'Brightway Home Ltd', 'ORG')"
    )
    fresh.conn.execute(
        "INSERT INTO entity_aliases VALUES (?, 'ent:brightway', 'exact', 1.0)",
        [normalize_surface("Brightway Home Ltd.")],
    )
    proposals = fresh.store.propose_party_links(NS, scopes=h.READ)
    party = next(p for p in proposals["parties"] if p["name"] == "Brightway Home Ltd.")
    assert (
        party["link"] is None and party["candidates"][0]["entity_id"] == "ent:brightway"
    )
    unlinked = next(
        p for p in proposals["parties"] if p["name"] == "Exampla Displays Inc."
    )
    assert unlinked["candidates"] == [] and unlinked["link"] is None
    with pytest.raises(ProductSafetyError) as caught:
        fresh.store.decide_party_link(
            NS,
            "Brightway Home Ltd.",
            "ent:brightway",
            "match",
            "same company",
            scopes=h.REVIEW,
            principal_id="reviewer",
        )
    assert caught.value.code == "unauthorized"
    decided = fresh.store.decide_party_link(
        NS,
        "Brightway Home Ltd.",
        "ent:brightway",
        "match",
        "same company",
        scopes=h.ALL,
        principal_id="reviewer",
    )
    assert decided["merged"] is False and decided["decision_id"].startswith(
        "entity-decision:"
    )
    view = fresh.store.inspect(NS, "cpsc:26117", scopes=h.READ)
    manufacturer = next(
        p for p in view["revision"]["parties"] if p["role"] == "manufacturer"
    )
    assert manufacturer["entity"]["entity_id"] == "ent:brightway"
    reverted = fresh.store.revert_party_link(
        NS, decided["link_id"], scopes=h.ALL, principal_id="reviewer"
    )
    assert reverted["status"] == "reverted"
    view = fresh.store.inspect(NS, "cpsc:26117", scopes=h.READ)
    assert (
        next(p for p in view["revision"]["parties"] if p["role"] == "manufacturer")[
            "entity"
        ]["status"]
        == "unmatched"
    )


# ------------------------------------------------------------------ R07 citations


def test_citations_resolve_only_by_exact_reference_or_identifier(env):
    rows = env.conn.execute(
        "SELECT c.raw, c.kind, l.target_kind, l.target_id, l.basis FROM product_safety_citations c "
        "LEFT JOIN product_safety_citation_links l ON l.citation_id=c.citation_id ORDER BY c.raw"
    ).fetchall()
    resolved = {r[0]: (r[2], r[3], r[4]) for r in rows if r[2]}
    unresolved = {r[0] for r in rows if not r[2]}
    assert resolved["ISO 6579-1:2017"] == ("standard", "standard:iso:56712", "cited")
    assert (
        resolved["Regulation (EU) 2023/988"][0] == "legal-work"
        and resolved["Regulation (EU) 2023/988"][2] == "cited"
    )
    # Not acquired (or not a catalogue reference): kept as the source string.
    assert {
        "EN 62368-1:2014+A11:2017",
        "EN 60335-2-15",
        "Directive 2014/35/EU",
    } <= unresolved
    work = env.conn.execute(
        "SELECT identifiers_json FROM legal_works WHERE work_id=?",
        [resolved["Regulation (EU) 2023/988"][1]],
    ).fetchone()[0]
    assert '"celex":"32023R0988"' in work


def test_linking_is_idempotent_and_needs_the_consumed_stores_read_scopes(fresh):
    again = fresh.store.link_citations(NS, scopes=h.ALL, principal_id="linker")
    assert again["linked"] == []
    with pytest.raises(ProductSafetyError) as caught:
        fresh.store.link_citations(
            NS, scopes=h.WRITE | {"knowledge:legal:read"}, principal_id="linker"
        )
    assert caught.value.code == "unauthorized"


def test_a_citation_dropped_by_a_later_revision_keeps_its_link_on_the_citing_revision(
    fresh,
):
    notice = fresh.notice("safety-gate", "SR/00417/26")
    first = fresh.store.revisions(NS, notice)[0]["revision_id"]
    native = h.pages("safety-gate-alerts")
    page = h.alert_page(native, "SR/00417/26")
    page["body"]["alert"].update({"lastUpdateDate": "2026-04-02"})
    page["body"]["alert"]["risk"]["compliance"] = (
        "The product does not comply with EN 62368-1:2014+A11:2017."
    )
    fresh.run_notices(
        "dropped",
        adapters={"safety-gate-alerts": fresh.compiled("safety-gate-alerts", native)},
        source_ids=["safety-gate-alerts"],
    )
    fresh.store.link_citations(NS, scopes=h.ALL, principal_id="linker")
    view = fresh.store.inspect(NS, notice, scopes=h.READ)
    assert view["revision"]["revision_no"] == 2
    assert not [c for c in view["revision"]["citations"] if c["kind"] == "legal"]
    old = next(r for r in view["revision_history"] if r["revision_id"] == first)
    gpsr = next(
        c
        for c in old["superseded_parts"]["citations"]
        if c["raw"] == "Regulation (EU) 2023/988"
    )
    assert gpsr["links"] and gpsr["links"][0]["basis"] == "cited"


def test_no_link_comes_from_topic_category_or_certificates(fresh):
    # The kettle and vehicle notices cite nothing: no category or hazard similarity links them to acts or standards.
    for notice in (fresh.notice("cpsc", "26117"), fresh.notice("nhtsa", "26V104000")):
        view = fresh.store.inspect(NS, notice, scopes=h.READ)
        assert all(not c["links"] for c in view["revision"]["citations"])
    # A certificate linked to a model is not notice evidence: the model still has no notice on record.
    model = fresh.model("icecat", "EX-34Q4")
    fresh.conn.execute(
        "INSERT INTO certificate_product_links VALUES ('cert-link-1', ?, 'CB-1', 'Examplecert', ?, "
        "'accepted', 'identifier', 'reviewer')",
        [NS, model],
    )
    answer = fresh.store.lookup(NS, scopes=h.READ, model_id=model)
    assert answer["status"] == "no notice on record" and answer["notices"] == []


def test_citation_extraction_reads_official_numbers_and_shares_one_boundary():
    found = extract_citations(
        "Breaches Regulation (EC) No 178/2002, Directive 2001/95/EC, Directive 85/374/EEC and Regulation (EU) "
        "2023/988 (http://data.europa.eu/eli/reg/2023/988/oj; CELEX 32023R0988); tested to EN ISO 8124-1:2018.",
        "/x",
    )
    celex = sorted(
        {c["identifiers"].get("celex") for c in found if c["kind"] == "legal"} - {None}
    )
    assert celex == ["31985L0374", "32001L0095", "32002R0178", "32023R0988"]
    assert any(
        c["identifiers"].get("eli") == "http://data.europa.eu/eli/reg/2023/988/oj"
        for c in found
    )
    assert [c["raw"] for c in found if c["kind"] == "standard"] == [
        "EN ISO 8124-1:2018"
    ]
    assert standard_key("EN 62368-1 : 2014 + A11:2017") == standard_key(
        "en 62368-1:2014+A11:2017"
    )
    # Path separators delimit identifiers in both the Python and the SQL form of the boundary.
    python = token_regex(re.escape("SR/00417/26"))
    sql = re.compile(token_sql_pattern("SR/00417/26"))
    for text, expected in (
        ("see alerts/SR/00417/26/detail", True),
        ("(SR/00417/26)", True),
        ("SR/00417/261", False),
        ("XSR/00417/26", False),
    ):
        assert (
            bool(python.search(text)) is expected and bool(sql.search(text)) is expected
        ), text


def test_notice_level_upcs_of_a_multi_product_recall_never_contradict_one_product(
    fresh,
):
    import copy
    import json

    from src.ingestion.product_sources import parse_cpsc_recall

    recall = copy.deepcopy(
        json.loads(h.FIXTURES["cpsc-recalls"].read_text())["native_pages"][1]["body"][0]
    )
    recall.update(
        {
            "RecallNumber": "26151",
            "LastPublishDate": "2026-05-01T00:00:00",
            "ProductUPCs": [{"UPC": "4012345000016"}, {"UPC": "N/A"}],
        }
    )
    recall["Products"] = [
        {"Name": "27-inch monitor", "Model": "EX-27Q4"},
        {"Name": "Wall mount", "Model": "EX-WM1"},
    ]
    statement = parse_cpsc_recall(recall)
    # A missing marker is absent, never an identifier.
    assert [
        i["value"] for i in statement["identifications"] if i["kind"] == "gtin"
    ] == ["4012345000016"]
    fresh.store.apply(NS, statement)
    fresh.store.propose_matches(NS, scopes=h.WRITE, principal_id="matcher")
    matches = fresh.store.matches_for_notice(NS, fresh.notice("cpsc", "26151"))
    assert {(m["group"], m["basis"], m["candidate_state"]) for m in matches} == {
        (-1, "gtin", "proposed")
    }
    assert {m["model_id"] for m in matches} == {fresh.model("icecat", "EX-27Q4")}
