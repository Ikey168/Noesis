"""Platform-transparency records: immutable revisions, removals as revisions, as-of lookup and write-time
minimisation (#2591)."""

from __future__ import annotations

import copy

import pytest

from src.kb.platform_transparency_records import (
    PlatformTransparencyError,
    PlatformTransparencyStore,
    readiness,
    validate,
)
from tests.unit import platform_transparency_harness as h

AD = "platform-transparency:meta:ad:880000000000001"
REMOVED = "platform-transparency:meta:ad:880000000000003"
STATEMENT = "platform-transparency:dsa:sor:exampla-social:00000000-0000-4000-8000-00000000a001"
WITHDRAWN = "platform-transparency:dsa:sor:exampla-social:00000000-0000-4000-8000-00000000a003"


@pytest.fixture()
def loaded():
    conn = h.connection()
    h.load_all(conn)
    h.load_all(conn, version="v2", run_id="run:v2")
    return conn, PlatformTransparencyStore(conn)


def test_every_record_carries_source_revision_and_as_of_time(loaded):
    _, store = loaded
    for row in store.records(h.NS, scopes=h.SCOPES):
        citation = row["citation"]
        assert citation["source_id"] in h.SOURCES and citation["revision_id"].startswith("pt-rev:")
        assert citation["observed_at_ms"] in {h.T1, h.T2} and citation["locator"].startswith("https://")
        assert citation["evidence_origin"] == "fixture"
    google = store.records(h.NS, scopes=h.SCOPES, providers=["google-political-ads"])
    assert {r["citation"]["data_as_of"] for r in google if r["record"]["listing_status"] == "listed"} == {
        "2099-11-17T06:00:00Z"}  # the bundle refresh time on every listed record
    assert {r["citation"]["data_as_of"] for r in google if r["record"]["listing_status"] != "listed"} == {
        "2099-11-10T06:00:00Z"}  # a removal keeps the refresh time it was last listed under


def test_changed_ads_and_corrected_statements_are_revision_chains(loaded):
    _, store = loaded
    chain = store.history(h.NS, AD, scopes=h.SCOPES)
    assert [(r["revision_no"], r["change"]) for r in chain] == [(1, "new"), (2, "revised")]
    assert chain[1]["previous_revision_id"] == chain[0]["revision_id"]
    assert chain[0]["record"]["fields"]["spend_range_as_published"]["lower_bound"] == "100"  # never overwritten
    assert chain[1]["record"]["fields"]["spend_range_as_published"]["lower_bound"] == "200"
    corrected = store.history(h.NS, STATEMENT, scopes=h.SCOPES)
    assert [r["record"]["fields"]["category"] for r in corrected] == [
        "STATEMENT_CATEGORY_ILLEGAL_OR_HARMFUL_SPEECH", "STATEMENT_CATEGORY_VIOLENCE"]
    dump = store.history(h.NS, "platform-transparency:dsa:dump:exampla-social:2099-05-01:light", scopes=h.SCOPES)
    assert [r["change"] for r in dump] == ["new", "revised"]
    assert dump[0]["record"]["fields"]["sha256"] != dump[1]["record"]["fields"]["sha256"]


def test_removals_are_not_returned_revisions_never_deletions(loaded):
    conn, store = loaded
    for key in (REMOVED, WITHDRAWN, "platform-transparency:google:ad:CR10000000000000000002"):
        chain = store.history(h.NS, key, scopes=h.SCOPES)
        assert [r["change"] for r in chain] == ["new", "not-returned"]
        assert chain[-1]["record"]["listing_status"] == "not-returned"
        assert "observed absence" in chain[-1]["record"]["not_returned"]["statement"]
    total = conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0]
    assert total > len(store.records(h.NS, scopes=h.SCOPES))  # nothing deleted


def test_as_of_lookup_answers_the_revision_on_record_at_a_time(loaded):
    _, store = loaded
    assert store.as_of(h.NS, AD, "2099-11-01", scopes=h.SCOPES) is None  # before the first acquisition
    first = store.as_of(h.NS, AD, "2099-11-20", scopes=h.SCOPES)
    later = store.as_of(h.NS, AD, "2099-12-02", scopes=h.SCOPES)
    assert (first["revision_no"], later["revision_no"]) == (1, 2)
    assert store.as_of(h.NS, REMOVED, "2099-11-20", scopes=h.SCOPES)["record"]["listing_status"] == "listed"
    assert store.as_of(h.NS, REMOVED, h.T2, scopes=h.SCOPES)["record"]["listing_status"] == "not-returned"


def test_replays_are_idempotent_listed_again_records_are_revisions_and_receipts_are_kept(loaded):
    conn, store = loaded
    again = h.apply(conn, "meta-ad-library-political", version="v2", run_id="run:v2-replay")
    assert sum(o["counts"]["new"] + o["counts"]["revised"] + o["counts"]["not-returned"] for o in again) == 0
    relisted = h.apply(conn, "meta-ad-library-political", version="v1", run_id="run:v1-again",
                       observed_at_ms=h.T2 + 1)
    # the removed ad is listed again (a revision); the replayed older ranges of another ad are already on record
    assert sum(o["counts"]["revised"] for o in relisted) == 1
    assert store.history(h.NS, AD, scopes=h.SCOPES)[-1]["revision_no"] == 2
    assert store.history(h.NS, REMOVED, scopes=h.SCOPES)[-1]["record"]["listing_status"] == "listed"
    receipts = store.receipts(h.NS, "run:v2", scopes=h.SCOPES)
    assert receipts and all(r["receipt"]["contract"] == "noesis-platform-transparency-acquisition-receipt-v1"
                            for r in receipts)
    assert all("Bearer" not in str(r) for r in receipts)


def test_minimisation_is_enforced_at_write_time():
    conn = h.connection()
    store = PlatformTransparencyStore(conn)
    record = h.adapter("dsa-sor-dumps").fetch_page({"operation": "selection", "parameters": {}, "limit": 500},
                                                   cursor=None).records[1]["platform_transparency_record"]
    for key, value in (("decision_facts", "free text"), ("puid", "post-1"), ("source_identity", "a notifier")):
        bad = copy.deepcopy(record)
        bad["fields"][key] = value
        with pytest.raises(PlatformTransparencyError) as refused:
            store.project(h.NS, [bad], run_id="r", source_id="dsa-sor-dumps")
        assert refused.value.code == "minimisation_violation"
    bad = copy.deepcopy(record)
    bad["locator"] = "https://www.facebook.com/ads/archive/render_ad/?id=1&access_token=x"
    with pytest.raises(PlatformTransparencyError):
        validate(bad)
    assert conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0] == 0
    with pytest.raises(PlatformTransparencyError) as unauthorized:
        store.records(h.NS, scopes={"knowledge:read"})
    assert unauthorized.value.code == "unauthorized"


def test_readiness_reports_lumen_as_degraded():
    conn = h.connection()
    h.load_all(conn)
    ready = readiness(conn)
    assert ready["store_ready"] and ready["degraded"] == ["lumen"]
    assert ready["providers"]["lumen"]["state"] == "unavailable" and "not been granted" in \
        ready["providers"]["lumen"]["reason"]
    assert ready["providers"]["meta-ad-library"]["records"] == 9
    assert not any(p["enabled"] for p in ready["providers"].values())  # features default off
