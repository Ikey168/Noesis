"""Evidence Independence Graph storage, inference, evaluation, and surfaces."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.database.local_warehouse_seed import ensure_schema
from src.kb import contract
from src.kb.clusters import ensure_cluster_schema
from src.kb.membership import run_membership_pass
from src.kb.registry import load_registry
from src.osint.corroboration import corroborate
from src.osint.independence import (
    METHOD_VERSION,
    SIGNAL_VERSION,
    backfill_status,
    compare_signals,
    document_signals,
    ensure_independence_schema,
    extract_document_signals,
    origin_graph,
    origin_summary,
    record_document_signals,
    run_origin_backfill,
    run_origin_inference,
)
from src.osint.independence_eval import evaluate_fixture_files
from src.osint.reliability import source_reliability

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES = REPO_ROOT / "tests/fixtures/evidence_independence"

CONFIG = """
version: 1
domains:
  - name: economics
    backing: corpus-view
    embedding_model: fake-embed
    tags: [economics]
    keywords: [inflation, factory, output]
"""


def _insert_document(
    conn,
    document_id: str,
    source: str | None,
    content: str,
    *,
    title: str | None = None,
    created_at: int | None = None,
    metadata: dict | None = None,
    authors: list[str] | None = None,
    url: str | None = None,
    source_type: str = "news",
) -> None:
    conn.execute(
        "INSERT INTO documents"
        " (document_id, source_type, language, ingested_at, created_at, source_id,"
        " url, title, content, authors, metadata)"
        " VALUES (?, ?, 'en', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            document_id,
            source_type,
            created_at or 1,
            created_at,
            source,
            url,
            title or document_id,
            content,
            json.dumps(authors or []),
            json.dumps(metadata or {}),
        ],
    )


def _five_publications(conn) -> list[str]:
    rows = [
        ("origin-a-1", "Outlet A1", "Inflation output rose in May after exports recovered.", 100, {}),
        ("origin-a-2", "Outlet A2", "Inflation output rose in May after exports recovered.", 110, {}),
        (
            "origin-b-1",
            "Outlet B1",
            "According to Reuters factory output rose in May as export demand recovered.",
            120,
            {},
        ),
        (
            "origin-b-2",
            "Outlet B2",
            "Reuters reported factory output rose during May while export demand recovered.",
            130,
            {"wire_service": "Reuters"},
        ),
        ("unresolved-1", None, "An unattributed inflation bulletin.", 140, {}),
    ]
    for document_id, source, content, created, metadata in rows:
        _insert_document(
            conn,
            document_id,
            source,
            content,
            created_at=created,
            metadata=metadata,
        )
        record_document_signals(conn, document_id, extracted_at_ms=1_000)
    run_origin_inference(conn, as_of_ms=2_000, run_id="five-publications")
    return [row[0] for row in rows]


def test_schema_migration_and_current_link_constraints(tmp_path):
    database = tmp_path / "origins.duckdb"
    conn = duckdb.connect(str(database))
    ensure_schema(conn)
    _insert_document(conn, "existing", "Source", "Existing populated warehouse row.")
    ensure_independence_schema(conn)
    ensure_independence_schema(conn)
    assert conn.execute(
        "SELECT version FROM noesis_schema_migrations"
        " WHERE component='evidence-independence'"
    ).fetchall() == [(1,)]
    record_document_signals(conn, "existing", extracted_at_ms=10)
    run_origin_inference(conn, as_of_ms=20, run_id="migration")
    with pytest.raises(duckdb.ConstraintException):
        conn.execute(
            "INSERT INTO document_origin_links SELECT * FROM document_origin_links"
        )
    conn.close()
    reopened = duckdb.connect(str(database))
    graph = origin_graph(reopened)
    assert graph["publication_count"] == 1
    schema = json.loads(
        (
            REPO_ROOT
            / "contracts/schemas/jsonschema/noesis-evidence-independence-v1.json"
        ).read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(graph))
    reopened.close()


def test_signal_extraction_is_traceable_idempotent_and_high_precision():
    base = {
        "document_id": "wire",
        "source_type": "feed",
        "source_id": "Example",
        "url": "https://example.test/a?utm_source=rss",
        "title": "Factory output",
        "content": (
            "According to Reuters factory output rose. "
            "“Exports recovered across the region this month,” an analyst said. "
            "https://data.example/report"
        ),
        "authors": ["A Reporter"],
        "created_at": 123,
        "metadata": {
            "dateline": "Berlin",
            "media_hashes": ["media-1"],
            "publisher_owner": "Group One",
            "claim_texts": ["Factory output rose"],
        },
    }
    first = extract_document_signals(base)
    assert first == extract_document_signals(base)
    assert first["explicit_upstreams"] == ["reuters"]
    assert first["canonical_url"] == "https://example.test/a"
    assert first["bylines"] == ["a reporter"]
    assert first["dateline"] == "berlin"
    assert first["media_hashes"] == ["media-1"]
    assert first["quote_markers"] and first["outbound_links"]
    assert first["claim_fingerprints"]

    unrelated = extract_document_signals(
        {
            **base,
            "document_id": "unrelated",
            "url": "https://elsewhere.test/b",
            "content": "A sports desk previewed the final match and interviewed both coaches.",
            "authors": ["Another Reporter"],
        }
    )
    decision = compare_signals(first, unrelated)
    assert decision["dependent"] is False
    assert "shared_ownership_nondecisive" in decision["reason_codes"]


def test_five_publications_resolve_to_two_origins_and_one_unknown():
    conn = duckdb.connect()
    ensure_schema(conn)
    ids = _five_publications(conn)
    summary = origin_summary(
        conn,
        ids,
        sources=["Outlet A1", "Outlet A2", "Outlet B1", "Outlet B2", None],
    )
    assert summary["publication_count"] == 5
    assert summary["probable_origin_count"] == 2
    assert summary["independent_source_count"] == 2
    assert summary["likely_dependent_count"] == 4
    assert summary["unresolved_count"] == 1
    assert all(
        row["method_version"] == METHOD_VERSION
        and row["reason_codes"]
        and row["as_of_ms"] == 2_000
        for row in summary["dependency_evidence"]
    )


def test_incremental_rerun_converges_and_split_preserves_history():
    conn = duckdb.connect()
    ensure_schema(conn)
    _insert_document(conn, "copy-a", "A", "Identical syndicated copy.")
    _insert_document(conn, "copy-b", "B", "Identical syndicated copy.")
    for document_id in ("copy-a", "copy-b"):
        record_document_signals(conn, document_id, extracted_at_ms=1)
    first = run_origin_inference(conn, as_of_ms=2, run_id="first")
    second = run_origin_inference(conn, as_of_ms=3, run_id="second")
    assert first["links"][0]["origin_id"] == second["links"][0]["origin_id"]
    assert conn.execute("SELECT COUNT(*) FROM document_origin_links").fetchone()[0] == 2

    conn.execute(
        "UPDATE documents SET content='A genuinely independent investigation.'"
        " WHERE document_id='copy-b'"
    )
    record_document_signals(conn, "copy-b", extracted_at_ms=4)
    split = run_origin_inference(conn, as_of_ms=5, run_id="split")
    assert split["probable_origins"] == 0
    assert split["unresolved"] == 2
    assert conn.execute(
        "SELECT COUNT(*) FROM document_origin_link_history"
    ).fetchone()[0] == 6
    assert conn.execute(
        "SELECT COUNT(*) FROM reporting_origins WHERE active"
    ).fetchone()[0] == 0


def test_incremental_copy_merges_into_the_existing_deterministic_origin():
    conn = duckdb.connect()
    ensure_schema(conn)
    for document_id in ("copy-a", "copy-b"):
        _insert_document(conn, document_id, document_id, "One syndicated report.")
        record_document_signals(conn, document_id, extracted_at_ms=1)
    first = run_origin_inference(conn, as_of_ms=2, run_id="two-copies")
    origin_id = first["links"][0]["origin_id"]
    _insert_document(conn, "copy-c", "copy-c", "One syndicated report.")
    record_document_signals(conn, "copy-c", extracted_at_ms=3)
    merged = run_origin_inference(conn, as_of_ms=4, run_id="three-copies")
    assert {row["origin_id"] for row in merged["links"]} == {origin_id}
    assert merged["probable_origins"] == 1
    assert conn.execute(
        "SELECT member_count FROM reporting_origins WHERE origin_id = ? AND active",
        [origin_id],
    ).fetchone() == (3,)


def test_backfill_is_resumable_restart_safe_and_observable(tmp_path):
    database = tmp_path / "backfill.duckdb"
    conn = duckdb.connect(str(database))
    ensure_schema(conn)
    for index in range(3):
        _insert_document(
            conn,
            f"doc-{index}",
            f"Source {index}",
            f"Unique document number {index} with its own reporting.",
        )
    first = run_origin_backfill(conn, batch_size=2, now_ms=10)
    assert first["status"] == "running"
    assert first["processed_this_batch"] == 2
    conn.close()

    reopened = duckdb.connect(str(database))
    second = run_origin_backfill(reopened, batch_size=2, now_ms=20)
    assert second["status"] == "complete"
    assert second["processed_documents"] == 3
    assert backfill_status(reopened)["last_run_id"] == second["last_run_id"]
    third = run_origin_backfill(reopened, batch_size=2, now_ms=30)
    assert third["processed_this_batch"] == 0
    assert third["processed_documents"] == 3
    reopened.close()

    command = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts/backfill_evidence_independence.py"),
            "--db-path",
            str(database),
            "--batch-size",
            "2",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert command.returncode == 0, command.stderr
    assert json.loads(command.stdout)["processed_this_batch"] == 0


def test_absent_lineage_uses_explicit_compatible_fallback():
    conn = duckdb.connect()
    ensure_schema(conn)
    summary = origin_summary(
        conn,
        ["one", "two", "three"],
        sources=["Wire", "Wire", "Independent"],
    )
    assert summary["lineage_available"] is False
    assert summary["method"] == "distinct-source-fallback-v1"
    assert summary["publication_count"] == 3
    assert summary["independent_source_count"] == 2


def test_offline_evaluation_is_partitioned_calibrated_and_complete():
    report = evaluate_fixture_files(
        FIXTURES / "development.json", FIXTURES / "final.json"
    )
    assert report["calibration"]["selected_threshold"] == 0.78
    assert report["partitions"]["final_test_used_for_selection"] is False
    assert report["development"]["pairwise"]["false_independence"] == 0
    assert report["final_test"]["pairwise"]["false_independence"] == 0
    assert report["final_test"]["pairwise"]["precision"]["n"] > 0
    assert report["final_test"]["pairwise"]["recall"]["hi"] == 1.0
    assert report["final_test"]["cluster_exact_match"]["value"] == 1.0
    fixture_text = (FIXTURES / "development.json").read_text() + (
        FIXTURES / "final.json"
    ).read_text()
    for signal in (
        "wire_service",
        "canonical",
        "byline",
        "dateline",
        "source_links",
        "publisher_owner",
        "media_hashes",
        "created_at",
        "press_release",
        "claim_texts",
    ):
        assert signal in fixture_text


def test_origin_aware_corroboration_and_reliability_do_not_create_truth_score():
    conn = duckdb.connect()
    ensure_schema(conn)
    _insert_document(
        conn,
        "claim-doc",
        "Claim Source",
        "Inflation output increased according to the monthly data.",
    )
    ids = _five_publications(conn)
    conn.execute(
        "INSERT INTO argument_claims"
        " (claim_id, claim_text, document_id, source_type, confidence)"
        " VALUES ('target', 'Inflation output increased.', 'claim-doc', 'news', 0.9)"
    )
    for index, document_id in enumerate(ids):
        conn.execute(
            "INSERT INTO claim_evidence"
            " (evidence_id, claim_id, evidence_text, evidence_document_id,"
            " evidence_source_type, relation, similarity_score)"
            " VALUES (?, 'target', 'support', ?, 'news', 'supports', 0.9)",
            [f"evidence-{index}", document_id],
        )
    result = corroborate(conn, "target")
    assert result["publication_support_count"] == 5
    assert result["probable_origin_support_count"] == 2
    assert result["independent_support_count"] == 2
    assert result["unresolved_support_count"] == 1
    assert result["independence"]["support"]["dependency_evidence"]

    reliability = source_reliability(conn, "Outlet A1")
    assert reliability["lineage"]["probable_origin_count"] == 1
    assert "lineage" not in reliability["components"]


def test_kb_rest_and_mcp_share_the_same_origin_aware_service(tmp_path, monkeypatch):
    conn = duckdb.connect()
    ensure_schema(conn)
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    _insert_document(
        conn,
        "claim-doc",
        "Claim Source",
        "Inflation factory output increased in the monthly report.",
    )
    conn.execute(
        "INSERT INTO argument_claims"
        " (claim_id, claim_text, document_id, source_type, confidence)"
        " VALUES ('target', 'Inflation factory output increased.', 'claim-doc', 'news', 0.9)"
    )
    run_membership_pass(conn, load_registry(config_path))
    direct = contract.kb_corroborate(
        "economics", "target", conn=conn, config_path=config_path
    )

    contract_call = contract.kb_corroborate
    monkeypatch.setattr(
        contract,
        "kb_corroborate",
        lambda domain, claim_id: contract_call(
            domain, claim_id, conn=conn, config_path=config_path
        ),
    )
    from src.api.routes import kb_routes

    rest = kb_routes.corroboration("economics", "target")
    server_path = REPO_ROOT / "tools/kb_mcp/server.py"
    spec = importlib.util.spec_from_file_location("independence_kb_mcp", server_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    mcp = tools["kb_corroborate"].fn("economics", "target")
    assert rest["data"] == mcp["data"] == direct["data"]
    assert "publication_support_count" in rest["data"]


def test_answer_and_cluster_surface_origin_counts(tmp_path):
    conn = duckdb.connect()
    ensure_schema(conn)
    ensure_cluster_schema(conn)
    config_path = tmp_path / "domains.yml"
    config_path.write_text(CONFIG)
    content = "Inflation factory output increased in May after export demand recovered."
    for index, source in enumerate(("Outlet One", "Outlet Two"), start=1):
        document_id = f"answer-doc-{index}"
        claim_id = f"answer-claim-{index}"
        _insert_document(conn, document_id, source, content, created_at=index)
        conn.execute(
            "INSERT INTO argument_claims"
            " (claim_id, claim_text, document_id, source_type, confidence, prediction_mode)"
            " VALUES (?, 'Inflation factory output increased in May.', ?, 'news', 0.9, 'fixture')",
            [claim_id, document_id],
        )
        conn.execute(
            "INSERT INTO claim_clusters VALUES (?, 'answer-cluster', 'fixture', 1)",
            [claim_id],
        )
        record_document_signals(conn, document_id, extracted_at_ms=10)
    run_origin_inference(conn, as_of_ms=20, run_id="answer-origin")
    run_membership_pass(conn, load_registry(config_path))

    claims = contract.kb_claims(
        "economics", conn=conn, config_path=config_path
    )["data"]
    assert claims[0]["corroboration"] == 1
    assert claims[0]["independence"]["publication_count"] == 2
    answer = contract.kb_answer(
        "economics",
        "Did inflation factory output increase in May?",
        conn=conn,
        config_path=config_path,
    )["data"]
    statement = answer["statements"][0]
    assert statement["corroboration"]["publication_count"] == 2
    assert statement["corroboration"]["probable_origin_count"] == 1
    assert statement["corroboration"]["dependency_evidence"]


def test_signal_repository_is_separate_from_decisions():
    conn = duckdb.connect()
    ensure_schema(conn)
    _insert_document(
        conn,
        "signal-doc",
        "Wire",
        "According to AP, inflation eased.",
        metadata={"original_reporting": False},
    )
    recorded = record_document_signals(conn, "signal-doc", extracted_at_ms=10)
    assert recorded["signal_version"] == SIGNAL_VERSION
    assert document_signals(conn, "signal-doc") == recorded
    assert conn.execute("SELECT COUNT(*) FROM document_origin_links").fetchone()[0] == 0
    run_origin_inference(conn, as_of_ms=20, run_id="signals-separate")
    assert document_signals(conn, "signal-doc") == recorded


@pytest.mark.parametrize("alias", ["evidence-independence", "origin-graph"])
def test_contract_registry_validates_origin_graph_example(alias):
    from tools.contract_mcp.server import validate

    result = validate.fn(alias, "valid-origin-graph")
    assert result["valid"] is True
    assert result["verdicts"]["jsonschema"]["contract"] == (
        "noesis-evidence-independence-v1"
    )
