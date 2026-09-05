"""Optional document converters with explicit representation and locator limits."""

import io
from .common import IntegrationError, version


def markitdown(content, fmt):
    from markitdown import MarkItDown

    if fmt not in {"txt", "md", "html", "pdf", "docx", "pptx", "xlsx", "csv"}:
        raise IntegrationError("unsupported_format", "Unsupported MarkItDown format")
    if len(content) > 20_000_000:
        raise IntegrationError("input_limit", "Document exceeds conversion limit")
    result = MarkItDown(enable_plugins=False).convert_stream(
        io.BytesIO(content), file_extension="." + fmt
    )
    text = result.text_content
    if len(text) > 4_000_000:
        raise IntegrationError("output_limit", "Converted document exceeds limit")
    return text, {
        "extractor": "markitdown",
        "version": version("markitdown"),
        "content_representation": "converted-markdown",
        "exact_source_spans": False,
        "title": result.title,
    }


def lighton_ocr(path, *, max_pages=20, device="cpu"):
    import torch
    import fitz
    from PIL import Image
    from transformers import LightOnOcrForConditionalGeneration, LightOnOcrProcessor
    from .models import model_path, pin

    name = "lightonai/LightOnOCR-2-1B"
    local = model_path(name)
    processor = LightOnOcrProcessor.from_pretrained(local, local_files_only=True)
    model = (
        LightOnOcrForConditionalGeneration.from_pretrained(local, local_files_only=True)
        .to(device)
        .eval()
    )
    pages = []
    with fitz.open(path) as document:
        if len(document) > max_pages:
            raise IntegrationError("page_limit", "PDF exceeds OCR page limit")
        for number, page in enumerate(document):
            scale = min(2.0, 1600 / max(page.rect.width, page.rect.height))
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            messages = [
                {"role": "user", "content": [{"type": "image", "image": image}]}
            ]
            inputs = processor.apply_chat_template(
                messages,
                add_generation_prompt=True,
                tokenize=True,
                return_dict=True,
                return_tensors="pt",
            ).to(device)
            with torch.inference_mode():
                output = model.generate(**inputs, max_new_tokens=4096)
            produced = output[0, inputs["input_ids"].shape[1] :]
            if len(produced) >= 4096:
                raise IntegrationError(
                    "truncated_ocr", "Page OCR hit the generation limit"
                )
            pages.append(
                {
                    "page": number + 1,
                    "text": processor.decode(produced, skip_special_tokens=True),
                }
            )
    return {
        "text": "\n\n".join(p["text"] for p in pages),
        "pages": pages,
        "version": pin(name)["revision"],
        "model": name,
        "exact_source_spans": False,
        "limitations": [
            "Generated OCR requires fidelity evaluation; page locators only."
        ],
    }
