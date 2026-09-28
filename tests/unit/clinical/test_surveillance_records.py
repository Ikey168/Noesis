"""The surveillance record owner (I02): series, case-definition revisions, delay notes and vintages."""

from __future__ import annotations

import copy
import json

import duckdb
import pytest

from src.kb.surveillance import (
    CONTRACT,
    FORBIDDEN_KEYS,
    SurveillanceError,
    SurveillanceStore,
    forbidden_keys,
)
from tests.unit.clinical import surveillance_harness as h

NS = h.NS
SHA = "a" * 64


def item(
    values=None,
    *,
    kind="observation",
    code="Tuberkulose",
    geo="09162",
    revisions=None,
    **extra,
):
    base = {
        "condition": {"scheme": "rki-meldekategorie", "code": code, "label": code},
        "indicator": {"code": "AnzahlFall", "label": "Gemeldete Fälle"},
        "geography": {
            "system": "ags",
            "code": geo,
            "label": None,
            "code_list_version": "2099-01-01",
        },
        "unit": {"label": "cases", "published": "cases"},
        "interval": "day",
        "kind": kind,
        "dimensions": {},
        "denominator": None,
        "citations": [{"kind": "doi", "identifier": "10.5281/zenodo.9900002"}],
        "case_definition": None
        if revisions is None
        else {"key": "tuberkulose", "revisions": revisions},
        "delay_note": None,
        "values": values
        if values is not None
        else [
            {
                "reference_period": "2099-01-02",
                "reporting_date": "2099-01-05",
                "value_text": "2",
                "value": "2",
                "lower": None,
                "upper": None,
                "flags": [],
                "locator": {"row": 2},
            }
        ],
        "locator": {},
    }
    base.update(extra)
    return base


def header(published_on="2099-01-20", file_sha=SHA, **extra):
    return {
        "provider": "rki-open-data",
        "format": "rki-github-csv",
        "document": {"label": "fixture"},
        "native_revision": f"repo@{published_on}",
        "published_on": published_on,
        "published_at": None,
        "release_basis": "declared_release",
        "file_sha256": file_sha,
        "content_sha256": "",
        "item_count": 1,
        "structure": {},
        "evidence_origin": "fixture",
        "url": None,
        **extra,
    }


def value(reference, reporting, number, **extra):
    return {
        "reference_period": reference,
        "reporting_date": reporting,
        "value_text": number,
        "value": number,
        "lower": None,
        "upper": None,
        "flags": [],
        "locator": {},
        **extra,
    }


EDITION_2019 = {
    "key": "tuberkulose",
    "version": "2019",
    "valid_from": "2019-01-01",
    "valid_to": "2098-12-31",
    "text": "Klinisches Bild einer Tuberkulose (fiktiver Auszug)",
    "locator": None,
    "icd_scope": ["A15-A19"],
}
EDITION_2099 = {
    "key": "tuberkulose",
    "version": "2099",
    "valid_from": "2099-01-01",
    "valid_to": None,
    "text": "Klinisches Bild einer Tuberkulose, erweitert (fiktiver Auszug)",
    "locator": None,
    "icd_scope": ["A15-A19", "A31.0"],
}


@pytest.fixture
def store():
    return SurveillanceStore(duckdb.connect(), now=h.Clock())


def apply(store, items, **header_extra):
    head = header(**header_extra)
    head["item_count"] = len(items)
    return store.apply_release(NS, head, items, run_id="run", source_id="src")


def test_the_contract_schema_names_every_record_type_and_forbids_derived_values():
    schema = json.loads(
        (h.ROOT / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text()
    )
    assert set(schema["properties"]["record_type"]["enum"]) == {
        "release",
        "surveillance-series",
        "case-definition",
        "case-definition-revision",
        "reporting-delay-note",
        "indicator-vintage",
        "surveillance-value",
        "series-break",
    }
    assert schema["properties"]["kind"]["enum"] == [
        "observation",
        "estimate",
        "model-output",
    ]
    forbidden = {r["required"][0] for r in schema["not"]["anyOf"]}
    assert (
        forbidden <= FORBIDDEN_KEYS
        and {"nowcast", "prediction", "advice", "corrected_value"} <= forbidden
    )
    value_rule = next(
        r
        for r in schema["allOf"]
        if r["if"]["properties"]["record_type"]["const"] == "surveillance-value"
    )
    assert {"reference_period", "reporting_date", "unknown"} <= set(
        value_rule["then"]["required"]
    )


@pytest.mark.parametrize(
    "key",
    [
        "nowcast",
        "prediction",
        "health_advice",
        "corrected_value",
        "suggested_threshold",
    ],
)
def test_keys_that_would_carry_a_prediction_nowcast_advice_or_correction_are_refused(
    store, key
):
    bad = item()
    bad["values"][0][key] = "1"
    assert forbidden_keys(bad)
    with pytest.raises(SurveillanceError) as caught:
        apply(store, [bad])
    assert caught.value.code == "outside_boundary"


def test_a_series_without_a_kind_or_an_observation_with_bounds_is_refused(store):
    with pytest.raises(SurveillanceError) as caught:
        apply(store, [item(kind=None)])
    assert caught.value.code == "missing_kind"
    bounded = item(
        values=[value("2098", "2099-02-10", "6.4", lower="5.4", upper="7.5")]
    )
    with pytest.raises(SurveillanceError) as caught:
        apply(store, [bounded])
    assert caught.value.code == "kind_mismatch"
    with pytest.raises(SurveillanceError) as caught:
        apply(store, [item(values=[value(None, None, "1")])])
    assert caught.value.code == "invalid_value"


def test_estimates_and_observations_of_the_same_period_are_separate_series(store):
    observed = item(values=[value("2098", "2099-02-10", "5.6")], kind="observation")
    estimated = item(
        values=[value("2098", "2099-02-10", "6.4", lower="5.4", upper="7.5")],
        kind="estimate",
    )
    result = apply(store, [observed, estimated])
    assert result["series"] == 2
    kinds = sorted(s["kind"] for s in store.find_series(NS))
    assert kinds == ["estimate", "observation"]
    (only,) = store.find_series(NS, kind="estimate")
    assert only["kind"] == "estimate"


def test_values_carry_both_dates_and_say_which_is_unknown(store):
    apply(
        store,
        [
            item(
                values=[
                    value("2099-01-02", "2099-01-05", "2"),
                    value(None, "2099-01-12", "1"),
                    value("2098", None, "3"),
                ]
            )
        ],
    )
    (series,) = store.find_series(NS)
    answer = store.answer(NS, series["series_id"], scopes=h.READ_ONLY)
    unknown = {
        (v["reference_period"], v["reporting_date"]): v["unknown"]
        for v in answer["values"]
    }
    assert unknown == {
        ("2099-01-02", "2099-01-05"): [],
        (None, "2099-01-12"): ["reference_period"],
        ("2098", None): ["reporting_date"],
    }


def test_reingestion_is_idempotent_and_a_new_release_is_a_new_addressable_vintage(
    store,
):
    first = apply(store, [item()])
    again = apply(store, [item()])
    assert again["status"] == "unchanged" and again["release_id"] == first["release_id"]
    revised = item(values=[value("2099-01-02", "2099-01-05", "3")])
    second = apply(store, [revised], published_on="2099-02-03", file_sha="b" * 64)
    assert second["vintages"] == 1
    (series,) = store.find_series(NS)
    vintages = store.vintage_rows(NS, series["series_id"])
    assert [v["revision_of"] for v in vintages] == [None, vintages[0]["vintage_id"]]
    old = store.answer(
        NS,
        series["series_id"],
        scopes=h.READ_ONLY,
        vintage_id=vintages[0]["vintage_id"],
    )
    assert old["values"][0]["value"] == "2"
    assert (
        store.vintage(NS, vintages[0]["vintage_id"])["source_revision"]["published_on"]
        == "2099-01-20"
    )


def test_late_arriving_older_releases_land_as_history_and_a_reversion_is_a_new_vintage(
    store,
):
    apply(
        store,
        [item(values=[value("2099-01-02", "2099-01-05", "3")])],
        published_on="2099-02-03",
        file_sha="b" * 64,
    )
    apply(store, [item()], published_on="2099-01-20", file_sha="a" * 64)  # arrives late
    (series,) = store.find_series(NS)
    vintages = store.vintage_rows(NS, series["series_id"])
    assert [v["native_revision"] for v in vintages] == [
        "repo@2099-01-20",
        "repo@2099-02-03",
    ]
    assert (
        store.answer(NS, series["series_id"], scopes=h.READ_ONLY)["values"][0]["value"]
        == "3"
    )
    # A later release returns to the earlier value: a new vintage, never a reactivation of the old one.
    apply(store, [item()], published_on="2099-02-17", file_sha="c" * 64)
    vintages = store.vintage_rows(NS, series["series_id"])
    assert len(vintages) == 3 and len({v["vintage_id"] for v in vintages}) == 3
    assert vintages[2]["values_changed"] is True


def test_a_changed_release_under_the_same_clock_is_a_correction_not_a_conflict(store):
    apply(store, [item()], file_sha="a" * 64)
    apply(
        store,
        [item(values=[value("2099-01-02", "2099-01-05", "4")])],
        file_sha="d" * 64,
    )
    (series,) = store.find_series(NS)
    vintages = store.vintage_rows(NS, series["series_id"])
    assert vintages[1]["correction_of_same_release_clock"] == vintages[0]["vintage_id"]
    assert (
        store.answer(NS, series["series_id"], scopes=h.READ_ONLY)["values"][0]["value"]
        == "4"
    )


def test_case_definitions_are_first_class_and_a_change_is_a_break_that_restates_nothing(
    store,
):
    values = [
        value("2098-12-20", "2098-12-28", "3"),
        value("2099-01-02", "2099-01-05", "2"),
    ]
    apply(store, [item(values=values, revisions=[EDITION_2019, EDITION_2099])])
    (series,) = store.find_series(NS)
    answer = store.answer(NS, series["series_id"], scopes=h.READ_ONLY)
    assert [v["case_definition"]["version"] for v in answer["values"]] == [
        "2019",
        "2099",
    ]
    (brk,) = series["breaks"]
    assert (brk["kind"], brk["period"], brk["from"], brk["to"]) == (
        "case-definition",
        "2099-01-02",
        "2019",
        "2099",
    )
    assert "not restated" in brk["note"]


def test_a_corrected_declaration_of_an_edition_is_a_correction_revision_never_a_conflict(
    store,
):
    values = [value("2099-01-02", "2099-01-05", "2")]
    apply(store, [item(values=values, revisions=[EDITION_2099])])
    corrected = dict(
        EDITION_2099,
        text="Klinisches Bild einer Tuberkulose, erweitert (berichtigt, fiktiv)",
    )
    result = apply(
        store,
        [item(values=values, revisions=[corrected])],
        published_on="2099-02-03",
        file_sha="e" * 64,
    )
    assert result["status"] == "applied" and result["definition_revisions"] == 1
    (series,) = store.find_series(NS)
    history = store.definition_history(NS, series["definition_key"])
    first, second = history["revisions"]
    assert (
        second["correction_of"] == first["revision_id"]
        and second["current_for_edition"] is True
    )
    # The new vintage's value references the corrected wording; the earlier vintage keeps the first declaration.
    vintages = store.vintage_rows(NS, series["series_id"])
    early = store.answer(
        NS,
        series["series_id"],
        scopes=h.READ_ONLY,
        vintage_id=vintages[0]["vintage_id"],
    )
    late = store.answer(NS, series["series_id"], scopes=h.READ_ONLY)
    assert early["values"][0]["case_definition"]["revision_id"] == first["revision_id"]
    assert late["values"][0]["case_definition"]["revision_id"] == second["revision_id"]
    # Re-declaring the first wording afterwards is a new correction (a reversion), not a reactivation.
    apply(
        store,
        [item(values=values, revisions=[EDITION_2099])],
        published_on="2099-02-17",
        file_sha="f" * 64,
    )
    history = store.definition_history(NS, series["definition_key"])
    assert (
        len(history["revisions"]) == 3
        and history["revisions"][2]["correction_of"] == second["revision_id"]
    )


def test_reads_need_clinical_read_and_namespace_scopes(store):
    apply(store, [item()])
    (series,) = store.find_series(NS)
    with pytest.raises(SurveillanceError) as caught:
        store.answer(NS, series["series_id"], scopes={"knowledge:clinical:read"})
    assert caught.value.code == "unauthorized"
    with pytest.raises(SurveillanceError):
        store.answer("other", series["series_id"], scopes={"operator"})


def test_as_of_by_reporting_date_returns_only_values_reported_by_then(store):
    values = [
        value("2099-01-02", "2099-01-05", "2"),
        value("2099-01-02", "2099-01-25", "1"),
        value("2098", None, "7"),
    ]
    apply(store, [item(values=values)], published_on="2099-01-26")
    (series,) = store.find_series(NS)
    early = store.answer(
        NS, series["series_id"], scopes=h.READ_ONLY, reporting_as_of="2099-01-30"
    )
    assert [(v["reporting_date"], v["value"]) for v in early["values"]] == [
        ("2099-01-05", "2"),
        ("2099-01-25", "1"),
    ]
    assert [v["value"] for v in early["reporting_date_unknown"]] == ["7"]
    earlier = store.answer(
        NS, series["series_id"], scopes=h.READ_ONLY, reporting_as_of="2099-01-10"
    )
    # The vintage was released after 2099-01-10: nothing was published by then.
    assert (
        earlier["status"] == "unavailable"
        and earlier["reason"] == "historical_vintage_unavailable"
    )


def test_publisher_break_flags_and_code_list_changes_are_marked(store):
    flagged = item(
        values=[value("2098", None, "288", flags=["b: break in time series"])]
    )
    apply(store, [flagged])
    moved = copy.deepcopy(flagged)
    moved["geography"]["code_list_version"] = "2100-01-01"
    apply(store, [moved], published_on="2099-02-03", file_sha="b" * 64)
    (series,) = store.find_series(NS)
    assert {(b["kind"], b["period"], b["from"], b["to"]) for b in series["breaks"]} == {
        ("publisher-flag", "2098", None, None),
        ("geography", None, "2099-01-01", "2100-01-01"),
    }
