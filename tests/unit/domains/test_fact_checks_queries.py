"""Fact-checks of a claim or claimant as of a date and fact-checks citing a news article (#2698, #2703, FC08-FC09)."""

from __future__ import annotations

import pytest

from src.kb.citation_preservation import CAPTURE_SCOPE, CitationPreservationStore
from src.kb.fact_checks_queries import FactCheckQueries
from src.kb.fact_checks_records import FactCheckError, forbidden_keys
from tests.unit import fact_checks_harness as h


def ratings(answer):
    return {(r["publisher_site"], r["source_id"]): r["textual_rating"]
            for group in answer["ratings_side_by_side"] for r in group["ratings_as_published"]}


def test_claim_answers_use_accepted_matches_only_and_show_conflicting_ratings_side_by_side():
    conn = h.accepted_world()
    ask = FactCheckQueries(conn)
    answer = ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-seals", as_of="2025-12-31")
    assert answer["status"] == "answered" and answer["matching"].startswith("accepted claim matches only")
    assert len(answer["fact_checks"]) == 2
    (group,) = answer["ratings_side_by_side"]
    assert group["distinct_textual_ratings"] == ["False", "Misleading"]
    assert group["publishers"] == ["factcheck.example.org", "verifica.example.net"]
    datacommons = next(r for r in group["ratings_as_published"] if r["publisher_site"] == "verifica.example.net"
                       and r["source_id"] == h.DATACOMMONS)
    assert (datacommons["rating_value"], datacommons["worst_rating"], datacommons["best_rating"]) == (2, 1, 7)
    assert "not a reviewed claim match" in group["grouping_basis"]
    for item in answer["fact_checks"]:
        assert all(a["citation"]["revision_id"] for a in item["source_assertions"])
        assert item["basis"]["kind"] == "accepted-match"
    assert forbidden_keys(answer) == []
    # the lexical candidate for claim-array is never answered
    lexical = ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-array")
    assert lexical["status"] == "none_on_record" and lexical["unreviewed_candidates"][0]["method"] == \
        "lexical-overlap"


def test_as_of_answers_follow_each_publishers_revisions_and_status_at_review_time():
    conn = h.accepted_world(version="v2")
    ask = FactCheckQueries(conn)
    early = ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-seals", as_of="2025-04-01")
    late = ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-seals", as_of="2025-12-31")
    assert ratings(early)[("factcheck.example.org", h.GOOGLE)] == "False"
    assert ratings(late)[("factcheck.example.org", h.GOOGLE)] == "Mostly false"
    assert ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-seals", as_of="2025-03-01")["status"] == \
        "none_on_record"
    verifica = next(i for i in late["fact_checks"] if i["publisher"]["site"] == "verifica.example.net")
    (status,) = verifica["publisher_status_at_review"]["signatories"]
    assert status["status_as_published"] == "Under renewal"  # in effect on 2025-03-12, before the 2025-08-15 change
    assert status["citation"]["record_key"] == "fact-checks:ifcn:verifica-example"
    assert verifica["publisher"]["name_as_published"] == "Verifica Example"


def test_text_search_is_labelled_a_search_and_claimant_queries_need_the_claimant_scope():
    conn = h.accepted_world()
    ask = FactCheckQueries(conn)
    search = ask.for_claim(h.NS, scopes=h.SCOPES, text="killed 4,000 seals")
    assert search["status"] == "answered" and search["fact_checks"][0]["basis"]["kind"] == "text-search"
    with pytest.raises(FactCheckError) as refused:
        ask.for_claimant(h.NS, "Mayor Alex Example", scopes=h.SCOPES)
    assert refused.value.code == "unauthorized"
    named = ask.for_claimant(h.NS, "Mayor Alex Example", scopes=h.REVIEW_SCOPES)
    by_entity = ask.for_claimant(h.NS, "ent-alex-example", scopes=h.REVIEW_SCOPES)
    assert {i["record_key"] for i in named["fact_checks"]} == {i["record_key"] for i in by_entity["fact_checks"]}
    assert len(named["fact_checks"]) == 2
    nobody = ask.for_claimant(h.NS, "Nobody Example", scopes=h.REVIEW_SCOPES)
    assert nobody["status"] == "none_on_record" and nobody["fact_checks"] == []


def test_citing_answers_use_the_stated_url_rules_and_archived_captures():
    conn = h.accepted_world()
    CitationPreservationStore(conn).record_capture(h.NS, {
        "archive_id": "internet-archive", "archive_kind": "memento-archive", "resolver": "timetravel",
        "uri_r": h.APPEARANCE, "uri_m": "https://web.archive.org/web/20250301120000/" + h.APPEARANCE,
        "memento_datetime": "Sat, 01 Mar 2025 12:00:00 GMT", "status": 200, "mimetype": "text/html", "digests": [],
        "receipt": {"request_id": "req-1", "adapter": "test", "evidence_origin": "fixture"}},
        principal_id="curator", scopes={CAPTURE_SCOPE})
    ask = FactCheckQueries(conn)
    answer = ask.citing(h.NS, scopes=h.SCOPES, url="http://www." + h.APPEARANCE[len("https://"):] + "/?utm_source=x")
    assert answer["url_matching"]["version"] == "wa-canon-v1"
    (given,) = answer["url_matching"]["input"].values()
    assert set(given["rules_applied"]) >= {"scheme-equivalence", "strip-www", "drop-tracking-parameters"}
    assert len(answer["fact_checks"]) == 2
    roles = {a["cites_as"][0] for i in answer["fact_checks"] for a in i["source_assertions"]}
    assert roles == {"appearance"}
    assert {a["source_id"] for i in answer["fact_checks"] for a in i["source_assertions"]} == {h.DATACOMMONS}
    assert answer["archived_captures"]["status"] == "answered"
    assert answer["archived_captures"]["captures"][0]["uri_m"].startswith("https://web.archive.org/web/2025")
    by_document = ask.citing(h.NS, scopes=h.SCOPES, document_id="doc-seals")
    assert {i["record_key"] for i in by_document["fact_checks"]} == {i["record_key"] for i in answer["fact_checks"]}
    without_scope = ask.citing(h.NS, scopes=h.SCOPES - {"knowledge:citation:read"}, url=h.APPEARANCE)
    assert without_scope["archived_captures"]["status"] == "unavailable"
    nothing = ask.citing(h.NS, scopes=h.SCOPES, url="https://news.example.com/unrelated")
    assert nothing["status"] == "none_on_record"


def test_evidence_bundle_cites_every_fact_check_revision_and_quotes_ratings():
    conn = h.accepted_world()
    ask = FactCheckQueries(conn)
    answer = ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-seals", as_of="2025-12-31")
    bundle = ask.evidence_bundle(answer)
    revisions = {a["citation"]["revision_id"] for i in answer["fact_checks"] for a in i["source_assertions"]}
    assert {b["id"] for b in bundle["bibliography"]} == revisions
    assertions = bundle["sections"][0]["assertions"]
    assert all(a["citations"] and a["dependencies"][0]["revision"] in revisions for a in assertions)
    assert any("“Misleading” (2 on the publisher's scale 1-7)" in a["text"] for a in assertions)
    assert all("observed" in b["text"] and "source" in b["text"] for b in bundle["bibliography"])
    assert bundle["exclusions"][0] == "truth verdicts by Noesis"
