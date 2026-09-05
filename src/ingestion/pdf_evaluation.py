"""Optional PDF adapters and explicit fixture-fidelity metrics."""

import importlib.metadata
import re
from dataclasses import asdict


def parse_backend(path, backend, *, grobid_url=None):
    if backend == "lighton":
        from src.integrations.documents import lighton_ocr
        return lighton_ocr(path)
    if backend == "markitdown":
        from src.integrations.documents import markitdown
        text, metadata = markitdown(path.read_bytes(), path.suffix.lstrip("."))
        return {"text": text, **metadata}
    if backend == "pymupdf":
        import fitz

        from src.ingestion.connectors.paper.pdf_parser import parse_pdf

        parsed = asdict(parse_pdf(path.read_bytes()))
        with fitz.open(path) as document:
            parsed["locators"] = [
                {"page": i + 1, "bbox": list(block[:4]), "text": block[4]}
                for i, page in enumerate(document)
                for block in page.get_text("blocks")
            ]
        return {**parsed, "version": importlib.metadata.version("PyMuPDF")}
    if backend == "docling":
        import torch

        torch.set_num_threads(2)
        from docling.datamodel.accelerator_options import (
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions()
        options.accelerator_options = AcceleratorOptions(
            num_threads=2, device=AcceleratorDevice.CPU
        )
        converted = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        ).convert(path)
        document = converted.document
        return {
            "text": document.export_to_markdown(),
            "structured": document.export_to_dict(),
            "version": importlib.metadata.version("docling"),
        }
    if backend == "grobid":
        if not grobid_url:
            raise ValueError("GROBID service URL not configured")
        import httpx
        from defusedxml import ElementTree as ET

        with path.open("rb") as stream:
            response = httpx.post(
                grobid_url.rstrip("/") + "/api/processFulltextDocument",
                files={"input": (path.name, stream, "application/pdf")},
                data={"teiCoordinates": "ref,biblStruct,p,s,figure"},
                timeout=60,
                follow_redirects=False,
            )
        response.raise_for_status()
        if len(response.content) > 10_000_000:
            raise ValueError("GROBID TEI too large")
        root = ET.fromstring(response.content)
        return {
            "text": " ".join(root.itertext()),
            "tei": response.text,
            "locators": [
                {
                    "tag": node.tag,
                    "coords": node.get("coords"),
                    "id": node.get("{http://www.w3.org/XML/1998/namespace}id"),
                }
                for node in root.iter()
                if node.get("coords")
            ],
            "version": "service-provided; pin deployment image separately",
        }
    raise ValueError("unknown PDF backend")


def score(expected, result):
    text = result.get("text", "")
    words = lambda value: re.findall(r"\w+", value.casefold())
    required = words(" ".join(item["text"] for item in expected["expected"]))
    observed = words(text)
    from collections import Counter

    overlap = sum((Counter(required) & Counter(observed)).values())
    positions = [text.find(item["text"]) for item in expected["expected"]]
    from itertools import pairwise

    pairs = list(pairwise(positions))
    cells = expected.get("table_cells", [])
    references = expected.get("references", [])
    return {
        "token_recall": overlap / len(required) if required else 0,
        "expected_line_order_recall": sum(a >= 0 and b > a for a, b in pairs)
        / len(pairs)
        if pairs
        else None,
        "table_cell_text_recall": sum(
            cell.casefold() in text.casefold() for cell in cells
        )
        / len(cells)
        if cells
        else None,
        "reference_text_recall": sum(value in text for value in references)
        / len(references)
        if references
        else None,
        "limitations": "Text/order proxy metrics; table text is not table structure fidelity; coordinates retained but not independently adjudicated.",
    }
