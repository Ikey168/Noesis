"""H01 source contracts and H03-H06 acquisition through the source-pack runtime (offline fixtures)."""

import json

import pytest

from src.ingestion import clinical_providers as cp
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, load_source_packs, validate_source_pack
from src.kb.clinical_records import ClinicalRecordError, ClinicalRecordStore
from tests.unit.clinical import harness
from tests.unit.clinical.harness import NS, ROOT, Env, load

OPS = {"knowledge:clinical:read", f"namespace:{NS}:read"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("offline tests must not open network connections")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


def test_contracts_document_access_identifiers_and_unavailable_fallbacks():
    required = {"documentation", "access", "authentication", "rate_limits", "pagination", "cadence", "terms",
                "retained_evidence", "identifiers", "cross_references", "status", "unavailable_fallback"}
    for provider in ("ctgov", "ctis", "euctr", "openfda", "ema"):
        assert required <= set(cp.PROVIDER_CONTRACTS[provider]), provider
        assert cp.PROVIDER_HOSTS[provider]
    for provider in ("prospero", "who-ictrp", "cochrane"):
        contract = cp.PROVIDER_CONTRACTS[provider]
        assert contract["status"] == "not-implemented" and contract["reason"]
        assert cp.LIVE_VERIFICATION[provider]["status"] == "not-implemented"
    for provider in ("europe-pmc", "pubmed", "medrxiv-biorxiv"):
        assert cp.PROVIDER_CONTRACTS[provider]["status"] == "reused"
    identifiers = " ".join(str(c.get("identifiers")) for c in cp.PROVIDER_CONTRACTS.values())
    for name in ("NCT", "EU CT", "EudraCT", "PMID", "DOI", "NDA", "CRD"):
        assert name in identifiers
    # Nothing is claimed live: the last bounded check was blocked by the build network.
    for provider in ("ctgov", "ctis", "euctr", "openfda", "ema"):
        assert cp.LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert "blocked" in cp.LIVE_VERIFICATION[provider]["last_check"]["result"]


def test_source_pack_declares_each_source_with_pinned_fixtures_that_replay():
    pack = next(p for p in load_source_packs(ROOT / "config/source_packs") if p["pack_id"] == "clinical-evidence")
    # surveillance (#1917) and medicines (#2214) sources have their own tests
    clinical = [s for s in pack["sources"] if s["mapping"]["target_schema"] == "noesis-clinical-record-v1"]
    assert {s["connector"] for s in clinical} == {"ctgov", "ctis", "eu-ctr", "openfda", "ema-medicines"}
    assert {s["mapping"]["target_schema"] for s in pack["sources"] if s not in clinical} == {
        "noesis-surveillance-record-v1", "noesis-clinical-medicines-record-v1"}
    openfda = next(s for s in pack["sources"] if s["connector"] == "openfda")
    assert openfda["auth"] == {"kind": "optional-secret", "secret_ref": "NOESIS_OPENFDA_API_KEY"}
    result = SourcePackConformance(ROOT).offline(pack)
    assert result["valid"], result["sources"]
    fixtures = [json.loads((ROOT / s["fixture"]["path"]).read_text()) for s in clinical]
    assert all(f["authored"] is True and "not a live capture" in f["note"] for f in fixtures)


def test_ctgov_versions_become_revisions_and_secondary_ids_are_kept():
    env = Env()
    receipt = env.acquire("r1", ["ctgov-trial-history"])
    assert receipt["status"] == "complete"
    source = receipt["sources"][0]
    assert source["counts"]["pages"] == 2 and source["cursor"]["end"] is None
    store = ClinicalRecordStore(env.conn)
    rid = store.trial_id(NS, "ctgov", "NCT09000001")
    history = store.history(NS, rid, scopes=OPS)
    assert [r["version_date"] for r in history["revisions"]] == ["2015-11-20", "2016-06-03", "2017-11-20",
                                                                 "2019-02-14"]
    current = history["revisions"][-1]["record"]
    assert {(s["kind"], s["value"]) for s in current["secondary_identifiers"]} >= {
        ("eudract", "2015-900001-10"), ("sponsor-protocol", "NTG-3001")}
    assert current["registration"]["prospective"] is True
    evidence = store.get(NS, rid, scopes=OPS)["evidence"]
    assert evidence["source_pack"]["pack_id"] == "clinical-evidence" and evidence["document"]["revision_id"]
    assert {r["path"] for r in evidence["capture"]["requests"]} >= {"/api/v2/studies/NCT09000001",
                                                                    "/api/int/studies/NCT09000001/history"}
    assert all(len(r["sha256"]) == 64 for r in evidence["capture"]["requests"])
    postings = store.find(NS, scopes=OPS, kinds={"result-posting"})
    assert [p["native_id"] for p in postings] == ["NCT09000001"]
    assert postings[0]["record"]["outcome_results"][0]["analyses"][0]["p_value"] == "<0.001"
    assert store.provider_state(NS, "ctgov")["last_execution"] == "injected"


def test_ctgov_search_follows_page_tokens_within_budget():
    env = Env()
    receipt = env.acquire("r1", ["ctgov-question-search"])
    source = receipt["sources"][0]
    assert source["status"] == "complete" and source["counts"]["pages"] == 2 and source["counts"]["fetched"] == 3
    assert any("pageToken=FIXTURE-TOKEN-2" in c["url"] for c in env.web.calls)
    store = ClinicalRecordStore(env.conn)
    assert {r["native_id"] for r in store.find(NS, scopes=OPS, kinds={"registered-trial"})} == {
        "NCT09000001", "NCT09000002", "NCT09000003"}
    rid = store.trial_id(NS, "ctgov", "NCT09000001")
    assert store.get(NS, rid, scopes=OPS)["version_key"].startswith("current-record:")


def test_eu_registries_project_member_states_results_and_link_to_ctgov():
    env = Env()
    env.acquire("r1", ["ctis-trials", "euctr-trials", "ctgov-question-search"])
    store = ClinicalRecordStore(env.conn)
    ctis = store.trial(NS, store.trial_id(NS, "ctis", "2023-509001-12-00"), scopes=OPS)
    assert [s["decision"] for s in ctis["trial"]["member_states"]] == ["authorised", "authorised"]
    assert ctis["trial"]["status"]["normalized"] == "ongoing"
    euctr = store.trial(NS, store.trial_id(NS, "euctr", "2015-900001-10"), scopes=OPS)
    assert [s["state"] for s in euctr["trial"]["member_states"]] == ["Germany", "France"]
    posting = euctr["result_postings"][0]["record"]
    assert posting["content_acquired"] is False and posting["results_summary_url"].startswith("https://")
    assert "result_content" in posting["unknowns"]
    for view in (ctis, euctr):
        linked = view["cross_registry"][0]
        assert linked["registry"] == "ctgov" and linked["record_id"] and linked["merged"] is False
    assert {d["field"] for d in ctis["cross_registry"][0]["disagreements"]} >= {"status",
                                                                                "design.enrollment_planned"}


def test_ctis_result_summaries_are_postings_whose_content_is_not_acquired():
    payload = load("ctis_2023-509001-12-00.json")
    payload["results"] = {"summaryResults": [{"url": "https://euclinicaltrials.eu/fixture-summary.pdf",
                                              "submissionDate": "2027-01-10"}]}
    parsed = cp.parse_ctis_trial(payload)
    posting = next(r for r in parsed["records"] if r["record_kind"] == "result-posting")
    assert posting["content_acquired"] is False and posting["posted"]["first_posted"] == "2027-01-10"
    assert parsed["records"][0]["results_indicator"]["has_results"] is True
    assert posting["results_summary_url"] == "https://euclinicaltrials.eu/fixture-summary.pdf"


def test_openfda_keeps_disclaimers_reporting_counts_and_no_dosing_text():
    env = Env()
    env.acquire("r1", ["openfda-products"])
    store = ClinicalRecordStore(env.conn)
    records = {r["record"]["regulatory_kind"]: r["record"] for r in store.find(NS, scopes=OPS,
                                                                                kinds={"regulatory-record"})}
    assert set(records) == {"label-revision", "approval", "adverse-event-summary"}
    for item in records.values():
        assert item["disclaimer"]["text"].startswith("Do not rely on openFDA")
    label = records["label-revision"]
    assert label["native_version"] == {"basis": "label-version", "date": "2025-04-15", "version": "7"}
    assert "dosage_and_administration" not in label["sections"]
    assert label["omitted_sections"][0]["section"] == "dosage_and_administration"
    events = records["adverse-event-summary"]
    assert events["counts"][0] == {"term": "NAUSEA", "reports": 412} and events["reports_total"] == 1210
    assert "not incidence" in events["count_semantics"] and "caused" in events["count_semantics"]
    assert records["approval"]["dates"]["original_approval"] == "2019-09-05"
    assert "strength" not in json.dumps(records["approval"]["product"])


def test_openfda_api_key_is_sent_as_a_parameter_and_never_stored():
    env = Env()
    manifest, _ = env.runtime._manifest("clinical-evidence")
    source = next(s for s in manifest["sources"] if s["source_id"] == "openfda-products")
    secret = "fixture-openfda-key-123456"
    adapter = env.runtime.factory.compile(source, transport=env.web.transport, secret=secret)
    receipt = env.runtime.run({"pack_id": "clinical-evidence", "run_key": "keyed", "operation": "records",
                               "source_ids": ["openfda-products"]}, principal_id="operator",
                              adapters={"openfda-products": adapter})
    assert receipt["sources"][0]["status"] == "complete"
    assert len(ClinicalRecordStore(env.conn).find(NS, scopes=OPS, kinds={"regulatory-record"})) == 3
    assert all(call["params"].get("api_key") == secret for call in env.web.calls)
    assert all(secret not in call["url"] for call in env.web.calls)
    stored = json.dumps([row for row in env.conn.execute("SELECT * FROM clinical_record_revisions").fetchall()])
    runs = json.dumps([row for row in env.conn.execute("SELECT * FROM source_pack_source_runs").fetchall()], default=str)
    assert secret not in stored and secret not in runs
    env.web.set("/drug/label.json?search=openfda.generic_name%3A%22noetiglutide%22&limit=3",
                {"meta": {"disclaimer": "x"}, "results": [], "echo": secret})
    echoed = env.runtime.run({"pack_id": "clinical-evidence", "run_key": "echo", "operation": "records",
                              "source_ids": ["openfda-products"]}, principal_id="operator",
                             adapters={"openfda-products": env.runtime.factory.compile(
                                 source, transport=env.web.transport, secret=secret)})
    assert echoed["sources"][0]["failure"]["code"] == "authentication_failed"


def test_ema_and_prospero_records_with_documented_access():
    env = Env()
    env.acquire("r1", ["ema-medicines"])
    store = ClinicalRecordStore(env.conn)
    [ema] = store.find(NS, scopes=OPS, kinds={"regulatory-record"})
    assert ema["record"]["status"]["normalized"] == "authorised"
    assert ema["record"]["native_version"] == {"basis": "revision-number", "date": "2026-06-01", "version": "4"}
    assert ema["record"]["documents"][0]["kind"] == "epar"
    assert "export_path" in json.dumps(env.manifest)  # access method: the published export file
    protocol = env.review_protocol()
    imported = env.import_prospero(protocol_id=protocol["protocol_id"])
    assert imported["registration"]["source"]["kind"] == "user-supplied-export"
    assert imported["protocol_link"]["registry"] == "prospero"
    from src.kb.systematic_reviews import SystematicReviewStore

    links = SystematicReviewStore(env.conn).registrations(NS, protocol["protocol_id"], principal_id="alice",
                                                           scopes=env.scopes())
    assert links[0]["registration_id"] == "CRD42099000001"
    with pytest.raises(ClinicalRecordError):
        cp.parse_prospero_export({"title": "no number"}, supplied_by="alice")


def test_parsers_fail_closed_and_failures_mark_sources_stale_without_changing_records():
    with pytest.raises(SourcePackError) as caught:
        cp.parse_ctgov_study({"protocolSection": {}})
    assert caught.value.code == "schema_drift"
    with pytest.raises(SourcePackError):
        cp.parse_openfda_labels({"results": []}, query="q")  # no disclaimer: refused
    with pytest.raises(SourcePackError):
        cp.parse_ctis_trial({"ctNumber": "2023-509001-12-00"})
    env = Env()
    env.acquire("r1", ["ctis-trials"])
    store = ClinicalRecordStore(env.conn)
    before = store.find(NS, scopes=OPS)
    env.web.set("/ctis-public-api/retrieve/2023-509001-12-00", {"unexpected": True})
    failed = env.acquire("r2", ["ctis-trials"])
    assert failed["sources"][0]["status"] == "failed"
    assert store.find(NS, scopes=OPS) == before
    state = store.provider_state(NS, "ctis")
    assert state["stale"] is True and state["last_failure_code"] == "schema_drift"
    env.web.set("/ctis-public-api/retrieve/2023-509001-12-00", None, status=503)
    env.acquire("r3", ["ctis-trials"])
    assert store.provider_state(NS, "ctis")["last_failure_code"] == "source_unavailable"


def test_bounded_selection_is_enforced():
    manifest = json.loads(harness.PACK.read_text())
    source = next(s for s in manifest["sources"] if s["connector"] == "ctgov")
    source = {**source, "clinical": {**source["clinical"], "nct_ids": [f"NCT0{i:07d}" for i in range(51)]}}
    with pytest.raises(SourcePackError) as caught:
        normalized = validate_source_pack({**manifest, "sources": [source]})["sources"][0]
        cp.CtgovAdapter(normalized)
    assert caught.value.code == "unbounded_source"
