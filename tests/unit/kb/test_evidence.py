"""Unit tests for prediction-mode propagation and the evidence-quality summary."""

import duckdb

from src.kb import contract
from src.kb.evidence import evidence_quality_summary
from src.kb.membership import run_membership_pass
from src.kb.registry import load_registry
from tests.unit.kb.test_claim_links import (
    BASE_MS,
    DUP_A,
    DUP_B,
    FakeNLI,
    FakeProvider,
    _seed_claims,
)
from tests.unit.kb.test_contract import CONFIG


class TestWrapperModes:
    def test_all_three_wrappers_expose_heuristic_mode(self):
        from src.argument_mining.frames import FrameClassifier
        from src.argument_mining.models import ClaimDetector, StanceClassifier

        # No checkpoints in this environment -> heuristic everywhere.
        assert ClaimDetector().prediction_mode == "heuristic"
        assert StanceClassifier().prediction_mode == "heuristic"
        assert FrameClassifier().prediction_mode == "heuristic"


class TestMigration:
    def test_prediction_columns_added_and_backfilled(self):
        conn = duckdb.connect()
        from src.database.local_warehouse_seed import ensure_schema

        # Simulate a legacy warehouse: argument_claims existed pre-#958.
        conn.execute(
            "CREATE TABLE argument_claims (claim_id VARCHAR PRIMARY KEY,"
            " claim_text VARCHAR NOT NULL, document_id VARCHAR NOT NULL,"
            " source_type VARCHAR NOT NULL, confidence DOUBLE)"
        )
        conn.execute(
            "INSERT INTO argument_claims VALUES ('legacy', 'Old claim.', 'd0',"
            " 'news', 0.7)"
        )
        ensure_schema(conn)
        mode = conn.execute(
            "SELECT prediction_mode FROM argument_claims WHERE claim_id = 'legacy'"
        ).fetchone()[0]
        assert mode == "heuristic"
        # Every prediction table now carries the column.
        for table in (
            "source_stances", "document_frames", "policy_positions",
            "claim_conflicts", "stance_drift_events",
        ):
            assert conn.execute(
                "SELECT 1 FROM information_schema.columns"
                " WHERE table_name = ? AND column_name = 'prediction_mode'",
                [table],
            ).fetchone() is not None


class TestSummary:
    def test_mode_distribution_and_fraction(self):
        conn = duckdb.connect()
        _seed_claims(
            conn,
            [
                ("c1", DUP_A, "d1", BASE_MS, "web3"),
                ("c2", DUP_B, "d2", BASE_MS, "web3"),
            ],
        )
        from src.kb.claim_links import run_claim_linking_pass

        run_claim_linking_pass(conn, provider=FakeProvider(), nli=FakeNLI())

        summary = evidence_quality_summary(conn)
        claims = summary["tables"]["argument_claims"]
        assert claims["modes"] == {"heuristic": 2}
        assert claims["model_grade_fraction"] == 0.0

        links = summary["tables"]["claim_links"]
        assert set(links["modes"]) == {"zero-shot:fake-model"}
        assert links["model_grade_fraction"] == 1.0
        assert 0.0 < summary["model_grade_fraction"] < 1.0

    def test_coverage_carries_the_honesty_rider(self, tmp_path):
        conn = duckdb.connect()
        config_path = tmp_path / "domains.yml"
        config_path.write_text(CONFIG)
        from src.ingestion.document_store import DocumentStore
        from src.kb.claim_links import ensure_claim_link_schema

        ensure_claim_link_schema(conn)
        DocumentStore(conn).upsert(
            [
                {
                    "document_id": "d1",
                    "source_type": "news",
                    "language": "en",
                    "ingested_at": BASE_MS,
                    "source_id": "wire",
                    "url": "https://example.com/d1",
                    "title": "Defi staking news",
                    "content": "defi staking coverage continues.",
                    "metadata": {"tags": ["web3"]},
                }
            ]
        )
        run_membership_pass(conn, load_registry(config_path))
        conn.execute(
            "INSERT INTO argument_claims (claim_id, claim_text, document_id,"
            " source_type, confidence, prediction_mode)"
            " VALUES ('c1', ?, 'd1', 'news', 0.8, 'heuristic')",
            [DUP_A],
        )
        payload = contract.kb_coverage("web3", conn=conn, config_path=config_path)
        quality = payload["data"]["evidence_quality"]
        assert quality["tables"]["argument_claims"]["modes"] == {"heuristic": 1}
        assert quality["model_grade_fraction"] == 0.0
