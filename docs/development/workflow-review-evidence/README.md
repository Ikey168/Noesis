# Review reproductions

These diagnostic scripts accompany the [workflow review](../workflow-improvement-review.md) at commit `0bf70327` on 2026-09-05. They exercise existing code and report observed behavior; they are not regression tests asserting desired behavior.

Run from the repository root with its development environment:

```bash
PYTHONPATH=. .venv/bin/python docs/development/workflow-review-evidence/core_probes.py
PYTHONPATH=. .venv/bin/python docs/development/workflow-review-evidence/additional_probes.py
```

`results.json` captures the twelve observations from this review. Database identifiers, hashes involving generated metadata, and wall-clock timings may differ on subsequent runs. Each scenario catches and reports setup/runtime errors so that an unavailable dependency cannot be mistaken for a successful reproduction.

The probes use in-memory databases and temporary local paths. Embedding/enrichment providers are deterministic injected implementations. The mining scenario injects a model failure; it does not perform inference. The workflow scenario injects failure immediately before watermark commit and uses pass-through stage handlers to isolate the state machine. The package scenario deliberately recalculates an unsigned package's outer digest after removing a dependency, testing structural completeness rather than cryptographic forgery.

The chunking probe loads `services/rag/chunking.py` directly because the package's eager imports require `psycopg2`, which was absent in the review environment. The underlying chunker implementation is unchanged.
