"""NH08-NH09 (#2341, #2347): reviewable correspondences without merging, places from published geometry, cited links."""

from __future__ import annotations

import pytest

from src.kb.hazards_identity import HazardIdentity, side_by_side
from src.kb.hazards_links import HazardLinks
from src.kb.hazards_store import HazardStoreError
from tests.unit.hazards import harness as h

SAMOS = {"type": "Polygon", "coordinates": [[[26.55, 37.65], [27.1, 37.65], [27.1, 37.82], [26.55, 37.82], [26.55, 37.65]]]}


def register_place(store, name, geometry, *, valid_from="2098-01-01T00:00:00Z"):
    place = store.geo.register_place(h.NS, name, "island", names=[{"value": name, "language": "en", "kind": "canonical"}],
                                     source_ids={"authored": name}, parent_ids=[], principal_id="operator",
                                     scopes=h.SCOPES, place_key=f"authored:{name}")
    store.geo.store_geometry(h.NS, geometry, place_id=place["place_id"], crs="EPSG:4326", precision_m=0.0,
                             simplified_from=None, disputed=False, admin_hierarchy=[],
                             source={"kind": "authored-boundary", "vintage": "2098"}, evidence=[],
                             principal_id="operator", scopes=h.SCOPES, generation=3, valid_from_ms=h.ms(valid_from))
    return place["place_id"]


def loaded():
    conn = h.connection()
    h.load_all(conn)
    identity = HazardIdentity(conn, now=lambda: h.ms("2099-09-03T00:00:00Z"))
    return conn, identity


def by_pair(identity, store):
    names = {}
    for item in identity.correspondences(h.NS, scopes=h.SCOPES):
        left = store.record(h.NS, item["left_record"], scopes=h.SCOPES)
        right = store.record(h.NS, item["right_record"], scopes=h.SCOPES)
        names[(frozenset({left["native_id"], right["native_id"]}), item["method"])] = item
    return names


def test_candidates_carry_method_evidence_and_confidence_and_review_never_merges():
    conn, identity = loaded()
    store = identity.store
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)
    pairs = by_pair(identity, store)
    usgs_gdacs = pairs[(frozenset({"us7000zz01", "EQ1400001"}), "shared_identifier")]
    assert usgs_gdacs["evidence"]["shared"] == ["usgs:us7000zz01"] and usgs_gdacs["confidence"] == 0.99
    usgs_emsc = pairs[(frozenset({"us7000zz01", "20990810_0000031"}), "proximity")]
    assert usgs_emsc["evidence"]["origin_time_difference_s"] < 2 and usgs_emsc["evidence"]["spatial_receipt_id"]
    assert 0.1 <= usgs_emsc["confidence"] < 0.99
    assert (frozenset({"AL052099", "TC1000501"}), "shared_identifier") in pairs
    assert (frozenset({"99001", "WF1020001"}), "shared_identifier") in pairs
    assert not any("us7000zz02" in key[0] or "us7000zz03" in key[0] for key in pairs)  # deleted / merged
    assert all(item["state"] == "proposed" for item in identity.correspondences(h.NS, scopes=h.SCOPES))
    assert {u["native_id"] for u in proposed["unmatched"]} >= {"us7000zz01", "20990810_0000031"}

    with pytest.raises(HazardStoreError) as caught:
        identity.review(h.NS, usgs_gdacs["correspondence_id"], "accept", "same id", principal_id="analyst",
                        scopes=h.REVIEW_SCOPES)
    assert caught.value.code == "self_review"
    usgs = store.find(h.NS, "usgs", "hazard_event", "us7000zz01")
    before = store.record(h.NS, usgs, scopes=h.SCOPES)["content"]
    accepted = identity.review(h.NS, usgs_emsc["correspondence_id"], "accept", "same origin within tolerance",
                               principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert accepted["state"] == "accepted" and accepted["merge"] is False
    assert accepted["reviews"][-1]["entity_decision_id"].startswith("entity-decision:")
    assert store.record(h.NS, usgs, scopes=h.SCOPES)["content"] == before  # never replaced or averaged
    beside = side_by_side(store, h.NS, usgs, scopes=h.SCOPES)
    (emsc,) = beside
    assert emsc["provider"] == "emsc" and {p["name"]: p["value"] for p in emsc["parameters"]}["magnitude"] == "6.0"
    reverted = identity.revert(h.NS, usgs_emsc["correspondence_id"], "wrong pairing", principal_id="reviewer",
                               scopes=h.REVIEW_SCOPES)
    assert reverted["state"] == "reverted" and side_by_side(store, h.NS, usgs, scopes=h.SCOPES) == []
    assert "20990810_0000031" in {u["native_id"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}


def test_places_resolve_from_published_geometry_with_boundary_vintage():
    conn, identity = loaded()
    store = identity.store
    samos = register_place(store, "Samos (authored boundary)", SAMOS)
    fire = store.find(h.NS, "effis", "hazard_event", "99001")
    resolved = identity.resolve_places(h.NS, fire, principal_id="analyst", scopes=h.SCOPES)
    (link,) = resolved["links"]
    assert link["place_id"] == samos and link["relation"] == "intersects" and link["spatial_receipt_id"]
    assert link["boundary"]["generation"] == 3 and link["boundary"]["valid_from_ms"] == h.ms("2098-01-01T00:00:00Z")
    assert link["boundary"]["source"] == {"kind": "authored-boundary", "vintage": "2098"}
    quake = store.find(h.NS, "usgs", "hazard_event", "us7000zz01")
    offshore = identity.resolve_places(h.NS, quake, principal_id="analyst", scopes=h.SCOPES)
    assert offshore["state"] == "no place matched" and offshore["method"].startswith("published geometry only")
    assert identity.place_links(h.NS, place_id=samos)[0]["record_id"] == fire


def test_links_only_from_citations_shared_ids_or_accepted_matches_and_missing_providers_reported():
    from services.ingest.common.document_model import Document
    from src.ingestion.document_store import DocumentStore

    conn, identity = loaded()
    store = identity.store
    DocumentStore(conn).upsert([Document(
        document_id="news:quake-report", source_type="news", source_id="fictional-wire", language="en",
        ingested_at=h.ms("2099-08-10T12:00:00Z"), url="https://news.example.org/quake", title="Quake felt on islands",
        content="The USGS listed the event as us7000zz01 (GLIDE EQ-2099-000101-GRC). Fictional report.", metadata={})])
    identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)
    usgs = store.find(h.NS, "usgs", "hazard_event", "us7000zz01")
    pairs = by_pair(identity, store)
    identity.review(h.NS, pairs[(frozenset({"us7000zz01", "EQ1400001"}), "shared_identifier")]["correspondence_id"],
                    "accept", "GDACS cites the USGS id", principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    links = HazardLinks(conn, now=identity.now)
    found = links.discover(h.NS, usgs, principal_id="analyst", scopes=h.SCOPES)
    bases = {(link["target"]["kind"], link["basis"]) for link in found["links"]}
    assert bases == {("document", "citation"), ("hazard-record", "accepted_correspondence")}
    document = next(link for link in found["links"] if link["target"]["kind"] == "document")
    assert document["evidence"]["cited_identifier"] == "us7000zz01" and "us7000zz01" in document["evidence"]["quote"]
    assert document["target"]["revision"] and document["record_revision_id"] == found["record_revision_id"]
    assert set(found["unavailable_providers"]) >= {"weather", "humanitarian"}
    assert "no causal" in found["claims"]
    with pytest.raises(HazardStoreError) as caught:
        links.link(h.NS, usgs, target={"kind": "weather", "id": "w1", "revision": "r1"}, basis="citation",
                   evidence={"quote": "x"}, principal_id="analyst", scopes=h.SCOPES)
    assert caught.value.code == "provider_unavailable"
    with pytest.raises(HazardStoreError) as caught:
        links.link(h.NS, usgs, target={"kind": "document", "id": "news:quake-report", "revision": "x"},
                   basis="co-occurrence", evidence={"near": True}, principal_id="analyst", scopes=h.SCOPES)
    assert caught.value.code == "invalid_basis"
    gdacs = store.find(h.NS, "gdacs", "hazard_event", "EQ1400001")
    glide = links.discover(h.NS, gdacs, principal_id="analyst", scopes=h.SCOPES)
    assert {link["evidence"].get("cited_identifier") for link in glide["links"]} >= {"EQ-2099-000101-GRC"}
