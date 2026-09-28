"""Regional surveillance series on Geospatial boundaries and as-of answers by reporting date (I07)."""

from __future__ import annotations

import json

import pytest

from src.kb.surveillance import SurveillanceError
from src.kb.surveillance_places import side_by_side, within
from tests.unit.clinical import surveillance_harness as h


@pytest.fixture
def env():
    loaded = h.Env()
    loaded.load_all()
    h.import_boundaries(loaded.conn)
    h.align_terms(loaded)
    h.resolve(loaded)
    return loaded


def feature(env, native):
    return env.conn.execute(
        "SELECT feature_id FROM geospatial_features WHERE native_id=?", [native]
    ).fetchone()[0]


def test_codes_resolve_by_the_published_code_with_system_version_and_boundary_revision(
    env,
):
    resolutions = {
        (r["geography_system"], r["geography_code"]): r
        for r in h.places(env).resolutions(h.NS, scopes=h.READ_ONLY)
    }
    assert {k for k, r in resolutions.items() if r["effective"] == "linked"} == {
        ("ags", "09"),
        ("ags", "11"),
        ("ags", "09162"),
        ("eu-country", "DE"),
        ("iso3166-1-alpha3", "DEU"),
        ("nuts", "DE2"),
    }
    bayern = resolutions[("ags", "09")]
    assert (
        bayern["code_list_version"] == "2099-01-01"
        and bayern["collection"] == "bkg:vg250:lan"
    )
    assert (
        bayern["boundary_vintage"]["feature_revision_id"]
        == bayern["feature_revision_id"]
    )
    assert bayern["review_state"] == "unreviewed"
    unresolved = {
        k: r["reason"] for k, r in resolutions.items() if r["effective"] == "unresolved"
    }
    assert unresolved == {
        ("ags", "09184"): "no boundary feature states this code",
        ("ecdc-aggregate", "EU_EEA31"): "the code system has no boundary collection",
    }
    # Idempotent: the same evaluation adds nothing.
    assert h.resolve(env) == {
        "matched": [],
        "unresolved": [],
        "geography_breaks": 0,
        "geo_namespace": "geo",
    }


def test_condition_within_a_boundary_lists_own_and_contained_series_side_by_side(env):
    answer = h.places(env).boundary_series(
        h.NS,
        scopes=h.READ_ONLY | {"knowledge:geospatial:read"},
        boundary_name="Bayern",
        boundary_collection="bkg:vg250:lan",
        geo_namespace="geo",
        condition="tuberculosis",
    )
    assert answer["boundary_name_resolution"]["status"] == "resolved"
    own = {(c["provider"], c["geography"]["code"]) for c in answer["series"]}
    assert own == {("destatis-health", "09"), ("eurostat-health", "DE2")}
    contained = {
        (c["provider"], c["geography"]["code"]) for c in answer["contained_units"]
    }
    assert contained == {("rki-open-data", "09162"), ("rki-open-data", "09184")}
    conflict = next(
        row for row in answer["side_by_side"] if row["reference_period"] == "2097"
    )
    assert conflict["differ"] is True and {
        v["provider"]: v["value"] for v in conflict["values"]
    } == {"destatis-health": "40", "eurostat-health": "60"}
    assert all(c["resolution"]["boundary_vintage"] for c in answer["series"])
    assert "never aggregated" in answer["note"]


def test_estimates_and_observations_for_the_same_period_stay_separate_kinds(env):
    answer = h.places(env).boundary_series(
        h.NS,
        scopes=h.READ_ONLY,
        feature_id=feature(env, "cntr.DE"),
        condition="tuberculosis",
    )
    rates = [
        c for c in answer["series"] if c["unit"]["label"] == "per 100 000 population"
    ]
    assert {(c["provider"], c["kind"]) for c in rates} == {
        ("ecdc-atlas", "observation"),
        ("who-gho", "observation"),
        ("who-gho", "estimate"),
    }
    row = next(
        r
        for r in answer["side_by_side"]
        if r["reference_period"] == "2098"
        and r["kind"] == "observation"
        and r["unit"] == "per 100 000 population"
    )
    assert {v["provider"]: v["value"] for v in row["values"]} == {
        "ecdc-atlas": "5.4",
        "who-gho": "5.6",
    }
    assert not [
        r for r in answer["side_by_side"] if r["kind"] == "estimate"
    ]  # one estimating source only


def test_as_of_selects_by_reporting_date_and_lists_unknown_reporting_dates_apart(env):
    env.upgrade_rki("2099-02-03")
    env.acquire("r2", ["rki"])
    krs = feature(env, "krs.09162")
    early = h.places(env).boundary_series(
        h.NS, scopes=h.READ_ONLY, feature_id=krs, reporting_as_of="2099-01-21"
    )
    (young,) = [
        c for c in early["series"] if c["dimensions"] == {"age_group": "A15-A34"}
    ]
    assert young["vintage"]["native_revision"].endswith("@2099-01-20")
    assert {(v["reference_period"], v["reporting_date"]) for v in young["values"]} == {
        ("2098-12-20", "2098-12-28"),
        ("2099-01-02", "2099-01-05"),
        (None, "2099-01-12"),
    }
    late = h.places(env).boundary_series(
        h.NS, scopes=h.READ_ONLY, feature_id=krs, reporting_as_of="2099-02-05"
    )
    (young,) = [
        c for c in late["series"] if c["dimensions"] == {"age_group": "A15-A34"}
    ]
    assert ("2099-01-02", "2099-01-25") in {
        (v["reference_period"], v["reporting_date"]) for v in young["values"]
    }
    country = h.places(env).boundary_series(
        h.NS,
        scopes=h.READ_ONLY,
        feature_id=feature(env, "cntr.DE"),
        reporting_as_of="2099-12-31",
    )
    eurostat = next(c for c in country["series"] if c["provider"] == "eurostat-health")
    assert (
        eurostat["values"] == [] and len(eurostat["reporting_date_unknown"]) == 2
    )  # never counted as reported


def test_receipts_record_every_parameter_and_replay(env):
    places = h.places(env)
    answer = places.boundary_series(
        h.NS,
        scopes=h.READ_ONLY | {"knowledge:geospatial:read"},
        boundary_name="Bayern",
        boundary_collection="bkg:vg250:lan",
        geo_namespace="geo",
        condition="tuberculosis",
        reporting_as_of="2099-12-31",
        kind="observation",
    )
    request = answer["receipt"]["request"]
    assert (
        request["geo_namespace"] == "geo"
        and request["kind"] == "observation"
        and request["condition"]
    )
    replayed = places.replay(
        answer["receipt"], scopes=h.READ_ONLY | {"knowledge:geospatial:read"}
    )
    assert replayed["status"] == "reproduced"
    env.eurostat_update()
    env.acquire("r2", ["eurostat"])
    assert (
        places.replay(
            answer["receipt"], scopes=h.READ_ONLY | {"knowledge:geospatial:read"}
        )["status"]
        == "changed"
    )
    with pytest.raises(SurveillanceError):
        places.replay({"request": {}}, scopes=h.READ_ONLY)


def test_a_boundary_revision_is_a_geography_break_and_rejected_resolutions_are_not_used(
    env,
):
    from src.kb.geospatial_features import GeospatialFeatureStore

    square = [[[11.5, 48.1], [11.7, 48.1], [11.7, 48.3], [11.5, 48.3], [11.5, 48.1]]]
    GeospatialFeatureStore(env.conn).import_feature_collection(
        "geo",
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "lan.09",
                        "geometry": {"type": "Polygon", "coordinates": square},
                        "properties": {"AGS": "09", "NUTS": "DE2", "GEN": "Bayern"},
                    },
                    {
                        "type": "Feature",
                        "id": "lan.11",
                        "geometry": {"type": "Polygon", "coordinates": square},
                        "properties": {"AGS": "11", "NUTS": "DE3", "GEN": "Berlin"},
                    },
                ],
            }
        ),
        provider="bkg",
        collection="bkg:vg250:lan",
        source_crs="EPSG:4326",
        title_property="GEN",
        principal_id="p",
        scopes={"knowledge:geospatial:read", "knowledge:geospatial:write"},
    )
    result = h.resolve(env)
    assert result["geography_breaks"] >= 1
    (bayern,) = env.series(provider="destatis-health", geography_code="09")
    (brk,) = [b for b in bayern["breaks"] if b["kind"] == "geography"]
    assert brk["from"] != brk["to"] and "not re-aggregated" in brk["note"]
    places = h.places(env)
    current = next(
        r
        for r in places.resolutions(h.NS, scopes=h.READ_ONLY)
        if (r["geography_system"], r["geography_code"]) == ("ags", "09")
    )
    places.review(
        h.NS,
        current["resolution_id"],
        "reject",
        "boundary under review",
        principal_id="rita",
        scopes=h.SCOPES,
    )
    answer = places.boundary_series(
        h.NS, scopes=h.READ_ONLY, feature_id=feature(env, "lan.09")
    )
    assert "destatis-health" not in {c["provider"] for c in answer["series"]}
    with pytest.raises(SurveillanceError) as caught:
        places.review(
            h.NS,
            current["resolution_id"],
            "accept",
            "ok",
            principal_id="x",
            scopes=h.READ_ONLY,
        )
    assert caught.value.code == "unauthorized"


def test_boundary_names_need_the_geospatial_read_scope_at_call_time(env):
    with pytest.raises(SurveillanceError) as caught:
        h.places(env).boundary_series(
            h.NS, scopes=h.READ_ONLY, boundary_name="Bayern", geo_namespace="geo"
        )
    assert caught.value.code == "unauthorized"
    with pytest.raises(SurveillanceError):
        h.places(env).boundary_series(h.NS, scopes=h.READ_ONLY)


def test_code_hierarchy_and_side_by_side_helpers():
    assert within(("ags", "09162"), ("ags", "09")) and not within(
        ("ags", "09"), ("ags", "09")
    )
    assert within(("nuts", "DE2"), ("eu-country", "DE")) and not within(
        ("nuts", "FR1"), ("eu-country", "DE")
    )
    assert within(("ags", "11"), ("iso3166-1-alpha3", "DEU"))
    assert side_by_side([]) == []


def test_a_boundary_with_only_contained_series_answers_by_its_own_code():
    env = h.Env()
    env.acquire("r1", ["rki"])
    h.import_boundaries(env.conn)
    h.resolve(env)
    answer = h.places(env).boundary_series(
        h.NS, scopes=h.READ_ONLY, feature_id=feature(env, "lan.09")
    )
    assert answer["series"] == []
    assert {c["geography"]["code"] for c in answer["contained_units"]} == {
        "09162",
        "09184",
    }


def test_a_receipt_request_with_other_parameters_is_refused(env):
    places = h.places(env)
    answer = places.boundary_series(
        h.NS, scopes=h.READ_ONLY, feature_id=feature(env, "cntr.DE")
    )
    tampered = dict(
        answer["receipt"], request={**answer["receipt"]["request"], "limit": 1}
    )
    with pytest.raises(SurveillanceError) as caught:
        places.replay(tampered, scopes=h.READ_ONLY)
    assert caught.value.code == "invalid_receipt"
