"""GFW, FAO FishStat, RFMO register and IUU list acquisition through the source-pack runtime (FI01, FI03-FI06)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.ingestion.fisheries_sources import (
    EXCLUDED_FIELDS,
    EXCLUDED_RFMOS,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    FisheriesSourceAdapter,
    fixture_transport,
    selection_entries,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, validate_source_pack
from src.kb.fisheries_records import METHOD_NOTE_GFW
from tests.unit.fisheries import harness as h

NS = h.NS


@pytest.fixture()
def env():
    item = h.Env()
    yield item
    item.conn.close()


def _records(env, record_type, provider=None, subject=None):
    return [r for r in env.store.records(NS, record_type=record_type, provider=provider)
            if subject is None or r["subject_key"] == subject]


def _latest(env, record):
    return env.store.revisions(NS, record["record_id"])[-1]


def test_pack_validates_pins_fixtures_and_records_access_decisions():
    raw = json.loads(h.PACK.read_text())
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text())
    pack = validate_source_pack(raw)
    assert not list(Draft7Validator(schema).iter_errors(pack))
    result = SourcePackConformance(h.ROOT).offline(raw)
    assert result["valid"] and len(result["sources"]) == 6
    assert {s["fisheries"]["provider"] for s in pack["sources"]} == set(PROVIDER_CONTRACTS)
    gfw = next(s for s in pack["sources"] if s["fisheries"]["provider"] == "gfw")
    assert gfw["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_GFW_API_TOKEN"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert {"licence", "terms_url", "attribution", "rate_limits", "revision_behaviour", "authentication",
                "access"} <= set(contract), provider
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    decision = PROVIDER_CONTRACTS["gfw"]["licence_decision"]
    assert "per-vessel tracks or positions" in decision["not_stored"] and "not confirmed" in METHOD_NOTE_GFW
    assert {"ccsbt", "iattc"} <= set(EXCLUDED_RFMOS) and "track" in EXCLUDED_FIELDS
    assert "fisheries-evidence/source-audit.md" in raw["description"]
    assert "fixture-credential" not in json.dumps(pack)


def test_selections_are_explicit_and_bounded():
    iccat = copy.deepcopy(next(s for s in h.manifest()["sources"] if s["source_id"] == "iccat-vessel-lists"))
    provider, entries = selection_entries(iccat)
    assert provider == "iccat" and len(entries) == 2
    unbounded = copy.deepcopy(iccat)
    del unbounded["fisheries"]["selection"][0]["flags"]
    with pytest.raises(SourcePackError) as caught:
        selection_entries(unbounded)
    assert caught.value.code == "invalid_manifest"
    moved = copy.deepcopy(iccat)
    moved["endpoint"] = "https://example.org"
    with pytest.raises(SourcePackError):
        selection_entries(moved)


def test_gfw_identity_segments_and_effort_aggregates_only(env):
    assert env.run(source_ids=["gfw-vessels-effort"])["status"] == "complete"
    outcomes = env.store.runs(NS)[0]["outcomes"]
    assert [o["outcome"] for o in outcomes].count("not_found") == 1
    assert outcomes[0]["excluded_fields_dropped"] == ["vessel:track"]  # tracks are never stored
    segments = _records(env, "vessel", "gfw", "gfw:vessel:a1b2c3d4-0002-4000-8000-000000000002")
    published = [_latest(env, r)["statement"]["as_published"] for r in segments]
    assert [(p["shipname"], p["flag"], p["transmission_from"]) for p in published] == [
        ("SAMPLE STAR", "GHA", "2018-01-01"), ("SAMPLE NOVA", "TGO", "2025-01-15")]
    source = _latest(env, segments[0])["statement"]["source"]
    assert source["dataset_version"] == "public-global-vessel-identity:v3.0"
    effort = _records(env, "effort_aggregate", "gfw")
    assert len(effort) == 3
    cell = _latest(env, effort[0])["statement"]
    assert cell["as_published"]["method"] == METHOD_NOTE_GFW and cell["as_published"]["unit"] == "hours"
    assert cell["as_published"]["grid"]["resolution_deg"] == 0.1
    assert cell["source"]["dataset_version"] == "public-global-fishing-effort:v3.0"
    assert not {"track", "positions", "events", "vesselIDs"} & set(cell["as_published"])


def test_fishstat_catch_keeps_units_status_flags_and_release_revisions(env):
    assert env.run(source_ids=["fao-fishstat-capture"])["status"] == "complete"
    rows = _records(env, "catch_observation", "fao-fishstat")
    assert len(rows) == 6 and not any("JPN" in r["record_key"] for r in rows)  # outside the bounded selection
    estimate = next(r for r in rows if r["record_key"] == "capture:GHA:SKJ:2021:Q_tlw")
    published = _latest(env, estimate)["statement"]["as_published"]
    assert published["status_flags"] == ["E"] and published["unit"] == "t (tonnes live weight)"
    assert published["quantity"] == "50000" and published["release"] == "2025.1"
    assert env.run("release-2", source_ids=["fao-fishstat-capture"], overrides=h.LATER["release"])["status"] \
        == "complete"
    revisions = env.store.revisions(NS, estimate["record_id"])
    assert [(r["release"], r["statement"]["as_published"]["quantity"],
             r["statement"]["as_published"]["status_flags"]) for r in revisions] == [
        ("2025.1", "50000", ["E"]), ("2026.1", "51200", [])]
    assert revisions[1]["supersedes"] == revisions[0]["revision_id"]
    releases = [s["release"] for s in env.store.snapshots(NS, provider="fao-fishstat")]
    assert releases == ["2025.1", "2026.1"]


def test_registers_keep_snapshot_date_retrieval_time_and_removals(env):
    assert env.run(source_ids=["iccat-vessel-lists", "iotc-vessel-lists", "wcpfc-vessel-lists"])["status"] \
        == "complete"
    iccat = _records(env, "authorisation", "iccat")
    assert {r["record_key"] for r in iccat} == {"authorisation:AT000ESP00001", "authorisation:AT000GHA00002"}
    star = next(r for r in iccat if r["record_key"].endswith("GHA00002"))
    first = _latest(env, star)
    assert first["statement"]["source"]["snapshot_date"] == "2026-09-01" and first["observed_at_ms"] > 0
    assert (first["statement"]["as_published"]["valid_from"], first["statement"]["as_published"]["valid_to"]) == (
        "2023-01-01", "2024-12-31")
    outcome = next(o for o in env.store.runs(NS) if o["source_id"] == "iccat-vessel-lists")["outcomes"][0]
    assert outcome["excluded_fields_dropped"] == ["authorised-vessels:OwnerAddress"]
    assert "Calle" not in json.dumps(first["statement"])
    coral = next(r for r in _records(env, "authorisation", "iotc") if r["record_key"].endswith("000102"))
    assert _latest(env, coral)["statement"]["as_published"]["imo_malformed"] is True
    assert env.run("second", source_ids=["iccat-vessel-lists"], overrides=h.LATER["second"])["status"] \
        == "complete"
    revisions = env.store.revisions(NS, star["record_id"])
    assert [(r["event"], r["effective_from"]) for r in revisions] == [("authorised", "2023-01-01"),
                                                                      ("removed", "2026-11-01")]
    snapshots = env.store.snapshots(NS, list_key="iccat:authorised-vessels")
    assert [(s["snapshot_date"], s["removed"]) for s in snapshots] == [("2026-09-01", 0), ("2026-11-01", 1)]
    assert all(s["retrieved_at_ms"] for s in snapshots)


def test_iuu_lists_keep_reasons_verbatim_delistings_and_combined_provenance(env):
    assert env.run(source_ids=["iccat-vessel-lists", "iotc-vessel-lists", "combined-iuu-vessel-list"])["status"] \
        == "complete"
    (drifter,) = _records(env, "listing", "iccat")
    published = _latest(env, drifter)["statement"]["as_published"]
    assert published["stated_reason"].startswith("Fishing activities in the Convention area")
    assert (published["listed_on"], published["delisted_on"]) == ("2016-11-20", "2024-11-18")
    assert _latest(env, drifter)["event"] == "delisted"
    (nova,) = _records(env, "listing", "iotc")
    identities = _latest(env, nova)["statement"]["as_published"]["previous_identities"]
    assert identities == {"names": ["SAMPLE STAR"], "flags": ["GHA"]}
    combined = _records(env, "listing", "combined-iuu")
    assert len(combined) == 3
    for record in combined:
        statement = _latest(env, record)["statement"]["as_published"]
        assert statement["independent_confirmation"] is False and statement["originating_listings"]
    assert env.run("second", source_ids=["iccat-vessel-lists"], overrides=h.LATER["second"])["status"] \
        == "complete"
    late = next(r for r in _records(env, "listing", "iccat") if r["record_key"].endswith("20260007"))
    assert _latest(env, late)["event"] == "listed"
    assert not h.forbidden_keys([_latest(env, r)["statement"] for r in env.store.records(NS)])


def test_failures_are_classified_and_the_gfw_token_is_required(env):
    installed = env.runtime._manifest(env.value["pack_id"])[0]
    source = next(s for s in installed["sources"] if s["source_id"] == "gfw-vessels-effort")
    adapter = FisheriesSourceAdapter(source, transport=fixture_transport(h.pages("gfw-vessels-effort")), secret=None)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "fisheries", "parameters": {}}, cursor=None)
    assert caught.value.code == "authentication_failed"
    native = h.pages("iccat-vessel-lists")
    native[0]["status"] = 503
    iccat = next(s for s in installed["sources"] if s["source_id"] == "iccat-vessel-lists")
    broken = {"iccat-vessel-lists": env.runtime.factory.compile(iccat, transport=fixture_transport(native))}
    assert env.run("broken", source_ids=["iccat-vessel-lists"], adapters=broken)["status"] != "complete"
    assert env.store.runs(NS)[-1]["status"] == "failed"
    drift = h.pages("iccat-vessel-lists")
    drift[0]["body"] = "Serial,Name\n1,x\n"
    adapter = FisheriesSourceAdapter(iccat, transport=fixture_transport(drift))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "fisheries", "parameters": {}}, cursor=None)
    assert caught.value.code == "schema_drift"
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "fisheries", "parameters": {"flag": "all"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
