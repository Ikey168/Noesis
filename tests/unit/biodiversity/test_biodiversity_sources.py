"""BD01 and BD03-BD05 (#2504, #2511, #2515, #2518): contracts, bounded acquisition, releases, occurrences, IUCN."""

import json

import pytest

from src.ingestion.biodiversity_sources import (
    IUCN_LICENCE_DECISION,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    SENSITIVE_SPECIES_POLICY,
    selection_entries,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, validate_source_pack
from tests.unit.biodiversity import fixture_builder, harness
from tests.unit.biodiversity.harness import NS


def test_fixtures_are_pinned_and_in_sync():
    for path, text in fixture_builder.build(write=False).items():
        assert path.read_text(encoding="utf-8") == text, path
    result = SourcePackConformance(harness.ROOT).offline(json.loads(harness.PACK.read_text()))
    assert result["valid"] and len(result["sources"]) == 3


def test_contracts_record_licences_iucn_decision_sensitive_policy_and_live_state():
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert {"licence", "terms_url", "attribution", "redistribution", "rate_limits", "authentication",
                "versioning", "access"} <= set(contract), provider
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live" and LIVE_VERIFICATION[provider]["intended"]
    assert "CC BY-NC" in PROVIDER_CONTRACTS["gbif"]["licence"] and "DOI" in PROVIDER_CONTRACTS["gbif"]["attribution"]
    assert "monthly" in PROVIDER_CONTRACTS["col"]["versioning"]
    assert IUCN_LICENCE_DECISION["decision"] == "reference-only" and IUCN_LICENCE_DECISION["withheld"]
    assert "never de-generalised" in SENSITIVE_SPECIES_POLICY["coordinates"]
    pack = harness.manifest()
    iucn = next(s for s in pack["sources"] if s["biodiversity"]["provider"] == "iucn")
    assert iucn["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_IUCN_API_TOKEN"}
    assert {s["biodiversity"]["live_verification"] for s in pack["sources"]} == {"unverified-live"}
    audit = (harness.ROOT / "docs/development/biodiversity-evidence/source-audit.md").read_text()
    assert "reference-only" in audit and "Sensitive-species policy" in audit and "Bounded coverage" in audit


def test_selections_are_bounded_and_fail_closed():
    source = json.loads(json.dumps(next(s for s in harness.manifest()["sources"]
                                        if s["biodiversity"]["provider"] == "gbif")))
    unbounded = [{"kind": "occurrences", "taxon_key": "1", "limit": 20}]
    for bad in (unbounded, [{"kind": "occurrences", "taxon_key": "1", "country": "DE", "limit": 5000}],
                [{"kind": "download"}]):
        with pytest.raises(SourcePackError):
            selection_entries({**source, "biodiversity": {**source["biodiversity"], "selection": bad}})
    with pytest.raises(SourcePackError):
        selection_entries({**source, "endpoint": "https://example.org"})


def test_col_releases_become_revisions_with_a_dated_status_change_citing_both():
    env = harness.Env().loaded()
    record = env.store.find(NS, "taxon", "col", "CCRN1")
    revisions = env.store.revisions(NS, record["record_id"])
    assert [r["release"] for r in revisions] == ["310001@2026-07-10", "310002@2026-08-12"]
    assert [r["statement"]["as_published"]["status"] for r in revisions] == ["accepted", "synonym"]
    (change,) = env.store.status_changes(NS, subject_key="col:CCRN1")
    assert change["changed_on"] == "2026-08-12" and change["from"]["release"] == "310001@2026-07-10"
    assert change["to"]["accepted"] == "CCOR1" and change["from_revision"] != change["to_revision"]
    assert not env.store.status_changes(NS, subject_key="col:PDOM1")
    classification = revisions[0]["statement"]["as_published"]["classification"]
    assert [c["rank"] for c in classification][:3] == ["kingdom", "phylum", "class"]
    again = env.run("biodiversity-2")
    assert again["status"] == "complete"
    assert len(env.store.revisions(NS, record["record_id"])) == 2  # re-reading both releases adds nothing


def test_gbif_occurrences_keep_published_precision_flags_datasets_and_licences():
    env = harness.Env().loaded()
    otters = {r["record_key"]: env.store.current(NS, r["record_id"])["statement"]
              for r in env.store.records(NS, record_type="occurrence") if r["record_key"].startswith("4022")}
    generalised, withheld = otters["4022001"]["as_published"], otters["4022002"]["as_published"]
    assert generalised["coordinates"] == {"latitude": 52.55, "longitude": 13.45}
    assert generalised["generalisation"]["generalised"] and generalised["generalisation"]["precision_m"] == 10_000.0
    assert generalised["issues"] == ["COORDINATE_ROUNDED"] and generalised["coordinate_uncertainty_m"] == 7071.0
    assert withheld["coordinates"] is None and withheld["generalisation"]["coordinates_withheld"]
    assert generalised["licence"]["id"] == "CC-BY-NC-4.0" and generalised["dataset_key"].startswith("aaaa0003")
    datasets = {r["record_key"]: env.store.current(NS, r["record_id"])["statement"]["as_published"]
                for r in env.store.records(NS, record_type="dataset", provider="gbif")}
    assert datasets["0012345-260901000000000"]["kind"] == "download"
    assert datasets["0012345-260901000000000"]["doi"] == "10.5555/gbif.fixture.dl1"
    assert {d["licence"]["id"] for d in datasets.values() if d["kind"] == "occurrence"} == {
        "CC-BY-4.0", "CC0-1.0", "CC-BY-NC-4.0"}


def test_upstream_changes_are_revisions_and_removals_are_tombstones():
    env = harness.Env().loaded()
    env.advance(7)
    later = env.run("biodiversity-later", later=True)
    assert later["status"] == "complete"
    by_key = {r["record_key"]: r for r in env.store.records(NS, record_type="occurrence")}
    removed = env.store.revisions(NS, by_key["4011003"]["record_id"])
    assert [r["event"] for r in removed] == ["published", "removed"]
    assert removed[0]["statement"]["as_published"]["gbif_id"] == "4011003"  # earlier revision stays readable
    changed = env.store.revisions(NS, by_key["4011002"]["record_id"])
    assert [r["statement"]["as_published"]["coordinate_uncertainty_m"] for r in changed] == [5000.0, 2000.0]
    assert "4011004" in by_key
    runs = env.store.runs(NS)
    gbif = [r for r in runs if r["source_id"] == "gbif-species-occurrences"][-1]
    assert any(o["removed"] == 1 for o in gbif["outcomes"])


def test_iucn_reference_only_history_with_the_assessors_latest_designation():
    env = harness.Env().loaded()
    assessments = {r["record_key"]: env.store.current(NS, r["record_id"])["statement"]
                   for r in env.store.records(NS, record_type="conservation_assessment")}
    assert set(assessments) == {"900001", "900002", "900003", "800001", "800002"}
    latest = {k for k, v in assessments.items() if v["as_published"]["latest"]}
    assert latest == {"900003", "800001", "800002"}  # global and European designations stay distinct
    europe = assessments["800001"]["as_published"]["scope"]
    assert europe == {"kind": "regional", "label": "Europe", "code": "2"}
    assert assessments["900001"]["as_published"]["criteria"] == "A2ace"
    assert "criteria" in assessments["900002"]["unknowns"]  # listing only: no document fetched
    for statement in assessments.values():
        assert statement["as_published"]["licence_tier"] == "reference-only"
        assert not {"documentation", "threats", "habitats", "population_trend", "locations"} & set(
            statement["as_published"])
    iucn = [r for r in env.store.runs(NS) if r["source_id"] == "iucn-red-list-reference"][-1]
    dropped = {f for o in iucn["outcomes"] for f in o["withheld_fields_dropped"]}
    assert {"assessment:documentation", "assessment:threats", "assessment:habitats"} <= dropped


def test_iucn_needs_its_credential_and_failures_are_receipted():
    env = harness.Env()
    source = next(s for s in env.runtime._manifest(env.value["pack_id"])[0]["sources"]
                  if s["source_id"] == "iucn-red-list-reference")
    from src.ingestion.biodiversity_sources import BiodiversitySourceAdapter, fixture_transport

    adapter = BiodiversitySourceAdapter(source, transport=fixture_transport([]), secret=None)
    with pytest.raises(SourcePackError) as error:
        adapter.fetch_page({"operation": "biodiversity", "parameters": {}}, cursor=None)
    assert error.value.code == "authentication_failed"
    limited = BiodiversitySourceAdapter(source, secret="fixture-credential-not-a-real-token",
                                        transport=lambda **_: {"status": 429, "headers": {"Retry-After": "5"},
                                                               "content": b"", "origin": "fixture"})
    with pytest.raises(SourcePackError) as rate:
        limited.fetch_page({"operation": "biodiversity", "parameters": {}}, cursor=None)
    assert rate.value.code == "rate_limited"
    assert validate_source_pack(json.loads(harness.PACK.read_text()))["pack_id"] == "climate-environment-biodiversity"
