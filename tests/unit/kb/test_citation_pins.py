"""Pin citations from any pack to an archived capture and export the pin (#2298, WA09)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.kb.citation_preservation import CitationPreservationError, CitationPreservationStore
from tests.unit import web_archive_harness as h

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"
# Two packs cite the same page under their own citation ids; pins link by citation only.
LEGAL_CITATION = "legal:docket:70001:entry:3"
ENERGY_CITATION = "energy:series:de-load:note:1"


def _seeded():
    conn = h.connect()
    client = h.client(conn)
    client.resolve(h.URL, request_id="agg", crawls=[h.CRAWL])
    client.resolve_archive(h.URL, "internet-archive", request_id="ia")
    policy = client.store.register_policy(h.NS, "policy:web", "1", principal_id="curator", scopes=h.SCOPES)
    for citation in (LEGAL_CITATION, ENERGY_CITATION):
        client.store.capture(h.NS, policy["policy_id"], citation, h.URL, content="Quarterly report text.",
                             principal_id="curator", scopes=h.SCOPES)
    return conn, client


def _capture(client, archive_id, memento_datetime):
    return next(c for c in client.store.captures_for_url(h.NS, h.URL, scopes=h.SCOPES)
                if c["archive_id"] == archive_id and c["memento_datetime"] == memento_datetime)


def test_pins_from_two_packs_export_next_to_the_untouched_original_url():
    conn, client = _seeded()
    store = client.store
    unpinned = store.export(h.NS, [LEGAL_CITATION, ENERGY_CITATION], scopes=h.SCOPES)
    ia = _capture(client, "internet-archive", "2024-02-15T09:30:00Z")
    ukwa = _capture(client, "uk-web-archive", "2024-01-20T10:00:00Z")
    legal = store.pin_citation(h.NS, LEGAL_CITATION, ia["capture_id"], h.URL, principal_id="alice",
                               scopes=h.SCOPES)
    energy = store.pin_citation(h.NS, ENERGY_CITATION, ukwa["capture_id"], h.URL, principal_id="bob",
                                scopes=h.SCOPES)
    assert legal["citation_id"] != energy["citation_id"] and legal["capture_id"] != energy["capture_id"]
    exported = store.export(h.NS, [LEGAL_CITATION, ENERGY_CITATION], scopes=h.SCOPES)
    Draft202012Validator(json.loads((SCHEMAS / "noesis-citation-export-v1.json").read_text())).validate(exported)
    by_id = {item["citation_id"]: item for item in exported["items"]}
    pin = by_id[LEGAL_CITATION]["archive_pin"]
    assert pin["cited_url"] == h.URL and pin["archive_id"] == "internet-archive"
    assert pin["uri_m"] == h.IA_MEMENTO and pin["memento_datetime"] == "2024-02-15T09:30:00Z"
    assert pin["digests"][0]["basis"] == "published"
    assert by_id[ENERGY_CITATION]["archive_pin"]["archive_id"] == "uk-web-archive"
    # The original citation snapshot keeps its URL.
    assert by_id[LEGAL_CITATION]["snapshots"][0]["source_url"] == h.URL
    for revision in pin["revisions"]:
        Draft202012Validator(json.loads((SCHEMAS / "noesis-citation-pin-v1.json").read_text())).validate(revision)
    assert unpinned["export_hash"] != exported["export_hash"]


def test_bundles_without_pins_are_unchanged():
    conn, client = _seeded()
    before = client.store.export(h.NS, [LEGAL_CITATION], scopes=h.SCOPES)
    ia = _capture(client, "internet-archive", "2024-02-15T09:30:00Z")
    client.store.pin_citation(h.NS, ENERGY_CITATION, ia["capture_id"], h.URL, principal_id="alice",
                              scopes=h.SCOPES)
    after = client.store.export(h.NS, [LEGAL_CITATION], scopes=h.SCOPES)
    assert after == before and "archive_pin" not in after["items"][0]
    # A store that never saw web-archive tables exports as before.
    import duckdb

    fresh = duckdb.connect()
    CitationPreservationStore(fresh).register_policy(h.NS, "p", "1", principal_id="c", scopes=h.SCOPES)
    fresh.execute("DROP TABLE citation_pins")
    assert CitationPreservationStore(fresh, initialize=False).export(h.NS, ["x"], scopes=h.SCOPES)["items"]


def test_repinning_adds_a_revision_and_earlier_pins_remain_inspectable():
    conn, client = _seeded()
    store = client.store
    first = store.pin_citation(h.NS, LEGAL_CITATION, _capture(client, "internet-archive",
                                                              "2024-02-15T09:30:00Z")["capture_id"],
                               h.URL, principal_id="alice", scopes=h.SCOPES)
    second = store.pin_citation(h.NS, LEGAL_CITATION, _capture(client, "common-crawl",
                                                               "2024-02-20T14:30:00Z")["capture_id"],
                                h.URL, principal_id="bob", scopes=h.SCOPES, reason="crawl record has a WARC locator")
    assert second["revision"] == 2 and second["supersedes_pin_id"] == first["pin_id"]
    revisions = store.pins(h.NS, LEGAL_CITATION, scopes=h.SCOPES)
    assert [p["pin_id"] for p in revisions] == [first["pin_id"], second["pin_id"]]
    assert revisions[0]["capture"]["archive_id"] == "internet-archive"
    exported = store.export(h.NS, [LEGAL_CITATION], scopes=h.SCOPES)["items"][0]["archive_pin"]
    assert exported["revision"] == 2 and exported["archive_id"] == "common-crawl"
    with pytest.raises(CitationPreservationError):
        store.pin_citation(h.NS, LEGAL_CITATION, "web-archive-capture:" + "0" * 24, h.URL, principal_id="a",
                           scopes=h.SCOPES)
