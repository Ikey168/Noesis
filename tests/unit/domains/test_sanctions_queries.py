"""What each list stated about a party as of a date, per list and cited (#1963)."""

from __future__ import annotations

import pytest

from src.kb.sanctions import SanctionsError, SanctionsStore
from src.kb.sanctions_queries import SanctionsQueries
from tests.unit import sanctions_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_legal(conn, "cellar-sanctions-acts-eng")
    for list_id, files in h.FILES.items():
        for name in files:
            h.apply(conn, list_id, name)
    yield conn, SanctionsStore(conn), SanctionsQueries(conn)
    conn.close()


def test_as_of_answers_follow_snapshots_and_never_infer_between_them(loaded):
    _, _, queries = loaded
    states = {}
    for day in ("2026-01-01", "2026-01-15", "2026-03-01", "2026-04-15", "2026-07-01"):
        answer = queries.history_as_of(
            "global", day, scopes=h.SCOPES, list_id="eu", list_entry_id="EU.9002.02"
        )
        states[day] = answer["lists"]["eu"][0]
    assert (
        states["2026-01-01"]["status"] == "unknown"
    )  # before the first acquired snapshot
    assert (
        states["2026-01-15"]["status"] == "not_listed_in_snapshot"
        and states["2026-01-15"]["delisting"] is None
    )
    listed = states["2026-03-01"]
    assert (
        listed["status"] == "listed"
        and listed["coverage"]["basis"] == "snapshot published on the date"
    )
    assert listed["statement"]["listing_revision"]["change"] == "listed"
    gap = states["2026-04-15"]
    assert gap["status"] == "unknown" and [
        s["status"] for s in gap["statements_side_by_side"]
    ] == ["listed", "not_listed_in_snapshot"]
    after = states["2026-07-01"]
    assert (
        after["status"] == "not_listed_in_snapshot"
        and after["delisting"]["change"] == "delisted"
    )
    assert after["coverage"]["basis"].startswith("latest acquired snapshot")


def test_answers_are_grouped_by_list_with_side_by_side_conflicts_and_cited_legal_basis(
    loaded,
):
    _, _, queries = loaded
    answer = queries.history_as_of(
        "global", "2026-06-15", scopes=h.SCOPES, identifier="X1234567"
    )
    assert sorted(answer["lists"]) == ["eu", "un"]
    assert answer["differences_between_lists"][0] == {
        "attribute": "date_of_birth",
        "by_list": {"eu": ["1970-01-01"], "un": ["1970-01-02"]},
    }
    vessel = queries.history_as_of(
        "global", "2026-03-01", scopes=h.SCOPES, identifier="IMO 9999991"
    )
    eu = vessel["lists"]["eu"][0]
    basis = eu["statement"]["legal_basis"][0]
    assert basis["status"] == "resolved" and basis["passages"]["status"] == "found"
    assert basis["passages"]["passages"][0]["locator"]["kind"] == "xhtml-paragraph"
    assert basis["passages"]["passages"][0]["text"].startswith("MV Fictional Star")
    assert eu["statement"]["programmes"][0]["code"] == "UKR"
    assert h.forbidden_keys(answer) == [] and h.forbidden_keys(vessel) == []
    assert "not a screening result" in answer["notice"]


def test_name_lookup_is_exact_and_reported_as_a_statement_not_an_identity(loaded):
    _, _, queries = loaded
    exact = queries.lookup("global", scopes=h.SCOPES, name="Examplar Freight LLC")
    assert sorted(exact["lists"]) == ["eu", "ofac", "uk", "un"]
    assert exact["matched_by"].startswith("exact stated name")
    assert (
        queries.lookup("global", scopes=h.SCOPES, name="Examplar Freight")["status"]
        == "not_found_in_acquired_lists"
    )  # no similarity search


def test_reads_need_scopes_and_an_uninitialized_warehouse_is_never_written():
    conn = h.connection()
    queries = SanctionsQueries(conn)
    answer = queries.history_as_of(
        "global", "2026-01-01", scopes=h.READ_ONLY, list_id="eu", list_entry_id="x"
    )
    assert answer["status"] == "not_found_in_acquired_lists"
    assert not conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='sanctions_snapshots'"
    ).fetchone()
    with pytest.raises(SanctionsError) as caught:
        queries.history_as_of(
            "global",
            "2026-01-01",
            scopes={"namespace:global:read"},
            list_id="eu",
            list_entry_id="x",
        )
    assert caught.value.code == "unauthorized"
