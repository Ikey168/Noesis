"""Store: round trip, release append, corrections, reversions, late older data and as-of reads (MT02, #2080)."""

from __future__ import annotations

import duckdb
import pytest

from src.kb.materials_store import MaterialsError, MaterialsStore
from tests.unit.materials import builders as b


class Clock:
    def __init__(self):
        self.t = 1_000

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture()
def store():
    return MaterialsStore(duckdb.connect(":memory:"), now=Clock())


def gap(number, release="2099.1.0", released_on="2099-01-15", **kw):
    return b.entry(
        "materials-project",
        "mp-990001",
        [b.value("band_gap", number, "eV", method=b.computed("GGA"))],
        release=release,
        released_on=released_on,
        **kw,
    )


def current_gap(store, **kw):
    (value,) = store.current_values(b.NS, prop="band_gap", **kw)
    return value


def test_round_trip_and_idempotent_reacquisition(store):
    first = store.apply(
        b.NS,
        [gap("1.80")],
        run_id="r1",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=10,
    )
    assert (
        first["series"] == 1 and first["value_versions"] == 1 and first["releases"] == 1
    )
    again = store.apply(
        b.NS,
        [gap("1.8")],
        run_id="r2",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=20,
    )
    assert (
        again["value_versions"] == 0 and again["unchanged"] == 1
    )  # '1.8' == '1.80' after normalisation
    value = current_gap(store)
    assert value["value"] == "1.80" and value["normalized"]["normalized_value"] == "1.8"
    assert (
        value["method_class"] == "computed" and value["release"]["label"] == "2099.1.0"
    )
    assert (
        value["change"] == "initial"
        and value["record_key"] == "materials:entry:materials-project:mp-990001"
    )
    entry = store.entry(b.NS, value["entry_id"])
    assert entry["content"]["material"]["reduced_formula"] == "O2Ti" and entry[
        "in_releases"
    ] == ["2099.1.0"]


def test_correction_and_reversion_are_new_versions_compared_with_the_current_only(
    store,
):
    for index, number in enumerate(("1.80", "1.95", "1.80")):
        store.apply(
            b.NS,
            [gap(number)],
            run_id=f"r{index}",
            principal_id="p",
            scopes=b.SCOPES,
            observed_at_ms=10 * (index + 1),
        )
    history = store.value_history(b.NS, current_gap(store)["identity_key"])
    assert [(h["version"], h["value"], h["change"]) for h in history] == [
        (1, "1.80", "initial"),
        (2, "1.95", "correction"),
        (3, "1.80", "correction"),
    ]
    assert (
        current_gap(store)["value"] == "1.80"
        and current_gap(store, as_of_ms=25)["value"] == "1.95"
    )


def test_new_release_appends_and_prior_release_stays_queryable(store):
    store.apply(
        b.NS,
        [gap("1.80")],
        run_id="r1",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=10,
    )
    store.apply(
        b.NS,
        [gap("1.70", release="2099.2.0", released_on="2099-06-01")],
        run_id="r2",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=20,
    )
    assert (
        current_gap(store)["release"]["label"] == "2099.2.0"
        and current_gap(store)["change"] == "initial"
    )
    assert current_gap(store, as_of_ms=15)["release"]["label"] == "2099.1.0"
    history = store.value_history(b.NS, current_gap(store)["identity_key"])
    assert [(h["release"]["label"], h["value"]) for h in history] == [
        ("2099.1.0", "1.80"),
        ("2099.2.0", "1.70"),
    ]


def test_late_arriving_older_release_is_not_current_and_not_a_correction(store):
    store.apply(
        b.NS,
        [gap("1.70", release="2099.2.0", released_on="2099-06-01")],
        run_id="r1",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=10,
    )
    late = store.apply(
        b.NS,
        [gap("1.80", release="2099.1.0", released_on="2099-01-15")],
        run_id="r2",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=20,
    )
    assert late["corrections"] == 0
    value = current_gap(store)
    assert (
        value["release"]["label"] == "2099.2.0"
        and value["release"]["order_basis"] == "source release date"
    )
    assert all(
        h["change"] == "initial"
        for h in store.value_history(b.NS, value["identity_key"])
    )


def test_late_older_capture_within_a_release_is_history_not_a_correction(store):
    store.apply(
        b.NS,
        [gap("1.95", source_updated_at="2099-03-01")],
        run_id="r1",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=10,
    )
    late = store.apply(
        b.NS,
        [gap("1.80", source_updated_at="2099-01-20")],
        run_id="r2",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=20,
    )
    assert late["corrections"] == 0
    assert current_gap(store)["value"] == "1.95"
    assert [
        h["change"]
        for h in store.value_history(b.NS, current_gap(store)["identity_key"])
    ] == ["initial", "late-older"]


def test_undated_releases_fall_back_to_observation_order(store):
    store.apply(
        b.NS,
        [gap("1.80", release="beta", released_on=None, basis="operator-declared")],
        run_id="r1",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=10,
    )
    store.apply(
        b.NS,
        [gap("1.70", release="alpha", released_on=None, basis="operator-declared")],
        run_id="r2",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=20,
    )
    value = current_gap(store)
    assert value["release"]["label"] == "alpha" and value["release"][
        "order_basis"
    ].startswith("observation order")
    assert "released_on" not in value["release"]  # absent, never 'None'


def test_corrected_release_date_is_a_correction_not_a_conflict(store):
    store.apply(
        b.NS,
        [gap("1.80")],
        run_id="r1",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=10,
    )
    result = store.apply(
        b.NS,
        [gap("1.80", released_on="2099-01-16")],
        run_id="r2",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=20,
    )
    assert result["release_corrections"] == 1 and result["value_versions"] == 0
    (release,) = store.releases(b.NS, "materials-project")
    assert (
        release["released_on"] == "2099-01-16"
        and release["corrections"][0]["released_on"] == "2099-01-15"
    )


def test_keys_do_not_depend_on_arrival_order_and_snapshot_changes_with_any_source():
    records = [
        gap("1.80"),
        b.entry(
            "oqmd",
            "990003",
            [b.value("band_gap", "2.1", "eV", method=b.computed("PBE"))],
            release="v9.9",
        ),
    ]
    keys = []
    for ordered in (records, list(reversed(records))):
        store = MaterialsStore(duckdb.connect(":memory:"), now=Clock())
        store.apply(
            b.NS,
            ordered,
            run_id="r",
            principal_id="p",
            scopes=b.SCOPES,
            observed_at_ms=10,
        )
        keys.append(sorted(v["series_key"] for v in store.current_values(b.NS)))
    assert keys[0] == keys[1]
    before = store.snapshot_id(b.NS)
    store.apply(
        b.NS,
        [
            b.entry(
                "oqmd",
                "990003",
                [b.value("band_gap", "2.2", "eV", method=b.computed("PBE"))],
                release="v9.9",
            )
        ],
        run_id="r2",
        principal_id="p",
        scopes=b.SCOPES,
        observed_at_ms=20,
    )
    assert (
        store.snapshot_id(b.NS) != before
        and store.snapshot_id(b.NS, as_of_ms=15) == before
    )


def test_reads_require_scope_and_report_not_ready():
    conn = duckdb.connect(":memory:")
    empty = MaterialsStore(conn, initialize=False)
    with pytest.raises(MaterialsError) as error:
        empty.require_ready()
    assert error.value.code == "not_ready"
    with pytest.raises(MaterialsError) as denied:
        MaterialsStore(conn).apply(
            b.NS, [gap("1")], run_id="r", principal_id="p", scopes={b.WRITE}
        )
    assert denied.value.code == "unauthorized"
