"""Actual optional OCR and forced alignment with source-identity preservation.

Run these adapters through runtime_jobs for hard deadlines and process-tree RSS
limits. Original binaries and transcript segments are never rewritten here.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import math
import re
from pathlib import Path

from src.argument_mining.model_registry import optional_model_spec
from src.evaluation.model_backends import bounded_texts, model_path
from src.evaluation.runtime_errors import BackendError


def _version(package):
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return "injected-test-backend"


def bounded_file(path, digest, *, max_bytes=50 * 1024**2):
    path = Path(path)
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("expected source SHA-256 required")
    if not path.is_file() or not 0 < path.stat().st_size <= max_bytes:
        raise BackendError("input_limit", "missing or oversized local source file")
    with path.open("rb") as stream:
        raw = stream.read(max_bytes + 1)
    if len(raw) > max_bytes or hashlib.sha256(raw).hexdigest() != digest:
        raise BackendError(
            "source_changed", "source content differs from frozen input identity"
        )
    return raw


def render_pages(
    path, digest, *, pages=None, max_pages=10, dpi=150, max_pixels=8_000_000
):
    """Render one bounded PDF page at a time; coordinates describe rendered page."""
    import pymupdf
    from PIL import Image

    if (
        type(max_pages) is not int
        or not 1 <= max_pages <= 25
        or type(dpi) is not int
        or not 72 <= dpi <= 250
    ):
        raise ValueError("invalid OCR page/resolution limits")
    if type(max_pixels) is not int or not 1 <= max_pixels <= 16_000_000:
        raise ValueError("invalid rendered pixel budget")
    raw = bounded_file(path, digest)
    if not raw.startswith(b"%PDF"):
        raise ValueError("OCR input must be a PDF")
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        if document.needs_pass:
            raise BackendError(
                "encrypted_source",
                "encrypted PDFs require a separately authorized decryption stage",
            )
        selected = list(range(1, len(document) + 1)) if pages is None else list(pages)
        if (
            not selected
            or len(selected) > max_pages
            or len(set(selected)) != len(selected)
        ):
            raise BackendError(
                "input_limit", "explicit distinct OCR pages must fit the page budget"
            )
        if any(type(p) is not int or not 1 <= p <= len(document) for p in selected):
            raise ValueError("page selection outside document")
        for number in selected:
            page = document[number - 1]
            width, height = (
                math.ceil(page.rect.width * dpi / 72),
                math.ceil(page.rect.height * dpi / 72),
            )
            if width * height > max_pixels:
                raise BackendError(
                    "pixel_limit", "rendered page exceeds its pixel budget"
                )
            pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB, alpha=False)
            image = Image.frombytes(
                "RGB", (pixmap.width, pixmap.height), pixmap.samples
            )
            receipt = {
                "source_sha256": digest,
                "page": number,
                "page_count": len(document),
                "width_px": pixmap.width,
                "height_px": pixmap.height,
                "dpi": dpi,
                "rotation_degrees": int(page.rotation),
                "cropbox": list(page.cropbox),
                "render_sha256": hashlib.sha256(pixmap.samples).hexdigest(),
                "coordinate_frame": "rendered-page top-left pixels; not raw PDF user-space",
                "rendering_engine": "pymupdf:" + _version("PyMuPDF"),
            }
            try:
                yield image, receipt
            finally:
                image.close()


class PaddleOCRBackend:
    """Pinned PP-OCRv5 detection and Latin-script recognition, no implicit downloads."""

    def __init__(self, *, engine=None):
        self.models = [
            optional_model_spec("paddle-det"),
            optional_model_spec("paddle-latin"),
        ]
        if engine is None:
            from paddleocr import PaddleOCR

            detection, _ = model_path("paddle-det")
            recognition, _ = model_path("paddle-latin")
            engine = PaddleOCR(
                text_detection_model_name="PP-OCRv5_mobile_det",
                text_recognition_model_name="latin_PP-OCRv5_mobile_rec",
                text_detection_model_dir=detection,
                text_recognition_model_dir=recognition,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
                device="cpu",
                # Pinned Paddle 3.3.1 CPU oneDNN cannot execute the selected
                # detector's PIR DoubleAttribute array on this runtime.
                enable_mkldnn=False,
            )
        self.engine = engine

    def recognize(self, image, *, receipt):
        import numpy as np

        # Paddle expects OpenCV BGR arrays. All spatial pre-processing models
        # are disabled, so returned coordinates remain in this page image.
        array = np.asarray(image.convert("RGB"))[:, :, ::-1].copy()
        iterator = iter(self.engine.predict(array, text_rec_score_thresh=0.0))
        first = next(iterator, None)
        if first is None or next(iterator, None) is not None:
            raise BackendError(
                "invalid_model_output", "expected exactly one Paddle page result"
            )
        native = getattr(first, "json", first)
        if not isinstance(native, dict):
            raise BackendError(
                "invalid_model_output", "Paddle output must expose native JSON"
            )
        native = native.get("res", native)
        text, scores, boxes = (
            native.get("rec_texts", []),
            native.get("rec_scores", []),
            native.get("rec_boxes", []),
        )
        if not len(text) == len(scores) == len(boxes) or len(text) > 10000:
            raise BackendError(
                "invalid_model_output", "unaligned or excessive Paddle OCR regions"
            )
        regions = []
        for index, (content, confidence, bbox) in enumerate(
            zip(text, scores, boxes, strict=True)
        ):
            confidence = float(confidence)
            bbox = list(map(float, bbox))
            if (
                not isinstance(content, str)
                or len(content) > 100000
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise BackendError(
                    "invalid_model_output", "invalid OCR text/confidence"
                )
            if (
                len(bbox) != 4
                or any(not math.isfinite(v) for v in bbox)
                or not 0 <= bbox[0] <= bbox[2] <= image.width
                or not 0 <= bbox[1] <= bbox[3] <= image.height
            ):
                raise BackendError(
                    "invalid_model_output", "OCR bbox outside rendered page"
                )
            regions.append(
                {
                    "index": index,
                    "text": content,
                    "confidence": confidence,
                    "bbox_pixels": bbox,
                    "page": receipt["page"],
                    "uncertain": confidence < 0.8 or not content.strip(),
                }
            )
        detected = native.get("dt_polys", [])
        return {
            "backend": "paddleocr",
            "version": _version("paddleocr"),
            "runtime_versions": {
                "paddlepaddle": _version("paddlepaddle"),
                "paddlex": _version("paddlex"),
            },
            "models": self.models,
            "configuration": {
                "device": "cpu",
                "enable_mkldnn": False,
                "orientation_correction": False,
            },
            "page_receipt": receipt,
            "regions": regions,
            "text": "\n".join(row["text"] for row in regions),
            "unrecognized_detection_count": max(0, len(detected) - len(regions)),
            "region_order": "native backend reading-order proposal",
            "table_structure": None,
            "table_structure_status": "not_produced_by_plain_ocr",
            "correctness_verified": False,
        }


class LightOnOCRBackend:
    """LightOnOCR generation with page identity and explicit unavailable text boxes."""

    def __init__(
        self, *, model=None, processor=None, max_new_tokens=1024, max_input_tokens=4096
    ):
        if (
            type(max_new_tokens) is not int
            or not 1 <= max_new_tokens <= 4096
            or not 64 <= max_input_tokens <= 8192
        ):
            raise ValueError("invalid OCR generation token limit")
        self.max_new_tokens, self.max_input_tokens = max_new_tokens, max_input_tokens
        self.spec = optional_model_spec("lightonocr")
        if model is None or processor is None:
            import torch
            from transformers import (
                LightOnOcrForConditionalGeneration,
                LightOnOcrProcessor,
            )

            path, _ = model_path("lightonocr")
            model = (
                LightOnOcrForConditionalGeneration.from_pretrained(
                    path,
                    torch_dtype=torch.float32,
                    local_files_only=True,
                    trust_remote_code=False,
                )
                .to("cpu")
                .eval()
            )
            processor = LightOnOcrProcessor.from_pretrained(
                path, local_files_only=True, trust_remote_code=False
            )
        self.model, self.processor = model, processor

    def recognize(self, image, *, receipt):
        import torch

        messages = [{"role": "user", "content": [{"type": "image", "image": image}]}]
        inputs = self.processor.apply_chat_template(
            messages,
            add_generation_prompt=True,
            tokenize=True,
            return_dict=True,
            return_tensors="pt",
        )
        count = int(inputs["input_ids"].shape[1])
        if count > self.max_input_tokens:
            raise BackendError(
                "token_limit", "OCR image tokens exceed configured input budget"
            )
        device = self.model.device
        inputs = {
            key: value.to(device=device, dtype=self.model.dtype)
            if value.is_floating_point()
            else value.to(device)
            for key, value in inputs.items()
        }
        with torch.inference_mode():
            generated = self.model.generate(
                **inputs, max_new_tokens=self.max_new_tokens, do_sample=False
            )
        suffix = generated[0, count:]
        eos = getattr(self.model.generation_config, "eos_token_id", None)
        eos = set(eos if isinstance(eos, list) else [eos])
        truncated = len(suffix) >= self.max_new_tokens and (
            not len(suffix) or int(suffix[-1]) not in eos
        )
        text = self.processor.decode(suffix, skip_special_tokens=True)
        if not isinstance(text, str) or len(text) > 262144:
            raise BackendError("output_budget", "OCR returned excessive generated text")
        return {
            "backend": "lightonocr",
            "model": self.spec,
            "page_receipt": receipt,
            "text": text,
            "status": "partial" if truncated else "completed",
            "truncated": truncated,
            "generated_tokens": len(suffix),
            "text_coordinates": None,
            "coordinate_fidelity": "page-only; no text bounding boxes produced",
            "correctness_verified": False,
            "generation_is_transcription_not_source_bytes": True,
        }


def ocr_pdf(path, digest, backend, *, pages=None, max_pages=10, dpi=150):
    results = []
    for image, receipt in render_pages(
        path, digest, pages=pages, max_pages=max_pages, dpi=dpi
    ):
        results.append(backend.recognize(image, receipt=receipt))
    complete = bool(results) and all(
        not row.get("truncated") and row.get("status", "completed") == "completed"
        for row in results
    )
    return {
        "source_sha256": digest,
        "pages": results,
        "coverage": "selected-pages" if pages is not None else "all-pages",
        "status": "completed" if complete else "partial",
        "complete": complete,
        "original_modified": False,
    }


class WhisperXAligner:
    """Align existing transcripts; retain unaligned tokens, never run new ASR."""

    def __init__(self, language, *, aligner=None, model=None, metadata=None):
        if language not in {"de", "en"}:
            raise BackendError(
                "unsupported_language",
                "only explicitly pinned German/English alignment models are configured",
            )
        self.language = language
        self.spec = optional_model_spec("align-" + language)
        if aligner is None:
            import nltk
            from whisperx.alignment import align, load_align_model

            # WhisperX otherwise tries downloading a tokenizer from inside align.
            punkt = "german" if language == "de" else "english"
            try:
                nltk.data.find("tokenizers/punkt_tab/" + punkt + "/")
            except LookupError as exc:
                raise BackendError(
                    "model_unavailable",
                    "install the required NLTK punkt_tab resources explicitly",
                ) from exc
            path, _ = model_path("align-" + language)
            model, metadata = load_align_model(
                language_code=language,
                device="cpu",
                model_name=path,
                model_cache_only=True,
            )
            aligner = align
        self.aligner, self.model, self.metadata = aligner, model, metadata

    def align(self, audio, transcript, *, media_sha256):
        import numpy as np

        samples = np.asarray(audio, dtype=np.float32)
        if (
            samples.ndim != 1
            or not 0 < len(samples) <= 600 * 16000
            or not np.isfinite(samples).all()
        ):
            raise ValueError("finite mono 16 kHz audio up to 600 seconds required")
        duration = len(samples) / 16000
        segments = transcript.get("segments")
        if (
            not transcript.get("media_id")
            or not isinstance(segments, list)
            or not 1 <= len(segments) <= 128
        ):
            raise ValueError("stable media ID and bounded original segments required")
        output, unaligned = [], []
        for index, segment in enumerate(segments):
            start, end = segment.get("start"), segment.get("end")
            if (
                type(start) not in {int, float}
                or type(end) not in {int, float}
                or not 0 <= start < end <= duration
            ):
                raise ValueError("transcript segment outside media duration")
            text = segment.get("text")
            bounded_texts([text], max_chars=8192)
            raw = self.aligner(
                [{"start": start, "end": end, "text": text}],
                self.model,
                self.metadata,
                samples,
                device="cpu",
                return_char_alignments=True,
                print_progress=False,
            )
            words = list(raw.get("word_segments", []))
            if len(words) > 5000:
                raise BackendError("output_budget", "excessive alignment output")
            cursor = 0
            # Retain every source token. Native timestamps with no alignment score
            # may be interpolated; do not expose them as measured boundaries.
            for token in re.finditer(r"\S+", text):
                word = words[cursor] if cursor < len(words) else None
                if word and word.get("word") == token.group():
                    cursor += 1
                else:
                    word = None
                row = {
                    "word": token.group(),
                    "segment_index": index,
                    "start_char": token.start(),
                    "end_char": token.end(),
                    "speaker": segment.get("speaker"),
                    "media_id": transcript["media_id"],
                }
                a, b, confidence = (
                    (word.get("start"), word.get("end"), word.get("score"))
                    if word
                    else (None, None, None)
                )
                if (
                    all(
                        type(v) in {int, float} and math.isfinite(v)
                        for v in (a, b, confidence)
                    )
                    and start <= a <= b <= end
                    and 0 <= confidence <= 1
                ):
                    output.append(
                        {
                            **row,
                            "start_s": float(a),
                            "end_s": float(b),
                            "alignment_score": float(confidence),
                            "status": "aligned",
                        }
                    )
                else:
                    unaligned.append(
                        {**row, "start_s": None, "end_s": None, "status": "unaligned"}
                    )
            if cursor < len(words):
                raise BackendError(
                    "source_changed",
                    "alignment output tokens do not match the original transcript",
                )
        return {
            "contract": "noesis-word-alignment-v1",
            "media_id": transcript["media_id"],
            "media_sha256": media_sha256,
            "model": self.spec,
            "version": _version("whisperx"),
            "language": self.language,
            "original_segments": copy.deepcopy(segments),
            "aligned_words": output,
            "unaligned_words": unaligned,
            "coverage": len(output) / (len(output) + len(unaligned))
            if output or unaligned
            else 0.0,
            "transcription_accuracy_inferred": False,
            "speaker_attribution_changed": False,
        }


def decode_audio_bytes(raw, *, max_seconds=600, sample_rate=16000):
    """Decode captured bytes via pipes only; media cannot open URLs or local files.

    External/seek-dependent formats fail explicitly. An extra second detects
    overlong media instead of silently truncating it to an accepted duration.
    """
    import subprocess

    import numpy as np

    if (
        not isinstance(raw, bytes)
        or not 1 <= len(raw) <= 50_000_000
        or not 0.1 <= max_seconds <= 600
        or sample_rate != 16000
    ):
        raise BackendError(
            "input_limit", "bounded captured audio and 16 kHz alignment required"
        )
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-threads",
                "2",
                "-protocol_whitelist",
                "pipe",
                "-i",
                "pipe:0",
                "-t",
                str(max_seconds + 1),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-acodec",
                "pcm_s16le",
                "-ar",
                str(sample_rate),
                "pipe:1",
            ],
            input=raw,
            capture_output=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackendError(
            "optional_dependency_unavailable",
            "ffmpeg is required for local audio decoding",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendError(
            "deadline_exceeded", "audio decoding exceeded its deadline"
        ) from exc
    if result.returncode or not result.stdout or len(result.stdout) % 2:
        raise BackendError(
            "unsupported_media",
            "audio decoding failed under pipe-only protocol isolation",
        )
    if len(result.stdout) > 2 * sample_rate * max_seconds:
        raise BackendError("input_limit", "audio duration exceeds the allowed maximum")
    return np.frombuffer(result.stdout, np.int16).astype(np.float32) / 32768.0


def media_job(operation, payload):
    if operation == "ocr-tesseract":
        import pymupdf

        from src.ingestion.connectors.book.pdf_parser import _parse_with_ocr

        raw = bounded_file(payload["path"], payload["sha256"])
        maximum = payload.get("max_pages", 10)
        if type(maximum) is not int or not 1 <= maximum <= 25:
            raise ValueError("bounded OCR page count required")
        with pymupdf.open(stream=raw, filetype="pdf") as document:
            if document.needs_pass or not 1 <= len(document) <= maximum:
                raise BackendError(
                    "input_limit", "baseline PDF exceeds page budget or is encrypted"
                )
            count = len(document)
        parsed = _parse_with_ocr(raw)
        if parsed is None:
            raise BackendError(
                "unavailable_text", "existing OCR baseline returned no text"
            )
        import pytesseract

        return {
            "backend": "existing-book-ocr-tesseract",
            "tesseract_version": str(pytesseract.get_tesseract_version()),
            "version": _version("pytesseract"),
            "source_sha256": payload["sha256"],
            "page_count": count,
            "text": "\n".join(section.text for section in parsed[0]),
            "configuration": "existing default English OCR; pdf2image default rendering",
            "locator_status": "existing baseline does not return page/bounding-box locators",
            "original_modified": False,
        }
    if operation in {"paddleocr", "lightonocr"}:
        backend = (
            PaddleOCRBackend()
            if operation == "paddleocr"
            else LightOnOCRBackend(max_new_tokens=payload.get("max_new_tokens", 1024))
        )
        return ocr_pdf(
            payload["path"],
            payload["sha256"],
            backend,
            pages=payload.get("pages"),
            max_pages=payload.get("max_pages", 10),
            dpi=payload.get("dpi", 150),
        )
    if operation == "whisperx":
        raw = bounded_file(payload["path"], payload["sha256"], max_bytes=50_000_000)
        audio = decode_audio_bytes(raw, max_seconds=payload.get("max_seconds", 600))
        return WhisperXAligner(payload["language"]).align(
            audio, payload["transcript"], media_sha256=payload["sha256"]
        )
    raise ValueError("unsupported media operation")
