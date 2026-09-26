"""Mathematics in Research: zbMATH Open, OEIS and pinned formal-library snapshots (offline fixtures)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion.math_sources import (
    PROVIDER_CONTRACTS,
    FormalLibraryAdapter,
    OeisAdapter,
    ZbmathAdapter,
    fixture_transport,
    scan_file,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    validate_source_pack,
)
from src.kb.mathematics import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    MathematicsError,
    MathStore,
    normalize_notation,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/research.json"
SCOPES = {READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE}
MATH_SOURCES = {"zbmath-open": "documents", "oeis-sequences": "sequences", "mathlib4-fib": "snapshot",
                "afp-zeckendorf": "snapshot"}
NEW = "b2bf051988bf69448bce88722cc09a05fea31662"
OLD = "aa936c36e8484abd300577139faf8e945850831a"
AFP = "5818ae444ceea2858b02b70bb36c3cd6a6a47bea"


def schema(name):
    return jsonschema.Draft7Validator(json.loads((ROOT / f"contracts/schemas/jsonschema/{name}.json").read_text()))


def pack():
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id):
    return copy.deepcopy(next(s for s in pack()["sources"] if s["source_id"] == source_id))


def fixture_pages(source_id):
    return json.loads((ROOT / source(source_id)["fixture"]["path"]).read_text())["native_pages"]


def run_all(conn, value, key):
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    receipts = {}
    for source_id, operation in MATH_SOURCES.items():
        receipts[source_id] = runtime.run(
            {"pack_id": value["pack_id"], "run_key": f"{key}:{source_id}", "operation": operation,
             "source_ids": [source_id], "max_pages": 50, "max_results": 50, "max_bytes": 5_000_000,
             "timeout_ms": 30_000, "mode": "backfill", "backfill": {"from_ms": 0}},
            principal_id="operator", adapters=runtime.fixture_adapters(value["pack_id"], ROOT),
            dns_resolver=lambda _h: ["8.8.8.8"])
    return receipts


@pytest.fixture()
def loaded():
    conn = duckdb.connect(":memory:")
    value = pack()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=10)
    runtime = SourcePackRuntime(conn)
    for item in value["sources"]:
        runtime.accept_license(value["pack_id"], item["source_id"], principal_id="operator")
    receipts = run_all(conn, value, "fx")
    yield conn, value, receipts, MathStore(conn)
    conn.close()


def test_contracts_fixtures_and_offline_conformance():
    assert {k: v["status"] for k, v in PROVIDER_CONTRACTS.items()} == {
        "zbmath-open": "unverified-live", "oeis": "unverified-live", "formal-library": "validated-live-bounded",
        "arxiv-crossref-openalex": "reused", "mathscinet": "not-implemented"}
    result = SourcePackConformance(ROOT).offline(pack())
    assert result["valid"]
    assert {s["source_id"]: s["records"] for s in result["sources"] if s["source_id"] in MATH_SOURCES} == {
        "zbmath-open": 3, "oeis-sequences": 2, "mathlib4-fib": 2, "afp-zeckendorf": 1}
    for source_id in ("mathlib4-fib", "afp-zeckendorf"):
        data = json.loads((ROOT / source(source_id)["fixture"]["path"]).read_text())
        assert data["captured"] is True
        assert all(hashlib.sha256(p["body"].encode()).hexdigest() == p["sha256"] for p in data["native_pages"])
    for source_id in ("zbmath-open", "oeis-sequences"):
        assert json.loads((ROOT / source(source_id)["fixture"]["path"]).read_text())["authored"] is True


def test_adapters_are_bounded_pinned_and_validated():
    for source_id, block, change, code in (
            ("zbmath-open", "zbmath", {"document_ids": []}, "unbounded_source"),
            ("zbmath-open", "zbmath", {"document_ids": ["abc"]}, "invalid_mapping"),
            ("oeis-sequences", "oeis", {"a_numbers": ["45"]}, "invalid_mapping"),
            ("mathlib4-fib", "formal", {"commit": "master"}, "invalid_mapping"),
            ("mathlib4-fib", "formal", {"paths": ["../secrets"]}, "invalid_mapping"),
            ("mathlib4-fib", "formal", {"system": "agda"}, "invalid_mapping")):
        bad = source(source_id)
        bad[block].update(change)
        adapter = {"zbmath": ZbmathAdapter, "oeis": OeisAdapter, "formal": FormalLibraryAdapter}[block]
        with pytest.raises(SourcePackError) as caught:
            adapter(bad)
        assert caught.value.code == code
    item = source("zbmath-open")
    adapter = ZbmathAdapter(item, transport=fixture_transport(fixture_pages("zbmath-open")))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "documents", "parameters": {"q": "x"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    first = adapter.fetch_page({"operation": "documents"}, cursor=None)
    other = ZbmathAdapter({**item, "zbmath": {"document_ids": ["9000003"]}},
                          transport=fixture_transport(fixture_pages("zbmath-open")))
    with pytest.raises(SourcePackError) as caught:
        other.fetch_page({"operation": "documents"}, cursor=first.next_cursor)
    assert caught.value.code == "cursor_drift"
    wrong = [dict(p, request="/v1/document/9000001", body={**p["body"], "result": {**p["body"]["result"], "id": 1}})
             for p in fixture_pages("zbmath-open")[:1]]
    with pytest.raises(SourcePackError) as caught:
        ZbmathAdapter(item, transport=fixture_transport(wrong)).fetch_page({"operation": "documents"}, cursor=None)
    assert caught.value.code == "schema_drift"
    missing = ZbmathAdapter(item, transport=fixture_transport([])).fetch_page({"operation": "documents"}, cursor=None)
    assert missing.records == () and missing.receipt["outcome"] == "not_found"
    oeis = source("oeis-sequences")
    broken = [dict(p, body=[{**p["body"][0], "data": "1,2,x"}]) for p in fixture_pages("oeis-sequences")]
    with pytest.raises(SourcePackError) as caught:
        OeisAdapter(oeis, transport=fixture_transport(broken)).fetch_page({"operation": "sequences"}, cursor=None)
    assert caught.value.code == "schema_drift"
    small = source("mathlib4-fib")
    small["budgets"]["max_bytes"] = 1000
    with pytest.raises(SourcePackError) as caught:
        FormalLibraryAdapter(small, transport=fixture_transport(fixture_pages("mathlib4-fib"))).fetch_page(
            {"operation": "snapshot"}, cursor=None)
    assert caught.value.code == "response_too_large"
    validator = schema("noesis-math-record-v1")
    for source_id, operation in MATH_SOURCES.items():
        adapter_type = {"zbmath": ZbmathAdapter, "oeis": OeisAdapter, "formal-library": FormalLibraryAdapter}[
            source(source_id)["connector"]]
        adapter = adapter_type(source(source_id), transport=fixture_transport(fixture_pages(source_id)))
        page = adapter.fetch_page({"operation": operation}, cursor=None)
        assert all(not list(validator.iter_errors(r["math_record"])) for r in page.records)


def test_lexical_scanner_for_lean_isabelle_and_coq():
    pages = {p["request"]: p["body"] for p in fixture_pages("mathlib4-fib")}
    zeck = scan_file("lean", "Mathlib/Data/Nat/Fib/Zeckendorf.lean",
                     pages[f"/leanprover-community/mathlib4/{NEW}/Mathlib/Data/Nat/Fib/Zeckendorf.lean"])
    assert zeck["module"] == "Mathlib.Data.Nat.Fib.Zeckendorf" and zeck["imports"] == ["Mathlib.Data.Nat.Fib.Basic"]
    names = {d["name"]: d for d in zeck["declarations"]}
    assert names["List.IsZeckendorfRep"]["kind"] == "def"
    assert names["Nat.zeckendorfEquiv"]["statement"] == "def zeckendorfEquiv : ℕ ≃ {l // IsZeckendorfRep l}"
    assert names["List.IsZeckendorfRep"]["doc"].startswith("A list of natural numbers is a Zeckendorf")
    afp = scan_file("isabelle", "thys/Zeckendorf/Zeckendorf.thy", fixture_pages("afp-zeckendorf")[0]["body"])
    assert afp["imports"] == ["Main", "HOL-Number_Theory.Number_Theory"]
    unique = next(d for d in afp["declarations"] if d["name"] == "Zeckendorf.zeckendorf_unique")
    assert unique["kind"] == "theorem" and "\\<Sum>" in unique["statement"]
    coq_lines = ["Require Import Arith.", "From Coq Require Import Lia.", "Fixpoint fib (n : nat) : nat :=",
                 "  match n with 0 => 0 | 1 => 1 | S (S m as p) => fib p + fib m end.",
                 "Lemma fib_add_two : forall n, fib (S (S n)) = fib (S n) + fib n.", "Proof. reflexivity. Qed."]
    coq = scan_file("coq", "theories/Fixture/Fib.v", "\n".join(coq_lines))
    assert coq["imports"] == ["Arith", "Lia"]
    assert [(d["name"], d["kind"]) for d in coq["declarations"]] == [("Fib.fib", "fixpoint"),
                                                                     ("Fib.fib_add_two", "lemma")]
    assert coq["declarations"][1]["statement"] == "Lemma fib_add_two : forall n, fib (S (S n)) = fib (S n) + fib n"


def test_ingestion_is_idempotent_and_keeps_source_identity(loaded):
    conn, value, receipts, store = loaded
    assert {k: r["status"] for k, r in receipts.items()} == dict.fromkeys(MATH_SOURCES, "complete")
    tables = ("math_literature", "math_sequences", "formal_files", "formal_declarations", "formal_dependencies",
              "math_identifiers")
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    run_all(conn, value, "again")
    assert {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables} == before
    paper = store.literature("zbmath-open", "9000001", scopes=SCOPES)
    assert paper["record"]["identifiers"] == {"doi": ["10.5555/zeckendorf-1972"], "zbl": ["0000.00001"],
                                              "zbmath": ["9000001"]}
    assert len(paper["revisions"]) == 1
    sequence = store.sequence("A000045", scopes=SCOPES)
    assert sequence["record"]["terms"][:8] == ["0", "1", "1", "2", "3", "5", "8", "13"]
    assert sequence["record"]["cited_identifiers"] == {"doi": ["10.5555/zeckendorf-1972"], "zbmath": []}


def test_declarations_pin_revisions_with_dependencies(loaded):
    _, _, _, store = loaded
    with pytest.raises(MathematicsError) as caught:
        store.declaration("mathlib4", "Nat.fib", "master", scopes=SCOPES)
    assert caught.value.code == "revision_required"
    fib = store.declaration("mathlib4", "Nat.fib", NEW, scopes=SCOPES)
    assert not list(schema("noesis-formal-declaration-v1").iter_errors(fib))
    assert fib["permalink"] == ("https://github.com/leanprover-community/mathlib4/blob/"
                                f"{NEW}/Mathlib/Data/Nat/Fib/Basic.lean#L59-L59")
    assert {"name": "Nat.fib_add_two", "kind": "lexical-reference", "method": "lexical-token-match"} in \
        fib["dependencies"]["used_by"]
    module = store.dependencies("mathlib4", NEW, "Mathlib.Data.Nat.Fib.Zeckendorf", scopes=SCOPES)
    assert module["uses"] == [{"name": "Mathlib.Data.Nat.Fib.Basic", "kind": "module-import",
                               "method": "explicit-import"}]
    equiv = store.dependencies("mathlib4", NEW, "Nat.zeckendorfEquiv", scopes=SCOPES)
    assert {"name": "List.IsZeckendorfRep", "kind": "lexical-reference", "method": "lexical-token-match"} in \
        equiv["uses"]
    afp = store.declaration("afp", "Zeckendorf.zeckendorf_unique", AFP, scopes=SCOPES)
    assert any(d["name"] == "Zeckendorf.inc_seq_on" for d in afp["dependencies"]["uses"])
    exported = store.export_references("mathlib4", NEW, ["Nat.zeckendorfEquiv", "Nat.fib"], scopes=SCOPES)
    assert not list(schema("noesis-formal-references-v1").iter_errors(exported))
    assert exported == store.export_references("mathlib4", NEW, ["Nat.fib", "Nat.zeckendorfEquiv"], scopes=SCOPES)


def test_snapshot_comparison_detects_renames_and_import_changes(loaded):
    conn, _, _, store = loaded
    older = source("mathlib4-fib")
    older["formal"]["commit"] = OLD
    adapter = FormalLibraryAdapter(older, transport=fixture_transport(fixture_pages("mathlib4-fib")))
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "snapshot"}, cursor=cursor)
        records += page.records
        cursor = page.next_cursor
        if cursor is None:
            break
    MathStore(conn, now=lambda: 1).observe_page(records, run_id="old")
    diff = store.compare_snapshots("mathlib4", OLD, NEW, scopes=SCOPES)
    assert not list(schema("noesis-formal-snapshot-diff-v1").iter_errors(diff))
    assert "Nat.fast_fib_aux_bit_ff" in diff["removed"] and "Nat.fastFibAux_bit_false" in diff["added"]
    assert {"from": "Nat.fast_fib_aux_eq", "to": "Nat.fastFibAux_eq"} in [
        {k: r[k] for k in ("from", "to")} for r in diff["rename_candidates"]]
    assert diff["unchanged"] > 20
    assert store.declaration("mathlib4", "Nat.fast_fib_aux_eq", OLD, scopes=SCOPES)["commit"] == OLD
    with pytest.raises(MathematicsError) as caught:
        store.declaration("mathlib4", "Nat.fast_fib_aux_eq", NEW, scopes=SCOPES)
    assert caught.value.code == "not_found"


def test_objects_keep_exact_spans_and_normalize_only_for_search(loaded):
    _, _, _, store = loaded
    extracted = store.extract_text_objects("zbmath-open", "9000002", scopes=SCOPES, principal_id="p")
    [object_id] = extracted["objects"]
    obj = store.object(object_id, scopes=SCOPES)
    title = store.literature("zbmath-open", "9000002", scopes=SCOPES)["record"]["title"]
    ref = obj["source_ref"]
    assert title[ref["start"]:ref["end"]] == obj["exact_text"] == "n \\in \\mathbb{N}"
    assert obj["normalized_text"] == "n ∈ ℕ" and obj["status"] == "candidate"
    alias = store.record_object("notation-alias", "manual", {"note": "unicode"}, "n ∈ ℕ", method="manual",
                                confidence=None, scopes=SCOPES, principal_id="p", alias_of=object_id,
                                notation="unicode")
    assert store.object(object_id, scopes=SCOPES)["aliases"] == [alias["object_id"]]
    assert normalize_notation("\\<forall>n. fib n \\<le> fib (Suc n)") == "∀n. fib n ≤ fib (Suc n)"
    for bad in ({"kind": "lemma"}, {"kind": "notation-alias"}, {"confidence": 2.0}):
        args = {"kind": "expression", "confidence": 0.5, **bad}
        with pytest.raises(MathematicsError):
            store.record_object(args["kind"], "text", {}, "x", method="m", confidence=args["confidence"],
                                scopes=SCOPES, principal_id="p")


def test_explicit_links_are_separate_from_reviewable_candidates(loaded):
    _, _, _, store = loaded
    proposed = store.propose_links(scopes=SCOPES, principal_id="p")
    assert len(proposed["explicit"]) == 4 and proposed["candidates"]
    assert store.propose_links(scopes=SCOPES, principal_id="p") == {**proposed, "explicit": [], "candidates": []}
    validator = schema("noesis-math-link-v1")
    paper = store.links("literature", "zbmath-open:9000001", scopes=SCOPES)["links"]
    assert all(not list(validator.iter_errors(link)) for link in paper)
    explicit = {(link["subject"]["id"], link["basis"]) for link in paper if link["state"] == "explicit"}
    assert explicit == {("zbmath-open:9000002", "explicit-citation"), ("zbmath-open:9000003", "explicit-citation"),
                        ("A000045", "explicit-citation")}
    fib_def = f"mathlib4@{NEW}:Nat.fib"
    sequence_links = store.links("sequence", "A000045", scopes=SCOPES)["links"]
    candidate = next(link for link in sequence_links if link["object"]["id"] == fib_def)
    assert candidate["basis"] == "candidate-name" and candidate["state"] == "candidate"
    assert candidate["evidence"]["shared_words"] == ["fibonacci"]
    reviewed = store.review_link(candidate["link_id"], "accepted", scopes=SCOPES, principal_id="reviewer")
    assert reviewed["state"] == "accepted" and reviewed["asserts_identity"] is False
    with pytest.raises(MathematicsError) as caught:
        store.review_link(next(link["link_id"] for link in paper if link["state"] == "explicit"), "rejected",
                          scopes=SCOPES, principal_id="reviewer")
    assert caught.value.code == "not_reviewable"
    formal = store.links("literature", "zbmath-open:9000002", scopes=SCOPES)["links"]
    assert {link["object"]["id"].split("@")[0] for link in formal if link["basis"] == "candidate-name"} == {
        "mathlib4", "afp"}
    with pytest.raises(MathematicsError) as caught:
        store.review_link(candidate["link_id"], "accepted", scopes={READ_SCOPE}, principal_id="x")
    assert caught.value.code == "unauthorized"


def test_research_query_returns_sources_revisions_access_and_link_evidence(loaded):
    _, _, _, store = loaded
    store.propose_links(scopes=SCOPES, principal_id="p")
    validator = schema("noesis-math-search-v1")
    answer = store.search("Zeckendorf representation", scopes=SCOPES)
    assert not list(validator.iter_errors(answer))
    assert {r["kind"] for r in answer["results"]} == {"literature", "sequence", "declaration"}
    assert "A003714" in {r["id"] for r in answer["results"]}  # its name mentions the Zeckendorf representation
    declaration = next(r for r in answer["results"] if r["kind"] == "declaration")
    assert declaration["revision"] in {NEW, AFP} and declaration["access"]
    assert any(link["basis"] == "candidate-name" for link in declaration["links"])
    by_terms = store.search("0, 1, 1, 2, 3, 5, 8", scopes=SCOPES)
    assert [r["id"] for r in by_terms["results"]] == ["A000045"]
    assert store.search("A003714", scopes=SCOPES)["results"][0]["links"][0]["basis"] == "explicit-citation"
    with pytest.raises(MathematicsError):
        store.search("fib", scopes={"knowledge:other:read"})


def test_offline_demo_answer_is_reproducible():
    import importlib.util

    spec = importlib.util.spec_from_file_location("mathematics_demo", ROOT / "scripts/mathematics_demo.py")
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    first, second = demo.answer(), demo.answer()
    assert first["answer_sha256"] == second["answer_sha256"]
    assert set(first["source_runs"].values()) == {"complete"}
    kinds = {item["kind"] for item in first["results"]}
    assert kinds == {"literature", "sequence", "declaration"}
    assert {"zbmath-open:9000001", "zbmath-open:9000002", "A003714"} <= {item["id"] for item in first["results"]}
    bases = {link["basis"] for item in first["results"] for link in item["links"]}
    assert {"explicit-citation", "candidate-name"} <= bases
    assert all(item["revision"] in {NEW, AFP} for item in first["results"] if item["kind"] == "declaration")
