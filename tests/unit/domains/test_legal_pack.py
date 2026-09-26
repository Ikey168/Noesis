"""Legal pack: CELLAR/RII/Berlin adapters, legal store, as-of selection, passages and retrieval gating.

CELLAR fixtures replay the SPARQL bindings captured live on 2026-09-09; court
decisions are rebuilt from the parsed 2026-09-08 captures; Berlin pages are
authored fictional publications. No test touches the network.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import zipfile
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.domains.pack_format import PackManifest, validate_manifest
from src.domains.pack_install import install_manifest, uninstall
from src.ingestion.legal_sources import (
    BerlinLegalAdapter,
    CellarLegalAdapter,
    RiiDecisionAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import (
    SUPPORTED_CONNECTORS,
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)
from src.kb.legal import READ_SCOPE, WRITE_SCOPE, LegalError, LegalStore, readiness
from src.kb.legal_retrieval import (
    RetrievalEvaluationError,
    decide,
    evaluate,
    require_enabled,
    retrieval_modes,
    validate_judgments,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/legal.json"
SCHEMAS = ROOT / "contracts/schemas/jsonschema"
SCOPES = {READ_SCOPE, WRITE_SCOPE, "namespace:global:write"}
PUBLIC_DNS = lambda _host: ["8.8.8.8"]


def schema(name):
    return jsonschema.Draft7Validator(json.loads((SCHEMAS / f"{name}.json").read_text()))


def manifest():
    return validate_source_pack(json.loads(PACK.read_text()))


def source(value, source_id):
    return next(s for s in value["sources"] if s["source_id"] == source_id)


def fixture_pages(source_id):
    value = manifest()
    return copy.deepcopy(json.loads((ROOT / source(value, source_id)["fixture"]["path"]).read_text())["native_pages"])


def adapter(cls, source_id, pages=None, **legal_overrides):
    item = copy.deepcopy(source(manifest(), source_id))
    item["legal"].update(legal_overrides)
    return cls(item, transport=fixture_transport(pages if pages is not None else fixture_pages(source_id)))


def fetch(adapter_, cursor=None):
    return adapter_.fetch_page({"operation": "records", "parameters": {}, "limit": 100}, cursor=cursor)


def drain(adapter_):
    """Fetch pages up to the source's page budget, as the runtime does."""
    records, receipts, cursor = [], [], None
    for _ in range(int(adapter_.definition["limits"]["max_pages"])):
        page = fetch(adapter_, cursor)
        records.extend(page.records)
        receipts.append(page.receipt)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records, receipts


def install(conn):
    value = manifest()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for item in value["sources"]:
        runtime.accept_license(value["pack_id"], item["source_id"], principal_id="operator")
    return value, runtime


def run(runtime, value, key, **controls):
    return runtime.run(
        {"pack_id": value["pack_id"], "run_key": key, "operation": "records", "max_results": 200,
         "max_bytes": 50_000_000, "timeout_ms": 60_000, **controls},
        principal_id="operator", adapters=runtime.fixture_adapters(value["pack_id"], ROOT), dns_resolver=PUBLIC_DNS)


@pytest.fixture(scope="module")
def loaded():
    conn = duckdb.connect(":memory:")
    value, runtime = install(conn)
    receipt = run(runtime, value, "fixture-1")
    yield conn, value, runtime, receipt, LegalStore(conn)
    conn.close()


def work(store, **query):
    result = store.lookup("global", scopes=SCOPES, **query)
    assert result["status"] == "found", result
    return result["works"][0]


# ------------------------------------------------------------------ adapters


def test_pack_declares_implemented_connectors_and_replays_offline():
    value = manifest()
    assert {s["connector"] for s in value["sources"]} == {"cellar", "rii", "berlin-law"} <= SUPPORTED_CONNECTORS
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"]
    assert {s["source_id"]: s["records"] for s in result["sources"]} == {
        "cellar-gdpr-deu": 2, "cellar-gdpr-eng": 2, "cellar-c362-14-deu": 4, "rii-federal-decisions": 3,
        "berlin-law-publications": 3}


def test_records_follow_the_legal_record_contract_and_never_claim_current_law():
    validator = schema("noesis-legal-record-v1")
    for source_id, cls in (("cellar-gdpr-deu", CellarLegalAdapter), ("rii-federal-decisions", RiiDecisionAdapter),
                           ("berlin-law-publications", BerlinLegalAdapter)):
        records, _ = drain(adapter(cls, source_id))
        for record in records:
            assert not list(validator.iter_errors(record))
            assert record["legal_record"]["is_current_law"] is None


def test_cellar_failures_are_classified():
    page = fixture_pages("cellar-gdpr-deu")[0]
    for status, body, code in ((503, None, "source_unavailable"), (200, "not json", "schema_drift"),
                               (200, {"results": {"bindings": "x"}}, "schema_drift"),
                               (403, None, "authentication_failed")):
        broken = {**page, "status": status, "body": body}
        with pytest.raises(SourcePackError) as caught:
            fetch(adapter(CellarLegalAdapter, "cellar-gdpr-deu", [broken]))
        assert caught.value.code == code
    foreign = copy.deepcopy(page)
    foreign["body"]["results"]["bindings"][0]["celex"]["value"] = "32000L0031"
    with pytest.raises(SourcePackError) as caught:
        fetch(adapter(CellarLegalAdapter, "cellar-gdpr-deu", [foreign]))
    assert caught.value.code == "schema_drift"


def test_selection_is_bounded_and_checkpointed():
    with pytest.raises(SourcePackError) as caught:
        adapter(CellarLegalAdapter, "cellar-gdpr-deu", selection={"celex": [], "languages": ["DEU"]})
    assert caught.value.code == "unbounded_source"
    with pytest.raises(SourcePackError) as caught:
        adapter(BerlinLegalAdapter, "berlin-law-publications",
                selection=[{"official_id": "x", "format": "pdf", "historical": None, "url": "https://gesetze.berlin.de/x"}])
    assert caught.value.code == "invalid_mapping"
    rii = adapter(RiiDecisionAdapter, "rii-federal-decisions")
    with pytest.raises(SourcePackError) as caught:
        rii.fetch_page({"operation": "records", "parameters": {"court": "BGH"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    first = fetch(rii)
    other = adapter(RiiDecisionAdapter, "rii-federal-decisions", selection={"decisions": ["JURE100054597"]})
    with pytest.raises(SourcePackError) as caught:
        fetch(other, first.next_cursor)
    assert caught.value.code == "cursor_drift"
    offsite = [{"official_id": "NJRE1", "format": "rendered-html", "historical": None,
                "url": "https://example.org/bsbe/document/NJRE1"}]
    with pytest.raises(SourcePackError) as caught:
        fetch(adapter(BerlinLegalAdapter, "berlin-law-publications", selection=offsite))
    assert caught.value.code == "network_policy"


def test_rii_index_window_filters_and_inaccessible_decisions():
    records, receipts = drain(adapter(RiiDecisionAdapter, "rii-federal-decisions"))
    assert receipts[0]["index"]["matched"] == 3 and receipts[0]["queue_size"] == 4  # BFH item is older than since
    assert [r["outcome"] for r in receipts[1:]] == ["not_found", "returned", "returned", "returned"]
    decisions = {r["legal_record"]["provider_id"]: r["legal_record"] for r in records}
    assert decisions["JURE100054597"]["fields"]["ecli"] is None
    assert decisions["JURE100055033"]["fields"]["cited_norms"] == "§ 4 InsO, § 13 ZPO, § 251 ZPO"
    first = decisions["JURE100054597"]["sections"][0]["locator"]["path"]
    assert first == "/dokument[1]/tenor[1]/div[1]/dl[2]/dd[1]/p[1]"  # captured locator survives
    pages = fixture_pages("rii-federal-decisions")
    zipped = next(p for p in pages if p["request"].endswith("JURE100054597.zip"))
    zipped["body"] = base64.b64encode(b"PK not a zip").decode()
    with pytest.raises(SourcePackError) as caught:
        drain(adapter(RiiDecisionAdapter, "rii-federal-decisions", pages))
    assert caught.value.code == "schema_drift"


def test_berlin_archive_must_hold_one_xml_member_and_editorial_text_is_excluded():
    records, receipts = drain(adapter(BerlinLegalAdapter, "berlin-law-publications"))
    assert [r["outcome"] for r in receipts] == ["returned", "returned", "returned", "not_found"]
    judgment = next(r["legal_record"] for r in records if r["legal_record"]["kind"] == "court-decision")
    assert "Redaktioneller Hinweis (nicht amtlich)." in judgment["native"]["editorial_or_unclassified_text"]
    assert all("Redaktioneller" not in s["text"] for s in judgment["sections"])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.xml", "<x/>")
        archive.writestr("b.xml", "<x/>")
    pages = fixture_pages("berlin-law-publications")
    pages[0]["body"] = base64.b64encode(buffer.getvalue()).decode()
    with pytest.raises(SourcePackError) as caught:
        fetch(adapter(BerlinLegalAdapter, "berlin-law-publications", pages))
    assert caught.value.code == "schema_drift"


# ---------------------------------------------------------------- the store


def test_run_projects_every_source_and_repeats_idempotently(loaded):
    conn, value, runtime, receipt, _store = loaded
    assert receipt["status"] == "complete"
    assert {s["source_id"]: s["counts"]["quarantined"] for s in receipt["sources"]} == dict.fromkeys(
        [s["source_id"] for s in value["sources"]], 0)
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("legal_works", "legal_versions", "legal_passages", "legal_citations", "legal_facts")}
    assert counts["legal_works"] == 7  # GDPR, C-362/14, three federal decisions, a Berlin law and judgment
    # CELLAR runs are bounded to the captured first page, so an incremental
    # run continues at offset 100, which was never captured.
    resumed = run(runtime, value, "incremental-2")
    cellar = {s["source_id"]: s for s in resumed["sources"] if s["source_id"].startswith("cellar")}
    assert {json.loads(s["cursor"]["start"])["offset"] for s in cellar.values()} == {100}
    again = run(runtime, value, "backfill-2", mode="backfill", backfill={"from_ms": 0})
    assert again["status"] == "complete"
    assert {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in counts} == counts
    temporal = conn.execute("SELECT count(*) FROM kb_temporal_assertions WHERE domain='legal'").fetchone()[0]
    assert temporal >= counts["legal_facts"] // 2  # facts are also bitemporal assertions


def test_identifiers_expressions_and_locators_survive(loaded):
    _, _, _, _, store = loaded
    gdpr = work(store, identifier="32016R0679")
    assert gdpr["jurisdiction"] == "EU" and gdpr["work_kind"] == "normative"
    assert "http://data.europa.eu/eli/reg/2016/679/oj" in gdpr["identifiers"]["eli"]
    inspected = store.inspect("global", gdpr["work_id"], scopes=SCOPES)
    assert {v["language"] for v in inspected["versions"]} == {"de", "en"}
    assert len({v["expression_id"] for v in inspected["versions"]}) == 2  # translations are separate
    assert {c["relation"] for c in inspected["citations"]} >= {"cites"}
    validator = schema("noesis-legal-version-v1")
    assert all(not list(validator.iter_errors(v)) for v in inspected["versions"])
    schrems = work(store, identifier="62014CJ0362")
    assert schrems["work_kind"] == "decision"
    assert schrems["identifiers"]["ecli"] == ["ECLI:EU:C:2015:650"]
    decision = work(store, identifier="IX ZB 72/08")
    inspected = store.inspect("global", decision["work_id"], scopes=SCOPES)
    assert {c["target"] for c in inspected["citations"] if c["relation"] == "cites_norm"} == {
        "§ 4 InsO", "§ 13 ZPO", "§ 251 ZPO"}
    version = inspected["versions"][0]
    passages = store.passages("global", version["version_id"], scopes=SCOPES, limit=100)["passages"]
    assert passages and all(p["locator"]["path"].startswith("/dokument[1]/") for p in passages)


def test_lookup_never_substitutes_another_jurisdiction(loaded):
    _, _, _, _, store = loaded
    with pytest.raises(LegalError) as caught:
        store.lookup("global", scopes=SCOPES, title="Gesetz")
    assert caught.value.code == "jurisdiction_required"
    miss = store.lookup("global", scopes=SCOPES, citation="InsO", jurisdiction="EU")
    assert miss["status"] == "not_found_in_jurisdiction" and miss["other_jurisdictions_with_matches"] == ["DE"]
    assert store.lookup("global", scopes=SCOPES, identifier="C-999/99")["status"] == "not_covered"
    assert not list(schema("noesis-legal-work-v1").iter_errors(miss))


def test_as_of_selection_uses_only_sourced_dates(loaded):
    _, _, _, _, store = loaded
    gdpr = work(store, identifier="32016R0679")
    validator = schema("noesis-legal-version-selection-v1")
    between = store.select_as_of("global", gdpr["work_id"], "2016-06-01", scopes=SCOPES, language="de")
    assert between["status"] == "unknown"  # two sourced entry-into-force dates straddle the day
    assert {c["state"] for c in between["candidates"]} == {"ambiguous_commencement"}
    later = store.select_as_of("global", gdpr["work_id"], "2019-01-01", scopes=SCOPES, language="de")
    assert later["status"] == "selected" and later["equivalent_manifestations"]
    assert later["other_language_expressions"] == ["en"]
    assert store.select_as_of("global", gdpr["work_id"], "2010-01-01", scopes=SCOPES)["status"] == "none_applies"
    law = work(store, title="Beispielgesetz", jurisdiction="DE-BE")
    old = store.select_as_of("global", law["work_id"], "2020-01-01", scopes=SCOPES)
    new = store.select_as_of("global", law["work_id"], "2026-02-01", scopes=SCOPES)
    assert old["selected_version_id"] != new["selected_version_id"]
    historical = next(c for c in old["candidates"] if c["version_id"] == old["selected_version_id"])
    assert historical["historical"] is True
    assert "legal review" in new["review_boundary"]
    assert all(not list(validator.iter_errors(s)) for s in (between, later, old, new))


def test_version_comparison_and_passages(loaded):
    _, _, _, _, store = loaded
    law = work(store, title="Beispielgesetz", jurisdiction="DE-BE")
    versions = store.versions("global", law["work_id"])
    diff = store.compare_versions("global", versions[0]["version_id"], versions[1]["version_id"], scopes=SCOPES)
    assert [(c["change"], c["locator"]["norm_label"]) for c in diff["changes"]] == [("changed", "§ 2")]
    assert not list(schema("noesis-legal-version-comparison-v1").iter_errors(diff))
    found = store.passages("global", versions[1]["version_id"], scopes=SCOPES, contains="binnen eines Monats")
    assert found["status"] == "found" and found["passages"][0]["locator"]["official_norm_id"].endswith("NN2")
    gdpr = work(store, identifier="32016R0679")
    metadata = store.versions("global", gdpr["work_id"])[0]
    assert store.passages("global", metadata["version_id"], scopes=SCOPES, contains="x")["status"] == \
        "metadata_only_version"
    decision = work(store, identifier="9 B 3/09")
    with pytest.raises(LegalError) as caught:
        store.compare_versions("global", versions[0]["version_id"],
                               store.versions("global", decision["work_id"])[0]["version_id"], scopes=SCOPES)
    assert caught.value.code == "different_works"


def _berlin_record(official_id, text, *, effective_from=None, effective_until=None, historical=None,
                   relationships=(), sha="a"):
    return {"contract": "noesis-native-regional-v1", "provider": "berlin-law", "provider_id": official_id,
            "kind": "law", "title": "Synthetisches Gesetz", "source_url": "https://gesetze.berlin.de/x",
            "language": "de", "published_at": None, "updated_at": None,
            "fields": {"jurisdiction": "DE-BE", "historical": historical, "effective_from": effective_from,
                       "effective_until": effective_until},
            "sections": [{"text": text, "locator": {"kind": "xml-path", "path": "/textdaten[1]/p[1]",
                                                    "official_norm_id": official_id + "NN1"}}],
            "relationships": list(relationships), "native": {"original_sha256": sha * 64},
            "is_current_law": None}


def test_delayed_commencement_repeal_correction_missing_and_conflicting_dates():
    conn = duckdb.connect(":memory:")
    store = LegalStore(conn)
    store.project("global", [
        _berlin_record("jlr-A", "Fassung 1", effective_from="2030-01-01", sha="a"),
        _berlin_record("jlr-B", "Ohne Datum", sha="b"),
        _berlin_record("jlr-C", "Aufgehoben", effective_from="2020-01-01", effective_until="2024-06-30", sha="c"),
        _berlin_record("jlr-D", "Fassung A", effective_from="2021-01-01", sha="d"),
        _berlin_record("jlr-D", "Fassung B (berichtigt)", effective_from="2021-02-01", sha="e",
                       relationships=[{"relation": "corrects", "target": "jlr-D", "basis": "operator-supplied"}]),
    ], run_id="synthetic", source_id=None)
    ids = {w: work(store, identifier=w)["work_id"] for w in ("jlr-A", "jlr-B", "jlr-C", "jlr-D")}
    assert store.select_as_of("global", ids["jlr-A"], "2026-01-01", scopes=SCOPES)["candidates"][0]["state"] \
        == "not_yet_commenced"
    missing = store.select_as_of("global", ids["jlr-B"], "2026-01-01", scopes=SCOPES)
    assert missing["status"] == "unknown" and missing["candidates"][0]["state"] == "commencement_unknown"
    assert store.select_as_of("global", ids["jlr-C"], "2025-01-01", scopes=SCOPES)["candidates"][0]["state"] == "ended"
    assert store.select_as_of("global", ids["jlr-C"], "2023-01-01", scopes=SCOPES)["status"] == "selected"
    conflicting = store.select_as_of("global", ids["jlr-D"], "2022-01-01", scopes=SCOPES)
    assert conflicting["status"] == "ambiguous" and conflicting["selected_version_id"] is None
    corrected = store.inspect("global", ids["jlr-D"], scopes=SCOPES)
    assert {c["relation"] for c in corrected["citations"]} == {"corrects"}
    assert corrected["citations"][0]["target_work_id"] == ids["jlr-D"]
    assert len(corrected["versions"]) == 2  # the corrected text does not overwrite the first version
    conn.close()


def test_dossier_links_and_namespace_isolation(loaded):
    conn, _, _, _, store = loaded
    law = work(store, title="Beispielgesetz", jurisdiction="DE-BE")
    with pytest.raises(LegalError) as caught:
        store.link_dossier("global", "dossier-1", law["work_id"], "enacted_as", "GVBl.", scopes=SCOPES,
                           principal_id="p")
    assert caught.value.code == "dossier_not_found"
    from src.domains.political.legislative_dossiers import LegislativeDossierStore

    LegislativeDossierStore(conn)
    conn.execute("INSERT OR IGNORE INTO legislative_dossiers VALUES ('dossier-1','global','p','h',1)")
    store.link_dossier("global", "dossier-1", law["work_id"], "enacted_as", "GVBl. 2019, 1 (fiktiv)",
                       scopes=SCOPES, principal_id="p")
    links = store.inspect("global", law["work_id"], scopes=SCOPES)["procedure_links"]
    assert links[0]["relation"] == "enacted_as" and "not the authoritative legal text" in links[0]["notice"]
    other = {READ_SCOPE, "namespace:other:read"}
    with pytest.raises(LegalError) as caught:
        store.inspect("global", law["work_id"], scopes=other)
    assert caught.value.code == "unauthorized"
    assert store.lookup("other", scopes=other, identifier="32016R0679")["status"] == "not_covered"


# ------------------------------------------------------------ retrieval gate


def _eval_inputs():
    queries = [{"query_id": f"q{i}", "jurisdiction": "DE-BE" if i % 2 else "EU", "language": "de",
                "version": "v1", "split": "held-out"} for i in range(4)]
    judgments = []
    for i in range(4):
        for assessor in ("a1", "a2"):
            judgments.append({"query_id": f"q{i}", "passage_id": f"p{i}", "label": 2, "assessor_id": assessor,
                              "protocol_version": "legal-relevance-v1", "label_origin": "human-assessor"})
            judgments.append({"query_id": f"q{i}", "passage_id": "noise", "label": 0, "assessor_id": assessor,
                              "protocol_version": "legal-relevance-v1", "label_origin": "human-assessor"})
    runs = {
        "exact-identifier": {"rankings": {f"q{i}": [f"p{i}"] for i in range(4)}},
        "keyword": {"rankings": {f"q{i}": ["noise", f"p{i}"] for i in range(4)}},
        "hybrid": {"rankings": {f"q{i}": [f"p{i}", "noise"] for i in range(4)}, "latency_ms": {"q0": 120}},
    }
    return queries, judgments, runs


def test_retrieval_modes_stay_opt_in_without_human_judgments():
    modes = retrieval_modes()["modes"]
    assert {m for m, v in modes.items() if v["state"] == "enabled"} == {"exact-identifier", "exact-locator-or-substring"}
    for mode in ("semantic", "hybrid", "lexical-bm25", "reranked"):
        with pytest.raises(RetrievalEvaluationError) as caught:
            require_enabled(mode)
        assert caught.value.code == "mode_not_enabled"


def test_judgment_provenance_agreement_and_adjudication_are_enforced():
    queries, judgments, _ = _eval_inputs()
    for origin in ("model-generated", "exact-term-membership", "publisher-identifier-lookup"):
        with pytest.raises(RetrievalEvaluationError) as caught:
            validate_judgments(queries, [{**judgments[0], "label_origin": origin}])
        assert caught.value.code == "invalid_label_origin"
    with pytest.raises(RetrievalEvaluationError) as caught:
        validate_judgments(queries, [j for j in judgments if j["assessor_id"] == "a1"])
    assert caught.value.code == "too_few_assessors"
    disagreeing = copy.deepcopy(judgments)
    disagreeing[0]["label"] = 0
    with pytest.raises(RetrievalEvaluationError) as caught:
        validate_judgments(queries, disagreeing)
    assert caught.value.code == "unadjudicated_disagreement"
    adjudicated = disagreeing + [{**judgments[0], "assessor_id": "lead", "adjudication": True}]
    assert validate_judgments(queries, adjudicated)["disagreements"][0]["adjudicated"] == 2


def test_evaluation_is_stratified_and_the_decision_needs_every_threshold():
    queries, judgments, runs = _eval_inputs()
    result = evaluate(queries, judgments, runs)
    assert not list(schema("noesis-legal-retrieval-evaluation-v1").iter_errors(result))
    hybrid = result["modes"]["hybrid"]
    assert hybrid["overall"]["recall_at_k"] == 1.0 and hybrid["overall"]["mrr"] == 1.0
    assert set(hybrid["strata"]) == {"jurisdiction=DE-BE", "jurisdiction=EU", "language=de", "version=v1"}
    decision = decide(result)["decisions"]["hybrid"]
    assert decision["decision"] == "defer"
    assert "a stratum has too few held-out queries" in decision["reasons"]
    with pytest.raises(RetrievalEvaluationError) as caught:
        evaluate(queries, judgments, {"hybrid": runs["hybrid"]})
    assert caught.value.code == "missing_baseline"


# ---------------------------------------------------------------- packaging


def test_pack_and_domain_module_install_independently(loaded):
    conn, value, _, _, store = loaded
    data = json.loads((ROOT / "packs/legal/pack.json").read_text())
    assert validate_manifest(data) == []
    try:
        receipt = install_manifest(PackManifest.from_dict(data))
        assert "as-of-version-selection" in receipt["capabilities"]
    finally:
        assert uninstall("legal")
    import src.domains.legal  # noqa: F401 - registers the built-in pack
    from src.domains import registry

    assert registry.get_pack("legal") is not None
    assert registry.get_pack("political") is not None  # untouched
    state = readiness(conn)
    assert {p: v["jurisdiction"] for p, v in state["providers"].items()} == {
        "cellar": "EU", "rii": "DE", "berlin-law": "DE-BE"}
    assert all(v["pack_live_acceptance"] == "outstanding" for v in state["providers"].values())
    SourcePackStore(conn).set_enabled(value["pack_id"], False, principal_id="operator")
    assert {v["fixture"] for v in readiness(conn)["providers"].values()} == {"blocked"}
    assert store.lookup("global", scopes=SCOPES, identifier="32016R0679")["status"] == "found"
    SourcePackStore(conn).set_enabled(value["pack_id"], True, principal_id="operator")
