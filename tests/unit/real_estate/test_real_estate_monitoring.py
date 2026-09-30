"""RE10 (#2506): transaction releases, index vintages and parcel revisions through subscriptions, offline."""

from __future__ import annotations

import json

import pytest

from src.kb.real_estate import RealEstateError
from src.kb.real_estate_identity import RealEstateIdentity
from src.kb.real_estate_monitoring import EVENT_KINDS, RealEstateMonitor
from tests.unit.real_estate import fixture_builder as fb
from tests.unit.real_estate.harness import NS, SCOPES, Env, seed_places

TREND_WORDS = ("rise", "rising", "fall", "falling", "increase", "decrease", "trend", "buy", "sell", "recommend")


def kinds(result):
    return sorted({(n["kind"], n["watched"]) for n in result["notifications"]})


def test_each_event_type_is_delivered_once_and_unchanged_releases_emit_nothing():
    env = Env().loaded()
    ids = seed_places(env)
    RealEstateIdentity(env.conn, now=env.now).propose(NS, principal_id="alice", scopes=SCOPES)
    monitor = RealEstateMonitor(env.conn, now=env.now)
    created = monitor.create(NS, "m1", principal_id="alice", scopes=SCOPES, places=[ids["district"], ids["paris"]],
                             parcels=["75104000AB0013"])
    sid = created["subscription_id"]
    baseline = monitor.run(sid, principal_id="alice", scopes=SCOPES)
    assert baseline["baseline"] and {"transaction_new", "index_vintage_new"} <= {n["kind"] for n in
                                                                                  baseline["notifications"]}
    assert monitor.run(sid, principal_id="alice", scopes=SCOPES)["notifications"] == []
    env.ppd_release_2()
    ppd = monitor.run(sid, principal_id="alice", scopes=SCOPES)
    district = f"place:{ids['district']}"
    # TX4 is in ZZ2, outside the watched district: nothing about it.
    assert kinds(ppd) == [("transaction_revised", district), ("transaction_withdrawn", district)]
    revised = next(n for n in ppd["notifications"] if n["kind"] == "transaction_revised")
    assert revised["prior"]["revision_id"] and revised["new"]["release"] == "2099-03"
    assert revised["new"]["url"].startswith("https://price-paid-data.")
    env.ukhpi_release()
    hpi = monitor.run(sid, principal_id="alice", scopes=SCOPES)
    assert kinds(hpi) == [("index_vintage_new", district)]
    assert hpi["notifications"][0]["new"]["release"] == "2099-04"
    env.parcel_revision()
    parcel = monitor.run(sid, principal_id="alice", scopes=SCOPES)
    assert kinds(parcel) == [("parcel_revised", "parcel:75104000AB0013")]
    env.dvf_release()
    dvf = monitor.run(sid, principal_id="alice", scopes=SCOPES)
    paris = f"place:{ids['paris']}"
    assert {k for k, w in kinds(dvf) if w == paris} == {"transaction_new", "transaction_revised",
                                                        "transaction_withdrawn"}
    # The multi-parcel mutation is unchanged in the new release: nothing about it, for the place or the parcel.
    assert not [n for n in dvf["notifications"] if n.get("transaction") == "2098-1001"]
    # A re-read of the same release emits nothing; a replay at the same watermark delivers nothing.
    env.ppd_release_2("ppd-again")
    assert monitor.run(sid, principal_id="alice", scopes=SCOPES)["notifications"] == []
    seen = {n["kind"] for r in (baseline, ppd, hpi, parcel, dvf) for n in r["notifications"]}
    assert seen == set(EVENT_KINDS)
    text = json.dumps([baseline, ppd, hpi, parcel, dvf]).casefold()
    assert not [w for w in TREND_WORDS if f" {w} " in text]
    assert monitor.poll(sid, principal_id="alice", scopes=SCOPES)


def test_monitors_refuse_unknown_places_and_bad_codes_and_wait_for_a_committed_run():
    env = Env()
    monitor = RealEstateMonitor(env.conn, now=env.now)
    with pytest.raises(RealEstateError):
        monitor.create(NS, "x", principal_id="alice", scopes=SCOPES, places=["place:nope"])
    with pytest.raises(RealEstateError):
        monitor.create(NS, "y", principal_id="alice", scopes=SCOPES, codes=[{"scheme": "owner", "code": "x"}])
    created = monitor.create(NS, "z", principal_id="alice", scopes=SCOPES,
                             codes=[{"scheme": "uk-postcode-district", "code": "zz1"}])
    with pytest.raises(RealEstateError) as caught:
        monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert caught.value.code == "watermark_uncommitted"
    env.loaded()
    result = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert {n["transaction"] for n in result["notifications"]} == {fb.TX1.strip("{}"), fb.TX2.strip("{}")}
