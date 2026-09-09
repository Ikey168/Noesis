# Optional ADWIN operational drift evaluation (#1522)

Install `.[drift-evaluation]` for River **0.26.1** (BSD-3-Clause). No model download,
external service or API credentials are needed. The candidate remains optional;
existing anomaly watches keep their current detectors.

```python
from src.kb.adwin_evaluation import ADWINStream

stream = ADWINStream(conn, "operations", "berlin-extractor-v1",
                     principal_id=principal, scopes=current_scopes)
receipt = stream.consume("batch-1", [{
    "event_id": "run-123", "sequence": 1, "observed_at_ms": observed_at,
    "value": 0, "evidence_id": "extraction-run:123",
}], principal_id=principal, scopes=current_scopes)
```

Streams accept measured extraction failure (`0` success, `1` failure) or labelled
prediction error. Prediction errors additionally require
`label_origin="independent-human"` and a label evidence ID; the caller is
responsible for resolving that evidence to real independent annotations. Model
confidence is neither a binary error nor independently measured accuracy, and
is rejected as a substitute. These signals remain **operational** and are not
domain-event watches or changes in the underlying research subject.

Events require an immutable ID, consecutive positive sequence, nondecreasing
observation time and evidence ID. An unavailable sample is an explicit null with
a missing reason; it advances the watermark but is excluded from the detector's
sample count. Gaps, unseen late events and changed duplicate payloads reject the
whole batch. Exact duplicates are ignored. Reusing a request ID with different
input/principal is rejected. Current execute and namespace write authorization
is checked before receipt replay.

Configuration is immutable: delta defaults to 0.002, clock 32, five buckets,
minimum subwindow five and grace period 32. Delta is a statistical sensitivity
parameter, not a calibrated probability that any particular alert is false.
Stationarity/mean-change assumptions and correlated failures matter. Define a
separate stream per stable provider/extractor/model configuration; planned code
changes are not unexplained accuracy drift. Smaller delta generally delays
detection; this evaluation does not select production thresholds for each source.

State retains the ordered input log, configuration/version, sample/missing counts,
watermark and detector summary. It rebuilds the pinned detector from that bounded
log and verifies the summary on resume, avoiding opaque executable pickle state.
One batch is at most 1000 events/1 MB; default lifetime retention is 20000 events,
with a hard configurable maximum of 50000. Resume is O(retained events). Capacity
exhaustion is explicit; there is no silent truncation or hidden recalibration.
Use the normal serialized DuckDB writer for concurrent producers.

Each detection publishes through `KnowledgeAnomalyStore.run` and its existing
anomaly/run schemas. The score is an explicit binary change flag, not a z-score
or accuracy estimate; the baseline records the method, sample size, mean,
variance, version and triggering evidence. Existing notification grouping and
delivery deduplication remain usable. This adapter does not automatically send
notifications. State and anomaly publications commit in one transaction.

## Evaluation

```sh
.venv/bin/python -m pytest tests/unit/kb/test_adwin_evaluation.py tests/unit/kb/test_knowledge_anomalies.py -q --override-ini addopts=''
.venv/bin/python scripts/evaluate_adwin.py --out docs/development/workflow-implementation-evidence/adwin-evaluation.json
```

The new three tests cover restart versus one-batch equivalence, actual shift
detection, public anomaly schema validation, stored alert deduplication, missing
samples, late/gapped/conflicting events, capacity, immutable configuration,
unlabelled quality rejection, revoked access and detector replay integrity.

Three seeded streams contain 2048 binary outcomes each. The stationary failure
probability is 0.05; the abrupt stream changes to 0.6 at sample index 1024; the
gradual stream reaches 0.6 over the following 512 samples. The current baseline
uses its actual 64-sample rolling z-score API, minimum 32 samples, threshold 3.

| Case | Native stationary alerts | ADWIN stationary alerts | First change delay: native / ADWIN |
|---|---:|---:|---:|
| Stationary | 106 | 0 | No distribution change |
| Abrupt | 52 | 0 | 0 / 31 samples |
| Gradual | 46 | 0 | 37 / 159 samples |

Native thresholds flag unusual individual failures; ADWIN tests changes in the
mean. Thus stationary alerts count as false *drift* alerts for this comparison,
not necessarily incorrect native anomaly findings. These are seeded synthetic
results, not deployment false-positive guarantees. Raw candidate processing
took 0.27–0.30 ms per stream; its persisted adapter took 30–39 ms. The native API
loop took 1.37–1.42 seconds, including thousands of database-backed baseline
lookups; this is an interface measurement, not a like-for-like algorithm speedup.
Whole-process peak RSS was 269796 KiB.

The evaluation also reads two actual retained Wikidata acquisition outcomes,
with the evidence file's SHA-256. Both detectors emitted no alert. Two outcomes
spanning a known code fix are insufficient for calibration or a stationarity
claim. The script labels this limitation explicitly.

Decision: **defer default adoption**. Retain the optional adapter and reproduction
artifacts; obtain adequate ordered history for each stable operational stream
before enabling alerts. The synthetic checks establish correct plumbing and
expected mean-change behavior, not independently measured model-quality drift.
See [measured results](../development/workflow-implementation-evidence/adwin-evaluation.json)
and [River ADWIN documentation](https://riverml.xyz/latest/api/drift/ADWIN/).
