import json
from xml.etree.ElementTree import ParseError

import duckdb
import pytest

from src.ingestion.roadmap_integrations import (
    HostedDiscovery,
    JinaReaderFallback,
    OpenAlexContentAcquirer,
    normalize_paddleocr,
    normalize_regional_record,
    optional_document_conversion,
    parse_tei,
)
from src.ingestion.snapshots import SnapshotStore

TEI = b"""<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><div><head xml:id="h1" coords="1,1,2,3,4">Ergebnisse</head><p xml:id="p1" coords="1,4,5,6,7">Belegtext</p></div></body><back><listBibl><biblStruct xml:id="b1" coords="1,8,9,10,11"><monogr><title>Quelle</title></monogr></biblStruct></listBibl></back></text></TEI>"""


def test_openalex_content_tei_snapshot_cost_and_replay():
    calls = []

    def transport(**kw):
        calls.append(kw)
        return {
            "content": TEI,
            "headers": {"Content-Type": "application/xml"},
            "fetched_at_ms": 123,
        }

    acquirer = OpenAlexContentAcquirer(
        SnapshotStore(duckdb.connect()), transport=transport, budget_micros=10
    )
    work = {
        "id": "https://openalex.org/W123",
        "has_content": True,
        "content_urls": [
            {
                "url": "https://content.openalex.org/W123.xml",
                "format": "grobid-xml",
                "license": "cc-by",
            }
        ],
    }
    first = acquirer.acquire(work, price_micros=5)
    second = acquirer.acquire(work, price_micros=5)
    assert first == second and len(calls) == 1 and acquirer.spent_micros == 5
    assert first["sections"][1]["id"] == "p1" and first["references"][0]["id"] == "b1"
    assert first["snapshot"]["digest"] == first["sha256"]
    assert (
        acquirer.acquire({"id": "https://openalex.org/W2", "has_content": False})[
            "outcome"
        ]
        == "unavailable"
    )
    assert (
        acquirer.acquire(
            {
                "id": "https://openalex.org/W3",
                "has_content": True,
                "content_urls": [{"url": "https://content.openalex.org/W3.pdf"}],
            },
            price_micros=6,
        )["failure_code"]
        == "priced_download_budget_exceeded"
    )


def test_tei_rejects_malformed_and_oversized():
    assert parse_tei(TEI)["references"][0]["coords"]
    with pytest.raises(ParseError):
        parse_tei(b"<bad>")
    with pytest.raises(ValueError, match="oversized"):
        parse_tei(TEI, max_bytes=2)


@pytest.mark.parametrize(
    "provider",
    [
        "ctis",
        "drks",
        "cellar",
        "german-courts",
        "berlin-law",
        "opencorporates",
        "opensanctions",
        "ema",
        "bfarm",
    ],
)
def test_regional_provider_fixture_contracts(provider):
    item = normalize_regional_record(
        provider,
        {
            "id": "DE-123",
            "title": "Öffentlicher Beleg",
            "language": "de",
            "jurisdiction": "DE",
            "missing_fields": ["result"],
        },
    )
    assert (
        item["provider_id"] == "DE-123"
        and item["native"]["title"] == "Öffentlicher Beleg"
    )
    assert item["missing_fields"] == ["result"]
    if provider == "opensanctions":
        assert item["review_required"] is True


def test_discovery_is_opt_in_bounded_and_not_evidence():
    with pytest.raises(ValueError, match="opt-in"):
        HostedDiscovery(
            "exa",
            api_key="x",
            transport=lambda **kw: {},
            per_request_cost_micros=1,
            budget_micros=1,
        )

    def transport(**kw):
        return {
            "content": json.dumps(
                {
                    "results": [
                        {
                            "id": "native-1",
                            "url": "https://example.org/source",
                            "title": "Quelle",
                            "text": "provider generated summary",
                        }
                    ]
                }
            )
        }

    adapter = HostedDiscovery(
        "tavily",
        api_key="secret",
        transport=transport,
        per_request_cost_micros=2,
        budget_micros=2,
        enabled=True,
    )
    refs = list(
        adapter.discover(
            "Berlin Energie",
            domains=["example.org"],
            from_date="2026-01-01",
            max_results=1,
        )
    )
    assert refs[0].metadata["provider_summary_is_evidence"] is False
    assert refs[0].metadata["requires_source_acquisition"] is True
    with pytest.raises(ValueError, match="budget"):
        list(adapter.discover("again"))


def test_jina_requires_snapshot_and_labels_transformation():
    adapter = JinaReaderFallback(
        transport=lambda **kw: {"content": b"# transformed\ntext"}, enabled=True
    )
    with pytest.raises(ValueError, match="snapshot"):
        adapter.extract("https://example.org/a", original_snapshot=None)
    result = adapter.extract(
        "https://example.org/a", original_snapshot={"digest": "abc"}
    )
    assert (
        result["precise_locators"] is False
        and result["original_snapshot"]["digest"] == "abc"
    )


def test_markitdown_boundary_preserves_original_and_failures():
    result = optional_document_conversion(
        b"original",
        filename="brief.docx",
        converter=lambda data, file_extension: "Deutsch\n|A|B|",
    )
    assert result["status"] == "completed" and result["precise_locators"] is False
    failed = optional_document_conversion(
        b"broken",
        filename="bad.docx",
        converter=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bad")),
    )
    assert failed["status"] == "failed" and failed["failure_type"] == "RuntimeError"
    malformed = optional_document_conversion(
        b"PK\x03\x04not-a-docx", filename="bad.docx"
    )
    assert malformed["status"] == "failed"
    assert malformed["failure_type"] == "invalid_docx_container"


def test_office_expansion_limit_precedes_optional_converter_loading():
    import io
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "x" * 20000)
    raw = output.getvalue()
    assert len(raw) < 1000
    result = optional_document_conversion(raw, filename="expanded.docx", max_bytes=1000)
    assert result["failure_type"] == "document_expansion_limit"


def test_native_office_embedded_content_is_not_claimed_as_extracted():
    import io
    import zipfile
    from pathlib import Path

    import pytest

    pytest.importorskip("markitdown")
    raw = Path(
        "tests/fixtures/workflow_review/local_optional/german_english_table.docx"
    ).read_bytes()
    output = io.BytesIO(raw)
    with zipfile.ZipFile(output, "a") as archive:
        archive.writestr(
            "word/embeddings/unknown.bin", b"embedded content is not text evidence"
        )
    result = optional_document_conversion(output.getvalue(), filename="embedded.docx")
    assert result["status"] == "completed"
    assert result["embedded_content_count"] == 1
    assert result["embedded_content_coverage"] == "not_evaluated"
    assert "embedded content is not text evidence" not in result["text"]


def test_paddleocr_keeps_page_boxes_confidence_and_uncertainty():
    result = normalize_paddleocr(
        [
            [
                [[[0, 0], [10, 0], [10, 10], [0, 10]], ("Berlin", 0.95)],
                [[[0, 0], [5, 0], [5, 5], [0, 5]], ("unsicher", 0.4)],
            ]
        ],
        model_version="fixture-model",
        config={"lang": "german"},
        elapsed_seconds=1.2,
        peak_rss_kib=100,
    )
    assert [r["page"] for r in result["regions"]] == [1, 1]
    assert (
        result["regions"][1]["uncertain"] is True
        and result["missing_regions_preserved"] is True
    )
