"""Pinned Qwen scoring smoke probe; synthetic relevance labels, no adoption claim."""

import argparse
import json
from pathlib import Path
import platform
import resource
import statistics
import time


def main():
    import torch
    from src.integrations.models import QwenReranker, pin
    from services.rag.rerank import CrossEncoderReranker

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    torch.set_num_threads(2)
    started = time.perf_counter()
    scorer = QwenReranker(device="cpu", batch_size=2, max_tokens=512)
    load_ms = (time.perf_counter() - started) * 1000
    cases = [
        (
            "Welche Förderung unterstützt die Gebäudesanierung?",
            [
                "Das Förderprogramm unterstützt die energetische Sanierung von Wohngebäuden.",
                "Der Fußballverein gewann sein Heimspiel.",
            ],
        ),
        (
            "What supports renovation of residential buildings?",
            [
                "Das Förderprogramm unterstützt die energetische Sanierung von Wohngebäuden.",
                "Der Fußballverein gewann sein Heimspiel.",
            ],
        ),
        (
            "Welche Studie untersucht die Behandlung von Diabetes?",
            [
                "The clinical trial compares two diabetes treatments in adult patients.",
                "Train departures are listed in the railway timetable.",
            ],
        ),
        (
            "What does the diabetes trial compare?",
            [
                "The clinical trial compares two diabetes treatments in adult patients.",
                "Train departures are listed in the railway timetable.",
            ],
        ),
    ]
    runs, timings = [], []
    adapter = CrossEncoderReranker(model_name=scorer.MODEL, scorer=scorer)
    for query, passages in cases:
        started = time.perf_counter()
        results = adapter.rerank(
            query,
            [{"content": p} for p in passages],
            score_fusion="rerank_only",
            require_model=True,
        )
        scores = [
            r.rerank_score for r in sorted(results, key=lambda r: r.original_index)
        ]
        timings.append((time.perf_counter() - started) * 1000)
        runs.append(
            {
                "query": query,
                "passages": passages,
                "scores": scores,
                "expected_preference": 0,
                "preferred": max(range(len(scores)), key=scores.__getitem__),
            }
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "fixture_kind": "synthetic engineering probe; not independent human evaluation",
                "model": scorer.MODEL,
                "revision": pin(scorer.MODEL)["revision"],
                "hardware": platform.platform(),
                "threads": 2,
                "load_ms": load_ms,
                "batch_latency_p50_ms": statistics.median(timings),
                "batch_latencies_ms": timings,
                "process_peak_rss_kib": resource.getrusage(
                    resource.RUSAGE_SELF
                ).ru_maxrss,
                "runs": runs,
                "adoption": "defer; independent corpus, baseline comparison and calibration outstanding",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
