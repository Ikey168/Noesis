"""BaFin notice records and store: validation, identifiers, revisions, corrections and listings (#2106, BF02)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.domains.market.bafin_notices import (
    CONTRACT,
    BafinError,
    BafinNoticeStore,
    correction_chains,
    current_per_chain,
    cutoffs,
    decimal_text,
    end_of_day_ms,
    isin_valid,
    iso_day,
    lei_valid,
    normalize_bafin_id,
    normalize_isin,
    party_key,
    register_schemas,
    schema,
    semantic,
    validate_notice,
)
from src.kb.schema_registry import SchemaRegistry
from tests.unit import bafin_harness as h

NS = h.NS


def vr(
    source_id="VR-1",
    *,
    s33="5.12",
    publication="2026-03-05",
    event="2026-03-02",
    correction=None,
    **extra,
):
    notice = {
        "contract": CONTRACT,
        "kind": "voting_rights_notification",
        "source": {
            "provider": "bafin-voting-rights",
            "source_id": source_id,
            "source_id_basis": "stated",
            "url": "https://portal.mvp.bafin.de/fixture/vr.csv",
            "locator": {"row": 1},
        },
        "issuer": {"name": "Musterwerke AG", "isin": h.ISSUER, "lei": h.ISSUER_LEI},
        "notifier": {"name": "Fiktiva Holding SE", "kind": "legal_person"},
        "chain": [
            {"position": 1, "name": "Fiktiva Holding SE"},
            {
                "position": 2,
                "name": "Fiktiva Invest GmbH",
                "voting_rights_pct": s33,
                "total_pct": s33,
            },
        ],
        "thresholds": ["5"],
        "percentages": {"s33": s33, "s38": "0", "s39": s33},
        "event_date": event,
        "publication_date": publication,
        "correction_of": correction,
    }
    notice.update(extra)
    return notice


def store_at(conn, ms):
    return BafinNoticeStore(conn, now=lambda: ms)


def test_identifiers_normalise_the_same_way_on_both_sides():
    assert normalize_isin(" de000-mstr014 ") == h.ISSUER and isin_valid("de000mstr014")
    assert not isin_valid("DE000MSTR015")  # wrong check digit
    assert isin_valid("DE0007164600")  # a real ISIN shape passes the same check
    assert lei_valid(h.ISSUER_LEI) and not lei_valid(h.ISSUER_LEI[:-1] + "6")
    assert normalize_bafin_id("00123456") == normalize_bafin_id("123 456") == "123456"
    assert (
        party_key("Musterwerke AG")
        == party_key("MUSTERWERKE Aktiengesellschaft")
        == "musterwerke"
    )
    assert party_key("Müller & Söhne GmbH & Co. KG") == "mueller soehne"
    assert (
        party_key("Dr. Erika Musterfrau", kind="natural_person") == "erika musterfrau"
    )
    assert party_key("Fiktiva Invest GmbH") != party_key("Fiktiva Holding SE")


def test_values_parse_as_published_and_missing_markers_are_absent():
    assert iso_day("05.03.2026") == iso_day("2026-03-05") == "2026-03-05"
    assert iso_day("Mon, 04 May 2026 10:00:00 +0200") == "2026-05-04"
    assert iso_day("-") is None and iso_day("") is None and iso_day(None) is None
    assert (
        decimal_text("1.000", decimal=",") == "1000"
        and decimal_text("12,5333", decimal=",") == "12.5333"
    )
    assert decimal_text("1,234.5") == "1234.5" and decimal_text("3,01 %") == "3.01"
    assert decimal_text("-") is None and decimal_text("n/a") is None
    with pytest.raises(BafinError):
        iso_day("31.02.2026")


def test_notices_validate_and_round_trip_through_the_registered_schema():
    conn = h.connection()
    h.acquire_all(conn)
    validator = Draft7Validator(schema())
    views = BafinNoticeStore(conn).visible(NS)["notices"]
    kinds = {v["notice"]["kind"] for v in views}
    assert kinds == {
        "voting_rights_notification",
        "managers_transaction",
        "net_short_position",
        "bafin_warning",
        "bafin_measure",
        "authorised_entity",
    }
    for view in views:
        stored = json.loads(
            conn.execute(
                "SELECT payload_json FROM bafin_notice_revisions WHERE revision_id=?",
                [view["revision_id"]],
            ).fetchone()[0]
        )
        assert not list(validator.iter_errors(stored)), stored["kind"]
        assert validate_notice(stored) == stored  # round trip
        assert "None" not in json.dumps(stored)
    registered = register_schemas(
        conn, principal_id="svc", scopes={"knowledge:schema:register"}
    )
    assert registered[0]["name"] == "bafin-notice"
    resolved = SchemaRegistry(conn).resolve(
        "schema", "bafin-notice", "^1.0.0", scopes={"knowledge:schema:read"}
    )
    assert resolved["content"]["$id"] == CONTRACT


def test_validation_refuses_what_a_source_does_not_state():
    with pytest.raises(BafinError):
        validate_notice(
            {**vr(), "percentages": {"s33": 5.12}}
        )  # a float, never a decimal string
    with pytest.raises(BafinError):
        validate_notice(
            {**vr(), "issuer": {"name": "Musterwerke AG", "isin": "DE000MSTR015"}}
        )
    with pytest.raises(BafinError):
        validate_notice({**vr(), "chain": [{"position": 2, "name": "out of order"}]})
    with pytest.raises(BafinError):
        validate_notice({**vr(), "signal": "buy"})
    dealing = {
        "contract": CONTRACT,
        "kind": "managers_transaction",
        "source": {
            "provider": "bafin-managers-transactions",
            "source_id": "D",
            "source_id_basis": "stated",
        },
        "issuer": {"isin": h.ISSUER},
        "person": {"name": "Erika Musterfrau", "kind": "natural_person"},
        "instrument": {"isin": h.ISSUER},
        "nature": "Kauf",
        "trades": [],
    }
    with pytest.raises(BafinError, match="person table"):
        validate_notice(dealing)
    unknowns = validate_notice({**vr(), "publication_date": None, "event_date": None})[
        "unknowns"
    ]
    assert {"publication_date", "event_date"} <= set(unknowns)


def test_unchanged_reacquisition_adds_nothing_and_a_reversion_is_a_new_revision():
    conn = h.connection()
    first = store_at(conn, h.ms("2026-03-10")).apply(
        NS, [vr()], run_id="r1", observed_at_ms=h.ms("2026-03-10")
    )
    assert first["inserted"] == 1
    moved = copy.deepcopy(vr())
    moved["source"]["locator"] = {"row": 9}  # source-specific structure only
    moved["source"]["url"] = "https://portal.mvp.bafin.de/fixture/other.csv"
    assert (
        store_at(conn, 2).apply(
            NS, [moved], run_id="r2", observed_at_ms=h.ms("2026-03-11")
        )["unchanged"]
        == 1
    )
    edited = vr(s33="5.13")
    assert (
        store_at(conn, 3).apply(
            NS, [edited], run_id="r3", observed_at_ms=h.ms("2026-03-12")
        )["revised"]
        == 1
    )
    back = store_at(conn, 4).apply(
        NS, [vr()], run_id="r4", observed_at_ms=h.ms("2026-03-13")
    )
    assert back["revised"] == 1  # dedupe only against the current revision
    changes = [
        r["change"]
        for r in BafinNoticeStore(conn).history(
            NS, BafinNoticeStore(conn).visible(NS)["notices"][0]["notice_id"]
        )
    ]
    assert changes == ["new", "revised", "revised"]
    assert semantic(vr())["source"] == {
        "provider": "bafin-voting-rights",
        "source_id": "VR-1",
    }


def test_late_older_exports_are_history_never_current_in_any_arrival_order():
    newer, older = vr(s33="5.21"), vr(s33="5.12")
    orders = {}
    for name, sequence in {
        "new-first": [(newer, "2026-04-01"), (older, "2026-03-01")],
        "old-first": [(older, "2026-03-01"), (newer, "2026-04-01")],
    }.items():
        conn = h.connection()
        for index, (notice, source_as_of) in enumerate(sequence):
            counts = store_at(conn, index).apply(
                NS,
                [notice],
                run_id=f"r{index}",
                observed_at_ms=h.ms("2026-05-01") + index,
                source_as_of=source_as_of,
            )
        store = BafinNoticeStore(conn)
        current = store.visible(NS)["notices"][0]
        orders[name] = current["notice"]["percentages"]["s33"]
        if name == "new-first":
            assert counts["history"] == 1 and counts["revised"] == 0
        # As of mid-March only the March export's state is visible; the April one is not yet public.
        march = store.visible(NS, public_cutoff_ms=end_of_day_ms("2026-03-20"))[
            "notices"
        ][0]
        assert march["notice"]["percentages"]["s33"] == "5.12"
    assert orders == {"new-first": "5.21", "old-first": "5.21"}


def test_publication_clock_is_end_of_day_or_first_observation_never_the_event_date():
    conn = h.connection()
    store = store_at(conn, 0)
    store.apply(
        NS,
        [vr(publication="2026-03-05", event="2026-03-02")],
        run_id="r",
        observed_at_ms=h.ms("2026-04-01"),
    )
    store.apply(
        NS,
        [vr("VR-2", publication=None, event="2026-03-02")],
        run_id="r",
        observed_at_ms=h.ms("2026-04-01"),
    )
    ids = {v["notice"]["source"]["source_id"]: v for v in store.visible(NS)["notices"]}
    assert (
        ids["VR-1"]["public_at_ms"] == end_of_day_ms("2026-03-05")
        and ids["VR-1"]["publication_basis"] == "stated"
    )
    assert (
        ids["VR-2"]["public_at_ms"] == h.ms("2026-04-01")
        and ids["VR-2"]["publication_basis"] == "first-observed"
    )
    assert not store.visible(NS, public_cutoff_ms=end_of_day_ms("2026-03-04"))[
        "notices"
    ]
    assert [
        v["notice"]["source"]["source_id"]
        for v in store.visible(NS, public_cutoff_ms=end_of_day_ms("2026-03-05"))[
            "notices"
        ]
    ] == ["VR-1"]
    assert not store.visible(
        NS,
        public_cutoff_ms=end_of_day_ms("2026-03-05"),
        acquired_by_ms=h.ms("2026-03-31"),
    )["notices"]  # not yet acquired
    cut = cutoffs("2026-03-05", h.ms("2026-04-02"))
    assert cut["publicly_available_by_ms"] == end_of_day_ms("2026-03-05")
    assert cut["availability_policy"] == "public_and_acquired"


def test_corrections_supersede_whatever_the_arrival_order():
    for order in ("original-first", "correction-first"):
        conn = h.connection()
        original = vr("VR-1")
        correction = vr(
            "VR-7",
            s33="5.21",
            publication="2026-03-20",
            correction={"source_id": "VR-1"},
        )
        items = (
            [original, correction]
            if order == "original-first"
            else [correction, original]
        )
        for index, item in enumerate(items):
            store_at(conn, index).apply(
                NS,
                [item],
                run_id=f"r{index}",
                observed_at_ms=h.ms("2026-04-01") + index,
            )
        store = BafinNoticeStore(conn)
        current = current_per_chain(store.visible(NS)["notices"])
        assert [c["notice"]["source"]["source_id"] for c in current] == ["VR-7"], order
        assert current[0]["chain"]["link"]["status"] == "resolved"
        before = current_per_chain(
            store.visible(NS, public_cutoff_ms=end_of_day_ms("2026-03-19"))["notices"]
        )
        assert [c["notice"]["source"]["source_id"] for c in before] == ["VR-1"]


def test_a_correction_by_flag_and_date_links_only_a_unique_notice():
    conn = h.connection()
    store = store_at(conn, 0)
    flagged = vr(
        "VR-9",
        s33="5.30",
        publication="2026-03-25",
        correction={"publication_date": "2026-03-05", "stated": "Korrektur"},
    )
    store.apply(
        NS, [vr("VR-1"), flagged], run_id="r", observed_at_ms=h.ms("2026-04-01")
    )
    chains = correction_chains(store.visible(NS)["notices"])
    link = next(c["link"] for c in chains.values() if c["link"])
    assert link == {
        "status": "resolved",
        "basis": "same-issuer-notifier-publication-date",
        "corrects": next(n for n, c in chains.items() if c["superseded_by"]),
    }
    conn = h.connection()
    store = store_at(conn, 0)
    store.apply(
        NS,
        [vr("VR-1"), vr("VR-2", s33="6"), flagged],
        run_id="r",
        observed_at_ms=h.ms("2026-04-01"),
    )
    link = next(
        c["link"]
        for c in correction_chains(store.visible(NS)["notices"]).values()
        if c["link"]
    )
    assert link["status"] == "unresolved" and link["basis"] == "ambiguous"


def test_listing_removals_are_observations_and_person_data_is_withdrawn_per_notice():
    conn = h.connection()
    h.acquire(conn, "dealings", "2026-04-10")
    h.acquire(conn, "dealings", "2026-06-01")
    store = BafinNoticeStore(conn)
    views = {
        v["notice"]["source"]["source_id"]: v
        for v in store.visible(NS, kinds=("managers_transaction",))["notices"]
    }
    assert views["DD-2026-0101"]["listing"]["state"] == "no_longer_listed"
    assert views["DD-2026-0101"]["listing"]["observed_on"] == "2026-06-01"
    assert views["DD-2026-0101"]["notice"]["person"]["withdrawn"] is True
    # The same person in a notice still listed keeps her name.
    assert views["DD-2026-0110"]["notice"]["person"]["name"] == "Dr. Erika Musterfrau"
    # Revisions never held a name; only the person table did, and it is erased.
    assert "Musterfrau" not in json.dumps(
        [
            r[0]
            for r in conn.execute(
                "SELECT payload_json FROM bafin_notice_revisions"
            ).fetchall()
        ]
    )
    names = {r[0] for r in conn.execute("SELECT name FROM bafin_persons").fetchall()}
    assert names == {"Dr. Erika Musterfrau", "Max Mustermann", None}
    # As of a date before the removal the notice was listed, but the erased name stays erased.
    april = {
        v["notice"]["source"]["source_id"]: v
        for v in store.visible(
            NS,
            kinds=("managers_transaction",),
            public_cutoff_ms=end_of_day_ms("2026-04-30"),
        )["notices"]
    }
    assert (
        april["DD-2026-0101"]["listing"]["state"] == "listed"
        and april["DD-2026-0101"]["notice"]["person"]["withdrawn"]
    )
    # Re-listing restores the published name and records the listing again.
    h.acquire(conn, "dealings", "2026-04-10", run_id="relisted")
    again = {
        v["notice"]["source"]["source_id"]: v
        for v in store.visible(NS, kinds=("managers_transaction",))["notices"]
    }
    assert again["DD-2026-0101"]["notice"]["person"]["name"] == "Dr. Erika Musterfrau"


def test_reads_before_any_source_ran_are_not_ready_and_listings_survive_a_bad_row():
    conn = h.connection()
    with pytest.raises(BafinError) as caught:
        BafinNoticeStore(conn, initialize=False).visible(NS)
    assert caught.value.code == "not_ready"
    h.acquire(conn, "voting", "2026-03-10")
    conn.execute(
        "UPDATE bafin_notice_revisions SET payload_json='{broken' WHERE revision=1"
    )
    result = BafinNoticeStore(conn).visible(NS)
    assert result["notices"] == [] and result["unreadable"][0]["reason"].startswith(
        "JSONDecodeError"
    )


def test_generation_changes_whenever_any_source_changes():
    conn = h.connection()
    store = BafinNoticeStore(conn)
    empty = store.generation(NS)
    h.acquire(conn, "voting", "2026-03-10")
    first = store.generation(NS)
    h.acquire(conn, "voting", "2026-03-10", run_id="replay")
    assert store.generation(NS) == first  # an unchanged replay changes nothing
    h.acquire(conn, "voting", "2026-04-20")
    assert len({empty, first, store.generation(NS)}) == 3
