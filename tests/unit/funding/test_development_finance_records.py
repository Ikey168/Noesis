"""Development-finance records: publishers, revisions, coverage, CRS vintages and World Bank projects (#1967)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from src.kb.development_finance import (
    DevelopmentFinanceError,
    DevelopmentFinanceStore,
    as_of_ms,
    forbidden_keys,
    transaction_keys,
)
from tests.unit.funding import development_finance_harness as h

SCHEMA = Draft7Validator(
    json.loads(
        (
            h.ROOT
            / "contracts/schemas/jsonschema/noesis-development-finance-record-v1.json"
        ).read_text()
    )
)
WATER = "XM-DAC-99901-FICT-0001"


@pytest.fixture()
def env():
    env = h.Env().load()
    yield env
    env.conn.close()


def valid(record):
    errors = sorted(SCHEMA.iter_errors(json.loads(json.dumps(record))), key=str)
    assert not errors, errors[0]
    return record


def test_every_record_type_validates_against_the_registered_contract(env):
    store = DevelopmentFinanceStore(env.conn)
    for revision in store.current(h.NS).values():
        valid(revision)
        for tx in revision["transactions"]:
            valid(tx)
        assert not forbidden_keys(revision)
    for publisher in store.publishers(h.NS):
        valid(publisher)
    for coverage in store.coverage_rows(h.NS):
        valid(coverage)
    for cell in store.crs_cells(h.NS):
        valid(cell)
        for vintage in store.crs_vintages(h.NS, cell["cell_id"]):
            valid(vintage)
    for project in store.world_bank_projects(h.NS):
        valid(project)
    assert (
        SCHEMA.is_valid(
            {
                "record_type": "publisher",
                "publisher_id": "x",
                "provider": "iati",
                "verdict": "good",
            }
        )
        is False
    )


def test_publishers_are_first_class_and_the_same_identifier_stays_per_publisher(env):
    store = DevelopmentFinanceStore(env.conn)
    reports = [r for r in store.current(h.NS).values() if r["iati_identifier"] == WATER]
    assert {r["publisher_id"] for r in reports} == {
        "iati:ref:XM-DAC-99901",
        "iati:ref:XI-IATI-FICTNGO",
    }
    assert {r["dataset_id"] for r in reports} and all(
        r["capture_sha256"] and r["source"]["dataset_id"] for r in reports
    )
    # The organisation a publisher reports on is not the publisher: the NGO's funder is publisher A's reference,
    # but the NGO's revision still points to the NGO as publisher.
    ngo = next(r for r in reports if r["publisher_id"].endswith("FICTNGO"))
    assert ngo["activity"]["participating_orgs"][0]["ref"] == h.FDPA
    assert {p["provider"] for p in store.publishers(h.NS)} == {
        "iati",
        "oecd-crs",
        "world-bank-projects",
    }


def test_identifiers_are_stored_verbatim(env):
    store = DevelopmentFinanceStore(env.conn)
    rows = env.conn.execute(
        "SELECT iati_identifier FROM devfin_activities ORDER BY 1"
    ).fetchall()
    assert [r[0] for r in rows] == [WATER, WATER, "XM-DAC-99901-FICT-0002"]
    assert store.publishers(h.NS)[0]["publisher_ref"] == h.NGO


def test_unchanged_reacquisition_adds_nothing_and_a_change_is_a_new_revision(env):
    before = env.conn.execute(
        "SELECT count(*) FROM devfin_activity_revisions"
    ).fetchone()[0]
    generation = DevelopmentFinanceStore(env.conn).generation(h.NS)
    again = env.at("2098-03-20T08:00:00").iati(
        "iati_fdpa_2098-03.xml", observation="r1b:fdpa"
    )
    assert again["counts"] == {"unchanged": 2}
    assert (
        env.conn.execute("SELECT count(*) FROM devfin_activity_revisions").fetchone()[0]
        == before
    )
    store = DevelopmentFinanceStore(env.conn)
    assert store.generation(h.NS) == generation  # no source changed
    changed = env.at("2098-06-05T08:00:00").iati(
        "iati_fdpa_2098-06.xml", observation="r2:fdpa"
    )
    assert changed["counts"] == {"newer": 1, "first": 1}
    history = store.history(h.NS, h.activity_key(env.conn, h.FDPA, WATER))
    assert [r["revision_no"] for r in history] == [1, 2] and history[1][
        "supersedes"
    ] == history[0]["revision_id"]
    # The first revision is untouched: an overwrite would have changed its transactions.
    assert (
        history[0]["transactions"][1]["value"] == "250000"
        and history[1]["transactions"][1]["value"] == "275000"
    )


def test_current_follows_the_publishers_stamp_and_late_older_versions_are_history(env):
    env.at("2098-06-05T08:00:00").iati("iati_fdpa_2098-06.xml", observation="r2:fdpa")
    # The March file arrives again later (a replayed mirror): it is known history, not a new revision.
    late = env.at("2098-07-01T08:00:00").iati(
        "iati_fdpa_2098-03.xml", observation="r3:fdpa"
    )
    assert late["counts"] == {"known-history": 1, "unchanged": 1}
    store = DevelopmentFinanceStore(env.conn)
    key = h.activity_key(env.conn, h.FDPA, WATER)
    assert store.current(h.NS)[key]["last_updated_at"] == "2098-06-01T10:00:00+00:00"
    # A modified older version is stored as history without becoming current.
    older = h.edited("iati_fdpa_2098-03.xml", (">1000000<", ">999999<"))
    result = env.at("2098-07-02T08:00:00").iati(older, observation="r4:fdpa")
    outcome = next(a for a in result["activities"] if a["activity_key"] == key)
    assert outcome["outcome"] == "late-older"
    assert store.current(h.NS)[key]["last_updated_at"] == "2098-06-01T10:00:00+00:00"


def test_as_of_selects_the_revision_observed_by_then(env):
    env.at("2098-06-05T08:00:00").iati("iati_fdpa_2098-06.xml", observation="r2:fdpa")
    store = DevelopmentFinanceStore(env.conn)
    key = h.activity_key(env.conn, h.FDPA, WATER)
    assert store.current(h.NS, as_of=as_of_ms("2098-04-30"))[key]["revision_no"] == 1
    assert store.current(h.NS, as_of=as_of_ms("2098-06-05"))[key]["revision_no"] == 2
    assert store.current(h.NS, as_of=as_of_ms("2098-01-01")) == {}


def test_a_complete_selection_records_a_withdrawal_that_never_means_ended(env):
    result = env.at("2098-06-05T08:00:00").iati(
        "iati_fdpa_2098-06.xml", observation="r2:fdpa"
    )
    education = h.activity_key(env.conn, h.FDPA, "XM-DAC-99901-FICT-0002")
    assert result["withdrawn"] == [education]
    store = DevelopmentFinanceStore(env.conn)
    state = store.publication_state(h.NS, education)
    assert (
        state["state"] == "withdrawn-by-publisher"
        and "not mean the activity ended" in state["note"]
    )
    assert (
        store.publication_state(h.NS, education, as_of=as_of_ms("2098-05-01"))["state"]
        == "published"
    )
    # Its last revision stays addressable and current; nothing is deleted.
    assert store.current(h.NS)[education]["revision_no"] == 1
    # Re-running the same complete selection records nothing new.
    again = env.at("2098-06-20T08:00:00").iati(
        "iati_fdpa_2098-06.xml", observation="r3:fdpa"
    )
    assert (
        again["withdrawn"] == []
        and env.conn.execute("SELECT count(*) FROM devfin_withdrawals").fetchone()[0]
        == 1
    )


def test_a_reversion_is_a_new_revision_and_unknown_amounts_stay_unknown(env):
    env.at("2098-06-05T08:00:00").iati("iati_fdpa_2098-06.xml", observation="r2:fdpa")
    reverted = h.edited(
        "iati_fdpa_2098-06.xml",
        (">275000<", ">250000<"),
        ("2098-06-01T10:00:00Z", "2098-07-01T10:00:00Z"),
    )
    env.at("2098-07-05T08:00:00").iati(reverted, observation="r3:fdpa")
    store = DevelopmentFinanceStore(env.conn)
    key = h.activity_key(env.conn, h.FDPA, WATER)
    history = store.history(h.NS, key)
    assert [r["revision_no"] for r in history] == [1, 2, 3]
    assert (
        history[-1]["transactions"][1]["value"] == "250000"
        and history[-1]["arrival"] == "newer"
    )
    undated = history[-1]["transactions"][2]
    assert undated["value_date"] is None and undated["amount_state"] == "unknown"
    assert not env.conn.execute(
        "SELECT * FROM information_schema.columns WHERE table_name='devfin_activities' "
        "AND column_name LIKE '%total%'"
    ).fetchall()


def test_transaction_identity_uses_ref_then_type_date_and_parties():
    keys = transaction_keys(
        [
            {"ref": "A-1", "type": "3"},
            {"type": "3", "date": "2098-01-01"},
            {"type": "3", "date": "2098-01-01"},
        ]
    )
    assert keys == ["ref:A-1", "pos:3|2098-01-01||", "pos:3|2098-01-01||#1"]


def test_crs_vintages_are_statistics_and_as_of_selects_the_vintage(env):
    store = DevelopmentFinanceStore(env.conn)
    july = env.at("2099-07-20T08:00:00").crs(
        "crs_deu_ken_140_2099-07.csv",
        release={"label": "CRS release 2099-07", "published_on": "2099-07-15"},
    )
    outcomes = sorted(c["outcome"] for c in july["cells"])
    # Every declared release is a vintage of each cell it states, even where a value repeats.
    assert outcomes == ["newer", "newer", "newer"]
    (current,) = store.crs_cells(h.NS, recipient="KEN", price_basis="current")
    vintages = store.crs_vintages(h.NS, current["cell_id"])
    assert [v["published_on"] for v in vintages] == ["2099-01-15", "2099-07-15"]
    assert (
        vintages[0]["observations"][1]["value"] == "12.25"
        and vintages[1]["observations"][1]["value"] == "13"
    )
    as_of_may = store.crs_vintages(
        h.NS, current["cell_id"], as_of=as_of_ms("2099-05-01")
    )
    assert [v["published_on"] for v in as_of_may] == ["2099-01-15"]
    (constant,) = store.crs_cells(h.NS, recipient="KEN", price_basis="constant")
    assert store.crs_vintages(h.NS, constant["cell_id"])[-1]["base_year"] == "2097"
    (aggregate,) = store.crs_cells(h.NS, recipient="DPGC")
    assert aggregate["recipient"]["kind"] == "aggregate"
    first, second = store.crs_vintages(h.NS, aggregate["cell_id"])
    assert (
        first["observations"][1]["value"] is None
        and first["content_hash"] == second["content_hash"]
    )
    # The same release again adds nothing; an older release arriving late is history, not a correction.
    assert all(
        c["outcome"] == "unchanged"
        for c in env.crs(
            "crs_deu_ken_140_2099-07.csv",
            release={"label": "CRS release 2099-07", "published_on": "2099-07-15"},
        )["cells"]
    )
    late = env.at("2099-08-01T08:00:00").crs("crs_deu_ken_140_2099-01.csv")
    assert {c["outcome"] for c in late["cells"]} == {"known-history"}
    # An older release under another label lands as history and never becomes the current vintage.
    relabelled = env.at("2099-08-02T08:00:00").crs(
        "crs_deu_ken_140_2099-01.csv",
        release={"label": "CRS release 2099-01 (mirror)", "published_on": "2099-01-15"},
    )
    assert {c["outcome"] for c in relabelled["cells"]} == {"late-older"}
    assert (
        store.crs_vintages(h.NS, current["cell_id"])[-1]["published_on"] == "2099-07-15"
    )
    assert not env.conn.execute(
        "SELECT count(*) FROM devfin_activities WHERE publisher_id LIKE 'oecd%'"
    ).fetchone()[0]


def test_world_bank_projects_are_their_own_records(env):
    store = DevelopmentFinanceStore(env.conn)
    projects = {p["project_id"]: p for p in store.world_bank_projects(h.NS)}
    assert set(projects) == {"P999001", "P999002", "P999003"}
    assert projects["P999001"]["publisher_id"] == "world-bank-projects:ref:WORLD-BANK"
    again = env.at("2098-05-01T08:00:00").world_bank(observation="r2:wb")
    assert {p["outcome"] for p in again["projects"]} == {"unchanged"}


def test_reads_before_any_acquisition_are_not_ready():
    import duckdb

    store = DevelopmentFinanceStore(duckdb.connect(), initialize=False)
    with pytest.raises(DevelopmentFinanceError) as error:
        store.require_ready()
    assert error.value.code == "not_ready"
    assert (
        store.current("x") == {}
        and store.coverage_rows("x") == []
        and store.world_bank_projects("x") == []
    )


def test_writes_need_the_write_scope_and_namespace(env):
    store = DevelopmentFinanceStore(env.conn)
    with pytest.raises(DevelopmentFinanceError):
        store.record_failure(
            h.NS,
            "iati-datastore",
            {"publishers": [h.FDPA]},
            failure_code="x",
            observed_at_ms=1,
            scopes=h.READ_ONLY,
        )
