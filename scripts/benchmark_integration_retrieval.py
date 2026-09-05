"""Real local embedding comparison on an explicitly synthetic German/English probe."""

import argparse
import json
from pathlib import Path
import platform
import resource
import statistics
import time


def main():
    import torch
    import numpy as np
    from services.embeddings.backends.local_sentence_transformers import (
        LocalSentenceTransformersBackend,
    )
    from src.integrations.models import pin

    torch.set_num_threads(2)
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    docs = [
        (
            "berlin",
            "Das Berliner Förderprogramm unterstützt die energetische Sanierung von Wohngebäuden.",
        ),
        (
            "trial",
            "The clinical trial compares two treatments for diabetes in adult patients.",
        ),
        (
            "climate",
            "Die Studie untersucht den Zusammenhang zwischen CO2-Emissionen und globaler Erwärmung.",
        ),
        (
            "rail",
            "The railway timetable lists train departures from Berlin to Hamburg.",
        ),
    ]
    queries = [
        (
            "Welche Förderung gibt es für die Sanierung von Wohnungen?",
            "berlin",
            "de-de",
        ),
        ("Welche Studie vergleicht Diabetesbehandlungen?", "trial", "de-en"),
        ("What does the study say about emissions and warming?", "climate", "en-de"),
        ("When do trains depart for Hamburg?", "rail", "en-en"),
    ]
    output = {
        "fixture_kind": "synthetic engineering probe; not independent human evaluation",
        "hardware": platform.platform(),
        "threads": 2,
        "documents": docs,
        "queries": queries,
        "runs": [],
    }
    for name in [
        "sentence-transformers/all-MiniLM-L6-v2",
        "intfloat/multilingual-e5-small",
    ]:
        extra = (
            {"revision": pin(name)["revision"]} if name.startswith("intfloat/") else {}
        )
        started = time.perf_counter()
        model = LocalSentenceTransformersBackend(
            name, device="cpu", local_files_only=True, **extra
        )
        load = time.perf_counter() - started
        started = time.perf_counter()
        vectors = model.embed_texts([d[1] for d in docs])
        index_time = time.perf_counter() - started
        latency = []
        ranks = []
        for query, expected, kind in queries:
            for _ in range(5):
                t = time.perf_counter()
                q = model.embed_queries([query])[0]
                latency.append((time.perf_counter() - t) * 1000)
            order = np.argsort(-(vectors @ q))
            rank = [docs[i][0] for i in order].index(expected) + 1
            ranks.append({"language_pair": kind, "expected_rank": rank})
        output["runs"].append(
            {
                "model": name,
                "revision": model.tokenizer_identity()["revision"],
                "dimensions": model.dim(),
                "input_policy": model.input_policy,
                "load_seconds": load,
                "index_documents_per_second": len(docs) / index_time,
                "query_p50_ms": statistics.median(latency),
                "query_p95_ms": float(np.percentile(latency, 95)),
                "recall_at_1": sum(r["expected_rank"] == 1 for r in ranks) / len(ranks),
                "ranks": ranks,
            }
        )
        del model
    output["peak_process_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    output["decision"] = (
        "defer default change pending independent corpus, larger workload and migration evaluation"
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(output["runs"], indent=2))


if __name__ == "__main__":
    main()
