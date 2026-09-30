"""HR03-HR06 (#2239 #2244 #2249 #2254): bounded, receipted ReliefWeb/HDX/UCDP acquisition; ACLED gated."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.ingestion.humanitarian_sources import (
    ACLED_DECISION,
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    HumanitarianAdapter,
    fixture_transport,
    parse_hxl_header,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, validate_source_pack
from src.kb.humanitarian_store import HumanitarianStore
from tests.unit.humanitarian import fixture_builder
from tests.unit.humanitarian.harness import NS, PACK, ROOT, SCOPES, World, pack_manifest


def source(source_id, **humanitarian):
    raw = json.loads(PACK.read_text())
    for item in raw["sources"]:
        if item["source_id"] == source_id:
            item.setdefault("humanitarian", {}).update(humanitarian)
    return next(s for s in validate_source_pack(raw)["sources"] if s["source_id"] == source_id)


def spy(pages):
    calls = []
    inner = fixture_transport(pages)

    def transport(**kwargs):
        calls.append(kwargs)
        return inner(**kwargs)
    return transport, calls


@pytest.fixture()
def acquired():
    world = World()
    world.install()
    world.acquire_all()
    return world


def test_source_pack_validates_and_fixtures_replay_to_pinned_hashes():
    manifest = pack_manifest()
    schema = json.loads((ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text())
    assert not list(Draft7Validator(schema).iter_errors(manifest))
    result = SourcePackConformance(ROOT).offline(json.loads(PACK.read_text()))
    assert result["valid"] and result["coverage"]["configured"] == result["coverage"]["verified"] == 6
    statuses = {s["source_id"]: s["humanitarian"]["live_verification"] for s in manifest["sources"]}
    assert statuses.pop("acled-events") == "declined" and set(statuses.values()) == {"unverified-live"}
    assert LIVE_VERIFICATION["acled"]["status"] == "declined"


def test_reliefweb_reports_keep_publishers_dates_and_tags_verbatim_with_appname(acquired):
    store = HumanitarianStore(acquired.conn, initialize=False)
    report = store.revision(NS, "reliefweb:situation_report:9900001", scopes=SCOPES)["content"]
    assert report["publishers"] == [{"id": 99901, "name": "Fixture Coordination Office", "shortname": "FCO"}]
    assert report["report_date"] == "2098-05-01T00:00:00+00:00"
    assert report["disasters"] == [{"id": 99001, "name": "Sudan: Fixture Conflict - Apr 2098", "glide": "CE-2098-000001-SDN"}]
    assert report["body"] == {"locator": "https://reliefweb.int/report/sudan/fixture-9900001", "mirrored": False}
    assert store.revision(NS, "reliefweb:appeal:9900002", scopes=SCOPES)["content"]["formats"] == ["Flash Appeal"]
    assert not store.keys(NS, record_type="situation_report") == []
    assert "reliefweb:situation_report:9900003" not in store.keys(NS)  # a map is out of scope
    crisis = store.revision(NS, "reliefweb:crisis:99001", scopes=SCOPES)["content"]
    assert crisis["glide"] == "CE-2098-000001-SDN" and crisis["status"] == "current"
    transport, calls = spy(fixture_builder.native_pages("reliefweb-reports-sdn"))
    page = HumanitarianAdapter(source("reliefweb-reports-sdn"), transport=transport).fetch_page(
        {"operation": "reports", "parameters": {}}, cursor=None)
    assert calls[0]["params"]["appname"] == "noesis-humanitarian"
    assert page.receipt["out_of_scope_format"] == 1 and page.receipt["page_sha256"]


def test_updated_report_is_a_new_revision_linked_to_the_previous(acquired):
    store = HumanitarianStore(acquired.conn, initialize=False)
    acquired.run("reliefweb-reports-sdn")  # unchanged re-acquisition adds nothing
    assert len(store.history(NS, "reliefweb:situation_report:9900001", scopes=SCOPES)) == 1
    acquired.run("reliefweb-reports-sdn", revised=True)
    history = store.history(NS, "reliefweb:situation_report:9900001", scopes=SCOPES)
    assert len(history) == 2 and history[1]["predecessor_revision_id"] == history[0]["revision_id"]
    assert {c["field"] for c in history[1]["changed_fields"]} == {"as_of", "revision", "title"}
    receipts = store.receipts(NS, scopes=SCOPES)
    assert receipts and all(r["detail"]["evidence"]["pack_id"] == "humanitarian-response" for r in receipts)


def test_hdx_hxl_tags_as_published_untagged_marked_and_restricted_metadata_only(acquired):
    store = HumanitarianStore(acquired.conn, initialize=False)
    sites = store.revision(NS, "hdx:dataset:hdx-fixture-0001", scopes=SCOPES)["content"]
    columns = sites["resources"][0]["hxl"]["columns"]
    assert [c["hashtag"] for c in columns] == ["#adm1", "#adm1", None, "#affected", "#contact"]
    assert columns[2]["status"] == "untagged" and columns[3]["attributes"] == ["idps", "ind"]
    assert columns[4]["personal_data_tag"] and sites["resources"][0]["personal_data_tags"]
    assert sites["resources"][0]["hash"] == "md5:1a2b3c4d" and sites["licence"]["reuse"] == "open"
    restricted = store.revision(NS, "hdx:dataset:hdx-fixture-0002", scopes=SCOPES)["content"]
    assert restricted["metadata_only"] and restricted["access"] == "hdx-connect"
    assert restricted["resources"][0]["hxl"]["status"] == "not-read"
    transport, calls = spy(fixture_builder.native_pages("hdx-sdn-datasets"))
    HumanitarianAdapter(source("hdx-sdn-datasets"), transport=transport).fetch_page(
        {"operation": "datasets", "parameters": {}}, cursor=None)
    assert not any("r-fixture-0002" in c["url"] for c in calls)  # never read a restricted resource
    acquired.run("hdx-sdn-datasets", revised=True)
    history = store.history(NS, "hdx:dataset:hdx-fixture-0001", scopes=SCOPES)
    assert [h["content"]["resources"][0]["hash"] for h in history] == ["md5:1a2b3c4d", "md5:9f8e7d6c"]


def test_hxl_parser_marks_a_file_without_hashtags_untagged():
    parsed = parse_hxl_header("a,b\n1,2\n")
    assert parsed["status"] == "untagged" and {c["status"] for c in parsed["columns"]} == {"untagged"}


def test_ucdp_precision_verbatim_candidate_then_final_release_revisions(acquired):
    store = HumanitarianStore(acquired.conn, initialize=False)
    history = store.history(NS, "ucdp:conflict_event:990002", scopes=SCOPES)
    candidate, final = history[0]["content"], history[1]["content"]
    assert candidate["coding_status"] == "candidate" and final["coding_status"] == "final"
    assert candidate["precision"]["where"] == {"code": 4, "scheme": "ucdp-where_prec",
                                               "label": "first-order administrative division"}
    assert final["precision"]["where"]["code"] == 3
    assert candidate["counts"] == {"best": 7, "low": 7, "high": 12, "deaths_a": 1, "deaths_b": 2,
                                   "deaths_civilians": 0, "deaths_unknown": 0}
    assert [a["label"] for a in candidate["actors"]] == ["Fixture Militia", "Civilians"]
    assert "precision" in {c["field"] for c in history[1]["changed_fields"]}
    for field in ("source_headline", "source_original", "source_article", "where_description"):
        assert field not in json.dumps(candidate)
    dropped = store.history(NS, "ucdp:conflict_event:990003", scopes=SCOPES)
    assert [h["content"]["coding_status"] for h in dropped] == ["candidate", "dropped-in-release"]
    assert dropped[1]["content"]["dropped_by_release"]["dataset_version"] == "99.1"
    assert [h["content"]["coding_status"] for h in store.history(NS, "ucdp:conflict_event:990010", scopes=SCOPES)] == ["final"]


def test_incomplete_final_release_never_drops_a_candidate():
    ged = source("ucdp-ged-sdn")
    pages = fixture_builder.native_pages("ucdp-ged-sdn")
    pages[0]["json"] = {**pages[0]["json"], "TotalPages": 3}
    adapter = HumanitarianAdapter(ged, transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "events", "parameters": {}}, cursor=None)
    assert caught.value.code == "budget_exhausted"  # more pages than the source allows: stop, record nothing as dropped


def test_acled_is_declined_by_default_and_never_fetched():
    transport, calls = spy([])
    adapter = HumanitarianAdapter(source("acled-events"), transport=transport, secret=FIXTURE_SECRET)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "events", "parameters": {}}, cursor=None)
    assert caught.value.code == "licence_declined" and not calls
    assert ACLED_DECISION["status"] == "declined" and ACLED_DECISION["query_notice"] == "not acquired (licence)"
    assert source("acled-events")["humanitarian"]["licence_decision"]["reference"].endswith(
        "#acled-licence-and-access-decision")


def test_acled_adapter_under_an_accepted_decision_keeps_acled_coding_separate():
    accepted = source("acled-events", licence_decision={"status": "accepted", "recorded": "2099-01-01",
                                                        "reference": "hypothetical accepted decision (test only)"})
    row = {"event_id_cnty": "SDN99001", "event_date": "2098-05-10", "timestamp": 4081000000, "time_precision": 1,
           "geo_precision": 2, "event_type": "Battles", "sub_event_type": "Armed clash", "actor1": "Fixture Armed Forces",
           "actor2": "Fixture Militia", "country": "Sudan", "iso3": "SDN", "admin1": "Khartoum", "location": "Khartoum",
           "latitude": "15.55", "longitude": "32.53", "fatalities": 3, "notes": "free text", "source": "Fixture Wire"}
    pages = [{"url": "https://acleddata.com/api/acled/read", "json": {"status": 200, "data": [row]}}]
    with pytest.raises(SourcePackError):
        HumanitarianAdapter(accepted, transport=fixture_transport(pages)).fetch_page(
            {"operation": "events", "parameters": {}}, cursor=None)  # still needs a credential
    page = HumanitarianAdapter(accepted, transport=fixture_transport(pages), secret=FIXTURE_SECRET).fetch_page(
        {"operation": "events", "parameters": {}}, cursor=None)
    record = page.records[0]["humanitarian_record"]
    assert record["coding_source"] == "acled" and record["counts"] == {"fatalities": 3}
    assert record["precision"]["where"] == {"code": 2, "scheme": "acled-geo_precision", "label": None}
    assert "notes" not in record and "free text" not in json.dumps(record)


@pytest.mark.parametrize("source_id,override", [
    ("reliefweb-reports-sdn", {"limit": 5000}),
    ("ucdp-ged-sdn", {"end_date": "2100-12-31"}),
    ("hdx-sdn-datasets", {"max_hxl_resources": 500}),
])
def test_unbounded_selections_are_rejected(source_id, override):
    raw = json.loads(PACK.read_text())
    item = next(s for s in raw["sources"] if s["source_id"] == source_id)
    item["humanitarian"]["selection"].update(override)
    candidate = next(s for s in validate_source_pack(raw)["sources"] if s["source_id"] == source_id)
    with pytest.raises(SourcePackError) as caught:
        HumanitarianAdapter(candidate, transport=fixture_transport([]))
    assert caught.value.code == "unbounded_source"


def test_a_failed_refresh_reads_stale_and_keeps_stored_revisions(acquired):
    store = HumanitarianStore(acquired.conn, initialize=False)
    before = store.revision(NS, "ucdp:conflict_event:990001", scopes=SCOPES)
    runtime = acquired.runtime()
    candidate = copy.deepcopy(next(s for s in runtime._manifest("humanitarian-response")[0]["sources"]
                                   if s["source_id"] == "ucdp-candidate-sdn"))
    failing = HumanitarianAdapter(candidate, transport=lambda **_k: {"status": 503, "headers": {}, "content": b""})
    receipt = runtime.run({"pack_id": "humanitarian-response", "run_key": "fail-1", "operation": "events",
                           "source_ids": ["ucdp-candidate-sdn"], "max_results": 100, "max_bytes": 1_000_000,
                           "timeout_ms": 10_000}, principal_id="operator", adapters={"ucdp-candidate-sdn": failing},
                          dns_resolver=lambda _h: ["8.8.8.8"], secret_resolver=lambda _r: FIXTURE_SECRET)
    assert receipt["status"] != "complete"
    state = store.provider_state(NS, "ucdp-candidate")
    assert state["stale"] and state["last_failure_code"].startswith("source_run_")
    assert store.revision(NS, "ucdp:conflict_event:990001", scopes=SCOPES)["revision_id"] == before["revision_id"]
