# Binary forecasts

The knowledge-engine MCP server exposes `create_binary_forecast`,
`inspect_binary_forecast`, `revise_binary_forecast`, `propose_forecast_resolution`,
`resolve_binary_forecast`, and `score_binary_forecasts`. Each call checks current
ownership, `knowledge:forecasts:read` or `knowledge:forecasts:write`, and namespace
access, including evidence namespaces. An operator can administer forecasts.

Registration requires an explicit probability between zero and one, question,
outcome rule, future resolution timestamp in milliseconds, and evidence references.
Each reference carries `kind` (source, event, observation, snapshot), stable `id`,
string `revision`, and `namespace`. No hypothesis comparison score is converted
into a probability. The system records the forecaster and recording time.

Updates require the current revision and a rationale. They append probability
and rule revisions; they cannot backdate a forecast or change its deadline.
Rules/probabilities freeze at the resolution deadline. Inspection with `cutoff_ms`
selects only information recorded by that time. `outcome_cutoff_ms` independently
selects the resolution history, allowing a later correction to be compared with
the originally reviewed outcome.

## Resolution

Optional `resolution_match` registers an exact quantitative rule:

```json
{
  "namespace": "economics", "metric_id": "metric:investment",
  "provider": "official-statistics", "provider_series_id": "investment-series",
  "period": "2026-Q3", "unit_id": "unit:percent",
  "comparison": "gt", "threshold": "2.0"
}
```

Supported comparisons are gt, gte, lt, lte, and eq with decimal thresholds.
Matching uses the latest release at or before the registered resolution deadline
for that exact series, period, provider, and unit. Missing, preliminary, unsourced,
or conflicting observations do not settle the outcome. Proposals retain observation
IDs/vintages and always require review. No unit conversion or vague event matching
is inferred. Other rules, including event references, require manual review.
Rule changes are visible revisions; pass an empty matching object when revising to
disable automatic quantitative proposals.

Reviewers submit the current forecast revision and expected outcome revision.
Outcomes can be unresolved, disputed, cancelled, or resolved. Only resolved
outcomes carry a binary value, require source references, and must be recorded
after the deadline. Later corrections append outcome revisions and retain the
reviewer's rationale. Reference identity is preserved; an explicit review is not
proof that an external source is accurate.

## Evaluation

Supply an explicit cohort of forecast IDs and cutoff. The scorer uses the latest
probability recorded by the earlier of the cutoff and the instant before each
deadline. It excludes unresolved, disputed, cancelled, absent-at-cutoff forecasts,
and forecasts whose outcome rule changed after the cutoff. It reports exclusions,
sample sizes, mean Brier score, a fixed 0.5-probability baseline, and ten reliability
bins with Wilson 95% intervals for outcome frequencies. Default outcome selection
uses the latest reviewed correction; supply an outcome cutoff to reproduce an
earlier assessment. Caller-selected cohorts and missing resolutions can bias
results, and correlated forecasts weaken the independence assumption behind the
intervals. No forecaster ranking is produced from these samples.
