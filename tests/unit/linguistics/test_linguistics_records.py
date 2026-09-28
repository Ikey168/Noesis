"""Linguistic record model, normalisation and revision-append store (LG02, #2180)."""

from __future__ import annotations

import json
import unicodedata

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb import linguistics_records as lr
from src.kb.linguistics_store import LinguisticsStore

NS = "ling-test"
CC0 = {"id": "CC0-1.0", "attribution": "Wikidata, CC0", "share_alike": False}


def definition(text, revision, *, revision_date=None, sense="L1-S1", language="en"):
    source = {
        "source_id": f"{sense}:{language}",
        "revision": str(revision),
        "licence": CC0,
    }
    if revision_date:
        source["revision_date"] = revision_date
    return {
        "contract": lr.CONTRACT,
        "kind": "definition_revision",
        "provider": "wikidata-lexemes",
        "source": source,
        "body": {
            "sense": f"sense:wikidata-lexemes:{sense}",
            "language": language,
            "text": text,
        },
    }


@pytest.fixture()
def store():
    return LinguisticsStore(duckdb.connect(":memory:"), now=lambda: 1_000)


def test_nfc_folding_script_and_language_tags_are_explicit():
    decomposed = "Café"
    assert lr.nfc(decomposed) == "Café" and unicodedata.is_normalized(
        "NFC", lr.nfc(decomposed)
    )
    assert lr.comparable("  Café \n au   lait ") == "Café au lait"
    assert lr.fold("STRASSE") == lr.fold("Straße") == "strasse"
    assert lr.fold("Ǆ") == "ǆ"
    assert lr.script_of("Wörterbuch")["script"] == "Latn"
    assert lr.script_of("словарь")["script"] == "Cyrl"
    assert lr.script_of("λέξη")["script"] == "Grek"
    assert lr.script_of("辞書")["script"] == "Hani"
    assert lr.script_of("123 - !")["script"] == "Zyyy"
    mixed = lr.script_of("Moskva Москва")
    assert mixed["mixed"] is True and set(mixed["scripts"]) == {"Latn", "Cyrl"}
    assert lr.script_of("é")["script"] == "Latn"  # combining marks are ignored
    assert lr.language_tag("SR_latn_rs") == "sr-Latn-RS"
    assert lr.language_tag("de-x-Q123") == "de-x-Q123"
    assert lr.language_tag("") == "und"
    rep = lr.representation("Café", "FR")
    assert rep == {"text": "Café", "nfc": "Café", "language": "fr", "script": "Latn"}


def test_dates_are_iso_and_compared_as_dates():
    assert lr.iso_date("2030-01-05") == "2030-01-05"
    assert lr.iso_date("2030-01-05T10:00:00+02:00") == "2030-01-05T08:00:00Z"
    assert lr.iso_date(None) is None
    assert (
        lr.date_key("2030-01-05")
        < lr.date_key("2030-01-05T00:00:01Z")
        < lr.date_key("2030-01-05", end_of_day=True)
    )
    with pytest.raises(lr.LinguisticsError):
        lr.iso_date("5 January 2030")
    assert lr.revision_order("9") < lr.revision_order("10")
    assert lr.revision_order("v5.9") < lr.revision_order("v5.10")


def test_leipzig_gloss_validation_uses_the_vocabulary_and_reports_unknown_labels():
    ok = lr.validate_gloss("dog-PL see-PST-3SG=Q 1PL.EXCL NPST")
    assert ok["status"] == "validated" and ok["categories"] == [
        "PL",
        "PST",
        "3SG",
        "Q",
        "1PL",
        "EXCL",
        "NPST",
    ]
    bad = lr.validate_gloss("house-XYZ go-PST")
    assert bad["status"] == "unvalidated" and bad["unknown"] == ["XYZ"]
    assert (
        len(lr.LEIPZIG_ABBREVIATIONS) > 80
        and lr.LEIPZIG_ABBREVIATIONS["ERG"] == "ergative"
    )


def test_validation_refuses_machine_output_null_strings_and_bad_identifiers():
    record = definition("a small domestic animal", 7)
    assert lr.validate_record(record)["body"]["text_nfc"] == "a small domestic animal"
    for patch in (
        {"producer": {"name": "mt"}},
        {"machine_translated": True},
        {"translated_text": "x"},
        {"text_origin": "machine"},
    ):
        broken = json.loads(json.dumps(record))
        broken["body"].update(patch)
        with pytest.raises(lr.LinguisticsError) as caught:
            lr.validate_record(broken)
        assert caught.value.code == "machine_output_refused"
    for patch in ({"note": "None"}, {"note": None}):
        broken = json.loads(json.dumps(record))
        broken["body"].update(patch)
        with pytest.raises(lr.LinguisticsError):
            lr.validate_record(broken)
    languoid = {
        "contract": lr.CONTRACT,
        "kind": "languoid",
        "provider": "glottolog",
        "source": {"source_id": "BAD", "revision": "v1", "licence": CC0},
        "body": {"glottocode": "BAD", "name": "x", "level": "language"},
    }
    with pytest.raises(lr.LinguisticsError):
        lr.validate_record(languoid)
    no_licence = json.loads(json.dumps(record))
    no_licence["source"]["licence"].pop("attribution")
    with pytest.raises(lr.LinguisticsError):
        lr.validate_record(no_licence)


def test_schema_accepts_records_and_rejects_machine_output():
    validator = Draft7Validator(lr.schema())
    Draft7Validator.check_schema(lr.schema())
    assert not list(validator.iter_errors(definition("text", 1)))
    broken = definition("text", 1)
    broken["body"]["producer"] = {"name": "mt"}
    assert list(validator.iter_errors(broken))


def test_round_trip_and_revision_append(store):
    counts = store.apply(
        NS,
        [definition("a small cat", 10, revision_date="2030-01-01")],
        run_id="r1",
        observed_at_ms=1_000,
    )
    assert counts["new"] == 1
    key = "definition_revision:wikidata-lexemes:L1-S1:en"
    current = store.current(NS, key)
    assert (
        current["body"]["text"] == "a small cat"
        and current["source"]["retrieved_at"] == "1970-01-01T00:00:01Z"
    )
    assert current["source"]["licence"] == CC0
    # re-acquiring the same revision adds nothing
    again = store.apply(
        NS,
        [definition("a small cat", 10, revision_date="2030-01-01")],
        run_id="r2",
        observed_at_ms=2_000,
    )
    assert again["duplicate"] == 1 and store.snapshot(NS)["sightings"] == 1
    # a new revision with the same (NFD, re-spaced) text adds a sighting and no revision
    same = store.apply(
        NS,
        [definition("a  small cat", 11, revision_date="2030-02-01")],
        run_id="r3",
        observed_at_ms=3_000,
    )
    assert same["unchanged"] == 1 and len(store.history(NS, key)) == 1
    assert store.history(NS, key)[0]["restated_in"] == ["11"]
    # a changed definition appends a revision
    revised = store.apply(
        NS,
        [definition("a small feline", 12, revision_date="2030-03-01")],
        run_id="r4",
        observed_at_ms=4_000,
    )
    assert revised["revised"] == 1 and revised["changed"] == [key]
    history = store.history(NS, key)
    assert [h["body"]["text"] for h in history] == ["a small cat", "a small feline"]
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    # as-of selection by the source's own date
    assert store.current(NS, key, as_of="2030-02-15")["body"]["text"] == "a small cat"
    assert (
        store.current(NS, key, as_of="2030-03-01")["body"]["text"] == "a small feline"
    )
    assert store.current(NS, key, as_of="2029-12-31") is None
    # acquisition cutoff: only sightings acquired by then
    assert store.current(NS, key, acquired_by_ms=3_500)["body"]["text"] == "a small cat"


def test_reversion_is_a_new_revision_and_late_older_data_is_history(store):
    key = "definition_revision:wikidata-lexemes:L1-S1:en"
    store.apply(
        NS,
        [definition("A", 1, revision_date="2030-01-01")],
        run_id="a",
        observed_at_ms=1,
    )
    store.apply(
        NS,
        [definition("B", 3, revision_date="2030-03-01")],
        run_id="b",
        observed_at_ms=2,
    )
    back = store.apply(
        NS,
        [definition("A", 5, revision_date="2030-05-01")],
        run_id="c",
        observed_at_ms=3,
    )
    assert back["revised"] == 1
    ids_before = [h["revision_id"] for h in store.history(NS, key)]
    assert [h["body"]["text"] for h in store.history(NS, key)] == ["A", "B", "A"]
    late = store.apply(
        NS,
        [definition("C", 2, revision_date="2030-02-01")],
        run_id="d",
        observed_at_ms=4,
    )
    assert late["historical"] == 1 and late["changed"] == []
    history = store.history(NS, key)
    assert [h["body"]["text"] for h in history] == ["A", "C", "B", "A"]
    assert set(ids_before) <= {
        h["revision_id"] for h in history
    }  # keys never change when older data arrives
    assert store.current(NS, key)["body"]["text"] == "A"


def test_arrival_order_does_not_change_the_chain():
    records = [
        definition("A", 1, revision_date="2030-01-01"),
        definition("A", 2, revision_date="2030-02-01"),
        definition("B", 3, revision_date="2030-03-01"),
    ]
    chains = []
    for order in (records, list(reversed(records))):
        store = LinguisticsStore(duckdb.connect(":memory:"))
        for index, record in enumerate(order):
            store.apply(NS, [record], run_id=f"r{index}", observed_at_ms=100)
        chains.append(
            [
                (h["revision_id"], h["body"]["text"])
                for h in store.history(
                    NS, "definition_revision:wikidata-lexemes:L1-S1:en"
                )
            ]
        )
    assert chains[0] == chains[1] and [text for _, text in chains[0]] == ["A", "B"]


def test_undated_sources_fall_back_to_observation_order(store):
    key = "definition_revision:wikidata-lexemes:L1-S1:en"
    store.apply(
        NS, [definition("first", "x")], run_id="a", observed_at_ms=86_400_000 * 2
    )
    store.apply(
        NS, [definition("second", "y")], run_id="b", observed_at_ms=86_400_000 * 3
    )
    assert store.current(NS, key)["body"]["text"] == "second"


def test_not_ready_before_any_source_ran():
    store = LinguisticsStore(duckdb.connect(":memory:"), initialize=False)
    with pytest.raises(lr.LinguisticsError) as caught:
        store.require_ready()
    assert caught.value.code == "not_ready"
    assert store.keys(NS) == [] and store.snapshot(NS)["sightings"] == 0


def test_schema_and_glossing_vocabulary_register_in_the_shared_registry():
    conn = duckdb.connect(":memory:")
    scopes = {"knowledge:schema:register", "knowledge:schema:read"}
    first = lr.register_schemas(conn, principal_id="svc", scopes=scopes)
    again = lr.register_schemas(conn, principal_id="svc", scopes=scopes)
    assert first[0]["module_id"] == again[0]["module_id"]
    published = lr.publish_glossing_vocabulary(conn, principal_id="svc")
    assert published["concepts"] == len(lr.LEIPZIG_ABBREVIATIONS)
    vocabulary = lr.glossing_vocabulary(conn)
    assert (
        vocabulary["PST"] == "past"
        and lr.validate_gloss("go-PST", vocabulary)["status"] == "validated"
    )
