#!/usr/bin/env python3
"""Offline end-to-end evidence showcase with machine-checkable receipts.

The flow is intentionally small and deterministic: KB search -> clustered
claims -> corroboration -> claim-vs-data -> daily brief. It also demonstrates
the three failure states agents must preserve: uncited, unverifiable, and a
person-dossier refusal.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _seed(conn, config_path: Path) -> None:
    from src.database.local_warehouse_seed import ensure_schema
    from src.ingestion.document_store import DocumentStore
    from src.kb.membership import run_membership_pass
    from src.kb.registry import load_registry

    ensure_schema(conn)
    now = int(time.time() * 1000)
    documents = [
        {
            "document_id": "showcase-wire-a", "source_type": "news",
            "source_id": "Wire A", "language": "en", "ingested_at": now - 2,
            "url": "https://example.invalid/wire-a", "title": "Inflation report",
            "content": "Annual inflation fell to 3.0 percent in 2025.",
            "metadata": {"tags": ["news", "economics"]},
        },
        {
            "document_id": "showcase-wire-b", "source_type": "news",
            "source_id": "Wire B", "language": "en", "ingested_at": now - 1,
            "url": "https://example.invalid/wire-b", "title": "Second inflation report",
            "content": "A separate release also reported annual inflation at 3.0 percent.",
            "metadata": {"tags": ["news", "economics"]},
        },
        {
            "document_id": "showcase-note", "source_type": "note",
            "source_id": "Local note", "language": "en", "ingested_at": now,
            "url": None, "title": "Unmatched Atlantis statistic",
            "content": "Atlantis unemployment was 99 percent in 2025.",
            "metadata": {"tags": ["news", "local", "private"]},
        },
    ]
    DocumentStore(conn).upsert(documents)
    run_membership_pass(conn, load_registry(config_path))

    conn.executemany(
        """INSERT INTO argument_claims
           (claim_id, claim_text, document_id, source_type, confidence,
            prediction_mode, extracted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            ("showcase-claim", documents[0]["content"], documents[0]["document_id"],
             "news", 0.91, "pretrained:Nithiwat/mdeberta-v3-base_claimbuster", "2025-01-01T00:00:00Z"),
            ("showcase-support", documents[1]["content"], documents[1]["document_id"],
             "news", 0.88, "pretrained:Nithiwat/mdeberta-v3-base_claimbuster", "2025-01-01T00:00:01Z"),
            ("showcase-unverifiable", documents[2]["content"], documents[2]["document_id"],
             "note", 0.72, "pretrained:Nithiwat/mdeberta-v3-base_claimbuster", "2025-01-01T00:00:02Z"),
        ],
    )
    conn.execute(
        """INSERT INTO claim_evidence
           (evidence_id, claim_id, evidence_text, evidence_document_id,
            evidence_source_type, relation, similarity_score, found_at)
           VALUES ('showcase-evidence', 'showcase-claim', ?,
                   'showcase-wire-b', 'news', 'supports', 0.94, ?)""",
        [documents[1]["content"], "2025-01-01T00:00:03Z"],
    )


def build_receipts(conn, config_path: Path) -> dict[str, Any]:
    from src.analytics.claim_check import check_assertion, record_check
    from src.analytics.honesty import validate_analytic_output
    from src.argument_mining.quantities import QuantityExtractor
    from src.kb.contract import kb_brief, kb_claims, kb_search
    from src.osint.corroboration import corroborate
    from src.osint.dossier import entity_dossier
    from src.osint.evidence import citation

    _seed(conn, config_path)
    search = kb_search("news", "inflation", conn=conn, config_path=config_path)
    claims = kb_claims("news", conn=conn, config_path=config_path)
    corroboration = corroborate(conn, "showcase-claim")

    assertion = QuantityExtractor().extract_sentences(
        ["Atlantis unemployment was 99 percent in 2025."]
    )[0]
    data_check = check_assertion(conn, assertion)
    record_check(conn, data_check, claim_id="showcase-unverifiable")

    brief = kb_brief(
        domains=["news"], since="1970-01-01T00:00:00Z", budget=5,
        conn=conn, config_path=config_path,
    )
    uncited = citation(None, None, None)
    refusal = entity_dossier(conn, "Unobserved Person", entity_type="Person")

    checks = {
        "search_found_documents": bool(search["data"]),
        "claims_found": bool(claims["data"]),
        "corroboration_honesty_valid": not validate_analytic_output(corroboration),
        "corroboration_has_independent_source": (
            corroboration.get("independent_support_count", 0) >= 1
        ),
        "data_check_honesty_valid": not validate_analytic_output(data_check),
        "unverifiable_is_explicit": data_check.get("verdict") == "unverifiable",
        "uncited_is_flagged": uncited.get("cited") is False,
        "person_refusal_is_explicit": refusal.get("code") == "person_requires_documents",
        "brief_is_cited": "uncited — flagged" not in brief["data"]["markdown"],
    }
    return {
        "flow": {
            "kb_search": search,
            "kb_claims": claims,
            "corroborate": corroboration,
            "claim_vs_data": data_check,
            "kb_brief": brief,
        },
        "intentional_failure_states": {
            "uncited": uncited,
            "unverifiable": data_check,
            "person_dossier_refusal": refusal,
        },
        "verification": {**checks, "all_passed": all(checks.values())},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="also write the JSON receipt here")
    parser.add_argument(
        "--bundle-output",
        type=Path,
        help="also write a verified noesis-evidence-bundle-v1 package",
    )
    args = parser.parse_args()

    import duckdb

    with tempfile.TemporaryDirectory(prefix="noesis-showcase-") as directory:
        conn = duckdb.connect(str(Path(directory) / "showcase.duckdb"))
        try:
            receipt = build_receipts(conn, REPO_ROOT / "config" / "domains.yml")
        finally:
            conn.close()
    bundle_valid = True
    if args.bundle_output:
        from src.evidence_bundle import export_receipt, verify_bundle

        created_at_ms = receipt["flow"]["kb_brief"]["data"]["meta"]["generated_at_ms"]
        bundle = export_receipt(
            receipt,
            inputs={"showcase": "offline-evidence-flow"},
            created_at_ms=created_at_ms,
        )
        verification = verify_bundle(bundle, bundle_path=args.bundle_output)
        bundle_valid = verification.valid
        args.bundle_output.parent.mkdir(parents=True, exist_ok=True)
        args.bundle_output.write_text(
            json.dumps(bundle, indent=2, default=str) + "\n", encoding="utf-8"
        )
        receipt["evidence_bundle"] = {
            "path": str(args.bundle_output),
            "bundle_id": bundle["bundle_id"],
            "verification": verification.to_dict(),
        }
    rendered = json.dumps(receipt, indent=2, default=str)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if receipt["verification"]["all_passed"] and bundle_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
