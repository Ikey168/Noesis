"""Real pinned SaT ONNX probe preserving Unicode input offsets."""

import argparse
import json
import time
from pathlib import Path


def main():
    from huggingface_hub import snapshot_download

    from src.integrations.models import pin
    from src.integrations.text import SaTSegmenter

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    model, tokenizer = "segment-any-text/sat-3l-sm", "FacebookAI/xlm-roberta-base"
    model_path = snapshot_download(
        model,
        revision=pin(model)["revision"],
        local_files_only=True,
        allow_patterns=["config.json", "model_optimized.onnx"],
    )
    tokenizer_path = snapshot_download(
        tokenizer,
        revision=pin(tokenizer)["revision"],
        local_files_only=True,
        allow_patterns=[
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "sentencepiece.bpe.model",
        ],
    )
    segment = SaTSegmenter(model_path, tokenizer_path=tokenizer_path, use_onnx=True)
    samples = [
        "Dr. Müller wohnt in Berlin. Er arbeitet z. B. an EU-Projekten.\n\nThis is English. It has two sentences.",
        "Die Kosten betragen 1,5 Mio. Euro.\n§ 3 gilt ab dem 1. Januar.\n\nThe report cites Art. 5 of the regulation.",
    ]
    results = []
    for text in samples:
        started = time.perf_counter()
        spans = segment(text)
        results.append(
            {
                "text": text,
                "latency_ms": (time.perf_counter() - started) * 1000,
                "spans": [
                    {"start": start, "end": end, "text": text[start:end]}
                    for start, end in spans
                ],
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "fixture_kind": "synthetic engineering probe; not independent human evaluation",
                "model": model,
                "model_revision": pin(model)["revision"],
                "tokenizer": tokenizer,
                "tokenizer_revision": pin(tokenizer)["revision"],
                "backend": "wtpsplit ONNX CPU",
                "results": results,
                "adoption": "defer; independent sentence-boundary benchmark outstanding",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
