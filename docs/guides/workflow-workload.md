# Bounded workflow workload

Run `python scripts/benchmark_workflow.py --out workload.json` with the cached production extraction and embedding models. The fixed manifest uses batches of 2, 8 and 16 documents drawn from the pinned Federal Reserve/NASA text, two domains, two concurrent query workers and 32 semantic queries requesting up to 25 results. Original text lengths and hashes, model pins, host CPU/platform and configuration accompany the measurements.

The recorded run in `docs/development/workflow-implementation-evidence/workflow-workload.json` met its declared small-workload targets:

| Measurement | Observed | Target |
|---|---:|---:|
| Query p95 | 82.74 ms | <2,000 ms |
| Query maximum | 85.53 ms | — |
| Warm ingestion/extraction/publication | 3.65 documents/s | ≥1 document/s |
| Peak parent-process RSS | 1,470 MiB | <4,096 MiB |
| Query failures | 0 / 32 | 0 |
| Noncooperative provider admissions | 8 | ≤8 |

The run mixes actual local extraction/embedding work, committed maintenance generations, semantic reads and saved-query/export workflow stages. Its first batch resumes after an injected stage interruption. A separate process reopens the final warehouse and replays the last workflow without creating another stage receipt. Per-batch publication lag and subscription events are in the report. External inference charges are zero; local compute cost and token use are not metered.

Fault timings are explicitly separate: ten controlled slow-provider requests produced eight approximately 40 ms deadline returns and two immediate `source_busy` responses. Cancellation and partial failure also produced explicit outcomes. These fixtures establish admission/deadline behavior, not live-provider throughput.

The targets are engineering guards for this small developer workload: they allow interactive reads within the configured two-second budget and bounded memory/concurrency during maintenance. The repeated two-source corpus benefits from caches and is not a capacity prediction for heterogeneous production data. The measured bottleneck is extraction/publication relative to semantic reads. The host is recorded, but the benchmark does not reserve CPU resources or certify an isolated machine. Larger corpora, remote services and independently judged answer quality require separate measurements.
