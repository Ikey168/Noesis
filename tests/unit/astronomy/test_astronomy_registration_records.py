"""Registration, operator-assertion and re-entry records and their revisioned store (#2224, SO02)."""

from __future__ import annotations

import json

import duckdb
import jsonschema
import pytest

from src.kb.astronomy_records import AstronomyError, day_ms
from src.kb.astronomy_registration import (
    NO_UN_REGISTRATION,
    RegistrationStore,
    identifier_keys,
    object_keys,
    schema,
    validate_record,
)

NS = "astronomy"


def entry(**over):
    record = {
        "kind": "registration_entry",
        "source": {"provider": "unoosa-registration-documents", "source_record_id": "ST/SG/SER.E/9901#para 2"},
        "entry_kind": "registration",
        "cospar": "2099-001A",
        "norad": "99001",
        "object_name": "FICTSAT-1",
        "registering_state": "Fictland",
        "registrant_kind": "state",
        "un_document": "ST/SG/SER.E/9901",
        "document_date": "2099-03-01",
        "document_locator": {"paragraph": "2"},
        "language": "en",
        "quotation": "2. International designator: 2099-001A ...",
        "registered_orbit": {"nodal_period": {"value": "95.1", "unit": "minutes"}},
    }
    record.update(over)
    return record


def reentry(issued, kind="prediction", **over):
    record = {
        "kind": "reentry_report",
        "source": {"provider": "aerospace-reentry", "source_record_id": "99002"},
        "report_kind": kind,
        "issued_at": issued,
        "norad": "99002",
        "cospar": "2099-001B",
        "uncertainty": {"text": "± 6 hours"},
        "location": {"text": "South Pacific Ocean"},
    }
    record.update(over)
    return record


def test_valid_records_match_the_json_schema_and_keep_locators():
    for record in (
        entry(),
        reentry("2099-07-30T12:00:00Z", reported_time="2099-08-01T10:00:00Z"),
        {
            "kind": "operator_assertion",
            "source": {"provider": "unoosa-registration-documents", "source_record_id": "x"},
            "operator_name": "Fictland Space Agency",
            "role": "operator",
            "cospar": "2099-001A",
        },
        {
            "kind": "discos_object",
            "source": {"provider": "esa-discos", "source_record_id": "70001"},
            "discos_id": "70001",
            "restricted": True,
            "cospar": "2099-001A",
        },
    ):
        validated = validate_record(record)
        jsonschema.validate(validated, schema())
    assert validate_record(entry())["document_locator"] == {"paragraph": "2"}


@pytest.mark.parametrize(
    "bad",
    [
        entry(quotation=None),  # a document entry keeps its quotation
        entry(language="de"),
        entry(un_document="not a symbol"),
        entry(operator_military="yes"),  # no attribution beyond the source
        entry(entry_kind="index_entry"),  # index entries come from the index provider
        entry(entry_kind="transfer_of_supervision"),  # needs supervision.to
        entry(cospar="2099-1a"),
        reentry("2099-07-30T12:00:00Z", predicted_by_noesis="x"),
        reentry("2099-07-30T12:00:00Z", report_kind="guess"),
        {"kind": "discos_object", "source": {"provider": "esa-discos", "source_record_id": "1"}, "discos_id": "1"},
    ],
)
def test_invalid_records_are_refused(bad):
    with pytest.raises(AstronomyError):
        validate_record(bad)


def test_linkage_keys_by_cospar_norad_and_name():
    keys = object_keys(validate_record(entry()))
    assert {"cospar:2099-001A", "norad:99001", "name:fictsat1", "state:fictland", "doc:ST/SG/SER.E/9901"} <= set(
        keys
    )
    assert identifier_keys("2099-001A") == ["cospar:2099-001A"]
    assert identifier_keys("99001") == ["norad:99001"]
    assert identifier_keys("Fictsat 1") == ["name:fictsat1"]
    assert NO_UN_REGISTRATION == "no UN registration on record"


def test_revision_semantics_predictions_then_post_event_and_late_history():
    conn = duckdb.connect(":memory:")
    store = RegistrationStore(conn)
    first = store.apply(NS, [reentry("2099-07-30T12:00:00Z")], run_id="r1", observed_at_ms=day_ms("2099-07-30"))
    assert first["inserted"] == 1
    store.apply(NS, [reentry("2099-07-31T12:00:00Z")], run_id="r2", observed_at_ms=day_ms("2099-07-31"))
    post = reentry("2099-08-02T00:00:00Z", kind="post_event", reported_time="2099-08-01T10:07:00Z")
    store.apply(NS, [post], run_id="r3", observed_at_ms=day_ms("2099-08-02"))
    # A late copy of the first prediction adds nothing; an unseen older prediction is history.
    again = store.apply(NS, [reentry("2099-07-30T12:00:00Z")], run_id="r4", observed_at_ms=day_ms("2099-08-03"))
    assert again["unchanged"] == 1
    late = store.apply(NS, [reentry("2099-07-29T00:00:00Z")], run_id="r5", observed_at_ms=day_ms("2099-08-04"))
    assert late["history"] == 1
    view = store.visible(NS, keys=["norad:99002"])["records"][0]
    assert view["record"]["report_kind"] == "post_event"
    assert [r["record"]["report_kind"] for r in view["revisions"]] == [
        "prediction", "prediction", "prediction", "post_event"]
    before = store.visible(NS, keys=["norad:99002"], public_cutoff_ms=day_ms("2099-07-31"))["records"][0]
    assert before["record"]["report_kind"] == "prediction" and before["later"]
    early = store.visible(NS, keys=["norad:99002"], public_cutoff_ms=day_ms("2099-07-01"))
    assert not early["records"] and early["pending"]
    assert json.loads(json.dumps(store.history(NS, view["record_id"])))[-1]["change"] == "history"


def test_unchanged_reacquisition_adds_nothing_and_generation_follows_revisions():
    conn = duckdb.connect(":memory:")
    store = RegistrationStore(conn)
    store.apply(NS, [entry()], run_id="r1", observed_at_ms=1)
    generation = store.generation(NS)
    assert store.apply(NS, [entry()], run_id="r2", observed_at_ms=2)["unchanged"] == 1
    assert store.generation(NS) == generation
    store.apply(NS, [entry(status="in orbit")], run_id="r3", observed_at_ms=3)
    assert store.generation(NS) != generation
