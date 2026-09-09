"""Dispatch/SDK-boundary regressions, not native Docling quality measurements."""

import hashlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from src.evaluation.runtime_errors import BackendError
from src.evaluation.runtime_jobs import dispatch
from src.evaluation.runtime_outcomes import backend_outcome
from src.ingestion import pdf_evaluation


@pytest.fixture
def pdf(tmp_path):
    pytest.importorskip("pymupdf")
    path = tmp_path / "captured.pdf"
    raw = (
        Path(__file__).parents[2] / "fixtures/pdf_benchmark/digital.pdf"
    ).read_bytes()
    path.write_bytes(raw)
    return path, raw, hashlib.sha256(raw).hexdigest()


@pytest.fixture
def docling_double(monkeypatch, tmp_path):
    state = {"status": "success", "inputs": []}
    modules = {
        name: ModuleType(name)
        for name in [
            "docling",
            "docling.datamodel",
            "docling.datamodel.base_models",
            "docling.datamodel.accelerator_options",
            "docling.datamodel.pipeline_options",
            "docling.document_converter",
        ]
    }
    modules["docling.datamodel.base_models"].InputFormat = SimpleNamespace(PDF="pdf")
    modules["docling.datamodel.base_models"].DocumentStream = SimpleNamespace
    modules[
        "docling.datamodel.accelerator_options"
    ].AcceleratorDevice = SimpleNamespace(CPU="cpu")
    modules[
        "docling.datamodel.accelerator_options"
    ].AcceleratorOptions = SimpleNamespace
    modules["docling.datamodel.pipeline_options"].EasyOcrOptions = SimpleNamespace
    modules["docling.datamodel.pipeline_options"].PdfPipelineOptions = SimpleNamespace
    modules["docling.document_converter"].PdfFormatOption = SimpleNamespace

    class Converter:
        def __init__(self, **kwargs):
            state["configuration"] = kwargs

        def convert(self, source, **kwargs):
            state["inputs"].append(source)
            state["arguments"] = kwargs
            if state.get("replace"):
                state["replace"].write_bytes(b"changed after validation")
            return SimpleNamespace(
                status=SimpleNamespace(value=state["status"]),
                document=SimpleNamespace(
                    export_to_dict=lambda: {"pages": {"1": {}}, "texts": []},
                    export_to_markdown=lambda: "partial evidence",
                ),
            )

    modules["docling.document_converter"].DocumentConverter = Converter
    for name, value in modules.items():
        monkeypatch.setitem(sys.modules, name, value)
    native_version = pdf_evaluation.importlib.metadata.version
    monkeypatch.setattr(
        pdf_evaluation.importlib.metadata,
        "version",
        lambda name: "fixture-sdk" if name == "docling" else native_version(name),
    )
    monkeypatch.setenv("NOESIS_DOCLING_ARTIFACTS_PATH", str(tmp_path))
    return state


@pytest.mark.parametrize(
    "native,expected",
    [
        ("success", "completed"),
        ("partial_success", "partial"),
        ("failure", "failed"),
        ("skipped", "unavailable"),
    ],
)
def test_native_conversion_status_reaches_worker_outcome(
    pdf, docling_double, native, expected
):
    path, _raw, digest = pdf
    docling_double["status"] = native
    result = dispatch("pdf-docling", {"path": str(path), "sha256": digest})
    assert result["status"] == expected
    assert result["native_status"] == native
    assert backend_outcome("pdf-docling", result)["status"] == expected


def test_docling_reads_only_digest_verified_stream(pdf, docling_double):
    path, raw, digest = pdf
    docling_double["replace"] = path
    result = dispatch("pdf-docling", {"path": str(path), "sha256": digest})
    supplied = docling_double["inputs"][0]
    assert not isinstance(supplied, (str, Path))
    assert supplied.stream.getvalue() == raw
    assert docling_double["arguments"]["raises_on_error"] is False
    assert result["original_sha256"] == digest


def test_pdf_dispatch_detects_change_between_wrapper_and_parser(pdf, monkeypatch):
    path, _raw, digest = pdf
    original = pdf_evaluation.parse_backend
    replacement = (
        Path(__file__).parents[2] / "fixtures/pdf_benchmark/columns.pdf"
    ).read_bytes()

    def replace_then_parse(*args, **kwargs):
        path.write_bytes(replacement)
        return original(*args, **kwargs)

    monkeypatch.setattr(pdf_evaluation, "parse_backend", replace_then_parse)
    with pytest.raises(BackendError) as failure:
        dispatch("pdf-pymupdf", {"path": str(path), "sha256": digest})
    assert failure.value.code == "source_changed"


def test_pdf_page_budget_is_enforced_before_parsing(tmp_path):
    import pymupdf

    from src.ingestion.pdf_evaluation import parse_backend

    path = tmp_path / "two-pages.pdf"
    with pymupdf.open() as document:
        document.new_page()
        document.new_page()
        document.save(path)
    with pytest.raises(ValueError, match="page budget"):
        parse_backend(path, "pymupdf", max_pages=1)
    for invalid in (True, 0, 251, "100"):
        with pytest.raises(ValueError, match="page budget"):
            parse_backend(path, "pymupdf", max_pages=invalid)
