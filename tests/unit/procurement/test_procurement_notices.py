"""Normalisation of notices, revisions, deadlines, CPV codes and lots across sources (P07)."""

import pytest

from src.kb.procurement_notices import NoticeError, classify_changes, lot_view
from tests.unit.procurement.harness import NS, SCOPES, TED_CANTEEN, TED_CLOSED, TED_N1, TED_PIN, UK_TENDER, Env


@pytest.fixture
def env():
    value = Env()
    value.acquire()
    return value


def get(env, provider, procedure_id, **kwargs):
    return env.notices().get(NS, env.key(provider, procedure_id), scopes=SCOPES, as_of_ms=env.now(), **kwargs)


def test_procedures_are_comparable_across_sources_with_originals_retained(env):
    procedures = env.notices().list(NS, scopes=SCOPES, as_of_ms=env.now())
    assert {p["provider"] for p in procedures} == {"ted", "uk-fts", "uk-cf", "sam-gov"}
    ted, uk = get(env, "ted", TED_N1), get(env, "uk-fts", UK_TENDER)
    assert ted["record"]["procedure"] == {**ted["record"]["procedure"], "type": "open", "native": "open"}
    assert uk["record"]["procedure"]["native"] == "open / Open procedure"
    assert ted["status"]["state"] == uk["status"]["state"] == "open"
    assert uk["record"]["classifications"][0]["native"] == "72253000-3" and uk["record"]["classifications"][0]["code"] == "72253000"
    assert ted["status"]["next_deadline"]["text"] == "2026-11-03+01:00 12:00:00+01:00"
    assert get(env, "ted", TED_PIN)["status"]["state"] == "forthcoming"
    assert get(env, "ted", TED_CLOSED)["status"]["state"] == "closed"


def test_lots_are_first_class_with_their_own_criteria_deadlines_and_values(env):
    record = get(env, "ted", TED_N1)["record"]
    one, two = lot_view(record, "LOT-0001"), lot_view(record, "LOT-0002")
    assert one["cpv"] == ["72253000"] and two["cpv"] == ["72222300"]
    assert one["estimated_value"]["amount"] == "400000" and two["estimated_value"]["amount"] == "900000"
    ids = {r["requirement_id"] for r in one["requirements"]}
    assert "ted:LOT-0001:criterion:1" in ids and "ted:LOT-0002:criterion:1" not in ids and "ted:exclusion:tax-pay" in ids
    assert {d["lot_id"] for d in one["deadlines"]} == {"LOT-0001"}
    status = get(env, "ted", TED_N1)["status"]
    assert set(status["lots"]) == {"LOT-0001", "LOT-0002"}


def test_corrigendum_is_a_revision_with_classified_changes_and_cancellation_is_not_closure_by_absence(env):
    summary = env.acquire(2)
    assert summary["status"] == "complete"
    n1 = get(env, "ted", TED_N1)
    assert n1["revision"] == 2 and n1["cause"]["stage"] == "corrigendum" and n1["cause"]["notice_id"] == "00650001-2026"
    kinds = [c["kind"] for c in n1["changes"]]
    assert kinds[0] == "corrigendum" and "deadline_change" in kinds and "requirement_change" in kinds
    assert n1["status"]["next_deadline"]["text"] == "2026-11-17+01:00 12:00:00+01:00"
    history = env.notices().history(NS, env.key("ted", TED_N1), scopes=SCOPES)
    assert [h["cause"]["notice_id"] for h in history] == ["00612345-2026", "00650001-2026"]
    old = get(env, "ted", TED_N1, revision=1)
    assert old["record"]["deadlines"][0]["text"].startswith("2026-11-03")
    canteen = get(env, "ted", TED_CANTEEN)
    assert canteen["status"]["state"] == "cancelled" and canteen["cancellation"]["notice_id"] == "00650002-2026"
    assert canteen["record"]["title"] == "Canteen and catering services"


def test_failed_refresh_marks_the_source_stale_and_closes_nothing(env):
    receipt = env.acquire(fail=["ted-notices"], sources=["ted-notices"])
    assert receipt["sources"][0]["status"] == "failed"
    state = env.notices().provider_state(NS, "ted")
    assert state["stale"] and state["last_failure_code"] == "source_unavailable" and state["last_success_ms"]
    n1 = get(env, "ted", TED_N1)
    assert n1["status"]["state"] == "open" and any("refresh failed" in r for r in n1["status"]["reasons"])


def test_absence_from_a_complete_listing_is_unconfirmed_never_closed(env):
    store = env.notices()
    key = env.key("ted", TED_N1)
    record = store.get(NS, key, scopes=SCOPES)["record"]
    others = [store.get(NS, env.key("ted", p), scopes=SCOPES)["record"] for p in (TED_PIN,)]
    store.ingest(NS, "ted", others, observation_id="partial", observed_at_ms=env.now(), scopes=SCOPES,
                 coverage={"complete": True, "listing": "ted-notices"})
    n1 = store.get(NS, key, scopes=SCOPES, as_of_ms=env.now())
    assert n1["listing_state"] == "absent_from_listing" and n1["status"]["state"] == "unconfirmed"
    store.ingest(NS, "ted", [record], observation_id="again", observed_at_ms=env.now(), scopes=SCOPES)
    assert store.get(NS, key, scopes=SCOPES, as_of_ms=env.now())["status"]["state"] == "open"


def test_identical_and_late_older_notices_create_no_revision(env):
    store = env.notices()
    key = env.key("ted", TED_N1)
    record = store.get(NS, key, scopes=SCOPES)["record"]
    summary = store.ingest(NS, "ted", [record], observation_id="same", observed_at_ms=env.now(), scopes=SCOPES)
    assert summary["unchanged"] == [key]
    env.acquire(2)
    late = store.ingest(NS, "ted", [record], observation_id="late", observed_at_ms=env.now(), scopes=SCOPES)
    assert late["unchanged"] == [key] or late["superseded"]
    assert store.get(NS, key, scopes=SCOPES)["revision"] == 2


def test_awards_are_linked_history_that_never_opens_a_procedure(env):
    store = env.notices()
    awards = store.award_history(NS, scopes=SCOPES)
    assert {a["stage"] for a in awards} == {"award", "modification"}
    assert all(a["semantics"].startswith("award history") for a in awards)
    assert not any(p["procedure_id"] in {a["procedure_id"] for a in awards}
                   for p in store.list(NS, scopes=SCOPES) if p["status"]["state"] == "open")
    helpdesk = store.award_history(NS, scopes=SCOPES, supplier="Helpdesk Fixture Services GmbH")
    assert len(helpdesk) == 1 and helpdesk[0]["value"]["kind"] == "awarded" and helpdesk[0]["date"] == "2024-03-15"
    assert store.award_history(NS, scopes=SCOPES, cpv="72200000") and not store.award_history(NS, scopes=SCOPES, cpv="55000000")


def test_views_are_invalidated_by_revisions_and_by_awards(env):
    store = env.notices()
    key = env.key("ted", TED_N1)
    store.register_view(NS, "view:a", "alice", {key: 1})
    assert store.view_status("view:a")["current"]
    env.acquire(2)
    status = store.view_status("view:a")
    assert not status["current"] and "corrigendum" in status["invalidations"][0]["reasons"]
    record = store.get(NS, key, scopes=SCOPES)["record"]
    store.register_view(NS, "view:b", "alice", {key: 2})
    award = {**{k: v for k, v in record.items() if k not in {"deadlines", "requirements", "lots", "estimated_value", "documents",
                                                            "place_of_performance", "changes", "unknowns"}},
             "notice_id": "award-x", "stage": "award", "status": {"asserted": "complete"},
             "awards": [{"award_id": "1", "lot_ids": ["LOT-0001"], "suppliers": [], "status": "active"}]}
    store.ingest(NS, "ted", [award], observation_id="award", observed_at_ms=env.now(), scopes=SCOPES)
    assert store.view_status("view:b")["invalidations"][-1]["reasons"] == ["award"]
    lots = store.get(NS, key, scopes=SCOPES, as_of_ms=env.now())["status"]["lots"]
    assert lots["LOT-0001"]["state"] == "awarded" and lots["LOT-0002"]["state"] == "open"


def test_reads_require_scope_and_namespace(env):
    with pytest.raises(NoticeError):
        env.notices().list(NS, scopes={"knowledge:procurement:read"})
    with pytest.raises(NoticeError):
        env.notices().ingest(NS, "ted", [], observation_id="x", observed_at_ms=1, scopes={"knowledge:procurement:read"})


def test_classify_changes_names_lot_level_deadline_moves():
    before = {"deadlines": [{"kind": "submission", "lot_id": "L1", "text": "a"}], "requirements": [], "lots": []}
    after = {"deadlines": [{"kind": "submission", "lot_id": "L1", "text": "b"}], "requirements": [], "lots": []}
    change = classify_changes(before, after)[0]
    assert change == {"kind": "deadline_change", "deadline_kind": "submission", "lot_id": "L1", "before": "a", "after": "b"}
