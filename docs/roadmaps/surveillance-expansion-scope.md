# Public-health surveillance expansion scope

Status: planned, 2026-09-27. An expansion of the existing Clinical Evidence
bundle; it does not create a separate domain or pack. It produces no outbreak
prediction, no health advice and no nowcast of its own; a case-definition
change is shown as a break in the series, never silently corrected.

## Delivery state (audited 2026-09-27)

- Shipped: nothing yet. Planning issues #1929–#2034 are open.
- Composition dependency: a `clinical.surveillance` provider descriptor and
  optional `surveillance` feature (default false) in the Clinical Evidence
  bundle (`packs/clinical-evidence/`), composed with `clinical.terms`,
  `clinical.monitoring`, `clinical.publication-links`,
  `science.literature-claims`, `geospatial.feature-query`,
  `geospatial.place-resolution`, `platform.subscriptions` and
  `platform.source-acquisition`, and calling the vintage comparison of
  `environment.vintages`.

Tracking: [#1917](https://github.com/Ikey168/Noesis/issues/1917).

## Outcome

Given a condition and a geography, assemble notifiable-disease and
health-indicator series with their case definitions and definition revisions,
reporting date against reference date, reporting-delay notes and published
vintages. Align conditions to MeSH and ICD through the existing clinical term
crosswalks, project regional series onto Geospatial boundaries, and link series
to trials and publications only where a record explicitly cites the dataset.
Observation, estimate and model output stay distinguished; every value cites
the source revision and vintage it came from; two sources for the same period
stay side by side.

## GitHub implementation issues

- [ ] [#1929](https://github.com/Ikey168/Noesis/issues/1929) — I01 Audit
  public-health surveillance source contracts and select bounded provider
  coverage.
- [ ] [#1940](https://github.com/Ikey168/Noesis/issues/1940) — I02 Define
  surveillance-series, case-definition-revision, reporting-delay and
  indicator-vintage records.
- [ ] [#1949](https://github.com/Ikey168/Noesis/issues/1949) — I03 Acquire RKI
  open-data series with reporting and reference dates and case-definition
  versions.
- [ ] [#1961](https://github.com/Ikey168/Noesis/issues/1961) — I04 Acquire ECDC
  Surveillance Atlas and WHO GHO indicators with their metadata.
- [ ] [#1971](https://github.com/Ikey168/Noesis/issues/1971) — I05 Acquire
  Eurostat health series through the existing SDMX connector with vintages.
- [ ] [#1981](https://github.com/Ikey168/Noesis/issues/1981) — I06 Align
  conditions to MeSH and ICD through the existing clinical term crosswalks.
- [ ] [#1992](https://github.com/Ikey168/Noesis/issues/1992) — I07 Project
  regional series onto Geospatial boundaries and answer as-of queries.
- [ ] [#2002](https://github.com/Ikey168/Noesis/issues/2002) — I08 Compare
  vintages and show reporting-delay effects with cited sources and no
  nowcasting.
- [ ] [#2010](https://github.com/Ikey168/Noesis/issues/2010) — I09 Link series
  to trials and publications through the clinical providers by explicit
  citation only.
- [ ] [#2019](https://github.com/Ikey168/Noesis/issues/2019) — I10 Monitor
  threshold exceedances and case-definition changes through subscriptions.
- [ ] [#2027](https://github.com/Ikey168/Noesis/issues/2027) — I11 Register the
  `clinical.surveillance` provider descriptor and optional feature in the
  Clinical Evidence bundle.
- [ ] [#2031](https://github.com/Ikey168/Noesis/issues/2031) — I12 Add offline
  condition-to-series-and-definitions acceptance coverage.
- [ ] [#2034](https://github.com/Ikey168/Noesis/issues/2034) — I13 Validate
  live coverage and publish a cited surveillance dossier demo.

Order: I01 → I02 → {I03, I04, I05} → I06 → I07 → I08 → I09 → I10 → I11 →
I12 → I13. Each issue contains its acceptance criteria and prerequisite
references.

## Sources

- [RKI open data on GitHub](https://github.com/robert-koch-institut):
  versioned notifiable-disease datasets with reporting and reference dates;
  each release is a native revision.
- [SurvStat@RKI](https://survstat.rki.de/): query interface for notifiable
  diseases; machine access to exports is to be verified, not assumed.
- [ECDC Surveillance Atlas](https://atlas.ecdc.europa.eu/public/index.aspx):
  disease and indicator series per country with extraction dates; the access
  path is to be verified.
- [WHO GHO OData API](https://www.who.int/data/gho/info/gho-odata-api):
  indicator values, many of them estimates with bounds, which stay estimates.
- [Eurostat health database](https://ec.europa.eu/eurostat/web/health/database):
  `hlth_*` datasets through the existing SDMX connector and the
  `eurostat-dissemination` endpoint already declared for economics.
- [Destatis](https://www.destatis.de/): German health statistics; the
  documented machine-access path, if any, decides implemented or
  `not-implemented`.

I01 verifies the documented access method, terms, authentication, rate
limits, identifiers, cadence and definition publication per provider and
records an `unverified-live` or `not-implemented` decision for each. These
links establish source candidates, not guaranteed APIs or complete live
coverage.

## Expansion of existing capabilities

**Clinical Evidence (host):** the `clinical-evidence` source pack
(`config/source_packs/clinical-evidence.json`) gains surveillance source
entries, `src/ingestion/clinical_providers.py` gains their provider contracts,
and `src/kb/surveillance.py` becomes the record owner for series, case
definitions and their revisions, reporting-delay notes and indicator vintages.
Reporting date and reference date are separate fields on every value. The
provider descriptor `packs/clinical-evidence/providers/clinical.surveillance.json`
and the optional `surveillance` feature in `packs/clinical-evidence/manifest.json`
are off by default; with the feature off, the Clinical Evidence and Science
bindings are unchanged.

**Clinical terms (`clinical.terms`):** source-native condition terms are
published as registry-term modules through `ClinicalTerms`
(`src/kb/clinical_terms.py`) and cross-walked to the existing `clinical-mesh`
module in `src/kb/ontology.py`. An ICD module is added beside it in the same
`OntologyAlignmentStore`. Crosswalk kinds `equivalent`, `broader`, `narrower`,
`related` and `incompatible` are preserved, expansion steps are explained, and
unmapped terms are listed as gaps. A definition revision that changes a
condition's ICD scope is recorded on the revision, not by re-labelling earlier
values.

**Clinical monitoring (`clinical.monitoring`):** surveillance monitors follow
the `ClinicalMonitor` pattern (`src/kb/clinical_monitoring.py`) as knowledge
subscriptions over one series or one condition-within-boundary query.
Thresholds are user-configured values with pint-checked units; the pack never
derives or adjusts a threshold. Case-definition changes, new vintages, value
revisions, geography breaks and stale sources are events of their own.
Refreshes run only through the source pack's runtime schedule.

**Clinical publication links (`clinical.publication-links`) and trials:** a
series links to a publication or trial only through `PublicationLinker`
(`src/kb/clinical_publications.py`) from an explicit dataset citation (DOI,
accession, release tag, indicator or dataset code) or a registry-declared
reference. Textual mentions are review candidates. Series without links and
cited datasets not held as series are coverage gaps.

**Science literature claims (`science.literature-claims`):** claims about a
series appear beside it as a separate view with their document revision cited;
the expansion draws no conclusion from them.

**Geospatial (`geospatial.feature-query`, `geospatial.place-resolution`):**
series geography codes (NUTS, AGS, ISO) resolve to boundary features in the
existing `GeospatialFeatureStore` (`src/kb/geospatial_features.py`) through
reviewable place resolutions in `src/kb/geospatial.py`. Boundary revisions are
geography breaks on the series; the pack never re-aggregates. As-of queries
select by reporting date without interpolation. No spatial store, geometry
table or coordinate transform is added.

**Environment vintages (`environment.vintages`):** vintage comparison and
pin-status for surveillance series call `src/kb/environment_vintages.py` (or a
shared helper both modules call); the diff logic is not copied. Every changed
period cites both vintages, and a pinned view reports `stale` rather than
following a newer vintage silently.

**Platform (`platform.source-acquisition`, `platform.subscriptions`):**
acquisition runs in the shared source-pack runtime with budgets, cursors and
page receipts; monitors are knowledge subscriptions. Eurostat runs through the
existing `SDMXConnector` (`src/ingestion/connectors/dataset/sdmx.py`).

**Rights and terms:** each provider's reuse terms and attribution are recorded
in its contract and carried on every record. Portals without documented
machine access are not scraped.

## V1 acceptance

- Pinned fixtures for RKI, ECDC, WHO GHO and Eurostat cover reporting versus
  reference dates, case-definition revisions, provisional and revised values,
  estimates with bounds, series-break flags and regional codes.
- Re-running acquisition of the same release adds no revision; a new release
  adds a vintage and marks pinned views stale.
- A condition query expands through MeSH and ICD crosswalks with every step
  explained; unmapped terms are gaps, and `incompatible` mappings block
  expansion.
- A condition-within-boundary query returns series with the place resolution
  and boundary revision cited; as-of selection uses reporting date only.
- Vintage comparison and the reporting-delay view cite both vintages, show
  definition and geography breaks separately from data revisions, and compute
  no completeness estimate or nowcast.
- Two sources for the same period are returned side by side; an estimate and an
  observation stay separate kinds; a missing reporting date stays unknown.
- Links to trials and publications come only from explicit citations;
  mentions remain candidates until reviewed.
- A user-threshold monitor fires with the value, vintage and both dates cited,
  and a case-definition change is its own event.
- With the `surveillance` feature off, the Clinical Evidence acceptance suite
  is unchanged. Offline acceptance
  (`tests/unit/domains/test_surveillance_acceptance.py`, the demo) and bounded
  live checks (`docs/development/surveillance-evidence/`) are reported
  separately.

## Deferred

Defer nowcasting and outbreak modelling of any kind, age- or sex-standardised
rates computed by the pack, cross-source harmonised series, automatic
re-aggregation across boundary revisions, hospital and mortality registers
beyond the listed statistical offices, non-European surveillance systems, and
any advisory or alerting text beyond the user-configured threshold events.
Provider record counts do not imply complete coverage of a condition or
geography.

There is no standalone Public-health surveillance domain manifest, source-pack
manifest, enablement flag, or separate installation lifecycle in this scope.
