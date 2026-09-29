"""Threshold, vintage and case-definition monitors over surveillance series through subscriptions (I10)."""

from __future__ import annotations

import pytest

from src.kb.surveillance import SurveillanceError
from src.kb.surveillance_monitoring import SurveillanceMonitor, threshold_in_series_unit
from tests.unit.clinical import surveillance_harness as h


@pytest.fixture
def env():
    loaded = h.Env()
    loaded.acquire("r1", ["rki", "gho", "eurostat"])
    return loaded


def monitor(env):
    return SurveillanceMonitor(env.conn, now=env.clock)


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_thresholds_are_user_configured_and_unit_checked_never_derived(env):
    (rate,) = env.series(provider="who-gho", kind="observation")
    converted = threshold_in_series_unit(
        {"value": "0.05", "unit": "per 1000 population"}, rate["unit"]["label"]
    )
    assert converted["value"] == "5" and converted["unit"] == "per 100 000 population"
    with pytest.raises(SurveillanceError) as caught:
        monitor(env).create(
            h.NS,
            "m-bad",
            watch={"series_id": rate["series_id"]},
            principal_id="alice",
            scopes=h.SCOPES,
            thresholds=[{"value": "3", "unit": "cases"}],
        )
    assert caught.value.code == "incompatible_unit"
    created = monitor(env).create(
        h.NS,
        "m-1",
        watch={"series_id": rate["series_id"]},
        principal_id="alice",
        scopes=h.SCOPES,
        thresholds=[{"value": "5.5", "unit": "per 100 000 population"}],
    )
    assert (
        "never derives" in created["threshold_policy"]
        and created["refresh"]["source_pack"] == "clinical-evidence"
    )
    row = env.conn.execute(
        "SELECT view_id FROM clinical_monitors WHERE subscription_id=?",
        [created["subscription_id"]],
    ).fetchone()
    assert row[0].startswith(
        "surveillance:"
    )  # the clinical monitor store; no new monitor table


def test_an_exceedance_cites_value_vintage_and_both_dates_and_replay_emits_nothing(env):
    (rate,) = env.series(provider="who-gho", kind="observation")
    created = monitor(env).create(
        h.NS,
        "m-1",
        watch={"series_id": rate["series_id"]},
        principal_id="alice",
        scopes=h.SCOPES,
        thresholds=[{"value": "5.5", "unit": "per 100 000 population"}],
    )
    first = monitor(env).run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )
    assert kinds(first) == ["new-vintage", "threshold-exceeded"]
    exceeded = next(
        n for n in first["notifications"] if n["kind"] == "threshold-exceeded"
    )
    item = exceeded["item"]
    assert (item["value"], item["reference_period"], item["reporting_date"]) == (
        "5.6",
        "2098",
        "2099-02-10",
    )
    assert item["vintage_id"] and item["source_revision"]["release_id"]
    assert "user-configured threshold" in exceeded["message"]
    replay = monitor(env).run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )
    assert replay["status"] == "replayed" and replay["notifications"] == []
    again = monitor(env).run(
        created["subscription_id"], 2, principal_id="alice", scopes=h.SCOPES
    )
    assert (
        again["notifications"] == []
    )  # the same state at a later watermark: nothing new


def test_new_vintages_value_revisions_and_case_definition_changes_are_their_own_events(
    env,
):
    (district,) = env.series(provider="rki-open-data", geography_code="09184")
    created = monitor(env).create(
        h.NS,
        "m-2",
        watch={"series_id": district["series_id"]},
        principal_id="alice",
        scopes=h.SCOPES,
        thresholds=[{"value": "1", "unit": "cases"}],
    )
    first = monitor(env).run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )
    assert kinds(first) == [
        "case-definition-changed",
        "new-vintage",
        "threshold-exceeded",
    ]
    env.upgrade_rki("2099-02-03")
    env.acquire("r2", ["rki"])
    second = monitor(env).run(
        created["subscription_id"], 2, principal_id="alice", scopes=h.SCOPES
    )
    # The revised value (2 -> 3) exceeds again; the case-definition change neither suppresses nor re-baselines it.
    assert kinds(second) == ["threshold-exceeded", "value-revised"]
    revised = next(n for n in second["notifications"] if n["kind"] == "value-revised")
    assert revised["item"]["changed_values"] == [
        "2099-01-08|2099-01-14",
        "2099-01-21|2099-01-28",
    ]
    exceeded = next(
        n for n in second["notifications"] if n["kind"] == "threshold-exceeded"
    )
    assert exceeded["item"]["value"] == "3"


def test_a_condition_within_a_boundary_can_be_watched(env):
    env.import_ecdc()
    h.import_boundaries(env.conn)
    h.align_terms(env)
    h.resolve(env)
    feature = env.conn.execute(
        "SELECT feature_id FROM geospatial_features WHERE native_id='cntr.DE'"
    ).fetchone()[0]
    created = monitor(env).create(
        h.NS,
        "m-3",
        watch={"condition": "tuberculosis", "feature_id": feature},
        principal_id="alice",
        scopes=h.SCOPES,
        thresholds=[
            {
                "value": "6",
                "unit": "per 100 000 population",
                "comparison": "at_or_above",
            }
        ],
    )
    result = monitor(env).run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )
    exceeded = [
        n["item"] for n in result["notifications"] if n["kind"] == "threshold-exceeded"
    ]
    assert {(i["kind"], i["value"]) for i in exceeded} == {
        ("estimate", "6.1"),
        ("estimate", "6.4"),
    }
    # Counts are never compared with a rate threshold: listed as not applicable.
    assert {n["series_id"] for n in result["not_applicable"]}


def test_a_failed_refresh_is_a_stale_source_event(env):
    (rate,) = env.series(provider="who-gho", kind="observation")
    created = monitor(env).create(
        h.NS,
        "m-4",
        watch={"series_id": rate["series_id"]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    monitor(env).run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )
    for request in [
        r for r in env.web.pages if "NOE_TB_NOTIF_RATE" in r and "Indicator" not in r
    ]:
        env.web.set(request, "", status=503)
    failed = env.acquire("r2", ["gho"])
    assert failed["status"] != "complete"
    result = monitor(env).run(
        created["subscription_id"], 2, principal_id="alice", scopes=h.SCOPES
    )
    assert kinds(result) == ["stale-source"] and result["coverage"][
        "stale_providers"
    ] == ["who-gho"]


def test_monitors_belong_to_their_owner_and_need_read_scopes(env):
    (rate,) = env.series(provider="who-gho", kind="observation")
    created = monitor(env).create(
        h.NS,
        "m-5",
        watch={"series_id": rate["series_id"]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    with pytest.raises(Exception) as caught:
        monitor(env).run(
            created["subscription_id"], 1, principal_id="mallory", scopes=h.SCOPES
        )
    assert getattr(caught.value, "code", "") == "monitor_not_found"
    with pytest.raises(SurveillanceError):
        monitor(env).create(
            h.NS,
            "m-6",
            watch={"series_id": rate["series_id"]},
            principal_id="alice",
            scopes={"knowledge:subscriptions:write"},
        )
    with pytest.raises(SurveillanceError):
        monitor(env).create(
            h.NS,
            "m-7",
            watch={"condition": "tuberculosis"},
            principal_id="alice",
            scopes=h.SCOPES,
        )
    monitor(env).run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )
    polled = monitor(env).poll(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert (
        len(polled["events"]) == 1
        and polled["subscription_id"] == created["subscription_id"]
    )
