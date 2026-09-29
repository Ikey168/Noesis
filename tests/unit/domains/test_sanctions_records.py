"""Sanctions records: snapshots, per-list revisions, legal-basis resolution (#1915, #1920, #1927)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from src.kb.sanctions import SanctionsError, SanctionsStore
from tests.unit import sanctions_harness as h

SCHEMA = jsonschema.Draft7Validator(
    json.loads(
        (
            Path(h.ROOT)
            / "contracts/schemas/jsonschema/noesis-sanctions-record-v1.json"
        ).read_text()
    )
)


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_legal(conn, "cellar-sanctions-acts-eng")
    for list_id, files in h.FILES.items():
        for name in files:
            h.apply(conn, list_id, name)
    yield conn, SanctionsStore(conn), None
    conn.close()


def tables(conn):
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        for t in (
            "sanctions_snapshots",
            "sanctions_designations",
            "sanctions_revisions",
            "sanctions_snapshot_members",
            "sanctions_aliases",
            "sanctions_programmes",
        )
    }


def designation(conn, list_id, entry_id):
    return conn.execute(
        "SELECT designation_id FROM sanctions_designations WHERE list_id=? AND list_entry_id=?",
        [list_id, entry_id],
    ).fetchone()[0]


def test_revisions_are_derived_from_successive_snapshots_citing_both():
    conn = h.connection()
    first = h.apply(conn, "eu", "eu_fsf_2026-01-15.xml")
    second = h.apply(conn, "eu", "eu_fsf_2026-03-01.xml")
    third = h.apply(conn, "eu", "eu_fsf_2026-06-01.xml")
    assert (first["listed"], second["listed"]) == (2, 1)
    assert {k: third[k] for k in ("listed", "amended", "delisted", "relisted")} == {
        "listed": 1,
        "amended": 1,
        "delisted": 1,
        "relisted": 0,
    }
    store = SanctionsStore(conn)
    history = store.history("global", designation(conn, "eu", "EU.9002.02"))
    assert [r["change"] for r in history] == ["listed", "delisted"]
    delisting = history[-1]
    assert delisting["record_type"] == "delisting" and delisting["statement"] is None
    assert [s["publication_date"] for s in delisting["compared_snapshots"]] == [
        "2026-03-01",
        "2026-06-01",
    ]
    assert (
        delisting["source_dates"]["delisted_on"] is None
    )  # the file states no delisting date
    amended = store.history("global", designation(conn, "eu", "EU.9001.01"))
    assert [r["change"] for r in amended] == ["listed", "amended"]
    assert amended[-1]["compared_snapshots"][0]["publication_date"] == "2026-03-01"
    for record in history + amended:
        assert not list(SCHEMA.iter_errors(record)), record


def test_replay_is_idempotent_whatever_the_fetch_time_and_older_snapshots_are_refused():
    conn = h.connection()
    for name in h.FILES["eu"]:
        h.apply(conn, "eu", name)
    before = tables(conn)
    again = h.apply(conn, "eu", "eu_fsf_2026-06-01.xml", run_id="a-later-run")
    assert again["status"] == "unchanged" and tables(conn) == before
    # An already-applied older file is recognised by its digest: unchanged, not refused as out of order.
    assert (
        h.apply(conn, "eu", "eu_fsf_2026-03-01.xml", run_id="replay")["status"]
        == "unchanged"
    )
    assert tables(conn) == before


def test_out_of_order_and_partial_snapshots_are_refused():
    conn = h.connection()
    h.apply(conn, "eu", "eu_fsf_2026-06-01.xml")
    with pytest.raises(SanctionsError) as caught:
        h.apply(conn, "eu", "eu_fsf_2026-03-01.xml")
    assert caught.value.code == "out_of_order_snapshot"
    page, _ = h.list_page("un", "un_sc_2026-03-10.xml")
    header = dict(page.records[0]["sanctions_snapshot"])
    with pytest.raises(SanctionsError) as caught:
        SanctionsStore(conn).apply_snapshot(
            "global",
            header,
            [page.records[0]["sanctions_entry"]],
            run_id="r",
            source_id="un-sc-consolidated",
        )
    assert caught.value.code == "incomplete_snapshot"


def test_identity_is_per_list_and_aliases_are_assertions_of_one_revision(loaded):
    conn, store, _ = loaded
    keys = [
        r[0]
        for r in conn.execute(
            "SELECT record_key FROM sanctions_designations ORDER BY 1"
        ).fetchall()
    ]
    assert (
        "sanctions:eu:EU.9002.02" in keys
        and "sanctions:ofac:99001" in keys
        and "sanctions:uk:RUS9002" in keys
    )
    assert (
        len(keys) == len(set(keys)) == 10
    )  # same vessel on three lists: three designations, no shared key
    vessel = designation(conn, "ofac", "99001")
    first, second = store.history("global", vessel)
    assert {a["value"] for a in store.aliases("global", second["revision_id"])} - {
        a["value"] for a in store.aliases("global", first["revision_id"])
    } == {"STAR OF FICTION"}
    for alias in store.aliases("global", second["revision_id"]):
        assert alias["assertion"] == "stated by the list at this revision" and not list(
            SCHEMA.iter_errors(alias)
        )
    snapshot = store.snapshot("global", second["snapshot_id"])
    assert (
        snapshot["publication_date"] == "2026-06-05"
        and len(snapshot["file_sha256"]) == 64
    )
    assert snapshot["acquired_at_ms"] and snapshot["source_id"] == "ofac-sls"
    assert not list(SCHEMA.iter_errors(store.designation("global", vessel)))


def test_legal_basis_resolves_to_cellar_works_by_exact_celex_or_stays_unresolved(
    loaded,
):
    conn, store, _ = loaded
    rows = dict(
        conn.execute(
            "SELECT celex, status FROM sanctions_legal_bases ORDER BY 1"
        ).fetchall()
    )
    assert rows == {"32014R0269": "resolved", "32022R0336": "unresolved"}
    work = conn.execute(
        "SELECT work_id FROM sanctions_legal_bases WHERE celex='32014R0269'"
    ).fetchone()[0]
    identifiers = json.loads(
        conn.execute(
            "SELECT identifiers_json FROM legal_works WHERE work_id=?", [work]
        ).fetchone()[0]
    )
    assert identifiers["celex"] == "32014R0269"
    # Resolution is a link, not a list statement: re-resolving adds no revision.
    before = tables(conn)
    assert store.resolve_legal_bases("global") == 0 and tables(conn) == before


def test_a_legal_act_acquired_later_resolves_on_the_next_unchanged_replay():
    conn = h.connection()
    h.apply(conn, "eu", "eu_fsf_2026-01-15.xml")
    assert (
        conn.execute(
            "SELECT count(*) FROM sanctions_legal_bases WHERE status='resolved'"
        ).fetchone()[0]
        == 0
    )
    h.load_legal(conn, "cellar-sanctions-acts-eng")
    replay = h.apply(conn, "eu", "eu_fsf_2026-01-15.xml", run_id="later")
    assert replay["status"] == "unchanged" and replay["legal_bases_resolved"] == 1
