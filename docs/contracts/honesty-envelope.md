# Honesty envelope contract

Noesis treats provenance and uncertainty as part of an answer's schema, not as
optional prose. Tool and REST consumers should reject a response that claims
more than its envelope supports.

## Analytical outputs

Every successful analytical output has these top-level fields:

| Field | Rule |
| --- | --- |
| `n` | Non-negative integer: the number of documents, observations, windows, or resamples actually used. |
| `method` | Non-empty, specific method name. It identifies heuristic/model mode where applicable. |
| `assumptions` | Array of explicit caveats, including insufficient-data and independence assumptions. |
| headline estimate | `{value, lo, hi, level}`; never an unqualified point estimate. |

`src.analytics.honesty.validate_analytic_output` is the executable validator.
Where calibration residuals exist, intervals are split-conformal and the
response also reports calibration size and empirical coverage. An interval is
not described as calibrated when those receipts are absent.

## Evidence-bearing outputs

Every factual rendered line carries one or more evidence locators: a source
URL, a stable `document_id`, a source snapshot/revision, or a media timestamp.
If no locator exists, the line remains visible and is marked `uncited`; it is
never silently discarded. Model-generated records carry `prediction_mode` and
confidence. Heuristic results never masquerade as pretrained results.

The integrity ledger records snapshot and refetch state, corrections, silent
edits with both versions, image reuse/C2PA status, and cross-modal checks. An
unavailable check is reported as `not_checked` or `unverifiable`, not as clean.

## Error and refusal envelopes

Expected failures are data. A tool returns a stable error/refusal object with a
machine-readable status, human explanation, and the missing precondition. It
must not invent an empty success. Examples include:

- quantitative claim with no matching series: `unverifiable`;
- person dossier below the minimum evidence threshold: `refused`;
- absent required model: an explicit unavailable error naming the missing model precondition;
- missing table or offline source: explicit unavailable/empty-state metadata.

Error responses are exempt from numerical interval fields because they make no
estimate. They are not exempt from being explicit.

## MCP compliance audit

The schema gate currently covers these inferential tools:

- `argument_mcp`: `score_confidence`, `stance_significance`;
- `kg_mcp`: `kg_communities`, `kg_centrality`;
- `pipeline_mcp`: `detect_anomalies`, `lead_lag`, `cluster_narratives`,
  `semantic_drift`, `forecast_topic`, `speaker_balance`;
- `research_mcp`: `venues`;
- `osint_mcp`: `corroborate`, `source_reliability`, `image_reuse_findings`,
  `image_reuse`.

Several older warehouse-summary tools (`sentiment_by_topic`,
`sentiment_heatmap`, `coverage_clusters`, `geo_map`, `topic_model`, and the
statistics ledger/explorer views) expose descriptive rows and counts rather
than an inferential headline estimate. They are discovery-tested and
read-only-tested, but are deliberately not labelled interval-compliant. If one
gains a forecast, score, or population-level estimate, its MCP schema must move
under `honesty_output_schema()` in the same change.

| Server family | Required discipline | Executable check |
| --- | --- | --- |
| statistics, pipeline analytics, research analytics | sample/method/assumptions plus intervals | `tests/unit/analytics/test_tool_contracts.py` and MCP harness |
| KB, argument, sources | citations and prediction mode on generated assertions | KB contract and brief tests |
| OSINT | per-line evidence, refusal threshold, gated sensitive tools | OSINT evidence/gate tests and MCP harness |
| KG | read-only standalone access; provenance on triples | persistent-store subprocess tests |
| schema, contract, monitoring, security | explicit error/empty envelopes; no fabricated values | direct MCP harness |

The audit inventory is generated from every `tools/*_mcp/server.py` server, so
adding a server or tool without discovery metadata makes CI fail.
