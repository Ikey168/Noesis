import json
from pathlib import Path

from jsonschema import Draft7Validator

from scripts.market_provider_sample_evidence import COVERAGE_AREAS, evaluate


ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64


def _manifest(*, evidence_kind="fixture"):
    receipt = {
        "receipt_id": "receipt:all",
        "endpoint": "authorized-sample",
        "http_status": 200,
        "records": 50,
        "requested_at": "2026-09-24T10:00:00Z",
        "retrieved_at": "2026-09-24T10:00:01Z",
        "request_sha256": HASH,
        "response_sha256": "b" * 64,
    }
    instruments = []
    for index, (ticker, identity_kind) in enumerate(
        [
            ("MSFT", "active"),
            ("ORCL", "active"),
            ("CRM", "active"),
            ("ADBE", "active"),
            ("NOW", "active"),
            ("OLD1", "delisted"),
            ("USED", "recycled"),
        ]
    ):
        instruments.append(
            {
                "sample_id": f"sample:{ticker}",
                "ticker": ticker,
                "identity_kind": identity_kind,
                "provider_instrument_id": f"provider:{ticker}",
                "venue": "XNAS",
                "currency": "USD",
                "timezone": "America/New_York",
                "history": {
                    "first_date": "2016-01-04",
                    "last_date": "2026-09-23",
                    "raw_adjusted_distinguished": True,
                    "missing_sessions_checked": True,
                    "timezone_boundary_checked": True,
                    "correction_status": "observed" if index == 0 else "no_correction_in_sample",
                },
                "coverage": {
                    area: {
                        "status": "available" if area in {"prices", "corporate_actions"} else "unavailable",
                        "receipt_ids": ["receipt:all"] if area in {"prices", "corporate_actions"} else [],
                    }
                    for area in COVERAGE_AREAS
                },
            }
        )
    return {
        "contract": "noesis-market-provider-sample-manifest-v1",
        "evidence_kind": evidence_kind,
        "provider": "fixture-vendor",
        "recorded_at": "2026-09-24T10:00:02Z",
        "authorization": {"status": "authorized", "license_reference": "evaluation-order:fixture"},
        "rights": {
            "local_retention": "confirmed",
            "internal_display": "confirmed",
            "external_display": "denied",
            "derived_data": "confirmed",
            "report_export": "denied",
        },
        "receipts": [receipt],
        "instruments": instruments,
    }


def test_complete_fixture_exercises_harness_without_claiming_live_evidence():
    result = evaluate(_manifest())

    assert result["status"] == "fixture_verified_only"
    assert all(row["status"] == "passed" for row in result["checks"])
    assert result["coverage_comparison"]["prices"]["available"] == 7
    assert result["coverage_comparison"]["transcripts"]["unavailable"] == 7
    Draft7Validator(
        json.loads(
            (ROOT / "contracts/schemas/jsonschema/noesis-market-provider-sample-evaluation-v1.json").read_text()
        )
    ).validate(result)


def test_live_manifest_requires_authorization_rights_history_and_correction_evidence():
    manifest = _manifest(evidence_kind="live_provider")
    manifest["authorization"] = {"status": "credential_blocked", "license_reference": None}
    manifest["rights"]["report_export"] = "pending"
    manifest["instruments"][0]["history"]["correction_status"] = "no_correction_in_sample"

    result = evaluate(manifest)

    assert result["status"] == "credential_blocked"
    statuses = {row["name"]: row["status"] for row in result["checks"]}
    assert statuses["authorized_sample_access"] == "blocked"
    assert statuses["rights_answers_explicit"] == "blocked"
    assert statuses["price_history_calendar_and_corrections"] == "blocked"


def test_available_dataset_requires_a_valid_payload_hash_receipt():
    manifest = _manifest()
    manifest["instruments"][0]["coverage"]["prices"]["receipt_ids"] = ["missing"]

    result = evaluate(manifest)

    assert result["status"] == "fixture_incomplete"
    receipt_check = next(row for row in result["checks"] if row["name"] == "hashed_request_response_receipts")
    assert receipt_check["status"] == "failed"
    assert receipt_check["details"]["missing_references"] == ["sample:MSFT:prices:missing"]
