"""Regional series on Geospatial boundaries and as-of answers with pins (#1978)."""

from __future__ import annotations

import json

import pytest

from src.kb.demographics import DemographicError
from src.kb.demographics_places import DemographicPlaces, related
from tests.unit import demographics_harness as h

GEO = {"knowledge:geospatial:read", "knowledge:geospatial:write"}
SQUARE = [[[13.3, 52.5], [13.4, 52.5], [13.4, 52.6], [13.3, 52.6], [13.3, 52.5]]]
LAND = {"nuts": {"collection": "bkg:vg250:lan", "property": "NUTS"}}
MARCH_16 = 4_077_302_400_000  # 2099-03-16T00:00:00Z


def import_boundaries(conn) -> dict[str, str]:
    """Fictional boundaries: two Berlin districts (Pankow is missing), the Land and the member state."""
    from src.kb.geospatial_features import GeospatialFeatureStore

    store = GeospatialFeatureStore(conn)

    def collection(name, features, provider, title):
        store.import_feature_collection(
            "geo",
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "id": fid,
                            "geometry": {"type": "Polygon", "coordinates": SQUARE},
                            "properties": props,
                        }
                        for fid, props in features
                    ],
                }
            ),
            provider=provider,
            collection=name,
            source_crs="EPSG:4326",
            title_property=title,
            principal_id="p",
            scopes=GEO,
        )

    collection(
        "alkis_bezirke:bezirksgrenzen",
        [
            ("bezirksgrenzen.11000001", {"gem": "001", "namgem": "Mitte"}),
            (
                "bezirksgrenzen.11000002",
                {"gem": "002", "namgem": "Friedrichshain-Kreuzberg"},
            ),
        ],
        "gdi-berlin",
        "namgem",
    )
    collection(
        "bkg:vg250:lan",
        [("lan.11", {"AGS": "11", "NUTS": "DE3", "GEN": "Berlin"})],
        "bkg",
        "GEN",
    )
    collection(
        "gisco:countries",
        [
            (
                "cntr.DE",
                {
                    "CNTR_ID": "DE",
                    "ISO3_CODE": "DEU",
                    "NAME": "Germany (fictional geometry)",
                },
            )
        ],
        "gisco",
        "NAME",
    )
    return {
        row[1]: row[0]
        for row in conn.execute(
            "SELECT feature_id, native_id FROM geospatial_features"
        ).fetchall()
    }


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    h.load_bamf(conn)
    features = import_boundaries(conn)
    places = DemographicPlaces(conn)
    places.resolve_geographies(
        h.NS, principal_id="op", scopes=h.SCOPES, geo_namespace="geo", collections=LAND
    )
    yield conn, places, features
    conn.close()


def test_codes_resolve_by_the_published_code_and_unknown_codes_stay_unresolved(env):
    conn, places, _ = env
    by_code = {
        r["geography_code"]: r for r in places.resolutions(h.NS, scopes=h.READ_ONLY)
    }
    assert by_code["001"]["effective"] == "linked"
    assert (
        by_code["001"]["boundary_vintage"]["collection"]
        == "alkis_bezirke:bezirksgrenzen"
    )
    assert (
        by_code["001"]["code_list_version"] == "Berliner Bezirke (Gebietsreform 2001)"
    )
    assert by_code["003"]["effective"] == "unresolved"
    assert by_code["003"]["reason"] == "no boundary feature states this code"
    assert by_code["XA01"]["effective"] == "unresolved"
    assert by_code["11"]["effective"] == by_code["DE3"]["effective"] == "linked"
    before = conn.execute(
        "SELECT count(*) FROM demographic_geo_resolutions"
    ).fetchone()[0]
    places.resolve_geographies(
        h.NS, principal_id="op", scopes=h.SCOPES, geo_namespace="geo", collections=LAND
    )
    assert (
        conn.execute("SELECT count(*) FROM demographic_geo_resolutions").fetchone()[0]
        == before
    )
    with pytest.raises(DemographicError):
        places.resolve_geographies(
            h.NS, principal_id="op", scopes=h.SCOPES - {"knowledge:geospatial:read"}
        )


def test_a_district_query_returns_matching_series_and_lists_other_levels(env):
    _, places, features = env
    answer = places.boundary_series(
        h.NS, features["bezirksgrenzen.11000001"], scopes=h.READ_ONLY
    )
    assert {c["indicator"] for c in answer["series"]} == {
        "residents",
        "foreign_residents",
    }
    assert {c["geography_level"]["level"] for c in answer["series"]} == {"bezirk"}
    others = {
        (o["provider"], o["geography_level"]["level"]) for o in answer["other_levels"]
    }
    assert (
        ("destatis", "land") in others
        and ("eurostat", "nuts1") in others
        and ("eurostat", "country") in others
    )
    assert all(
        o["availability"] == "available_at_different_level"
        for o in answer["other_levels"]
    )
    assert not any("observations" in o for o in answer["other_levels"])
    assert all(
        c["resolution"]["boundary_vintage"]["feature_revision_id"]
        for c in answer["series"]
    )


def test_a_land_and_a_member_state_return_each_publishers_series_side_by_side(env):
    _, places, features = env
    land = places.boundary_series(
        h.NS, features["lan.11"], scopes=h.READ_ONLY, concept="population_stock"
    )
    assert {
        (c["provider"], c["geography_level"]["scheme"]) for c in land["series"]
    } == {
        ("destatis", "ags"),
        ("eurostat", "nuts"),
    }
    assert land["pairs"] and all(
        p["comparability"] == "comparability_unknown" for p in land["pairs"]
    )
    country = places.boundary_series(h.NS, features["cntr.DE"], scopes=h.READ_ONLY)
    assert {c["provider"] for c in country["series"]} == {"eurostat", "unhcr", "bamf"}


def test_as_of_selects_the_vintage_released_by_then_and_pins_turn_stale(env):
    conn, places, features = env
    early = places.boundary_series(
        h.NS,
        features["cntr.DE"],
        scopes=h.READ_ONLY,
        as_of_ms=MARCH_16,
        concept="population_stock",
    )
    (pjan,) = [c for c in early["series"] if c["series_code"] == "demo_pjan"]
    assert (
        pjan["status"] == "available"
        and pjan["observations"][-1]["value"] == "90250300"
    )
    before = places.boundary_series(
        h.NS,
        features["cntr.DE"],
        scopes=h.READ_ONLY,
        as_of_ms=1,
        concept="population_stock",
    )
    assert {c["reason"] for c in before["series"]} == {"historical_vintage_unavailable"}
    pinned = places.pin(h.NS, early["receipt"], principal_id="alice", scopes=h.SCOPES)
    assert {p["status"] for p in pinned["pins"]} == {"current"}
    assert places.replay(early["receipt"], scopes=h.READ_ONLY)["status"] == "reproduced"
    h.apply(conn, "eurostat", 0, h.PJAN_SEPTEMBER)
    pins = places.pins(
        h.NS, scopes=h.READ_ONLY, receipt_digest=early["receipt"]["digest"]
    )["pins"]
    (pin,) = [p for p in pins if p["series_id"] == pjan["series_id"]]
    assert pin["status"] == "stale" and len(pin["newer_vintage_ids"]) == 1
    # The pinned values are the pinned vintage's own; the later revision never replaces them.
    assert pin["observations"][-1]["value"] == "90250300"
    # The same as-of date still selects the earlier vintage; a later date selects the revision.
    assert places.replay(early["receipt"], scopes=h.READ_ONLY)["status"] == "reproduced"
    later = places.boundary_series(
        h.NS, features["cntr.DE"], scopes=h.READ_ONLY, concept="population_stock"
    )
    (pjan_now,) = [c for c in later["series"] if c["series_code"] == "demo_pjan"]
    assert pjan_now["observations"][-1]["value"] == "90260400"
    tampered = {**early["receipt"], "selected": []}
    with pytest.raises(DemographicError):
        places.pin(h.NS, tampered, principal_id="alice", scopes=h.SCOPES)


def test_a_rejected_resolution_is_not_used_and_review_needs_the_review_scope(env):
    _, places, features = env
    mitte = next(
        r
        for r in places.resolutions(h.NS, scopes=h.READ_ONLY)
        if r["geography_code"] == "001"
    )
    with pytest.raises(DemographicError):
        places.review(
            h.NS,
            mitte["resolution_id"],
            "reject",
            "wrong",
            principal_id="a",
            scopes=h.SCOPES,
        )
    rejected = places.review(
        h.NS,
        mitte["resolution_id"],
        "reject",
        "boundary vintage mismatch",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    assert rejected["effective"] == "rejected"
    answer = places.boundary_series(
        h.NS, features["bezirksgrenzen.11000001"], scopes=h.READ_ONLY
    )
    assert answer["series"] == []
    accepted = places.review(
        h.NS,
        mitte["resolution_id"],
        "accept",
        "checked",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    assert accepted["effective"] == "linked" and accepted["review"]["revision"] == 2
    pankow = next(
        r
        for r in places.resolutions(h.NS, scopes=h.READ_ONLY)
        if r["geography_code"] == "003"
    )
    with pytest.raises(DemographicError):
        places.review(
            h.NS,
            pankow["resolution_id"],
            "accept",
            "x",
            principal_id="rev",
            scopes=h.REVIEW_SCOPES,
        )


def test_code_list_hierarchies():
    assert related(("berlin-bezirk", "001"), ("ags", "11"))
    assert related(("nuts", "DE300"), ("eu-country", "DE"))
    assert not related(("ags", "09"), ("berlin-bezirk", "001"))
    assert not related(("nuts", "DE3"), ("nuts", "DE2"))


def test_a_pin_must_name_each_vintage_with_its_own_series_and_a_bad_row_never_breaks_reads(
    env,
):
    from src.kb.demographics import digest
    from src.kb.demographics_monitoring import DemographicMonitor
    from src.kb.subscriptions import SubscriptionStore

    conn, places, features = env
    answer = places.boundary_series(h.NS, features["cntr.DE"], scopes=h.READ_ONLY)
    receipt = json.loads(json.dumps(answer["receipt"]))
    assert len([i for i in receipt["selected"] if i["vintage_id"]]) >= 2
    first, second = receipt["selected"][0], receipt["selected"][1]
    first["vintage_id"], second["vintage_id"] = (
        second["vintage_id"],
        first["vintage_id"],
    )
    # A caller can recompute the plain digest; the pairs are still checked against the store.
    receipt["digest"] = digest({k: v for k, v in receipt.items() if k != "digest"})
    with pytest.raises(DemographicError) as refused:
        places.pin(h.NS, receipt, principal_id="mallory", scopes=h.SCOPES)
    assert refused.value.code == "invalid_receipt"
    assert conn.execute("SELECT count(*) FROM demographic_pins").fetchone()[0] == 0
    # A mismatched row that is already stored is reported as invalid, and monitors keep running.
    conn.execute(
        "INSERT INTO demographic_pins VALUES (?,?,?,?,?,?,?,?)",
        [
            h.NS,
            "dm-pin:bad",
            "digest",
            first["series_id"],
            first["vintage_id"],
            "{}",
            "x",
            1,
        ],
    )
    (bad,) = places.pins(h.NS, scopes=h.READ_ONLY)["pins"]
    assert bad["status"] == "invalid" and bad["observations"] == []
    monitor = DemographicMonitor(conn)
    watch = monitor.create(
        h.NS,
        "all-pjan",
        series_filter={"series_code": "demo_pjan"},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    result = monitor.run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert result["stale_pins"] == []


def test_cod_ab_pcodes_take_their_country_from_the_alpha2_prefix():
    from src.kb.demographics_places import ancestry

    assert ("C", "UA") in ancestry("cod-ab-pcode", "UA80")[1]
    assert ("C", "SY") in ancestry("cod-ab-pcode", "SY01")[1]
    assert related(("cod-ab-pcode", "XA01"), ("eu-country", "XA"))
    assert related(("cod-ab-pcode", "XA0101"), ("cod-ab-pcode", "XA01"))
    assert not related(("cod-ab-pcode", "XA01"), ("cod-ab-pcode", "XB01"))
    # A source prefixing a known alpha-3 code is still read through the alpha-3 table.
    assert ("C", "DE") in ancestry("cod-ab-pcode", "DEU01")[1]
