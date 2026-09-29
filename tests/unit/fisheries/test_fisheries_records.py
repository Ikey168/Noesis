"""Fisheries record schema, validation and revision/supersession semantics (FI02, #2307)."""

from __future__ import annotations

import copy
import json

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.fisheries_records import (
    METHOD_NOTE_GFW,
    FisheriesError,
    flag_code,
    imo_key,
    statement,
    validate_statement,
)
from src.kb.fisheries_store import FisheriesStore
from tests.unit.fisheries import harness as h

NS = h.NS
SRC = {"url": "https://www.iccat.int/en/vesselsrecord.asp?export=csv", "locator": "/row/0",
       "attribution": "Source: ICCAT", "list": "authorised-vessels", "snapshot_date": "2026-09-01",
       "evidence_origin": "fixture"}
VESSEL = {"key": "iccat:vessel:AT1", "kind": "vessel", "name": "SAMPLE ONE"}


def authorisation(**changes):
    published = {"register": "ICCAT Record of Vessels", "register_number": "AT1", "vessel_name": "SAMPLE ONE",
                 "flag": "ESP", "call_sign": "EAAA1", "imo": h.IMO_CLEAN, "gear": "LL", "valid_from": "2024-01-01",
                 "valid_to": "2026-12-31", **changes}
    return statement("authorisation", "iccat", VESSEL, "authorisation:AT1", published, source=SRC,
                     event="authorised", effective_from=published["valid_from"], effective_to=published["valid_to"])


def test_schema_is_draft7_and_record_types_carry_their_required_fields():
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-fisheries-record-v1.json").read_text())
    Draft7Validator.check_schema(schema)
    assert set(schema["properties"]["record_type"]["enum"]) == {"vessel", "authorisation", "listing",
                                                                "effort_aggregate", "catch_observation"}
    good = authorisation()
    assert not list(Draft7Validator(schema).iter_errors(good))
    missing = copy.deepcopy(good)
    del missing["as_published"]["gear"]
    with pytest.raises(FisheriesError):
        validate_statement(missing)
    catch = {"area": {"scheme": "fao-major-area", "code": "34"}, "species": "SKJ", "flag": "ESP",
             "period": {"from": "2022-01-01", "to": "2022-12-31", "resolution": "year"}, "quantity": "13050",
             "unit": "t", "status_flags": ["E"]}
    ok = statement("catch_observation", "fao-fishstat", {"key": "fao-fishstat:area:34", "kind": "area"},
                   "capture:ESP:SKJ:2022:Q_tlw", catch,
                   source={**SRC, "url": "https://www.fao.org/x.csv", "list": "capture-production",
                           "release": "2025.1"}, event="release")
    assert ok["as_published"]["status_flags"] == ["E"]
    with pytest.raises(FisheriesError):
        statement("catch_observation", "iccat", {"key": "iccat:area:34", "kind": "area"}, "k", catch, source=SRC)


def test_listing_reasons_are_verbatim_and_no_derived_status_is_accepted():
    listing = {"list_body": "ICCAT", "list_entry": "1", "vessel_name": "SAMPLE", "listed_on": "2020-01-01",
               "delisted_on": None, "stated_reason": "Reason as stated, verbatim."}
    subject = {"key": "iccat:iuu-entry:1", "kind": "vessel"}
    made = statement("listing", "iccat", subject, "iuu-listing:1", listing, source={**SRC, "list": "iuu-vessels"},
                     event="listed", effective_from="2020-01-01")
    assert made["as_published"]["stated_reason"] == "Reason as stated, verbatim."
    for key in ("illegal", "iuu_status", "is_compliant", "enforcement", "legal_status"):
        with pytest.raises(FisheriesError) as caught:
            statement("listing", "iccat", subject, "iuu-listing:1", {**listing, key: True},
                      source={**SRC, "list": "iuu-vessels"})
        assert caught.value.code == "invalid_record"
    combined = {**listing, "originating_listings": [{"rfmo": "ICCAT", "reference": "1"}]}
    with pytest.raises(FisheriesError):  # a compilation entry must say it is not an independent confirmation
        statement("listing", "combined-iuu", {"key": "combined-iuu:iuu-entry:1", "kind": "vessel"},
                  "iuu-listing:1", combined, source={**SRC, "list": "iuu-vessels"})


def test_effort_carries_the_apparent_fishing_method_note_and_imo_check_digits():
    effort = {"area": {"scheme": "rfmo-convention-area", "code": "ICCAT"}, "grid": None,
              "period": {"from": "2024-01-01", "to": "2024-01-31", "resolution": "month"}, "flag": "ESP",
              "gear": "trawlers", "unit": "hours", "value": 1.0, "method": "fishing hours"}
    region = {"key": "gfw:region:public-rfmo/ICCAT", "kind": "area"}
    with pytest.raises(FisheriesError):
        statement("effort_aggregate", "gfw", region, "effort:x", effort, source=SRC)
    assert statement("effort_aggregate", "gfw", region, "effort:x", {**effort, "method": METHOD_NOTE_GFW},
                     source=SRC)["as_published"]["method"] == METHOD_NOTE_GFW
    assert imo_key(h.IMO_CLEAN) == h.IMO_CLEAN and imo_key(f"IMO {h.IMO_CLEAN}") == h.IMO_CLEAN
    assert imo_key(h.IMO_MALFORMED) is None and imo_key("12345") is None
    with pytest.raises(FisheriesError):
        authorisation(imo=h.IMO_MALFORMED)
    assert authorisation(imo=h.IMO_MALFORMED, imo_malformed=True)["as_published"]["imo_malformed"]
    assert flag_code("ESP") == "ES" and flag_code("es") == "ES" and flag_code("Unknown") is None


def test_revisions_are_immutable_and_a_newer_release_supersedes_without_deleting():
    store = FisheriesStore(duckdb.connect(":memory:"), now=iter(range(1, 10_000)).__next__)
    first = store.apply(NS, authorisation())
    assert first["status"] == "created"
    assert store.apply(NS, authorisation())["status"] == "unchanged"  # replay adds nothing
    amended = store.apply(NS, authorisation(valid_to="2027-12-31"))
    assert amended["status"] == "revised" and amended["revision_no"] == 2
    revisions = store.revisions(NS, first["record_id"])
    assert [r["statement"]["as_published"]["valid_to"] for r in revisions] == ["2026-12-31", "2027-12-31"]
    assert revisions[1]["supersedes"] == revisions[0]["revision_id"]
    # Returning to the earlier content is a third revision, never a rewrite of the first.
    back = store.apply(NS, authorisation())
    assert back["revision_no"] == 3 and len(store.revisions(NS, first["record_id"])) == 3


def test_absence_from_a_later_snapshot_is_a_dated_removal_and_the_entry_stays():
    store = FisheriesStore(duckdb.connect(":memory:"), now=iter(range(1, 10_000)).__next__)
    kept = store.apply(NS, authorisation())
    kwargs = {"list_key": "iccat:authorised-vessels", "provider": "iccat", "list_kind": "authorised-vessels",
              "release": None, "url": SRC["url"], "run_id": "r", "source_id": "s", "evidence_origin": "fixture"}
    store.close_snapshot(NS, snapshot_date="2026-09-01", selection_key="a", present_record_ids=[kept["record_id"]],
                         **kwargs)
    other = store.close_snapshot(NS, snapshot_date="2026-10-01", selection_key="b", present_record_ids=[], **kwargs)
    assert other["removed"] == [] and "selection changed" in other["note"]  # never compared across selections
    closed = store.close_snapshot(NS, snapshot_date="2026-11-01", selection_key="b", present_record_ids=[],
                                  **kwargs)
    assert closed["removed"] == []  # the previous snapshot used selection b and was empty
    store.close_snapshot(NS, snapshot_date="2026-11-02", selection_key="a", present_record_ids=[kept["record_id"]],
                         **kwargs)
    removed = store.close_snapshot(NS, snapshot_date="2026-12-01", selection_key="a", present_record_ids=[],
                                   **kwargs)
    assert removed["removed"] == [kept["record_id"]]
    events = [(r["event"], r["effective_from"]) for r in store.revisions(NS, kept["record_id"])]
    assert events == [("authorised", "2024-01-01"), ("removed", "2026-12-01")]
    assert [s["snapshot_date"] for s in store.snapshots(NS)][-1] == "2026-12-01"
