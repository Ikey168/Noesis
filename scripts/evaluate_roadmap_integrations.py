"""Deterministic offline status report for roadmap provider adapters."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingestion.roadmap_integrations import (
    REGIONAL_PROVIDER_SPECS,
    normalize_regional_record,
)


def build_report(fixture: Path) -> dict:
    payload = json.loads(fixture.read_text())
    normalized = []
    for provider, record in sorted(payload["records"].items()):
        normalized.append(normalize_regional_record(provider, record))
    return {
        "fixture_provenance": payload["provenance"],
        "offline_contracts": {
            "status": "completed",
            "providers": [item["provider"] for item in normalized],
            "records": len(normalized),
        },
        "access_contracts": {
            name: {
                "access": spec.access,
                "coverage": spec.coverage,
                "version": spec.version,
            }
            for name, spec in sorted(REGIONAL_PROVIDER_SPECS.items())
        },
        "live_status": {
            "openalex_paid_content": "unavailable:not-run-account-gated",
            "opencorporates": "unavailable:not-run-account-gated",
            "opensanctions": "unavailable:not-run-account-gated",
            "exa": "unavailable:not-run-account-gated",
            "tavily": "unavailable:not-run-account-gated",
            "jina_reader": "unavailable:not-run-remote-processing",
            "markitdown": "unavailable-or-local-optional-dependency; no representative corpus run",
            "paddleocr": "unavailable:no-model-inference-run",
        },
        "decisions": {
            "regional_import_adapters": "adopt as bounded import/normalization contracts; provider live coverage remains separately gated",
            "hosted_discovery": "defer pending credentialed shared-query relevance/cost evaluation",
            "jina_reader": "defer pending public-page fidelity/cost evaluation",
            "markitdown": "defer pending format-specific representative corpus",
            "paddleocr": "defer pending real German/multilingual scan benchmark",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/roadmap_integrations/eu-provider-records.json"),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(build_report(args.fixture), indent=2, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    main()
