"""Astronomy records and their store (#2149, AS02): validation, round trips, revisions and as-of reads."""

from __future__ import annotations

import json

import duckdb
import jsonschema
import pytest

from src.kb.astronomy_records import (
    CONTRACT,
    AstronomyError,
    cutoffs,
    decimal_text,
    designation_key,
    jd_of_day,
    normalise_quantity,
    normalize_bibcode,
    normalize_circular,
    normalize_designation,
    normalize_doi,
    parse_reference,
    register_schemas,
    schema,
    validate_record,
)
from src.kb.astronomy_store import AstronomyStore, feature_enabled

NS = "astronomy"
DAY = 86_400_000


def ms(day: str) -> int:
    from src.kb.astronomy_records import day_ms

    return day_ms(day) + 12 * 3_600_000


def orbit(solution_id: str, computed_at: str, a: str = "2.7") -> dict:
    return {
        "kind": "orbit_solution",
        "source": {
            "provider": "jpl-sbdb",
            "source_record_id": f"2026 AB12|{solution_id}",
            "url": "https://ssd-api.jpl.nasa.gov/sbdb.api?sstr=2026%20AB12",
        },
        "object_designation": "2026 AB12",
        "publisher": "JPL",
        "solution_id": solution_id,
        "epoch": {"jd": "2461000.5"},
        "elements": {"a": {"value": a, "unit": "au"}, "e": {"value": "0.12"}},
        "computed_at": computed_at,
        "n_obs_used": 40,
    }


def status(disposition: str, native: str, asserted_at: str) -> dict:
    return {
        "kind": "exoplanet_status_assertion",
        "source": {
            "provider": "nasa-exoplanet-archive",
            "source_record_id": "toi|9001.01",
        },
        "object_name": "9001.01",
        "object_scheme": "toi",
        "source_table": "toi",
        "native_disposition": native,
        "disposition": disposition,
        "asserted_at": asserted_at,
    }


def test_designations_pack_and_unpack_deterministically():
    assert normalize_designation("2026 AB12") == {
        "unpacked": "2026 AB12",
        "packed": "K26A12B",
        "kind": "provisional",
    }
    assert normalize_designation("K26A12B")["unpacked"] == "2026 AB12"
    assert normalize_designation("1999 XA123")["packed"] == "J99XC3A"
    assert normalize_designation("J99XC3A")["unpacked"] == "1999 XA123"
    assert normalize_designation("2026 CD")["packed"] == "K26C00D"
    for number, packed in (
        (4179, "04179"),
        (123456, "C3456"),
        (620000, "~0000"),
        (700001, "~0KoL"),
    ):
        assert normalize_designation(str(number))["packed"] == packed
        assert normalize_designation(packed)["unpacked"] == f"({number})"
    assert normalize_designation("2040 P-L")["packed"] == "PLS2040"
    assert normalize_designation("PLS2040")["unpacked"] == "2040 P-L"
    assert designation_key("K26A12B") == designation_key("2026 ab12") == "2026 AB12"
    assert designation_key("(700001)") == designation_key("~0KoL") == "(700001)"
    assert designation_key("Fictaria") == "name:fictaria"
    assert normalize_designation("") is None


def test_exact_epochs_decimals_citations_and_units():
    assert jd_of_day("2000-01-01") == "2451544.5"
    assert (
        decimal_text("0.1200") == "0.12"
        and decimal_text("1.5D-3") == "0.0015"
        and decimal_text("x") is None
    )
    assert normalize_bibcode("2026AJ....171...12S") == "2026AJ....171...12S"
    assert normalize_bibcode("not a bibcode") is None
    assert (
        normalize_doi("https://doi.org/10.5555/Astro.Fict.1") == "10.5555/astro.fict.1"
    )
    ref = parse_reference(
        "<a refstr=FICT href=https://ui.adsabs.harvard.edu/abs/2026AJ....171...12S/abstract "
        "target=ref>Fict et al. 2026</a>"
    )
    assert (
        ref["bibcode"] == "2026AJ....171...12S"
        and ref["label"] == "Fict et al. 2026"
        and "doi" not in ref
    )
    assert parse_reference("") is None
    assert (
        normalize_circular("E2026-C99") == "MPEC 2026-C99"
        and normalize_circular("MPO 123456") == "MPO 123456"
    )
    quantity = normalise_quantity("1.5", "au")
    assert quantity["value"] == "1.5" and quantity["unit"] == "au"
    # Pint is optional (not installed in the CI lane): the native value always stands.
    assert "normalised" in quantity or quantity["normalisation"] == "unavailable"
    assert normalise_quantity("3", "M_earth")["normalisation"] == "no_definition"
    assert (
        cutoffs("2026-03-01")["published_by_ms"]
        == ms("2026-03-01") + 12 * 3_600_000 - 1
    )


def test_normalised_units_when_pint_is_installed():
    pytest.importorskip("pint")
    assert normalise_quantity("1", "au")["normalised"] == {
        "value": "149597870.700000",
        "unit": "kilometer",
        "via": "src.integrations.units",
    }
    assert normalise_quantity("1", "R_jupiter")["normalised"]["value"] == "71492.000"


def test_validation_refuses_what_no_publisher_states():
    record = validate_record(orbit("12", "2026-02-01"))
    assert record["contract"] == CONTRACT and record["unknowns"] == [
        "arc",
        "source.published_at",
        "uncertainty",
    ]
    with pytest.raises(AstronomyError, match="undeclared"):
        validate_record({**orbit("12", "2026-02-01"), "impact_probability": "1e-6"})
    with pytest.raises(AstronomyError, match="declared publisher"):
        validate_record(
            {
                **orbit("12", "2026-02-01"),
                "source": {"provider": "noesis", "source_record_id": "x"},
            }
        )
    with pytest.raises(AstronomyError, match="does not publish"):
        validate_record(
            {
                **orbit("12", "2026-02-01"),
                "source": {"provider": "noaa-swpc", "source_record_id": "x"},
            }
        )
    with pytest.raises(AstronomyError, match="decimal text"):
        validate_record(
            {
                **orbit("12", "2026-02-01"),
                "elements": {"a": {"value": 2.7, "unit": "au"}},
            }
        )
    with pytest.raises(AstronomyError, match="exact Julian"):
        validate_record({**orbit("12", "2026-02-01"), "epoch": {"jd": 2461000.5}})
    with pytest.raises(AstronomyError, match="disposition"):
        validate_record(status("validated_by_noesis", "PC", "2026-02-01"))
    with pytest.raises(AstronomyError, match="composite"):
        validate_record(
            {
                "kind": "exoplanet",
                "source": {
                    "provider": "nasa-exoplanet-archive",
                    "source_record_id": "ps|x|y",
                },
                "name": "Fict-1 b",
                "source_table": "ps",
                "composite": True,
            }
        )
    # Absent is absent: None and "None" never reach a record.
    cleaned = validate_record(
        {**orbit("12", "2026-02-01"), "computer": None, "reference": "None"}
    )
    assert (
        "computer" not in cleaned
        and "reference" not in cleaned
        and "None" not in json.dumps(cleaned)
    )


def test_every_kind_round_trips_through_the_json_schema_and_the_store():
    conn = duckdb.connect(":memory:")
    store = AstronomyStore(conn, now=lambda: ms("2026-06-01"))
    records = [
        orbit("12", "2026-02-01"),
        status("candidate", "PC", "2026-02-01"),
        {
            "kind": "space_weather_product",
            "source": {"provider": "noaa-swpc", "source_record_id": "K05W|9001"},
            "product_id": "K05W",
            "serial": "9001",
            "issue_time": "2026-05-01T12:00:00Z",
            "product_kind": "warning",
            "message": "Space Weather Message Code: WARK05\r\nSerial Number: 9001",
            "scales": ["G1"],
        },
        {
            "kind": "orbital_object",
            "source": {"provider": "celestrak-satcat", "source_record_id": "99901"},
            "cospar": "2026-900A",
            "norad": "99901",
            "name": "FICTSAT 1",
            "status": "D",
            "decay_date": "2026-05-20",
        },
    ]
    store.apply(NS, records, run_id="r1", observed_at_ms=ms("2026-06-01"))
    validator = jsonschema.Draft7Validator(schema())
    views = store.visible(NS)["records"]
    assert len(views) == 4
    for view in views:
        assert not list(validator.iter_errors(view["record"])), view["kind"]
        assert validate_record(view["record"]) == view["record"]
    registered = register_schemas(
        conn, principal_id="svc", scopes={"knowledge:schema:register"}
    )
    assert registered[0]["name"] == "astronomy-record"


def test_unchanged_is_idempotent_superseded_appends_and_late_older_is_history():
    conn = duckdb.connect(":memory:")
    store = AstronomyStore(conn)
    first = store.apply(
        NS,
        [status("candidate", "PC", "2026-02-01")],
        run_id="r1",
        observed_at_ms=ms("2026-02-02"),
    )
    assert first["inserted"] == 1
    again = store.apply(
        NS,
        [status("candidate", "PC", "2026-02-01")],
        run_id="r2",
        observed_at_ms=ms("2026-02-03"),
    )
    assert again["unchanged"] == 1 and again["changed"] == []
    moved = store.apply(
        NS,
        [status("false_positive", "FP", "2026-05-01")],
        run_id="r3",
        observed_at_ms=ms("2026-05-02"),
    )
    assert moved["revised"] == 1
    # A late copy of the older disposition is already on record: no new revision, no false correction.
    late = store.apply(
        NS,
        [status("candidate", "PC", "2026-02-01")],
        run_id="r4",
        observed_at_ms=ms("2026-05-03"),
    )
    assert late["unchanged"] == 1 and late["changed"] == []
    # A late, older state never seen before is history, never current.
    older = store.apply(
        NS,
        [status("candidate", "APC", "2026-01-15")],
        run_id="r5",
        observed_at_ms=ms("2026-05-04"),
    )
    assert older["history"] == 1 and older["changed"] == []
    current = store.visible(NS)["records"][0]
    assert current["record"]["disposition"] == "false_positive"
    rid = current["record_id"]
    assert [r["change"] for r in store.history(NS, rid)] == [
        "new",
        "revised",
        "history",
    ]
    # A reversion (the source states the earlier value again with a newer date) is a new revision.
    back = store.apply(
        NS,
        [status("candidate", "PC", "2026-06-01")],
        run_id="r6",
        observed_at_ms=ms("2026-06-02"),
    )
    assert back["revised"] == 1


def test_as_of_reads_follow_the_source_date_and_the_acquisition_cutoff():
    conn = duckdb.connect(":memory:")
    store = AstronomyStore(conn)
    store.apply(
        NS,
        [status("candidate", "PC", "2026-02-01")],
        run_id="r1",
        observed_at_ms=ms("2026-02-02"),
    )
    store.apply(
        NS,
        [status("false_positive", "FP", "2026-05-01")],
        run_id="r2",
        observed_at_ms=ms("2026-05-02"),
    )

    def disposition(as_of, acquired_by=None):
        cut = cutoffs(as_of, acquired_by)
        result = store.visible(
            NS, public_cutoff_ms=cut["published_by_ms"], acquired_by_ms=acquired_by
        )
        return (
            [v["record"]["disposition"] for v in result["records"]],
            len(result["pending"]),
        )

    assert disposition("2026-03-01") == (["candidate"], 0)
    assert disposition("2026-05-01") == (["false_positive"], 0)
    assert disposition("2026-01-01") == ([], 1)  # not yet published, not absent
    # Only what was acquired by the cutoff exists for the read.
    assert disposition("2026-06-01", acquired_by=ms("2026-03-01")) == (["candidate"], 0)
    later = store.visible(
        NS, public_cutoff_ms=cutoffs("2026-03-01")["published_by_ms"]
    )["records"][0]["later"]
    assert len(later) == 1


def test_not_ready_before_any_source_ran_and_features_default_off():
    conn = duckdb.connect(":memory:")
    store = AstronomyStore(conn, initialize=False)
    with pytest.raises(AstronomyError) as error:
        store.visible(NS)
    assert error.value.code == "not_ready"
    assert store.generation(NS) == "astronomy-generation:empty"
    assert feature_enabled(conn, "astronomy-launches") is False
    with pytest.raises(AstronomyError):
        feature_enabled(conn, "astronomy_launches")


def test_generation_changes_when_any_record_changes():
    conn = duckdb.connect(":memory:")
    store = AstronomyStore(conn)
    store.apply(
        NS, [orbit("12", "2026-02-01")], run_id="r1", observed_at_ms=ms("2026-02-02")
    )
    before = store.generation(NS)
    store.apply(
        NS, [orbit("12", "2026-02-01")], run_id="r2", observed_at_ms=ms("2026-02-03")
    )
    assert store.generation(NS) == before
    store.apply(
        NS,
        [orbit("13", "2026-03-01", a="2.71")],
        run_id="r3",
        observed_at_ms=ms("2026-03-02"),
    )
    assert store.generation(NS) != before
