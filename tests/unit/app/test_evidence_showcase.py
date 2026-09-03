from pathlib import Path

import duckdb

from scripts.evidence_showcase import build_receipts
from src.evidence_bundle import export_receipt, verify_bundle


def test_evidence_showcase_receipts_are_machine_validated():
    receipt = build_receipts(
        duckdb.connect(), Path(__file__).resolve().parents[3] / "config" / "domains.yml"
    )
    assert receipt["verification"]["all_passed"] is True
    assert receipt["flow"]["corroborate"]["independent_support_count"] == 1
    assert receipt["intentional_failure_states"]["uncited"]["cited"] is False
    assert receipt["intentional_failure_states"]["unverifiable"]["verdict"] == "unverifiable"


def test_evidence_showcase_emits_a_verifiable_portable_bundle():
    receipt = build_receipts(
        duckdb.connect(), Path(__file__).resolve().parents[3] / "config" / "domains.yml"
    )
    bundle = export_receipt(receipt, created_at_ms=0)
    result = verify_bundle(bundle)
    assert result.status == "valid"
    assert result.stats["evidence_objects"] >= 1
