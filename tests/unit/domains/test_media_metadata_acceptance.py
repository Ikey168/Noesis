"""MM12 (#2510): offline title-and-creator-to-authority acceptance for Cultural Collections media metadata.

Synthetic, pinned fixtures for Open Library, MusicBrainz, Wikidata, the
Deutsche Nationalbibliothek and the Library of Congress replay through the
real ``media-metadata`` adapter and the source-pack runtime. Sockets are
blocked and the ``media-metadata`` feature is selected. The journey runs
through the MCP tools:

* a book title and creator resolve to cited authority records with their
  revisions and identity matches;
* a music recording resolves by ISRC and by a merged MBID with its redirect
  history;
* negative cases: an ambiguous title, a conflicting ISBN assertion, a
  deprecated authority and an unknown identifier.

The result is offline evidence only. Live evidence (#2513) is reported
separately and stays ``unverified-live``.
"""

from __future__ import annotations

import asyncio
import socket

import duckdb
import pytest

from src.kb.media_metadata import feature_enabled, record_id_for
from tests.unit import media_metadata_harness as h
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp import server


@pytest.fixture(autouse=True)
def isolated_registry():
    """The composition migration hands pack authority to the coordinator; restore the registry afterwards."""
    from src.domains import registry as domain_registry

    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


@pytest.fixture()
def offline(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def sources(identity):
    return {(e["source"], e["native_id"]) for e in identity["authority_records"]}


def test_title_creator_and_recording_to_cited_authority_records_with_revisions(offline, tmp_path, monkeypatch):
    # The feature is optional and off until selected in the active composition.
    conn0, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn0) is False
    coordinator.select("science", bundles["science"]["version"], features=["media-metadata"])
    assert coordinator.activate("science-media-on")["status"] == "published" and feature_enabled(conn0)

    path = str(tmp_path / "media-acceptance.duckdb")
    env = h.Env(duckdb.connect(path))
    run = env.run()
    assert run["status"] == "complete"
    before = env.conn.execute("SELECT count(*) FROM media_revisions").fetchone()[0]
    assert env.run("media-replay")["status"] == "complete"  # re-acquisition adds no revision
    assert env.conn.execute("SELECT count(*) FROM media_revisions").fetchone()[0] == before
    env.conn.close()

    state = {"principal": "analyst", "scopes": set(h.ALL)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())

    # Offline evidence is reported separately from live evidence: every source stays unverified-live.
    status = tools["media_metadata_status"].fn()
    assert {s["LIVE_VERIFICATION"] for s in status["sources"]} == {"unverified-live"}
    assert status["records_by_source"] == {"dnb": 3, "loc": 2, "musicbrainz": 5, "open-library": 7, "wikidata": 6}

    proposed = tools["propose_media_identity_matches"].fn(namespace=h.NS)
    assert {m["basis"] for m in proposed["matches"]} >= {"explicit-identifier", "shared-identifier", "similarity"}

    # --- a book: title and creator -> ranked candidates with evidence -> the identity and its authority records.
    book = tools["search_media_titles"].fn(namespace=h.NS, title="The Glass Orchard", creator="Mara Quell")
    top = book["candidates"][0]
    assert book["status"] == "candidates" and top["score"] == 1.0
    assert top["evidence"]["title"]["matched"] and top["evidence"]["creator"]["matched"] == "Mara Quell"
    work = next(c for c in book["candidates"] if c["native_id"] == h.WORK_OLID or
                ("open-library", h.WORK_OLID) in sources(c["identity"]))
    assert ("wikidata", h.WORK_QID) in sources(work["identity"])  # P648 explicit identifier match
    author = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.AUTHOR_QID)
    assert author["status"] == "resolved"
    identity = author["identities"][0]
    assert sources(identity) == {("wikidata", h.AUTHOR_QID), ("open-library", h.AUTHOR_OLID), ("dnb", h.GND),
                                 ("loc", h.LCNAF)}
    for entry in identity["authority_records"]:
        assert entry["citation"]["revision_marker"] and entry["citation"]["retrieved_at"]
    wikidata = next(e for e in identity["authority_records"] if e["source"] == "wikidata")
    assert [r["marker"] for r in wikidata["revision_history"]] == ["2000000001", "2100000001"]  # authority revisions
    gnd = next(e for e in identity["authority_records"] if e["source"] == "dnb")
    assert gnd["citation"]["revision_basis"] == "marc-005" and gnd["citation"]["revision_date"] == "2026-01-15"
    assert {m["evidence"][0]["property"] for m in identity["matches_used"]} >= {"P227", "P244", "P648"}
    earlier = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.AUTHOR_QID, as_of="2025-07-01")
    assert next(e for e in earlier["identities"][0]["authority_records"] if e["source"] == "wikidata"
                )["citation"]["revision_marker"] == "2000000001"
    second = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.SECOND_ISBN)
    assert second["status"] == "resolved" and ("open-library", h.SECOND_EDITION_OLID) in sources(
        second["identities"][0])
    bundle = tools["export_media_evidence_bundle"].fn(namespace=h.NS, identifier=h.AUTHOR_QID)
    assert bundle["completeness"]["status"] == "complete"

    # --- a music recording: exact ISRC, the merged MBID's redirect history, and the work it records.
    recording = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.ISRC)
    assert recording["status"] == "resolved"
    assert ("musicbrainz", h.RECORDING_MBID) in sources(recording["identities"][0])
    merged = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.OLD_RECORDING_MBID)
    assert merged["redirects"][0]["chain"] == [h.OLD_RECORDING_MBID, h.RECORDING_MBID]
    assert merged["redirects"][0]["history"][0]["decision_id"].startswith("entity-decision:")
    song = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.WORK_MBID)
    assert sources(song["identities"][0]) == {("musicbrainz", h.WORK_MBID), ("wikidata", h.SONG_QID)}
    history = tools["media_authority_history"].fn(namespace=h.NS, record=f"musicbrainz:{h.OLD_RECORDING_MBID}")
    assert history["revisions"][-1]["status"] == "redirected"

    # --- negative cases.
    ambiguous = tools["search_media_titles"].fn(namespace=h.NS, title="The Glass Orchard")
    assert ambiguous["ambiguous"] is True and len(ambiguous["candidates"]) > 1  # never one asserted answer
    conflict = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.ISBN)
    assert conflict["status"] == "ambiguous" and conflict["conflicts"][0]["key"] == f"isbn:{h.ISBN}"
    assert tools["export_media_evidence_bundle"].fn(namespace=h.NS, identifier=h.ISBN)["completeness"][
        "status"] == "partial"
    deprecated = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.OLD_GND, scheme="gnd")
    assert deprecated["redirects"][0]["chain"] == [h.OLD_GND, h.GND]
    old = tools["media_authority_history"].fn(namespace=h.NS, record=f"dnb:{h.OLD_GND}")
    assert old["revisions"][0]["status"] == "redirected"
    unknown = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.UNKNOWN_ISBN)
    assert unknown["status"] == "unresolved" and unknown["unknowns"][0]["kind"] == "unknown_identifier"
    unknown_mbid = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.UNKNOWN_MBID)
    assert unknown_mbid["status"] == "unresolved"

    # A reviewer resolves the conflict for the DNB edition; the decision is recorded and the identity now cites it.
    edition = record_id_for(h.NS, "open-library", h.EDITION_OLID)
    pending = tools["list_media_identity_matches"].fn(namespace=h.NS, record=f"dnb:{h.IDN}")["matches"]
    dnb_match = next(m for m in pending if m["candidate_state"] == "conflict" and edition in {m["left_id"],
                                                                                             m["right_id"]})
    reviewed = tools["review_media_identity_match"].fn(namespace=h.NS, match_id=dnb_match["match_id"],
                                                       decision="accepted", reason="same title, author, year and LCCN")
    assert reviewed["state"] == "accepted" and reviewed["candidate_state"] == "conflict"
    dnb = tools["resolve_media_identifier"].fn(namespace=h.NS, identifier=h.IDN, scheme="idn")
    assert dnb["status"] == "resolved" and ("open-library", h.EDITION_OLID) in sources(dnb["identities"][0])
    assert dnb_match["match_id"] in {m["match_id"] for m in dnb["identities"][0]["matches_used"]}
