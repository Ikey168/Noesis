"""Normalised codes, places and currencies beside the reported values, per publisher (#2028)."""

from __future__ import annotations

import pytest

from src.kb.development_finance import DevelopmentFinanceError, DevelopmentFinanceStore
from src.kb.development_finance_normalise import DevelopmentFinanceNormaliser
from tests.unit.funding import development_finance_harness as h

WATER = "XM-DAC-99901-FICT-0001"
EDUCATION = "XM-DAC-99901-FICT-0002"


@pytest.fixture()
def env():
    env = h.Env().load()
    yield env
    env.conn.close()


def _current(env, publisher, identifier):
    return DevelopmentFinanceStore(env.conn).current(h.NS)[
        h.activity_key(env.conn, publisher, identifier)
    ]


def test_codes_are_read_from_published_ontology_modules_and_unknown_codes_stay_as_reported(
    env,
):
    normaliser = DevelopmentFinanceNormaliser(env.conn, now=env.now)
    before = normaliser.normalise(
        h.NS, _current(env, h.FDPA, WATER)["revision_id"], scopes=h.SCOPES
    )
    assert (
        before["sectors"][0]["state"] == "codelist-unavailable"
    )  # nothing is guessed without the module
    published = normaliser.publish_codelists(principal_id="operator", scopes=h.SCOPES)
    assert published["codelists"]["iati-transaction-type"]["version"] == "2.3.0"
    assert published["codelists"]["dac-purpose-code"]["release"] == "2024"
    again = normaliser.publish_codelists(principal_id="operator", scopes=h.SCOPES)
    assert (
        again["codelists"]["dac-purpose-code"]["module_id"]
        == published["codelists"]["dac-purpose-code"]["module_id"]
    )
    water = normaliser.normalise(
        h.NS, _current(env, h.FDPA, WATER)["revision_id"], scopes=h.SCOPES
    )
    assert (
        water["sectors"][0]["label"]
        == "Basic drinking water supply and basic sanitation"
    )
    assert water["sectors"][0]["category"]["label"] == "Water Supply & Sanitation"
    assert water["transactions"][0]["type"]["label"] == "Outgoing Commitment"
    assert water["participating_orgs"][1]["role"]["label"] == "Implementing"
    region = water["recipient_regions"][0]
    assert (
        region["label"] == "South of Sahara, regional" and region["kind"] == "aggregate"
    )
    education = normaliser.normalise(
        h.NS, _current(env, h.FDPA, EDUCATION)["revision_id"], scopes=h.SCOPES
    )
    unknown = education["sectors"][1]
    assert unknown == {
        "reported": "99999",
        "list": "dac-purpose-code",
        "version": "2024.0.0",
        "state": "unknown-code",
        "flag": "unknown-code",
        "percentage": "30",
        "percentage_text": "30",
    }


def test_allocations_that_do_not_sum_to_100_are_kept_and_flagged(env):
    normaliser = DevelopmentFinanceNormaliser(env.conn, now=env.now)
    education = normaliser.normalise(
        h.NS, _current(env, h.FDPA, EDUCATION)["revision_id"], scopes=h.SCOPES
    )
    (sector,) = [c for c in education["checks"] if c["allocation"] == "sector"]
    assert sector["sum"] == "90" and sector["flag"] == "allocation-sum-not-100"
    water = normaliser.normalise(
        h.NS, _current(env, h.FDPA, WATER)["revision_id"], scopes=h.SCOPES
    )
    assert {c["state"] for c in water["checks"]} == {
        "sums-to-100"
    }  # country 60 + region 40
    # The reported percentages are untouched.
    stored = DevelopmentFinanceStore(env.conn).revision(
        h.NS, _current(env, h.FDPA, EDUCATION)["revision_id"]
    )
    assert [s["percentage"] for s in stored["activity"]["sectors"]] == ["60", "30"]


def test_amounts_without_a_value_date_or_currency_stay_unknown(env):
    normaliser = DevelopmentFinanceNormaliser(env.conn, now=env.now)
    water = normaliser.normalise(
        h.NS, _current(env, h.FDPA, WATER)["revision_id"], scopes=h.SCOPES
    )
    assert water["transactions"][2]["amount"]["flags"] == ["value-date-unknown"]
    education = normaliser.normalise(
        h.NS, _current(env, h.FDPA, EDUCATION)["revision_id"], scopes=h.SCOPES
    )
    assert education["transactions"][1]["amount"]["flags"] == ["currency-unknown"]
    undated = _current(env, h.FDPA, WATER)["transactions"][2]["transaction_id"]
    with pytest.raises(DevelopmentFinanceError) as refused:
        normaliser.convert(
            h.NS,
            undated,
            target_currency="USD",
            rate="1.1",
            rate_date="2098-02-25",
            rate_source="Fictional central bank reference rate",
            citation="fixture",
            principal_id="analyst",
            scopes=h.SCOPES,
        )
    assert refused.value.code == "unconvertible"


def test_a_conversion_needs_a_cited_rate_and_is_stored_beside_the_original(env):
    normaliser = DevelopmentFinanceNormaliser(env.conn, now=env.now)
    commitment = _current(env, h.FDPA, WATER)["transactions"][0]
    with pytest.raises(DevelopmentFinanceError) as uncited:
        normaliser.convert(
            h.NS,
            commitment["transaction_id"],
            target_currency="USD",
            rate="1.1",
            rate_date="2098-02-01",
            rate_source="",
            citation="",
            principal_id="analyst",
            scopes=h.SCOPES,
        )
    assert uncited.value.code == "uncited_rate"
    converted = normaliser.convert(
        h.NS,
        commitment["transaction_id"],
        target_currency="USD",
        rate="1.0825",
        rate_date="2098-02-01",
        rate_source="Fictional reference rate (authored fixture)",
        citation="https://example.org/fictional-rate",
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    assert converted["converted"] == {"value": "1082500.0000", "currency": "USD"}
    assert converted["original"] == {
        "value": "1000000",
        "currency": "EUR",
        "value_date": "2098-02-01",
    }
    assert converted["rate"]["date"] == "2098-02-01" and converted["flags"] == []
    again = normaliser.convert(
        h.NS,
        commitment["transaction_id"],
        target_currency="USD",
        rate="1.0825",
        rate_date="2098-02-01",
        rate_source="Fictional reference rate (authored fixture)",
        citation="https://example.org/fictional-rate",
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    assert again["conversion_id"] == converted["conversion_id"]
    other_date = normaliser.convert(
        h.NS,
        commitment["transaction_id"],
        target_currency="USD",
        rate="1.09",
        rate_date="2098-03-01",
        rate_source="Fictional reference rate (authored fixture)",
        citation="https://example.org/fictional-rate",
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    assert other_date["flags"] == ["rate-date-differs-from-value-date"]
    assert _current(env, h.FDPA, WATER)["transactions"][0]["value"] == "1000000"


def test_two_publishers_yield_two_normalised_records(env):
    normaliser = DevelopmentFinanceNormaliser(env.conn, now=env.now)
    records = normaliser.normalise_current(h.NS, scopes=h.SCOPES)
    water = [r for r in records if r["activity_key"].endswith(WATER)]
    assert {r["publisher_id"] for r in water} == {
        "iati:ref:XM-DAC-99901",
        "iati:ref:XI-IATI-FICTNGO",
    }
    assert len({r["normalisation_id"] for r in water}) == 2


def test_places_resolve_by_code_regions_stay_aggregates_and_locations_wait_for_review(
    env,
):
    h.register_places(env.conn, now=env.now())
    normaliser = DevelopmentFinanceNormaliser(env.conn, now=env.now)
    result = normaliser.resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    by = {
        (link["reference_kind"], link["code"] or link["mention"]): link
        for link in result["links"]
    }
    assert by[("recipient-country", "KE")]["state"] == "resolved"
    assert by[("recipient-country", "UG")]["state"] == "resolved"
    assert (
        by[("recipient-region", "289")]["state"] == "aggregate"
        and by[("recipient-region", "289")]["resolution_id"] is None
    )
    location = by[("location", "Fictional Lakeside Settlement")]
    assert location["state"] == "pending-review"
    saved = env.conn.execute(
        "SELECT status FROM geocode_resolutions WHERE resolution_id=?",
        [by[("recipient-country", "KE")]["resolution_id"]],
    ).fetchone()
    assert saved == ("resolved",)
    again = normaliser.resolve_places(h.NS, principal_id="analyst", scopes=h.SCOPES)
    assert {link["link_id"] for link in again["links"]} == {
        link["link_id"] for link in result["links"]
    }
    links = {
        (link["reference_kind"], link["code"] or link["mention"]): link
        for link in normaliser.place_links(h.NS, scopes=h.SCOPES)
    }
    assert (
        links[("location", "Fictional Lakeside Settlement")]["state"]
        == "pending-review"
    )
    with pytest.raises(DevelopmentFinanceError):
        normaliser.review_place(
            h.NS,
            "geocode-resolution:unknown",
            "reject",
            selected_place_id=None,
            reason="not ours",
            principal_id="reviewer",
            scopes=h.REVIEW_SCOPES,
        )
    normaliser.review_place(
        h.NS,
        location["resolution_id"],
        "reject",
        selected_place_id=None,
        reason="free text names no registered place",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    links = {
        (link["reference_kind"], link["code"] or link["mention"]): link
        for link in normaliser.place_links(h.NS, scopes=h.SCOPES)
    }
    assert links[("location", "Fictional Lakeside Settlement")]["state"] == "rejected"


def test_a_code_carried_by_two_places_stays_ambiguous(env):
    h.register_places(env.conn, now=env.now())
    from src.kb.geospatial import GeospatialStore

    GeospatialStore(env.conn, now=env.now).register_place(
        "global",
        "Kenya (duplicate)",
        "country",
        names=[{"value": "Kenya (duplicate)", "language": "en", "kind": "canonical"}],
        source_ids={"iso3166-1-alpha2": "KE"},
        parent_ids=[],
        principal_id="system",
        scopes={"knowledge:geospatial:write"},
        place_key="fixture:KE:duplicate",
    )
    result = DevelopmentFinanceNormaliser(env.conn, now=env.now).resolve_places(
        h.NS, principal_id="analyst", scopes=h.SCOPES
    )
    (kenya,) = [link for link in result["links"] if link["code"] == "KE"]
    assert kenya["state"] == "ambiguous"


def test_sector_percentages_are_checked_per_vocabulary_and_recipients_together(env):
    """DAC and SDG sector lists are separate 100% allocations; countries and regions are one (review)."""
    normaliser = DevelopmentFinanceNormaliser(env.conn, now=env.now)
    activity = {
        "sectors": [
            {
                "code": "14030",
                "vocabulary": "1",
                "percentage_text": "100",
                "percentage": "100",
            },
            {
                "code": "6",
                "vocabulary": "7",
                "percentage_text": "60",
                "percentage": "60",
            },
            {
                "code": "6.1",
                "vocabulary": "8",
                "percentage_text": "100",
                "percentage": "100",
            },
            {
                "code": "1",
                "vocabulary": "7",
                "percentage_text": "40",
                "percentage": "40",
            },
        ],
        "recipient_countries": [
            {"code": "KE", "percentage_text": "60", "percentage": "60"}
        ],
        "recipient_regions": [
            {
                "code": "289",
                "vocabulary": "1",
                "percentage_text": "40",
                "percentage": "40",
            }
        ],
    }
    checks = normaliser._allocation_checks(activity)
    assert {
        (c["allocation"], tuple(c["group"].values())): c["state"] for c in checks
    } == {
        ("sector", ("1",)): "sums-to-100",
        ("sector", ("7",)): "sums-to-100",
        ("sector", ("8",)): "sums-to-100",
        ("recipient", ("1",)): "sums-to-100",
    }
    # A single sector without a percentage is 100% by convention; several without percentages are flagged.
    single = normaliser._allocation_checks(
        {"sectors": [{"code": "14030", "percentage_text": None}]}
    )
    assert single[0]["state"] == "sums-to-100"
    missing = normaliser._allocation_checks(
        {
            "sectors": [
                {"code": "14030", "percentage_text": None},
                {"code": "11220", "percentage_text": None},
            ]
        }
    )
    assert missing[0]["flag"] == "allocation-percentage-missing"
    # Countries alone must add up to 100 as well.
    alone = normaliser._allocation_checks(
        {
            "recipient_countries": [
                {"code": "KE", "percentage_text": "60", "percentage": "60"}
            ]
        }
    )
    assert alone[0]["flag"] == "allocation-sum-not-100"
