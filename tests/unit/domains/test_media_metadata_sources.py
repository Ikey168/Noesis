"""Open Library, MusicBrainz, Wikidata, DNB and Library of Congress acquisition (#2225, MM03-MM06).

Replays the synthetic ``tests/fixtures/source_packs/media-*.json`` responses
through :class:`MediaMetadataAdapter` and the source-pack runtime into the
media metadata store. Offline only.
"""

from __future__ import annotations

import json

import pytest

from src.ingestion.media_metadata_sources import (
    EXCLUDED_RECORD_CLASSES,
    EXCLUDED_SOURCES,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    MediaMetadataAdapter,
    fixture_transport,
    parse_marc,
    parse_musicbrainz,
    parse_open_library,
)
from src.ingestion.source_packs import SUPPORTED_CONNECTORS, SourcePackConformance, SourcePackError
from tests.unit import media_metadata_harness as h


def drain(adapter):
    records, receipts, cursor = [], [], None
    for _ in range(60):
        page = adapter.fetch_page({"operation": "media", "parameters": {}}, cursor=cursor)
        records += page.records
        receipts.append(page.receipt)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records, receipts


def adapter(source_id, native=None, **overrides):
    item = h.source(source_id)
    item["media_metadata"].update(overrides)
    return MediaMetadataAdapter(item, transport=fixture_transport(native if native is not None else h.pages(source_id)))


@pytest.fixture(scope="module")
def env():
    value = h.Env()
    receipt = value.run()
    assert receipt["status"] == "complete", receipt
    yield value
    value.conn.close()


def test_sources_extend_the_scientific_pack_with_audited_contracts():
    value = h.manifest()
    assert value["pack_id"] == "primary-scientific-evidence" and value["version"] == "1.2.0"
    media = [s for s in value["sources"] if s["connector"] == "media-metadata"]
    assert sorted(s["source_id"] for s in media) == sorted(h.MEDIA_SOURCES)
    assert "media-metadata" in SUPPORTED_CONNECTORS
    for item in media:
        assert item["auth"] == {"kind": "optional-secret", "secret_ref": "NOESIS_MEDIA_METADATA_CONTACT"}
        assert item["media_metadata"]["live_verification"] == "unverified-live"
        assert 1 <= len(item["media_metadata"]["selection"]) <= 50
    result = SourcePackConformance(h.ROOT).offline(value)
    assert result["valid"]
    assert {s["source_id"]: s["records"] for s in result["sources"] if s["source_id"] in h.MEDIA_SOURCES} == {
        "openlibrary-media": 7, "musicbrainz-media": 6, "wikidata-media": 8, "dnb-gnd-media": 2,
        "dnb-catalogue-media": 1, "loc-lcnaf-media": 1, "loc-catalogue-media": 1}
    assert set(PROVIDER_CONTRACTS) == set(LIVE_VERIFICATION) == {"open-library", "musicbrainz", "wikidata", "dnb",
                                                                  "loc"}
    assert all(v["status"] == "unverified-live" for v in LIVE_VERIFICATION.values())
    assert "CC0" in PROVIDER_CONTRACTS["musicbrainz"]["licence"] and "excluded" in \
        PROVIDER_CONTRACTS["musicbrainz"]["licence"]
    assert any("cover" in c for c in EXCLUDED_RECORD_CLASSES) and "wikidata-dumps-and-sparql" in EXCLUDED_SOURCES
    audit = (h.ROOT / "docs/development/cultural-evidence/media-metadata-source-audit.md").read_text()
    assert "Excluded record classes" in audit and "media-metadata-source-audit.md" in (
        h.ROOT / "docs/guides/cultural-collections.md").read_text()


def test_selections_are_bounded_and_audited():
    with pytest.raises(SourcePackError) as caught:
        adapter("openlibrary-media", selection=[{"q": "orchard"}])  # no free-text search
    assert caught.value.code == "invalid_mapping"
    with pytest.raises(SourcePackError) as caught:
        adapter("openlibrary-media", selection=[{"olid": f"OL{i + 1}W"} for i in range(51)])
    assert caught.value.code == "unbounded_source"
    with pytest.raises(SourcePackError) as caught:
        adapter("musicbrainz-media", selection=[{"entity": "label", "mbid": h.ARTIST_MBID}])
    assert caught.value.code == "invalid_mapping"
    with pytest.raises(SourcePackError) as caught:
        adapter("dnb-gnd-media", selection=[{"isbn": h.ISBN}])  # catalogue selector on the authorities endpoint
    assert caught.value.code == "invalid_mapping"
    first = adapter("wikidata-media").fetch_page({"operation": "media"}, cursor=None)
    other = adapter("wikidata-media", selection=[{"qid": "Q1"}])
    with pytest.raises(SourcePackError) as caught:
        other.fetch_page({"operation": "media"}, cursor=first.next_cursor)
    assert caught.value.code == "cursor_drift"


def test_open_library_keeps_olids_revisions_and_assertions_and_drops_content():
    records, receipts = drain(adapter("openlibrary-media"))
    by_id = {r["media_metadata"]["native_id"]: r["media_metadata"] for r in records}
    edition = by_id[h.EDITION_OLID]
    assert edition["record_type"] == "edition" and edition["revision"] == {
        "marker": "7", "basis": "ol-revision", "order": "0000000007", "date": "2026-01-18",
        "declared": "2026-01-18T09:00:00.000000"}
    assert {(i["scheme"], i["value"]) for i in edition["identifiers"]} >= {
        ("isbn10", "3000000011"), ("isbn13", "9783000000010"), ("lccn", "2019001234"), ("wikidata", "Q999900003")}
    assert {"covers", "ocaid", "identifiers/goodreads"} <= set(edition["excluded_fields"])
    assert edition["relations"][0] == {"type": "edition_of", "target": {"source": "open-library",
                                                                         "native_id": h.WORK_OLID}}
    assert "description" in by_id[h.WORK_OLID]["excluded_fields"]
    assert by_id[h.MERGED_AUTHOR_OLID]["status"] == "redirected" and \
        by_id[h.MERGED_AUTHOR_OLID]["redirect_to"] == h.AUTHOR_OLID
    assert h.SECOND_EDITION_OLID in by_id  # found by ISBN
    unknown = next(r for r in receipts if r.get("selector", {}).get("isbn") == h.UNKNOWN_ISBN)
    assert unknown["selector_outcome"] == "not_found" and unknown["status"] == 404
    assert "Excluded description text" not in json.dumps([r["media_metadata"] for r in records])


def test_musicbrainz_keeps_cc0_core_data_relationships_and_merges():
    records, receipts = drain(adapter("musicbrainz-media"))
    statements = [r["media_metadata"] for r in records]
    recording = next(s for s in statements if s["native_id"] == h.RECORDING_MBID)
    assert [i["value"] for i in recording["identifiers"] if i["scheme"] == "isrc"] == [h.ISRC]
    assert {"type": "recording_of", "target": {"source": "musicbrainz", "native_id": h.WORK_MBID,
                                               "label": "Harbour Lights"}} in recording["relations"]
    assert set(recording["excluded_fields"]) == {"annotation", "rating", "tags"}
    assert "Supplementary annotation" not in json.dumps(statements)
    merged = next(s for s in statements if s["native_id"] == h.OLD_RECORDING_MBID)
    assert merged["status"] == "redirected" and merged["redirect_to"] == h.RECORDING_MBID
    release = next(s for s in statements if s["record_type"] == "release")
    assert release["relations"][1]["type"] == "has_track" and "cover-art-archive" in release["excluded_fields"]
    work = next(s for s in statements if s["record_type"] == "work")
    assert ("iswc", "T-000.000.001-0") in {(i["scheme"], i["value"]) for i in work["identifiers"]}
    assert ("wikidata", "Q999900011") in {(i["scheme"], i["value"]) for i in work["identifiers"]}
    assert any(r.get("selector_outcome") == "not_found" for r in receipts)
    assert all(r.get("min_interval_ms") == 1100 for r in receipts if "provider" in r)


def test_musicbrainz_throttling_is_a_rate_limit_and_pacing_applies_to_live_transport():
    throttled = [{**p, "status": 503, "headers": {"Retry-After": "2"}} for p in h.pages("musicbrainz-media")]
    with pytest.raises(SourcePackError) as caught:
        drain(adapter("musicbrainz-media", throttled))
    assert caught.value.code == "rate_limited"
    waits, now = [], [0.0]
    paced = MediaMetadataAdapter(h.source("musicbrainz-media"), sleep=waits.append, clock=lambda: now[0])
    paced.transport = fixture_transport(h.pages("musicbrainz-media"))
    paced.fetch_page({"operation": "media"}, cursor=None)
    page = paced.fetch_page({"operation": "media"}, cursor=None)
    assert page.records and waits == [pytest.approx(1.1)]


def test_wikidata_keeps_revision_ids_ranks_references_and_redirects():
    records, _ = drain(adapter("wikidata-media"))
    statements = [r["media_metadata"] for r in records]
    author = [s for s in statements if s["native_id"] == h.AUTHOR_QID]
    assert [s["revision"]["marker"] for s in author] == ["2000000001", "2100000001"]
    current = author[1]
    gnd = [(i["value"], i["rank"], i["property"]) for i in current["identifiers"] if i["scheme"] == "gnd"]
    assert gnd == [(h.GND, "preferred", "P227"), (h.OLD_GND, "deprecated", "P227")]  # deprecated kept, not dropped
    reference = next(i for i in current["identifiers"] if i["scheme"] == "gnd")["references"][0]
    assert reference["snaks"] == {"P248": ["Q36578"]}
    assert current["record_type"] == "creator" and current["describes"] == "creator"
    edition = next(s for s in statements if s["native_id"] == h.EDITION_QID)
    assert edition["record_type"] == "authority-link" and edition["describes"] == "edition"
    redirected = next(s for s in statements if s["native_id"] == h.REDIRECTED_QID)
    assert redirected["status"] == "redirected" and redirected["redirect_to"] == h.BAND_QID
    pinned = h.pages("wikidata-media")[0]
    pinned["request"] = pinned["request"].replace("2000000001", "2000000002")
    with pytest.raises(SourcePackError) as caught:
        drain(adapter("wikidata-media", [pinned], selection=[{"qid": h.AUTHOR_QID, "revision": 2000000002}]))
    assert caught.value.code == "schema_drift"  # a pinned revision must be the revision answered


def test_marc_authorities_and_catalogue_records_keep_005_and_redirects():
    gnd, _ = drain(adapter("dnb-gnd-media"))
    current, redirected = (r["media_metadata"] for r in gnd)
    assert current["native_id"] == h.GND and current["revision"]["marker"] == "20260115103000.0"
    assert current["revision"]["date"] == "2026-01-15" and current["names"][0]["value"] == "Quell, Mara"
    assert redirected["status"] == "redirected" and redirected["redirect_to"] == h.GND
    catalogue, _ = drain(adapter("dnb-catalogue-media"))
    book = catalogue[0]["media_metadata"]
    assert book["record_type"] == "edition" and book["native_id"] == h.IDN
    assert book["creators"][0]["ref"] == {"scheme": "gnd", "value": h.GND} and book["excluded_fields"] == ["MARC 856"]
    names, _ = drain(adapter("loc-lcnaf-media"))
    assert names[0]["media_metadata"]["native_id"] == h.LCNAF
    loc, _ = drain(adapter("loc-catalogue-media"))
    assert loc[0]["media_metadata"]["creators"][0]["ref"] == {"scheme": "lcnaf", "value": h.LCNAF}
    assert "Rights note" not in json.dumps(loc[0]["media_metadata"])  # rights notes are never interpreted
    with pytest.raises(SourcePackError):
        parse_marc(b"<record/>", "loc", "catalogue", "https://lccn.loc.gov/x")
    with pytest.raises(SourcePackError):
        parse_marc(b"not xml", "dnb", "authorities", "https://services.dnb.de/sru/authorities")


def test_parsers_refuse_drifted_payloads():
    with pytest.raises(SourcePackError):
        parse_open_library({"key": "/works/OL1W", "type": {"key": "/type/work"}}, "https://openlibrary.org")
    with pytest.raises(SourcePackError):
        parse_musicbrainz("recording", {"title": "no id"}, "https://musicbrainz.org", h.RECORDING_MBID)
    with pytest.raises(SourcePackError):
        parse_musicbrainz("label", {"id": h.RECORDING_MBID}, "https://musicbrainz.org", h.RECORDING_MBID)


def test_the_runtime_projects_records_idempotently_with_redirect_history(env):
    store = env.store
    counts = {s: len(store.records(h.NS, source=s)) for s in ("open-library", "musicbrainz", "wikidata", "dnb",
                                                                  "loc")}
    assert counts == {"open-library": 7, "musicbrainz": 5, "wikidata": 6, "dnb": 3, "loc": 2}
    author = env.record_id("wikidata", h.AUTHOR_QID)
    assert [r["marker"] for r in store.revisions(h.NS, author)] == ["2000000001", "2100000001"]
    assert store.current_revision(h.NS, author)["marker"] == "2100000001"
    assert store.follow(h.NS, "musicbrainz", h.OLD_RECORDING_MBID) == [h.OLD_RECORDING_MBID, h.RECORDING_MBID]
    assert store.follow(h.NS, "dnb", h.OLD_GND) == [h.OLD_GND, h.GND]
    assert store.follow(h.NS, "open-library", h.MERGED_AUTHOR_OLID) == [h.MERGED_AUTHOR_OLID, h.AUTHOR_OLID]
    assert store.follow(h.NS, "wikidata", h.REDIRECTED_QID) == [h.REDIRECTED_QID, h.BAND_QID]
    selection = env.conn.execute("SELECT count(*), sum(statements) FROM media_selection").fetchone()
    assert selection == (26, 26)  # 26 selectors, two of them unknown identifiers
    before = env.conn.execute("SELECT count(*) FROM media_revisions").fetchone()[0]
    again = env.run("media-2")
    assert again["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM media_revisions").fetchone()[0] == before  # replays add nothing
