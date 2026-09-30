"""UNOOSA index and registration documents, ESA DISCOS and Aerospace re-entries, offline (#2224, SO03-SO06)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.ingestion.astronomy_registration_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    RegistrationAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.astronomy_records import day_ms
from src.kb.astronomy_registration import RegistrationStore
from tests.unit.astronomy import registration_harness as h

NS = "astronomy"


@pytest.fixture
def conn():
    value = duckdb.connect(":memory:")
    yield value
    value.close()


def views(conn, *keys, kinds=None, cutoff=None):
    return RegistrationStore(conn, initialize=False).visible(
        NS, keys=list(keys), kinds=kinds, public_cutoff_ms=cutoff
    )["records"]


def test_access_decisions_are_recorded_and_nothing_is_live_verified():
    for provider in ("unoosa-index", "unoosa-registration-documents", "esa-discos", "aerospace-reentry"):
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    assert PROVIDER_CONTRACTS["esa-discos"]["auth"].startswith("required-secret")
    assert PROVIDER_CONTRACTS["space-track-decay"]["access_decision"] == "not-implemented"
    assert not any(v["status"] == "live-verified" for v in LIVE_VERIFICATION.values())


def test_unoosa_index_entries_keyed_by_cospar_with_status_revisions_and_receipts(conn):
    receipts = h.acquire(conn, "index", "2099-03-20")
    counts = receipts[0]["counts"]
    assert counts["out_of_scope"] == 1 and counts["rejected"] == 1  # 2098 row bounded out; no-designator rejected
    assert any(r.get("rejection") for r in receipts[0]["records"])
    entry = views(conn, "cospar:2099-001A", kinds=["registration_entry"])[0]["record"]
    assert entry["un_document"] == "ST/SG/SER.E/9901" and entry["status"] == "in orbit"
    assert entry["index_retrieved_on"] == "2099-03-20"
    rb = views(conn, "cospar:2099-001B")[0]["record"]
    assert rb["un_registered"] is False and "un_document" not in rb and "registering_state" not in rb
    h.acquire(conn, "index", "2099-07-05")
    view = views(conn, "cospar:2099-001A", kinds=["registration_entry"])[0]
    assert view["record"]["status"] == "decayed"
    assert [r["record"]["status"] for r in view["revisions"]] == ["in orbit", "decayed"]
    earlier = views(conn, "cospar:2099-001A", cutoff=day_ms("2099-04-01"))[0]
    assert earlier["record"]["status"] == "in orbit"
    assert RegistrationStore(conn).receipts(NS)[0]["receipt"]["stored"]["inserted"] == 2


def test_registration_documents_quote_entries_with_locators_and_keep_registered_values(conn):
    h.acquire(conn, "documents", "2099-04-02")
    records = [v["record"] for v in views(conn, "doc:ST/SG/SER.E/9901")]
    entry = next(r for r in records if r["kind"] == "registration_entry" and r.get("cospar") == "2099-001A")
    assert entry["entry_kind"] == "registration" and entry["document_locator"] == {"paragraph": "2"}
    assert entry["language"] == "en" and entry["quotation"].startswith("2. International designator: 2099-001A")
    assert entry["registered_orbit"]["nodal_period"] == {"value": "94.1", "unit": "minutes"}
    assert {i["identifier"] for i in entry["instruments"]} == {"UNTS-1023-15", "FL-SAA-2090"}
    operators = {(r["operator_name"], r["role"]) for r in records if r["kind"] == "operator_assertion"}
    assert ("Fictional Satellite Operations LLC", "operator") in operators
    assert ("Fictland Space Agency", "owner") in operators
    lei = next(r for r in records if r["kind"] == "operator_assertion" and r["role"] == "operator"
               and r.get("cospar"))
    assert lei["identifier"] == {"scheme": "lei", "value": "5299FICT0000000000001"}
    cube = next(r for r in records if r["kind"] == "registration_entry" and r.get("object_name") == "FICTCUBE")
    assert "cospar" not in cube and "norad" not in cube  # registered by name only


def test_transfers_and_status_changes_are_dated_entries_and_the_original_is_kept(conn):
    for stage in ("2099-04-02", "2099-05-11", "2099-05-16", "2099-06-02", "2099-06-26"):
        h.acquire(conn, "documents", stage)
    entries = sorted(
        (v["record"] for v in views(conn, "cospar:2099-001A", kinds=["registration_entry"])),
        key=lambda r: r["document_date"],
    )
    assert [e["entry_kind"] for e in entries] == ["registration", "transfer_of_supervision", "re_entry_notice"]
    assert entries[1]["supervision"] == {"from": "Fictland", "to": "Republic of Examplia",
                                         "effective_date": "2099-05-01"}
    status = views(conn, "cospar:2099-003B", kinds=["registration_entry"])
    kinds = {v["record"]["entry_kind"]: v["record"] for v in status}
    assert kinds["change_of_status"]["status_change"] == {"status": "non-functional", "effective_date": "2099-05-30"}
    assert kinds["registration"]["registrant_kind"] == "intergovernmental_organisation"


def test_a_symbol_mismatch_is_rejected_and_undeclared_languages_are_refused(conn):
    source = h.fictional("documents", ["ser_e_9901.txt"])
    source["astronomy_registration"]["documents"][0]["symbol"] = "ST/SG/SER.E/0001"
    adapter = RegistrationAdapter(source, transport=fixture_transport(h.pages(["ser_e_9901.txt"])))
    page = adapter.fetch_page({"operation": "documents", "parameters": {}, "limit": 50}, cursor=None)
    assert [r["rejection"]["code"] for r in page.records] == ["symbol_mismatch"]
    source = h.fictional("documents", ["ser_e_9901.txt"])
    source["astronomy_registration"]["documents"][0]["language"] = "fr"
    adapter = RegistrationAdapter(source, transport=fixture_transport(h.pages(["ser_e_9901.txt"])))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "documents", "parameters": {}, "limit": 50}, cursor=None)
    assert caught.value.code == "schema_drift"


def test_discos_is_disabled_without_an_account(conn):
    with pytest.raises(SourcePackError) as caught:
        h.acquire(conn, "discos_objects", "2099-06-01", secret=None)
    assert caught.value.code == "credential_missing"


def test_discos_permitted_subset_is_restricted_and_citation_only_mode_stores_no_values(conn):
    receipts = h.acquire(conn, "discos_objects", "2099-06-01")
    assert receipts[0]["counts"]["out_of_scope"] == 1
    # The runtime document keeps only the citation of restricted values.
    content = json.loads(next(r for r in receipts[0]["records"] if "registration_record" in r)["content"])
    assert content["restricted"] is True and "object_name" not in content
    objects = views(conn, "discos:70001")
    kinds = {v["record"]["kind"] for v in objects}
    assert kinds == {"discos_object", "operator_assertion"}
    assert all(v["record"]["restricted"] is True for v in objects)
    h.acquire(conn, "discos_reentries", "2099-06-25")
    reentry = views(conn, "discos:70001", kinds=["reentry_report"])[0]["record"]
    assert reentry["report_kind"] == "post_event" and reentry["reported_time"] == "2099-06-21T10:07:00Z"
    other = duckdb.connect(":memory:")
    h.acquire(other, "discos_objects", "2099-06-01", mode="citation-only")
    stored = views(other, "discos:70001")
    assert {v["record"]["kind"] for v in stored} == {"discos_citation"}
    assert "object_name" not in stored[0]["record"]


def test_aerospace_predictions_are_dated_revisions_and_the_confirmed_report_supersedes_them(conn):
    h.acquire(conn, "aerospace", "2099-06-19")
    first = views(conn, "norad:99901", kinds=["reentry_report"])[0]
    assert first["record"]["report_kind"] == "prediction" and first["record"]["issued_at"] == "2099-06-19T06:00:00Z"
    h.acquire(conn, "aerospace", "2099-06-22")
    view = views(conn, "norad:99901", kinds=["reentry_report"])[0]
    assert view["record"]["report_kind"] == "post_event"
    assert view["record"]["location"] == {"text": "South Pacific Ocean", "latitude": "-45.2", "longitude": "-130.5"}
    assert [r["record"]["report_kind"] for r in view["revisions"]] == [
        "prediction", "prediction", "prediction", "post_event"]
    assert view["revisions"][0]["record"]["uncertainty"] == {"text": "± 24 hours"}
    before = views(conn, "norad:99901", kinds=["reentry_report"], cutoff=day_ms("2099-06-20"))[0]
    assert before["record"]["report_kind"] == "prediction" and len(before["later"]) == 2
