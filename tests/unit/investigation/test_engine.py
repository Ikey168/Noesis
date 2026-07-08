"""Tests for the investigation engine loop (open -> plan -> pursue ->
matrix -> conclude), including the evidence-discipline gate and the
claim-alignment flip."""

from src.analytics.honesty import validate_analytic_output
from src.investigation import (
    advance_case,
    case_file,
    conclude_case,
    hypothesis_matrix,
    open_case,
    plan_leads,
    pursue_lead,
    pursue_open_leads,
    run_case,
)
from src.investigation import store

QUESTION = "Severe flooding struck the delta region"


# --------------------------------------------------------------------------- #
# Open
# --------------------------------------------------------------------------- #

def test_open_seeds_affirmative_and_null(conn):
    out = open_case(conn, QUESTION, topic="flooding", entities=["A", "B"])
    kinds = {h["hypothesis_id"]: h["kind"] for h in out["hypotheses"]}
    assert kinds == {"h1": "affirmative", "h0": "null"}
    assert out["case"]["status"] == store.STATUS_OPEN
    assert out["case"]["entities"] == ["A", "B"]
    # The journal starts with the opening.
    assert out["journal"][0]["event"] == "case_opened"


def test_open_with_custom_hypotheses_keeps_them_and_adds_null_when_lonely(conn):
    out = open_case(conn, "who benefited?", hypotheses=["insiders benefited"])
    kinds = [h["kind"] for h in out["hypotheses"]]
    assert kinds.count("custom") == 1 and kinds.count("null") == 1
    two = open_case(conn, "who benefited most?",
                    hypotheses=["insiders benefited", "outsiders benefited"])
    assert [h["kind"] for h in two["hypotheses"]] == ["custom", "custom"]


def test_open_requires_a_question(conn):
    assert open_case(conn, "  ")["code"] == "empty_question"


def test_case_ids_never_collide(conn):
    a = open_case(conn, QUESTION)["case"]["case_id"]
    b = open_case(conn, QUESTION)["case"]["case_id"]
    assert a != b and b.startswith(a)


# --------------------------------------------------------------------------- #
# Plan
# --------------------------------------------------------------------------- #

def test_plan_creates_claim_topic_and_entity_leads(corpus):
    conn = corpus.conn
    out = open_case(conn, QUESTION, topic="flooding", entities=["A", "B"])
    case_id = out["case"]["case_id"]
    planned = plan_leads(conn, case_id)
    tools = sorted(p["tool"] for p in planned["planned"])
    assert tools == [
        "contradiction_scan", "corroborate", "corroborate", "entity_dossier",
        "entity_dossier", "relationship_path", "timeline_reconstruct",
    ]
    # Claim leads target the affirmative hypothesis.
    claim_leads = [p for p in planned["planned"] if p["tool"] == "corroborate"]
    assert all(p["hypothesis_id"] == "h1" for p in claim_leads)
    # Planning again adds nothing new (idempotent).
    assert plan_leads(conn, case_id)["count"] == 0
    assert store.get_case(conn, case_id)["status"] == store.STATUS_ACTIVE


def test_plan_labels_counterclaims_opposed(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    planned = plan_leads(conn, case_id)
    stances = {
        p["params"]["claim_id"]: p["params"]["stance"]
        for p in planned["planned"] if p["tool"] == "corroborate"
    }
    # k1 matches best and anchors the aligned side; k2 contradicts it.
    assert stances == {"k1": "aligned", "k2": "opposed"}


def test_plan_refuses_a_concluded_case(conn):
    case_id = open_case(conn, QUESTION)["case"]["case_id"]
    store.set_case_verdict(conn, case_id, "v", "h1", "2026-07-01")
    assert plan_leads(conn, case_id)["code"] == "concluded"


# --------------------------------------------------------------------------- #
# Pursue
# --------------------------------------------------------------------------- #

def test_pursue_harvests_cited_weighted_evidence_with_null_mirror(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    planned = plan_leads(conn, case_id)
    k1_lead = next(
        p for p in planned["planned"]
        if p["tool"] == "corroborate" and p["params"]["claim_id"] == "k1"
    )
    out = pursue_lead(conn, case_id, k1_lead["lead_id"])
    assert out["ok"] is True
    rows = store.list_evidence(conn, case_id, hypothesis_id="h1")
    supports = [r for r in rows if r["relation"] == "supports"]
    assert {r["source"] for r in supports} == {"Beta Journal", "Gamma Review"}
    assert all(r["cited"] for r in supports)
    assert all(r["credibility"] is not None for r in supports)
    # Every h1 row is mirrored into the null with the relation flipped.
    mirrored = store.list_evidence(conn, case_id, hypothesis_id="h0")
    assert {(r["source"], r["relation"]) for r in mirrored} == {
        ("Beta Journal", "contradicts"), ("Gamma Review", "contradicts"),
        ("Delta Post", "supports"),
    }


def test_pursue_flips_direction_for_opposed_claims(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    planned = plan_leads(conn, case_id)
    k2_lead = next(
        p for p in planned["planned"]
        if p["tool"] == "corroborate" and p["params"]["claim_id"] == "k2"
    )
    pursue_lead(conn, case_id, k2_lead["lead_id"])
    rows = store.list_evidence(conn, case_id, hypothesis_id="h1")
    # Alpha Wire contradicts the counter-claim k2 -> that SUPPORTS h1.
    alpha = next(r for r in rows if r["source"] == "Alpha Wire")
    assert alpha["relation"] == "supports"
    assert "counter-claim" in alpha["summary"]


def test_pursue_marks_broken_leads_failed(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION)["case"]["case_id"]
    now = "2026-07-01"
    lead_id = store.upsert_lead(
        conn, case_id, "corroborate", {"claim_id": "nope"}, "r", "h1", now
    )
    out = pursue_lead(conn, case_id, lead_id)
    assert out["ok"] is False
    assert store.get_lead(conn, case_id, lead_id)["status"] == store.LEAD_FAILED
    # A pursued/failed lead cannot be pursued twice.
    assert pursue_lead(conn, case_id, lead_id)["code"] == "not_open"


def test_single_sourced_claims_are_journalled_as_gaps(corpus):
    conn = corpus.conn
    corpus.claims([("lonely", "Flooding delta region uncorroborated angle.", "d1", "news", 0.5, None)])
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    plan_leads(conn, case_id)
    pursue_open_leads(conn, case_id)
    gaps = [e for e in store.list_events(conn, case_id) if e["event"] == "gap_noted"]
    assert any("lonely" in g["detail"]["gap"] for g in gaps)


# --------------------------------------------------------------------------- #
# Matrix
# --------------------------------------------------------------------------- #

def test_matrix_counts_independent_sources_and_validates(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    plan_leads(conn, case_id)
    pursue_open_leads(conn, case_id)
    matrix = hypothesis_matrix(conn, case_id)
    assert validate_analytic_output(matrix) == []
    h1 = next(h for h in matrix["hypotheses"] if h["hypothesis_id"] == "h1")
    h0 = next(h for h in matrix["hypotheses"] if h["hypothesis_id"] == "h0")
    # Beta + Gamma support k1; Alpha contradicts the counter-claim; Delta contra.
    assert h1["independent_support_count"] == 3
    assert h1["independent_contradict_count"] == 1
    assert h0["single_sourced"] is True
    assert matrix["leader"] == "h1"
    assert matrix["margin"] > 0
    assert matrix["support_credibility"] is not None
    assert set(h1["diagnostic_sources"]) == {"Alpha Wire", "Beta Journal", "Gamma Review"}


def test_matrix_never_ships_a_bare_score(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    matrix = hypothesis_matrix(conn, case_id)
    assert "confidence" not in matrix
    assert {"n", "method", "assumptions"} <= set(matrix)


# --------------------------------------------------------------------------- #
# Conclude
# --------------------------------------------------------------------------- #

def test_conclude_withholds_and_names_gaps_when_underdetermined(conn):
    # Empty corpus: nothing corroborates anything.
    case_id = open_case(conn, QUESTION)["case"]["case_id"]
    out = conclude_case(conn, case_id)
    assert out["concluded"] is False
    assert any("independent supporting source" in g for g in out["gaps"])
    assert store.get_case(conn, case_id)["status"] != store.STATUS_CONCLUDED
    journal = [e["event"] for e in store.list_events(conn, case_id)]
    assert "conclusion_withheld" in journal


def test_conclude_refuses_while_leads_are_open(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    plan_leads(conn, case_id)
    out = conclude_case(conn, case_id)
    assert out["concluded"] is False
    assert any("not yet pursued" in g for g in out["gaps"])


def test_conclude_passes_the_gate_on_a_corroborated_case(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    plan_leads(conn, case_id)
    pursue_open_leads(conn, case_id)
    plan_leads(conn, case_id)          # vetting round for discovered sources
    pursue_open_leads(conn, case_id)
    out = conclude_case(conn, case_id)
    assert out["concluded"] is True
    assert out["hypothesis"] == "h1"
    case = store.get_case(conn, case_id)
    assert case["status"] == store.STATUS_CONCLUDED
    assert QUESTION in case["verdict"]
    hyps = {h["hypothesis_id"]: h["status"] for h in store.list_hypotheses(conn, case_id)}
    assert hyps == {"h1": "supported", "h0": "unsupported"}
    # Concluding again is a no-op that reports the standing verdict.
    again = conclude_case(conn, case_id)
    assert again["concluded"] is True and again.get("already") is True


# --------------------------------------------------------------------------- #
# The drive loop
# --------------------------------------------------------------------------- #

def test_run_case_drives_to_a_disciplined_verdict(corpus):
    conn = corpus.conn
    out = run_case(conn, QUESTION, topic="flooding")
    assert out["conclusion"]["concluded"] is True
    assert out["case"]["verdict"].startswith("supported by the record")
    assert out["rounds"] >= 2  # corroboration round + source-vetting round
    # Sources introduced by evidence were vetted (the engine investigates its
    # own witnesses).
    vetted = [l for l in out["file"]["leads"] if l["tool"] == "source_reliability"]
    assert {l["params"]["source"] for l in vetted} >= {"Beta Journal", "Gamma Review"}
    assert out["file"]["open_lead_count"] == 0
    assert out["file"]["reconstructable"] is True
    events = [e["event"] for e in out["file"]["journal"]]
    assert events[0] == "case_opened" and events[-1] == "case_concluded"


def test_run_case_on_an_empty_corpus_stays_open(conn):
    out = run_case(conn, QUESTION)
    assert out["conclusion"]["concluded"] is False
    assert out["conclusion"]["gaps"]
    assert out["case"]["status"] != store.STATUS_CONCLUDED


def test_advance_case_is_one_round(corpus):
    conn = corpus.conn
    case_id = open_case(conn, QUESTION, topic="flooding")["case"]["case_id"]
    out = advance_case(conn, case_id)
    assert out["planned"] > 0
    assert out["pursued"] == out["planned"]
    assert validate_analytic_output(out["matrix"]) == []


def test_case_file_missing_case(conn):
    store.ensure_schema(conn)
    assert case_file(conn, "case-nope")["code"] == "not_found"
