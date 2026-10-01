"""IP02 (#2592): income series records, immutable vintages, removals as revisions and as-of lookup."""

from __future__ import annotations

import json

import pytest

from src.kb.income_distribution_records import IncomeError, check_item
from tests.unit import income_distribution_harness as h


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    return conn


def _headcount() -> dict:
    record = next(r for r in h.fetch("pip")[0] if r["income_item"]["native_key"].endswith("headcount"))
    return json.loads(json.dumps(record))


def test_series_are_keyed_by_source_welfare_scale_line_ppp_place_and_coverage(loaded):
    store = h.store(loaded)
    head_2017 = h.series(loaded, "pip", "pip:DEU:national:income:headcount", ppp=2017)
    head_2021 = h.series(loaded, "pip", "pip:DEU:national:income:headcount", ppp=2021)
    assert head_2017["series_id"] != head_2021["series_id"]
    assert head_2017["key"]["poverty_line"] == {"basis": "absolute-ppp", "amount": "2.15",
                                                "unit": "2017 PPP dollars per person per day",
                                                "label": "International poverty line ($2.15, 2017 PPP)",
                                                "ppp_base_year": 2017}
    assert head_2021["key"]["poverty_line"]["ppp_base_year"] == 2021
    silc = h.series(loaded, "eurostat-silc", "LI_R_MD60")
    assert silc["key"]["equivalence_scale"] == "modified-oecd" and silc["key"]["survey"] == "EU-SILC"
    assert silc["key"]["poverty_line"]["basis"] == "relative-median"
    oecd = h.series(loaded, "oecd-idd", "INC_DISP_GINI")
    assert oecd["key"]["methodology"] == "METH2012" and "D_CUR" in oecd["key"]["income_definition"]
    region = h.series(loaded, "pip", "pip-grp:ECA:regional:mixed:headcount")
    assert region["key"]["welfare_concept"] == "mixed" and region["area"]["scheme"] == "wb-region"
    # Values also live in the Economics series storage, domain society.
    vintage = store.vintage_rows(h.NS, silc["series_id"])[0]
    assert loaded.execute("SELECT count(*) FROM economic_vintages WHERE domain='society' AND series_id=?",
                          [silc["series_id"]]).fetchone()[0] == 2
    numeric = {o["period"]: o["numeric_value"] for o in store.observations(h.NS, vintage["vintage_id"])}
    assert numeric["2094"] == pytest.approx(16.1)


def test_each_release_is_an_appended_vintage_and_a_ppp_revision_never_overwrites(loaded):
    store = h.store(loaded)
    head = h.series(loaded, "pip", "pip:DEU:national:income:headcount", ppp=2017)
    first, second = store.vintage_rows(h.NS, head["series_id"])
    assert first["release_label"] == "20980915_2017_01_02_PROD" and second["release_label"] == "20990320_2017_02_02_PROD"
    assert second["previous_vintage_id"] == first["vintage_id"]
    assert second["changes"]["ppp_revision"] == {"before": {"base_year": 2017, "revision": "01_02"},
                                                 "after": {"base_year": 2017, "revision": "02_02"}}
    assert second["changes"]["new_periods"] == ["2097"]
    assert [r["period"] for r in second["changes"]["revised"]] == ["2094"]
    old = {o["period"]: o["value"] for o in store.observations(h.NS, first["vintage_id"])}
    new = {o["period"]: o["value"] for o in store.observations(h.NS, second["vintage_id"])}
    assert old["2094"] == "0.0021" and new["2094"] == "0.0022"  # the earlier vintage is untouched


def test_as_of_lookup_selects_the_vintage_released_by_the_date(loaded):
    store = h.store(loaded)
    head = h.series(loaded, "pip", "pip:DEU:national:income:headcount", ppp=2017)
    before = store.values(h.NS, head["series_id"], as_of_ms=h.day_ms("2098-01-01"))
    assert before["status"] == "unavailable" and before["reason"] == "no_release_by_as_of"
    mid = store.values(h.NS, head["series_id"], as_of_ms=h.day_ms("2099-01-01"))
    assert mid["vintage"]["release_label"] == "20980915_2017_01_02_PROD"
    assert mid["citation"]["as_of"] == "2098-09-15T00:00:00Z" and mid["citation"]["retrieved_at"]
    latest = store.values(h.NS, head["series_id"])
    assert latest["vintage"]["release_label"] == "20990320_2017_02_02_PROD"


def test_a_removal_by_the_source_is_a_vintage_never_a_deletion(loaded):
    store = h.store(loaded)
    rate = h.series(loaded, "oecd-idd", "PR_INC_DISP")
    published, removed = store.vintage_rows(h.NS, rate["series_id"])
    assert removed["status"] == "removed" and "removed_by_source" in removed["changes"]
    assert store.values(h.NS, rate["series_id"])["status"] == "removed_by_source"
    earlier = store.values(h.NS, rate["series_id"], as_of_ms=h.day_ms("2099-01-01"))
    assert earlier["status"] == "available" and earlier["vintage"]["vintage_id"] == published["vintage_id"]


def test_reacquisition_is_idempotent_and_an_unchanged_republication_adds_no_vintage(loaded):
    before = loaded.execute("SELECT count(*) FROM income_vintages").fetchone()[0]
    results = h.apply(loaded, "silc", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert {r["status"] for r in results} == {"unchanged"}
    assert loaded.execute("SELECT count(*) FROM income_vintages").fetchone()[0] == before
    gini = h.series(loaded, "eurostat-silc", "GINI_HND")
    assert len(h.store(loaded).vintage_rows(h.NS, gini["series_id"])) == 1


def test_minimisation_and_exclusions_are_enforced_at_write_time():
    conn = h.connection()
    record = _headcount()
    item = record["income_item"]
    check_item(item)
    leaky = json.loads(json.dumps(item))
    leaky["observations"][0]["attributes"]["household_id"] = "HH-1"
    with pytest.raises(IncomeError) as caught:
        check_item(leaky)
    assert caught.value.code == "personal_data"
    filled = json.loads(json.dumps(item))
    filled["observations"][0]["gap_filled"] = True
    with pytest.raises(IncomeError) as caught:
        check_item(filled)
    assert caught.value.code == "derived_value"
    no_line = json.loads(json.dumps(item))
    no_line["poverty_line"] = None
    with pytest.raises(IncomeError):
        check_item(no_line)
    header = record["income_release"]
    result = h.store(conn).apply_release(h.NS, header, [leaky], source_id="x", run_id="r", principal_id="svc",
                                         scopes=h.SCOPES, retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert result["rejected"][0]["code"] == "personal_data" and result["vintages"] == 0


def test_writes_and_reads_need_scopes():
    conn = h.connection()
    header = h.fetch("pip")[0][0]["income_release"]
    item = h.fetch("pip")[0][0]["income_item"]
    with pytest.raises(IncomeError) as caught:
        h.store(conn).apply_release(h.NS, header, [item], source_id="x", run_id="r", principal_id="svc",
                                    scopes=h.READ_ONLY)
    assert caught.value.code == "unauthorized"
