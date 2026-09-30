"""Archived-capture, TimeMap snapshot and citation-pin records (#2246, WA02)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.citation_preservation import (
    CAPTURE_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    CitationPreservationError,
    CitationPreservationStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"
NS = "research"
URL = "https://example.org/report"
ALL = {CAPTURE_SCOPE, READ_SCOPE, WRITE_SCOPE}


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _capture(**changes):
    value = {
        "archive_id": "internet-archive",
        "archive_kind": "memento-archive",
        "resolver": "timetravel",
        "uri_r": URL,
        "uri_m": "https://web.archive.org/web/20240102030405/https://example.org/report",
        "memento_datetime": "Tue, 02 Jan 2024 03:04:05 GMT",
        "status": 200,
        "mimetype": "text/html",
        "digests": [],
        "receipt": {"request_id": "req-1", "adapter": "test", "evidence_origin": "fixture"},
    }
    value.update(changes)
    return value


@pytest.fixture()
def store():
    clock = iter(range(1_000, 10_000_000, 1_000))
    return CitationPreservationStore(duckdb.connect(), now=lambda: next(clock))


def test_capture_record_round_trips_offline_and_is_idempotent_on_archive_and_uri_m(store):
    first = store.record_capture(NS, _capture(), principal_id="curator", scopes=ALL)
    _validate("noesis-web-archive-capture-v1.json", first)
    assert first["memento_datetime"] == "2024-01-02T03:04:05Z"
    assert first["archive_id"] == "internet-archive" and first["resolver"] == "timetravel"
    again = store.record_capture(NS, _capture(), principal_id="curator", scopes=ALL)
    assert again["idempotent"] is True and again["capture_id"] == first["capture_id"]
    # The same memento with its CDX digest enriches the record without rewriting it.
    digest = {"algorithm": "sha1-base32", "value": "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", "basis": "published"}
    enriched = store.record_capture(NS, _capture(digests=[digest], receipt={"request_id": "req-2"}),
                                    principal_id="curator", scopes=ALL)
    assert enriched["enriched"] and enriched["digests"] == [digest]
    assert enriched["enrichments"][0]["fields"] == ["digests"]
    stored = store.capture_record(NS, first["capture_id"], scopes={READ_SCOPE})
    _validate("noesis-web-archive-capture-v1.json", stored)
    assert stored["receipt"]["request_id"] == "req-1"
    assert store.conn.execute("SELECT count(*) FROM web_archive_captures").fetchone()[0] == 1


def test_capture_digests_are_labelled_and_receipts_required(store):
    with pytest.raises(CitationPreservationError) as unlabelled:
        store.record_capture(NS, _capture(digests=[{"algorithm": "sha1", "value": "x"}]), principal_id="c",
                             scopes=ALL)
    assert unlabelled.value.code == "invalid_capture"
    with pytest.raises(CitationPreservationError):
        store.record_capture(NS, _capture(receipt={}), principal_id="c", scopes=ALL)
    with pytest.raises(CitationPreservationError) as denied:
        store.record_capture(NS, _capture(), principal_id="c", scopes={READ_SCOPE})
    assert denied.value.code == "unauthorized"


def test_unchanged_timemap_adds_nothing_and_a_change_adds_a_snapshot(store):
    capture = store.record_capture(NS, _capture(), principal_id="c", scopes=ALL)
    first = store.record_timemap(NS, URL, "internet-archive", resolver="internet-archive", outcome="captures",
                                 capture_ids=[capture["capture_id"]], retrieved_at_ms=5,
                                 principal_id="c", scopes=ALL)
    _validate("noesis-web-archive-timemap-v1.json", first)
    same = store.record_timemap(NS, URL, "internet-archive", resolver="internet-archive", outcome="captures",
                                capture_ids=[capture["capture_id"]], retrieved_at_ms=9,
                                principal_id="c", scopes=ALL)
    assert same["unchanged"] is True and same["timemap_id"] == first["timemap_id"]
    down = store.record_timemap(NS, URL, "internet-archive", resolver="internet-archive",
                                outcome="archive_unavailable", retrieved_at_ms=12, principal_id="c", scopes=ALL)
    assert down["previous_timemap_id"] == first["timemap_id"]
    assert store.conn.execute("SELECT count(*) FROM web_archive_timemaps").fetchone()[0] == 2
    (latest,) = store.timemaps_for_url(NS, URL, scopes={READ_SCOPE})
    assert latest["outcome"] == "archive_unavailable"
    with pytest.raises(CitationPreservationError):
        store.record_timemap(NS, URL, "x", resolver="x", outcome="guessed", principal_id="c", scopes=ALL)


def test_citation_pin_binds_a_capture_and_never_rewrites_the_cited_url(store):
    capture = store.record_capture(NS, _capture(), principal_id="c", scopes=ALL)
    pin = store.pin_citation(NS, "cite:1", capture["capture_id"], URL, principal_id="alice", scopes=ALL)
    _validate("noesis-citation-pin-v1.json", pin)
    assert pin["cited_url"] == URL and pin["original_url_unchanged"] is True
    assert pin["principal_id"] == "alice" and pin["revision"] == 1 and pin["match"]["match_kind"] == "exact"
    assert store.pin_citation(NS, "cite:1", capture["capture_id"], URL, principal_id="alice",
                              scopes=ALL)["idempotent"] is True
    other = store.record_capture(NS, _capture(uri_r="https://www.example.org/report/",
                                              uri_m="https://web.archive.org/web/2024/https://www.example.org/report/"),
                                 principal_id="c", scopes=ALL)
    with pytest.raises(CitationPreservationError) as needs_match:
        store.pin_citation(NS, "cite:1", other["capture_id"], URL, principal_id="alice", scopes=ALL)
    assert needs_match.value.code == "match_required"
    with pytest.raises(CitationPreservationError):
        store.pin_citation(NS, "cite:1", capture["capture_id"], URL, principal_id="r", scopes={READ_SCOPE})
