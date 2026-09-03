from __future__ import annotations

import pytest

duckdb = pytest.importorskip("duckdb")

from scripts.evidence_showcase import _seed, build_receipts
from src.evidence_bundle import (
    export_claim,
    export_integrity,
    export_receipt,
    verify_bundle,
)


@pytest.fixture
def showcase(tmp_path):
    conn = duckdb.connect()
    # tmp_path is not guaranteed to be below the repository under every runner.
    from pathlib import Path

    config = Path(__file__).resolve().parents[3] / "config" / "domains.yml"
    _seed(conn, config)
    yield conn, config
    conn.close()


def test_claim_export_contains_corroboration_and_citation_closure(showcase):
    conn, _ = showcase
    bundle = export_claim(conn, "showcase-claim", created_at_ms=0)
    root = next(row for row in bundle["objects"] if row["type"] == "claim")
    assert root["payload"]["corroboration"]["independent_support_count"] == 1
    assert root["payload"]["citation_state"] == "cited"
    assert len(root["payload"]["evidence_refs"]) == 2
    assert verify_bundle(bundle).status == "valid"


def test_integrity_export_is_portable_and_honesty_valid(showcase):
    conn, _ = showcase
    from src.ingestion.corrections import record_revision

    record_revision(conn, "showcase-wire-a", "Annual inflation was 4 percent.", 1000)
    record_revision(
        conn, "showcase-wire-a", "Annual inflation fell to 3 percent.", 2000
    )
    bundle = export_integrity(conn, "showcase-wire-a", created_at_ms=0)
    root = next(row for row in bundle["objects"] if row["type"] == "integrity")
    assert root["payload"]["document"]["document_id"] == "showcase-wire-a"
    revision = next(
        finding
        for finding in root["payload"]["findings"]
        if finding["kind"] == "document_revision"
    )
    assert len(revision["evidence"]) == 2
    assert verify_bundle(bundle).status == "valid"


def test_showcase_receipt_exports_as_a_valid_bundle(tmp_path):
    conn = duckdb.connect()
    from pathlib import Path

    config = Path(__file__).resolve().parents[3] / "config" / "domains.yml"
    receipt = build_receipts(conn, config)
    bundle = export_receipt(receipt, created_at_ms=0)
    result = verify_bundle(bundle, bundle_path=tmp_path / "bundle.json")
    assert result.status == "valid"
    assert result.stats["evidence_objects"] >= 1
