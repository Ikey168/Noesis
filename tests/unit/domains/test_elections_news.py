"""News frames and sentiment beside contests as dated evidence, never as a cause (#1983)."""

from __future__ import annotations

import json

import pytest

from src.database.local_warehouse_seed import _SCHEMA as WAREHOUSE
from src.ingestion.document_store import _SCHEMA as DOCUMENTS
from src.ingestion.election_sources import day_ms
from src.kb.elections import ElectionError, forbidden_keys
from src.kb.elections_identity import ElectionIdentity
from src.kb.elections_news import ElectionNews
from src.kb.elections_polls import ElectionPolls
from tests.unit import elections_harness as h

SCOPES = h.REVIEW_SCOPES | {"knowledge:read"}


def article(conn, document_id, day, title, content, outlet="Example Daily"):
    conn.execute(
        "INSERT INTO documents (document_id, source_type, language, ingested_at, created_at, source_id, url, "
        "content_hash, title, content, metadata) VALUES (?, 'news', 'de', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            document_id,
            day_ms(day),
            day_ms(day),
            outlet,
            f"https://news.example.org/{document_id}",
            f"sha-{document_id}",
            title,
            content,
            json.dumps({"outlet": outlet}),
        ],
    )


@pytest.fixture()
def env():
    conn = h.connection()
    conn.execute(DOCUMENTS)
    conn.execute(WAREHOUSE)
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, url VARCHAR, sentiment_label VARCHAR, sentiment_score DOUBLE)"
    )
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    h.apply(conn, "de-btw", h.DE_FINAL)
    ElectionPolls(conn).import_release(
        "global",
        publisher="Beispiel Institut",
        election_id=h.DE_ELECTION,
        source_url="https://www.beispiel-institut.example/sonntagsfrage.csv",
        csv_text=(h.FIXTURES / "poll_beispiel_institut_2099-02-21.csv").read_text(),
        redistribution="allowed",
        principal_id="alice",
        scopes=SCOPES,
    )
    article(
        conn,
        "doc-mention",
        "2099-02-20",
        "Beispielpartei presents its programme",
        "Interview.",
    )
    conn.execute(
        "INSERT INTO document_actors VALUES ('doc-mention', 'news', 'Beispielpartei', NULL, 'speaker', 0.9, NULL)"
    )
    conn.execute(
        "INSERT INTO document_frames VALUES ('doc-mention', 'news', 'economic', 0.8, '2099-02-21')"
    )
    conn.execute(
        "INSERT INTO news_articles VALUES ('doc-mention', NULL, 'positive', 0.4)"
    )
    article(
        conn,
        "doc-keyword",
        "2099-02-25",
        "Weather report",
        "Sunny weather near the Musterunion offices.",
    )
    article(conn, "doc-other", "2099-02-26", "Sports", "No party named here.")
    article(conn, "doc-late", "2099-09-01", "Beispielpartei anniversary", "Much later.")
    conn.execute(
        "INSERT INTO document_actors VALUES ('doc-late', 'news', 'Beispielpartei', NULL, 'subject', 0.9, NULL)"
    )
    news = ElectionNews(conn, now=h.Clock())
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "second-vote")
    yield conn, news, contest
    conn.close()


def test_explicit_mentions_link_and_shared_words_are_only_candidates(env):
    _, news, contest = env
    refreshed = news.refresh_links(
        "global", contest, principal_id="alice", scopes=SCOPES
    )
    assert refreshed["window"] == {"from": "2098-12-31", "to": "2099-03-31"}
    kinds = {(item["document_id"], item["link_kind"]) for item in refreshed["links"]}
    assert kinds == {
        ("doc-mention", "explicit-mention"),
        ("doc-keyword", "keyword-candidate"),
    }
    again = news.refresh_links("global", contest, principal_id="alice", scopes=SCOPES)
    assert (
        again["linked"] == again["candidate"] == 0 and again["unchanged"] == 2
    )  # idempotent
    link = next(
        item for item in refreshed["links"] if item["document_id"] == "doc-mention"
    )
    assert link["article"]["source_revision"]["content_hash"] == "sha-doc-mention"
    assert link["evidence"]["mentions"][0]["actor_name"] == "Beispielpartei"


def test_accepted_identity_entities_count_as_explicit_mentions(env):
    conn, news, contest = env
    from src.domains.political.model import record_alias, record_object

    record_object(
        conn,
        object_id="jurisdiction:de",
        object_type="jurisdiction",
        canonical_name="Germany",
    )
    record_object(
        conn,
        object_id="party:muster",
        object_type="party",
        canonical_name="Musterunion",
        jurisdiction_id="jurisdiction:de",
    )
    record_alias(conn, object_id="party:muster", alias="Musterunion")
    identity = ElectionIdentity(conn, now=h.Clock())
    identity.propose(
        "global",
        principal_id="alice",
        scopes=SCOPES,
        political_jurisdictions={"DE": "jurisdiction:de"},
    )
    pair = next(
        c
        for c in identity.candidates("global", scopes=SCOPES)
        if "political:party:muster" in c["records"]
        and f"elections:{h.DE_ELECTION}:party:musterunion" in c["records"]
    )
    identity.service.review(
        "global",
        pair["candidate_id"],
        "accept",
        "register entry",
        principal_id="bob",
        scopes=SCOPES,
    )
    article(conn, "doc-entity", "2099-02-27", "Union leader speaks", "An interview.")
    conn.execute(
        "INSERT INTO document_actors VALUES ('doc-entity', 'news', 'the union leader', 'party:muster', "
        "'speaker', 0.8, NULL)"
    )
    links = news.refresh_links("global", contest, principal_id="alice", scopes=SCOPES)[
        "links"
    ]
    assert ("doc-entity", "explicit-mention") in {
        (item["document_id"], item["link_kind"]) for item in links
    }


def test_evidence_sits_beside_results_and_polls_and_carries_no_causal_field(env):
    _, news, contest = env
    news.refresh_links("global", contest, principal_id="alice", scopes=SCOPES)
    answer = news.evidence("global", contest, scopes=SCOPES)
    assert [v["kind"] for v in answer["result_vintages"]] == [
        "preliminary",
        "certified",
    ]
    assert {r["typed_as"] for r in answer["poll_readings"]} == {"poll"} and answer[
        "poll_readings"
    ]
    mention = next(
        n
        for n in answer["news_evidence"]
        if n["article"]["document_id"] == "doc-mention"
    )
    assert (
        mention["article"]["date"] == "2099-02-20"
        and mention["article"]["outlet"] == "Example Daily"
    )
    assert mention["frames"] == [
        {"frame": "economic", "score": 0.8, "classified_at": "2099-02-21"}
    ]
    assert mention["sentiment"] == {"label": "positive", "score": 0.4}
    assert "no correlation or causal effect" in answer["separation"]
    assert forbidden_keys(answer) == []
    assert not any(
        k in json.dumps(answer)
        for k in ('"correlation"', '"causation"', '"effect"', '"influence"')
    )
    strict = news.evidence("global", contest, scopes=SCOPES, include_candidates=False)
    assert {n["article"]["document_id"] for n in strict["news_evidence"]} == {
        "doc-mention"
    }


def test_reviewed_assertions_and_reverts_are_new_revisions(env):
    _, news, contest = env
    news.refresh_links("global", contest, principal_id="alice", scopes=SCOPES)
    asserted = news.assert_link(
        "global",
        contest,
        "doc-other",
        target_key=contest,
        reason="covers the local race",
        principal_id="bob",
        scopes=SCOPES,
    )
    assert (
        asserted["link_kind"] == "reviewed-assertion" and asserted["revision_no"] == 1
    )
    reverted = news.revert(
        "global",
        asserted["link_id"],
        "wrong constituency",
        principal_id="bob",
        scopes=SCOPES,
    )
    assert reverted["state"] == "reverted" and reverted["revision_no"] == 2
    keyword = next(
        item
        for item in news.links("global", contest, scopes=SCOPES)
        if item["document_id"] == "doc-keyword"
    )
    news.revert(
        "global",
        keyword["link_id"],
        "not about the party",
        principal_id="bob",
        scopes=SCOPES,
    )
    news.refresh_links(
        "global", contest, principal_id="alice", scopes=SCOPES
    )  # never overrides a reviewer
    states = {
        item["document_id"]: item["state"]
        for item in news.links("global", contest, scopes=SCOPES)
    }
    assert states == {
        "doc-mention": "linked",
        "doc-keyword": "reverted",
        "doc-other": "reverted",
    }
    with pytest.raises(ElectionError):
        news.assert_link(
            "global",
            contest,
            "doc-other",
            target_key="elections:x",
            reason="r",
            principal_id="b",
            scopes=SCOPES,
        )
    with pytest.raises(ElectionError) as exc:
        news.evidence("global", contest, scopes=h.READ_ONLY)
    assert exc.value.code == "unauthorized"
