# Optional Pandera dataset validation (#1516)

Install `.[dataset-validation]` for Pandera **0.33.1** and its pinned pandas
backend. The adapter is an evaluation path; native `DatasetIntelligenceStore`
ingestion remains the production writer and keeps its release, quarantine and
replay contracts.

```python
from src.kb.pandera_evaluation import validate_release

receipt = validate_release(
    store, "economic", release_id, table_id,
    content=csv_bytes.decode("utf-8"), format="csv", delimiter=";",
    coercion="de-DE",
    constraints={
        "version": "berlin-v1",
        "ranges": {"value": {"min": 0, "max": 100}},
        "unique": ["geo"],
        "cross_fields": [],
    },
    principal_id=principal_id, scopes=current_scopes,
)
```

The candidate resolves the immutable Noesis release and table schema first. It
supports bounded CSV and base64 Parquet inputs, preserves the original payload
bytes and SHA-256, and records the dataset revision, release hash, provenance,
Pandera version, constraint version, coercion policy and format in the receipt.
Inputs are capped at 2 MB, 10,000 rows, 100 columns and 50 MB expanded Parquet;
errors/coercion entries are capped at 1,000. CSV accepts comma, semicolon and
tab delimiters. German decimal mode accepts values such as `1.234,5` and records
the conversion separately from validation failure. `none` disables conversion;
`native` uses the existing Noesis scalar conversion rules.

Checks cover declared types, nullability, per-column ranges, individually unique
columns, composite primary keys, and bounded cross-field `le`, `lt` and `eq`
constraints. Date fields use ISO dates. Each failure reports row index, CSV line
when applicable, column, phase (`coercion` or `validation`), check name and a
bounded original-value preview. Missing nullable values remain explicit nulls;
they are not silently converted to zero. A rejected result is a validation report,
not a quarantine mutation. Native ingestion remains responsible for its existing
quarantine/rejection decisions, row locators and durable dataset rows.

Validation requests are idempotent by namespace, release/table identity,
configuration and original content hash. Reopening a database returns the stored
receipt and verifies retained original bytes. Changed request parameters receive
a new validation identity; changed retained bytes fail replay. Authorization
requires dataset read plus an authenticated namespace writer or operator. There
are no network calls, model downloads or credentials. Pandera is imported only
when this optional adapter is called.

## Reproduction and measured result

```sh
.venv/bin/python -m pytest tests/unit/kb/test_pandera_evaluation.py tests/unit/kb/test_dataset_intelligence.py -q --override-ini addopts=''
.venv/bin/python scripts/evaluate_pandera.py --out docs/development/workflow-implementation-evidence/pandera-evaluation.json
```

The focused adapter suite passed **8 tests**. The native dataset suite remains
green. Tests cover German decimals, date parsing, missing nullable values,
ranges, individual/composite uniqueness, cross-field bounds, schema drift,
Parquet, original bytes, replay, limits and authorization. Fixtures represent
German/EU-shaped data but are synthetic contract fixtures; they are not human
quality labels or independent statistical ground truth.

The bounded 1,000-row Berlin-shaped CSV benchmark measured native ingestion at
**396.9 ms** and Pandera validation at **168.2 ms** on Python 3.14.6, with peak
process RSS **203592 KiB**. The candidate produced zero errors and 1,000 recorded
coercions; native ingestion inserted 1,000 rows. Timing includes DuckDB receipt
work and is a single-run comparison, not a throughput guarantee. Pandera's
vectorized checks are useful for richer reports, but the adapter retains a
separate receipt table and DataFrame dependency; it does not implement native
quarantine or row persistence.

Decision: **defer production replacement; retain as an optional evaluation
adapter**. It meets the contract and gives actionable validation locators while
preserving original values and provenance. More representative Parquet batches,
memory profiling and operational policy calibration are needed before changing
the native ingestion default. See [measured evidence](../development/workflow-implementation-evidence/pandera-evaluation.json),
[Pandera schemas](https://pandera.readthedocs.io/en/stable/dataframe_schemas.html),
and [Pandera checks](https://pandera.readthedocs.io/en/stable/checks.html).
