"""Compare bounded PaddleOCR with the existing book OCR on authored scan probes."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.evaluation.benchmark_runtime import score_output
from src.evaluation.runtime_jobs import execute_job


def corpus(directory):
    import pymupdf
    from PIL import Image

    directory.mkdir(parents=True, exist_ok=True)
    existing = directory / "manifest.json"
    if existing.exists():
        manifest = json.loads(existing.read_text())
        path = directory / "authored-scans.pdf"
        if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
            raise ValueError("frozen OCR corpus digest changed")
        return path, manifest
    cases = [
        (
            "german-english",
            [
                "Berliner Verwaltung: Beispielbescheid",
                "Die Frist endet am 15. September 2026.",
                "This English abstract describes an authored test.",
            ],
        ),
        (
            "table",
            [
                "Jahr     Berlin     Hamburg",
                "2024     120        95",
                "2025     130        98",
                "Tabelle: frei erfundene Zahlen.",
            ],
        ),
        (
            "rotated",
            [
                "Gedrehte Seite: deutsche Forschung",
                "Die gemessene Anzahl ist 42.",
                "Rotation must preserve source coordinates.",
            ],
        ),
        (
            "low-quality",
            [
                "Unscharfer Scan: öffentliche Forschung",
                "Die Prüfung bleibt ausdrücklich erforderlich.",
                "Noisy scans may omit important words.",
            ],
        ),
    ]
    output = pymupdf.open()
    references = []
    for name, lines in cases:
        source = pymupdf.open()
        page = source.new_page(width=595, height=842)
        for i, text in enumerate(lines):
            page.insert_text((50, 80 + i * 35), text, fontsize=14)
        boxes = [
            line["bbox"]
            for block in page.get_text("dict")["blocks"]
            if "lines" in block
            for line in block["lines"]
        ]
        pix = page.get_pixmap(dpi=72 if name == "low-quality" else 150)
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=20 if name == "low-quality" else 90)
        scanned = output.new_page(width=595, height=842)
        scanned.insert_image(scanned.rect, stream=buffer.getvalue())
        if name == "rotated":
            scanned.set_rotation(90)
        references.append(
            {
                "case": name,
                "page": len(output),
                "text": "\n".join(lines),
                "regions": [
                    {
                        "text": text,
                        "bbox_pixels": list(
                            pymupdf.Rect(box)
                            * scanned.rotation_matrix
                            * pymupdf.Matrix(150 / 72, 150 / 72)
                        ),
                    }
                    for text, box in zip(lines, boxes, strict=True)
                ],
            }
        )
        source.close()
    path = directory / "authored-scans.pdf"
    output.save(path)
    output.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {
        "license": "CC0-1.0",
        "provenance": "Authored regression probes, not independently annotated real documents.",
        "sha256": digest,
        "cases": references,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return path, manifest


def iou(left, right):
    overlap = max(0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0, min(left[3], right[3]) - max(left[1], right[1])
    )
    area = lambda b: max(0, b[2] - b[0]) * max(0, b[3] - b[1])
    return overlap / max(1, area(left) + area(right) - overlap)


def evaluate(directory):
    path, manifest = corpus(directory)
    report = {
        "contract": "noesis-native-ocr-comparison-v1",
        "fixture": manifest,
        "runs": {},
    }
    expected = " ".join(" ".join(row["text"].split()) for row in manifest["cases"])
    for operation in ("ocr-tesseract", "paddleocr"):
        job = execute_job(
            operation,
            {
                "path": str(path.resolve()),
                "sha256": manifest["sha256"],
                "max_pages": 4,
                "dpi": 150,
            },
            timeout_s=120,
            max_rss_bytes=3 * 1024**3,
        )
        row = {"job": job}
        if job["status"] == "completed":
            value = job["result"]
            text = (
                value.get("text")
                if operation == "ocr-tesseract"
                else "\n".join(p["text"] for p in value["pages"])
            )
            row["metrics"] = score_output("text", " ".join(text.split()), expected)
            row["metrics"]["normalization"] = (
                "collapse whitespace for both outputs and authored reference"
            )
            if operation == "paddleocr":
                row["pages"] = []
                for actual, ref in zip(value["pages"], manifest["cases"], strict=True):
                    matching = [
                        (
                            truth,
                            next(
                                (
                                    v
                                    for v in actual["regions"]
                                    if v["text"] == truth["text"]
                                ),
                                None,
                            ),
                        )
                        for truth in ref["regions"]
                    ]
                    matches = [(a, b) for a, b in matching if b is not None]
                    indices = [b["index"] for a, b in matches]
                    row["pages"].append(
                        {
                            "case": ref["case"],
                            "metrics": score_output(
                                "text",
                                " ".join(actual["text"].split()),
                                " ".join(ref["text"].split()),
                            ),
                            "exact_region_recall": len(matches) / len(matching),
                            "matched_region_order_correct": indices == sorted(indices),
                            "mean_bbox_iou_for_exact_matches": sum(
                                iou(a["bbox_pixels"], b["bbox_pixels"])
                                for a, b in matches
                            )
                            / len(matches)
                            if matches
                            else None,
                            "table_structure_status": actual["table_structure_status"],
                        }
                    )
                    row["pages"][-1]["metrics"]["normalization"] = row["metrics"][
                        "normalization"
                    ]
        report["runs"][operation] = row
    report["original_unchanged"] = (
        hashlib.sha256(path.read_bytes()).hexdigest() == manifest["sha256"]
    )
    report["decision"] = (
        "defer default adoption; authored probes do not establish real-domain quality, and plain OCR does not reconstruct table structure"
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.write_text(json.dumps(evaluate(args.workdir), indent=2) + "\n")
