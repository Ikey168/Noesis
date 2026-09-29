"""Vintage comparison, reporting-delay view and pins for surveillance series (I08)."""

from __future__ import annotations

import inspect

import pytest

from src.kb import surveillance_vintages as sv
from src.kb.surveillance import SurveillanceError
from tests.unit.clinical import surveillance_harness as h


@pytest.fixture
def env():
    loaded = h.Env()
    loaded.acquire("r1", ["rki", "eurostat"])
    return loaded


def rki_series(env, geo, age="A15-A34"):
    (found,) = [
        s
        for s in env.series(provider="rki-open-data", geography_code=geo)
        if s["dimensions"] == {"age_group": age}
    ]
    return found


def test_the_comparison_calls_the_shared_environment_logic_rather_than_copying_it():
    source = inspect.getsource(sv)
    assert "diff_values(" in source and "pin_entry(" in source
    assert (
        "Decimal(after" not in source
    )  # the diff algorithm lives in environment_vintages only


def test_changes_cite_both_vintages_and_say_no_cause_is_inferred(env):
    env.upgrade_rki("2099-02-03")
    env.acquire("r2", ["rki"])
    series = rki_series(env, "09184")
    result = sv.compare(env.conn, h.NS, series["series_id"], scopes=h.READ_ONLY)
    assert (
        result["notice"]
        == "differences between published vintages; no cause is inferred"
    )
    assert result["left"]["native_revision"].endswith("@2099-01-20")
    assert result["right"]["native_revision"].endswith("@2099-02-03")
    assert (
        result["left"]["source_revision"]["release_id"]
        != result["right"]["source_revision"]["release_id"]
    )
    changes = {
        (c["reference_period"], c["reporting_date"]): c for c in result["changes"]
    }
    revised = changes[("2099-01-08", "2099-01-14")]
    assert (
        revised["change"],
        revised["delta"],
        revised["left"]["value"],
        revised["right"]["value"],
    ) == ("value_revised", "1", "2", "3")
    assert revised["left"]["vintage_id"] == result["left"]["vintage_id"]
    assert changes[("2099-01-21", "2099-01-28")]["change"] == "added"
    assert all(c["attribution"] == "published-difference" for c in result["changes"])


def test_a_case_definition_change_is_not_presented_as_a_data_revision(env):
    from tests.unit import surveillance_fixture_builder as fb

    # The source re-declares when the 2099 edition applies: the same value now falls under another edition.
    document = fb.rki_document("2099-02-03")
    document["case_definitions"][0]["valid_to"] = "2098-12-21"
    document["case_definitions"][1]["valid_from"] = "2098-12-22"
    env.upgrade_rki("2099-02-03")
    import json

    from src.ingestion.source_packs import SourcePackStore, validate_source_pack

    manifest = json.loads(h.PACK.read_text())
    manifest["version"] = "0.1.3"
    source = next(s for s in manifest["sources"] if s["source_id"] == h.SOURCES["rki"])
    source["surveillance"]["documents"] = [document]
    SourcePackStore(env.conn).install(
        validate_source_pack(manifest), principal_id="operator", enable=True, now_ms=3
    )
    env.runtime.accept_license(
        "clinical-evidence", h.SOURCES["rki"], principal_id="operator"
    )
    env.acquire("r2", ["rki"])
    series = rki_series(env, "09184")
    result = sv.compare(env.conn, h.NS, series["series_id"], scopes=h.READ_ONLY)
    moved = [
        c for c in result["changes"] if c["attribution"] == "case-definition-change"
    ]
    assert [(c["reference_period"], c["case_definition"]) for c in moved] == [
        ("2098-12-30", {"left": "2019", "right": "2099"})
    ]
    assert (
        moved[0]["change"] == "status_changed"
        or moved[0]["left"]["value"] == moved[0]["right"]["value"]
    )
    assert result["summary"]["definition_changes"] == 1
    assert any(b["kind"] == "case-definition" for b in result["breaks"])


def test_the_reporting_delay_view_shows_first_and_later_reports_and_only_the_sources_note(
    env,
):
    env.upgrade_rki("2099-02-03")
    env.acquire("r2", ["rki"])
    series = rki_series(env, "09162")
    view = sv.reporting_delay(env.conn, h.NS, series["series_id"], scopes=h.READ_ONLY)
    rows = {r["reference_period"]: r for r in view["periods"]}
    january = rows["2099-01-02"]
    assert [r["reporting_date"] for r in january["first_reported"]["reported"]] == [
        "2099-01-05"
    ]
    assert [r["reporting_date"] for r in january["later"][0]["reported"]] == [
        "2099-01-05",
        "2099-01-25",
    ]
    assert january["changed_after_first"] is True
    # The source's note: the last three weeks before a release are incomplete. 2099-01-02 is within three weeks
    # of the 2099-01-20 release but not of the 2099-02-03 release.
    assert january["first_reported"]["labels"] == ["incomplete-by-source-note"]
    assert january["later"][0]["labels"] == []
    assert rows["2098-12-20"]["first_reported"]["labels"] == []
    assert [u["reporting_date"] for u in view["reference_unknown"]] == [
        "2099-01-12",
        "2099-01-12",
    ]
    assert view["delay_notes"][0]["incomplete_recent"] == {
        "interval": "week",
        "count": 3,
    }
    assert "no completeness estimate or nowcast" in view["notice"]


def test_series_without_a_source_note_get_no_incompleteness_label(env):
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    view = sv.reporting_delay(env.conn, h.NS, germany["series_id"], scopes=h.READ_ONLY)
    assert view["delay_notes"] == [] and all(
        not r["first_reported"]["labels"] for r in view["periods"]
    )


def test_a_pinned_view_turns_stale_and_never_follows_a_newer_vintage(env):
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    (first,) = env.store().vintage_rows(h.NS, germany["series_id"])
    pinned = sv.pin(
        env.conn,
        h.NS,
        "dossier:tb-germany",
        [first["vintage_id"]],
        principal_id="alice",
        scopes=h.SCOPES,
        now=env.clock,
    )
    assert pinned["stale"] is False and pinned["pins"][0]["state"] == "current"
    env.eurostat_update()
    env.acquire("r2", ["eurostat"])
    status = sv.pin_status(env.conn, h.NS, "dossier:tb-germany", scopes=h.READ_ONLY)
    (entry,) = status["pins"]
    assert (
        status["stale"] is True
        and entry["state"] == "stale"
        and len(entry["newer_vintages"]) == 1
    )
    assert {v["reference_period"]: v["value"] for v in entry["values"]}[
        "2098"
    ] == "288"  # still the pinned value
    with pytest.raises(SurveillanceError) as caught:
        sv.pin(
            env.conn,
            h.NS,
            "dossier:tb-germany",
            ["sv-vintage:missing"],
            principal_id="alice",
            scopes=h.SCOPES,
        )
    assert caught.value.code == "invalid_pin"
    with pytest.raises(SurveillanceError):
        sv.pin(
            env.conn,
            h.NS,
            "view",
            [first["vintage_id"]],
            principal_id="alice",
            scopes=h.READ_ONLY,
        )


def test_a_bad_pin_row_is_reported_without_breaking_the_listing(env):
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    (vintage,) = env.store().vintage_rows(h.NS, germany["series_id"])
    sv.pin(
        env.conn,
        h.NS,
        "view",
        [vintage["vintage_id"]],
        principal_id="alice",
        scopes=h.SCOPES,
    )
    env.conn.execute(
        "INSERT INTO surveillance_pins VALUES (?,?,?,?,?,?,?)",
        [
            h.NS,
            "sv-pin:broken",
            "view",
            germany["series_id"],
            "sv-vintage:gone",
            "x",
            1,
        ],
    )
    states = {
        p["vintage_id"]: p["state"]
        for p in sv.pin_status(env.conn, h.NS, "view", scopes=h.READ_ONLY)["pins"]
    }
    assert states == {
        vintage["vintage_id"]: "current",
        "sv-vintage:gone": "unavailable",
    }


def test_a_single_vintage_has_nothing_to_compare(env):
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    result = sv.compare(env.conn, h.NS, germany["series_id"], scopes=h.READ_ONLY)
    assert result["status"] == "single_vintage" and result["changes"] == []
    with pytest.raises(SurveillanceError):
        sv.compare(
            env.conn,
            h.NS,
            germany["series_id"],
            scopes=h.READ_ONLY,
            right="sv-vintage:other",
        )


def test_a_republished_number_in_another_notation_is_not_a_revision(env):
    from tests.unit import surveillance_fixture_builder as fb

    env.web.set(
        fb.eurostat_request(),
        fb.eurostat_csv()
        .replace("15/03/99 11:00:00", "16/03/99 11:00:00")
        .replace(",288,", ",288.0,"),
        headers={"Content-Type": "text/csv"},
    )
    env.acquire("r2", ["eurostat"])
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    result = sv.compare(env.conn, h.NS, germany["series_id"], scopes=h.READ_ONLY)
    assert result["status"] == "compared" and result["changes"] == []
