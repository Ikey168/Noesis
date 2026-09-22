"""Bounded native PDF evaluations with comparable page, table and TEI provenance."""

from __future__ import annotations

import hashlib
import importlib.metadata
import ipaddress
import math
import os
import re
from dataclasses import asdict
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit


def _box(value):
    return (
        isinstance(value, (list, tuple))
        and len(value) == 4
        and all(type(v) in {int, float} and math.isfinite(v) for v in value)
        and value[0] <= value[2]
        and value[1] <= value[3]
    )


def normalize_docling(structured):
    """Normalize native Docling coordinates, preserving unknown coordinate states."""
    pages = structured.get("pages", {})

    def convert(box, page):
        if not isinstance(box, dict) or not all(k in box for k in ("l", "t", "r", "b")):
            return None
        values = [box["l"], box["t"], box["r"], box["b"]]
        if box.get("coord_origin", "TOPLEFT") == "BOTTOMLEFT":
            height = (
                pages.get(str(page), pages.get(page, {})).get("size", {}).get("height")
            )
            if type(height) not in {int, float}:
                return None
            values[1], values[3] = height - values[1], height - values[3]
        elif box.get("coord_origin", "TOPLEFT") != "TOPLEFT":
            return None
        return values if _box(values) else None

    locators, tables = [], []
    for item in structured.get("texts", []):
        for prov in item.get("prov", []):
            locators.append(
                {
                    "page": prov.get("page_no"),
                    "bbox": convert(prov.get("bbox"), prov.get("page_no")),
                    "text": item.get("text", ""),
                    "native_ref": item.get("self_ref"),
                    "kind": item.get("label"),
                    "native_provenance": prov,
                }
            )
    for table in structured.get("tables", []):
        provenance = table.get("prov", [])
        page = provenance[0].get("page_no") if len(provenance) == 1 else None
        cells = []
        for cell in table.get("data", {}).get("table_cells", []):
            row = cell.get("start_row_offset_idx", cell.get("start_row_offset"))
            col = cell.get("start_col_offset_idx", cell.get("start_col_offset"))
            if type(row) is not int or type(col) is not int:
                continue
            cells.append(
                {
                    "row": row,
                    "col": col,
                    "text": cell.get("text", ""),
                    "row_span": cell.get("row_span"),
                    "col_span": cell.get("col_span"),
                    "bbox": convert(cell.get("bbox"), page),
                }
            )
        tables.append(
            {
                "page": page,
                "cells": cells,
                "native_ref": table.get("self_ref"),
                "native_provenance": provenance,
            }
        )
    return {
        "locators": locators,
        "tables": tables,
        "references": [],
        "citation_links": [],
    }


def normalize_tei(raw):
    from defusedxml import ElementTree as ET

    if not isinstance(raw, bytes) or not 0 < len(raw) <= 10_000_000:
        raise ValueError("bounded TEI bytes required")
    root = ET.fromstring(raw)
    if root.tag.rsplit("}", 1)[-1] != "TEI":
        raise ValueError("expected TEI document")
    locators, references, links, tables, sections = [], [], [], [], []

    def coords(value):
        output = []
        if not value:
            return output
        for part in value.split(";"):
            parts = part.split(",")
            if len(parts) != 5:
                raise ValueError("invalid GROBID coordinate tuple")
            page, x, y, width, height = map(float, parts)
            box = [x, y, x + width, y + height]
            if (
                not math.isfinite(page)
                or page != int(page)
                or page < 1
                or not _box(box)
            ):
                raise ValueError("invalid GROBID page or bounding box")
            output.append({"page": int(page), "bbox": box})
        return output

    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        text = " ".join("".join(node.itertext()).split())
        identity = node.get("{http://www.w3.org/XML/1998/namespace}id")
        boxes = coords(node.get("coords"))
        if tag in {"head", "p", "s", "biblStruct", "ref"}:
            for box in boxes:
                locators.append(
                    {
                        **box,
                        "text": text,
                        "id": identity,
                        "kind": tag,
                        "coords": node.get("coords"),
                    }
                )
        if tag in {"head", "p"} and text:
            sections.append(
                {"kind": tag, "id": identity, "text": text, "locators": boxes}
            )
        if tag == "biblStruct":
            references.append({"id": identity, "text": text, "locators": boxes})
        if tag == "ref" and node.get("type") == "bibr":
            links.append(
                {
                    "text": text,
                    "targets": node.get("target", "").split(),
                    "locators": boxes,
                }
            )
        if tag == "table":
            cells = []
            for row_index, row in enumerate(node.findall("{*}row")):
                col_index = 0
                for cell in row.findall("{*}cell"):
                    span = int(cell.get("cols", "1"))
                    if not 1 <= span <= 1000:
                        raise ValueError("invalid TEI cell span")
                    cells.append(
                        {
                            "row": row_index,
                            "col": col_index,
                            "text": " ".join("".join(cell.itertext()).split()),
                            "col_span": span,
                        }
                    )
                    col_index += span
            tables.append(
                {
                    "page": boxes[0]["page"] if len(boxes) == 1 else None,
                    "cells": cells,
                    "id": identity,
                    "locators": boxes,
                }
            )
    text = root.find(".//{*}text")
    return {
        "text": " ".join(
            "".join((text if text is not None else root).itertext()).split()
        ),
        "tei": raw.decode("utf-8"),
        "locators": locators,
        "sections": sections,
        "references": references,
        "citation_links": links,
        "tables": tables,
    }


def parse_backend(
    path, backend, *, grobid_url=None, expected_sha256=None, max_pages=250
):
    path = Path(path)
    with path.open("rb") as source:
        raw = source.read(20_000_001)
    if not raw.startswith(b"%PDF") or len(raw) > 20_000_000:
        raise ValueError("bounded PDF input required")
    if expected_sha256 is not None:
        from src.evaluation.runtime_errors import BackendError

        if (
            not isinstance(expected_sha256, str)
            or hashlib.sha256(raw).hexdigest() != expected_sha256
        ):
            raise BackendError(
                "source_changed", "PDF bytes differ from the captured digest"
            )
    if type(max_pages) is not int or not 1 <= max_pages <= 250:
        raise ValueError("PDF page budget must be an integer from 1 to 250")
    import pymupdf

    with pymupdf.open(stream=raw, filetype="pdf") as document:
        if document.needs_pass or not 1 <= len(document) <= max_pages:
            raise ValueError("PDF is encrypted or exceeds page budget")
        if backend == "pymupdf":
            from src.ingestion.connectors.paper.pdf_parser import parse_pdf

            parsed = asdict(parse_pdf(raw))
            parsed.update(locators=[], tables=[], references=[], citation_links=[])
            for number, page in enumerate(document, 1):
                parsed["locators"].extend(
                    {"page": number, "bbox": list(block[:4]), "text": block[4]}
                    for block in page.get_text("blocks")
                    if block[6] == 0
                )
                for table in page.find_tables(
                    strategy="text", min_words_vertical=2
                ).tables:
                    extracted = table.extract()
                    parsed["tables"].append(
                        {
                            "page": number,
                            "bbox": list(table.bbox),
                            "cells": [
                                {
                                    "row": r,
                                    "col": c,
                                    "text": value or "",
                                    "bbox": list(table.rows[r].cells[c])
                                    if table.rows[r].cells[c]
                                    else None,
                                }
                                for r, row in enumerate(extracted)
                                for c, value in enumerate(row)
                            ],
                        }
                    )
            return {
                **parsed,
                "version": importlib.metadata.version("PyMuPDF"),
                "original_sha256": hashlib.sha256(raw).hexdigest(),
                "configuration": {
                    "table_strategy": "text",
                    "min_words_vertical": 2,
                    "ocr": False,
                    "table_spans_inferred": False,
                },
            }
    if backend == "lighton":
        from src.integrations.documents import lighton_ocr

        return lighton_ocr(path)
    if backend == "markitdown":
        from src.integrations.documents import markitdown

        text, metadata = markitdown(raw, path.suffix.lstrip("."))
        return {"text": text, **metadata}
    if backend == "docling":
        from docling.datamodel.accelerator_options import (
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.datamodel.pipeline_options import (
            EasyOcrOptions,
            PdfPipelineOptions,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption

        artifacts = os.environ.get("NOESIS_DOCLING_ARTIFACTS_PATH")
        if not artifacts or not Path(artifacts).is_dir():
            from src.evaluation.runtime_errors import BackendError

            raise BackendError(
                "model_unavailable",
                "explicit local Docling artifact cache required; no implicit downloads",
            )
        options = PdfPipelineOptions(
            artifacts_path=Path(artifacts), enable_remote_services=False
        )
        options.ocr_options = EasyOcrOptions(
            lang=["de", "en"], use_gpu=False, download_enabled=False
        )
        options.accelerator_options = AcceleratorOptions(
            num_threads=2, device=AcceleratorDevice.CPU
        )
        converted = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
        ).convert(
            DocumentStream(name=path.name, stream=BytesIO(raw)),
            raises_on_error=False,
            max_num_pages=250,
            max_file_size=20_000_000,
        )
        native_status = getattr(converted.status, "value", converted.status)
        statuses = {
            "success": "completed",
            "partial_success": "partial",
            "failure": "failed",
            "skipped": "unavailable",
        }
        if native_status not in statuses:
            from src.evaluation.runtime_errors import BackendError

            raise BackendError(
                "invalid_model_output",
                "Docling returned a nonterminal or unknown conversion state",
            )
        status = statuses[native_status]
        outcome = {"status": status, "native_status": native_status}
        if status != "completed":
            outcome["failure_code"] = "docling_" + native_status
        if status in {"failed", "unavailable"}:
            return {
                **outcome,
                "original_sha256": hashlib.sha256(raw).hexdigest(),
                "version": importlib.metadata.version("docling"),
            }
        document = converted.document
        structured = document.export_to_dict()
        return {
            **outcome,
            "text": document.export_to_markdown(),
            "structured": structured,
            **normalize_docling(structured),
            "version": importlib.metadata.version("docling"),
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            "configuration": {
                "device": "cpu",
                "threads": 2,
                "local_artifacts_only": True,
                "ocr": "easyocr",
                "ocr_languages": ["de", "en"],
                "ocr_downloads": False,
            },
        }
    if backend == "grobid":
        if not grobid_url:
            from src.evaluation.runtime_errors import BackendError

            raise BackendError(
                "model_unavailable", "GROBID local service is not configured"
            )
        parsed = urlsplit(grobid_url)
        try:
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            local = False
        if (
            not local
            or parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "GROBID evaluation accepts only an explicitly configured loopback service"
            )
        import httpx

        parts = [("input", (path.name, raw, "application/pdf"))]
        parts += [
            ("teiCoordinates", (None, value))
            for value in ("ref", "biblStruct", "p", "s", "figure", "head")
        ]
        parts += [("includeRawCitations", (None, "1"))]
        with httpx.Client(
            timeout=60, follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "GET", grobid_url.rstrip("/") + "/api/version"
            ) as response:
                response.raise_for_status()
                version = b""
                for chunk in response.iter_bytes():
                    version += chunk
                    if len(version) > 1000:
                        raise ValueError("invalid GROBID version response")
            with client.stream(
                "POST",
                grobid_url.rstrip("/") + "/api/processFulltextDocument",
                files=parts,
            ) as response:
                response.raise_for_status()
                output = bytearray()
                for chunk in response.iter_bytes():
                    output.extend(chunk)
                    if len(output) > 10_000_000:
                        raise ValueError("GROBID TEI response budget exceeded")
        return {
            **normalize_tei(bytes(output)),
            "version": version.decode().strip(),
            "original_sha256": hashlib.sha256(raw).hexdigest(),
            "configuration": {
                "teiCoordinates": ["ref", "biblStruct", "p", "s", "figure", "head"],
                "service_process_memory_included": False,
            },
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
    locators = result.get("locators") or []

    def _iou(left, right):
        lx0, ly0, lx1, ly1 = left
        rx0, ry0, rx1, ry1 = right
        ix0, iy0 = max(lx0, rx0), max(ly0, ry0)
        ix1, iy1 = min(lx1, rx1), min(ly1, ry1)
        intersection = max(0, ix1 - ix0) * max(0, iy1 - iy0)
        union = (lx1 - lx0) * (ly1 - ly0) + (rx1 - rx0) * (ry1 - ry0) - intersection
        return intersection / union if union > 0 else 0.0

    locator_matches = []
    for item in expected["expected"]:
        candidates = [
            locator
            for locator in locators
            if item["text"].casefold() in str(locator.get("text", "")).casefold()
            and locator.get("page") == item.get("page")
        ]
        locator_matches.append(candidates[0] if candidates else None)
    bbox_ious = [
        _iou(item["bbox"], locator["bbox"])
        for item, locator in zip(expected["expected"], locator_matches, strict=True)
        if locator and locator.get("bbox") and item.get("bbox")
    ]

    def table_cells(tables):
        from collections import Counter

        return Counter(
            (
                table.get("page"),
                index,
                cell["row"],
                cell["col"],
                " ".join(str(cell.get("text", "")).casefold().split()),
            )
            for index, table in enumerate(tables)
            for cell in table.get("cells", [])
        )

    wanted = table_cells(expected.get("tables", []))
    actual = table_cells(result.get("tables", []))
    matched = sum((wanted & actual).values())
    reference_records = [item["text"] for item in result.get("references", [])]
    return {
        "token_precision": overlap / len(observed) if observed else 0,
        "table_positional_cell_precision": matched / sum(actual.values())
        if actual and wanted
        else None,
        "table_positional_cell_recall": matched / sum(wanted.values())
        if wanted
        else None,
        "expected_table_count": len(expected.get("tables", [])),
        "detected_table_count": len(result.get("tables", [])),
        "structured_reference_recall": sum(
            any(value in text for text in reference_records) for value in references
        )
        / len(references)
        if references
        else None,
        "token_recall": overlap / len(required) if required else 0,
        "page_text_locator_recall": sum(
            locator is not None for locator in locator_matches
        )
        / len(locator_matches)
        if locator_matches
        else None,
        "mean_bbox_iou": sum(bbox_ious) / len(bbox_ious) if bbox_ious else None,
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
