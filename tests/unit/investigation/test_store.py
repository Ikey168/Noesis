"""Tests for the case-file store."""

from src.investigation import store

NOW = "2026-07-01 12:00:00"


def test_schema_ready_flips_after_ensure(conn):
    assert store.schema_ready(conn) is False
    store.ensure_schema(conn)
    assert store.schema_ready(conn) is True
    store.ensure_schema(conn)  # idempotent


def test_case_roundtrip(conn):
    store.ensure_schema(conn)
    case = store.insert_case(conn, "case-x", "Did X happen?", "x", ["A"], NOW)
    assert case["status"] == store.STATUS_OPEN
    assert case["entities"] == ["A"]
    assert store.get_case(conn, "case-x")["question"] == "Did X happen?"
    assert store.get_case(conn, "case-missing") is None

    store.set_case_verdict(conn, "case-x", "supported", "h1", NOW)
    concluded = store.get_case(conn, "case-x")
    assert concluded["status"] == store.STATUS_CONCLUDED
    assert concluded["verdict"] == "supported"
    assert store.list_cases(conn, include_concluded=False) == []
    assert len(store.list_cases(conn)) == 1


def test_hypotheses_are_idempotent_by_id(conn):
    store.ensure_schema(conn)
    store.insert_case(conn, "case-x", "q", None, [], NOW)
    store.add_hypothesis(conn, "case-x", "h1", "it happened", "affirmative", NOW)
    store.add_hypothesis(conn, "case-x", "h1", "it happened AGAIN", "affirmative", NOW)
    rows = store.list_hypotheses(conn, "case-x")
    assert len(rows) == 1
    assert rows[0]["statement"] == "it happened"
    store.set_hypothesis_status(conn, "case-x", "h1", store.HYPOTHESIS_SUPPORTED)
    assert store.list_hypotheses(conn, "case-x")[0]["status"] == "supported"


def test_lead_planning_is_idempotent_and_never_reopens(conn):
    store.ensure_schema(conn)
    store.insert_case(conn, "case-x", "q", None, [], NOW)
    lead_id = store.upsert_lead(conn, "case-x", "corroborate", {"claim_id": "k1"}, "r", "h1", NOW)
    assert lead_id is not None
    # Same tool + params: already planned.
    assert store.upsert_lead(conn, "case-x", "corroborate", {"claim_id": "k1"}, "r2", "h1", NOW) is None
    # Even after pursuit it stays closed.
    store.mark_lead(conn, "case-x", lead_id, store.LEAD_PURSUED, 3, NOW)
    assert store.upsert_lead(conn, "case-x", "corroborate", {"claim_id": "k1"}, "r3", "h1", NOW) is None
    lead = store.get_lead(conn, "case-x", lead_id)
    assert lead["status"] == store.LEAD_PURSUED
    assert lead["evidence_found"] == 3
    assert store.list_leads(conn, "case-x", status=store.LEAD_OPEN) == []


def test_evidence_converges_on_reharvest(conn):
    store.ensure_schema(conn)
    store.insert_case(conn, "case-x", "q", None, [], NOW)
    args = ("case-x", "h1", store.RELATION_SUPPORTS, "corroboration", "k1",
            "Beta Journal")
    assert store.upsert_evidence(conn, *args, 0.8, True, "s1", "lead-a", NOW) is True
    # Re-harvest updates in place instead of duplicating.
    assert store.upsert_evidence(conn, *args, 0.9, True, "s2", "lead-a", NOW) is False
    rows = store.list_evidence(conn, "case-x")
    assert len(rows) == 1
    assert rows[0]["credibility"] == 0.9
    assert rows[0]["summary"] == "s2"


def test_journal_reads_oldest_first(conn):
    store.ensure_schema(conn)
    store.insert_case(conn, "case-x", "q", None, [], NOW)
    store.record_event(conn, "case-x", "case_opened", {}, NOW)
    store.record_event(conn, "case-x", "leads_planned", {"new": 2}, NOW)
    store.record_event(conn, "case-y", "case_opened", {}, NOW)
    events = store.list_events(conn, "case-x")
    assert [e["event"] for e in events] == ["case_opened", "leads_planned"]
    assert events[0]["seq"] < events[1]["seq"]


def test_digest_ids_are_deterministic():
    a = store.digest_id("lead", "corroborate", '{"claim_id": "k1"}')
    b = store.digest_id("lead", "corroborate", '{"claim_id": "k1"}')
    c = store.digest_id("lead", "corroborate", '{"claim_id": "k2"}')
    assert a == b != c
    assert a.startswith("lead-")


def test_slugify():
    assert store.slugify("Did the merger breach the filing rules?") == \
        "did-the-merger-breach-the-filing-rules"
    assert store.slugify("???") == "case"
