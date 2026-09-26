"""Cultural collections inside Science/Research and Geospatial (DDB, Europeana).

Replays the authored DDB Solr and Europeana Search API envelopes in
``tests/fixtures/source_packs/cultural-*.json`` through the real adapters, the
scientific source pack, the cultural store and the existing geospatial store.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion.cultural_sources import (
    DdbAdapter,
    EuropeanaAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)
from src.kb.cultural import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    CulturalError,
    CulturalStore,
    readiness,
    rights_policy,
)
from src.kb.geospatial import READ_SCOPE as GEO_READ
from src.kb.geospatial import WRITE_SCOPE as GEO_WRITE

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/scientific.json"
SCHEMAS = ROOT / "contracts/schemas/jsonschema"
SCOPES = {READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE, "namespace:global:write"}
CULTURAL = ["ddb-berlin-photographs", "europeana-berlin-images"]


def schema(name):
    return jsonschema.Draft7Validator(json.loads((SCHEMAS / f"{name}.json").read_text()))


def manifest():
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id):
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def pages(source_id):
    return copy.deepcopy(json.loads((ROOT / source(source_id)["fixture"]["path"]).read_text())["native_pages"])


def adapter(cls, source_id, native=None, *, secret="k", **cultural):
    item = source(source_id)
    item["cultural"].update(cultural)
    return cls(item, transport=fixture_transport(native if native is not None else pages(source_id)), secret=secret)


def drain(adapter_):
    records, receipts, cursor = [], [], None
    for _ in range(10):
        page = adapter_.fetch_page({"operation": "objects", "parameters": {}}, cursor=cursor)
        records += page.records
        receipts.append(page.receipt)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records, receipts


def run(runtime, value, key):
    return runtime.run(
        {"pack_id": value["pack_id"], "run_key": key, "operation": "objects", "source_ids": CULTURAL,
         "max_results": 200, "max_bytes": 50_000_000, "timeout_ms": 60_000},
        principal_id="operator", adapters=runtime.fixture_adapters(value["pack_id"], ROOT),
        dns_resolver=lambda _h: ["8.8.8.8"], secret_resolver=lambda _ref: "k")


@pytest.fixture()
def loaded():
    conn = duckdb.connect(":memory:")
    value = manifest()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for item in value["sources"]:
        runtime.accept_license(value["pack_id"], item["source_id"], principal_id="operator")
    receipt = run(runtime, value, "fixture-1")
    yield conn, value, runtime, receipt, CulturalStore(conn)
    conn.close()


def by_title(store, fragment, provider=None):
    return next(o for o in store.search("global", scopes=SCOPES, limit=100, provider=provider)["objects"]
                if fragment in o["titles"][0]["value"])


# ----------------------------------------------------------- integration


def test_sources_extend_the_scientific_pack_without_a_new_domain():
    value = manifest()
    assert value["pack_id"] == "primary-scientific-evidence" and value["domains"] == ["scientific"]
    assert {s["connector"] for s in value["sources"] if s["source_id"] in CULTURAL} == {"ddb", "europeana"}
    assert not (ROOT / "packs/cultural-collections").exists() and not (ROOT / "src/domains/cultural").exists()
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"]
    assert {s["source_id"]: s["records"] for s in result["sources"] if s["source_id"] in CULTURAL} == {
        "ddb-berlin-photographs": 4, "europeana-berlin-images": 4}


def test_adapters_need_keys_classify_failures_and_bound_pages():
    with pytest.raises(SourcePackError) as caught:
        drain(adapter(DdbAdapter, "ddb-berlin-photographs", secret=None))
    assert caught.value.code == "authentication_failed"
    broken = pages("ddb-berlin-photographs")[:1]
    broken[0]["body"] = {"response": {"numFound": 1}}
    with pytest.raises(SourcePackError) as caught:
        drain(adapter(DdbAdapter, "ddb-berlin-photographs", broken))
    assert caught.value.code == "schema_drift"
    throttled = [{**pages("europeana-berlin-images")[0], "status": 429, "headers": {"Retry-After": "3"}}]
    with pytest.raises(SourcePackError) as caught:
        drain(adapter(EuropeanaAdapter, "europeana-berlin-images", throttled))
    assert caught.value.code == "rate_limited"
    denied = [{**pages("europeana-berlin-images")[0], "body": {"success": False, "error": "Invalid API key"}}]
    with pytest.raises(SourcePackError) as caught:
        drain(adapter(EuropeanaAdapter, "europeana-berlin-images", denied))
    assert caught.value.code == "authentication_failed"
    records, receipts = drain(adapter(DdbAdapter, "ddb-berlin-photographs"))
    assert len(records) == 4 and receipts[1]["duplicates_skipped"] == 1
    capped, _ = drain(adapter(DdbAdapter, "ddb-berlin-photographs", max_records=2))
    assert len(capped) == 2
    first = adapter(EuropeanaAdapter, "europeana-berlin-images").fetch_page({"operation": "objects"}, cursor=None)
    other = adapter(EuropeanaAdapter, "europeana-berlin-images", query={"q": "Paris"})
    with pytest.raises(SourcePackError) as caught:
        other.fetch_page({"operation": "objects"}, cursor=first.next_cursor)
    assert caught.value.code == "cursor_drift"
    with pytest.raises(SourcePackError) as caught:
        adapter(DdbAdapter, "ddb-berlin-photographs", rows=1000)
    assert caught.value.code == "unbounded_source"


def test_records_keep_languages_dates_rights_and_representations():
    validator = schema("noesis-cultural-object-v1")
    records, _ = drain(adapter(EuropeanaAdapter, "europeana-berlin-images"))
    for record in records:
        assert not list(validator.iter_errors(record["cultural_record"]))
    multilingual = next(r["cultural_record"] for r in records if "fixture_multilingual" in r["id"])
    assert {(t["language"], t["value"]) for t in multilingual["titles"]} == {
        (None, "Street scene"), ("de", "Straßenszene (fiktiv)"), ("fr", "Scène de rue (fictif)")}
    shown = next(r["cultural_record"] for r in records if r["id"].endswith("fixture_A"))
    assert shown["same_as"] == ["ddb:FIXTUREDDBA000000000000000000001"]
    assert shown["aggregation"]["aggregator"] == "Deutsche Digitale Bibliothek"
    assert shown["institution"]["name"] == "Landesarchiv Berlin (fiktiv)"
    ddb, _ = drain(adapter(DdbAdapter, "ddb-berlin-photographs"))
    poster = next(r["cultural_record"] for r in ddb if "Plakat" in r["title"])
    assert [(d["original"], d["normalized"]) for d in poster["dates"]] == [
        ("1925", {"start": "1925", "end": "1925", "precision": "year"}),
        ("um 1926", {"start": "1926", "end": "1926", "precision": "year"})]
    postcard = next(r["cultural_record"] for r in ddb if "Postkarte" in r["title"])
    assert {rep["role"] for rep in postcard["representations"]} == {"preview", "original"}
    assert all("iiif" not in r["content"] for r in ddb)  # asset links never become text content


# ---------------------------------------------------------------- rights


@pytest.mark.parametrize(("statement", "category", "store", "export"), [
    ("http://creativecommons.org/publicdomain/mark/1.0/", "public-domain", True, True),
    ("https://creativecommons.org/licenses/by-sa/4.0/", "open-attribution", True, True),
    ("http://creativecommons.org/licenses/by-nc/4.0/", "noncommercial", False, False),
    ("http://rightsstatements.org/vocab/InC/1.0/", "restricted", False, False),
    ("http://example.org/custom", "unrecognized", False, False),
    (None, "missing", False, False),
])
def test_rights_policy_defaults_to_link_only(statement, category, store, export):
    policy = rights_policy(statement)
    assert (policy["category"], policy["allows"]["store_asset"], policy["allows"]["export_asset"]) == (
        category, store, export)
    assert policy["allows"]["metadata"] is True


# ---------------------------------------------------------- store & places


def test_run_projects_objects_places_and_is_idempotent(loaded):
    conn, value, runtime, receipt, store = loaded
    assert receipt["status"] == "complete"
    result = store.search("global", scopes=SCOPES, limit=100)
    assert result["count"] == 8
    gate = by_title(store, "Brandenburger Tor", "ddb")
    assert gate["rights"]["category"] == "public-domain"
    [place] = gate["places"]
    assert (place["role"], place["state"], place["precision_basis"]) == (
        "depicted", "coordinates-projected", "provider-unspecified; pinned default")
    geometry = store.geo.geometry("global", place["geometry_id"], scopes={GEO_READ})
    assert geometry["geometry"]["coordinates"] == [13.3777, 52.5163]
    plan = by_title(store, "Stadtplan", "ddb")
    assert plan["places"][0]["state"] == "unresolved" and plan["places"][0]["geometry_id"] is None
    map_ = by_title(store, "Plan von Berlin", "europeana")
    assert map_["places"][0]["state"] == "name-resolved" and map_["places"][0]["geometry_id"] is None
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("cultural_objects", "cultural_revisions", "cultural_places")}
    again = runtime.run({"pack_id": value["pack_id"], "run_key": "fixture-2", "operation": "objects",
                         "source_ids": CULTURAL, "mode": "backfill", "backfill": {"from_ms": 0}},
                        principal_id="operator", adapters=runtime.fixture_adapters(value["pack_id"], ROOT),
                        dns_resolver=lambda _h: ["8.8.8.8"], secret_resolver=lambda _r: "k")
    assert again["status"] == "complete"
    assert {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counts} == counts


def _record(record_id, *, places, rights="http://creativecommons.org/publicdomain/mark/1.0/", sha="a"):
    return {"id": f"europeana:{record_id}", "cultural_record": {
        "contract": "noesis-cultural-object-v1", "provider": "europeana", "provider_record_id": record_id,
        "source_url": "https://www.europeana.eu/item" + record_id, "titles": [{"value": "Synthetic", "language": None}],
        "descriptions": [], "creators": [], "subjects": [], "dates": [], "languages": [], "places": places,
        "rights": {"statement": rights}, "representations": [
            {"role": "original", "url": "https://media.example.org/x.jpg", "media_type": None, "rights": rights}],
        "same_as": [], "native_sha256": sha * 64}}


def test_place_roles_crs_conflicts_and_review_states():
    conn = duckdb.connect(":memory:")
    store = CulturalStore(conn)
    geo_scopes = {GEO_READ, GEO_WRITE}
    for key in ("a", "b"):
        store.geo.register_place("global", "Neustadt", "city", names=[{"value": "Neustadt", "language": "de"}],
                                 source_ids={"fixture": key}, parent_ids=[], principal_id="t", scopes=geo_scopes)
    store.observe_page("global", [_record("/1/utm", places=[
        {"role": "event", "name": None, "coordinates": {"x": 390000.0, "y": 5820000.0, "crs": "EPSG:25833"}}]),
        _record("/1/bad", places=[{"role": "depicted", "name": None, "coordinates": {"lat": 95, "lon": 13}}]),
        _record("/1/ambiguous", places=[{"role": "institution", "name": "Neustadt", "coordinates": None},
                                        {"role": "unknown-role", "name": "Berlin", "coordinates": None}])],
        run_id="synthetic", source=None)
    objects = {o["provider_record_id"]: o for o in store.search("global", scopes=SCOPES, limit=10)["objects"]}
    [utm] = objects["/1/utm"]["places"]
    assert utm["role"] == "event" and utm["state"] == "coordinates-projected" and utm["transform"]["source_crs"] == \
        "EPSG:25833"
    assert objects["/1/bad"]["places"][0]["state"] == "coordinates-rejected"
    states = {(p["role"], p["state"]) for p in objects["/1/ambiguous"]["places"]}
    assert states == {("institution", "needs-review"), ("depicted", "name-resolved")}
    assert all(p["resolution_id"] for p in objects["/1/ambiguous"]["places"] if p["state"] == "needs-review")
    conn.close()


def test_changed_rights_create_a_revision_and_keep_history():
    conn = duckdb.connect(":memory:")
    store = CulturalStore(conn)
    store.observe_page("global", [_record("/1/x", places=[], sha="a")], run_id="r1", source=None)
    store.observe_page("global", [_record("/1/x", places=[], sha="b",
                                          rights="http://rightsstatements.org/vocab/InC/1.0/")], run_id="r2",
                       source=None)
    [obj] = store.search("global", scopes=SCOPES)["objects"]
    assert obj["rights"]["category"] == "restricted" and len(obj["revisions"]) == 2
    conn.close()


# ------------------------------------------------------ matches & research


def test_matches_keep_provider_records_separate(loaded):
    _, _, _, _, store = loaded
    matches = store.propose_matches("global", scopes=SCOPES)["matches"]
    validator = schema("noesis-cultural-object-match-v1")
    assert all(not list(validator.iter_errors(m)) for m in matches)
    kinds = sorted((m["basis"], m["candidate_state"], m["review_state"]) for m in matches)
    assert kinds == [("explicit_identifier", "identifier-match", "accepted"),
                     ("multi-field", "conflicting-candidate", "unreviewed")]
    candidate = next(m for m in matches if m["basis"] == "multi-field")
    store.review_match("global", candidate["match_id"], "accepted", "same poster series", scopes=SCOPES,
                       principal_id="r1")
    reversed_ = store.review_match("global", candidate["match_id"], "rejected", "different year and institution",
                                   scopes=SCOPES, principal_id="r2")
    assert [h["decision"] for h in reversed_["review_history"]] == ["accepted", "rejected"]
    assert store.search("global", scopes=SCOPES, limit=100)["count"] == 8  # nothing merged or deleted


def test_research_links_need_evidence_and_lead_to_places(loaded):
    _, _, _, _, store = loaded
    gate = by_title(store, "Brandenburger Tor", "ddb")
    with pytest.raises(CulturalError) as caught:
        store.link_research("global", gate["object_id"], "doi", "10.9999/fixture.1", "keyword", "overlap",
                            scopes=SCOPES, principal_id="p")
    assert caught.value.code == "invalid_basis"
    store.link_research("global", gate["object_id"], "doi", "10.9999/fixture.1", "explicit_citation",
                        "Fig. 3 cites the DDB item", scopes=SCOPES, principal_id="p")
    suggested = store.suggest_research_candidates("global", "doi", "10.9999/fixture.1",
                                                  "Stadtplan aus der Kartensammlung", scopes=SCOPES,
                                                  principal_id="p")
    [candidate] = suggested["candidates"]
    assert candidate["state"] == "candidate" and candidate["basis"] == "keyword-overlap"
    trail = store.primary_sources_for_work("global", "doi", "10.9999/fixture.1", scopes=SCOPES)
    assert [p["object"]["object_id"] for p in trail["primary_sources"]] == [gate["object_id"]]
    assert len(trail["candidates"]) == 1 and trail["geometry_ids"]
    # The place leads back to every object projected at that geometry.
    found = store.search("global", scopes=SCOPES, geometry_ids=trail["geometry_ids"], limit=100)
    assert {o["object_id"] for o in found["objects"]} >= {gate["object_id"]}
    accepted = store.review_research_candidate("global", candidate["link_id"], "accepted", "map shows the area",
                                               scopes=SCOPES, principal_id="r")
    assert (accepted["state"], accepted["basis"]) == ("linked", "reviewed_assertion")
    assert not list(schema("noesis-cultural-research-link-v1").iter_errors(accepted))
    other = {READ_SCOPE, "namespace:other:read"}
    with pytest.raises(CulturalError) as caught:
        store.object("global", gate["object_id"], scopes=other)
    assert caught.value.code == "unauthorized"


def test_date_subject_and_place_queries(loaded):
    _, _, _, _, store = loaded
    early = store.search("global", scopes=SCOPES, date_from="1900", date_to="1915", limit=100)
    assert {o["titles"][0]["value"] for o in early["objects"]} == {
        "Brandenburger Tor, Ansicht von Osten (fiktiv)", "Stadtplan von Berlin-Mitte (fiktiv)"}
    berlin = by_title(store, "Plan von Berlin", "europeana")["places"][0]["place_id"]
    assert store.search("global", scopes=SCOPES, place_id=berlin, limit=100)["count"] >= 4
    assert store.search("global", scopes=SCOPES, subject="Stadtplan")["count"] == 1
    assert store.search("global", scopes=SCOPES, collection="Kartensammlung")["count"] == 1


# ------------------------------------------------------------------ assets


def test_assets_follow_item_rights_and_keep_metadata(loaded):
    _, _, _, _, store = loaded
    served = {}

    def transport(*, url, params, headers, timeout):
        return served.get(url, {"status": 404, "headers": {}, "content": b""})

    gate = by_title(store, "Brandenburger Tor", "ddb")
    preview = gate["representations"][0]["url"]
    served[preview] = {"status": 200, "headers": {"Content-Type": "image/jpeg"}, "content": b"jpeg-bytes"}
    kept = store.acquire_assets("global", gate["object_id"], scopes=SCOPES, principal_id="p",
                                allowed_hosts=["iiif.example-ddb.de"], transport=transport)
    assert kept["assets"][0]["state"] == "retained" and kept["assets"][0]["content_sha256"]
    assert not list(schema("noesis-cultural-asset-v1").iter_errors(kept["assets"][0]))
    plan = by_title(store, "Stadtplan", "ddb")
    denied = store.acquire_assets("global", plan["object_id"], scopes=SCOPES, principal_id="p",
                                  allowed_hosts=["iiif.example-ddb.de"], transport=transport)
    assert {a["state"] for a in denied["assets"]} == {"link-only"}
    postcard = by_title(store, "Postkarte", "ddb")
    for url in (r["url"] for r in postcard["representations"]):
        served[url] = {"status": 410, "headers": {}, "content": b""}
    research = store.acquire_assets("global", postcard["object_id"], scopes=SCOPES, principal_id="p",
                                    purpose="noncommercial-research", allowed_hosts=["iiif.example-ddb.de"],
                                    transport=transport)
    assert {a["state"] for a in research["assets"]} == {"unavailable"}  # expired URLs
    export = store.acquire_assets("global", postcard["object_id"], scopes=SCOPES, principal_id="p", action="export",
                                  purpose="noncommercial-research", allowed_hosts=["iiif.example-ddb.de"],
                                  transport=transport)
    assert {a["state"] for a in export["assets"]} == {"link-only"}
    offsite = store.acquire_assets("global", gate["object_id"], scopes=SCOPES, principal_id="p",
                                   allowed_hosts=[], transport=transport)
    assert offsite["assets"][0]["state"] == "link-only"
    assert by_title(store, "Stadtplan", "ddb")["titles"]  # metadata unaffected


def test_readiness_is_per_provider(loaded):
    conn, _, _, _, _ = loaded
    state = readiness(conn, secrets=lambda _name: None)
    assert {p: v["live"] for p, v in state["providers"].items()} == {"ddb": "blocked", "europeana": "blocked"}
    assert {b["code"] for v in state["providers"].values() for b in v["blockers"]} == {"credential_missing"}
    assert readiness(conn, secrets=lambda _name: "k")["providers"]["ddb"]["live"] == "ready"
