"""Media metadata records: schema, typed identifiers and immutable revisions (#2225, MM02 #2482)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.kb.media_metadata import (
    MediaMetadataError,
    MediaMetadataStore,
    detect_scheme,
    normalize_identifier,
    validate_statement,
)

ROOT = Path(__file__).resolve().parents[3]
NS = "global"
SCOPES = {"knowledge:cultural:read", "knowledge:cultural:write", "namespace:global:write"}


def statement(**overrides):
    value = {
        "contract": "noesis-media-metadata-record-v1", "record_type": "edition", "source": "open-library",
        "native_id": "OL9990001M", "url": "https://openlibrary.org/books/OL9990001M",
        "revision": {"marker": "3", "basis": "ol-revision", "order": "0000000003", "date": "2026-01-10",
                     "declared": "2026-01-10T09:00:00.000000"},
        "status": "active", "redirect_to": None, "titles": [{"value": "The Glass Orchard", "language": None}],
        "names": [], "identifiers": [
            {"scheme": "olid", "value": "OL9990001M", "role": "self"},
            {"scheme": "isbn13", "value": "978-3-00-000001-0", "role": "asserted"}],
        "relations": [{"type": "edition_of", "target": {"source": "open-library", "native_id": "OL9990001W"}}],
        "creators": [], "dates": [{"kind": "published", "original": "2019"}],
        "licence": {"id": "open-library-data", "scope": "catalogue metadata", "attribution": "Open Library"},
    }
    value.update(overrides)
    return value


@pytest.fixture()
def store():
    conn = duckdb.connect(":memory:")
    clock = iter(range(1_000, 10_000_000, 1_000))
    yield MediaMetadataStore(conn, now=lambda: next(clock))
    conn.close()


def test_the_schema_validates_every_record_type_and_rejects_content_fields():
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-media-metadata-record-v1.json").read_text())
    validator = jsonschema.Draft7Validator(schema)
    assert not list(validator.iter_errors(statement()))
    for kind, source, self_scheme, native in (("work", "open-library", "olid", "OL9990001W"),
                                              ("recording", "musicbrainz", "mbid",
                                               "0c8a2f53-1111-4a4a-9b9b-000000000001"),
                                              ("authority-link", "wikidata", "wikidata", "Q999900001"),
                                              ("creator", "dnb", "gnd", "1099990001")):
        value = statement(record_type=kind, source=source, native_id=native, relations=[],
                          identifiers=[{"scheme": self_scheme, "value": native, "role": "self"}])
        assert validate_statement(value)["record_type"] == kind
    with pytest.raises(MediaMetadataError):
        validate_statement(statement(full_text="Once upon a time"))  # no content storage
    with pytest.raises(MediaMetadataError):
        validate_statement(statement(record_type="recording"))  # Open Library has no recordings
    with pytest.raises(MediaMetadataError):
        validate_statement(statement(status="redirected"))  # a redirect names its target
    with pytest.raises(MediaMetadataError):
        validate_statement(statement(identifiers=[{"scheme": "isbn13", "value": "9783000000010",
                                                   "role": "asserted"}]))  # no self key
    with pytest.raises(MediaMetadataError):
        validate_statement(statement(relations=[{"type": "has_edition",
                                                 "target": {"source": "open-library", "native_id": "OL1M"}}]))


@pytest.mark.parametrize(("scheme", "value", "key"), [
    ("isbn13", "978-3-00-000001-0", "isbn:9783000000010"),
    ("isbn10", "3-00-000001-1", "isbn:9783000000010"),  # ISBN-10 meets its ISBN-13 form
    ("isbn13", "979-10-0000001-5", "isbn:9791000000015"),
    ("isrc", "ZZ-FIX-26-00001", "isrc:ZZFIX2600001"),
    ("iswc", "T-000.000.001-0", "iswc:T0000000010"),
    ("mbid", "0C8A2F53-1111-4A4A-9B9B-000000000001", "mbid:0c8a2f53-1111-4a4a-9b9b-000000000001"),
    ("wikidata", "http://www.wikidata.org/entity/Q999900001", "wikidata:Q999900001"),
    ("gnd", "(DE-588)1099990001", "gnd:1099990001"),
    ("lcnaf", "n  2099000001", "lccn:n2099000001"),
    ("lccn", "2019-1234", "lccn:2019001234"),
    ("olid", "/works/OL9990001W", "olid:OL9990001W"),
])
def test_identifiers_normalize_with_checksums(scheme, value, key):
    result = normalize_identifier(scheme, value)
    assert result["valid"] and result["key"] == key and result["value"] == value.strip()


@pytest.mark.parametrize(("scheme", "value"), [
    ("isbn13", "978-3-00-000001-1"), ("isbn10", "3-00-000001-2"), ("isbn13", "977-3-00-000001-0"),
    ("isrc", "ZZ-FIX-26-0001"), ("isrc", "Z1-FIX-26-00001"), ("iswc", "T-000.000.001-1"), ("mbid", "not-a-uuid"),
    ("wikidata", "P212"), ("olid", "OL0W"),
])
def test_invalid_identifiers_stay_visible_as_invalid(scheme, value):
    result = normalize_identifier(scheme, value)
    assert result == {**result, "valid": False, "key": None} and result["reason"]


def test_bare_identifiers_are_detected():
    assert detect_scheme("9783000000010") == "isbn13"
    assert detect_scheme("3000000011") == "isbn10"
    assert detect_scheme("ZZFIX2600001") == "isrc"
    assert detect_scheme("0c8a2f53-1111-4a4a-9b9b-000000000001") == "mbid"
    assert detect_scheme("Q999900001") == "wikidata"
    assert detect_scheme("OL9990001W") == "olid"
    assert detect_scheme("n2099000001") == "lccn"


def test_revisions_are_immutable_and_keyed_by_the_provider_marker(store):
    first = store.apply(NS, statement())
    assert first["status"] == "created"
    assert store.apply(NS, statement())["status"] == "unchanged"  # a replayed marker adds nothing
    later = statement(revision={"marker": "5", "basis": "ol-revision", "order": "0000000005", "date": "2026-03-01",
                                "declared": None}, titles=[{"value": "The Glass Orchard (2nd ed.)", "language": None}])
    assert store.apply(NS, later)["status"] == "revised"
    earlier = statement(revision={"marker": "4", "basis": "ol-revision", "order": "0000000004", "date": "2026-02-01",
                                  "declared": None})
    assert store.apply(NS, earlier)["status"] == "history"  # a late older revision never becomes current
    record_id = first["record_id"]
    current = store.current_revision(NS, record_id)
    assert current["marker"] == "5"
    assert [r["marker"] for r in store.revisions(NS, record_id)] == ["3", "5", "4"]
    offered = copy.deepcopy(later)
    offered["titles"] = [{"value": "Rewritten", "language": None}]
    conflict = store.apply(NS, offered)
    assert conflict["status"] == "conflict" and store.conflicts(NS, record_id)
    assert store.statement(NS, current["revision_id"])["titles"][0]["value"] == "The Glass Orchard (2nd ed.)"
    at, known = store.revision_as_of(NS, record_id, __import__("datetime").date(2026, 2, 15))
    assert at["marker"] == "4" and [r["marker"] for r in known] == ["3", "4"]


def test_an_edition_never_becomes_a_work(store):
    store.apply(NS, statement())
    work = statement(record_type="work", relations=[], revision={
        "marker": "9", "basis": "ol-revision", "order": "0000000009", "date": "2026-05-01", "declared": None})
    with pytest.raises(MediaMetadataError) as caught:
        store.apply(NS, work)
    assert caught.value.code == "level_change"


def test_identifiers_are_stored_per_revision_with_validity(store):
    bad = statement(identifiers=[{"scheme": "olid", "value": "OL9990001M", "role": "self"},
                                 {"scheme": "isbn13", "value": "9783000000011", "role": "asserted"}])
    result = store.apply(NS, bad)
    rows = store.identifiers(NS, result["revision_id"])
    assert [(r["scheme"], r["valid"], r["key"]) for r in rows] == [
        ("olid", True, "olid:OL9990001M"), ("isbn13", False, None)]


def test_redirects_are_revisions_of_the_old_record_and_identity_history(store):
    old, new = "0c8a2f53-1111-4a4a-9b9b-000000000001", "0c8a2f53-1111-4a4a-9b9b-000000000002"
    base = {"record_type": "recording", "source": "musicbrainz", "relations": [], "dates": [],
            "licence": {"id": "cc0-1.0", "scope": "MusicBrainz core data", "attribution": None}}
    store.apply(NS, statement(**base, native_id=old, identifiers=[{"scheme": "mbid", "value": old, "role": "self"}],
                              revision={"marker": "mb-core:a", "basis": "mb-core-digest", "order": None,
                                        "date": None, "declared": None}))
    stub = statement(**base, native_id=old, status="redirected", redirect_to=new,
                     identifiers=[{"scheme": "mbid", "value": old, "role": "self"}],
                     revision={"marker": f"redirect:{new}", "basis": "provider-redirect", "order": None,
                               "date": None, "declared": None})
    assert store.apply(NS, stub)["status"] == "revised"
    assert store.follow(NS, "musicbrainz", old.upper()) == [old, new]
    redirect = store.redirects(NS, "musicbrainz", old)[0]
    assert redirect["kind"] == "redirect" and redirect["decision_id"].startswith("entity-decision:")
    decision = store.conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                                  [redirect["decision_id"]]).fetchone()
    assert decision == ("redirect",)


def test_dump_slices_are_bounded_to_named_keys(store):
    body = {"key": "/works/OL9990001W", "type": {"key": "/type/work"}, "revision": 2,
            "last_modified": {"type": "/type/datetime", "value": "2026-01-01T00:00:00.000000"},
            "title": "The Glass Orchard", "covers": [1]}
    line = "\t".join(["/type/work", "/works/OL9990001W", "2", "2026-01-01T00:00:00.000000", json.dumps(body)])
    receipt = store.import_dump_slice(NS, line + "\n", dump_name="ol_dump_works_2026-08-31", dump_date="2026-08-31",
                                      keys=["/works/OL9990001W", "/works/OL9990002W"], scopes=SCOPES)
    assert receipt["found_keys"] == ["/works/OL9990001W"] and receipt["missing_keys"] == ["/works/OL9990002W"]
    assert receipt["counts"]["created"] == 1 and len(receipt["slice_sha256"]) == 64
    with pytest.raises(MediaMetadataError) as caught:
        store.import_dump_slice(NS, line, dump_name="ol_dump_works_2026-08-31", dump_date="2026-08-31",
                                keys=["/works/OL1W"], scopes=SCOPES)
    assert caught.value.code == "unbounded_slice"
    with pytest.raises(MediaMetadataError) as caught:
        store.import_dump_slice(NS, line, dump_name="full-dump", dump_date="2026-08-31",
                                keys=["/works/OL9990001W"], scopes=SCOPES)
    assert caught.value.code == "invalid_slice"
