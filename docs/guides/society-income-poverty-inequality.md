# Society income, poverty and inequality guide

Given a place and an indicator, which income, poverty and inequality figures
have the sources published - poverty headcounts at stated lines, Gini and other
inequality measures, income shares and medians - under which definitions and
release vintages, as of a date? The `society.income` provider of the new
`society` bundle (Society and population, ADR-005; #2583) answers from the World
Bank Poverty and Inequality Platform (PIP), Eurostat EU-SILC and the OECD Income
Distribution Database (IDD), side by side. Values and vintages live in the
Economics series storage (`economic_indicators`, `economic_series_map`,
`economic_vintages`, `dataset_observations`); `income_*` tables hold only the
income metadata. Existing Society-and-population providers (demographics,
labour, education, housing, justice statistics) keep their bundles and ids.

Exclusions: no nowcasting, no filled years, no poverty line of our own, no
blending of PIP, EU-SILC and OECD figures into one series, no re-harmonisation
of welfare concepts, equivalence scales or PPP rounds, no derived indicators,
and no person- or household-level data.

## Enable and acquire

Select the `society` bundle (`packs/society/manifest.json`). It binds
`society.income`, `economics.core`, `geospatial.core`,
`platform.subscriptions` and `platform.source-runtime`. The optional features
are `pip`, `eu-silc` and `oecd-idd` (default on, one per source; a refresh of a
deselected source is refused as `feature_not_selected`) and
`demographics-links` and `labour-links` (default off; they bind
`economics.demographics` and `economics.labour`). Acquisition runs through the
`society-statistics` source pack (`config/source_packs/society.json`, 1.0.0):
`worldbank-pip-poverty-inequality`, `eurostat-eu-silc-income` and
`oecd-income-distribution-database`. No source needs a key. Every provider is
`unverified-live` until a dated bounded run (IP13, #2648); see the
[source audit](../development/income-distribution-evidence/source-audit.md).

A later PIP release is acquired by pinning its version in the document's
`params.version`; each PIP release and each PPP revision is a separate vintage.

## Ask

- `income_indicator_for_place(namespace, place, concept?, provider?, as_of_ms?)`
  - `place` is a Geospatial place id (resolved through accepted mappings only)
  or a published `{scheme, code}` (`iso3166-1-alpha3`, `eurostat-geo`,
  `wb-region`). Each source's vintage released by the date is returned with
  its welfare concept, equivalence scale, poverty line, PPP base year,
  definition and citation per value. `groups` separate different lines, PPP
  rounds and welfare concepts; they are never combined. Pairs of series carry
  recorded differences and notes, or `comparability_unknown`.
- `income_series_history(namespace, series_id)` - every vintage with release
  dates, new, revised and removed reference years, PPP revisions and breaks.
- `export_income_evidence_bundle(...)` - the answer as a
  `noesis-evidence-bundle-v1`, every value cited with source, record revision
  and as-of time.
- `income_source_contracts`, `income_readiness`, `list_income_series`,
  `income_comparability_notes`, `list_income_identity_assertions`,
  `list_income_links`.

## Review and link

`propose_income_place_matches` offers each ISO, Eurostat GEO or World Bank
region code to Geospatial places by the published code (EU-SILC country codes
also as ISO alpha-2); ambiguous codes wait for a reviewer to choose a cited
candidate and unmatched codes stay visible. `propose_income_related_indicators`
records the same indicator from two sources for the same place as related
(shown beside, never merged). A reviewer other than the proposer accepts or
rejects with `review_income_identity_match`; `revert_income_identity_match`
undoes it. `link_income_records` links methodology references by exact URL,
named Demographics denominators and Labour series of the same place and period
(basis `citation`, `shared_identifier` or `accepted_match`, each pointing at
record revisions); missing providers and targets are reported.

## Monitor

`create_income_monitor` subscribes to a series, a place or an indicator concept
(optionally one provider) through `platform.subscriptions`; `run_income_monitor`
and `poll_income_monitor` return notices for new releases and reference years,
revisions, PPP revisions, definition changes and withdrawals, each citing the
new and the revised vintage. A replay or restart emits nothing.
