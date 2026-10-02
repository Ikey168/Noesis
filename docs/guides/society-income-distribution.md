# Society: income, poverty and inequality

The Society and population bundle (`packs/society/`, ADR-005) starts with one
provider, `society.income` (#2583). It answers: *given a country or region and
an indicator, what poverty headcounts, Gini coefficients, income shares and
medians did each source publish, at which poverty line and PPP round, under
which welfare concept and definition, in which release, as of a date?*

It quotes what each source released. It never nowcasts, fills a year a source
did not publish, sets a poverty line of its own, blends PIP, EU-SILC and OECD
figures into one series or re-harmonises welfare concepts, equivalence scales
or income definitions, and it derives no indicator.

## Sources

Four sources in the `society-income-distribution` source pack (1.0.0,
`config/source_packs/society.json`), connector `income-distribution`, all
`unverified-live` until the dated live run (IP13, #2648). Access, terms, rate
limits, the revision model, the minimisation decision and the bounded coverage
are in the [source audit](../development/income-distribution-evidence/source-audit.md);
it was written without network access, so terms were not re-verified live.

| Source | Publisher | Units | Feature |
| --- | --- | --- | --- |
| `pip-country-estimates` | World Bank PIP API | country, poverty line, PPP version and release version per document | `pip` |
| `pip-regional-aggregates` | World Bank PIP API (`pip-grp`) | region, line, PPP version, release version | `pip` |
| `eurostat-silc-income-poverty` | Eurostat EU-SILC (SDMX-CSV) | dataset and series key | `eu-silc` |
| `oecd-idd-income-distribution` | OECD IDD (SDMX-CSV) | dataflow and series key | `oecd-idd` |

The three features are on by default and can be deselected independently;
answers list a deselected source under `features_disabled`.

## Records and vintages

`noesis-income-distribution-record-v2` (`src/kb/income_distribution_records.py`,
`src/kb/income_distribution_store.py`). A **series** is keyed by source,
indicator concept and native measure, welfare concept (income, consumption,
or PIP's `mixed` regional aggregate), equivalence scale, poverty line with its
PPP base year, place, coverage, survey, income definition and methodology.
Reference years are the observations; each keeps its value text as published,
status, flags (EU-SILC `OBS_FLAG`, OECD `OBS_STATUS`), PIP's estimation type
(`survey`, `interpolation`, `extrapolation`, `regional-line-up`) and the survey
and income reference years.

Each release that changes a series is an appended **vintage** with release
and retrieval clocks. A PIP release restating values under a new PPP revision
is a vintage with `ppp_revision` recorded; a new PPP round is a different
series. A series a complete later release no longer states gets a
`removed_by_source` vintage; nothing is deleted or overwritten. Values also
live in the Economics series storage (`economic_vintages`,
`dataset_observations`, domain `society`).

## Answers

- `income_indicator_for_place` - each source's figure for a place as released
  by the date, side by side with definitions and cited vintages, grouped by
  comparability; `combined_value` is always empty.
- `income_series_history` - every vintage with changed, new and dropped
  periods, PPP revisions, definition changes and removals; consecutive pairs
  without a note are `comparability_unknown`.
- `income_profile` / `export_income_profile` - poverty headcount and Gini for a
  place, and the same as a `noesis-evidence-bundle-v1` citing source, record
  revision (vintage) and as-of time for every figure.

## Identity, links and monitoring

- `propose_income_place_matches` matches area codes (ISO alpha-3, Eurostat
  GEO and NUTS, World Bank regions) to Geospatial places by published
  identifier only; another principal accepts or rejects; reverts are recorded.
  Unmatched areas stay visible. `propose_related_income_indicators` records the
  same concept across sources as related, never merged.
- `link_income_series` links methodology documents by exact URL, Demographics
  population denominators and Labour series for the same place by shared
  identifier or accepted match, pinning both revisions. When Demographics or
  Labour is absent, the link is `provider_absent`.
- `create_income_monitor` / `run_income_monitor` notify new releases, new
  periods, revised values, PPP revisions, definition changes and removals,
  through `platform.subscriptions`, citing the vintages before and after.

## Data minimisation

Aggregate statistics only. Person- and household-level fields are refused at
write time; microdata (EU-SILC UDB, PIP survey microdata) are excluded. Reads
need `knowledge:income:read` and namespace access.

## Evidence

Offline: `tests/unit/domains/test_income_distribution_*.py`, the acceptance
journey `test_income_distribution_acceptance.py` and
`tests/unit/composition/test_society_income_composition.py`. Live: none yet
(IP13, #2648). Offline coverage is not live coverage.
