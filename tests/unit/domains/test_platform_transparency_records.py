"""Platform-transparency records: immutable revisions, removals as revisions, as-of lookup and write-time
minimisation (#2591)."""

from __future__ import annotations

import copy

import pytest

from src.ingestion.platform_transparency_sources import (
    MINIMISATION_POLICY,
    RECORD_CONTRACT,
)
from src.kb.platform_transparency_records import (
    PlatformTransparencyError,
    PlatformTransparencyStore,
    feature_enabled,
    readiness,
    validate,
)
from tests.unit import platform_transparency_harness as h

CORRECTED = "platform-transparency:dsa:sor:example-video:0a000000-0000-4000-8000-000000000003"
DROPPED = "platform-transparency:meta:ad:990000000000201"
REVISED = "platform-transparency:meta:ad:990000000000102"


def loaded(*, v2: bool = False):
    conn = h.connection()
    h.load_all(conn)
    if v2:
        h.load_all(conn, version="v2", run_id="run:v2")
    return conn, PlatformTransparencyStore(conn)


def test_every_record_carries_source_revision_and_as_of_time():
    _conn, store = loaded()
    rows = store.records(h.NS, scopes=h.SCOPES)
    assert {r["record_kind"] for r in rows} == {"dump-release", "statement-of-reasons", "ad", "advertiser", "listing"}
    for row in rows:
        citation = row["citation"]
        assert citation["source_id"] in h.SOURCES and citation["revision_id"].startswith("pt-rev:")
        assert citation["observed_at_ms"] == h.V1_MS and citation["locator"].startswith("https://")
        assert citation["evidence_origin"] == "fixture"
    statement = store.records(h.NS, scopes=h.SCOPES, record_keys=[CORRECTED])[0]
    assert statement["source_as_of"] == "2099-05-01" and statement["dump_key"].endswith(":2099-05-01:light")
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    dsa = [r for r in receipts if r["source_id"] == h.DSA]
    assert [len(r["receipt"]["dump_versions"]) for r in dsa] == [1, 1, 1]


def test_a_republished_dump_and_a_corrected_statement_are_revisions_and_the_rest_unchanged():
    _conn, store = loaded(v2=True)
    history = store.history(h.NS, CORRECTED, scopes=h.SCOPES)
    assert [(r["change"], r["record"]["fields"]["automated_decision"]) for r in history] == [
        ("new", "AUTOMATED_DECISION_PARTIALLY"), ("revised", "AUTOMATED_DECISION_FULLY")]
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    dump = store.history(h.NS, "platform-transparency:dsa:dump:example-video:2099-05-01:light", scopes=h.SCOPES)
    assert [r["change"] for r in dump] == ["new", "revised"]
    assert dump[0]["record"]["fields"]["sha1_as_published"] != dump[1]["record"]["fields"]["sha1_as_published"]
    untouched = store.history(h.NS, CORRECTED.replace("0003", "0001"), scopes=h.SCOPES)
    assert [r["change"] for r in untouched] == ["new"]


def test_an_ad_no_longer_returned_is_a_removal_revision_never_a_deletion():
    conn, store = loaded(v2=True)
    history = store.history(h.NS, DROPPED, scopes=h.SCOPES)
    assert [r["change"] for r in history] == ["new", "not-returned"]
    removal = history[-1]
    assert removal["listing_state"] == "not-returned"
    basis = removal["record"]["fields"]["not_returned_basis"]
    assert basis["listing_record_key"].startswith("platform-transparency:listing:meta:") and basis["listing_revision_id"]
    assert "did not state why" in basis["note"]
    assert removal["record"]["fields"]["spend"] == history[0]["record"]["fields"]["spend"]  # values kept as published
    google = store.history(h.NS, "platform-transparency:google:ad:CR00000000000000000103", scopes=h.SCOPES)
    assert [r["change"] for r in google] == ["new", "not-returned"]
    assert google[-1]["source_as_of"] == "2099-06-15T00:00:00Z"
    assert conn.execute("SELECT count(*) FROM platform_transparency_records WHERE record_key=?",
                        [DROPPED]).fetchone()[0] == 1


def test_as_of_lookup_returns_the_revision_current_then():
    _conn, store = loaded(v2=True)
    before = store.as_of(h.NS, REVISED, h.V1_MS + 1, scopes=h.SCOPES)
    after = store.as_of(h.NS, REVISED, h.V2_MS, scopes=h.SCOPES)
    assert before["record"]["fields"]["spend"]["lower_bound"] == "1000"
    assert after["record"]["fields"]["spend"]["lower_bound"] == "1500"
    assert store.as_of(h.NS, REVISED, h.V1_MS - 1, scopes=h.SCOPES) is None
    assert store.as_of(h.NS, REVISED, "2099-06-10", scopes=h.SCOPES)["revision_id"] == before["revision_id"]
    listed_then = store.records(h.NS, scopes=h.SCOPES, kinds=["ad"], provider="meta-ad-library", as_of=h.V1_MS)
    assert {r["record_key"]: r["listing_state"] for r in listed_then}[DROPPED] == "listed"
    now = store.records(h.NS, scopes=h.SCOPES, kinds=["ad"], provider="meta-ad-library")
    assert {r["record_key"]: r["listing_state"] for r in now}[DROPPED] == "not-returned"


def test_replays_add_nothing_and_an_older_response_never_relists_or_reverts():
    conn, store = loaded(v2=True)
    count = conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0]
    h.load_all(conn, version="v2", run_id="run:v2-again")
    h.load_all(conn, version="v1", run_id="run:v1-late")
    assert conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0] == count
    assert store.records(h.NS, scopes=h.SCOPES, record_keys=[DROPPED])[0]["listing_state"] == "not-returned"
    assert store.records(h.NS, scopes=h.SCOPES, record_keys=[REVISED])[0]["record"]["fields"]["spend"][
        "lower_bound"] == "1500"


def test_an_ad_that_returns_later_is_relisted():
    _conn, store = loaded(v2=True)
    fetched = h.adapter(h.META, "v2").fetch_page({"operation": "selection", "parameters": {}}, cursor=None)
    records = [copy.deepcopy(r["platform_transparency_record"]) for r in fetched.records]
    original = next(r["platform_transparency_record"] for r in h.adapter(h.META).fetch_page(
        {"operation": "selection", "parameters": {}}, cursor=None).records
        if r["platform_transparency_record"]["record_key"] == DROPPED)
    listing = next(r for r in records if r["record_kind"] == "listing")
    listing["fields"]["ad_keys"] = sorted(listing["fields"]["ad_keys"] + [DROPPED])
    outcome = store.project(h.NS, records + [original], run_id="run:v3", source_id=h.META)
    assert outcome["counts"]["relisted"] == 1
    assert [r["change"] for r in store.history(h.NS, DROPPED, scopes=h.SCOPES)] == ["new", "not-returned", "relisted"]


def _record(**overrides):
    record = {"contract": RECORD_CONTRACT, "format": None, "provider": "meta-ad-library", "platform": "meta",
              "record_kind": "ad", "record_key": "platform-transparency:meta:ad:1", "unit_key": "u",
              "advertiser_key": "platform-transparency:meta:advertiser:1", "dump_key": None, "native_revision": None,
              "revision_order": "", "effective_on": None, "source_as_of": None, "title": "ad",
              "locator": "https://www.facebook.com/ads/library/?id=1",
              "minimisation": {"policy": MINIMISATION_POLICY, "withheld": []},
              "fields": {"ad_id": "1", "spend": {"lower_bound": "0", "upper_bound": "99"}, "listing_state": "listed"}}
    record.update(overrides)
    return record


@pytest.mark.parametrize("fields", [
    {"ad_id": "1", "demographic_distribution": [{"age": "18-24"}]},
    {"ad_id": "1", "spend": {"lower_bound": "0", "upper_bound": "99", "midpoint": "49.5"}},
    {"ad_id": "1", "estimated_spend": "49.5"},
    {"uuid": "x", "platform_uid": "user-1"},
    {"uuid": "x", "source_identity": "A notifier"},
    {"ad_id": "1", "coordination_score": 0.7},
])
def test_withheld_and_point_estimate_fields_are_refused_at_write_time(fields):
    conn, store = loaded()
    before = conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0]
    with pytest.raises(PlatformTransparencyError) as refused:
        store.project(h.NS, [_record(fields=fields)], run_id="bad", source_id=h.META)
    assert refused.value.code == "minimisation_violation"
    assert conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0] == before


def test_structural_rules_locators_statements_and_takedown_notices():
    with pytest.raises(PlatformTransparencyError):
        validate(_record(locator="https://www.facebook.com/ads/archive/render_ad/?id=1&access_token=x"))
    with pytest.raises(PlatformTransparencyError):
        validate(_record(record_kind="statement-of-reasons", provider="dsa-transparency-db", fields={"uuid": "x"}))
    notice = _record(record_kind="takedown-notice", provider="lumen", platform="lumen",
                     record_key="platform-transparency:lumen:notice:1", advertiser_key=None,
                     locator="https://lumendatabase.org/notices/1",
                     fields={"notice_id": "1", "sender": {"kind": "withheld", "name": None},
                             "recipient": {"kind": "organisation", "name": "Example Video"},
                             "title": "[REDACTED] notice", "topics": ["Copyright"]})
    assert validate(notice)["fields"]["title"] == "[REDACTED] notice"  # redactions kept as published
    person = copy.deepcopy(notice)
    person["fields"]["sender"] = {"kind": "natural-person", "name": "Pat Placeholder"}
    with pytest.raises(PlatformTransparencyError) as refused:
        validate(person)
    assert refused.value.code == "minimisation_violation"
    body = copy.deepcopy(notice)
    body["fields"]["body"] = "the notice text"
    with pytest.raises(PlatformTransparencyError):
        validate(body)


def test_readiness_reports_providers_minimisation_and_features_off_by_default():
    conn, _ = loaded()
    state = readiness(conn)
    assert state["enabled"] == {"dsa-transparency-db": False, "meta-ad-library": False,
                                "google-political-ads": False, "lumen": False}
    assert state["providers"]["lumen"] == {"access_decision": "not-implemented", "live": "not-implemented",
                                           "records": 0}
    assert state["providers"]["meta-ad-library"]["records"] == 8
    assert not feature_enabled(conn, "platform-transparency-dsa")
    with pytest.raises(PlatformTransparencyError):
        PlatformTransparencyStore(conn).records(h.NS, scopes=set())
