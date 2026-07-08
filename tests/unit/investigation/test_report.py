"""Tests for the cited case brief and its markdown rendering."""

from src.investigation import case_brief, render_markdown, run_case
from src.investigation import store
from src.investigation.engine import open_case, plan_leads, pursue_open_leads

QUESTION = "Severe flooding struck the delta region"


def test_brief_on_a_concluded_case_carries_the_verdict_and_citations(corpus):
    conn = corpus.conn
    out = run_case(conn, QUESTION, topic="flooding")
    brief = case_brief(conn, out["case"]["case_id"])
    assert brief["verdict"] and brief["verdict_hypothesis"] == "h1"
    # Key evidence is the leader's, mirrors excluded, every line sourced.
    assert brief["key_evidence"]
    assert all(not e["summary"].startswith("mirror:") for e in brief["key_evidence"])
    assert all(e["source"] for e in brief["key_evidence"])
    # The record's internal disagreement is surfaced, not hidden.
    assert brief["contradictions"]
    # The brief carries its own honesty line.
    assert brief["n"] > 0 and brief["method"] and brief["assumptions"]


def test_brief_on_an_open_case_names_what_keeps_it_open(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    plan_leads(conn, case_id)
    brief = case_brief(conn, case_id)
    assert brief["verdict"] is None
    assert any("not yet pursued" in g for g in brief["gaps"])


def test_brief_missing_case(conn):
    store.ensure_schema(conn)
    assert case_brief(conn, "case-nope")["code"] == "not_found"


def test_markdown_render_flags_uncited_lines(corpus):
    conn = corpus.conn
    out = run_case(conn, QUESTION, topic="flooding")
    case_id = out["case"]["case_id"]
    # Force one uncited contradiction row into the record.
    store.upsert_evidence(
        conn, case_id, None, store.RELATION_CONTEXT, "contradiction", "x|y",
        "Rumor Blog vs Rumor Blog 2", None, False,
        "the record disagrees: unresolvable rumor pair", None, "2026-07-01",
    )
    md = render_markdown(case_brief(conn, case_id))
    assert md.startswith("# Case brief:")
    assert "Verdict:" in md
    assert "[UNCITED]" in md
    assert "uncited row(s) flagged" in md


def test_markdown_render_of_an_open_case_lists_gaps(conn):
    out = run_case(conn, QUESTION)
    md = render_markdown(case_brief(conn, out["case"]["case_id"]))
    assert "no verdict yet" in md
    assert "What is keeping this open:" in md
