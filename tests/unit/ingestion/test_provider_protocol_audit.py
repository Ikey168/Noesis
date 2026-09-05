"""Native envelope audit fixtures demonstrate why generic readiness is unsupported."""

import json
from pathlib import Path
import pytest
from src.ingestion.source_packs import load_source_packs
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.provider_readiness import protocol_status


@pytest.mark.parametrize(
    "source_id,payload",
    [
        (
            "world-bank-indicators",
            [{"page": 1, "pages": 2}, [{"indicator": {"id": "GDP"}, "value": 100}]],
        ),
        (
            "eurostat-dissemination",
            {
                "class": "dataset",
                "id": ["geo", "time"],
                "size": [1, 1],
                "dimension": {"geo": {"category": {"index": {"DE": 0}}}},
                "value": {"0": 100},
            },
        ),
        (
            "sec-edgar",
            {
                "cik": "0000320193",
                "filings": {
                    "recent": {"accessionNumber": ["filing-1"], "form": ["10-K"]}
                },
            },
        ),
        (
            "osv-api",
            {
                "vulns": [{"id": "OSV-fixture", "modified": "2026-01-01T00:00:00Z"}],
                "next_page_token": "native-page",
            },
        ),
    ],
)
def test_native_envelopes_do_not_establish_generic_provider_readiness(
    source_id, payload
):
    sources = [
        s for pack in load_source_packs(Path("config/source_packs")) for s in pack["sources"]
    ]
    source = next(s for s in sources if s["source_id"] == source_id)
    page = HTTPSPageAdapter(
        source, transport=lambda **_: {"content": json.dumps(payload)}
    ).fetch_page(
        {"operation": source["operations"][0], "parameters": {}, "limit": 10},
        cursor=None,
    )
    # The generic boundary wraps metadata or the complete envelope, losing native
    # row/cursor semantics. It must never be advertised as a verified adapter.
    assert not any(isinstance(record.get("id"), str) for record in page.records)
    assert page.next_cursor is None
    assert protocol_status(source)["native_mapping"] == "unsupported"
    assert protocol_status(source)["ready"] is False
