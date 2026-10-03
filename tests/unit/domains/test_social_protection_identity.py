"""SS06 (#2775): places and functions across social protection sources through reviewable identity."""

from __future__ import annotations

import pytest

from src.kb.social_protection_identity import SocialProtectionIdentity
from src.kb.social_protection_records import SocialProtectionError
from src.kb.social_protection_store import SocialProtectionStore
from tests.unit import social_protection_harness as h

OLD_ESSPROS = {"scheme": "esspros-spfunc", "code": "OLD"}
OLD_SOCX = {"scheme": "socx-branch", "code": "TP11"}
OLD_ILO = {"scheme": "ilo-contingency", "code": "SOC_CONTIG_OLD"}
CITED = [{"kind": "methodology", "url": "https://www.oecd.org/en/data/datasets/social-expenditure-database-socx.html",
          "note": "published ESSPROS-SOCX reconciliation (verify)"}]


def test_places_match_by_published_iso_codes_with_review_and_unmatched_stay_visible():
    conn = h.connection()
    h.load_all(conn)
    h.register_places(conn, keys=("de",))
    identity = SocialProtectionIdentity(conn)
    proposal = identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES)
    assert {(a["subject"]["scheme"], a["subject"]["code"]) for a in proposal["proposed"]} == {
        ("eurostat-geo", "DE"), ("iso3166-1-alpha3", "DEU")}
    methods = {a["subject"]["code"]: (a["method"], a["confidence"]) for a in proposal["proposed"]}
    assert methods == {"DE": ("iso-alpha2-equivalent", "high"), "DEU": ("published-code", "high")}
    assert {(u["scheme"], u["code"]) for u in proposal["unmatched"]} == {("eurostat-geo", "FR"),
                                                                         ("iso3166-1-alpha3", "FRA")}
    assert all(a["evidence"]["names_used"] is False for a in proposal["proposed"])
    # Nothing is auto-merged: no place is used until another principal accepts.
    assert identity.place_for_area(h.NS, "eurostat-geo", "DE") is None
    first = proposal["proposed"][0]
    with pytest.raises(SocialProtectionError) as caught:
        identity.review(h.NS, first["assertion_id"], "accept", "self", principal_id="proposer", scopes=h.SCOPES)
    assert caught.value.code == "self_review"
    for assertion in proposal["proposed"]:
        accepted = identity.review(h.NS, assertion["assertion_id"], "accept", "published ISO code",
                                   principal_id="reviewer", scopes=h.SCOPES)
        assert accepted["state"] == "accepted" and accepted["decision_id"]
    place = identity.place_for_area(h.NS, "eurostat-geo", "DE")["place_id"]
    assert {a["code"] for a in identity.areas_for_place(h.NS, place)} == {"DE", "DEU"}
    unmatched = identity.unmatched(h.NS, scopes=h.READ_ONLY)
    assert {u["code"] for u in unmatched} == {"FR", "FRA"}
    reverted = identity.revert(h.NS, first["assertion_id"], "re-check", principal_id="reviewer", scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and [s["state"] for s in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    assert len(identity.unmatched(h.NS, scopes=h.READ_ONLY)) == 3


def test_functions_are_related_by_review_never_reclassified_and_cofog_stays_distinct():
    conn = h.connection()
    h.load_all(conn)
    identity = SocialProtectionIdentity(conn)
    functions = {(f["publisher"], f["scheme"], f["code"]) for f in identity.functions(h.NS)}
    assert ("eurostat-esspros", "esspros-spfunc", "OLD") in functions
    assert ("ilo-social-protection-coverage", "ilo-contingency", "SOC_CONTIG_OLD") in functions
    by_label = identity.propose_function_relations(h.NS, principal_id="proposer", scopes=h.SCOPES)
    (label_match,) = by_label["proposed"]  # ESSPROS "Old age" and SOCX "Old age" state the same label
    assert label_match["method"] == "same-published-label" and label_match["confidence"] == "low"
    assert label_match["evidence"]["merge"] is False and label_match["evidence"]["reclassify"] is False
    stated = identity.propose_function_relation(h.NS, OLD_ESSPROS, OLD_ILO, cited=CITED,
                                                statement="ILO old-age coverage relates to ESSPROS old-age "
                                                          "expenditure as a different measure of the same "
                                                          "contingency", principal_id="proposer", scopes=h.SCOPES)
    assert stated["state"] == "proposed" and stated["method"] == "reviewer-stated"
    with pytest.raises(SocialProtectionError) as cofog:
        identity.propose_function_relation(h.NS, OLD_ESSPROS, {"scheme": "cofog", "code": "GF10"}, cited=CITED,
                                           statement="same", principal_id="proposer", scopes=h.SCOPES)
    assert cofog.value.code == "distinct_concept"
    with pytest.raises(SocialProtectionError) as same_publisher:
        identity.propose_function_relation(h.NS, OLD_ESSPROS, {"scheme": "esspros-spfunc", "code": "SICK"},
                                           cited=CITED, statement="x", principal_id="proposer", scopes=h.SCOPES)
    assert same_publisher.value.code == "invalid_relation"
    assert identity.related_functions(h.NS, "esspros-spfunc", "OLD") == []
    identity.review(h.NS, label_match["assertion_id"], "accept", "published reconciliation", principal_id="reviewer",
                    scopes=h.SCOPES)
    identity.review(h.NS, stated["assertion_id"], "reject", "coverage is not expenditure", principal_id="reviewer",
                    scopes=h.SCOPES)
    related = identity.related_functions(h.NS, "esspros-spfunc", "OLD")
    assert [(r["scheme"], r["code"], r["relation"]) for r in related] == [("socx-branch", "TP11", "related")]
    assert identity.related_functions(h.NS, "ilo-contingency", "SOC_CONTIG_OLD") == []
    # Every series keeps its own function: nothing was re-classified.
    store = SocialProtectionStore(conn)
    assert {s["function"]["scheme"] for s in store.find_series(h.NS, provider="oecd-socx")} == {"socx-branch"}
    unrelated = {(f["scheme"], f["code"]) for f in identity.unrelated_functions(h.NS)}
    assert OLD_SOCX["code"] not in {c for _, c in unrelated} and ("ilo-contingency", "SOC_CONTIG_OLD") in unrelated
